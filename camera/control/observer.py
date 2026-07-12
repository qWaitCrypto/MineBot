from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping
from typing import Any

from camera.control.follow import FollowConfig


CHANNEL = "observer-control"
PROTOCOL = "observer-control/1"


class ObserverControlError(RuntimeError):
    pass


class ObserverControlClient:
    """Least-privilege client for one observer-control lease."""

    def __init__(
        self,
        websocket: Any,
        *,
        observer_id: str,
        generation: int,
    ) -> None:
        self._websocket = websocket
        self._observer_id = observer_id
        self._generation = generation
        self._lease_id = secrets.token_hex(16)
        self._counter = 0
        self._request_lock = asyncio.Lock()
        self._attached = False

    @classmethod
    async def connect(
        cls,
        endpoint: str,
        *,
        observer_id: str,
        generation: int,
        target: str,
        follow: FollowConfig,
    ) -> "ObserverControlClient":
        try:
            import websockets
        except ImportError as error:  # pragma: no cover - environment preflight
            raise ObserverControlError("Camera requires the 'camera' optional dependency") from error
        websocket = await websockets.connect(endpoint, max_size=32_768, proxy=None)
        client = cls(
            websocket,
            observer_id=observer_id,
            generation=generation,
        )
        try:
            hello = await client._request("HELLO", protocol=PROTOCOL)
            if hello.get("protocol") != PROTOCOL:
                raise ObserverControlError("bridge negotiated an unexpected protocol")
            status = await client.status()
            last_generation = status.get("last_generation", 0)
            if isinstance(last_generation, bool) or not isinstance(last_generation, int) or last_generation < 0:
                raise ObserverControlError("bridge returned an invalid last_generation")
            client._generation = max(client._generation, last_generation + 1)
            await client._mutation(
                "ATTACH",
                mode="follow",
                target_name=target,
                follow=_follow_payload(follow),
            )
            client._attached = True
        except BaseException:
            await websocket.close()
            raise
        return client

    async def heartbeat(self) -> Mapping[str, Any]:
        return await self._mutation("HEARTBEAT")

    async def status(self) -> Mapping[str, Any]:
        return await self._request("STATUS")

    async def close(self) -> None:
        try:
            if self._attached:
                await self._advance("DETACH")
                self._attached = False
        finally:
            await self._websocket.close()

    async def _advance(self, request_type: str, **fields: Any) -> Mapping[str, Any]:
        generation = self._generation + 1
        response = await self._mutation(request_type, generation=generation, **fields)
        self._generation = generation
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
            observer_id=self._observer_id,
            lease_id=self._lease_id,
            generation=self._generation if generation is None else generation,
            **fields,
        )

    async def _request(self, request_type: str, **fields: Any) -> Mapping[str, Any]:
        self._counter += 1
        request_id = f"camera-g{self._generation}-{request_type.lower()}-{self._counter}"
        request = {
            "channel": CHANNEL,
            "type": request_type,
            "request_id": request_id,
            **fields,
        }
        async with self._request_lock:
            await self._websocket.send(json.dumps(request, separators=(",", ":")))
            raw_response = await asyncio.wait_for(self._websocket.recv(), timeout=3.0)
        response = json.loads(raw_response)
        if not isinstance(response, dict):
            raise ObserverControlError("observer-control returned a non-object response")
        if response.get("type") == "ERROR":
            code = str(response.get("code") or "bridge_error")
            message = str(response.get("message") or "observer-control request failed")
            raise ObserverControlError(f"{code}: {message}")
        if response.get("request_id") != request_id:
            raise ObserverControlError("observer-control response request_id mismatch")
        return response


def _follow_payload(config: FollowConfig) -> dict[str, float]:
    return {
        "distance": config.distance,
        "azimuth_deg": config.azimuth_deg,
        "elevation_deg": config.elevation_deg,
        "height_offset": config.height_offset,
        "stiffness": config.stiffness,
        "fov_deg": config.fov_deg,
        "collision_margin": config.collision_margin,
    }
