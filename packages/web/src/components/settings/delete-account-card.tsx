"use client";

/**
 * R9-284 T9: the user deletes their account (D-R9-284-erase ruling 2, cloud only).
 *
 * Immediate, with no grace period (ruling 3), so the card says plainly what goes and what is
 * kept before it asks, and the user confirms by typing their email (or the phrase, when the
 * account has no real email). The server checks the same rule; this only keeps the button off
 * until the text matches. On success the session is signed out and the user lands on the
 * public goodbye page: every later request from this account answers 410.
 */

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useAccount } from "@/auth";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { unwrap } from "@/lib/api/client";
import { useApi } from "@/lib/api/use-api";

/** What an account without a real email types (mirrors the api's ``CONFIRM_PHRASE``). */
export const CONFIRM_PHRASE = "delete my account";
const NOREPLY = "@users.noreply";

/** What the user must type to confirm: their email, or the phrase without a real one. */
export function confirmationFor(email: string | null): string {
  if (!email || email.endsWith(NOREPLY)) return CONFIRM_PHRASE;
  return email.trim().toLowerCase();
}

export function DeleteAccountCard() {
  const t = useTranslations("settings");
  const api = useApi();
  const account = useAccount();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  const expected = confirmationFor(account.email);
  const matches = typed.trim().toLowerCase() === expected;

  async function erase() {
    setBusy(true);
    setFailed(false);
    try {
      await unwrap(
        await api.POST("/v1/me/erase", { body: { confirm: typed.trim() } }),
      );
    } catch {
      setFailed(true);
      setBusy(false);
      return;
    }
    account.signOut?.();
    router.replace("/account-deleted");
  }

  return (
    <Card
      id="delete-account"
      className="gap-3 p-5 scroll-mt-20"
      data-slot="settings-delete-account"
    >
      <h2 className="type-caption font-mono text-muted-foreground uppercase">
        {t("deleteAccount")}
      </h2>
      <p className="type-body max-w-prose text-muted-foreground">
        {t("deleteAccountExplain")}
      </p>
      <p className="type-body max-w-prose text-muted-foreground">
        {t("deleteAccountKept")}
      </p>
      {open ? (
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (matches && !busy) void erase();
          }}
        >
          <label
            htmlFor="delete-account-confirm"
            className="type-ui flex flex-col gap-1.5"
          >
            {t("deleteAccountConfirmLabel", { expected })}
          </label>
          <Input
            id="delete-account-confirm"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            data-slot="delete-account-confirm"
          />
          {failed ? (
            <p role="alert" className="type-ui text-destructive">
              {t("deleteAccountFailed")}
            </p>
          ) : null}
          <div className="flex gap-2">
            <Button
              type="submit"
              variant="destructive"
              disabled={!matches || busy}
              data-slot="delete-account-submit"
            >
              {busy ? t("deleteAccountWorking") : t("deleteAccountConfirm")}
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setOpen(false);
                setTyped("");
                setFailed(false);
              }}
            >
              {t("deleteAccountCancel")}
            </Button>
          </div>
        </form>
      ) : (
        <div>
          <Button
            type="button"
            variant="destructive"
            onClick={() => setOpen(true)}
            data-slot="delete-account-start"
          >
            {t("deleteAccountStart")}
          </Button>
        </div>
      )}
    </Card>
  );
}
