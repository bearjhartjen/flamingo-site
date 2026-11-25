#!/bin/sh
# Open the Flamingo & Cactus website builder (loopback only).
# If it is already running and unchanged, just open the browser to it.
# If edit.py is newer than the running server, restart it first, so a save
# can never rebuild the site with code the old process still holds in memory.
URL="http://127.0.0.1:8767/"
LOG="$HOME/.local/state/flamingo-site.log"
STAMP="$HOME/.local/state/flamingo-site.stamp"
EDIT="$HOME/Flamingo&Cactus/flamingo-site/edit.py"

PORT_UP() {
  python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1', 8767)) == 0 else 1)"
}

PORT_PID() {
  ss -ltnp 2>/dev/null | grep ':8767 ' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1
}

# Chromium is the browser this machine actually uses. The system default
# handler is the OpenCode desktop app, which opens nothing for http URLs,
# so the builder used to start and then look dead. Ask Chromium directly.
open_builder() {
  for b in chromium-browser chromium google-chrome brave-browser microsoft-edge; do
    if command -v "$b" >/dev/null 2>&1; then
      # A new window, not a new tab: an existing window can sit on another
      # virtual desktop or stay minimised, and then nothing appears to happen.
      nohup "$b" --new-window "$URL" >/dev/null 2>&1 &
      return 0
    fi
  done
  if command -v xdg-open >/dev/null 2>&1; then
    nohup xdg-open "$URL" >/dev/null 2>&1 &
    return 0
  fi
  return 1
}

# Restart a running server whose code is older than edit.py on disk.
# When the systemd user unit exists it owns the process, so ask it to
# restart rather than killing the pid behind its back.
UNIT="flamingo-site-builder.service"
if systemctl --user cat "$UNIT" >/dev/null 2>&1; then
  if ! systemctl --user is-active --quiet "$UNIT"; then
    systemctl --user start "$UNIT" 2>/dev/null
    touch "$STAMP"
  elif [ -f "$STAMP" ] && [ "$EDIT" -nt "$STAMP" ]; then
    systemctl --user restart "$UNIT" 2>/dev/null
    touch "$STAMP"
  fi
  i=0
  while [ "$i" -lt 30 ] && ! PORT_UP; do i=$((i+1)); sleep 0.5; done
elif PORT_UP && [ -f "$STAMP" ] && [ "$EDIT" -nt "$STAMP" ]; then
  PID="$(PORT_PID)"
  if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null
    i=0
    while [ "$i" -lt 10 ] && PORT_UP; do i=$((i+1)); sleep 0.3; done
  fi
fi

if ! PORT_UP; then
  mkdir -p "$(dirname "$LOG")"
  # Run detached so the server survives after the launcher exits.
  # stderr (startup errors, request log) goes to the log file.
  nohup python3 "$HOME/Flamingo&Cactus/flamingo-site/edit.py" --no-open "$@" >>"$LOG" 2>&1 &
  # Wait up to ~15s for the server to come up before opening the browser.
  i=0
  while [ "$i" -lt 30 ] && ! PORT_UP; do i=$((i+1)); sleep 0.5; done
  if PORT_UP; then touch "$STAMP"; fi
fi

if ! PORT_UP; then
  # Something is wrong; say so instead of failing silently.
  MSG="The site builder did not start. See $LOG"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --text="$MSG" 2>/dev/null &
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "Flamingo Site Builder" "$MSG" 2>/dev/null
  fi
  echo "$MSG" >&2
  exit 1
fi

open_builder
