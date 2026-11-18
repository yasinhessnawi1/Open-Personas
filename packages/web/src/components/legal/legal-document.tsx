import Link from "next/link";
import { useTranslations } from "next-intl";
import { Markdown } from "@/components/ui/markdown";
import type { LegalDocument } from "@/content/legal/types";
import { LEGAL_ROUTES } from "@/lib/legal-routes";

/**
 * Renders one legal document (Privacy Policy or Terms of Service) from its content
 * module. The page chrome (version line, contents label, footer links) comes from the
 * `legal` catalogue namespace; the document text comes from `src/content/legal/*`.
 */
export function LegalDocumentView({ doc }: { doc: LegalDocument }) {
  const t = useTranslations("legal");
  return (
    <main
      className="mx-auto flex w-full max-w-prose flex-col gap-6 px-4 py-10 sm:px-6"
      data-slot="legal-document"
    >
      <header className="flex flex-col gap-2">
        <h1 className="type-display">{doc.title}</h1>
        <p className="type-ui text-muted-foreground">
          {t.rich("meta", {
            version: doc.version,
            date: doc.lastUpdated,
            time: (chunks) => <time dateTime={doc.lastUpdated}>{chunks}</time>,
          })}
        </p>
      </header>

      <Markdown>{doc.intro}</Markdown>

      <nav aria-label={t("contents")} className="flex flex-col gap-2">
        <h2 className="type-ui font-semibold">{t("contents")}</h2>
        <ol className="type-ui list-decimal space-y-1 pl-5">
          {doc.sections.map((section) => (
            <li key={section.id}>
              <a
                href={`#${section.id}`}
                className="text-primary underline-offset-4 hover:underline"
              >
                {section.heading}
              </a>
            </li>
          ))}
        </ol>
      </nav>

      {doc.sections.map((section, index) => (
        <section
          key={section.id}
          aria-labelledby={section.id}
          className="flex flex-col gap-2"
        >
          <h2 id={section.id} className="type-heading scroll-mt-6">
            {index + 1}. {section.heading}
          </h2>
          <Markdown>{section.body}</Markdown>
        </section>
      ))}

      <LegalLinks />
    </main>
  );
}

/** The footer of every legal page: both documents and the way back into the app. */
export function LegalLinks() {
  const t = useTranslations("legal");
  return (
    <footer className="type-ui flex flex-wrap gap-x-4 gap-y-2 border-t border-border pt-4 text-muted-foreground">
      <Link
        href={LEGAL_ROUTES.privacy}
        className="hover:text-foreground hover:underline"
      >
        {t("privacyLink")}
      </Link>
      <Link
        href={LEGAL_ROUTES.terms}
        className="hover:text-foreground hover:underline"
      >
        {t("termsLink")}
      </Link>
      <Link href="/" className="hover:text-foreground hover:underline">
        {t("home")}
      </Link>
    </footer>
  );
}
