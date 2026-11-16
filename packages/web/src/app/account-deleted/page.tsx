import Link from "next/link";
import { getTranslations } from "next-intl/server";

/**
 * R9-284 T9: where a user lands after deleting their account. Public (not behind sign-in):
 * the session is gone, and the account with it.
 */
export default async function AccountDeletedPage() {
  const t = await getTranslations("accountDeleted");
  return (
    <main
      className="mx-auto flex min-h-dvh max-w-prose flex-col justify-center gap-4 p-6"
      data-slot="account-deleted"
    >
      <h1 className="type-display">{t("title")}</h1>
      <p className="type-body text-muted-foreground">{t("body")}</p>
      <p className="type-body text-muted-foreground">{t("kept")}</p>
      <Link
        href="/"
        className="type-ui text-primary underline-offset-4 hover:underline"
      >
        {t("home")}
      </Link>
    </main>
  );
}
