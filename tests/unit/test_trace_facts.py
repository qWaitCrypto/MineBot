import asyncio
import copy
import hashlib
import json

import pytest
import httpx

from minebot.app.decision_client import DecisionClient
from minebot.app.observability import JsonlObservationSink, sanitize_observation
from minebot.app.runtime_trace import RuntimeTrace
from minebot.app.trace_facts import TraceFactsReader, TraceFactsWriter, encode_line, read_trace
from minebot.panel.source import TraceFollower, load_records
from tools.harvest_decision_corpus import _iter_events
from tools.rewrite_trace import rewrite


def frames():
    slots = [{"slot": index, "item": "oak_log", "count": 64} for index in range(36)]
    for index in (0, 0, 1, 0):
        facts = {"inventory_slots": slots, "pos": [index, 64, 0],
                 "inventory": {"z_item": 3, "a_item": 1},
                 "literal": {"facts_refs": [], "$facts_ref": "a" * 600}}
        yield {"event": "call_supervision_request", "seq": index,
               "request": {"state": {"position": index}, "questions": {"q": "continue?"}},
               "frame_input": {"execution": {"facts": facts, "result": {"state": facts}},
                               "ambient_facts": facts},
               "compiler_input": {"facts": facts, "decision_context": {"body_state": facts}}}


@pytest.mark.parametrize("background", [False, True])
def test_persisted_requests_reference_earlier_facts_and_restore_exactly(tmp_path, background):
    path = tmp_path / "trace.jsonl"
    trace = RuntimeTrace(sink=JsonlObservationSink(path, background=background))
    originals = []
    for record in frames():
        originals.append(copy.deepcopy(record))
        trace.emit(record["event"], **{k: v for k, v in record.items() if k != "event"})
    trace.close()
    assert list(frames()) == originals
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    known = set()
    definitions = 0
    requests = []
    reader = TraceFactsReader()
    for row in rows:
        if row.get("reset"):
            known.clear()
        if row["event"] == "frame_facts":
            if "base_id" in row:
                assert row["base_id"] in known
            assert row["facts_id"] not in known
            known.add(row["facts_id"])
            definitions += 1
        else:
            assert row["facts_id"] in known
            assert row["frame_input"] is None
            requests.append((definitions, row["facts_id"]))
        reader.decode(row)
    assert requests[0] == requests[1]
    assert requests[2][1] != requests[1][1]
    assert requests[3][1] == requests[0][1]
    restored = list(read_trace(path.read_text().splitlines()))
    assert restored == trace.events
    assert json.dumps(restored) == json.dumps(trace.events)
    assert [row["request"] for row in restored] == [row["request"] for row in originals]
    assert list(_iter_events(path)) == restored
    assert load_records(path) == restored


def test_changed_facts_still_compress_the_repeated_inputs():
    writer = TraceFactsWriter()
    first, _, changed, _ = frames()
    initial = writer.encode(first)
    later = writer.encode(changed)
    assert initial[0]["facts_id"] != later[0]["facts_id"]
    assert "facts_zlib" in later[0]
    assert later[0]["base_id"] == initial[0]["facts_id"]
    assert sum(map(lambda row: len(encode_line(row)), later)) < len(encode_line(changed)) / 3


def test_ordered_ids_and_redaction_precede_storage(tmp_path):
    path = tmp_path / "trace.jsonl"
    facts = {"z": "橡木" * 300, "a": "x" * 600, "api_key": "private-value"}
    reordered = {key: facts[key] for key in reversed(facts)}
    sink = JsonlObservationSink(path)
    sink.write({"event": "reflex_death", "facts": facts})
    sink.write({"event": "reflex_death", "facts": reordered})
    sink.close()
    raw = path.read_text()
    assert "private-value" not in raw
    restored = list(read_trace(raw.splitlines()))
    assert restored == [sanitize_observation({"event": "reflex_death", "facts": value})
                        for value in (facts, reordered)]
    ids = [row["facts_id"] for row in map(json.loads, raw.splitlines()) if row["event"] == "reflex_death"]
    assert ids[0] != ids[1]
    expected_id = hashlib.sha256(json.dumps({"facts": sanitize_observation(facts)}, ensure_ascii=True,
                                           separators=(",", ":")).encode()).hexdigest()
    assert ids[0] == expected_id


