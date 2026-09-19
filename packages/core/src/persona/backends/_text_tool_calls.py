"""Keep a model's tool-call channel out of its text channel.

Some providers (observed on GLM-class models served through OpenRouter since
2026-09-16) sometimes write the tool call they meant to make as TEXT in the
assistant content stream instead of populating the structured ``tool_calls``
field. The user then sees raw markup inside the persona's prose::

    <tool_call>schedule_introspect(scope: "all", days_ahead: 7)The schedule is...
    <tool_call>datetime tool_call: </arg_value><arg_key>tool": "mcp_search", ...

The same turn often ALSO makes real structured calls, so this is a leak of one
channel into the other, not a dead tool.

This module is the provider-boundary guard. It has one job with two outcomes:

* **Recoverable**: the fragment names a tool the caller advertised and carries
  parseable arguments: it becomes a real :class:`~persona.schema.tools.ToolCall`
  the caller dispatches through the normal path (normal audit, normal card).
* **Not recoverable**: malformed, or naming a tool nobody offered: it is
  removed from the user-visible text and logged at WARNING with the provider,
  the model, the fragment LENGTH and the tool name if one was legible. The
  fragment's own text reaches neither a user surface nor the log, because it
  can carry call arguments.

Three leaked spellings are understood, all of them after a ``<tool_call>``
opener: the GLM ``<arg_key>``/``<arg_value>`` tag form, a JSON object form
(``{"name": ..., "arguments": {...}}``), and a call-signature form
(``name(key: value, ...)``). Anything else after the opener is dropped.

:class:`TextToolCallFilter` is the streaming form: the opener can and does land
across two provider deltas, so it holds back text that could still turn out to
be markup and releases it once it cannot. :func:`strip_text_tool_calls` is the
whole-message form used on the non-streaming path.

Deliberate trade-off: markup this module recognises is removed even when a
model was quoting it on purpose (for example answering a question about this
very bug). Showing it is the defect being fixed, so removal wins.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger
from persona.schema.tools import ToolCall

if TYPE_CHECKING:
    from collections.abc import Collection

__all__ = [
    "TEXT_TOOL_CALL_OPEN",
    "TextToolCallFilter",
    "strip_text_tool_calls",
]

_logger = get_logger("backends.text_tool_calls")

#: The opener that starts a leaked tool-call region.
TEXT_TOOL_CALL_OPEN = "<tool_call>"

#: The closer, when the model bothers to emit one.
_CLOSE = "</tool_call>"

#: Markup that only ever appears as part of a leaked tool call. Seen without an
#: opener when the provider's own parser already ate the opening tag, so these
#: are dropped from pass-through text on sight.
_ORPHAN_MARKERS = (
    _CLOSE,
    "<arg_key>",
    "</arg_key>",
    "<arg_value>",
    "</arg_value>",
)

#: An orphan OPENER takes its value with it. Dropping only the tags would leave
#: the argument itself ("Europe/Oslo") sitting in the prose as stray words.
_ORPHAN_PAIRS = {"<arg_key>": "</arg_key>", "<arg_value>": "</arg_value>"}

#: Everything that can start a region or an orphan. Used for the "could this
#: still become a marker?" prefix test while a tag is arriving across deltas.
#: No member is a prefix of another, so the order is not load bearing.
_ALL_MARKERS = (TEXT_TOOL_CALL_OPEN, *_ORPHAN_MARKERS)

#: A tool name as a model spells it: wire names plus the ``mcp:server:tool``
#: real name, which a model occasionally echoes back instead of the wire one.
_NAME = r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}"

_HEAD_PAREN = re.compile(rf"\s*({_NAME})\s*\(")
_HEAD_BRACE = re.compile(r"\s*\{")
_HEAD_NAME = re.compile(rf"\s*({_NAME})")
_ARG_PAIR = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.DOTALL)


def _coerce(raw: str) -> Any:  # noqa: ANN401 (an argument value is any JSON type)
    """Read one argument value, preferring JSON and falling back to the literal."""
    text = raw.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def _scan_balanced(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the ``closer`` that balances ``text[start]``, or ``-1``.

    Quote aware, so a bracket inside a JSON string does not move the depth.
    ``-1`` means the region has not closed in the text seen so far, which on the
    streaming path means "wait for more" rather than "malformed".
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif in_string:
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _split_top_level(text: str) -> list[str]:
    """Split a call signature's argument list on its top-level commas."""
    parts: list[str] = []
    depth = 0
    in_string = False
    escape = False
    start = 0
    for i, ch in enumerate(text):
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif in_string:
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p for p in parts if p.strip()]


