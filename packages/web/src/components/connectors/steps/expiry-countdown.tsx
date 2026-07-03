"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

function remainingMs(expiresAt: string, nowMs: number): number {
  return Math.max(0, Date.parse(expiresAt) - nowMs);
}

function format(ms: number): string {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/**
 * Spec C6 (T6) — a live countdown to the server-authoritative `expires_at` (C6-D-8), shown
 * on link/code steps so the user knows the artifact's freshness. Display-only: the actual
 * expiry transition is the ConnectFlow poll's job (this never drives state). Its own 1s
 * interval is cleared on unmount — no zombie tick.
 */
export function ExpiryCountdown({
  expiresAt,
  now = Date.now,
}: {
  expiresAt: string;
  now?: () => number;
}) {
  const t = useTranslations("connectors");
  const [ms, setMs] = useState(() => remainingMs(expiresAt, now()));

  useEffect(() => {
    setMs(remainingMs(expiresAt, now()));
    const id = setInterval(() => setMs(remainingMs(expiresAt, now())), 1000);
    return () => clearInterval(id);
  }, [expiresAt, now]);

  return (
    <span
      className="type-caption text-muted-foreground"
      data-slot="expiry-countdown"
    >
      {ms <= 0
        ? t("connect.expiredShort")
        : t("connect.expiresIn", { time: format(ms) })}
    </span>
  );
}
