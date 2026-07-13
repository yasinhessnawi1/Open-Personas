/**
 * AuthorWizard — the Spec 36 new-persona flow.
 *
 * Three create paths converge on the shared editor + one direct-create assembly:
 *   1. pick a prebuilt starter → editable structured draft (no `/author` call);
 *   2. start from scratch → empty structured draft;
 *   3. describe your own → the drafter.
 * The gallery now LEADS (starters are primary) and picking a card opens the
 * editor directly rather than seeding the describe textarea.
 *
 * `PersonaEditor` is mocked (its form ⇄ YAML internals have their own tests); we
 * capture its props to assert the wizard hands it the right draft + wiring, and
 * drive its `onSave` to prove the direct-create assembly reaches `createPersona`
 * with the safety constraint intact. `useAuthor` + `createPersona` are mocked so
 * no Clerk provider or network is needed.
 */

import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { PERSONA_EXAMPLE_CATEGORIES } from "@/lib/persona-examples";
import { SAFETY_CONSTRAINT } from "@/lib/persona-safety";
import { AuthorWizard } from "./author-wizard";

// R9-025a — the description field now mounts <MicDictation>, which reads
// the auth façade for a Bearer token. Stub it so the suite needs no Clerk
// provider (mirrors message-element.test.tsx's exact double-mock).
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));
vi.mock("@/auth", () => ({
  useAuth: () => ({ getToken: () => Promise.resolve("test-token") }),
}));

type AuthorHandlers = {
  onChunk?: (delta: string) => void;
  onRetry?: (reason: string) => void;
};
type DraftResult = { yaml: string; questions: never[] };
const author = vi.fn(
  async (_desc: string, _handlers?: AuthorHandlers): Promise<DraftResult> => ({
    yaml: "",
    questions: [],
  }),
);
const createPersona = vi.fn(
  async (_yaml: string): Promise<{ error: string } | undefined> => undefined,
);

// Capture the props the wizard hands the editor + expose a save trigger.
const captured: { props: Record<string, unknown> | null } = { props: null };

// R9-025 reopen — context-pinned dictation: capture the props the wizard
// hands MicDictation to assert the locale->language wiring without needing
// a real MediaRecorder/getUserMedia harness (that machinery is covered in
// full by mic-dictation.test.tsx; this file only proves AuthorWizard picks
// the right `language` value).
const capturedMic: { props: Record<string, unknown> | null } = { props: null };
vi.mock("@/components/chat/mic-dictation", () => ({
  MicDictation: (props: Record<string, unknown>) => {
    capturedMic.props = props;
    return <button type="button" data-slot="mock-mic-dictation" />;
  },
}));

vi.mock("@/lib/hooks/use-author", () => ({
  useAuthor: () => ({ author, refine: vi.fn() }),
}));
vi.mock("@/lib/persona-actions", () => ({
  createPersona: (yaml: string) => createPersona(yaml),
}));
vi.mock("./persona-editor", () => ({
  PersonaEditor: (props: Record<string, unknown>) => {
    captured.props = props;
    return (
      <div data-slot="mock-editor">
        <button
          type="button"
          data-slot="mock-save"
          onClick={async () => {
            const { docToYaml } = await import("@/lib/persona-draft");
            await (props.onSave as (y: string) => Promise<unknown>)(
              docToYaml(props.initialDoc as Record<string, unknown>),
            );
          }}
        >
          save
        </button>
      </div>
    );
  },
}));

beforeEach(() => {
  author.mockClear();
  createPersona.mockClear();
  captured.props = null;
  capturedMic.props = null;
});

function renderWizard(defaultModel?: string | null, locale = "en") {
  return render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      <AuthorWizard tools={[]} skills={[]} defaultModel={defaultModel} />
    </NextIntlClientProvider>,
  );
}

function bySlot(container: HTMLElement, slot: string): Element {
  const el = container.querySelector(`[data-slot="${slot}"]`);
  expect(el, `missing [data-slot="${slot}"]`).not.toBeNull();
  return el as Element;
}

function isBefore(a: Element, b: Element): boolean {
  return Boolean(
    a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING,
  );
}

const firstStarter = PERSONA_EXAMPLE_CATEGORIES[0].examples[0];

describe("AuthorWizard — describe-phase layout", () => {
  it("LEADS with describe + start-from-scratch, above the starter gallery", () => {
    const { container } = renderWizard();
    const textarea = bySlot(container, "author-wizard-description");
    const scratch = bySlot(container, "author-wizard-scratch");
    const gallery = bySlot(container, "example-gallery");
    // Layout: the describe box + scratch button are on top; the starter
    // suggestions sit underneath.
    expect(isBefore(textarea, gallery)).toBe(true);
    expect(isBefore(scratch, gallery)).toBe(true);
  });

  it("offers a start-from-scratch path", () => {
    const { container } = renderWizard();
    expect(bySlot(container, "author-wizard-scratch")).toBeTruthy();
  });
});

