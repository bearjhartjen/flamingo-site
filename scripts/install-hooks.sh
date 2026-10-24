#!/bin/sh
# install-hooks.sh — wire the repo's .githooks/ into git so commits run them.
#
# GitKraken honours `core.hooksPath`, so that is the primary install; the hooks
# are also copied into .git/hooks/ as a fallback for tools that ignore it. The
# installer is idempotent: running it again simply re-applies the same state.
set -e

ROOT=$(git rev-parse --show-toplevel) || {
  printf 'install-hooks: not inside a git repository.\n' >&2
  exit 1
}
GITDIR=$(git rev-parse --absolute-git-dir) || {
  printf 'install-hooks: could not locate the git directory.\n' >&2
  exit 1
}

HOOKS="$ROOT/.githooks"
LIVE="$GITDIR/hooks"

for h in pre-commit pre-push; do
  if [ ! -f "$HOOKS/$h" ]; then
    printf 'install-hooks: missing %s\n' "$HOOKS/$h" >&2
    exit 1
  fi
done

# Primary install: point git at the tracked hooks directory.
git -C "$ROOT" config core.hooksPath .githooks
printf 'Set core.hooksPath = .githooks\n'

# Fallback install: copy into .git/hooks for tools that ignore core.hooksPath.
cp "$HOOKS/pre-commit" "$LIVE/pre-commit"
cp "$HOOKS/pre-push" "$LIVE/pre-push"
printf 'Copied hooks into %s (fallback).\n' "$LIVE"

chmod +x "$HOOKS/pre-commit" "$HOOKS/pre-push" "$LIVE/pre-commit" "$LIVE/pre-push"
printf 'Marked pre-commit and pre-push executable.\n'

printf '\nDone.\n'
printf 'Verify with: git config --get core.hooksPath\n'
printf 'Current value: %s\n' "$(git -C "$ROOT" config --get core.hooksPath)"
