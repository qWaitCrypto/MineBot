import asyncio

import httpx

from minebot.app.decision_client import DecisionAnswer, DecisionClient, DecisionPriority


class FakeClient:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, url, json):
        self.calls.append(json)
        self.started.set()
        await self.release.wait()
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"answers": {"q": {"choice": "satisfied"}}})

    async def aclose(self):
        return None


def test_single_inflight_and_high_priority_stales_lower_answer():
    async def scenario():
        fake = FakeClient()
        client = DecisionClient(client=fake)
        low = asyncio.create_task(client.ask({"id": "low"}, priority=DecisionPriority.PLAN))
        await fake.started.wait()
        high = asyncio.create_task(client.ask({"id": "high"}, priority=DecisionPriority.SUPERVISION))
        await asyncio.sleep(0)
        fake.release.set()
        low_answer, high_answer = await asyncio.gather(low, high)
        await client.close()
        return low_answer, high_answer, fake.calls

    low, high, calls = asyncio.run(scenario())
    assert low.stale and low.choice == "unknown"
    assert high.choice == "satisfied"
    assert [call["id"] for call in calls] == ["low", "high"]


def test_client_transport_failure_is_unknown():
    class Broken:
        async def post(self, url, json):
            raise httpx.ConnectError("offline")
        async def aclose(self):
            return None

    async def scenario():
        client = DecisionClient(client=Broken())
        result = await client.ask({"id": "x"})
        await client.close()
        return result

    answer = asyncio.run(scenario())
    assert answer.choice == "unknown"


def test_worker_shutdown_does_not_wait_again_when_transport_swallows_cancellation():
    async def run():
        client = DecisionClient()
        started = asyncio.Event()

        async def interrupted_post(request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A transport cleanup can return a last unknown response.
                return DecisionAnswer('unknown', None, 0, request.sequence)

        client._post = interrupted_post
        caller = asyncio.create_task(client.ask({'state': {}, 'questions': {}}))
        await started.wait()
        worker = client._worker
        worker.cancel()
        try:
            done, pending = await asyncio.wait({worker}, timeout=.1)
            assert not pending, 'cancelled worker waited on an empty queue again'
            assert worker.cancelled()
            assert (await caller).choice == 'unknown'
        finally:
            worker.cancel()
            caller.cancel()
            await asyncio.gather(worker, caller, return_exceptions=True)
            await client.close()

    asyncio.run(run())
