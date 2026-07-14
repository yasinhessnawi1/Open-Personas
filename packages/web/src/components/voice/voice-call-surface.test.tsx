import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import type { CallSession } from "@/lib/voice/call-session-context";
import {
  INITIAL_CALL_STATE,
  type VoiceCallState,
} from "@/lib/voice/call-state";
import { VoiceCallSurface } from "./voice-call-surface";

// A mutable session handle the useCallSession mock reads (vi.hoisted so the
// hoisted factory can close over it). `useVoiceCallSpy` proves the surface no
// longer owns a Room: if it ever instantiated the hook, this would be called.
const h = vi.hoisted(() => ({
  session: null as CallSession | null,
  replace: vi.fn(),
  useVoiceCallSpy: vi.fn(() => {
    throw new Error("VoiceCallSurface must bind the session, not own a Room");
  }),
}));

vi.mock("@/lib/voice/call-session-context", () => ({
  useCallSession: () => h.session,
}));
// The surface must NOT import/instantiate this anymore — keep a spy to assert it.
vi.mock("@/lib/voice/use-voice-call", () => ({
  useVoiceCall: h.useVoiceCallSpy,
}));
vi.mock("@/lib/voice/use-persona-avatar-src", () => ({
  usePersonaAvatarSrc: () => null,
}));
vi.mock("@/components/voice/identity-orb", () => ({
  IdentityOrb: () => <div data-testid="orb" />,
}));
// The reused chat panel is covered by chat's own tests; mock it here so the
// surface test stays free of the panel's auth-bound artifact fetch (useAuth).
vi.mock("@/components/chat/file-renderer-panel", () => ({
  FileRendererPanel: () => <div data-testid="file-renderer-panel" />,
}));
// R9-029: the transcript right panel has its own dedicated test coverage
// (voice-transcript-panel.test.tsx — avatars, markdown, the AUTO chip); stub
// it here so THIS suite stays focused on the surface's wiring/layout.
vi.mock("@/components/voice/voice-transcript-panel", () => ({
  VoiceTranscriptPanel: () => <div data-testid="transcript-panel" />,
}));
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
  }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: h.replace, push: vi.fn() }),
}));

function makeSession(
  state: VoiceCallState,
  over: Partial<CallSession> = {},
): CallSession {
  return {
    state,
    captions: [],
    artifacts: [],
    activities: [],
    target: null,
    isActive: state.phase !== "idle",
    startedAt: state.phase !== "idle" ? Date.now() : null,
    pendingSwitch: null,
    start: vi.fn(),
    requestCall: vi.fn(),
    confirmSwitch: vi.fn(),
    cancelSwitch: vi.fn(),
    resumable: null,
    resumeCall: vi.fn(),
    dismissResume: vi.fn(),
    end: vi.fn(),
    toggleMute: vi.fn(),
    inputMode: "always",
    setInputMode: vi.fn(),
    pttHeld: false,
    setPttHeld: vi.fn(),
    enableAudio: vi.fn(),
    getMicLevel: () => 0,
    getPersonaLevel: () => 0,
    ...over,
  };
}

function renderSurface() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages}>
      <VoiceCallSurface
        persona={{ id: "p1", name: "Astrid", role: "Advisor" }}
        conversationId="c1"
      />
    </NextIntlClientProvider>,
  );
}

const withPhase = (
  phase: VoiceCallState["phase"],
  error: VoiceCallState["error"] = null,
): VoiceCallState => ({ ...INITIAL_CALL_STATE, phase, error });

beforeEach(() => {
  h.replace.mockClear();
  h.useVoiceCallSpy.mockClear();
});

