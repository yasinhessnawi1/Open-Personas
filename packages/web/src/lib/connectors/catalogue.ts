import type { LucideIcon } from "lucide-react";
import {
  Hash,
  Mail,
  MessageCircle,
  MessageSquare,
  MessagesSquare,
  Send,
} from "lucide-react";

/**
 * Spec C6 — the static six-platform catalogue the connectors surface renders.
 *
 * The web owns this list (it is the same six platforms every render); the API's
 * `GET /v1/me/connectors` returns only the *connected* bindings, which the manager
 * merges against this catalogue to show connected / not-connected state per platform.
 *
 * `identityKind` drives how the connected identity is *displayed* (C6-KL-1): phone/email
 * bind a human-recognisable envelope shown verbatim; Telegram/Discord/Slack bind an opaque
 * platform id shown honestly as "ID <id>" (a `display_name` handle is a named C2/C3
 * fast-follow, not v1). `mechanism` is the connect flow each platform uses (C6-D-1) — the
 * middle step of the one coherent connect → step → progress → confirmed shape.
 */
export type ConnectorPlatform =
  | "telegram"
  | "whatsapp"
  | "sms"
  | "discord"
  | "slack"
  | "email";

export type IdentityKind = "handle" | "phone" | "email" | "id";
export type LinkMechanism = "deep_link" | "oauth" | "code";
/** R11-B4: the catalogue's section grouping (kit `connectors.html`). */
export type ConnectorCategory = "messaging" | "email";

export interface ConnectorMeta {
  readonly key: ConnectorPlatform;
  readonly icon: LucideIcon;
  readonly identityKind: IdentityKind;
  readonly mechanism: LinkMechanism;
  /** The kit's category section this platform files under (R11-B4). */
  readonly category: ConnectorCategory;
  /**
   * Whether this platform is connectable on the running deployment today (the
   * wired-capability rule, T10) — `false` renders "Coming soon" instead of a Connect
   * button that cannot succeed. The first-connection guidance invites only ready
   * platforms; the ConnectFlow's 503 fail-soft (T7) remains the safety net regardless.
   *
   * Two things gate readiness, and BOTH must hold:
   *  1. the connector service mounts the platform's link/OAuth routes, and
   *  2. the platform's own provider side is actually usable on this deployment.
   *
   * Current state (2026-07-29): Discord + Slack flipped to `true` — R9-061 mounted their
   * link + OAuth-callback routes for real (they had never been mounted in ANY transport),
   * verified live. WhatsApp + SMS flipped to `false` — their routes are mounted, but both
   * ride Twilio senders that require a paid Twilio subscription this deployment does not
   * have yet, so a Connect attempt cannot complete. Flip them back when the Twilio senders
   * are registered.
   */
  readonly backendReady: boolean;
}

export const CONNECTOR_CATALOGUE: readonly ConnectorMeta[] = [
  {
    key: "telegram",
    category: "messaging",
    icon: Send,
    identityKind: "id",
    mechanism: "deep_link",
    backendReady: true,
  },
  {
    key: "whatsapp",
    category: "messaging",
    icon: MessageCircle,
    identityKind: "phone",
    mechanism: "code",
    backendReady: false,
  },
  {
    key: "sms",
    category: "messaging",
    icon: MessageSquare,
    identityKind: "phone",
    mechanism: "code",
    backendReady: false,
  },
  {
    key: "discord",
    category: "messaging",
    icon: MessagesSquare,
    identityKind: "id",
    mechanism: "oauth",
    backendReady: true,
  },
  {
    key: "slack",
    category: "messaging",
    icon: Hash,
    identityKind: "id",
    mechanism: "oauth",
    backendReady: true,
  },
  {
    key: "email",
    category: "email",
    icon: Mail,
    identityKind: "email",
    mechanism: "code",
    backendReady: true,
  },
];