// R9-025 reopen — context-pinned dictation: the authoring mic has no
// persona to pin to (the persona doesn't exist yet), so it pins to the
// active UI locale instead — driven by `useLocale()`, not a literal.
describe("AuthorWizard — mic dictation language pin", () => {
  it("passes the active UI locale as the dictation language hint", () => {
    renderWizard(undefined, "en");
    expect(capturedMic.props?.language).toBe("en");
  });

  it("omits the hint for the 'xx' i18n pseudo-locale (not a real language)", () => {
    renderWizard(undefined, "xx");
    expect(capturedMic.props?.language).toBeUndefined();
  });
});

function pickFirstStarter(getByLabelText: (t: string) => HTMLElement): void {
  fireEvent.click(
    getByLabelText(
      messages.author.gallery.useNamed.replace("{name}", firstStarter.name),
    ),
  );
}

describe("AuthorWizard — prebuilt starter → quick-edit", () => {
  it("reveals the quick-edit card (not the full editor) seeded with the starter", () => {
    const { container, getByLabelText, getByDisplayValue } = renderWizard();
    pickFirstStarter(getByLabelText);

    // Quick-edit card appears; the full editor does NOT (it's behind "Open full
    // editor"). The starter's name is in the quick-edit name field.
    expect(bySlot(container, "quick-edit-card")).toBeTruthy();
    expect(container.querySelector('[data-slot="mock-editor"]')).toBeNull();
    expect(getByDisplayValue(firstStarter.name)).toBeTruthy();
    // The safety constraint shows pinned (a read-only line).
    const safety = getByDisplayValue(SAFETY_CONSTRAINT) as HTMLInputElement;
    expect(safety.readOnly).toBe(true);
  });

  it("creates DIRECTLY from quick-edit: guarded YAML → createPersona, no drafter", async () => {
    const { getByLabelText, container } = renderWizard();
    pickFirstStarter(getByLabelText);
    fireEvent.click(bySlot(container, "quick-create"));

    await waitFor(() => expect(createPersona).toHaveBeenCalledTimes(1));
    const yaml = createPersona.mock.calls[0][0] as string;
    expect(yaml).toContain(SAFETY_CONSTRAINT);
    expect(yaml).toContain(firstStarter.name);
    expect(author).not.toHaveBeenCalled();
  });

  it("carries quick edits into the full editor (Open full editor)", () => {
    const { getByLabelText, container } = renderWizard();
    pickFirstStarter(getByLabelText);

    // Edit the name in the quick-edit card, THEN open the full editor.
    const nameInput = bySlot(container, "quick-name") as HTMLInputElement;
    fireEvent.change(nameInput, { target: { value: "Renamed Persona" } });
    fireEvent.click(bySlot(container, "quick-open-full"));

    // The full editor mounts with the EDITED doc (carry-over), no refinement.
    expect(bySlot(container, "mock-editor")).toBeTruthy();
    const doc = captured.props?.initialDoc as { identity: { name: string } };
    expect(doc.identity.name).toBe("Renamed Persona");
    expect(captured.props?.refinement).toBeUndefined();
  });
});

describe("AuthorWizard — start from scratch", () => {
  it("reveals an empty quick-edit draft with the safety constraint pinned", () => {
    const { container, getByDisplayValue } = renderWizard();
    fireEvent.click(bySlot(container, "author-wizard-scratch"));
    expect(bySlot(container, "quick-edit-card")).toBeTruthy();
    expect(getByDisplayValue(SAFETY_CONSTRAINT)).toBeTruthy();
    expect(author).not.toHaveBeenCalled();
  });
});

describe("AuthorWizard — describe your own (drafter preserved)", () => {
  it("still calls the drafter on Generate", async () => {
    const { container } = renderWizard();
    const textarea = bySlot(
      container,
      "author-wizard-description",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, {
      target: { value: "a tenancy law assistant" },
    });
    fireEvent.click(bySlot(container, "author-wizard-generate"));
    await waitFor(() => expect(author).toHaveBeenCalledTimes(1));
  });
});

