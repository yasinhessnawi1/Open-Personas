"use client";

import type { ConnectorMeta } from "@/lib/connectors/catalogue";
import type { ConnectorLinkArtifact } from "@/lib/connectors/connect-flow-machine";
import { CodeStep } from "./code-step";
import { DeepLinkStep } from "./deep-link-step";
import { OAuthStep } from "./oauth-step";

export interface PlatformStepProps {
  artifact: ConnectorLinkArtifact;
  meta: ConnectorMeta;
}

/**
 * Spec C6 — the middle `PlatformStep` slot of the one coherent ConnectFlow frame (C6-D-1).
 * Dispatches on the platform's mechanism to the concrete step: deep-link (T6, Telegram),
 * OAuth (T7, Discord/Slack), code (T8, WhatsApp/SMS/Email). The frame around every one is
 * identical — this is the only thing that varies (the §3 coherence property).
 */
export function PlatformStep({ artifact, meta }: PlatformStepProps) {
  switch (meta.mechanism) {
    case "deep_link":
      return <DeepLinkStep artifact={artifact} meta={meta} />;
    case "oauth":
      return <OAuthStep artifact={artifact} meta={meta} />;
    case "code":
      return <CodeStep artifact={artifact} meta={meta} />;
    default:
      return null;
  }
}