describe("VoiceCallSurface (V7 — binds the session)", () => {
  it("binds the shared session and never instantiates useVoiceCall", () => {
    h.session = makeSession(withPhase("connected"), { isActive: true });
    renderSurface();
    expect(screen.getByTestId("orb")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "End call" }),
    ).toBeInTheDocument();
    // The HARD GUARD: no second Room — the surface owns no useVoiceCall instance.
    expect(h.useVoiceCallSpy).not.toHaveBeenCalled();
  });

  it("R11-B7: idle → auto-joins the session on mount, no dead Talk stop", () => {
    const session = makeSession(withPhase("idle"), { isActive: false });
    h.session = session;
    renderSurface();
    // No interactive start step — the call auto-joins on entry.
    expect(
      screen.queryByRole("button", { name: /Talk to Astrid/ }),
    ).not.toBeInTheDocument();
    expect(session.start).toHaveBeenCalledWith(
      expect.objectContaining({
        personaId: "p1",
        conversationId: "c1",
        personaName: "Astrid",
      }),
    );
  });

  it("R11-B7: ended → redirects to the transcript (the chat thread), no dead stop", () => {
    const session = makeSession(withPhase("ended"), { isActive: true });
    h.session = session;
    renderSurface();
    // No auto-redial of an ended call; straight to the transcript.
    expect(session.start).not.toHaveBeenCalled();
    expect(h.replace).toHaveBeenCalledWith("/chat/c1");
  });

  it("end → ends the session and returns to the conversation", () => {
    const session = makeSession(withPhase("connected"), { isActive: true });
    h.session = session;
    renderSurface();
    fireEvent.click(screen.getByRole("button", { name: "End call" }));
    expect(session.end).toHaveBeenCalledTimes(1);
  });

  it("mic_denied error → kind-specific copy + retry, no orb", () => {
    h.session = makeSession(
      withPhase("error", { kind: "mic_denied", message: "blocked" }),
      { isActive: true },
    );
    renderSurface();
    expect(screen.getByText("Microphone blocked")).toBeInTheDocument();
    expect(screen.getByText("Try again")).toBeInTheDocument();
    expect(screen.queryByTestId("orb")).not.toBeInTheDocument();
  });

  it("unauthorized error → a sign-in link, not a retry", () => {
    h.session = makeSession(
      withPhase("error", { kind: "unauthorized", message: "expired" }),
      { isActive: true },
    );
    renderSurface();
    expect(screen.getByText("Sign in")).toHaveAttribute("href", "/sign-in");
    expect(screen.queryByText("Try again")).not.toBeInTheDocument();
  });

  it("dropped → reconnect affordance", () => {
    h.session = makeSession(withPhase("dropped"), { isActive: true });
    renderSurface();
    expect(screen.getByText("Call dropped")).toBeInTheDocument();
    expect(screen.getByText("Try again")).toBeInTheDocument();
  });
});

describe("VoiceCallSurface — R9-029 desktop transcript panel", () => {
  it("renders the transcript panel (desktop-only aside) while live and captions are on", () => {
    h.session = makeSession(withPhase("connected"), { isActive: true });
    renderSurface();
    const aside = screen.getByLabelText("Transcript");
    expect(aside.tagName).toBe("ASIDE");
    expect(screen.getByTestId("transcript-panel")).toBeInTheDocument();
  });

  it("hides the transcript panel when captions are toggled off (parity with the mobile treatment)", () => {
    h.session = makeSession(withPhase("connected"), { isActive: true });
    renderSurface();
    fireEvent.click(screen.getByRole("button", { name: "Captions" }));
    expect(screen.queryByTestId("transcript-panel")).not.toBeInTheDocument();
  });

  it("hides the transcript panel on a terminal (error) phase — no orb, no transcript", () => {
    h.session = makeSession(
      withPhase("error", { kind: "mic_denied", message: "blocked" }),
      { isActive: true },
    );
    renderSurface();
    expect(screen.queryByTestId("transcript-panel")).not.toBeInTheDocument();
  });

  it("does not render the transcript panel before a call is active", () => {
    h.session = makeSession(withPhase("idle"), { isActive: false });
    renderSurface();
    expect(screen.queryByTestId("transcript-panel")).not.toBeInTheDocument();
  });
});

describe("VoiceCallSurface — R9-029 call-stage duration", () => {
  it("shows the elapsed call duration once the session has a startedAt", () => {
    const startedAt = Date.now() - 65_000; // 1:05 ago
    h.session = makeSession(withPhase("connected"), {
      isActive: true,
      startedAt,
    });
    renderSurface();
    const duration = document.querySelector(
      '[data-slot="voice-call-duration"]',
    );
    expect(duration).not.toBeNull();
    expect(duration?.textContent).toMatch(/^\d+:\d{2}$/);
  });

  it("shows no duration before the first tick lands (startedAt null)", () => {
    h.session = makeSession(withPhase("connected"), {
      isActive: true,
      startedAt: null,
    });
    renderSurface();
    expect(
      document.querySelector('[data-slot="voice-call-duration"]'),
    ).toBeNull();
  });
});
