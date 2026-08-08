#!/usr/bin/env bash
# Run the suite, then smoke-test the CLI itself.
#
# The smoke pass enumerates commands from `nat2 help --paths` rather than a
# hardcoded list, so a newly added command is exercised automatically. That is
# the whole point: this script does not need updating when the CLI grows.
#
#   ./test.sh            unit tests + offline CLI smoke
#   ./test.sh --live     also hit the Hyperliquid API (read-only)
#   ./test.sh --quick    unit tests only
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

LIVE=0
QUICK=0
for arg in "$@"; do
    case "$arg" in
        --live) LIVE=1 ;;
        --quick) QUICK=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

PASS=0
FAIL=0
FAILED_NAMES=()

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL + 1)); FAILED_NAMES+=("$1"); }

check() {  # check <name> <command...>
    local name="$1"; shift
    if output=$("$@" 2>&1); then ok "$name"; else
        bad "$name"
        printf '%s\n' "$output" | tail -5 | sed 's/^/        /'
    fi
}

# --- unit tests ------------------------------------------------------------

say "unit tests"
if uv run pytest -q 2>&1 | tail -3; then :; else FAIL=$((FAIL + 1)); FAILED_NAMES+=("pytest"); fi
uv run pytest -q >/dev/null 2>&1 && PASS=$((PASS + 1)) || true

if [ "$QUICK" = "1" ]; then
    say "quick mode: skipping CLI smoke"
    [ "$FAIL" = "0" ] && exit 0 || exit 1
fi

# --- every command answers --help -----------------------------------------
# Derived from the CLI, so new commands are covered without touching this file.

say "CLI surface"
COMMANDS=$(uv run nat2 help --paths) || { echo "cannot enumerate commands"; exit 1; }
while read -r path; do
    [ -z "$path" ] && continue
    # shellcheck disable=SC2086
    check "nat2 $path --help" uv run nat2 $path --help
done <<< "$COMMANDS"

check "nat2 help" uv run nat2 help

# --- offline behaviour -----------------------------------------------------
# Commands that must work without the network, against a scratch state so the
# real registry and ledger are never touched.

say "offline commands"
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

check "gate status (empty ledger)" uv run nat2 gate status --ledger "$SCRATCH/l.jsonl"
check "log verify (empty ledger)" uv run nat2 log verify --ledger "$SCRATCH/l.jsonl"
check "log query (empty ledger)" uv run nat2 log query --ledger "$SCRATCH/l.jsonl"
check "wallets status (empty registry)" uv run nat2 wallets status --registry "$SCRATCH/r.sqlite"
check "wallets replay (no tape)" \
    uv run nat2 wallets replay --registry "$SCRATCH/r.sqlite" --root "$SCRATCH/raw"
check "compact (no data)" \
    uv run nat2 compact --root "$SCRATCH/raw" --out "$SCRATCH/parquet"
check "cycle --once (empty everything)" \
    uv run nat2 cycle --once --registry "$SCRATCH/r.sqlite" --ledger "$SCRATCH/l.jsonl" \
        --root "$SCRATCH/raw"

# An empty store must FAIL the feed gate, not pass it by default.
say "gates refuse rather than default to pass"
if uv run nat2 gate feed --root "$SCRATCH/raw" --ledger "$SCRATCH/l.jsonl" >/dev/null 2>&1; then
    bad "gate feed passed on an empty store"
else
    ok "gate feed FAILs on an empty store"
fi
if uv run nat2 gate map --registry "$SCRATCH/r.sqlite" --ledger "$SCRATCH/l.jsonl" \
        >/dev/null 2>&1; then
    bad "gate map passed with no snapshot"
else
    ok "gate map FAILs with no snapshot"
fi

# --- live (read-only) ------------------------------------------------------

if [ "$LIVE" = "1" ]; then
    say "live API (read-only)"
    check "universe" uv run nat2 universe --limit 3
else
    say "live checks skipped (pass --live to run them)"
fi

# --- summary ---------------------------------------------------------------

say "summary"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    printf '  failed: %s\n' "${FAILED_NAMES[*]}"
    exit 1
fi
