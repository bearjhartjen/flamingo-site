#!/bin/sh
# sync-ledger.sh — copy the Desktop ledger into the repo and rebuild the site
# (main pages plus site/flank/), without committing. Handy for refreshing
# the site on its own.
#
#   sh scripts/sync-ledger.sh           # copy (if changed) + rebuild
#   sh scripts/sync-ledger.sh --check   # report what would change, write nothing
set -e

CHECK=0
if [ "$1" = "--check" ]; then
  CHECK=1
elif [ "$1" != "" ]; then
  printf 'sync-ledger: unknown argument "%s" (use --check)\n' "$1" >&2
  exit 2
fi

ROOT=$(git rev-parse --show-toplevel) || {
  printf 'sync-ledger: not inside a git repository.\n' >&2
  exit 1
}

SRC="$HOME/Desktop/The Flank.md"
DST="$ROOT/data/The Flank.md"
ENC="$ROOT/scripts/encrypt_flank.py"
BUILD="$ROOT/scripts/build.py"
SITE="$ROOT/site"

# Count transactions that actually carry a dollar balance. The ledger ends
# with an empty "Transaction N" template, which is not a transaction.
count_transactions() {
  awk '
    /^## Transaction / { inhdr = 1; next }
    inhdr && /New balance/ {
      line = $0
      sub(/.*\*\*New balance:\*\* */, "", line)
      if (line ~ /\$[0-9]/) count++
      inhdr = 0
    }
    END { print count + 0 }
  ' "$1"
}

if [ ! -f "$SRC" ]; then
  printf 'sync-ledger: source ledger not found: %s\n' "$SRC" >&2
  exit 1
fi

n=$(count_transactions "$SRC")

if [ "$CHECK" -eq 1 ]; then
  printf 'Ledger source:  %s (%s transactions)\n' "$SRC" "$n"
  printf 'Repo copy:      %s\n' "$DST"
  if [ -f "$ENC" ]; then
    printf 'Encryption:     would run: python3 %s\n' "$ENC"
    python3 "$ENC" --check || true
  else
    printf 'Encryption:     scripts/encrypt_flank.py not found\n'
  fi
  if [ -f "$BUILD" ]; then
    printf 'Build:          would run: python3 %s\n' "$BUILD"
  else
    printf 'Build:          scripts/build.py not found\n'
  fi
  printf 'Site output:    %s\n' "$SITE"
  printf 'Nothing written (--check).\n'
  exit 0
fi

# Normal run: encrypt the private notes into the repo copy (the Desktop
# file is the only plaintext), then rebuild. The encrypt step is
# deterministic and writes only when its output differs, so "changed"
# reports whether this run actually brought new ledger content in.
if [ ! -f "$ENC" ]; then
  printf 'sync-ledger: scripts/encrypt_flank.py not found; cannot sync safely.\n' >&2
  exit 1
fi

if [ -f "$DST" ]; then
  before=$(sha256sum "$DST" | cut -d' ' -f1)
else
  before=""
fi

if ! python3 "$ENC"; then
  printf 'sync-ledger: ledger encryption FAILED; repo copy left untouched.\n' >&2
  exit 1
fi

if [ -f "$DST" ]; then
  after=$(sha256sum "$DST" | cut -d' ' -f1)
else
  after=""
fi
if [ "$before" != "$after" ]; then changed=1; else changed=0; fi

if [ ! -f "$BUILD" ]; then
  printf 'sync-ledger: scripts/build.py not found; cannot build.\n' >&2
  exit 1
fi

if ! out=$(python3 "$BUILD" 2>&1); then
  printf '%s\n' "$out" >&2
  printf 'sync-ledger: build failed; site not updated.\n' >&2
  exit 1
fi

if [ "$changed" -eq 1 ]; then
  answer=yes
else
  answer=no
fi

printf '\nSummary\n'
printf '  Ledger changed:  %s (encrypted copy updated)\n' "$answer"
printf '  Transactions:    %s\n' "$n"
printf '  Site written to: %s\n' "$SITE"

exit 0
