#!/usr/bin/env bash
# =============================================================================
# ci-local.sh - run what CI runs, locally, before you push / after a merge.
# =============================================================================
#
# WHY
#   Mirrors .github/workflows/ci.yml EXACTLY (same tools, flags, order) so that
#   "green locally" reliably implies "green in CI". It does NOT invent checks:
#   every step below maps to a line in ci.yml (the mapping is in each step's
#   comment). It never modifies source, never commits, never pushes: it only
#   runs checks and reports.
#
# WHAT IT MIRRORS (ci.yml job -> step here)
#   lint-and-type-check:
#     uv run ruff check                                   -> ruff-check
#     uv run ruff format --check                          -> ruff-format
#     uv run mypy core/src runtime/src voice/src connectors/src --strict -> mypy-strict
#     uv run mypy packages/api/src                        -> mypy-api
#   test:
#     uv run pytest --collect-only -q (EARLY cheap gate)  -> pytest-collect *
#     uv run pytest                                       -> pytest-unit
#   test-integration:
#     uv run pytest -m integration  (real Postgres)       -> pytest-integration
#   web:
#     pnpm install --frozen-lockfile                      -> web-install
#     pnpm typecheck / lint / check:no-literals / build / test
#                                                         -> web-typecheck ...
#
#   (*) pytest-collect is NOT a literal CI step, but it catches the cross-package
#       test-file basename collision ("import file mismatch") that has broken CI
#       repeatedly: in seconds, before the slow full run. Treated as a hard gate.
#
# USAGE
#   After a merge (full honest run, default):
#       ./scripts/ci-local.sh
#
#   Fast pre-push (lint + types + collect-only + unit; DEFER integration + web):
#       ./scripts/ci-local.sh --fast
#
#   Skip the (slow, Postgres-dependent) integration leg explicitly:
#       ./scripts/ci-local.sh --no-integration      (or SKIP_INTEGRATION=1)
#
#   Skip the web (pnpm) leg:
#       ./scripts/ci-local.sh --no-web              (or SKIP_WEB=1)
#
#   Stop at the first failing step (CI is NOT fail-fast across jobs, so the
#   default here runs everything and reports the full picture):
#       ./scripts/ci-local.sh --fail-fast
#
#   Re-run ONLY one named step (fast iteration after reading a failure in the
#   SUMMARY below; it does not re-run uv-sync, so sync at least once first):
#       ./scripts/ci-local.sh --only pytest-unit
#
#   Combine freely, e.g.:  ./scripts/ci-local.sh --no-web --no-integration
#
# GIT PRE-PUSH HOOK
#   This same script backs the opt-in pre-push hook. Install:
#       ln -sf ../../scripts/pre-push.hook .git/hooks/pre-push   # from repo root
#   Bypass in an emergency:
#       git push --no-verify
#   The hook runs in --fast mode by default (see scripts/pre-push.hook).
#
# EXIT CODE
#   0  iff every step that RAN passed. Non-zero if any ran-step failed.
#   A SKIPPED integration/web leg is reported LOUDLY in the summary and does NOT
#   silently turn the run green: the summary always states what did not run.
#
# LOGS
#   Every step's full output is written to its own file under
#   .ci-local/<run-timestamp>/<step>.log, in addition to streaming to this
#   terminal exactly as before. For every FAILED step, the closing SUMMARY
#   prints a short excerpt, that file's path, and the exact command to re-run
#   just that step, so a failure never requires re-running the whole suite
#   just to see what broke.
#
# CANNOT BE REPRODUCED LOCALLY (faithfully noted, not faked)
#   - CI's HuggingFace embedder-cache warm step (actions/cache + HF 429 retry):
#     locally the bge-small-en-v1.5 model is already in ~/.cache/huggingface, so
#     the integration suite's first persona-create finds it on disk. We probe for
#     it and warn if absent rather than re-implementing the retry/backoff loop.
#   - CI's `next build` runs with Clerk CI-placeholder env. We replicate those
#     placeholders for the web build so it type-checks + bundles the same way.
# =============================================================================
set -uo pipefail

# --- Resolve repo root (script lives in scripts/) ----------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
cd "$REPO_ROOT"

# --- Flags / env -------------------------------------------------------------
FAST=0
FAIL_FAST=0
SKIP_INTEGRATION="${SKIP_INTEGRATION:-0}"
SKIP_WEB="${SKIP_WEB:-0}"
ONLY_STEP="${ONLY_STEP:-}"

