"use client";

import { Check, Copy } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";

/**
 * Spec C6 — a small copy affordance for the connect steps. Copies the value VERBATIM (the
 * canonical form the server will match — e.g. the raw OTP code, never a readability-formatted
 * variant, T8 bar 4), and confirms with a transient checkmark.
 */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const t = useTranslations("connectors");
  const [copied, setCopied] = useState(false);

  const onCopy = useCallback(() => {
    if (!value) return;
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }, [value]);

  return (
    <Button variant="outline" size="sm" onClick={onCopy} aria-label={label}>
      {copied ? (
        <Check className="size-4" aria-hidden />
      ) : (
        <Copy className="size-4" aria-hidden />
      )}
      {copied ? t("connect.copied") : label}
    </Button>
  );
}
