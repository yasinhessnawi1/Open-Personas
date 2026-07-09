"use client";

import { ChevronDown } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useConfirm } from "@/components/providers/confirm-provider";
import { useNotify } from "@/components/providers/notification-provider";
import { buttonVariants } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useApi } from "@/lib/api/use-api";
import { useSidebarRefresh } from "@/lib/hooks/use-sidebar-refresh";
import { renameInIdentity } from "@/lib/persona";
import { cn } from "@/lib/utils";

/**
 * Spec F5 T10 — persona-detail "Manage" action menu.
 *
 * Client island composed into the otherwise-server-rendered persona
 * detail page. Adds Duplicate + Delete actions (per D-F5-4 / D-F5-X-persona-
 * duplicate-flow) + an entry point routing to the Spec 10 authoring flow
 * for richer edits.
 *
 * T11 lands the richer F2 `<Sheet>` confirmation surfaces; T10 ships the
 * structural surface so the detail page extension is observable end-to-end.
 */
export interface PersonaDetailManageMenuProps {
  personaId: string;
  personaName: string;
}

export function PersonaDetailManageMenu({
  personaId,
  personaName,
}: PersonaDetailManageMenuProps) {
  const t = useTranslations("personas");
  const tc = useTranslations("confirm");
  const tn = useTranslations("notifications");
  const confirm = useConfirm();
  const { notify } = useNotify();
  const router = useRouter();
  const refreshSidebar = useSidebarRefresh();
  const api = useApi();
  const [busy, setBusy] = useState(false);

  async function handleDuplicate() {
    if (busy) return;
    const ok = await confirm({
      title: tc("duplicateTitle", { name: personaName }),
      description: t("library.duplicateConfirm", { name: personaName }),
      confirmLabel: tc("duplicate"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      // Fetch the original full YAML + mutate identity.name per D-F5-4.
      const original = await api.GET("/v1/personas/{persona_id}", {
        params: { path: { persona_id: personaId } },
      });
      if (!original.data) return;
      const newYaml = renameInIdentity(
        original.data.yaml,
        `${personaName} (copy)`,
      );
      await api.POST("/v1/personas", {
        body: { yaml: newYaml, avatar_url: null },
      });
      notify({
        level: "success",
        title: tn("duplicated", { name: personaName }),
      });
      router.push("/personas");
      // R9-012: the shared sidebar-refresh seam — rail + badges re-resolve.
      refreshSidebar();
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (busy) return;
    const ok = await confirm({
      title: tc("deleteTitle", { name: personaName }),
      description: t("library.deleteConfirm", { name: personaName }),
      confirmLabel: tc("delete"),
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await api.DELETE("/v1/personas/{persona_id}", {
        params: { path: { persona_id: personaId } },
      });
      notify({ level: "success", title: tn("deleted", { name: personaName }) });
      router.push("/personas");
      refreshSidebar(); // R9-012: shared sidebar-refresh seam
    } finally {
      setBusy(false);
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={t("detail.manageLabel", { name: personaName })}
        className={cn(buttonVariants({ variant: "outline" }), "gap-2")}
        data-slot="persona-detail-manage"
      >
        {t("detail.manage")}
        <ChevronDown className="size-4" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem
          render={<Link href={`/personas/${personaId}/edit`} />}
        >
          {t("detail.editViaAuthoring")}
        </DropdownMenuItem>
        <DropdownMenuItem
          render={<Link href={`/personas/${personaId}/files`} />}
        >
          {t("detail.files")}
        </DropdownMenuItem>
        <DropdownMenuItem disabled={busy} onClick={handleDuplicate}>
          {t("library.duplicate")}
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={busy}
          variant="destructive"
          onClick={handleDelete}
        >
          {t("library.delete")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
