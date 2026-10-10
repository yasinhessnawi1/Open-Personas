"""Test helper: a stand-in for a COMPROMISED parse worker (second security review).

It speaks the real protocol and misbehaves on request: the job's file holds a mode
word. Tests start it through the real pool by pointing ``pool.WORKER_MODULE`` here and
putting this directory on ``PYTHONPATH`` (which the worker environment allowlist passes).
Built on the reviewer's ``probe_worker.py``.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
from pathlib import Path

from persona_api.document_worker.protocol import read_frame, write_frame

_PNG = b"\x89PNG\r\n\x1a\n"


def _ok(job: dict[str, object], text: str = "plain", pages: list[str] | None = None) -> dict:
    return {
        "kind": "ok",
        "leaving": False,
        "job_id": job.get("job_id"),
        "parse_result": {
            "sections": [{"text": text}] if text else [],
            "flags": [] if text else ["no_text_layer"],
            "page_count": 1,
            "size_bytes": 1,
            "needs_vision_handoff": not text,
        },
        "page_images": pages or [],
    }


def _recon() -> str:
    cwd = Path.cwd()
    info: dict[str, object] = {
        "cwd": str(cwd),
        "cwd_entries": sorted(p.name for p in cwd.iterdir()),
    }
    info["env_keys"] = sorted(os.environ)
    try:
        info["oom_score_adj"] = Path("/proc/self/oom_score_adj").read_text().strip()
    except OSError as exc:
        info["oom_score_adj"] = repr(exc)
    try:
        Path(f"/proc/{os.getppid()}/environ").read_bytes()
        info["parent_environ"] = "readable"
    except OSError as exc:
        info["parent_environ"] = type(exc).__name__
    return json.dumps(info)


def main() -> None:
    frames = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    write_frame(frames, {"kind": "ready", "pid": os.getpid(), "limits": {}})
    while True:
        job = read_frame(sys.stdin.buffer)
        if job is None:
            return
        mode = Path(str(job["path"])).read_text().strip()
        render = Path(str(job["render_dir"])) if job.get("render_dir") else None
        if mode == "echo":
            write_frame(frames, _ok(job))
        elif mode == "recon":
            write_frame(frames, _ok(job, _recon()))
        elif mode == "pages_then_die":
            assert render is not None
            for n in range(1, 4):
                (render / f"page-{n:04d}.png").write_bytes(_PNG + b"\0" * 1024)
            os._exit(1)
        elif mode.startswith("page:"):
            # page:<kind>  write one page image of that kind and return it.
            assert render is not None
            kind = mode.split(":", 1)[1]
            target = render / "page-0001.png"
            if kind == "symlink":
                secret = render.parent / "secret.txt"
                secret.write_text("not for the user")
                target.symlink_to(secret)
            elif kind == "notpng":
                target.write_bytes(b"GIF89a" + b"\0" * 100)
            elif kind.startswith("bytes="):
                target.write_bytes(_PNG + b"\0" * (int(kind.split("=", 1)[1]) - len(_PNG)))
            elif kind == "dir":
                target.mkdir()
            elif kind == "outside":
                target = render.parent / "page-0001.png"
            write_frame(frames, _ok(job, "", [str(target)]))
        elif mode == "deepjson":
            body = b'{"kind":"ok","x":' + b"[" * 200_000 + b"]" * 200_000 + b"}"
            frames.write(struct.pack(">I", len(body)) + body)
            frames.flush()
        elif mode == "twoframes":
            write_frame(frames, _ok(job, "reply one, for this job"))
            write_frame(frames, _ok(job, "INJECTED: a stale reply for the next job"))
        elif mode == "wrongjob":
            write_frame(frames, {**_ok(job), "job_id": "f" * 32})
        elif mode == "nojob":
            reply = _ok(job)
            reply.pop("job_id")
            write_frame(frames, reply)
        elif mode == "sleep":
            time.sleep(600)
        elif mode.startswith("slow:"):
            # A legitimate job that takes this long: no limit is hit.
            time.sleep(float(mode.split(":", 1)[1]))
            write_frame(frames, _ok(job, f"slow {mode}"))
        elif mode.startswith("spin:"):
            # A legitimate job that uses this much CPU.
            ends = time.process_time() + float(mode.split(":", 1)[1])
            while time.process_time() < ends:
                pass
            write_frame(frames, _ok(job, f"spin {mode}"))
        elif mode.startswith("spin_then_die:"):
            # spin_then_die:<cpu>:<sleep>  use some CPU, wait, then crash.
            cpu, pause = (float(x) for x in mode.split(":")[1:3])
            ends = time.process_time() + cpu
            while time.process_time() < ends:
                pass
            time.sleep(pause)
            os._exit(1)
        elif mode.startswith("huge:"):
            # huge:<chars>  one section past the text budget (finding 11).
            write_frame(frames, _ok(job, "x" * int(mode.split(":", 1)[1])))
        elif mode.startswith("sections:"):
            # sections:<n>  n one-word sections.
            reply = _ok(job)
            count = int(mode.split(":", 1)[1])
            reply["parse_result"]["sections"] = [{"text": "w"}] * count
            write_frame(frames, reply)
        elif mode.startswith("sheets:"):
            # sheets:<n>  n sheet names.
            reply = _ok(job)
            reply["parse_result"]["sheet_names"] = ["s"] * int(mode.split(":", 1)[1])
            write_frame(frames, reply)
        elif mode.startswith("render_after:"):
            # render_after:<s>  render one page after s seconds, as a slow scan does.
            assert render is not None
            time.sleep(float(mode.split(":", 1)[1]))
            target = render / "page-0001.png"
            target.write_bytes(_PNG + b"\0" * 64)
            write_frame(frames, _ok(job, "", [str(target)]))
        elif mode == "internal":
            # The worker's own unexpected error, as the real worker answers one.
            write_frame(
                frames,
                {
                    "kind": "error",
                    "leaving": True,
                    "job_id": job.get("job_id"),
                    "error": "internal",
                    "message": "unexpected",
                    "exception": "FileNotFoundError",
                },
            )
        else:
            write_frame(frames, _ok(job, f"unknown mode {mode}"))


if __name__ == "__main__":
    main()