# Canonical step names, in run order. This is what --only accepts, and every
# step's log file under .ci-local/<run>/ is named "<one of these>.log".
CI_LOCAL_STEP_NAMES="uv-sync ruff-check ruff-format mypy-strict mypy-api pytest-collect pytest-unit pytest-integration web-install web-typecheck web-lint web-no-literals web-build web-test"

is_valid_step() { # name
  local n
  for n in $CI_LOCAL_STEP_NAMES; do
    [[ "$n" == "$1" ]] && return 0
  done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fast)            FAST=1; shift ;;
    --no-integration)  SKIP_INTEGRATION=1; shift ;;
    --no-web)          SKIP_WEB=1; shift ;;
    --fail-fast)       FAIL_FAST=1; shift ;;
    --only)
      if [[ $# -lt 2 ]]; then
        echo "ci-local: --only requires a step name (try --help)" >&2
        exit 2
      fi
      ONLY_STEP="$2"
      shift 2
      ;;
    --only=*)
      ONLY_STEP="${1#--only=}"
      shift
      ;;
    -h|--help)
      sed -n '2,82p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ci-local: unknown argument: $1 (try --help)" >&2
      exit 2
      ;;
  esac
done

if [[ -n "$ONLY_STEP" ]] && ! is_valid_step "$ONLY_STEP"; then
  echo "ci-local: unknown --only step: $ONLY_STEP" >&2
  echo "ci-local: valid steps: $CI_LOCAL_STEP_NAMES" >&2
  exit 2
fi

# Fast mode defers the slow legs (integration + web) to keep pre-push snappy.
if [[ "$FAST" -eq 1 ]]; then
  SKIP_INTEGRATION=1
  SKIP_WEB=1
fi

# --- Per-run log directory ----------------------------------------------------
# Every step's full output (stdout+stderr merged) is teed into its own file
# here, in addition to streaming to the terminal as it always has. The
# SUMMARY at the end points at these files (and, for failed steps, excerpts
# the relevant lines) instead of making you scroll back through everything.
CI_LOCAL_LOG_ROOT="${CI_LOCAL_LOG_ROOT:-$REPO_ROOT/.ci-local}"
RUN_TS="$(date +%Y%m%d-%H%M%S)"
RUN_LOG_DIR="$CI_LOCAL_LOG_ROOT/$RUN_TS"
mkdir -p "$RUN_LOG_DIR" || {
  echo "ci-local: could not create log directory: $RUN_LOG_DIR" >&2
  exit 1
}
LOG_DIR_REL="${RUN_LOG_DIR#"$REPO_ROOT"/}"
EXCERPT_CAP=60

# --- Local Postgres / integration DB config ----------------------------------
# CI uses a disposable ephemeral Postgres. Locally the dev Postgres on :5436 is
# the SHARED dev database and the integration fixtures DROP SCHEMA public CASCADE
# (running them against the dev `persona` DB would WIPE dev data). So we target a
# separate disposable DB whose name ends in `_test` (`persona_test`), which also
# satisfies the conftest safety gate. Override via PERSONA_TEST_DB_NAME / the URLs.
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${POSTGRES_HOST_PORT:-5436}"
TEST_DB_NAME="${PERSONA_TEST_DB_NAME:-persona_test}"
# These mirror ci.yml's env block (sync psycopg3 dialect, D-07-1), pointed at the
# local disposable _test DB instead of CI's ephemeral one.
CI_DATABASE_URL="${CI_LOCAL_DATABASE_URL:-postgresql+psycopg://persona:persona@${PG_HOST}:${PG_PORT}/${TEST_DB_NAME}}"
CI_APP_DATABASE_URL="${CI_LOCAL_APP_DATABASE_URL:-postgresql+psycopg://persona_app:persona_app@${PG_HOST}:${PG_PORT}/${TEST_DB_NAME}}"

# --- Output helpers ----------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YEL=$'\033[33m'; C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'
else
  C_RESET=''; C_RED=''; C_GREEN=''; C_YEL=''; C_BLUE=''; C_BOLD=''
fi

# Parallel arrays of step results for the final summary.
declare -a STEP_NAMES=()
declare -a STEP_STATUS=()   # PASS | FAIL | SKIP
declare -a STEP_NOTE=()
ANY_FAIL=0

hr()    { printf '%s\n' "------------------------------------------------------------"; }
banner(){ printf '\n%s== %s ==%s\n' "$C_BOLD$C_BLUE" "$1" "$C_RESET"; }

