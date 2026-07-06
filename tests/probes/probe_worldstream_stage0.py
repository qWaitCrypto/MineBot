#!/usr/bin/env python3
"""Stage-0 probe for the MineBot world-stream bridge.

This probe uses only the Python standard library. It connects to the bridge
WebSocket, performs HELLO/SUBSCRIBE, and verifies that a real TRANSFORM plus one
SECTION_KEYFRAME arrive with contiguous seq/server_tick metadata.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from ws_probe import WsClient, decode_indices, expect_type, num_array, require


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_BOT = "Bot1"
DEFAULT_DIMENSION = "minecraft:overworld"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe MineBot world-stream Stage 0.")
    parser.add_argument("--host", default=os.environ.get("MINEBOT_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MINEBOT_BRIDGE_PORT", DEFAULT_PORT)))
    parser.add_argument("--bot", default=os.environ.get("MINEBOT_WORLDSTREAM_BOT", DEFAULT_BOT))
    parser.add_argument("--dimension", default=os.environ.get("MINEBOT_WORLDSTREAM_DIMENSION", DEFAULT_DIMENSION))
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--linger-after-success", type=float, default=0.0)
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
        hello = expect_type(client.recv_json(args.timeout), "HELLO_ACK")
        require(hello.get("protocol") == "world-stream/1", "HELLO_ACK protocol mismatch")
        require("sections" in hello.get("capabilities", []), "HELLO_ACK missing sections capability")
        require("transform" in hello.get("capabilities", []), "HELLO_ACK missing transform capability")
        encodings = hello.get("encodings")
        require(isinstance(encodings, list), "HELLO_ACK missing encodings")
        for encoding in ("json-array-debug-u16", "base64-deflate-u8", "base64-deflate-u16le"):
            require(encoding in encodings, f"HELLO_ACK missing encoding {encoding}")

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
            require(seq == last_seq + 1, f"non-contiguous seq: {seq} after {last_seq}")
            last_seq = seq
            require("server_tick" in msg, f"{msg.get('type')} missing server_tick")
            require(msg.get("channel") == "world-stream", f"wrong channel: {msg!r}")
            msg_type = str(msg.get("type"))
            if msg_type == "ERROR":
                raise RuntimeError(f"world-stream error: {msg}")
            seen.setdefault(msg_type, msg)

        ack = expect_type(seen.get("ACK"), "ACK")
        transform = expect_type(seen.get("TRANSFORM"), "TRANSFORM")
        keyframe = expect_type(seen.get("SECTION_KEYFRAME"), "SECTION_KEYFRAME")

        require(transform.get("entity") == args.bot, f"transform entity mismatch: {transform}")
        require(num_array(transform.get("pos"), 3), f"invalid transform pos: {transform}")
        require(isinstance(keyframe.get("palette"), list) and keyframe["palette"], "empty keyframe palette")
        indices = decode_indices(keyframe)
        require(len(indices) == 4096, f"expected 4096 section indices, got {len(indices)}")
        palette_size = len(keyframe["palette"])
        require(all(isinstance(index, int) and 0 <= index < palette_size for index in indices), "keyframe index outside palette")

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
        if args.linger_after_success > 0:
            time.sleep(args.linger_after_success)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted; websocket closed", file=sys.stderr)
        raise SystemExit(130)
