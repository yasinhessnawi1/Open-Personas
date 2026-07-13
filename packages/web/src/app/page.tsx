import { redirect } from "next/navigation";
import { auth } from "@/auth/server";

/**
 * Auth-aware product root (`/`).
 *
 *   - signed-out → redirect to the standalone marketing site
 *     (NEXT_PUBLIC_MARKETING_URL); falls back to the in-app /sign-in route when
 *     unset so the app stays usable standalone.
 *   - signed-in → redirect to /activity.
 *
 * R11-B1 (D-R11-1/2): the Home fast-launch dashboard is retired — Activity is
 * the featured landing surface. The states Home owned re-homed with it: the
 * new-user onboarding renders on /activity (confirmed zero personas), and the
 * quick-launch affordance returns as the Activity quick-access strip (D-R11-5,
 * B2). `/` stays outside the (app) route group so signed-out visitors redirect
 * before the shell mounts.
 */
export default async function RootPage() {
  const { userId } = await auth();

  if (!userId) {
    const marketingUrl = process.env.NEXT_PUBLIC_MARKETING_URL?.trim();
    redirect(
      marketingUrl && marketingUrl.length > 0 ? marketingUrl : "/sign-in",
    );
  }

  redirect("/activity");
}
