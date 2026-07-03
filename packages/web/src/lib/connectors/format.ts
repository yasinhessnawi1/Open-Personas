import type { useTranslations } from "next-intl";
import type { IdentityKind } from "./catalogue";

type Translate = ReturnType<typeof useTranslations<"connectors">>;

/**
 * Spec C6 — the connected identity, shown honestly per platform (C6-KL-1): phone/email
 * verbatim (human-recognisable); Telegram/Discord/Slack as "ID <id>" (the bound opaque id,
 * never naked — so a user recognises which account they linked / are severing). `handle` is
 * reserved for the C2/C3 `display_name` fast-follow. Shared by the card (T4) and the
 * disconnect confirm (T9) so both name the same account the same way.
 */
export function formatIdentity(
  kind: IdentityKind,
  identity: string,
  t: Translate,
): string {
  switch (kind) {
    case "id":
      return t("identity.id", { id: identity });
    case "handle":
      return `@${identity}`;
    default:
      return identity;
  }
}
