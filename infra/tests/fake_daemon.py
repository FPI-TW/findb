"""Stateful fake commands served from the pytest process.

Shell transaction suites issue hundreds of fake docker/sudo calls per case.
Starting a Python interpreter for each call dominated their runtime, so every
fake command is a bash client that forwards argv (and the named environment
variables) to a localhost daemon. Calls are serialized on one in-memory state.
"""

import copy
import socketserver
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

CLIENT = r"""#!/bin/bash
exec 3<>"/dev/tcp/127.0.0.1/$FAKE_DAEMON_PORT" || exit 99
environment=()
for name in $FAKE_DAEMON_ENV; do
  if [ -n "${!name+x}" ]; then environment+=("$name=${!name}"); fi
done
printf '%s\0' "$#" "${#environment[@]}" "$PPID" "${0##*/}" "$@" \
  ${environment[@]+"${environment[@]}"} >&3
IFS= read -r status <&3
IFS= read -r -d '' output <&3
printf '%s' "$output"
if [ "$status" = local ]; then
  # Run the remaining arguments with the test interpreter, keeping stdin.
  shift
  "$FAKE_DAEMON_PYTHON" "$@" >/dev/null 2>&1
  exit
fi
exit "${status:-98}"
"""

# (command, args, parent pid, forwarded environment) -> (exit status or "local", stdout)
Handler = Callable[[str, list[str], int, dict[str, str]], tuple[int | str, str]]


def install(binaries: Path, names: tuple[str, ...]) -> None:
    binaries.mkdir(exist_ok=True)
    for name in names:
        path = binaries / name
        path.write_text(CLIENT)
        path.chmod(0o700)


@contextmanager
def serve(state: dict, handle: Handler, environment: tuple[str, ...] = ()) -> Iterator[dict]:
    """Yield the environment that routes installed clients to ``handle``.

    A handler exception behaves like the former per-call process crashing:
    exit status 1, no output, and no state change (including its log entry).
    The append-only ``state["log"]`` is truncated rather than copied per call.
    """
    lock = threading.Lock()

    class Request(socketserver.BaseRequestHandler):
        def handle(self):
            data = b""
            fields: list[str] = []
            while len(fields) < 4 or len(fields) < 4 + int(fields[0]) + int(fields[1]):
                chunk = self.request.recv(65536)
                if not chunk:
                    return
                data += chunk
                fields = [item.decode() for item in data.split(b"\0")[:-1]]
            argc, envc, parent, command = int(fields[0]), int(fields[1]), int(fields[2]), fields[3]
            args = fields[4 : 4 + argc]
            forwarded = dict(item.split("=", 1) for item in fields[4 + argc : 4 + argc + envc])
            with lock:
                saved = copy.deepcopy({k: v for k, v in state.items() if k != "log"})
                logged = len(state.get("log", ()))
                try:
                    status, output = handle(command, args, parent, forwarded)
                except Exception:
                    log = state.get("log")
                    state.clear()
                    state.update(saved)
                    if log is not None:
                        state["log"] = log[:logged]
                    status, output = 1, ""
            self.request.sendall(f"{status}\n{output}".encode())

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Request)
    server.daemon_threads = True
    # The default 0.5s poll interval would delay every teardown by up to 0.5s.
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield {
            "FAKE_DAEMON_PORT": str(server.server_address[1]),
            "FAKE_DAEMON_ENV": " ".join(environment),
            "FAKE_DAEMON_PYTHON": sys.executable,
        }
    finally:
        server.shutdown()
        server.server_close()
