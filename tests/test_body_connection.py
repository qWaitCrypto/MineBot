import asyncio
import json

import websockets


async def test():
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        await ws.send(json.dumps({"type": "ping"}))
        print(await ws.recv())

        await ws.send(json.dumps({"type": "getState"}))
        print(await ws.recv())

        await ws.send(json.dumps({
            "type": "action",
            "name": "lookAt",
            "params": {"x": 100, "y": 64, "z": 100},
        }))
        print(await ws.recv())

        await ws.send(json.dumps({"type": "action", "name": "probe", "params": {}}))
        print(await ws.recv())


if __name__ == "__main__":
    asyncio.run(test())
