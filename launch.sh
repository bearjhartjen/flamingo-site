#!/bin/sh
# Open the Flamingo & Cactus website builder (loopback only).
# If it is already running, just open the browser to it instead of
# starting a second copy (which would fail: the port is taken).
# Otherwise start it, wait for the port, then always open the browser,
# so double-clicking the launcher always shows something.
URL="http://127.0.0.1:8767/"

PORT_UP() {
  python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1', 8767)) == 0 else 1)"
}

if ! PORT_UP; then
  LOG="$HOME/.local/state/flamingo-site.log"
  mkdir -p "$(dirname "$LOG")"
  # Run detached so the server survives after the launcher exits.
  # stderr (startup errors, request log) goes to the log file.
  nohup python3 "$HOME/Flamingo&Cactus/flamingo-site/edit.py" --no-open "$@" >>"$LOG" 2>&1 &
  # Wait up to ~15s for the server to come up before opening the browser.
  i=0
  while [ "$i" -lt 30 ] && ! PORT_UP; do i=$((i+1)); sleep 0.5; done
fi

python3 -c "import webbrowser; webbrowser.open('http://127.0.0.1:8767/')"
