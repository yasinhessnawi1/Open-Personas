"""Shared system-reply copy for the inbound flow (Spec C3 amendment #2, D-C3-X-flow-skeleton).

The "bot speaking" replies (not a persona): the list-and-instructions first-contact
(C1-D-7, the I/O half of the C1 ``ListAndInstructions`` decision), the ``/new``
confirmations, and the no-personas edge. **Platform-neutral plain text** — no markup,
no platform branching — so every text adapter (Telegram / Discord / Slack) reuses it
verbatim; the connector sends it as plain text (system replies carry no persona tag).

Lifted from the C2 ``telegram/replies.py`` into the framework's owned surface (it was
always platform-neutral). Owned surface — api-free; stdlib only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "DECLINE_MEDIA",
    "DECLINE_UNKNOWN",
    "DECLINE_VOICE",
    "NEW_CONVERSATION_MESSAGE",
    "NO_ACTIVE_TO_RESET_MESSAGE",
    "NO_PERSONAS_MESSAGE",
    "TURN_FAILED_MESSAGE",
    "render_list_and_instructions",
]

NEW_CONVERSATION_MESSAGE = "Started a fresh conversation — the slate's clear."
NO_ACTIVE_TO_RESET_MESSAGE = (
    "There's no active conversation to reset — just message a persona by name to start."
)
NO_PERSONAS_MESSAGE = (
    "You don't have any personas yet. Create one in your Open Persona web app, "
    "then come back here and message it by name."
)

# R9-073b: the honest, human, non-technical reply for a failed turn (a provider error, an
# unavailable model, a credits/timeout fault — anything). NEVER leak provider text, model
# ids, or account/billing detail here — this is a fixed, product-voice string, never built
# from ``str(exc)``, so there is nothing for a provider's own wording to leak into.
TURN_FAILED_MESSAGE = "Sorry, something went wrong on my end. Please try again in a moment."

# The non-text graceful declines (D-C2-6) — platform-NEUTRAL product-voice copy, so
# every adapter maps its own (platform-specific) non-text *kind* to these shared
# *strings*. The kinds stay surface-local (Telegram stickers ≠ Discord attachment
# flags ≠ Slack files); only this copy is genuinely common. (Telegram's
# ``non_text.py`` still carries byte-identical literals — a trivial close-out tidy
# to re-point; not re-pointed mid-batch to avoid an ungated merged-code touch.)
DECLINE_VOICE = "I can't listen to voice messages yet — send me a text message and I'll reply."
DECLINE_MEDIA = "I work over text for now — type me a message and I'm all yours."
DECLINE_UNKNOWN = "I work over text — send me a message and I'll reply."


def render_list_and_instructions(persona_names: Mapping[str, Sequence[str]]) -> str:
    """Render the list-and-instructions first-contact reply (C1-D-7, the I/O half).

    Lists the owner's persona display names and how to address one, in the product
    voice. The C1 ``decide_route`` decision picks WHEN to show this; this renders
    the content from the names the flow already loaded.

    Args:
        persona_names: ``persona_id`` → its addressable names; the first entry is
            the display name shown to the user.

    Returns:
        A plain-text reply (the connector renders no markup for system replies).
    """
    display_names = sorted(names[0] for names in persona_names.values() if names)
    if not display_names:
        return NO_PERSONAS_MESSAGE
    listed = ", ".join(display_names)
    example = display_names[0]
    return (
        f"You can talk to: {listed}. Start your message with a persona's name, "
        f'for example "{example}, hello".'
    )
