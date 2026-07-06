from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SectionKey:
    x: int
    y: int
    z: int


@dataclass(frozen=True)
class Keyframe:
    sub_id: str
    dimension: str
    section: SectionKey
    palette: tuple[str, ...]
    indices: list[int]
    encoding: str


@dataclass(frozen=True)
class TransformSample:
    entity: str
    dimension: str
    pos: tuple[float, float, float]
    yaw: float
    pitch: float
    on_ground: bool
    pose: str
    monotonic_s: float


@dataclass(frozen=True)
class WorldStreamReconnect:
    reason: str
    attempt: int


class SequenceTracker:
    def __init__(self, initial_seq: int = 0) -> None:
        self.last_seq = initial_seq

    def check(self, message: dict[str, Any]) -> None:
        seq = int(message.get("seq", -1))
        expected = self.last_seq + 1
        if seq != expected:
            raise RuntimeError(f"world-stream sequence gap: got {seq}, expected {expected}")
        self.last_seq = seq


class WsClient:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

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


def read_stream(
    host: str,
    port: int,
    bot: str,
    dimension: str,
    radius_chunks: int,
    y_band_sections: tuple[int, int],
    rate_hz: int,
    timeout_s: float,
) -> Iterator[dict[str, Any] | WorldStreamReconnect]:
    attempt = 0
    backoff_s = 0.25
    while True:
        try:
            yield from _read_subscription(
                host=host,
                port=port,
                bot=bot,
                dimension=dimension,
                radius_chunks=radius_chunks,
                y_band_sections=y_band_sections,
                rate_hz=rate_hz,
                timeout_s=timeout_s,
            )
            attempt = 0
            backoff_s = 0.25
        except (OSError, RuntimeError, TimeoutError) as exc:
            attempt += 1
            yield WorldStreamReconnect(reason=str(exc), attempt=attempt)
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2.0, 2.0)


def _read_subscription(
    host: str,
    port: int,
    bot: str,
    dimension: str,
    radius_chunks: int,
    y_band_sections: tuple[int, int],
    rate_hz: int,
    timeout_s: float,
) -> Iterator[dict[str, Any]]:
    client = WsClient.connect(host, port, timeout_s=timeout_s)
    try:
        client.send_json(
            {
                "id": "h1",
                "channel": "world-stream",
                "type": "HELLO",
                "protocol": "world-stream/1",
                "client": "minebot-camera/stage1",
                "accept": ["json"],
                "max_radius_chunks": radius_chunks,
            }
        )
        hello = client.recv_json(timeout_s)
        if hello.get("type") != "HELLO_ACK":
            raise RuntimeError(f"expected HELLO_ACK, got {hello}")
        tracker = SequenceTracker(int(hello.get("seq", 0)))
        yield hello

        client.send_json(
            {
                "id": "s1",
                "channel": "world-stream",
                "type": "SUBSCRIBE",
                "sub_id": "camera-follow",
                "center": {"type": "entity", "entity": bot},
                "dimension": dimension,
                "radius_chunks": radius_chunks,
                "y_band_sections": list(y_band_sections),
                "rate_hz": rate_hz,
            }
        )
        while True:
            msg = client.recv_json(timeout_s)
            tracker.check(msg)
            if msg.get("type") == "ERROR":
                raise RuntimeError(f"world-stream error: {msg}")
            yield msg
    finally:
        client.close()


def parse_keyframe(message: dict[str, Any]) -> Keyframe:
    section = message.get("section")
    if not (isinstance(section, list) and len(section) == 3):
        raise RuntimeError(f"invalid section keyframe section: {message}")
    palette = message.get("palette")
    if not (isinstance(palette, list) and palette):
        raise RuntimeError(f"invalid section keyframe palette: {message}")
    indices = decode_indices(message)
    if len(indices) != 4096:
        raise RuntimeError(f"expected 4096 section indices, got {len(indices)}")
    return Keyframe(
        sub_id=str(message.get("sub_id", "")),
        dimension=str(message.get("dimension", "")),
        section=SectionKey(int(section[0]), int(section[1]), int(section[2])),
        palette=tuple(str(item) for item in palette),
        indices=indices,
        encoding=str(message.get("encoding")),
    )


def parse_transform(message: dict[str, Any]) -> TransformSample:
    pos = message.get("pos")
    if not (isinstance(pos, list) and len(pos) == 3):
        raise RuntimeError(f"invalid transform position: {message}")
    return TransformSample(
        entity=str(message.get("entity", "")),
        dimension=str(message.get("dimension", "")),
        pos=(float(pos[0]), float(pos[1]), float(pos[2])),
        yaw=float(message.get("yaw", 0.0)),
        pitch=float(message.get("pitch", 0.0)),
        on_ground=bool(message.get("on_ground", False)),
        pose=str(message.get("pose", "")),
        monotonic_s=time.monotonic(),
    )


def decode_indices(keyframe: dict[str, Any]) -> list[int]:
    encoding = keyframe.get("encoding")
    indices = keyframe.get("indices")
    if encoding == "json-array-debug-u16":
        if not isinstance(indices, list):
            raise RuntimeError("json-array-debug-u16 indices must be a JSON array")
        return [int(index) for index in indices]
    if not isinstance(indices, str):
        raise RuntimeError(f"{encoding} indices must be base64 string")
    raw = zlib.decompress(base64.b64decode(indices))
    if encoding == "base64-deflate-u8":
        if len(raw) != 4096:
            raise RuntimeError(f"u8 keyframe expected 4096 bytes, got {len(raw)}")
        return list(raw)
    if encoding == "base64-deflate-u16le":
        if len(raw) != 8192:
            raise RuntimeError(f"u16 keyframe expected 8192 bytes, got {len(raw)}")
        return [raw[offset] | (raw[offset + 1] << 8) for offset in range(0, len(raw), 2)]
    raise RuntimeError(f"unknown keyframe encoding: {encoding}")


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
