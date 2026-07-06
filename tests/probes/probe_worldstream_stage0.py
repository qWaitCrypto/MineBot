#!/usr/bin/env python3
"""Stage-0 probe for the MineBot world-stream bridge.

This probe uses only the Python standard library. It connects to the bridge
WebSocket, performs HELLO/SUBSCRIBE, and verifies that a real TRANSFORM plus one
SECTION_KEYFRAME arrive with contiguous seq/server_tick metadata.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_BOT = "Bot1"
DEFAULT_DIMENSION = "minecraft:overworld"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe MineBot world-stream Stage 0.")
    parser.add_argument("--host", default=os.environ.get("MINEBOT_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MINEBOT_BRIDGE_PORT", DEFAULT_PORT)))
    parser.add_argument("--bot", default=os.environ.get("MINEBOT_WORLDSTREAM_BOT", DEFAULT_BOT))
    parser.add_argument("--dimension", default=os.environ.get("MINEBOT_WORLDSTREAM_DIMENSION", DEFAULT_DIMENSION))
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    client = WsClient.connect(args.host, args.port, timeout_s=args.timeout)
    try:
        client.send_json(
            {
                "id": "h1",
                "channel": "world-stream",
                "type": "HELLO",
                "protocol": "world-stream/1",
                "client": "minebot-probe/stage0",
                "accept": ["json"],
                "max_radius_chunks": 1,
            }
        )
        hello = _expect_type(client.recv_json(args.timeout), "HELLO_ACK")
        _require(hello.get("protocol") == "world-stream/1", "HELLO_ACK protocol mismatch")
        _require("sections" in hello.get("capabilities", []), "HELLO_ACK missing sections capability")
        _require("transform" in hello.get("capabilities", []), "HELLO_ACK missing transform capability")
        encodings = hello.get("encodings")
        _require(isinstance(encodings, list), "HELLO_ACK missing encodings")
        for encoding in ("json-array-debug-u16", "base64-deflate-u8", "base64-deflate-u16le"):
            _require(encoding in encodings, f"HELLO_ACK missing encoding {encoding}")

        client.send_json(
            {
                "id": "s1",
                "channel": "world-stream",
                "type": "SUBSCRIBE",
                "sub_id": "probe",
                "center": {"type": "entity", "entity": args.bot},
                "dimension": args.dimension,
                "radius_chunks": 1,
                "rate_hz": 5,
            }
        )

        seen: dict[str, dict[str, Any]] = {}
        last_seq = int(hello.get("seq", 0))
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and not {"ACK", "TRANSFORM", "SECTION_KEYFRAME"} <= set(seen):
            msg = client.recv_json(max(0.1, deadline - time.monotonic()))
            seq = int(msg.get("seq", -1))
            _require(seq == last_seq + 1, f"non-contiguous seq: {seq} after {last_seq}")
            last_seq = seq
            _require("server_tick" in msg, f"{msg.get('type')} missing server_tick")
            _require(msg.get("channel") == "world-stream", f"wrong channel: {msg!r}")
            msg_type = str(msg.get("type"))
            if msg_type == "ERROR":
                raise RuntimeError(f"world-stream error: {msg}")
            seen.setdefault(msg_type, msg)

        ack = _expect_type(seen.get("ACK"), "ACK")
        transform = _expect_type(seen.get("TRANSFORM"), "TRANSFORM")
        keyframe = _expect_type(seen.get("SECTION_KEYFRAME"), "SECTION_KEYFRAME")

        _require(transform.get("entity") == args.bot, f"transform entity mismatch: {transform}")
        _require(_num_array(transform.get("pos"), 3), f"invalid transform pos: {transform}")
        _require(isinstance(keyframe.get("palette"), list) and keyframe["palette"], "empty keyframe palette")
        indices = _decode_indices(keyframe)
        _require(len(indices) == 4096, f"expected 4096 section indices, got {len(indices)}")
        palette_size = len(keyframe["palette"])
        _require(all(isinstance(index, int) and 0 <= index < palette_size for index in indices), "keyframe index outside palette")

        summary = {
            "hello": {
                "mc_version": hello.get("mc_version"),
                "capabilities": hello.get("capabilities"),
                "encodings": encodings,
                "limits": hello.get("limits"),
            },
            "ack": {"applied": ack.get("applied")},
            "transform": {
                "entity": transform.get("entity"),
                "dimension": transform.get("dimension"),
                "pos": transform.get("pos"),
                "yaw": transform.get("yaw"),
                "pitch": transform.get("pitch"),
            },
            "keyframe": {
                "dimension": keyframe.get("dimension"),
                "section": keyframe.get("section"),
                "palette_size": len(keyframe["palette"]),
                "indices": len(indices),
                "encoding": keyframe.get("encoding"),
            },
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        client.close()


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
    first = _recv_exact(sock, 2)
    opcode = first[0] & 0x0F
    if opcode == 0x8:
        raise RuntimeError("websocket closed by server")
    if opcode != 0x1:
        raise RuntimeError(f"unexpected websocket opcode: {opcode}")
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
    return data


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("connection closed")
        data.extend(chunk)
    return bytes(data)


def _expect_type(msg: dict[str, Any] | None, msg_type: str) -> dict[str, Any]:
    _require(isinstance(msg, dict), f"missing {msg_type}")
    _require(msg.get("type") == msg_type, f"expected {msg_type}, got {msg}")
    return msg


def _num_array(value: Any, length: int) -> bool:
    return isinstance(value, list) and len(value) == length and all(isinstance(item, int | float) for item in value)


def _decode_indices(keyframe: dict[str, Any]) -> list[int]:
    encoding = keyframe.get("encoding")
    indices = keyframe.get("indices")
    if encoding == "json-array-debug-u16":
        _require(isinstance(indices, list), "json-array-debug-u16 indices must be a JSON array")
        return [int(index) for index in indices]
    _require(isinstance(indices, str), f"{encoding} indices must be base64 string")
    raw = zlib.decompress(base64.b64decode(indices))
    if encoding == "base64-deflate-u8":
        _require(len(raw) == 4096, f"u8 keyframe expected 4096 bytes, got {len(raw)}")
        return list(raw)
    if encoding == "base64-deflate-u16le":
        _require(len(raw) == 8192, f"u16 keyframe expected 8192 bytes, got {len(raw)}")
        return [raw[offset] | (raw[offset + 1] << 8) for offset in range(0, len(raw), 2)]
    raise RuntimeError(f"unknown keyframe encoding: {encoding}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


if __name__ == "__main__":
    raise SystemExit(main())