record() { # name status note
  STEP_NAMES+=("$1"); STEP_STATUS+=("$2"); STEP_NOTE+=("${3:-}")
  case "$2" in
    FAIL) ANY_FAIL=1 ;;
  esac
}

# want_step "name": true (0) if this run should execute the named step, i.e.
# --only was not given, or was given and matches exactly.
want_step() {
  [[ -z "$ONLY_STEP" || "$ONLY_STEP" == "$1" ]]
}

# want_any_web_step: true (0) unless --only pins the run to a non-web step, so
# the web (pnpm) block below can decide once whether to even enter.
want_any_web_step() {
  [[ -z "$ONLY_STEP" ]] && return 0
  case "$ONLY_STEP" in
    web-*) return 0 ;;
    *)     return 1 ;;
  esac
}

# run_step "label" "ci.yml ref" cmd...
run_step() {
  local label="$1"; local ref="$2"; shift 2
  banner "$label  ${C_RESET}${C_YEL}(ci.yml: ${ref})${C_RESET}"
  printf '%s$ %s%s\n' "$C_BOLD" "$*" "$C_RESET"
  hr
  local start end rc log_file
  log_file="$RUN_LOG_DIR/$label.log"
  start=$(date +%s)
  "$@" 2>&1 | tee "$log_file"
  rc=${PIPESTATUS[0]}
  end=$(date +%s)
  hr
  if [[ $rc -eq 0 ]]; then
    printf '%s[PASS]%s %s (%ss)\n' "$C_GREEN" "$C_RESET" "$label" "$((end-start))"
    record "$label" PASS "$((end-start))s"
  else
    printf '%s[FAIL]%s %s (exit %d, %ss)\n' "$C_RED" "$C_RESET" "$label" "$rc" "$((end-start))"
    record "$label" FAIL "exit ${rc}"
    if [[ "$FAIL_FAST" -eq 1 ]]; then
      printf '%s--fail-fast set: stopping at first failure.%s\n' "$C_RED" "$C_RESET"
      summary
      exit 1
    fi
  fi
  return 0
}

skip_step() { # label reason
  banner "$1  ${C_RESET}${C_YEL}(SKIPPED)${C_RESET}"
  printf '%s[SKIP]%s %s: %s\n' "$C_YEL" "$C_RESET" "$1" "$2"
  record "$1" SKIP "$2"
}

# --- Special handling: collect-only is a HARD gate ---------------------------
# A non-zero exit here means a collection/import error (e.g. duplicate test
# basenames across packages -> "import file mismatch"). pytest exit 5 == "no
# tests collected", which for THIS repo (>1000 tests, CI treats empty as a
# failure) is also a hard failure. Anything non-zero => fail the gate.
run_collect_gate() {
  banner "pytest-collect (early cheap gate)  ${C_RESET}${C_YEL}(catches cross-package basename collisions)${C_RESET}"
  printf '%s$ uv run pytest --collect-only -q%s\n' "$C_BOLD" "$C_RESET"
  hr
  local out rc log_file
  log_file="$RUN_LOG_DIR/pytest-collect.log"
  out="$(uv run pytest --collect-only -q 2>&1)"
  rc=$?
  printf '%s\n' "$out" > "$log_file"
  printf '%s\n' "$out" | tail -n 25
  hr
  if [[ $rc -eq 0 ]]; then
    printf '%s[PASS]%s pytest-collect\n' "$C_GREEN" "$C_RESET"
    record "pytest-collect" PASS ""
  else
    printf '%s[FAIL]%s pytest-collect (exit %d)\n' "$C_RED" "$C_RESET" "$rc"
    if printf '%s' "$out" | grep -qi 'import file mismatch'; then
      printf '%s>>> COLLECTION ERROR: duplicate test-file basename across packages.%s\n' "$C_RED" "$C_RESET"
      printf '    Two test files share a basename (e.g. test_foo.py in two packages).\n'
      printf '    Rename one so every test file basename is unique across the workspace.\n'
      printf '    Offending lines:\n'
      printf '%s' "$out" | grep -i 'import file mismatch' | sed 's/^/      /'
    fi
    record "pytest-collect" FAIL "exit ${rc}"
    if [[ "$FAIL_FAST" -eq 1 ]]; then summary; exit 1; fi
    return 1
  fi
  return 0
}

