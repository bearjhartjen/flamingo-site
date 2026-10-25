#!/bin/sh
# install-watcher.sh — run the ledger watcher as a systemd user service, so it
# starts at login and keeps the repository in step with the Desktop ledger.
set -e

ROOT=$(git rev-parse --show-toplevel) || {
  printf 'install-watcher: not inside a git repository.\n' >&2
  exit 1
}

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/flamingo-ledger-watcher.service"

if [ ! -f "$ROOT/scripts/watch-ledger.py" ]; then
  printf 'install-watcher: scripts/watch-ledger.py is missing.\n' >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT" <<EOF
[Unit]
Description=Watch the Flank Ledger on the Desktop and stage updates in the repository
Documentation=file://$ROOT/README.md
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $ROOT/scripts/watch-ledger.py
WorkingDirectory=$ROOT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

printf 'Wrote %s\n' "$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now flamingo-ledger-watcher.service
printf 'Enabled and started flamingo-ledger-watcher.service\n'

printf '\nCheck it is running:\n'
printf '  systemctl --user status flamingo-ledger-watcher\n'
printf 'Follow its log:\n'
printf '  journalctl --user -u flamingo-ledger-watcher -f\n'
printf '\nTo remove it again:\n'
printf '  sh scripts/uninstall-watcher.sh\n'