def _parse_region(region: str) -> tuple[str, dict[str, Any]] | None:
    """Read ``(name, args)`` out of one leaked region, or ``None`` if unreadable.

    ``region`` is everything between the ``<tool_call>`` opener and whatever
    terminated it (the closer, a balanced bracket, a blank line, or the end of
    the message). It is NOT trusted: every shape below must fully account for
    itself or the whole region is declared unreadable.
    """
    # Shape 1, the GLM tag form: a bare name then <arg_key>/<arg_value> pairs.
    pairs = _ARG_PAIR.findall(region)
    if pairs:
        head = _HEAD_NAME.match(region)
        if head is None:
            return None
        return head.group(1), {k.strip(): _coerce(v) for k, v in pairs}

    # Shape 2, a JSON object, with or without the OpenAI function wrapper.
    brace = _HEAD_BRACE.match(region)
    if brace is not None:
        end = _scan_balanced(region, brace.end() - 1, "{", "}")
        if end == -1:
            return None
        try:
            obj = json.loads(region[brace.end() - 1 : end])
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        inner = obj.get("function")
        candidate = inner if isinstance(inner, dict) else obj
        name = candidate.get("name") or candidate.get("tool")
        if not isinstance(name, str) or not name:
            return None
        raw_args: Any = None
        for key in ("arguments", "args", "parameters"):
            if key in candidate:
                raw_args = candidate[key]
                break
        if isinstance(raw_args, str):
            raw_args = _coerce(raw_args)
        return name, raw_args if isinstance(raw_args, dict) else {}

    # Shape 3, a call signature: name(key: value, key: value).
    paren = _HEAD_PAREN.match(region)
    if paren is not None:
        end = _scan_balanced(region, paren.end() - 1, "(", ")")
        if end == -1:
            return None
        args: dict[str, Any] = {}
        for part in _split_top_level(region[paren.end() : end - 1]):
            key, sep, value = part.partition(":")
            if not sep:
                key, sep, value = part.partition("=")
            if not sep or not key.strip():
                return None
            args[key.strip().strip("\"'")] = _coerce(value)
        return paren.group(1), args

    return None


def _terminator(buffer: str, *, at_end: bool) -> int | None:
    """Index just past the end of the leaked region inside ``buffer``.

    ``None`` means "the region has not finished arriving": only possible while
    streaming; ``at_end=True`` (the provider's stream closed, or a whole message
    was handed in) makes the rest of the buffer the region.

    The terminator is whichever of these lands first: an explicit ``</tool_call>``,
    the bracket that balances a JSON or call-signature head, or a blank line. The
    blank line is the backstop for the mangled shapes that close nothing at all;
    it bounds the damage to one paragraph rather than the rest of the reply.
    """
    candidates: list[int] = []

    close = buffer.find(_CLOSE)
    if close != -1:
        candidates.append(close + len(_CLOSE))

    paren = _HEAD_PAREN.match(buffer)
    if paren is not None:
        end = _scan_balanced(buffer, paren.end() - 1, "(", ")")
        if end != -1:
            candidates.append(end)

    brace = _HEAD_BRACE.match(buffer)
    if brace is not None:
        end = _scan_balanced(buffer, brace.end() - 1, "{", "}")
        if end != -1:
            candidates.append(end)

    blank = buffer.find("\n\n")
    if blank != -1:
        candidates.append(blank)

    if candidates:
        return min(candidates)
    return len(buffer) if at_end else None


