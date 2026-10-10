"""Minimal Minecraft Server List Ping: version, players and latency, nothing else."""

from __future__ import annotations

import json
import socket
import struct
import time
from typing import Any


class PingError(RuntimeError):
    pass


def _varint(value: int) -> bytes:
    out = bytearray()
    value &= 0xFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _read_varint(connection: socket.socket) -> int:
    result = 0
    for shift in range(0, 35, 7):
        chunk = connection.recv(1)
        if not chunk:
            raise PingError("connection closed during status response")
        result |= (chunk[0] & 0x7F) << shift
        if not chunk[0] & 0x80:
            return result
    raise PingError("varint too long")


def _packet(packet_id: int, payload: bytes = b"") -> bytes:
    body = _varint(packet_id) + payload
    return _varint(len(body)) + body


def _receive(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise PingError("connection closed during status response")
        data += chunk
    return bytes(data)


def split_address(address: str, default_port: int = 25565) -> tuple[str, int]:
    host, _, port = address.strip().rpartition(":")
    if not host:
        return address.strip(), default_port
    try:
        return host.strip("[]"), int(port)
    except ValueError as exc:
        raise PingError(f"invalid server address {address!r}") from exc


def ping(address: str, *, timeout_s: float = 3.0) -> dict[str, Any]:
    """Status of the server at ``HOST:PORT``: version, protocol, players, motd, latency_ms."""
    host, port = split_address(address)
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as connection:
            encoded_host = host.encode("utf-8")
            handshake = _varint(-1) + _varint(len(encoded_host)) + encoded_host + struct.pack(">H", port) + _varint(1)
            connection.sendall(_packet(0, handshake) + _packet(0))
            _read_varint(connection)  # packet length
            if _read_varint(connection) != 0:
                raise PingError("unexpected status packet")
            raw = _receive(connection, _read_varint(connection))
            latency_ms = (time.monotonic() - started) * 1000
    except OSError as exc:
        raise PingError(f"{type(exc).__name__}: {exc}") from exc
    try:
        status = json.loads(raw)
    except ValueError as exc:
        raise PingError("status response is not JSON") from exc
    description = status.get("description")
    if isinstance(description, dict):
        motd = description.get("text", "") + "".join(
            str(part.get("text", "")) for part in description.get("extra") or [] if isinstance(part, dict)
        )
    else:
        motd = str(description or "")
    version = status.get("version") or {}
    players = status.get("players") or {}
    return {
        "version": str(version.get("name") or "?"),
        "protocol": version.get("protocol"),
        "online": players.get("online"),
        "max": players.get("max"),
        "names": [str(entry.get("name")) for entry in players.get("sample") or [] if isinstance(entry, dict)],
        "motd": motd.strip(),
        "latency_ms": round(latency_ms),
    }


__all__ = ["PingError", "ping", "split_address"]
