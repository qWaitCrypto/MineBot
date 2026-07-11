"""Interactive R0 probe for the typed observer-control channel.

This is an isolated-test driver, not part of the Camera production package.
It never accepts Minecraft commands and never prints its generated lease id.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shlex
import sys
from collections.abc import Mapping
from typing import Any

import websockets


CHANNEL = "observer-control"
PROTOCOL = "observer-control/1"


class ProbeError(RuntimeError):
    """Raised when the bridge rejects an R0 probe request."""


class ObserverControlProbe:
    def __init__(
        self,
        websocket: Any,
        *,
        observer_id: str,
        generation: int,
        target: str,
        heartbeat_s: float,
    ) -> None:
        self.websocket = websocket
        self.observer_id = observer_id
        self.generation = generation
        self.target = target
        self.heartbeat_s = heartbeat_s
        self.lease_id = secrets.token_hex(16)
        self._counter = 0
        self._request_lock = asyncio.Lock()
        self._attached = False
        self._expire_requested = False

    async def start(self) -> None:
        hello = await self._request("HELLO", protocol=PROTOCOL)
        if hello.get("protocol") != PROTOCOL:
            raise ProbeError("bridge negotiated an unexpected protocol")
        attached = await self._mutation(
            "ATTACH",
            mode="follow",
            target_name=self.target,
            follow=_follow_settings(),
        )
        self._attached = True
        print(
            "READY"
            f" state={attached.get('state')}"
            f" mode={attached.get('mode')}"
            f" generation={attached.get('generation')}",
            flush=True,
        )

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            response = await self._mutation("HEARTBEAT")
            if self._counter % 15 == 0:
                print(f"HEARTBEAT state={response.get('state')}", flush=True)

    async def command_loop(self) -> None:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            tokens = shlex.split(line)
            if not tokens:
                continue
            command, *values = tokens
            if command == "status" and not values:
                await self.status()
            elif command == "stale" and not values:
                await self._stale_update()
            elif command == "follow":
                await self._follow(values)
            elif command == "fixed":
                await self._fixed(values)
            elif command == "detach" and not values:
                await self.detach()
                return
            elif command == "expire" and not values:
                self._expire_requested = True
                print("EXPIRY armed", flush=True)
                return
            else:
                print(
                    "COMMANDS status | stale | follow [distance azimuth elevation stiffness fov] | "
                    "fixed x y z look_x look_y look_z [allow_chunk_loading] | detach | expire",
                    flush=True,
                )

    async def detach(self) -> None:
        if not self._attached:
            return
        response = await self._advance("DETACH")
        self._attached = False
        print(f"DETACH type={response.get('type')}", flush=True)

    async def close(self) -> None:
        if self._attached and not self._expire_requested:
            try:
                await self.detach()
            except Exception as error:  # cleanup must not hide the primary failure
                print(f"CLEANUP error={type(error).__name__}", file=sys.stderr, flush=True)

    async def status(self) -> None:
        response = await self._request("STATUS")
        observer = response.get("observer", {})
        server = response.get("server", {})
        telemetry = response.get("client_telemetry", {})
        print(
            "STATUS"
            f" state={response.get('state')}"
            f" mode={response.get('mode')}"
            f" spectator={observer.get('spectator')}"
            f" operator={observer.get('operator')}"
            f" target={observer.get('camera_target_id')}"
            f" loaded_chunks={server.get('loaded_chunks')}"
            f" online_players={server.get('online_players')}",
            f" observer_denials={json.dumps(server.get('observer_denials', {}), sort_keys=True)}",
            f" frame_samples={telemetry.get('sample_count')}"
            f" frame_window_ms={telemetry.get('window_ms')}"
            f" frame_mean_ms={telemetry.get('mean_frame_ms')}"
            f" frame_p50_ms={telemetry.get('p50_frame_ms')}"
            f" frame_p95_ms={telemetry.get('p95_frame_ms')}"
            f" frame_p99_ms={telemetry.get('p99_frame_ms')}"
            f" frame_age_ticks={telemetry.get('age_ticks')}"
            f" frame_stale={telemetry.get('stale')}"
            f" adapter_state={telemetry.get('adapter_state')}",
            flush=True,
        )

    async def _stale_update(self) -> None:
        try:
            await self._mutation(
                "UPDATE",
                mode="follow",
                target_name=self.target,
                follow=_follow_settings(),
            )
        except ProbeError as error:
            if str(error) != "invalid_request: lease or generation is stale":
                raise
            print("EXPECTED_ERROR stale_generation_rejected", flush=True)
            return
        raise ProbeError("bridge accepted a stale generation")

    async def _follow(self, values: list[str]) -> None:
        if len(values) not in {0, 5}:
            raise ProbeError("follow expects zero or five numeric arguments")
        settings = _follow_settings()
        if values:
            distance, azimuth, elevation, stiffness, fov = map(float, values)
            settings |= {
                "distance": distance,
                "azimuth_deg": azimuth,
                "elevation_deg": elevation,
                "stiffness": stiffness,
                "fov_deg": fov,
            }
        response = await self._advance(
            "UPDATE",
            mode="follow",
            target_name=self.target,
            follow=settings,
        )
        print(f"UPDATE state={response.get('state')} mode={response.get('mode')}", flush=True)

    async def _fixed(self, values: list[str]) -> None:
        if len(values) not in {6, 7}:
            raise ProbeError("fixed expects six coordinates and an optional boolean")
        coordinates = [float(value) for value in values[:6]]
        allow_chunk_loading = len(values) == 7 and _parse_bool(values[6])
        response = await self._advance(
            "UPDATE",
            mode="fixed",
            fixed={
                "dimension": "minecraft:overworld",
                "position": coordinates[:3],
                "look_at": coordinates[3:],
                "allow_chunk_loading": allow_chunk_loading,
            },
        )
        print(f"UPDATE state={response.get('state')} mode={response.get('mode')}", flush=True)

    async def _advance(self, request_type: str, **fields: Any) -> Mapping[str, Any]:
        generation = self.generation + 1
        response = await self._mutation(request_type, generation=generation, **fields)
        self.generation = generation
        return response

    async def _mutation(
        self,
        request_type: str,
        *,
        generation: int | None = None,
        **fields: Any,
    ) -> Mapping[str, Any]:
        return await self._request(
            request_type,
            observer_id=self.observer_id,
            lease_id=self.lease_id,
            generation=self.generation if generation is None else generation,
            **fields,
        )

    async def _request(self, request_type: str, **fields: Any) -> Mapping[str, Any]:
        self._counter += 1
        request_id = f"r0-g{self.generation}-{request_type.lower()}-{self._counter}"
        payload = {
            "channel": CHANNEL,
            "type": request_type,
            "request_id": request_id,
            **fields,
        }
        async with self._request_lock:
            await self.websocket.send(json.dumps(payload, separators=(",", ":")))
            response = json.loads(await asyncio.wait_for(self.websocket.recv(), timeout=3))
        if response.get("type") == "ERROR":
            raise ProbeError(f"{response.get('code')}: {response.get('message')}")
        if response.get("request_id") != request_id:
            raise ProbeError("observer-control response request_id mismatch")
        return response


def _follow_settings() -> dict[str, float]:
    return {
        "distance": 5.0,
        "azimuth_deg": 180.0,
        "elevation_deg": 25.0,
        "height_offset": 1.6,
        "stiffness": 0.2,
        "fov_deg": 70.0,
        "collision_margin": 0.25,
    }


def _parse_bool(value: str) -> bool:
    normalized = value.lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ProbeError("allow_chunk_loading must be true or false")


async def _run(arguments: argparse.Namespace) -> None:
    async with websockets.connect(arguments.endpoint, max_size=32_768, proxy=None) as websocket:
        probe = ObserverControlProbe(
            websocket,
            observer_id=arguments.observer_id,
            generation=arguments.generation,
            target=arguments.target,
            heartbeat_s=arguments.heartbeat_s,
        )
        if arguments.status_only:
            await probe.status()
            return
        await probe.start()
        heartbeat = asyncio.create_task(probe.heartbeat_loop())
        try:
            await probe.command_loop()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await probe.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="ws://127.0.0.1:8766")
    parser.add_argument("--observer-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--target", default="Bot1")
    parser.add_argument("--heartbeat-s", type=float, default=2.0)
    parser.add_argument("--status-only", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(_run(arguments))
    except (OSError, ProbeError, TimeoutError) as error:
        print(f"PROBE failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
