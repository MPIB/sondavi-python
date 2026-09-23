#!/usr/bin/env python3
"""The whole Python test run in one command.

    python tests/run.py

Python rather than a shell script, so the run is the same on every system: no CRLF
surprises on checkout, no `python3` that turns out to be a Store stub, no backgrounding
and `kill` under Git Bash.

Two things it does that a one-liner would not, both because a CI failure has to explain
itself without a second run: it picks a FREE port instead of a fixed one, and when the
fixture server does not come up it prints what the server said.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def free_port() -> int:
    """A port nothing else holds.

    A fixed one is a bet on the machine: CI runners carry their own services (macOS has
    several listening by default), and losing that bet looks exactly like a broken client.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fixtures_answer(port: int) -> bool:
    """Whether OUR fixture server is on that port — not merely whether something is.

    Checking the port alone was not enough: a foreign service listening there passes that
    check, and the tests then run against it and hang instead of failing.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/surveys", timeout=1) as resp:
            return b'"data"' in resp.read()
    except urllib.error.HTTPError as err:
        # 401 is the right answer to a request without a token: that is our server.
        return err.code == 401
    except Exception:  # noqa: BLE001 - not up yet, or not ours
        return False


def main() -> int:
    port = int(os.environ.get("SONDAVI_TEST_PORT") or free_port())

    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixture-server.py"), str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def server_said() -> str:
        try:
            server.terminate()
            out, _ = server.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - diagnostics must not fail themselves
            return "(keine Ausgabe)"
        return (out or "").strip() or "(keine Ausgabe)"

    try:
        # Waiting for the port rather than sleeping a fixed second: a loaded CI runner needs
        # longer, and a fixed wait then fails for a reason that has nothing to do with the client.
        for _ in range(50):
            if fixtures_answer(port):
                break
            if server.poll() is not None:
                print(f"Der Pruefstand endete sofort (Code {server.returncode}) auf Port {port}.",
                      file=sys.stderr)
                print(server_said(), file=sys.stderr)
                return 1
            time.sleep(0.2)
        else:
            print(f"Auf Port {port} hat der Pruefstand binnen 10 s nicht geantwortet.", file=sys.stderr)
            print(server_said(), file=sys.stderr)
            return 1

        env = dict(os.environ, SONDAVI_TEST_PORT=str(port))
        return subprocess.call([sys.executable, os.path.join(HERE, "test_client.py")], env=env)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
