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

`persona-connectors` reuses the API's reply producing chat flow and the identity
tagged delivery router **in process**, with the same per user isolation contextvar
the worker uses. It can be hosted two ways, and the composition is identical either
way:

- **as its own long lived process** (`python -m persona_connectors`), which serves
  the webhook and OAuth surfaces on its own port;
- **inside the api process**, enabled with `PERSONA_API_EMBED_CONNECTORS` (default
  off). The transports run as supervised tasks in the api's lifespan and their
  routes are mounted on the api's own port, so one process holds one model stack
  instead of two. The paths are unchanged, so provider registrations do not move.

`persona_connectors.service` is the shared composition root both hosts call. It
returns everything needed to run the connectors and leaves the decision of *how* to
run them to the host, which is why the same code serves both shapes.

The owned surface (`persona_connectors.domain`) is import decoupled from
`persona_api`. The api coupling lives only in `persona_connectors.composition` and
`persona_connectors.service`, so a future extraction is a dependency swap, not a
reshape.

---

Part of [Open Persona](../../README.md). See the root README for the full product
tour and quick start.
