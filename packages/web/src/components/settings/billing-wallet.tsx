"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { Card } from "@/components/ui/card";
import { unwrap } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { useApi } from "@/lib/api/use-api";

type Wallet = components["schemas"]["WalletResponse"];

/**
 * Spec M5 (T2c) — the caller's wallet on the billing page (D-M5-12).
 *
 * Reads `GET /v1/me/wallet`, the endpoint M4 built for this page and that had zero web
 * consumers until now (§1c.8). It carries everything the page needs about THIS user, so
 * the page never recomputes anything the server already decided — in particular
 * `low_balance` and `low_balance_threshold` come from the API (the wallet applies the
 * per-plan 20% rule), never from a client-side comparison that would drift the moment the
 * rule changes (D-M5-11).
 *
 * **Post-checkout re-poll (D-M5-2).** On `?checkout=success` Stripe has redirected the
 * browser back, but the credit GRANT happens in the webhook, which may not have landed
 * yet. So this bounded-re-polls the wallet until the balance MOVES from what it was on
 * arrival, then shows the real number. It never renders a fabricated or optimistic
 * balance: before the grant lands the page says the payment is confirmed and the balance
 * is on its way, which is true, rather than asserting credits that are not there yet
 * (the R9-050 honest-loading rule).
 *
 * The poll is bounded on both axes — a fixed interval and a hard attempt ceiling — so a
 * webhook that never lands degrades to "it is taking longer than usual" instead of
 * spinning forever. Cleared on unmount; no zombie tick.
 */

/** Poll cadence + ceiling: ~30s total, enough for a webhook round trip without hanging. */
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 15;

export interface BillingWalletProps {
  /** True on the `?checkout=success` return, which arms the bounded re-poll. */
  readonly awaitingGrant?: boolean;
  /** Injected in tests so the poll runs without real timers. */
  readonly pollIntervalMs?: number;
}

export function BillingWallet({
  awaitingGrant = false,
  pollIntervalMs = POLL_INTERVAL_MS,
}: BillingWalletProps) {
  const t = useTranslations("billing");
  const api = useApi();
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [failed, setFailed] = useState(false);
  const [grantLanded, setGrantLanded] = useState(false);
  const [pollExhausted, setPollExhausted] = useState(false);
  // The balance as it stood when we arrived back from Stripe. The grant is "landed" when
  // the served balance differs from this — an observed transition, never an assumption.
  const arrivalBalance = useRef<number | null>(null);

  const load = useCallback(async (): Promise<Wallet | null> => {
    try {
      const next = await unwrap(await api.GET("/v1/me/wallet"));
      setWallet(next);
      setFailed(false);
      return next;
    } catch {
      setFailed(true);
      return null;
    }
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    void load().then((first) => {
      if (!cancelled && first && arrivalBalance.current === null) {
        arrivalBalance.current = first.total_balance;
      }
    });
    return () => {
      cancelled = true;
    };
  }, [load]);

  useEffect(() => {
    if (!awaitingGrant || grantLanded || pollExhausted) return;
    let attempts = 0;
    let cancelled = false;
    const id = setInterval(() => {
      attempts += 1;
      void load().then((next) => {
        if (cancelled || !next) return;
        const baseline = arrivalBalance.current;
        if (baseline !== null && next.total_balance !== baseline) {
          setGrantLanded(true);
        } else if (attempts >= POLL_MAX_ATTEMPTS) {
          setPollExhausted(true);
        }
      });
    }, pollIntervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [awaitingGrant, grantLanded, pollExhausted, load, pollIntervalMs]);

  if (failed && wallet === null) {
    return (
      <Card className="p-6" data-slot="billing-wallet-error">
        <p className="type-body text-muted-foreground">
          {t("walletUnavailable")}
        </p>
      </Card>
    );
  }

  if (wallet === null) {
    return (
      <Card className="p-6" data-slot="billing-wallet-loading">
        <p className="type-body text-muted-foreground">{t("walletLoading")}</p>
      </Card>
    );
  }

  // Only while we are genuinely waiting on the webhook. Once the balance moves (or the
  // poll gives up) the real number below is the whole story.
  const pending = awaitingGrant && !grantLanded && !pollExhausted;

  return (
    <Card
      className="p-6"
      data-slot="billing-wallet"
      data-pending-grant={String(pending)}
    >
      <h2 className="type-caption font-mono uppercase text-muted-foreground">
        {t("balanceLabel")}
      </h2>
      <p className="type-display mt-2" data-slot="billing-wallet-balance">
        {wallet.total_balance}
      </p>
      <p className="type-caption mt-1 text-muted-foreground">
        {t("balanceHint", {
          allowance: wallet.allowance_balance,
          payg: wallet.total_balance - wallet.allowance_balance,
        })}
      </p>

      {pending ? (
        <p className="type-body mt-3" data-slot="billing-wallet-pending">
          {t("grantPending")}
        </p>
      ) : null}
      {pollExhausted && !grantLanded ? (
        <p className="type-body mt-3" data-slot="billing-wallet-slow">
          {t("grantSlow")}
        </p>
      ) : null}

      {wallet.low_balance && wallet.total_balance > 0 ? (
        <p className="type-body mt-3" data-slot="billing-wallet-low">
          {t("lowBalance")}
        </p>
      ) : null}
    </Card>
  );
}
