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
touch .nat2   # marks this as the data home for `nat2` run from elsewhere
echo "  data/raw       captured tape (append-only, never rewritten)"
echo "  data/parquet   compacted for the read side"

# --- put nat2 on PATH ------------------------------------------------------
# Without this step the command only exists inside .venv, so `nat2` is not a
# command you can actually type -- which is the whole point of installing it.
# Editable, so a code change takes effect without reinstalling.

say "recording the data home"
CONFIG_DIR="${HOME}/.config/nat2"
mkdir -p "$CONFIG_DIR"
printf '%s\n' "$PWD" > "$CONFIG_DIR/home"
echo "  $CONFIG_DIR/home -> $PWD"
echo "  so \`nat2\` run from anywhere uses this store, not the directory you are in"

say "installing the nat2 command"
BIN_DIR="${HOME}/.local/bin"
mkdir -p "$BIN_DIR"

if uv tool install --editable . --force >/dev/null 2>&1; then
    echo "  installed via uv tool"
else
    warn "  uv tool install failed; linking the venv entry point instead"
    ln -sf "$PWD/.venv/bin/nat2" "$BIN_DIR/nat2"
    echo "  linked $BIN_DIR/nat2 -> $PWD/.venv/bin/nat2"
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "  $BIN_DIR is not on your PATH. Add this to your shell profile:
      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# --- verify ----------------------------------------------------------------

say "verifying"
uv run nat2 help --paths >/dev/null || die "nat2 did not start"
COMMANDS=$(uv run nat2 help --paths | wc -l)
echo "  $COMMANDS commands available"

if command -v nat2 >/dev/null 2>&1; then
    echo "  \`nat2\` resolves to $(command -v nat2)"
else
    warn "  \`nat2\` is not on PATH yet -- open a new shell, or use \`uv run nat2\`"
fi

cat <<EOF

Installed. nat2 works from any directory: it finds this project by walking up
for a data/ directory, and \`NAT2_HOME\` overrides that. Every command reports
which store it opened, so an empty answer is never mistaken for a real one.

  ./test.sh                  run the suite (add --live to hit the API)
  nat2 help                  every command, one line each

  nat2 wallets seed          build the registry from HL's leaderboard
  nat2 capture hl --all      start capture -- this accrues calendar time that
                             no later code can recover, so start it before
                             writing anything that depends on data

Capture and \`nat2 cycle\` are meant to run continuously, under a supervisor.
Current data home: $PWD
EOF