class TextToolCallFilter:
    """Streaming guard: strips (or recovers) leaked tool-call markup in order.

    One instance per model round. ``feed`` takes each provider text delta and
    returns the text the consumer may show plus any calls recovered; ``finish``
    flushes whatever is still held when the stream closes.

    Text is held back only while it could still be markup: the tail of a delta
    that is a proper prefix of ``<tool_call>`` waits for the next delta rather
    than streaming a half-written tag.

    Args:
        known_tool_names: The names advertised to the model this round (wire
            names). A recovered call whose name is not among them is stripped
            instead of dispatched, so a hallucinated name never reaches dispatch.
        provider: Provider id, for the WARNING.
        model: Model id, for the WARNING.
    """

    def __init__(
        self,
        *,
        known_tool_names: Collection[str],
        provider: str,
        model: str,
    ) -> None:
        self._known = frozenset(known_tool_names)
        self._provider = provider
        self._model = model
        self._held = ""
        self._region = ""
        self._capturing = False

    def feed(self, chunk: str) -> tuple[str, list[ToolCall]]:
        """Consume one provider text delta.

        Args:
            chunk: The provider's ``delta.content`` fragment.

        Returns:
            ``(safe_text, recovered_calls)``. ``safe_text`` may be empty while
            markup is still arriving.
        """
        if not chunk:
            return "", []
        return self._run(chunk, at_end=False)

    def finish(self) -> tuple[str, list[ToolCall]]:
        """Flush held text at stream end.

        Returns:
            ``(safe_text, recovered_calls)`` for whatever was still buffered. An
            unterminated region is resolved here against everything it captured.
        """
        return self._run("", at_end=True)

    def _run(self, chunk: str, *, at_end: bool) -> tuple[str, list[ToolCall]]:
        out: list[str] = []
        calls: list[ToolCall] = []
        buffer = self._held + chunk
        self._held = ""

        while True:
            if self._capturing:
                self._region += buffer
                buffer = ""
                end = _terminator(self._region, at_end=at_end)
                if end is None:
                    return "".join(out), calls
                recovered = self._resolve(self._region[:end])
                if recovered is not None:
                    calls.append(recovered)
                buffer = self._region[end:]
                self._region = ""
                self._capturing = False
                continue

            consumed, text, opened = self._scan_pass(buffer, at_end=at_end)
            out.append(text)
            buffer = buffer[consumed:]
            if opened:
                self._capturing = True
                continue
            self._held = buffer
            break

        return "".join(out), calls

    def _scan_pass(self, buffer: str, *, at_end: bool) -> tuple[int, str, bool]:
        """Scan pass-through text up to the next marker.

        Returns:
            ``(consumed, safe_text, opened_a_region)``. When nothing opened and
            ``consumed < len(buffer)``, the remainder is a partial marker to hold.
        """
        out: list[str] = []
        i = 0
        while True:
            nxt = buffer.find("<", i)
            if nxt == -1:
                out.append(buffer[i:])
                return len(buffer), "".join(out), False
            out.append(buffer[i:nxt])
            rest = buffer[nxt:]
            if rest.startswith(TEXT_TOOL_CALL_OPEN):
                return nxt + len(TEXT_TOOL_CALL_OPEN), "".join(out), True
            orphan = next((m for m in _ORPHAN_MARKERS if rest.startswith(m)), None)
            if orphan is not None:
                closer = _ORPHAN_PAIRS.get(orphan)
                end = rest.find(closer, len(orphan)) if closer else -1
                i = nxt + (len(orphan) if end == -1 else end + len(closer or ""))
                continue
            if any(m.startswith(rest) for m in _ALL_MARKERS):
                if not at_end:
                    # Could still become a marker once the next delta lands.
                    # This includes a lone trailing "<": providers do split a
                    # delta there, and emitting it would leak the tag that
                    # follows (the rest of it carries no "<" to re-detect on).
                    return nxt, "".join(out), False
                if len(rest) > 1:
                    # The stream ended mid-tag. A truncated marker is markup,
                    # not prose, so it goes.
                    return len(buffer), "".join(out), False
                # A lone "<" at the very end of the stream: far likelier text.
            out.append("<")
            i = nxt + 1

    def _resolve(self, region: str) -> ToolCall | None:
        """Turn one captured region into a real call, or drop it with a WARNING."""
        body = region[: -len(_CLOSE)] if region.endswith(_CLOSE) else region
        parsed = _parse_region(body)
        if parsed is not None:
            name = self._resolve_name(parsed[0])
            if name is not None:
                _logger.info(
                    "recovered a tool call the model wrote as text",
                    provider=self._provider,
                    model=self._model,
                    tool=name,
                )
                return ToolCall(name=name, args=parsed[1], call_id="")
        _logger.warning(
            "stripped a malformed tool-call fragment from model text "
            "(fragment withheld: it can carry call arguments)",
            provider=self._provider,
            model=self._model,
            fragment_chars=len(region),
            named_tool=parsed[0] if parsed is not None else None,
        )
        return None

    def _resolve_name(self, name: str) -> str | None:
        """The advertised name behind what the model wrote, or ``None``.

        Accepts the wire name directly, and the ``mcp:server:tool`` real name a
        model sometimes echoes instead (imported lazily: ``persona.tools`` pulls
        in every built-in tool, and ``persona.tools.toolbox`` imports back into
        ``persona.backends``).
        """
        if name in self._known:
            return name
        from persona.tools.toolbox import wire_tool_name

        wire = wire_tool_name(name)
        return wire if wire in self._known else None


def strip_text_tool_calls(
    content: str,
    *,
    known_tool_names: Collection[str],
    provider: str,
    model: str,
) -> tuple[str, list[ToolCall]]:
    """Whole-message form of :class:`TextToolCallFilter` (the non-streaming path).

    Args:
        content: The assistant message content the provider returned.
        known_tool_names: The names advertised to the model (wire names).
        provider: Provider id, for the WARNING.
        model: Model id, for the WARNING.

    Returns:
        ``(clean_content, recovered_calls)``. ``content`` is returned unchanged
        when it carries no leaked markup.
    """
    if TEXT_TOOL_CALL_OPEN not in content and not any(m in content for m in _ORPHAN_MARKERS):
        return content, []
    f = TextToolCallFilter(known_tool_names=known_tool_names, provider=provider, model=model)
    text, calls = f.feed(content)
    tail, more = f.finish()
    return text + tail, [*calls, *more]
