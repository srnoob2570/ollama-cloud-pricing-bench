"""The sandbox subprocess entry point (run via -m inside the sandbox).

Installs the network guard before anything else runs, then executes the working
copy's pytest suite. The parent grades the exit code: 0 (every test passed) is the
only pass; failures, collection errors, and a suite that collected nothing all
grade as a failed checker.

`main` also distinguishes a sandbox that never got to run pytest (pytest not
importable in this environment - a harness misconfiguration) from a graded run:
exit code 90 plus a fixed message, so the parent aborts loudly instead of
publishing the misconfiguration as model results.
"""

from __future__ import annotations

import os
import socket
import sys

_BLOCKED = "network access is blocked inside the sandbox"
PYTEST_UNAVAILABLE_EXIT = 90  # the sandbox itself could not start: never a model verdict

# The handshake the parent looks for: printed only once pytest imported, so
# its absence in the captured output proves the sandbox never graded anything.
SANDBOX_READY = "ocharness-sandbox: ready"


def _guard(*_args, **_kwargs):
    raise RuntimeError(_BLOCKED)


class _NoNetworkSocket(socket.socket):
    """A socket that constructs but never leaves the machine (isinstance-safe).

    Connection-oriented sends raise on connect/connect_ex; datagram exfiltration
    raises on sendto/sendmsg whether or not the socket was ever connected.
    """

    def connect(self, *args, **kwargs):
        raise RuntimeError(_BLOCKED)

    def connect_ex(self, *args, **kwargs):
        raise RuntimeError(_BLOCKED)

    def sendto(self, *args, **kwargs):
        raise RuntimeError(_BLOCKED)

    def sendmsg(self, *args, **kwargs):
        raise RuntimeError(_BLOCKED)


def _no_self_exit(*_args, **_kwargs):
    raise RuntimeError("hard process exit is blocked inside the sandbox")


def install_network_guard() -> None:
    """Every outbound connection, datagram send, and name resolution raises.

    Local sockets still construct (bind/listen are not escapes). Boundary, on
    the record: the guard covers THIS process's sockets - a subprocess the
    model's code spawns runs outside it. The study does not treat the model as
    adversarial; the guard's job is that accidental network use fails loudly.
    """
    socket.socket = _NoNetworkSocket
    socket.create_connection = _guard
    socket.getaddrinfo = _guard
    socket.gethostbyname = _guard
    socket.gethostbyname_ex = _guard
    socket.gethostbyaddr = _guard
    socket.getnameinfo = _guard
    # The only exits the checker cannot see through: an uncatchable exit-0 from
    # a planted module would end the graded run as a pass. SIGKILL to self
    # grades as a signal death (a fail), so it needs no defense.
    os._exit = _no_self_exit
    os.abort = _no_self_exit


def main() -> int:
    install_network_guard()
    try:
        import pytest
    except ImportError:
        print(
            "ocharness-sandbox: pytest is not importable by the sandbox interpreter - "
            "the checkers cannot run (install the project's dev dependencies)",
            file=sys.stderr,
        )
        return PYTEST_UNAVAILABLE_EXIT
    print(SANDBOX_READY)
    sys.stdout.flush()
    # The parent pins pytest's config with `-c`; "." is the graded copy itself.
    return pytest.main(["-q", "--color=no", "-rA", "-p", "no:cacheprovider", *sys.argv[1:], "."])


if __name__ == "__main__":
    sys.exit(main())
