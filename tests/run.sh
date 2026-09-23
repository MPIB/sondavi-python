#!/usr/bin/env bash
# The whole Python test run in one command.
#
#   tests/run.sh
set -uo pipefail
cd "$(dirname "$0")/.."

# `python3` does not exist on Windows runners, where the interpreter is just `python`.
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "Kein Python gefunden." >&2; exit 1; }

"$PY" tests/fixture-server.py 8765 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT

# Wait for the port instead of sleeping a fixed second: a loaded CI runner needs longer,
# and a fixed wait then fails for a reason that has nothing to do with the client.
for _ in $(seq 1 50); do
    "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); sys.exit(0 if s.connect_ex(('127.0.0.1',8765))==0 else 1)" && break
    sleep 0.2
done

"$PY" tests/test_client.py
