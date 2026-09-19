import { render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAuthedImageCache } from "@/lib/authed-image-cache";
import { useAuthedImageBlobUrl } from "./use-authed-image-blob-url";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: () => Promise.resolve("jwt-token"),
  }),
}));

describe("useAuthedImageBlobUrl — D-F3-X-image-serve-auth", () => {
  let fetchCalls: Array<{ url: string; init?: RequestInit }>;

  beforeEach(() => {
    clearAuthedImageCache();
    fetchCalls = [];
    let n = 0;
    globalThis.URL.createObjectURL = vi.fn(() => {
      n += 1;
      return `blob:authed-${n}`;
    });
    globalThis.URL.revokeObjectURL = vi.fn();
    globalThis.fetch = vi.fn(async (url, init) => {
      fetchCalls.push({
        url: typeof url === "string" ? url : url.toString(),
        init,
      });
      // jsdom's Response/Blob constructor is flaky — patch blob() on the
      // response instance directly so res.blob() resolves to a known value.
      const res = new Response(null, { status: 200 });
      Object.defineProperty(res, "blob", {
        value: () =>
          Promise.resolve(
            new Blob([new Uint8Array(10)], { type: "image/png" }),
          ),
      });
      return res;
    }) as unknown as typeof fetch;
  });
  afterEach(() => {
    clearAuthedImageCache();
    vi.restoreAllMocks();
  });

  it("fetches with Bearer auth and yields a blob URL", async () => {
    const { result } = renderHook(() =>
      useAuthedImageBlobUrl("persona_abc", "uploads/x.png"),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.src).toMatch(/^blob:authed-/);
    expect(result.current.error).toBeNull();
    expect(fetchCalls[0].url).toContain(
      "/v1/personas/persona_abc/uploads/uploads/x.png",
    );
    expect(
      (fetchCalls[0].init?.headers as Record<string, string>).Authorization,
    ).toBe("Bearer jwt-token");
  });

  it("404 yields null src + null error (existence-disclosure-safe)", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response(null, { status: 404 }),
    ) as unknown as typeof fetch;
    const { result } = renderHook(() =>
      useAuthedImageBlobUrl("p", "uploads/missing.png"),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.src).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("5xx sets error so the consumer can render a retry affordance", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response(null, { status: 503 }),
    ) as unknown as typeof fetch;
    const { result } = renderHook(() =>
      useAuthedImageBlobUrl("p", "uploads/x.png"),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).not.toBeNull();
    expect(result.current.src).toBeNull();
  });

  it("two components asking for one ref trigger a single fetch", async () => {
    function Probe({ label }: { label: string }) {
      const { src } = useAuthedImageBlobUrl("p", "uploads/shared.png");
      return <span data-testid={label}>{src ?? ""}</span>;
    }
    render(
      <>
        <Probe label="a" />
        <Probe label="b" />
      </>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("a").textContent).toMatch(/^blob:authed-/),
    );
    expect(screen.getByTestId("b").textContent).toBe(
      screen.getByTestId("a").textContent,
    );
    expect(fetchCalls).toHaveLength(1);
  });

  it("renders a cached ref on the first frame of the next mount", async () => {
    const first = renderHook(() =>
      useAuthedImageBlobUrl("p", "uploads/cached.png"),
    );
    await waitFor(() => expect(first.result.current.src).not.toBeNull());
    const cachedSrc = first.result.current.src;
    first.unmount();

    const second = renderHook(() =>
      useAuthedImageBlobUrl("p", "uploads/cached.png"),
    );
    // No await: the very first render already carries the cached blob URL,
    // which is what removes the avatar pop-in on navigation.
    expect(second.result.current.src).toBe(cachedSrc);
    expect(second.result.current.loading).toBe(false);
    expect(fetchCalls).toHaveLength(1);
  });

  it("keeps the blob URL alive across unmount (the cache owns it)", async () => {
    const { result, unmount } = renderHook(() =>
      useAuthedImageBlobUrl("p", "uploads/x.png"),
    );
    await waitFor(() => expect(result.current.src).not.toBeNull());
    unmount();
    // Revoking here would break every other mount sharing this object URL;
    // the cache revokes on eviction instead.
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
  });

  it("re-fetches when workspacePath changes", async () => {
    const { rerender, result } = renderHook(
      ({ path }: { path: string }) => useAuthedImageBlobUrl("p", path),
      { initialProps: { path: "uploads/a.png" } },
    );
    await waitFor(() => expect(result.current.src).not.toBeNull());
    const firstSrc = result.current.src;
    rerender({ path: "uploads/b.png" });
    // Wait for BOTH conditions in one predicate: src is non-null AND
    // different from the original. The hook briefly nulls src while the new
    // ref loads; checking both together avoids racing the null window.
    await waitFor(() => {
      expect(result.current.src).not.toBeNull();
      expect(result.current.src).not.toBe(firstSrc);
    });
    expect(fetchCalls).toHaveLength(2);
  });
});
