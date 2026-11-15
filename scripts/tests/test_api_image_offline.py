"""The api image needs no network to import: tiktoken's encoding is baked in at build time.

Found on the Oracle host at the first deploy (2026-11-15): the one-shot `migrate` container
sits only on the internal `db` network, with no route out, by design. A migration imports
persona.tasks.checkpoint -> persona.skills._tokens, which builds tiktoken's cl100k_base
encoder at import, and tiktoken downloads
https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken the first time:
NameResolutionError, and the deploy stopped (safely). The api would have downloaded it on
every cold start too.

So the builder downloads the file once, checks the sha256 tiktoken itself expects, and
stores it under the name tiktoken looks for (the sha1 of the URL) in a root-owned,
read-only directory that TIKTOKEN_CACHE_DIR names in the runtime image. A `RUN
--network=none` step in the builder and the Images workflow's smoke (`docker run --network
none`) prove the import graph needs nothing else. Migrate keeps no route out.

Here: the Dockerfile's shape, the workflow's offline smoke, and a run of
packages/api/offline_import_check.py on this machine with every socket refused, which
shows that the api, its connectors, release.py and every migration import without the
network and ask tiktoken for cl100k_base only.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "packages" / "api" / "Dockerfile").read_text(encoding="utf-8")
BUILDER, _, RUNTIME = DOCKERFILE.partition(" AS runtime")
CHECK = ROOT / "packages" / "api" / "offline_import_check.py"
URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
CACHE_DIR = "/opt/tiktoken"


def _expected_sha256() -> str:
    """The hash tiktoken itself pins for cl100k_base, read from the installed source."""
    import tiktoken_ext.openai_public as public

    source = Path(public.__file__).read_text(encoding="utf-8")
    block = source[source.index("def cl100k_base") :]
    match = re.search(r'expected_hash="([0-9a-f]{64})"', block)
    assert match
    return match.group(1)


def _flat(text: str) -> str:
    return re.sub(r"\\\n\s*", " ", text)


def test_the_builder_downloads_and_checks_the_encoding() -> None:
    builder = _flat(BUILDER)
    cache_file = f"{CACHE_DIR}/{hashlib.sha1(URL.encode()).hexdigest()}"
    assert f'curl -fsSL "{URL}" -o "{cache_file}"' in builder
    assert f'echo "{_expected_sha256()}  {cache_file}" | sha256sum -c -' in builder
    assert builder.index("curl -fsSL") < builder.index("sha256sum -c")


def test_the_builder_proves_the_import_graph_offline() -> None:
    builder = _flat(BUILDER)
    offline = re.search(r"RUN --network=none (.+)", builder)
    assert offline, "no RUN --network=none step in the builder"
    step = offline.group(1)
    assert f"TIKTOKEN_CACHE_DIR={CACHE_DIR}" in step
    assert "packages/api/offline_import_check.py" in step
    assert "--stub-tiktoken" not in step
    assert builder.index("sha256sum -c") < builder.index("RUN --network=none")


def test_the_runtime_reads_the_baked_encoding_read_only() -> None:
    runtime = _flat(RUNTIME)
    assert f"COPY --from=builder {CACHE_DIR} {CACHE_DIR}" in runtime
    assert re.search(rf"\bTIKTOKEN_CACHE_DIR={CACHE_DIR}\b", runtime)
    # Root-owned and world-readable: uid 2001 (the server kit) and uid 1000 read it; no
    # one but root can replace it.
    assert f"chmod 0755 {CACHE_DIR}" in _flat(BUILDER)
    assert f"chmod 0644 {CACHE_DIR}/" in _flat(BUILDER)
    assert "--chown" not in runtime.split(f"COPY --from=builder {CACHE_DIR}", 1)[1].split("\n")[0]


def test_the_images_workflow_smokes_the_image_with_no_network() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "images.yml").read_text(encoding="utf-8")
    )
    runs = "\n".join(
        str(step.get("run", "")) for job in workflow["jobs"].values() for step in job["steps"]
    )
    docker_runs = [line for line in runs.splitlines() if "docker run" in line]
    assert len(docker_runs) >= 2
    for line in docker_runs:
        assert "docker run --rm --network none --user 2001:2001" in line, line
    assert "python packages/api/offline_import_check.py" in runs
    assert "tiktoken.get_encoding('cl100k_base')" in runs


def test_the_import_graph_needs_no_network_and_only_the_baked_encoding() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(CHECK), "--stub-tiktoken"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        timeout=600,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["network_attempts"] == []
    assert report["encodings"] == ["cl100k_base"]
    assert report["unbaked_encodings"] == []
    assert report["migrations"] >= 60
    assert result.returncode == 0, result.stderr[-2000:]


def test_the_check_fails_on_a_network_attempt() -> None:
    """Not vacuous: an import that dials out is caught."""
    probe = (
        "import runpy, socket, sys; sys.argv = ['x', '--stub-tiktoken'];"
        "import importlib.util as u;"
        f"s = u.spec_from_file_location('c', r'{CHECK}'); m = u.module_from_spec(s);"
        "s.loader.exec_module(m); m._refuse_the_network();"
        "\ntry:\n    socket.create_connection(('example.org', 443))\nexcept OSError:\n    pass\n"
        "print(len(m._attempts))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.stdout.strip() == "1", result.stderr
