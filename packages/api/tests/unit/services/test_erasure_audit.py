"""The JSONL store audit is rewritten line by line, and only by newline (R9-284 review M-1).

``str.splitlines`` also splits on U+2028, U+2029, U+0085 and others, which pydantic's JSON
leaves raw inside a string. Splitting on them cut one event into fragments that did not parse,
so its free text survived and the record was corrupted, while the step reported success.
"""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 - a fixture annotation

import pytest
from persona_api.services.erasure_audit import scrub_store_audit_files

_OWNER = "user_alice"
_PERSONA = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.mark.parametrize("separator", [" ", " ", "\u0085", "\x1c", "\x0b"])
def test_a_reason_with_a_unicode_line_separator_is_scrubbed_whole(
    tmp_path: Path, separator: str
) -> None:
    reason = f"the words{separator}the user said"
    events = [
        {"persona_id": _PERSONA, "chunk_ids": ["c1"], "reason": reason},
        {"persona_id": _PERSONA, "chunk_ids": ["c2"], "reason": None},
    ]
    log = tmp_path / f"{_PERSONA}.jsonl"
    # As pydantic writes them: the separator stays raw inside the JSON string.
    log.write_bytes(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
    )

    changed = scrub_store_audit_files(tmp_path, owner_id=_OWNER, persona_id=_PERSONA)

    lines = log.read_bytes().decode("utf-8").split("\n")
    assert lines[-1] == ""
    parsed = [json.loads(line) for line in lines[:-1]]
    assert changed == 1
    assert [e["chunk_ids"] for e in parsed] == [["c1"], ["c2"]]
    assert all(e["reason"] is None for e in parsed)
