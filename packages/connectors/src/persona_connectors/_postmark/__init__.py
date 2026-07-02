"""Postmark provider integration (Spec C5) — the ESP client + inbound-webhook auth.

The provider stays BEHIND the adapter (C1-D-1), mirroring ``_twilio``: a thin httpx
send client + the Basic-Auth webhook verifier. Swapping ESPs is contained here.
"""
