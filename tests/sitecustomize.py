"""Propagate the deterministic test network policy into child interpreters."""

from __future__ import annotations

import ipaddress
import os
import socket


if os.environ.get("SOXS_OFFLINE_TEST") == "1":
    blocked_proxy_ports = {1080, 1081, 1082, 3128, 7890}

    def _localhost(host: object) -> bool:
        value = str(host or "").strip().lower().strip("[]")
        if value in {"localhost", "localhost.localdomain"}:
            return True
        try:
            return ipaddress.ip_address(value).is_loopback
        except ValueError:
            return False

    def _check(address: object, operation: str) -> None:
        host = address[0] if isinstance(address, tuple) and address else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else "?"
        if not _localhost(host) or port in blocked_proxy_ports:
            raise RuntimeError(
                f"QCNONET001: child external network blocked: "
                f"operation={operation} host={host!r} port={port!r}"
            )

    _connect = socket.socket.connect
    _connect_ex = socket.socket.connect_ex
    _create_connection = socket.create_connection
    _getaddrinfo = socket.getaddrinfo

    def connect(sock: socket.socket, address: object):
        _check(address, "socket.connect")
        return _connect(sock, address)

    def connect_ex(sock: socket.socket, address: object):
        _check(address, "socket.connect_ex")
        return _connect_ex(sock, address)

    def create_connection(address: object, *args: object, **kwargs: object):
        _check(address, "socket.create_connection")
        return _create_connection(address, *args, **kwargs)

    def getaddrinfo(host: object, port: object, *args: object, **kwargs: object):
        if not _localhost(host):
            raise RuntimeError(
                f"QCNONET001: child external DNS blocked: host={host!r} port={port!r}"
            )
        return _getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo
