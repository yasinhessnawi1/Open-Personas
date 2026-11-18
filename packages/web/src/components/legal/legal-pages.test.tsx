/**
 * D-OWNER-11-18 (4): the Privacy Policy and the Terms of Service.
 *
 * Pins three things a user depends on: both pages render with their version and date,
 * every link to them (the sign-up line, the account menu, the legal footer) points at
 * a path that has a real page, and the account menu only offers them in the hosted
 * (cloud) build that the documents describe.
 */
import { existsSync } from "node:fs";
import { join } from "node:path";
import { render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import type { Account } from "@/auth/types";
import { PRIVACY_POLICY } from "@/content/legal/privacy";
import { TERMS_OF_SERVICE } from "@/content/legal/terms";
import messages from "@/i18n/messages/en.json";
import { LEGAL_ROUTES } from "@/lib/legal-routes";

vi.mock("@clerk/nextjs", () => ({
  useSignUp: () => ({
    signUp: { status: "missing_requirements", emailAddress: null },
    errors: { fields: {} },
    fetchStatus: "idle",
  }),
  useAuth: () => ({ isLoaded: true, isSignedIn: false }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
let account: Account;
vi.mock("@/auth", () => ({ useAccount: () => account }));
vi.mock("next-themes", () => ({ useTheme: () => ({ setTheme: vi.fn() }) }));

import PrivacyPage from "@/app/legal/privacy/page";
import TermsPage from "@/app/legal/terms/page";
import { SignUp } from "@/auth/sign-up.cloud";
import { AccountMenu } from "@/components/shell/account-menu";

const APP_DIR = join(process.cwd(), "src/app");

/** True when the App Router has a page for this path (no route groups involved). */
function hasPage(path: string): boolean {
  return existsSync(
    join(APP_DIR, ...path.split("/").filter(Boolean), "page.tsx"),
  );
}

function wrap(children: ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      {children}
    </NextIntlClientProvider>,
  );
}

describe("legal routes", () => {
  it("serves both documents under /legal, each with a page", () => {
    expect(LEGAL_ROUTES).toEqual({
      privacy: "/legal/privacy",
      terms: "/legal/terms",
    });
    for (const path of Object.values(LEGAL_ROUTES)) {
      expect(hasPage(path), path).toBe(true);
    }
  });
});

describe.each([
  ["privacy", PrivacyPage, PRIVACY_POLICY],
  ["terms", TermsPage, TERMS_OF_SERVICE],
] as const)("the %s page", (_key, Page, doc) => {
  it("renders the title, version, date and every section", () => {
    wrap(<Page />);
    expect(
      screen.getByRole("heading", { level: 1, name: doc.title }),
    ).toBeTruthy();
    expect(screen.getByText(doc.lastUpdated)).toBeTruthy();
    expect(screen.getByText(new RegExp(`Version ${doc.version}`))).toBeTruthy();
    for (const section of doc.sections) {
      expect(document.getElementById(section.id)?.textContent).toContain(
        section.heading,
      );
    }
  });

  it("links to both documents from its footer", () => {
    const { container } = wrap(<Page />);
    const footer = container.querySelector("footer");
    expect(footer).not.toBeNull();
    const links = within(footer as HTMLElement).getAllByRole("link");
    const hrefs = links.map((a) => a.getAttribute("href"));
    expect(hrefs).toContain(LEGAL_ROUTES.privacy);
    expect(hrefs).toContain(LEGAL_ROUTES.terms);
  });
});

describe("the sign-up screen", () => {
  it("links to the Terms and the Privacy Policy, and both pages exist", () => {
    wrap(<SignUp />);
    const terms = screen.getByRole("link", { name: "Terms of Service" });
    const privacy = screen.getByRole("link", { name: "Privacy Policy" });
    for (const link of [terms, privacy]) {
      const href = link.getAttribute("href") ?? "";
      expect(hasPage(href), href).toBe(true);
    }
    expect(terms.getAttribute("href")).toBe(LEGAL_ROUTES.terms);
    expect(privacy.getAttribute("href")).toBe(LEGAL_ROUTES.privacy);
  });
});

describe("the account menu", () => {
  function openMenu() {
    screen.getByRole("button", { name: "Account" }).click();
  }

  it("offers both documents in the hosted (cloud) build", async () => {
    account = {
      name: "Ada Lovelace",
      email: "ada@example.test",
      imageUrl: null,
      available: true,
      signOut: vi.fn(),
      manageAccount: vi.fn(),
    };
    wrap(<AccountMenu />);
    openMenu();
    const privacy = await screen.findByText("Privacy Policy");
    const terms = await screen.findByText("Terms of Service");
    expect(privacy.closest("a")?.getAttribute("href")).toBe(
      LEGAL_ROUTES.privacy,
    );
    expect(terms.closest("a")?.getAttribute("href")).toBe(LEGAL_ROUTES.terms);
  });

  it("leaves them out of a self-hosted community install they do not cover", async () => {
    account = { name: "", email: null, imageUrl: null, available: false };
    wrap(<AccountMenu />);
    openMenu();
    await screen.findByText("Settings");
    expect(screen.queryByText("Privacy Policy")).toBeNull();
    expect(screen.queryByText("Terms of Service")).toBeNull();
  });
});