# --- Integration-DB reachability probe ---------------------------------------
# Returns 0 if the disposable _test DB is reachable as BOTH roles ci.yml needs
# (superuser DATABASE_URL + persona_app APP_DATABASE_URL for the RLS suite).
probe_integration_db() {
  uv run python - "$CI_DATABASE_URL" "$CI_APP_DATABASE_URL" <<'PY'
import sys
from sqlalchemy import create_engine, text
for url in sys.argv[1:3]:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - best-effort reachability probe
        print(f"unreachable: {url.rsplit('@',1)[-1]} ({exc.__class__.__name__})", file=sys.stderr)
        sys.exit(1)
sys.exit(0)
PY
}

# --- Failure excerpts for the SUMMARY -----------------------------------------
# The full output already lives in $RUN_LOG_DIR/<step>.log (see run_step /
# run_collect_gate above). These pull just the part a human needs to know
# what broke, without re-running anything.

# Pull the parts of a pytest log that answer "what failed": every FAILED and
# ERROR line from the short test summary info section, plus the first few
# assertion (E-prefixed) lines from each failure's own traceback.
excerpt_pytest_log() { # log_file
  local log="$1" summary_lines traceback_lines
  summary_lines="$(grep -E '^(FAILED|ERROR)[[:space:]]' "$log")"
  traceback_lines="$(awk '
    BEGIN { active = 0; cnt = 0 }
    /^=+ *(FAILURES|ERRORS) *=+$/ { active = 1; next }
    /^=+ *short test summary info *=+$/ { active = 0 }
    active && /^_+ .+ _+$/ {
      name = $0
      sub(/^_+ /, "", name)
      sub(/ _+$/, "", name)
      print "-- " name " --"
      cnt = 0
      next
    }
    active && /^E( |$)/ {
      if (cnt < 4) { print; cnt++ }
    }
  ' "$log")"
  [[ -n "$summary_lines" ]] && printf '%s\n' "$summary_lines"
  if [[ -n "$traceback_lines" ]]; then
    printf -- '-- first E lines per failure --\n'
    printf '%s\n' "$traceback_lines"
  fi
}

# For everything else (mypy/ruff/pnpm steps): lines that look like an error,
# then the tail of the log for trailing context (e.g. a final error count).
excerpt_generic_log() { # log_file
  local log="$1" err_lines err_count cap
  cap=$((EXCERPT_CAP - 20))
  [[ "$cap" -lt 5 ]] && cap=5
  err_lines="$(grep -E 'error|Error|✖|FAIL' "$log")"
  if [[ -n "$err_lines" ]]; then
    err_count=$(printf '%s\n' "$err_lines" | wc -l | tr -d ' ')
    if [[ "$err_count" -gt "$cap" ]]; then
      printf '%s\n' "$err_lines" | head -n "$cap"
      printf '  ... %d more matching line(s) in the log\n' "$((err_count - cap))"
    else
      printf '%s\n' "$err_lines"
    fi
  else
    printf '(no line matched the usual error markers)\n'
  fi
  printf -- '-- last 20 lines --\n'
  tail -n 20 "$log"
}

# print_step_excerpt "name": look up that step's log file and print a capped,
# focused excerpt plus the log path and the command to re-run just this step.
print_step_excerpt() {
  local name="$1" log log_rel body total extra
  log="$RUN_LOG_DIR/$name.log"
  log_rel="${log#"$REPO_ROOT"/}"
  if [[ ! -s "$log" ]]; then
    printf '  (no log captured for %s)\n' "$name"
    return 0
  fi
  case "$name" in
    pytest-*)
      body="$(excerpt_pytest_log "$log")"
      if [[ -z "$body" ]]; then
        body="$(printf '(no FAILED/ERROR lines found; last 20 lines follow)\n'; tail -n 20 "$log")"
      fi
      total=$(printf '%s\n' "$body" | wc -l | tr -d ' ')
      if [[ "$total" -gt "$EXCERPT_CAP" ]]; then
        extra=$((total - EXCERPT_CAP))
        printf '%s\n' "$body" | head -n "$EXCERPT_CAP" | sed 's/^/  | /'
        printf '  | ... %d more line(s) in the log\n' "$extra"
      else
        printf '%s\n' "$body" | sed 's/^/  | /'
      fi
      ;;
    *)
      excerpt_generic_log "$log" | sed 's/^/  | /'
      ;;
  esac
  printf '  log:   %s\n' "$log_rel"
  printf '  rerun: ./scripts/ci-local.sh --only %s\n' "$name"
}

