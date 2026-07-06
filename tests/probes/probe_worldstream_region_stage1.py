#!/usr/bin/env python3
"""Stage-1 probe for MineBot world-stream region keyframe pacing."""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from ws_probe import WsClient, decode_indices, expect_type, num_array, require


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_BOT = "Bot1"
DEFAULT_DIMENSION = "minecraft:overworld"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe MineBot world-stream Stage 1 region flow.")
    parser.add_argument("--host", default=os.environ.get("MINEBOT_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MINEBOT_BRIDGE_PORT", DEFAULT_PORT)))
    parser.add_argument("--bot", default=os.environ.get("MINEBOT_WORLDSTREAM_BOT", DEFAULT_BOT))
    parser.add_argument("--dimension", default=os.environ.get("MINEBOT_WORLDSTREAM_DIMENSION", DEFAULT_DIMENSION))
    parser.add_argument("--radius-chunks", type=int, default=4)
    parser.add_argument("--y-band-below", type=int, default=0)
    parser.add_argument("--y-band-above", type=int, default=0)
    parser.add_argument("--rate-hz", type=int, default=5)
    parser.add_argument("--min-keyframes", type=int, default=16)
    parser.add_argument("--min-y-levels", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args(argv)

    client = WsClient.connect(args.host, args.port, timeout_s=args.timeout)
    try:
        client.send_json(
            {
                "id": "h1",
                "channel": "world-stream",
                "type": "HELLO",
                "protocol": "world-stream/1",
                "client": "minebot-probe/stage1-region",
                "accept": ["json"],
                "max_radius_chunks": args.radius_chunks,
            }
        )
        hello = expect_type(client.recv_json(args.timeout), "HELLO_ACK")
        require(hello.get("protocol") == "world-stream/1", "HELLO_ACK protocol mismatch")
        limits = hello.get("limits")
        require(isinstance(limits, dict), "HELLO_ACK missing limits")
        require(limits.get("max_keyframes_per_tick") == 4, f"unexpected keyframe budget: {limits}")

        client.send_json(
            {
                "id": "s1",
                "channel": "world-stream",
                "type": "SUBSCRIBE",
                "sub_id": "region-probe",
                "center": {"type": "entity", "entity": args.bot},
                "dimension": args.dimension,
                "radius_chunks": args.radius_chunks,
                "y_band_sections": [args.y_band_below, args.y_band_above],
                "rate_hz": args.rate_hz,
            }
        )

        last_seq = int(hello.get("seq", 0))
        ack: dict[str, Any] | None = None
        first_transform: dict[str, Any] | None = None
        keyframes: list[dict[str, Any]] = []
        decoded_sections: dict[tuple[int, int, int], dict[str, Any]] = {}
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and len(decoded_sections) < args.min_keyframes:
            msg = client.recv_json(max(0.1, deadline - time.monotonic()))
            seq = int(msg.get("seq", -1))
            require(seq == last_seq + 1, f"non-contiguous seq: {seq} after {last_seq}")
            last_seq = seq
            require("server_tick" in msg, f"{msg.get('type')} missing server_tick")
            require(msg.get("channel") == "world-stream", f"wrong channel: {msg!r}")
            msg_type = str(msg.get("type"))
            if msg_type == "ERROR":
                raise RuntimeError(f"world-stream error: {msg}")
            if msg_type == "ACK" and ack is None:
                ack = msg
            elif msg_type == "TRANSFORM" and first_transform is None:
                first_transform = msg
            elif msg_type == "SECTION_KEYFRAME":
                keyframes.append(msg)
                section = tuple(int(item) for item in msg.get("section", []))
                require(len(section) == 3, f"invalid section key: {msg}")
                indices = decode_indices(msg)
                require(len(indices) == 4096, f"expected 4096 indices for {section}, got {len(indices)}")
                palette = msg.get("palette")
                require(isinstance(palette, list) and palette, f"empty palette for {section}")
                require(
                    all(isinstance(index, int) and 0 <= index < len(palette) for index in indices),
                    f"palette index outside palette for {section}",
                )
                decoded_sections.setdefault(section, msg)

        ack = expect_type(ack, "ACK")
        first_transform = expect_type(first_transform, "TRANSFORM")
        require(first_transform.get("entity") == args.bot, f"transform entity mismatch: {first_transform}")
        require(num_array(first_transform.get("pos"), 3), f"invalid transform pos: {first_transform}")
        applied = ack.get("applied")
        require(isinstance(applied, dict), f"ACK missing applied settings: {ack}")
        require(applied.get("radius_chunks") == args.radius_chunks, f"radius was not applied: {applied}")
        require(applied.get("y_band_sections") == [args.y_band_below, args.y_band_above], f"y-band was not applied: {applied}")
        require(len(decoded_sections) >= args.min_keyframes, f"expected >= {args.min_keyframes} sections, got {len(decoded_sections)}")

        center_section = tuple(int(value) >> 4 for value in first_transform["pos"])
        first_section = tuple(int(item) for item in keyframes[0].get("section", []))
        require(first_section == center_section, f"first keyframe should be center section {center_section}, got {first_section}")
        require(any(section != center_section for section in decoded_sections), "region stream only returned the center section")
        y_levels = sorted({section[1] for section in decoded_sections})
        require(len(y_levels) >= args.min_y_levels, f"expected >= {args.min_y_levels} y-levels, got {y_levels}")
        if args.y_band_below or args.y_band_above:
            expected_min_y = center_section[1] - args.y_band_below
            expected_max_y = center_section[1] + args.y_band_above
            require(min(y_levels) <= expected_min_y, f"missing lower y-band {expected_min_y}: {y_levels}")
            require(max(y_levels) >= expected_max_y, f"missing upper y-band {expected_max_y}: {y_levels}")

        summary = {
            "hello": {
                "mc_version": hello.get("mc_version"),
                "limits": limits,
            },
            "ack": {"applied": applied},
            "transform": {
                "entity": first_transform.get("entity"),
                "dimension": first_transform.get("dimension"),
                "pos": first_transform.get("pos"),
                "center_section": list(center_section),
            },
            "keyframes": {
                "decoded_unique_sections": len(decoded_sections),
                "first_section": list(first_section),
                "sample_sections": [list(section) for section in list(decoded_sections)[:10]],
                "encodings": sorted({str(frame.get("encoding")) for frame in keyframes}),
                "y_levels": y_levels,
            },
            "seq": {"last": last_seq, "contiguous": True},
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
