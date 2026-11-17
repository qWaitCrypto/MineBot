import asyncio
import time
import unittest

from minebot.app.loop_watchdog import LoopWatchdog


class Trace:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append({"event": event, **fields})


def hold_the_loop(seconds):
    time.sleep(seconds)


class LoopWatchdogTests(unittest.TestCase):
    def test_a_blocked_loop_is_reported_with_the_blocking_stack(self):
        trace = Trace()

        async def scenario():
            watchdog = LoopWatchdog(trace, threshold_s=0.2, interval_s=0.05, sample_at_s=(0.2, 0.5))
            watchdog.start()
            await asyncio.sleep(0.2)
            hold_the_loop(0.7)
            await asyncio.sleep(0.2)
            await watchdog.stop()

        asyncio.run(scenario())

        stalls = [event for event in trace.events if event["event"] == "event_loop_stalled"]
        self.assertEqual(len(stalls), 1)
        self.assertGreaterEqual(stalls[0]["stalled_s"], 0.6)
        self.assertEqual(len(stalls[0]["samples"]), 2)
        self.assertTrue(any("hold_the_loop" in line for line in stalls[0]["samples"][0]["stack"]))

    def test_a_running_loop_reports_nothing(self):
        trace = Trace()

        async def scenario():
            watchdog = LoopWatchdog(trace, threshold_s=0.2, interval_s=0.05)
            watchdog.start()
            for _ in range(10):
                await asyncio.sleep(0.05)
            await watchdog.stop()

        asyncio.run(scenario())

        self.assertEqual(trace.events, [])


if __name__ == "__main__":
    unittest.main()
