"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useApi } from "@/lib/api/use-api";

// Session-scoped dismissal: once the user saves or skips, we do not ask again
// this session. Optional + skippable by design — never a blocking gate, never a
// re-nag (Spec K6, K6-D-1: name is optional/skippable, our DB the source of truth).
const DISMISS_KEY = "op.nameNudge.dismissed";
const NAME_MAX = 100; // mirrors the API cap (K6-D-8)

/**
 * The optional "what should we call you?" nudge (Spec K6, T5).
 *
 * Shown ONLY when our DB has no name for the caller (both columns null — so a
 * user who typed their name at signup, incl. one seeded from Clerk claims, is
 * never asked). A non-blocking, dismissible card → ``PATCH /v1/me/profile``.
 * Uses design-system components, so it is themed + reduced-motion-safe (the F1
 * token contract silences motion structurally).
 */
export function NameNudge() {
  const t = useTranslations("profile.nameNudge");
  const api = useApi();
  const [show, setShow] = useState(false);
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (typeof window !== "undefined" && sessionStorage.getItem(DISMISS_KEY))
      return;
    let active = true;
    void (async () => {
      const { data } = await api.GET("/v1/me/profile");
      if (!active || !data) return;
      // Gated on null: never nag a named (or Clerk-seeded) user.
      if (!data.first_name && !data.last_name) setShow(true);
    })();
    return () => {
      active = false;
    };
  }, [api]);

  function dismiss() {
    if (typeof window !== "undefined") sessionStorage.setItem(DISMISS_KEY, "1");
    setShow(false);
  }

  async function save() {
    setSaving(true);
    try {
      await api.PATCH("/v1/me/profile", {
        body: {
          first_name: first.trim() || null,
          last_name: last.trim() || null,
        },
      });
      dismiss();
    } finally {
      setSaving(false);
    }
  }

  if (!show) return null;

  const empty = !first.trim() && !last.trim();

  return (
    <Card
      data-slot="name-nudge"
      className="fixed right-4 bottom-4 z-40 w-[min(22rem,calc(100vw-2rem))] shadow-lg"
    >
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
        <CardDescription>{t("subtitle")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Input
          aria-label={t("firstLabel")}
          placeholder={t("firstPlaceholder")}
          value={first}
          maxLength={NAME_MAX}
          disabled={saving}
          onChange={(e) => setFirst(e.target.value)}
        />
        <Input
          aria-label={t("lastLabel")}
          placeholder={t("lastPlaceholder")}
          value={last}
          maxLength={NAME_MAX}
          disabled={saving}
          onChange={(e) => setLast(e.target.value)}
        />
      </CardContent>
      <CardFooter className="gap-2">
        <Button onClick={save} disabled={saving || empty}>
          {t("save")}
        </Button>
        {/* Equal-access skip — no dark pattern; dismissed for the session. */}
        <Button variant="ghost" onClick={dismiss} disabled={saving}>
          {t("skip")}
        </Button>
      </CardFooter>
    </Card>
  );
}
