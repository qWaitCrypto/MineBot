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
    assert low.stale and low.choice == "unknown" and low.raw is None
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


def test_outage_is_traced_then_told_to_the_agent_once_and_its_end_too(monkeypatch):
    from minebot.app import decision_client as module

    class Flaky:
        def __init__(self):
            self.down = True

        async def post(self, url, json):
            if self.down:
                raise httpx.ConnectError("offline")
            return httpx.Response(200, request=httpx.Request("POST", url),
                                  json={"answers": {"q": {"choice": "stay"}}})

        async def aclose(self):
            return None

    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    events, told = [], []

    async def scenario():
        fake = Flaky()
        client = DecisionClient(client=fake, emit=lambda name, **fields: events.append((name, fields)),
                                on_link=lambda state, facts: told.append((state, facts)))
        for _ in range(module.DOWN_AFTER_FAILURES):
            await client.ask({"id": "x"})
            clock[0] += 1.0
        links = [fields for name, fields in events if name == "decision_link"]
        assert [link["state"] for link in links] == ["unavailable"] and told == []
        clock[0] += module.TELL_AGENT_AFTER_S
        await client.ask({"id": "x"})
        await client.ask({"id": "x"})
        assert [state for state, _ in told] == ["unavailable"]
        fake.down = False
        answer = await client.ask({"id": "x"})
        await client.close()
        return answer

    answer = asyncio.run(scenario())
    assert answer.choice == "stay"
    assert [fields["state"] for name, fields in events if name == "decision_link"] == ["unavailable", "available"]
    assert [state for state, _ in told] == ["unavailable", "available"]
    assert told[1][1]["unanswered"] == module.DOWN_AFTER_FAILURES + 2


def test_short_outage_is_traced_but_not_told_to_the_agent():
    from minebot.app import decision_client as module

    class Flaky:
        def __init__(self):
            self.calls = 0

        async def post(self, url, json):
            self.calls += 1
            if self.calls <= module.DOWN_AFTER_FAILURES:
                raise httpx.ConnectError("offline")
            return httpx.Response(200, request=httpx.Request("POST", url),
                                  json={"answers": {"q": {"choice": "stay"}}})

        async def aclose(self):
            return None

    events, told = [], []

    async def scenario():
        client = DecisionClient(client=Flaky(), emit=lambda name, **fields: events.append((name, fields)),
                                on_link=lambda state, facts: told.append(state))
        for _ in range(module.DOWN_AFTER_FAILURES + 1):
            await client.ask({"id": "x"})
        await client.close()

    asyncio.run(scenario())
    assert [fields["state"] for name, fields in events if name == "decision_link"] == ["unavailable", "available"]
    assert told == []


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


def test_a_request_its_caller_stopped_waiting_for_is_not_sent():
    async def scenario():
        fake = FakeClient()
        client = DecisionClient(client=fake)
        first = asyncio.create_task(client.ask({"id": "first"}))
        await fake.started.wait()
        dropped = asyncio.create_task(client.ask({"id": "dropped"}))
        kept = asyncio.create_task(client.ask({"id": "kept"}))
        await asyncio.sleep(0)
        dropped.cancel()
        await asyncio.gather(dropped, return_exceptions=True)
        fake.release.set()
        await asyncio.gather(first, kept)
        await client.close()
        return fake.calls

    assert [call["id"] for call in asyncio.run(scenario())] == ["first", "kept"]
