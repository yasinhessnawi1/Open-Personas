import { fireEvent, render } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it, vi } from "vitest";
import messages from "@/i18n/messages/en.json";
import { useDragTarget, usePasteImage } from "./use-attach-non-click";

/** The folder-drop copy is the caller's now; the harness supplies the real one. */
const FOLDER_REJECT = messages.chat.composer.attach.folderRejected;

/**
 * F3 T08 — drag-and-drop + paste handlers (desktop-only enhancements).
 */

function DragHarness({
  onFiles,
  onReject,
  disabled,
}: {
  onFiles: (files: File[]) => void;
  onReject: (detail: string) => void;
  disabled?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const isOver = useDragTarget(ref, {
    onFiles,
    onReject,
    folderRejectDetail: FOLDER_REJECT,
    disabled,
  });
  return (
    <div
      ref={ref}
      data-testid="drop-zone"
      data-over={isOver ? "true" : "false"}
    >
      drop here
    </div>
  );
}

function PasteHarness({
  onFile,
  disabled,
}: {
  onFile: (file: File) => void;
  disabled?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  usePasteImage(ref, { onFile, disabled });
  return <textarea ref={ref} data-testid="paste-zone" />;
}

function file(name: string, type: string, size = 100): File {
  return new File([new Uint8Array(size)], name, { type });
}

function makeDragEvent(
  type: "dragover" | "dragleave" | "drop",
  items: Array<{
    kind: "file" | "string";
    type: string;
    file?: File;
    isFile?: boolean;
  }>,
): DragEvent {
  const event = new Event(type, {
    bubbles: true,
    cancelable: true,
  }) as DragEvent;
  const dataTransfer = {
    types: items.some((i) => i.kind === "file") ? ["Files"] : ["text/plain"],
    items: items.map((i) => ({
      kind: i.kind,
      type: i.type,
      webkitGetAsEntry: () => ({ isFile: i.isFile ?? true }),
      getAsFile: () => i.file ?? null,
    })),
  };
  Object.defineProperty(event, "dataTransfer", { value: dataTransfer });
  return event;
}

describe("useDragTarget", () => {
  it("calls onFiles with the dropped files", () => {
    const onFiles = vi.fn();
    const { getByTestId } = render(
      <DragHarness onFiles={onFiles} onReject={vi.fn()} />,
    );
    const f = file("a.png", "image/png");
    fireEvent(
      getByTestId("drop-zone"),
      makeDragEvent("drop", [{ kind: "file", type: "image/png", file: f }]),
    );
    expect(onFiles).toHaveBeenCalledWith([f]);
  });

  it("rejects folder drops via onReject (X-F3-3 edge case)", () => {
    const onReject = vi.fn();
    const { getByTestId } = render(
      <DragHarness onFiles={vi.fn()} onReject={onReject} />,
    );
    fireEvent(
      getByTestId("drop-zone"),
      makeDragEvent("drop", [
        { kind: "file", type: "", file: file("dir", ""), isFile: false },
      ]),
    );
    expect(onReject).toHaveBeenCalled();
    expect(onReject.mock.calls[0][0]).toBe(FOLDER_REJECT);
  });

  it("silently skips remote-URL drags (kind=string)", () => {
    const onFiles = vi.fn();
    const onReject = vi.fn();
    const { getByTestId } = render(
      <DragHarness onFiles={onFiles} onReject={onReject} />,
    );
    fireEvent(
      getByTestId("drop-zone"),
      makeDragEvent("drop", [{ kind: "string", type: "text/uri-list" }]),
    );
    expect(onFiles).not.toHaveBeenCalled();
    expect(onReject).not.toHaveBeenCalled();
  });

  it("ignores dragover when the transfer contains no files (text-only drag)", () => {
    const { getByTestId } = render(
      <DragHarness onFiles={vi.fn()} onReject={vi.fn()} />,
    );
    const zone = getByTestId("drop-zone");
    fireEvent(
      zone,
      makeDragEvent("dragover", [{ kind: "string", type: "text/plain" }]),
    );
    expect(zone.getAttribute("data-over")).toBe("false");
  });

  it("flips isOver on file dragover", () => {
    const { getByTestId } = render(
      <DragHarness onFiles={vi.fn()} onReject={vi.fn()} />,
    );
    const zone = getByTestId("drop-zone");
    fireEvent(
      zone,
      makeDragEvent("dragover", [{ kind: "file", type: "image/png" }]),
    );
    expect(zone.getAttribute("data-over")).toBe("true");
  });

  it("no-ops when disabled (touch UA / persona detail)", () => {
    const onFiles = vi.fn();
    const { getByTestId } = render(
      <DragHarness onFiles={onFiles} onReject={vi.fn()} disabled />,
    );
    fireEvent(
      getByTestId("drop-zone"),
      makeDragEvent("drop", [
        { kind: "file", type: "image/png", file: file("a.png", "image/png") },
      ]),
    );
    expect(onFiles).not.toHaveBeenCalled();
  });
});

describe("usePasteImage", () => {
  function makePasteEvent(
    items: Array<{ kind: string; type: string; file?: File }>,
  ): ClipboardEvent {
    const event = new Event("paste", {
      bubbles: true,
      cancelable: true,
    }) as ClipboardEvent;
    const clipboardData = {
      items: items.map((i) => ({
        kind: i.kind,
        type: i.type,
        getAsFile: () => i.file ?? null,
      })),
    };
    Object.defineProperty(event, "clipboardData", { value: clipboardData });
    return event;
  }

  it("calls onFile when an image is pasted", () => {
    const onFile = vi.fn();
    const { getByTestId } = render(<PasteHarness onFile={onFile} />);
    const f = file("clipboard.png", "image/png");
    fireEvent(
      getByTestId("paste-zone"),
      makePasteEvent([{ kind: "file", type: "image/png", file: f }]),
    );
    expect(onFile).toHaveBeenCalledWith(f);
  });

  it("ignores text pastes (falls through to textarea)", () => {
    const onFile = vi.fn();
    const { getByTestId } = render(<PasteHarness onFile={onFile} />);
    fireEvent(
      getByTestId("paste-zone"),
      makePasteEvent([{ kind: "string", type: "text/plain" }]),
    );
    expect(onFile).not.toHaveBeenCalled();
  });

  it("ignores document pastes (not in scope — document attach is button-only)", () => {
    const onFile = vi.fn();
    const { getByTestId } = render(<PasteHarness onFile={onFile} />);
    fireEvent(
      getByTestId("paste-zone"),
      makePasteEvent([
        {
          kind: "file",
          type: "application/pdf",
          file: file("r.pdf", "application/pdf"),
        },
      ]),
    );
    expect(onFile).not.toHaveBeenCalled();
  });

  it("no-ops when disabled", () => {
    const onFile = vi.fn();
    const { getByTestId } = render(<PasteHarness onFile={onFile} disabled />);
    fireEvent(
      getByTestId("paste-zone"),
      makePasteEvent([
        {
          kind: "file",
          type: "image/png",
          file: file("a.png", "image/png"),
        },
      ]),
    );
    expect(onFile).not.toHaveBeenCalled();
  });
});
