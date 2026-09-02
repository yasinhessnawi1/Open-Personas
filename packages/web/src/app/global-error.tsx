"use client";

/**
 * App-wide last-resort error boundary.
 *
 * `global-error.tsx` replaces the ROOT layout when a render throw escapes every
 * nested boundary, so it must render its own `<html>` / `<body>`. This is the
 * final defense against the "black screen" failure mode: if anything in the
 * tree (including the root layout itself) throws, the user still sees a calm,
 * actionable fallback rather than an empty document.
 *
 * It is intentionally self-contained — no design tokens / CSS modules / Clerk —
 * because at this point we cannot assume the app's CSS or providers mounted.
 * Inline styles cover both colour schemes via `color-scheme`.
 */
import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Global error boundary:", error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "24px",
          colorScheme: "light dark",
          background: "Canvas",
          color: "CanvasText",
          fontFamily:
            "system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif",
        }}
      >
        <div style={{ maxWidth: "420px", textAlign: "center" }} role="alert">
          <h1 style={{ fontSize: "1.4rem", fontWeight: 600, margin: 0 }}>
            Something went wrong
          </h1>
          <p
            style={{
              margin: "12px 0 24px",
              fontSize: "0.95rem",
              lineHeight: 1.55,
              opacity: 0.8,
            }}
          >
            Something broke on our side, not yours. Try again.
          </p>
          <button
            type="button"
            onClick={reset}
            style={{
              height: "42px",
              padding: "0 20px",
              borderRadius: "8px",
              border: "1px solid CanvasText",
              background: "transparent",
              color: "inherit",
              font: "inherit",
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
