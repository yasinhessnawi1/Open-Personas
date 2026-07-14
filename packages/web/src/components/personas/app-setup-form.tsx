"use client";

import { Check, KeyRound } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useAuth } from "@/auth";
import { buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, createApiClient, unwrap } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import type { McpCatalogEntry } from "./persona-form";

const TEMPLATE = process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE;

/**
 * Spec N4 (Group D) — the credential-isolated app setup form.
 *
 * The user-facing half of the credential-isolation mechanism. It occupies N3's
 * reserved `needs-setup` slot for an **adoptable** catalog app (a remote app that
 * declares a credential), and on submit POSTs the credential **straight to the
 * store** via `POST /v1/personas/{id}/adopted-apps` — never through a persona
 * turn. The persona names the app + states the requirement; the user supplies the
 * secret here; the persona never receives it (N4-D-1). The connection url/auth are
 * derived from the catalog server-side (N4-D-10); this form sends only the secret.
 *
 * see-then-grant: rendered INSIDE the expanded card detail, AFTER the trust
 * disclosure — the user sees what the app is + where it connects before granting.
 * The credential is cleared on success and never persisted client-side.
 */
export function AppSetupForm({
  app,
  personaId,
  onAdopted,
}: {
  app: McpCatalogEntry;
  personaId: string;
  onAdopted?: () => void;
}) {
  const t = useTranslations("apps");
  const { getToken } = useAuth();
  const [credential, setCredential] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const title = app.displayName || app.name;
  // Schema-driven: the declared secret drives the field label + how-to-obtain help.
  const secret = app.secrets[0];
  const env = secret?.env ?? app.requiredEnv[0] ?? "";
  // Spec N7 (D-N7-2, owner-ratified): a SECRETLESS catalog app (e.g. an image app
  // needing no credential) still adopts through this form — the backend already
  // accepts `credential: null` (`create_image_server`) — so no input is shown and
  // submit is enabled immediately rather than gated on a credential that will
  // never exist.
  const secretless = app.secrets.length === 0;

  async function submit() {
    if (submitting || (!secretless && !credential.trim())) return;
    setSubmitting(true);
    setError(null);
    try {
      const jwt = await getToken(TEMPLATE ? { template: TEMPLATE } : undefined);
      const api = createApiClient(() => Promise.resolve(jwt));
      await unwrap(
        await api.POST("/v1/personas/{persona_id}/adopted-apps", {
          params: { path: { persona_id: personaId } },
          body: {
            catalog_name: app.name,
            credential: secretless ? null : credential,
          },
        }),
      );
      setCredential(""); // never persist the secret client-side
      setDone(true);
      onAdopted?.();
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setError(
        status === 409
          ? t("setupForm.errorAlready")
          : status === 403
            ? t("setupForm.errorNotVetted")
            : t("setupForm.error"),
      );
    } finally {
      setSubmitting(false);
    }
  }

  if (done) {
    return (
      <p
        className="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 p-2 text-xs text-primary"
        data-slot="app-setup-done"
      >
        <Check className="size-3.5 shrink-0" aria-hidden="true" />
        {t("setupForm.done", { name: title })}
      </p>
    );
  }

  return (
    <div
      className="flex flex-col gap-2 rounded-md border border-border bg-muted/40 p-2"
      data-slot="app-setup-form"
    >
      <p className="text-xs text-muted-foreground">{t("setupForm.intro")}</p>
      {secret?.description ? (
        <p className="text-xs text-muted-foreground" data-slot="app-setup-help">
          {secret.description}
        </p>
      ) : null}
      {secretless ? null : (
        <Input
          type="password"
          value={credential}
          onChange={(e) => setCredential(e.target.value)}
          placeholder={secret?.example || t("setupForm.credentialPlaceholder")}
          aria-label={
            env
              ? t("setupForm.credentialLabel", { env })
              : t("setupForm.credentialPlaceholder")
          }
          data-slot="app-setup-credential"
          autoComplete="off"
        />
      )}
      <button
        type="button"
        onClick={() => void submit()}
        disabled={submitting || (!secretless && !credential.trim())}
        className={cn(buttonVariants({ size: "sm" }), "w-fit gap-1.5")}
        data-slot="app-setup-submit"
      >
        <KeyRound className="size-3.5" aria-hidden="true" />
        {submitting ? t("setupForm.submitting") : t("setupForm.submit")}
      </button>
      {error ? (
        <p className="text-xs text-destructive" data-slot="app-setup-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}
