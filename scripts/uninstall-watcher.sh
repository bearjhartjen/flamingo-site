#!/bin/sh
# uninstall-watcher.sh — stop and remove the ledger watcher service.
set -e

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/flamingo-ledger-watcher.service"

systemctl --user disable --now flamingo-ledger-watcher.service 2>/dev/null \
  || printf 'service was not running\n'

if [ -f "$UNIT" ]; then
  rm -f "$UNIT"
  printf 'Removed %s\n' "$UNIT"
fi

systemctl --user daemon-reload
printf 'Done. The git hooks still work, but a Desktop edit will no longer be\n'
printf 'staged automatically - run "sh scripts/sync-ledger.sh" by hand instead.\n'
