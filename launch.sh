#!/bin/sh
# Open the Flamingo & Cactus website builder (loopback only).
# If it is already running, just open the browser to it instead of
# starting a second copy (which would fail: the port is taken).
URL="http://127.0.0.1:8767/"
if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1', 8767)) == 0 else 1)"; then
  python3 -c "import webbrowser; webbrowser.open('http://127.0.0.1:8767/')"
else
  exec python3 "$HOME/Documents/flamingo-site/edit.py" "$@"
fi
