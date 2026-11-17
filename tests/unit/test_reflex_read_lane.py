"""Agent observation does not queue behind a reflex Body action."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from minebot.app.body_control import BodyControl
from minebot.app.execution_lane import SerialExecutionLane
from minebot.app.runner import AgentRuntime


@pytest.mark.parametrize('holder', ['life', 'threat'])
def test_agent_read_completes_while_reflex_writer_is_still_running(holder):
    async def run():
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.native_responses = False
        runtime.background_call = None
        runtime.body_control = BodyControl()
        runtime.body_control.claim('reflex-test', holder=holder)
        runtime.execution_lane = SerialExecutionLane(thread_name='test-reflex-writer')
        runtime._read_lane = SerialExecutionLane(thread_name='test-agent-reader')
        runtime.trace = SimpleNamespace(emit=lambda *args, **kwargs: None)
        started, release = threading.Event(), threading.Event()

        def write():
            started.set()
            release.wait(2)

        worker = asyncio.create_task(runtime.execution_lane.run(write))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            observed = await asyncio.wait_for(runtime.run_read(lambda: 'native facts'), .3)
            assert observed == 'native facts'
            assert not worker.done()
            assert runtime.body_control.snapshot().holder == holder
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
            runtime.execution_lane.close()
            runtime._read_lane.close()
    asyncio.run(run())
