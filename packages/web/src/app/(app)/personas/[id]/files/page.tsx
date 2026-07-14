import { redirect } from "next/navigation";

/**
 * R11-B6 rider (owner-ruled) — files are an OVERLAY on the persona page now,
 * not a route. Old links land on the page (open Files from the rail).
 */
export default async function PersonaFilesPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  redirect(`/personas/${id}`);
}
