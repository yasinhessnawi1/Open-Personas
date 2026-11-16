"use client";

/**
 * R9-284 T9: the user deletes their account (D-R9-284-erase ruling 2, cloud only).
 *
 * Immediate, with no grace period (ruling 3), so the card says plainly what goes and what is
 * kept before it asks, and the user confirms by typing what the server expects: the email the
 * account was stored with, or a phrase when it has no real email (review M5: read from
 * ``GET /v1/me/erase``, not the identity provider's primary email, which can differ). The
 * server checks the same string; this only keeps the button off until the text matches. On success the session is signed out and the user lands on the
 * public goodbye page: every later request from this account answers 410.
 */

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { useAccount } from "@/auth";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ACCOUNT_DELETED_PATH, unwrap } from "@/lib/api/client";
import { useApi } from "@/lib/api/use-api";

/** How long the card waits on the wallet read or the sign-out before going on (round 3 LOW-7). */
export const SETTLE_TIMEOUT_MS = 10_000;

/** ``promise``, or a rejection once ``ms`` has passed without it settling. */
function within<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timed out")), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
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
  const [expected, setExpected] = useState<string | null>(null);
  // Whether there is money the deletion forfeits (a paid plan, or prepaid credits left), so
  // the no-refund line shows only where it applies. Unknown until read; a wallet that cannot
  // be read counts as yes, since saying it needlessly costs less than not saying it.
  const [paid, setPaid] = useState<boolean | null>(null);

  // Read the expected confirmation once the form opens; until then nothing can match.
  useEffect(() => {
    if (!open || expected !== null) return;
    let live = true;
    void (async () => {
      try {
        const answer = await unwrap(await api.GET("/v1/me/erase"));
        if (live) setExpected(answer.confirm);
      } catch {
        if (live) setFailed(true);
      }
    })();
    return () => {
      live = false;
    };
  }, [open, expected, api]);

  useEffect(() => {
    if (!open || paid !== null) return;
    let live = true;
    void (async () => {
      try {
        // A wallet that has not answered in time counts as unknown: the line shows.
        const wallet = await unwrap(
          await within(api.GET("/v1/me/wallet"), SETTLE_TIMEOUT_MS),
        );
        if (live) {
          setPaid(
            wallet.plan_code !== "free" ||
              wallet.payg_lots.some((lot) => lot.credits_remaining > 0),
          );
        }
      } catch {
        if (live) setPaid(true);
      }
    })();
    return () => {
      live = false;
    };
  }, [open, paid, api]);

  const matches =
    expected !== null && typed.trim().toLowerCase() === expected.toLowerCase();

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
    // The session goes first (review L6), so nothing on the next page still holds it; Clerk
    // lands on the goodbye page itself. Whatever sign-out does, the account is gone, so the
    // user is always taken there (review L-6).
    try {
      await within(
        Promise.resolve(
          account.signOut?.({ redirectUrl: ACCOUNT_DELETED_PATH }),
        ),
        SETTLE_TIMEOUT_MS,
      );
    } catch {
      // A failed sign-out leaves a session for an account that no longer exists; the API
      // refuses it (410) and the goodbye page holds nothing of it.
    } finally {
      router.replace(ACCOUNT_DELETED_PATH);
    }
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
          {paid ? (
            <p
              className="type-body max-w-prose text-foreground"
              data-slot="delete-account-refund"
            >
              {t("deleteAccountRefund")}
            </p>
          ) : null}
          <label
            htmlFor="delete-account-confirm"
            className="type-ui flex flex-col gap-1.5"
          >
            {expected === null
              ? t("deleteAccountWorking")
              : t("deleteAccountConfirmLabel", { expected })}
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
              // Review L-5: not before the wallet answer is in, so the refund line (or its
              // absence) is settled before the user can confirm.
              disabled={!matches || busy || paid === null}
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
