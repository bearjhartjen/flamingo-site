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

# "Changed" = the repo copy is missing or differs in content.
if [ ! -f "$DST" ] || ! cmp -s "$SRC" "$DST"; then
  changed=1
else
  changed=0
fi

n=$(count_transactions "$SRC")

if [ "$CHECK" -eq 1 ]; then
  printf 'Ledger source:  %s (%s transactions)\n' "$SRC" "$n"
  printf 'Repo copy:      %s\n' "$DST"
  if [ "$changed" -eq 1 ]; then
    printf 'Status:         WOULD update (source differs from repo copy)\n'
  else
    printf 'Status:         up to date (no copy needed)\n'
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

# Normal run: copy when needed, then rebuild.
if [ "$changed" -eq 1 ]; then
  mkdir -p "$(dirname "$DST")"
  cp "$SRC" "$DST"
  printf 'Copied ledger into %s\n' "$DST"
else
  printf 'Ledger already up to date; nothing copied.\n'
fi

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
printf '  Ledger changed:  %s\n' "$answer"
printf '  Transactions:    %s\n' "$n"
printf '  Site written to: %s\n' "$SITE"

exit 0
