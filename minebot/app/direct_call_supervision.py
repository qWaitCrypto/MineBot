"""Supervise the existing direct build tool; no plan interpreter or auto repair."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter

import httpx

from minebot.app.construction_facts import read_construction
from minebot.contract.execution import tool_observation_scope

TOOL_EXPLANATION = (
    "build_structure fills the declared cuboid. Builder may navigate, switch segments, select "
    "materials from inventory and place while standing still. Every sample need not consume "
    "material. Heartbeats, a live process, repeated cancellations and Python pending counts "
    "are not progress. Only native/world evidence supports completion or a concrete blocker."
)
VOCABULARY = {
    "inventory_complete": "true: the count map was read in full; absent items count as zero",
    "inventory_includes_selected": "true: mainhand items are already counted; do not add again",
    "selected_item_known": "true with empty selected_item: observed empty hand; absent: unknown",
    "builder_paused": "Builder reports paused; this alone does not explain why",
    "path_position": "Index in current navigation path; compare only within the same path",
    "construction_progress": "Bounded native target-cell read. Matching count is world truth, not an attributable placement count.",
}
CRITERIA = {
    "continue": "Evidence shows normal ongoing work; continue the admitted call.",
    "inspect": "Evidence is missing or unclear; request a bounded read. Absence alone is not a blocker.",
    "yield": "Evidence supports handing control back; a recommendation, not a diagnosed cause.",
}


class DirectBuildSupervisor:
    def __init__(self, runtime, *, decision_url=None, suspicion_s=5.0, review_interval_s=15.0):
        self.runtime = runtime
        self.decision_url = decision_url or os.environ.get(
            "MINEBOT_DECISION_URL", "http://117.176.14.165:10004/v1/systemone")
        self.suspicion_s = suspicion_s
        self.review_interval_s = review_interval_s
        self._client = None
        self.current = None

    async def close(self):
        if self._client is not None:
            await self._client.aclose()

    async def judge(self, call_id, params, state, world, idle_s):
        request = {"state": {
            "protocol": "minebot-direct-build-supervision/1",
            "tool": {"name": "build_structure", "arguments": params, "normal_work": TOOL_EXPLANATION},
            "facts": {"bot": {"observed": bool(state), "values": {
                k: v for k, v in state.items() if k in {
                    "position", "creative", "inventory", "inventory_complete", "inventory_includes_selected",
                    "selected_slot", "selected_item", "selected_item_known", "sampled_at_ms", "navigation",
                    "builder_active", "builder_paused", "allow_inventory", "pathing", "any_process_active",
                    "active_action_id", "active_action_known", "construction_progress"}} or None},
                      "site": {"observed": world is not None, "values": {
                          k: v for k, v in world.items() if k not in {"matched_positions", "remaining_cells", "unknown_cells"}},
                          "remaining_cell_count": len(world["remaining_cells"]),
                          "remaining_block_types": dict(Counter(cell["actual"] for cell in world["remaining_cells"])),
                          "remaining_sample": world["remaining_cells"][:8],
                          "omitted_remaining_cell_count": max(0, len(world["remaining_cells"]) - 8),
                          "unknown_cell_count": len(world["unknown_cells"]),
                          "omitted_position_count": len(world["matched_positions"]) + len(world["remaining_cells"]) + len(world["unknown_cells"])}},
            "no_explainable_progress_s": round(idle_s, 2), "field_vocabulary": VOCABULARY,
            "evidence_scope": "Only sampled facts; omitted facts are unknown. A recommendation is not a physical result.",
        }, "questions": {"q": {"type": "choice",
            "instructions": "Judge the active tool using only these facts.", "criteria": CRITERIA}}}
        self.runtime.trace.emit("direct_supervision_request", tool_call_id=call_id,
                                regression_format="minebot-semantic-regression/1", request=request)
        started = time.monotonic()
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(trust_env=False, timeout=2.0)
            response = await self._client.post(self.decision_url, json=request)
            response.raise_for_status()
            raw = response.json()
            answer = raw.get("answers", {}).get("q", {}).get("choice")
            choice = answer if answer in CRITERIA else "unknown"
        except (httpx.HTTPError, ValueError, TypeError):
            raw, choice = None, "unknown"
        self.runtime.trace.emit("direct_supervision_result", tool_call_id=call_id,
                                response=raw, choice=choice, elapsed_s=time.monotonic() - started)
        return choice

    async def run(self, tool, callback, args, *, call_id, timeout_s):
        loop = asyncio.get_running_loop()
        latest = {}
        wake = asyncio.Event()
        record = {"call_id": call_id, "action_id": None, "cancel_requested": False, "settling": False}
        self.current = record
        def receive(payload):
            if self.current is not record or payload.get("call_id") != call_id:
                return
            record["action_id"] = payload.get("action_id")
            if payload.get("lite"):
                # Observer-only frames carry no inventory or site facts; keep the last full sample.
                return
            latest.clear()
            latest.update(payload)
            wake.set()
        telemetry = getattr(self.runtime, "panel_telemetry", None)
        def notify(payload):
            if telemetry is not None:
                telemetry.observe(call_id, payload)
            loop.call_soon_threadsafe(receive, dict(payload))
        def invoke():
            with tool_observation_scope(call_id, notify):
                return callback(*args)
        worker = asyncio.create_task(self.runtime.execution_lane.run(invoke, timeout_s=timeout_s))
        monitor = asyncio.create_task(self._monitor_safely(record, latest, wake, args[1], worker))
        try:
            result = await worker
            if record["settling"]:
                await monitor
            handoff = self.runtime.direct_call_handoffs.get(call_id)
            payload = result if isinstance(result, dict) else getattr(result, "result", None)
            if handoff is not None and isinstance(payload, dict):
                payload.setdefault("metrics", {})["direct_call_handoff"] = handoff
            if isinstance(payload, dict) and payload.get("metrics", {}).get("control_unknown"):
                self.runtime._body_control_unknown = True
            return result
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            self.runtime.trace.emit("direct_supervision_finished", tool_call_id=call_id,
                                    reads=record.get("reads", 0), questions=record.get("questions", 0))
            if self.current is record:
                self.current = None

    async def _monitor_safely(self, *args):
        try:
            await self._monitor(*args)
        except Exception as exc:
            self.runtime.trace.emit("direct_supervision_unknown", error_type=type(exc).__name__)

    async def _monitor(self, record, latest, wake, params, worker):
        last_progress, last_review = time.monotonic(), 0.0
        reads, questions = 0, 0
        previous = None
        last_received = None
        highwater = {}
        max_matching = None
        while not worker.done() and not record["cancel_requested"]:
            try:
                await asyncio.wait_for(wake.wait(), timeout=0.25)
                wake.clear()
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            state = dict(latest)
            if state and state.get("received_at_monotonic") != last_received:
                last_received = state.get("received_at_monotonic")
                self.runtime.trace.emit("direct_call_progress", tool_call_id=record["call_id"],
                                        action_id=record["action_id"], native_sample=state)
            if state.get("type") == "terminal":
                return
            nav = state.get("navigation") or {}
            matching = (state.get("construction_progress") or {}).get("matching_cells")
            if isinstance(matching, int):
                if max_matching is not None and matching > max_matching:
                    last_progress = now
                max_matching = max(matching, max_matching or 0)
            path = json.dumps([nav.get("src"), nav.get("dest"), nav.get("path_movements")])
            index = nav.get("path_position")
            if isinstance(index, int) and index > highwater.get(path, -1):
                if path in highwater:
                    last_progress = now
                highwater[path] = index
            if previous:
                pos, old = state.get("position"), previous.get("position")
                if nav.get("pathing") and pos and old and sum((a-b)**2 for a,b in zip(pos, old)) >= 0.25:
                    last_progress = now
            if state:
                previous = state
            if not state or now - last_progress < self.suspicion_s or now - last_review < self.review_interval_s:
                continue
            if reads >= 4:
                continue
            last_review = now
            for attempt in range(2):
                if worker.done() or record["cancel_requested"] or reads >= 4:
                    return
                if attempt == 0:
                    world = await self.runtime._read_lane.run(read_construction, self.runtime.body, params)
                else:
                    read = await self.runtime._read_lane.run(self.runtime.body.perceive, "baritoneState", {})
                    state = dict(read.data) if read.ok else {}
                reads += 1
                record["reads"] = reads
                if worker.done() or record["cancel_requested"]:
                    return
                if world["complete"] and world["matched_cells"] == world["expected_cells"]:
                    last_progress = time.monotonic()
                    break
                if attempt == 0:
                    state = dict(latest)
                item = (state.get("construction_progress") or {}).get("material_item")
                inventory = state.get("inventory")
                count = inventory.get(item, 0) if (state.get("inventory_complete") is True
                    and isinstance(inventory, dict) and isinstance(item, str) and item) else None
                if (world["complete"] and state.get("builder_paused") is True
                    and state.get("creative") is False and count == 0
                    and state.get("inventory_includes_selected") is True and world["remaining_cells"]):
                    await self._handoff(record, {
                        "reason": "material_exhausted", "confirmed": True,
                        "material": item, "available": 0, "construction": world,
                        "inventory_source": state.get("sampled_at_ms"),
                    })
                    return
                if questions >= 4:
                    break
                choice = await self.judge(record["call_id"], params, state, world, time.monotonic()-last_progress)
                questions += 1
                record["questions"] = questions
                if worker.done() or record["cancel_requested"]:
                    return
                if choice == "yield":
                    await self._handoff(record, {"reason": "supervision_yield", "confirmed": False,
                        "recommendation": "yield", "construction": world, "unknown": ["blocker_cause"]})
                    return
                if choice != "inspect":
                    break

    async def _handoff(self, record, facts):
        if self.current is not record or record["cancel_requested"]:
            return
        record["cancel_requested"] = record["settling"] = True
        call_id = record["call_id"]
        self.runtime.trace.emit("direct_call_blocked", tool_call_id=call_id, facts=facts)
        kernel = self.runtime.responses_kernel
        if kernel is not None:
            kernel.notify_fact({"call_id": call_id, "status": "handoff_requested", **facts})
        settled = await self.runtime.cancel_active_execution_with_facts("direct_call:" + facts["reason"])
        self.runtime.trace.emit("direct_call_handoff", tool_call_id=call_id, facts=facts, settlement=settled)
        self.runtime.direct_call_handoffs[call_id] = {"facts": facts, "settlement": settled}
