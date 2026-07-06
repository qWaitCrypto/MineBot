from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import zlib
from dataclasses import dataclass
from typing import Any


@dataclass
class WsClient:
    sock: socket.socket

    @classmethod
    def connect(cls, host: str, port: int, path: str = "/", timeout_s: float = 5.0) -> "WsClient":
        sock = socket.create_connection((host, port), timeout=timeout_s)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = _recv_until(sock, b"\r\n\r\n", timeout_s)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket handshake failed: {response[:200]!r}")
        accept = _header(response, b"sec-websocket-accept")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            raise RuntimeError("websocket accept header mismatch")
        sock.settimeout(timeout_s)
        return cls(sock)

    def send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.sock.sendall(_client_text_frame(data))

    def recv_json(self, timeout_s: float) -> dict[str, Any]:
        self.sock.settimeout(timeout_s)
        frame = _read_frame(self.sock)
        return json.loads(frame.decode("utf-8"))

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def decode_indices(keyframe: dict[str, Any]) -> list[int]:
    encoding = keyframe.get("encoding")
    indices = keyframe.get("indices")
    if encoding == "json-array-debug-u16":
        require(isinstance(indices, list), "json-array-debug-u16 indices must be a JSON array")
        return [int(index) for index in indices]
    require(isinstance(indices, str), f"{encoding} indices must be base64 string")
    raw = zlib.decompress(base64.b64decode(indices))
    if encoding == "base64-deflate-u8":
        require(len(raw) == 4096, f"u8 keyframe expected 4096 bytes, got {len(raw)}")
        return list(raw)
    if encoding == "base64-deflate-u16le":
        require(len(raw) == 8192, f"u16 keyframe expected 8192 bytes, got {len(raw)}")
        return [raw[offset] | (raw[offset + 1] << 8) for offset in range(0, len(raw), 2)]
    raise RuntimeError(f"unknown keyframe encoding: {encoding}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def expect_type(msg: dict[str, Any] | None, msg_type: str) -> dict[str, Any]:
    require(isinstance(msg, dict), f"missing {msg_type}")
    require(msg.get("type") == msg_type, f"expected {msg_type}, got {msg}")
    return msg


def num_array(value: Any, length: int) -> bool:
    return isinstance(value, list) and len(value) == length and all(isinstance(item, int | float) for item in value)


def _recv_until(sock: socket.socket, marker: bytes, timeout_s: float) -> bytes:
    sock.settimeout(timeout_s)
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed during handshake")
        data.extend(chunk)
    return bytes(data)


def _header(response: bytes, name: bytes) -> str | None:
    prefix = name.lower() + b":"
    for line in response.split(b"\r\n"):
        if line.lower().startswith(prefix):
            return line.split(b":", 1)[1].strip().decode("ascii")
    return None


def _client_text_frame(data: bytes) -> bytes:
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    mask = os.urandom(4)
    header.extend(mask)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
    return bytes(header) + masked


def _read_frame(sock: socket.socket) -> bytes:
    while True:
        first = _recv_exact(sock, 2)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
        mask = _recv_exact(sock, 4) if masked else b""
        data = _recv_exact(sock, length)
        if masked:
            data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))

        if opcode == 0x1:
            return data
        if opcode == 0x8:
            raise RuntimeError("websocket closed by server")
        if opcode == 0x9:
            sock.sendall(_client_control_frame(0xA, data))
            continue
        if opcode == 0xA:
            continue
        raise RuntimeError(f"unexpected websocket opcode: {opcode}")


def _client_control_frame(opcode: int, data: bytes) -> bytes:
    if len(data) > 125:
        raise RuntimeError("websocket control payload too large")
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
    return bytes([0x80 | opcode, 0x80 | len(data)]) + mask + masked


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("connection closed")
        data.extend(chunk)
    return bytes(data)
