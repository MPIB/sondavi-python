#!/usr/bin/env python3
"""The whole Python test run in one command.

    python tests/run.py

Python rather than a shell script, so the run is the same on every system: no CRLF
surprises on checkout, no `python3` that turns out to be a Store stub, no backgrounding
and `kill` under Git Bash. (None of those was the Windows failure that prompted this --
that was the console encoding, fixed in test_client.py. Removing the shell removes the
class of problem rather than one instance of it.)
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    server = subprocess.Popen([sys.executable, os.path.join(HERE, "fixture-server.py"), str(PORT)])

    try:
        # Waiting for the port rather than sleeping a fixed second: a loaded CI runner needs
        # longer, and a fixed wait then fails for a reason that has nothing to do with the client.
        for _ in range(50):
            if port_open(PORT):
                break
            if server.poll() is not None:
                print("Der Pruefstand ist nicht gestartet.", file=sys.stderr)
                return 1
            time.sleep(0.2)
        else:
            print(f"Port {PORT} wurde nicht offen.", file=sys.stderr)
            return 1

        return subprocess.call([sys.executable, os.path.join(HERE, "test_client.py")])
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
