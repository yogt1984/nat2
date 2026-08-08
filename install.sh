#!/usr/bin/env bash
# Set up nat2. Idempotent -- safe to re-run after a pull.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

say "nat2 install"

# --- toolchain -------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    die "uv is not installed. Get it with:
    curl -LsSf https://astral.sh/uv/install.sh | sh
then re-run this script."
fi
echo "  uv         $(uv --version)"

PYTHON_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 12) else 0)' 2>/dev/null || echo 0)
[ "$PYTHON_OK" = "1" ] || warn "  system python3 is older than 3.12; uv will fetch its own"

# --- dependencies ----------------------------------------------------------

say "syncing dependencies"
uv sync --extra dev

# --- data directories ------------------------------------------------------
# Created here rather than lazily at runtime so a permissions problem surfaces
# now, not eight hours into a capture run.

say "preparing data directories"
mkdir -p data/raw data/parquet
echo "  data/raw       captured tape (append-only, never rewritten)"
echo "  data/parquet   compacted for the read side"

# --- verify ----------------------------------------------------------------

say "verifying"
uv run nat2 help --paths >/dev/null || die "nat2 did not start"
COMMANDS=$(uv run nat2 help --paths | wc -l)
echo "  nat2 responds, $COMMANDS commands available"

cat <<'EOF'

Installed. What to do next:

  ./test.sh                      run the suite (add --live to hit the API)
  uv run nat2 help               every command, one line each

  uv run nat2 wallets seed       build the registry from HL's leaderboard
  uv run nat2 capture hl --all   start capture -- this accrues calendar time
                                 that no later code can recover, so start it
                                 before writing anything that depends on data

Capture and `nat2 cycle` are meant to run continuously, under a supervisor.
EOF