describe("AuthorWizard — streaming preview (P0)", () => {
  function startGenerate(container: HTMLElement): void {
    const textarea = bySlot(
      container,
      "author-wizard-description",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "a tenancy assistant" } });
    fireEvent.click(bySlot(container, "author-wizard-generate"));
  }

  it("paints the forming raw text into the loading preview (not a parsed form)", async () => {
    // The drafter streams chunks then stays pending (loading phase persists).
    author.mockImplementationOnce(
      async (_desc: string, handlers?: AuthorHandlers) => {
        handlers?.onChunk?.('schema_version: "1.0"');
        handlers?.onChunk?.("\nidentity:\n  name: Lex");
        return new Promise<DraftResult>(() => {}); // never resolves → stays in loading
      },
    );
    const { container } = renderWizard();
    startGenerate(container);
    await waitFor(() => {
      const pre = container.querySelector('[data-slot="author-wizard-stream"]');
      expect(pre?.textContent).toContain("schema_version");
      expect(pre?.textContent).toContain("name: Lex");
    });
  });

  it("single-flight: two generate triggers in one tick fire /author only once", async () => {
    // The draft is a PAID single-flight op. Two same-tick triggers (a double-click
    // or a re-render/remount race) must collapse to ONE /author via the
    // synchronous in-flight ref guard. Dispatching both clicks inside one act()
    // means React does not flush/unmount between them, so the describe button is
    // still mounted for the second click — isolating the guard (not the unmount).
    author.mockImplementationOnce(() => new Promise<DraftResult>(() => {})); // stays in-flight
    const { container } = renderWizard();
    const textarea = bySlot(
      container,
      "author-wizard-description",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "a tenancy assistant" } });
    const btn = bySlot(
      container,
      "author-wizard-generate",
    ) as HTMLButtonElement;
    act(() => {
      btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(author).toHaveBeenCalledTimes(1);
  });

  it("resets the preview and shows a regenerating status on the validation re-stream", async () => {
    author.mockImplementationOnce(
      async (_desc: string, handlers?: AuthorHandlers) => {
        handlers?.onChunk?.("attempt one text");
        handlers?.onRetry?.("validation"); // visible reset: attempt 2 must not append onto attempt 1
        handlers?.onChunk?.("attempt two text");
        return new Promise<DraftResult>(() => {});
      },
    );
    const { container } = renderWizard();
    startGenerate(container);
    await waitFor(() => {
      const pre = container.querySelector('[data-slot="author-wizard-stream"]');
      expect(pre?.textContent).toBe("attempt two text");
    });
    expect(bySlot(container, "author-wizard-loading-step").textContent).toBe(
      messages.author.streamRegenerating,
    );
  });
});

describe("AuthorWizard — sticky model prefill (Spec M1, M1-T7)", () => {
  it("seeds routing.preferred_model from the profile default (start from scratch)", () => {
    const { container } = renderWizard("anthropic/claude-sonnet-4.6");
    fireEvent.click(bySlot(container, "author-wizard-scratch"));
    fireEvent.click(bySlot(container, "quick-open-full"));
    const doc = captured.props?.initialDoc as {
      routing?: { preferred_model?: string };
    };
    expect(doc.routing?.preferred_model).toBe("anthropic/claude-sonnet-4.6");
  });

  it("seeds the prefill on a prebuilt starter too", () => {
    const { container, getByLabelText } = renderWizard("z-ai/glm-4.6");
    pickFirstStarter(getByLabelText);
    fireEvent.click(bySlot(container, "quick-open-full"));
    const doc = captured.props?.initialDoc as {
      routing?: { preferred_model?: string };
    };
    expect(doc.routing?.preferred_model).toBe("z-ai/glm-4.6");
  });

  it("does not author a routing block when the profile has no default", () => {
    const { container } = renderWizard(null);
    fireEvent.click(bySlot(container, "author-wizard-scratch"));
    fireEvent.click(bySlot(container, "quick-open-full"));
    const doc = captured.props?.initialDoc as { routing?: unknown };
    expect(doc.routing).toBeUndefined();
  });

  it("seeds a drafted persona too (the applyDraft path)", async () => {
    author.mockImplementationOnce(async () => ({
      yaml: 'schema_version: "1.0"\nidentity:\n  name: Drafted\n',
      questions: [],
    }));
    const { container } = renderWizard("openai/gpt-5.1");
    const textarea = bySlot(
      container,
      "author-wizard-description",
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "a tenancy assistant" } });
    fireEvent.click(bySlot(container, "author-wizard-generate"));
    await waitFor(() => expect(bySlot(container, "mock-editor")).toBeTruthy());
    const doc = captured.props?.initialDoc as {
      routing?: { preferred_model?: string };
    };
    expect(doc.routing?.preferred_model).toBe("openai/gpt-5.1");
  });
});
