"""Every 60 seconds sample vitals, position and inventory over read-only RCON.

Usage: python tools/eval/sampler.py BOT OUT.jsonl --server-dir DIR
The JSONL schema is compatible with the original eval-sampler.py. Error rows
contain only exception class names; connection credentials are never printed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import socket
import struct
import time


def split_snbt(text: str) -> list[str]:
    """Split only top-level commas, respecting nested components and strings."""
    parts, start, depth, quote, escaped = [], 0, 0, None, False
    for i, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def inventory_counts(text: str) -> dict[str, int]:
    counts = Counter()
    for item in split_snbt(text.strip().removeprefix("[").removesuffix("]")):
        fields = {}
        for field in split_snbt(item.removeprefix("{").removesuffix("}")):
            key, sep, value = field.partition(":")
            if sep:
                fields[key.strip().strip('"')] = value.strip()
        name = re.fullmatch(r'"minecraft:([a-z0-9_]+)"', fields.get("id", ""))
        count = re.fullmatch(r"(\d+)[bBsSlL]?", fields.get("count", fields.get("Count", "")))
        if name and count:
            counts[name[1]] += int(count[1])
    return dict(counts)


class Rcon:
    """Small read-only sampler transport, including split response draining."""

    def __init__(self, host: str, port: int, password: str):
        self.sock = socket.create_connection((host, port), timeout=20)
        self.sequence = 0
        try:
            self.request(3, password)
        except Exception:
            self.sock.close()
            raise

    def close(self) -> None:
        self.sock.close()

    def read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("RCON closed")
            data.extend(chunk)
        return bytes(data)

    def request(self, kind: int, payload: str) -> str:
        self.sequence += 1
        body = struct.pack("<ii", self.sequence, kind) + payload.encode() + b"\0\0"
        self.sock.sendall(struct.pack("<i", len(body)) + body)
        parts = []
        while True:
            size = struct.unpack("<i", self.read_exact(4))[0]
            if not 10 <= size <= 12298:
                raise ValueError("invalid RCON size")
            packet = self.read_exact(size)
            ident, _ = struct.unpack("<ii", packet[:8])
            if ident != self.sequence or packet[-2:] != b"\0\0":
                raise ValueError("RCON authentication or framing failed")
            text = packet[8:-2].decode("utf-8")
            parts.append(text)
            if len(text) < 4096:
                break
            # Peek before reading the next packet; a partial header still has
            # to complete under the normal timeout rather than be discarded.
            self.sock.settimeout(0.25)
            try:
                if not self.sock.recv(1, socket.MSG_PEEK):
                    raise ConnectionError("RCON closed")
            except socket.timeout:
                break
            finally:
                self.sock.settimeout(20)
        return "".join(parts)

    def command(self, text: str) -> str:
        return self.request(2, text)


def sample(rcon: Rcon, bot: str) -> dict:
    row = {"ts": time.time()}

    def value(path: str) -> str | None:
        text = rcon.command(f"data get entity {bot} {path}")
        return None if "No entity" in text or "found no" in text else text.split("data: ", 1)[-1]

    row["gametime"] = rcon.command("time query gametime").split()[-2]
    for key, path in (("health", "Health"), ("food", "foodLevel"), ("pos", "Pos"),
                      ("dimension", "Dimension"), ("xp", "XpLevel")):
        row[key] = value(path)
    row["inventory"] = inventory_counts(value("Inventory") or "")
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bot")
    parser.add_argument("out", type=Path)
    parser.add_argument("--server-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", args.bot) or args.interval <= 0:
        parser.error("invalid bot name or interval")
    try:
        props = dict(line.split("=", 1) for line in (args.server_dir / "server.properties").read_text().splitlines()
                     if "=" in line and not line.lstrip().startswith("#"))
        port, password = int(props["rcon.port"]), props["rcon.password"]
    except Exception as exc:
        parser.exit(1, f"Cannot read RCON settings: {type(exc).__name__}\n")
    with args.out.open("a", encoding="utf-8") as out:
        try:
            while True:
                row = {"ts": time.time()}
                connection = None
                try:
                    connection = Rcon(args.host, port, password)
                    row = sample(connection, args.bot)
                except Exception as exc:
                    row["error"] = type(exc).__name__
                finally:
                    if connection is not None:
                        connection.close()
                out.write(json.dumps(row) + "\n")
                out.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