summary() {
  printf '\n%s============================ SUMMARY ============================%s\n' "$C_BOLD" "$C_RESET"
  local i
  for i in "${!STEP_NAMES[@]}"; do
    local st="${STEP_STATUS[$i]}" color sym
    case "$st" in
      PASS) color="$C_GREEN"; sym="PASS" ;;
      FAIL) color="$C_RED";   sym="FAIL" ;;
      SKIP) color="$C_YEL";   sym="SKIP" ;;
    esac
    printf '  %s%-4s%s  %-22s %s\n' "$color" "$sym" "$C_RESET" "${STEP_NAMES[$i]}" "${STEP_NOTE[$i]}"
  done
  hr

  # The failing test names / error lines have usually scrolled off-screen by
  # now. Pull a focused excerpt (from the per-step log, already written under
  # $RUN_LOG_DIR) back up here instead of making the owner re-run the whole
  # (often 90-minute) suite just to see what broke.
  if [[ "$ANY_FAIL" -eq 1 ]]; then
    printf '%sFAILURE DETAILS%s  (full logs under %s/)\n' "$C_BOLD$C_RED" "$C_RESET" "$LOG_DIR_REL"
    for i in "${!STEP_NAMES[@]}"; do
      if [[ "${STEP_STATUS[$i]}" == "FAIL" ]]; then
        printf '\n%s%s%s\n' "$C_RED" "${STEP_NAMES[$i]}" "$C_RESET"
        print_step_excerpt "${STEP_NAMES[$i]}"
      fi
    done
    hr
  fi

  # Loud, explicit callout if integration or web did not run: never let a
  # skipped leg masquerade as a fully-green tree.
  local skipped_notice=0
  for i in "${!STEP_NAMES[@]}"; do
    if [[ "${STEP_STATUS[$i]}" == "SKIP" ]]; then
      printf '%s!! NOT RUN: %s: %s%s\n' "$C_YEL$C_BOLD" "${STEP_NAMES[$i]}" "${STEP_NOTE[$i]}" "$C_RESET"
      skipped_notice=1
    fi
  done
  if [[ "$skipped_notice" -eq 1 ]]; then
    printf '%s   ^ CI WILL still run these. Green here does NOT cover the skipped legs.%s\n' "$C_YEL" "$C_RESET"
    hr
  fi
  if [[ "$ANY_FAIL" -eq 0 ]]; then
    printf '%s%sALL RAN STEPS PASSED.%s\n' "$C_GREEN" "$C_BOLD" "$C_RESET"
  else
    printf '%s%sONE OR MORE STEPS FAILED.%s\n' "$C_RED" "$C_BOLD" "$C_RESET"
  fi
}

# =============================================================================
# RUN
# =============================================================================
printf '%s%sci-local%s - mirroring .github/workflows/ci.yml\n' "$C_BOLD" "$C_BLUE" "$C_RESET"
printf 'repo: %s\n' "$REPO_ROOT"
printf 'mode: %s | integration: %s | web: %s | fail-fast: %s\n' \
  "$([[ $FAST -eq 1 ]] && echo fast || echo full)" \
  "$([[ $SKIP_INTEGRATION -eq 1 ]] && echo SKIP || echo run)" \
  "$([[ $SKIP_WEB -eq 1 ]] && echo SKIP || echo run)" \
  "$([[ $FAIL_FAST -eq 1 ]] && echo on || echo off)"
if [[ -n "$ONLY_STEP" ]]; then
  printf 'only: %s (every other step is skipped this run)\n' "$ONLY_STEP"
fi
printf 'logs: %s/\n' "$LOG_DIR_REL"

# --- ci.yml: uv sync --all-packages (both python jobs depend on this) --------
if want_step "uv-sync"; then
  run_step "uv-sync" "uv sync --all-packages" uv sync --all-packages
fi

# --- Job lint-and-type-check -------------------------------------------------
if want_step "ruff-check"; then
  run_step "ruff-check"   "uv run ruff check"          uv run ruff check
fi
if want_step "ruff-format"; then
  run_step "ruff-format"  "uv run ruff format --check"  uv run ruff format --check
fi
if want_step "mypy-strict"; then
  run_step "mypy-strict"  "mypy core+runtime+voice+connectors --strict"  uv run mypy packages/core/src packages/runtime/src packages/voice/src packages/connectors/src --strict
