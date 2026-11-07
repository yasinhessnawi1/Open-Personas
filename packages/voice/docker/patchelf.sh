#!/bin/sh
# Fetch the patchelf 0.18.0 static release for one Docker architecture, verify its sha256
# and unpack bin/patchelf. Used by packages/voice/Dockerfile (builder stage), which passes
# the build's TARGETARCH. --clear-execstack needs patchelf 0.18 or later, and the apt and
# pip builds are older, hence the release binary with a pinned digest per architecture.
#
# Usage: patchelf.sh <amd64|arm64> [dest-prefix, default /usr/local]
# PATCHELF_PRINT_ONLY=1 prints "<url> <sha256>" and exits (the tests use this; no network).
#
# The digests were computed from the downloads (GitHub publishes none for these assets);
# the aarch64 binary was checked to be an EM_AARCH64 ELF.
set -eu

version=0.18.0
case "${1:-}" in
  amd64) arch=x86_64; sha256=ce84f2447fb7a8679e58bc54a20dc2b01b37b5802e12c57eece772a6f14bf3f0 ;;
  arm64) arch=aarch64; sha256=ae13e2effe077e829be759182396b931d8f85cfb9cfe9d49385516ea367ef7b2 ;;
  *)
    echo "patchelf.sh: unsupported architecture '${1:-}' (the voice image builds for amd64 and arm64)" >&2
    exit 2
    ;;
esac
url="https://github.com/NixOS/patchelf/releases/download/${version}/patchelf-${version}-${arch}.tar.gz"

if [ "${PATCHELF_PRINT_ONLY:-0}" = 1 ]; then
  echo "${url} ${sha256}"
  exit 0
fi

dest="${2:-/usr/local}"
tarball="$(mktemp)"
trap 'rm -f "${tarball}"' EXIT
curl -fsSL -o "${tarball}" "${url}"
echo "${sha256}  ${tarball}" | sha256sum -c -
tar -xzf "${tarball}" -C "${dest}" ./bin/patchelf
