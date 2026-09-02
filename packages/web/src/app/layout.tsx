import type { Metadata } from "next";
import { Fraunces, Geist, Geist_Mono } from "next/font/google";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getTranslations } from "next-intl/server";
import { AuthProvider } from "@/auth/provider";
import { ThemeProvider } from "@/components/theme-provider";
import "./globals.css";

// Typography concept (D-09-7 / "editorial instrument"): Fraunces (characterful
// serif) for persona names + headings — personas are named characters with a
// voice; Geist for UI/body; Geist Mono for code/YAML/tier badges. globals.css
// maps font-heading -> --font-display, font-sans -> --font-sans, mono -> --font-geist-mono.
const geistSans = Geist({ variable: "--font-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});
const fraunces = Fraunces({
  variable: "--font-display",
  subsets: ["latin"],
  display: "swap",
});

// metadataBase resolves the file-convention social images
// (app/opengraph-image.png, app/twitter-image.png) + manifest/icon URLs to
// absolute URLs. Sourced from NEXT_PUBLIC_APP_URL with a localhost fallback so
// dev still produces valid (if local) absolute tags.
const appUrl =
  process.env.NEXT_PUBLIC_APP_URL?.trim() || "http://localhost:3000";

// The tab title + share-card copy are user-visible, so they come from the
// message catalogue like every other string (next-intl resolves the request
// locale here exactly as it does in the tree below).
export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("app");
  const title = t("name");
  const description = t("description");
  return {
    metadataBase: new URL(appUrl),
    applicationName: title,
    title,
    description,
    // og/twitter *images* are supplied by the app-root file conventions
    // (opengraph-image.png / twitter-image.png + their .alt.txt); we only add
    // the accompanying text + card type here. Icons come from app/favicon.ico,
    // app/icon.svg, app/apple-icon.png + app/manifest.ts.
    openGraph: {
      type: "website",
      siteName: title,
      title,
      description,
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
    },
  };
}

export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const locale = await getLocale();
  return (
    <html
      lang={locale}
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} ${fraunces.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <AuthProvider>
          <ThemeProvider
            attribute="class"
            defaultTheme="system"
            enableSystem
            disableTransitionOnChange
          >
            <NextIntlClientProvider>{children}</NextIntlClientProvider>
          </ThemeProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