fi
if want_step "mypy-api"; then
  run_step "mypy-api"     "mypy packages/api/src"        uv run mypy packages/api/src
fi

# --- Job test: early collect gate, then full default suite -------------------
if want_step "pytest-collect"; then
  run_collect_gate
fi
if want_step "pytest-unit"; then
  run_step "pytest-unit"  "uv run pytest (default suite)" uv run pytest
fi

# --- Job test-integration ----------------------------------------------------
if want_step "pytest-integration"; then
  if [[ "$SKIP_INTEGRATION" -eq 1 ]]; then
    skip_step "pytest-integration" "skipped by flag/env ($([[ $FAST -eq 1 ]] && echo --fast || echo --no-integration/SKIP_INTEGRATION))"
  else
    banner "integration-db probe  ${C_RESET}${C_YEL}(disposable _test DB on :${PG_PORT})${C_RESET}"
    if probe_integration_db; then
      printf '%s[ok]%s reachable: %s + persona_app role\n' "$C_GREEN" "$C_RESET" "$TEST_DB_NAME"
      # Warn (do not fail) if the embedder model is not cached: first run would
      # hit the network (CI warms it with retries; we just surface the risk).
      if ! ls "${HOME}/.cache/huggingface/hub" 2>/dev/null | grep -qi 'bge-small-en-v1.5'; then
        printf '%s[warn]%s bge-small-en-v1.5 not in HF cache; first integration run will download it.\n' "$C_YEL" "$C_RESET"
      fi
      # Mirror ci.yml test-integration env block exactly.
      run_step "pytest-integration" "pytest -m integration" \
        env DATABASE_URL="$CI_DATABASE_URL" \
            APP_DATABASE_URL="$CI_APP_DATABASE_URL" \
            PERSONA_TEST_DB="1" \
            uv run pytest -m integration
    else
      skip_step "pytest-integration" "dev Postgres _test DB unreachable on :${PG_PORT} (start it: docker compose up -d postgres, ensure '${TEST_DB_NAME}' + persona_app exist)"
    fi
  fi
fi

# --- Job web (pnpm) ----------------------------------------------------------
if want_any_web_step; then
  if [[ "$SKIP_WEB" -eq 1 ]]; then
    skip_step "web" "skipped by flag/env ($([[ $FAST -eq 1 ]] && echo --fast || echo --no-web/SKIP_WEB))"
  elif ! command -v pnpm >/dev/null 2>&1; then
    skip_step "web" "pnpm not installed (install: corepack enable pnpm)"
  else
    WEB_DIR="$REPO_ROOT/packages/web"
    if want_step "web-install"; then
      run_step "web-install"      "pnpm install --frozen-lockfile" \
        bash -c "cd '$WEB_DIR' && pnpm install --frozen-lockfile"
    fi
    if want_step "web-typecheck"; then
      run_step "web-typecheck"    "pnpm typecheck" \
        bash -c "cd '$WEB_DIR' && pnpm typecheck"
    fi
    if want_step "web-lint"; then
      run_step "web-lint"         "pnpm lint" \
        bash -c "cd '$WEB_DIR' && pnpm lint"
    fi
    if want_step "web-no-literals"; then
      run_step "web-no-literals"  "pnpm check:no-literals" \
        bash -c "cd '$WEB_DIR' && pnpm check:no-literals"
    fi
    if want_step "web-build"; then
      # `next build` initialises Clerk at module load; CI placeholders are enough to
      # type-check + bundle (mirrors ci.yml web job env block).
      run_step "web-build"        "pnpm build (Clerk CI placeholders)" \
        bash -c "cd '$WEB_DIR' && \
          NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 \
          NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_ci_placeholder \
          CLERK_SECRET_KEY=sk_test_ci_placeholder \
          NEXT_PUBLIC_CLERK_JWT_TEMPLATE=persona-api \
          NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in \
          NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up \
          NEXT_PUBLIC_CLERK_SIGN_IN_FALLBACK_REDIRECT_URL=/personas \
          NEXT_PUBLIC_CLERK_SIGN_UP_FALLBACK_REDIRECT_URL=/personas \
          pnpm build"
    fi
    if want_step "web-test"; then
      run_step "web-test"         "pnpm test" \
        bash -c "cd '$WEB_DIR' && pnpm test"
    fi
  fi
fi

summary
[[ "$ANY_FAIL" -eq 0 ]] && exit 0 || exit 1