def test_blocks_and_appended_writers_are_independently_reconstructable():
    writer = TraceFactsWriter(block_bytes=1)
    records = list(frames())
    rows = [row for record in records for row in writer.encode(record)]
    rows.extend(TraceFactsWriter().encode(records[0]))
    assert sum(bool(row.get("reset")) for row in rows) == 5
    assert list(read_trace(map(encode_line, rows))) == [*records, records[0]]


def test_missing_facts_fail_explicitly_and_old_lines_still_work():
    record = list(frames())[0]
    assert list(read_trace(["broken", "[]", "", encode_line(record)])) == [record]
    compact = TraceFactsWriter().encode(record)[-1]
    with pytest.raises(ValueError, match="read the trace from its start"):
        TraceFactsReader().decode(compact)


def test_follower_handles_partial_lines_and_resets_facts_on_replacement(tmp_path):
    path = tmp_path / "trace.jsonl"
    original = list(frames())[0]
    data = "".join(map(encode_line, TraceFactsWriter().encode(original))).encode()
    path.write_bytes(data[:-5])
    follower = TraceFollower(path)
    assert follower.read()[0] == []
    with path.open("ab") as handle:
        handle.write(data[-5:])
    assert follower.read() == ([original], False)
    replacement = tmp_path / "new.jsonl"
    replacement.write_bytes(data)
    replacement.replace(path)
    assert follower.read() == ([original], True)


def test_rewrite_and_legacy_export_round_trip(tmp_path):
    original, compact, expanded = (tmp_path / name for name in ("old.jsonl", "new.jsonl", "expanded.jsonl"))
    records = list(frames())
    original.write_text("".join(map(encode_line, records)))
    report = rewrite(original, compact)
    assert report["round_tripped_events"] == 4
    assert report["after_mb"] < report["before_mb"]
    rewrite(compact, expanded, expand=True)
    assert [json.loads(line) for line in expanded.read_text().splitlines()] == records


def test_decision_http_payload_and_in_memory_events_are_unchanged(tmp_path):
    path = tmp_path / "trace.jsonl"
    record = list(frames())[0]
    expected = copy.deepcopy(record["request"])
    posted = []

    def handle(request):
        posted.append(request.content.decode())
        return httpx.Response(200, json={"answers": {"q": {"choice": "continue"}}})

    async def run():
        trace = RuntimeTrace(sink=JsonlObservationSink(path, background=True))
        trace.emit(record["event"], **{k: v for k, v in record.items() if k != "event"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
            decision = DecisionClient(url="http://decision.test/", client=transport, emit=trace.emit)
            try:
                assert (await decision.ask(record["request"])).choice == "continue"
            finally:
                await decision.close()
        trace.close()
        assert list(read_trace(path.read_text().splitlines())) == trace.events
        assert trace.events[0]["frame_input"] == record["frame_input"]

    asyncio.run(run())
    assert record["request"] == expected
    assert posted == [json.dumps(expected, ensure_ascii=False, separators=(",", ":"))]


def test_uncompressed_fallback_and_decoded_events_do_not_share_mutable_facts():
    import random
    import string

    randomizer = random.Random(1)
    text = "".join(randomizer.choices(string.ascii_letters + string.digits, k=800))
    original = {"event": "reflex_death", "facts": {"text": text, "small": [1]}}
    rows = TraceFactsWriter().encode(original)
    assert "facts" in rows[0]
    reader = TraceFactsReader()
    assert reader.decode(rows[0]) is None
    restored = reader.decode(rows[1])
    restored["facts"]["small"].append(2)
    assert reader.decode(rows[1]) == original
