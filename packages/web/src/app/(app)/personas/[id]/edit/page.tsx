import { redirect } from "next/navigation";

/**
 * R11-B6 — the edit route is RETIRED: the persona page itself is the editor
 * (inline, autosaving — the owner-ruled consolidation). Old links land there.
 */
export default async function EditPersonaPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  redirect(`/personas/${id}`);
}
