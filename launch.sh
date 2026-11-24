#!/bin/sh
# Open the Flamingo & Cactus website builder (loopback only).
# If it is already running, just open the browser to it instead of
# starting a second copy (which would fail: the port is taken).
# Otherwise start it, wait for the port, then open Chromium on it.
URL="http://127.0.0.1:8767/"
LOG="$HOME/.local/state/flamingo-site.log"

PORT_UP() {
  python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1', 8767)) == 0 else 1)"
}

# Chromium is the browser this machine actually uses. The system default
# handler is the OpenCode desktop app, which opens nothing for http URLs,
# so the builder used to start and then look dead. Ask Chromium directly.
open_builder() {
  for b in chromium-browser chromium google-chrome brave-browser microsoft-edge; do
    if command -v "$b" >/dev/null 2>&1; then
      # A new window, not a new tab: an existing Chromium window can sit on
      # another virtual desktop or stay minimised, and then a new tab looks
      # like nothing happened.
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

if ! PORT_UP; then
  mkdir -p "$(dirname "$LOG")"
  # Run detached so the server survives after the launcher exits.
  # stderr (startup errors, request log) goes to the log file.
  nohup python3 "$HOME/Flamingo&Cactus/flamingo-site/edit.py" --no-open "$@" >>"$LOG" 2>&1 &
  # Wait up to ~15s for the server to come up before opening the browser.
  i=0
  while [ "$i" -lt 30 ] && ! PORT_UP; do i=$((i+1)); sleep 0.5; done
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
