"""No network, enforced (review EXP-001, 0.47.0).

While the simulation runs, Python's name lookups and outbound connections are
refused for every host except the simulation's own database server (and
loopback). An unfaked outside call therefore fails loudly: it raises
``OutboundBlocked`` (not an ``OSError``, so the portal's "service not
answering" handling cannot swallow it quietly) and is recorded in
``Guard.blocked`` as ``host:port``, which the results file and the stage's
"no outbound network call" check report. Host names here are configuration
values (fictional relays, the fake scanner), never personal data.

The database itself is reached through libpq; psycopg resolves its host name
in Python first, which is why the database host is allowed.
"""

from __future__ import annotations

import collections
import ipaddress
import socket


class OutboundBlocked(RuntimeError):
    """The simulation tried to reach a host outside the runner."""


def _loopback(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


class Guard:
    def __init__(self, allowed_hosts=()):
        self.allowed = {str(h).lower() for h in allowed_hosts if h}
        self.blocked: collections.Counter = collections.Counter()
        self._saved = None

    def allows(self, host) -> bool:
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        if _loopback(host):
            return True
        host = str(host).lower()
        return host in self.allowed or host.startswith("/")  # a Unix socket path

    def _refuse(self, host, port):
        self.blocked[f"{host}:{port}"] += 1
        raise OutboundBlocked(f"the simulation may not reach {host}:{port} (no network)")

    def install(self) -> None:
        if self._saved is not None:
            return
        guard = self
        real_getaddrinfo = socket.getaddrinfo
        real_create_connection = socket.create_connection
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex

        def getaddrinfo(host, port, *args, **kwargs):
            if not guard.allows(host):
                guard._refuse(host, port)
            return real_getaddrinfo(host, port, *args, **kwargs)

        def create_connection(address, *args, **kwargs):
            host, port = address[0], address[1]
            if not guard.allows(host):
                guard._refuse(host, port)
            return real_create_connection(address, *args, **kwargs)

        def _check(sock, address):
            if (
                sock.family in (socket.AF_INET, socket.AF_INET6)
                and isinstance(address, tuple)
                and not guard.allows(address[0])
            ):
                guard._refuse(address[0], address[1])

        def connect(sock, address):
            _check(sock, address)
            return real_connect(sock, address)

        def connect_ex(sock, address):
            _check(sock, address)
            return real_connect_ex(sock, address)

        self._saved = (real_getaddrinfo, real_create_connection, real_connect, real_connect_ex)
        socket.getaddrinfo = getaddrinfo
        socket.create_connection = create_connection
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex

    def uninstall(self) -> None:
        if self._saved is None:
            return
        (
            socket.getaddrinfo,
            socket.create_connection,
            socket.socket.connect,
            socket.socket.connect_ex,
        ) = self._saved
        self._saved = None


def database_hosts() -> set[str]:
    from django.conf import settings

    return {str(db.get("HOST") or "") for db in settings.DATABASES.values()} - {""}
