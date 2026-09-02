# persona-connectors

> Message your persona by name on Telegram, WhatsApp, SMS, or email. Discord and Slack are next.

**License:** [PolyForm Noncommercial 1.0.0](LICENSE). This is the application
layer, not the MIT engine.

`persona-connectors` makes a persona reachable on the chat apps you already use.
The product model is *my persona, reachable by me*: an authenticated extension of
your own account onto a platform, never a public bot. Ownership isolation is
identical to the web app's.

## What the framework owns

Everything shared across platforms lives in the framework, so each adapter is only
its platform's glue:

- the inbound, route, respond, outbound flow;
- the `Connector` protocol and the normalisation contracts (an email and SMS
  floor, with real time delivery, threads, and rich formatting as optional
  capabilities);
- the **per persona parallel conversation model**. Each persona holds at most one
  active conversation per user per channel; switching personas *suspends* the
  previous one rather than ending it; only `/new` or the idle timeout end a
  conversation;
- persona selection by name;
- account linking and identity mapping, which is the security spine;
- identity tagged outbound delivery.

## Adapters

| Platform | Status |
| --- | --- |
| Telegram | Connectable (deep link from the web app) |
| WhatsApp | Connectable (verification code) |
| SMS (Twilio) | Connectable (verification code) |
| Email (Postmark) | Connectable (verification code) |
| Discord | Coming soon. Adapter built, OAuth mounting in progress |
| Slack | Coming soon. Adapter built, OAuth mounting in progress |

Turn platforms on from the web app under **Settings → Connectors**. Each one shows
what it is and what it can do before you connect it.

## Architecture

`persona-connectors` runs as a separate long lived process, the third after
`persona-api` and `persona-voice`. It reuses the API's reply producing chat flow
and the identity tagged delivery router **in process**, with the same per user
isolation contextvar the worker uses.

The owned surface (`persona_connectors.domain`) is import decoupled from
`persona_api`. The api coupling lives only in `persona_connectors.composition`, so
a future extraction is a dependency swap, not a reshape.

---

Part of [Open Persona](../../README.md). See the root README for the full product
tour and quick start.
