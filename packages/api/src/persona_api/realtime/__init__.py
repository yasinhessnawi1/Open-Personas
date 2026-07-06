"""Spec A11 — real-time delivery: the persistent user-level SSE push channel.

The out-of-turn channel (``GET /v1/me/events``) that makes a background delivery
surface live (the bell + the open chat, no reload — closes R4-C1-23). T1 ships the
closed event catalogue (:mod:`.events`), the transport envelope (:mod:`.envelope`),
and the per-user log + reconnect resolver (:mod:`.log`).
"""
