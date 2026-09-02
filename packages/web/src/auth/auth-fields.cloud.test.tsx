/**
 * Spec 34 — render + behaviour tests for the branded-auth controls.
 *
 * These exercise the Clerk-free presentational pieces and the cooldown hook
 * (the parts that don't require a live Clerk client): the OAuth gate rendering,
 * the error alert, the OTP input's typing/paste/auto-submit behaviour, and the
 * resend cooldown timer.
 */
import {
  act,
  fireEvent,
  renderHook,
  render as rtlRender,
  screen,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { type ReactNode, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import {
  ErrorAlert,
  Field,
  HiddenUsernameField,
  OAuthRow,
  OtpInput,
  useResendCooldown,
} from "./auth-fields.cloud";
import { dedupeFieldError } from "./auth-flow.cloud";

/** The control copy lives in `auth.*` now; render with the real catalogue. */
const render = (ui: ReactNode) =>
  rtlRender(
    <NextIntlClientProvider locale="en" messages={messages}>
      {ui}
    </NextIntlClientProvider>,
  );

/**
 * Mirrors how the forms compose the banner + a deduped field error: the same
 * message Clerk surfaces at both the global and field level must render once.
 */
function BannerAndField({
  banner,
  fieldMessage,
}: {
  banner: string | null;
  fieldMessage: string | null;
}) {
  const fieldError = dedupeFieldError(fieldMessage, banner);
  return (
    <>
      <ErrorAlert message={banner} />
      <Field id="email" label="Email" error={fieldError}>
        <input id="email" />
      </Field>
    </>
  );
}

describe("error dedupe (banner + field)", () => {
  it("renders a duplicated global+field message exactly once", () => {
    const message = "Couldn't find your account.";
    render(<BannerAndField banner={message} fieldMessage={message} />);
    expect(screen.getAllByText(message)).toHaveLength(1);
    // The surviving copy is the top banner (role=alert kept).
    expect(screen.getByRole("alert")).toHaveTextContent(message);
  });

  it("still shows a field-specific error that the banner does not carry", () => {
    const fieldOnly = "Your password must be at least 8 characters.";
    render(<BannerAndField banner={null} fieldMessage={fieldOnly} />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText(fieldOnly)).toBeTruthy();
  });
});

describe("OAuthRow (gated)", () => {
  it("renders nothing while OAUTH_PROVIDERS is empty (v1 default)", () => {
    const { container } = render(<OAuthRow onSelect={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("ErrorAlert", () => {
  it("renders nothing without a message", () => {
    const { container } = render(<ErrorAlert message={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders an ARIA alert with the themed message", () => {
    render(<ErrorAlert message="That password is incorrect." />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("That password is incorrect.");
  });
});

describe("HiddenUsernameField", () => {
  it("renders a read-only autoComplete=username email field in the DOM", () => {
    render(<HiddenUsernameField value="user@example.com" />);
    // Queried by its accessible label — it stays in the a11y tree (not
    // display:none / aria-hidden) so password managers + screen readers see a
    // complete credential form, clearing the "missing username field" warning.
    const field = screen.getByLabelText("Email") as HTMLInputElement;
    expect(field).toHaveAttribute("autocomplete", "username");
    expect(field).toHaveAttribute("type", "email");
    expect(field).toHaveAttribute("name", "username");
    expect(field.readOnly).toBe(true);
    expect(field.value).toBe("user@example.com");
  });
});

/** A controlled wrapper so the OTP input behaves as it does in a flow. */
function ControlledOtp({
  onComplete,
}: {
  onComplete?: (code: string) => void;
}) {
  const [value, setValue] = useState("");
  return <OtpInput value={value} onChange={setValue} onComplete={onComplete} />;
}

describe("OtpInput", () => {
  it("exposes six individually-labelled digit boxes in a labelled group", () => {
    render(<ControlledOtp />);
    expect(
      screen.getByRole("group", { name: "Verification code" }),
    ).toBeTruthy();
    for (let i = 1; i <= 6; i += 1) {
      expect(screen.getByLabelText(`Digit ${i}`)).toBeTruthy();
    }
  });

  it("fires onComplete once all six digits are present (auto-submit)", () => {
    const onComplete = vi.fn();
    render(<ControlledOtp onComplete={onComplete} />);
    const boxes = Array.from({ length: 6 }, (_, i) =>
      screen.getByLabelText(`Digit ${i + 1}`),
    );
    for (let i = 0; i < 6; i += 1) {
      fireEvent.change(boxes[i], { target: { value: String(i + 1) } });
    }
    expect(onComplete).toHaveBeenCalledWith("123456");
  });

  it("fills all boxes from a pasted 6-digit code", () => {
    const onComplete = vi.fn();
    render(<ControlledOtp onComplete={onComplete} />);
    const first = screen.getByLabelText("Digit 1");
    fireEvent.paste(first, {
      clipboardData: { getData: () => "987654" },
    });
    expect(onComplete).toHaveBeenCalledWith("987654");
    expect(screen.getByLabelText<HTMLInputElement>("Digit 6").value).toBe("4");
  });

  it("ignores non-numeric input", () => {
    const onComplete = vi.fn();
    render(<ControlledOtp onComplete={onComplete} />);
    fireEvent.change(screen.getByLabelText("Digit 1"), {
      target: { value: "a" },
    });
    expect(screen.getByLabelText<HTMLInputElement>("Digit 1").value).toBe("");
    expect(onComplete).not.toHaveBeenCalled();
  });
});

describe("useResendCooldown", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("starts idle (ready to send)", () => {
    const { result } = renderHook(() => useResendCooldown(30));
    expect(result.current.remaining).toBe(0);
    expect(result.current.isCoolingDown).toBe(false);
  });

  it("counts down once per second and clears at zero", () => {
    const { result } = renderHook(() => useResendCooldown(3));
    act(() => result.current.start());
    expect(result.current.remaining).toBe(3);
    expect(result.current.isCoolingDown).toBe(true);

    act(() => void vi.advanceTimersByTime(1000));
    expect(result.current.remaining).toBe(2);
    act(() => void vi.advanceTimersByTime(2000));
    expect(result.current.remaining).toBe(0);
    expect(result.current.isCoolingDown).toBe(false);
  });

  it("re-arming restarts the full countdown", () => {
    const { result } = renderHook(() => useResendCooldown(5));
    act(() => result.current.start());
    act(() => void vi.advanceTimersByTime(2000));
    expect(result.current.remaining).toBe(3);
    act(() => result.current.start());
    expect(result.current.remaining).toBe(5);
  });
});
