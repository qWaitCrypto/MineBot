# Decision wire replay (W1c)

Run these tools from the repository root using `.venv/bin/python`.
They use the standard library and the existing production `DecisionClient`/`httpx`
path. They never load credentials or provider configuration.

## Harvest

```bash
.venv/bin/python tools/decision_replay/harvest.py \
  logs/eval-20261019/track-s/session/trace.jsonl \
  --seed 0 --idle-stay-sample 600 --out w1-out/set-track-s.jsonl
.venv/bin/python tools/decision_replay/harvest.py \
  logs/eval-20261019/track-p/session/trace.jsonl \
  --seed 0 --idle-stay-sample 600 --out w1-out/set-track-p.jsonl
```

Multiple trace paths can also be supplied to make a combined set. Sampling resets
the seeded generator per trace run, so the same trace gets the same sample when
harvested alone or with other traces. Output order follows input traces, then wire
sequence. Headers contain trace hashes, selection rules, counts and exclusions;
they contain no creation timestamp, so repeated harvests are byte reproducible.
Trace lines are streamed, substring filtered, then decoded.

Each request record contains `source` (run, trace, wire seq, ts, session and
request_id), `purpose`, ordered `payload`, full `recorded` response including all
question choices and probabilities, wire round-trip `decision_ms`, and
`order_source` provenance. A separate `done_request` becomes a separate record
under the originating frame purpose; its question key is `a`.

Only complete, valid model answers qualify. No-response requests and supervisor
short circuits that never reached the wire do not have model answers to replay.
All recoverable non-idle requests are retained. Idle selection uses the Body's
danger-gated action (`p` when `d=in_danger`, otherwise `q`): all non-`stay` actions
are retained, and 600 `stay` requests per run are sampled by default. Agreement
still measures every question independently, even `p` when `d=safe`.

## Why this preserves wire order

* `minebot/app/decision_client.py:165-173` creates an `httpx.AsyncClient` with
  `trust_env=False` and posts `json=request.payload`. No model, temperature,
  effort, query parameters, or authorization is added. The live timeout is 2 s
  (`decision_client.py:60`). Replay calls that same client code.
* `minebot/app/observability.py:74-81` sorts all events except
  `call_supervision_request`. Thus `decision_request.payload` loses recursive key
  order, while `call_supervision_request.request` and `.done_request` retain it.
  `runtime_trace.py:44-61` and `observability.py:92-105` sanitize maps by iteration,
  preserving insertion order before serialization.
* Idle requests start with `state`, then `questions`; state starts with `body`,
  optional `agent_intent`, daylight/player surroundings, `threats`, `inventory`,
  `main_hand`, `position` (`reflex_question.py:931-949`). Ordinary question order
  is `q`, then optional `d`, `p`. Each question has `type`, `instructions`, then
  `criteria`, whose original option order is retained
  (`reflex_question.py:679-699`). Different frame types can have different state
  fields/orders: harvesting copies the entire preserved object recursively,
  rather than applying a fixed state/option ordering list.

Responses join **only by `(trace, session_id, request_id)`**. Equal full payload
content associates a wire request with an earlier preserved copy for its order
and purpose. Candidates with conflicting orders/purposes are excluded with an
explicit reason; timestamps never pair anything. The chosen source seq documents
an equivalent preserved copy, not an inferred supervision-result pairing.
String-only plan-condition requests have another verifiable source: the fixed
constructor at `work_runtime.py:1209-1234`, using prose fact order from
`reflex_question.py:899-913`. Their state order is `condition`, optional `about`,
Body prose facts, then `evidence_scope`; option order is `satisfied`, `pending`,
`unknown`. A strict schema check allows this reconstruction only when no nested
maps or unfamiliar fields are present, and labels the purpose `work_condition`.
The order provenance is marked `work_condition_constructor`.
Body-level final choices are not labels: they can combine normal, danger,
protective and subsequent intent answers
(`direct_call_supervision.py:323-334`).

Older all-sorted traces, redacted payloads, ambiguous sources and requests with no
preserved copy cannot establish exact wire order. These answered requests are
excluded and listed individually in the header; no guessed payload is emitted.

## Live baseline and resume

The code-derived endpoint is `$MINEBOT_DECISION_URL`
(`decision_client.py:65`). Neither track's `launch.sh` nor `session/launch.json`
records a Decision endpoint override. Historical inherited/environment-file
overrides are not captured in those launch files and were not inspected.
Both traces identify the answering model as `h2oai/h2o-lightning-4b`; that is a
response field, **not** a model parameter sent by the live client.

```bash
cd <repo root>
for track in s p; do
  .venv/bin/python tools/decision_replay/replay.py \
    "w1-out/set-track-$track.jsonl" \
    --endpoint $MINEBOT_DECISION_URL \
    --concurrency 2 --timeout 2 \
    --out "w1-out/baseline-track-$track.jsonl"
  .venv/bin/python tools/decision_replay/compare.py \
    "w1-out/baseline-track-$track.jsonl" > "w1-out/baseline-track-$track.md"
done
```

Use the same command to resume. Successful records are skipped; failures are
retried, and an interrupted final append is repaired. A changed set, endpoint,
model or timeout requires a new output file. `--model NAME` explicitly adds or
overrides a top-level model field for replacement-model experiments; omit it for
the baseline. Concurrency can change when resuming.

`compare.py` counts each record once across retries, includes failed answers as
disagreements, and reports coverage, agreement per purpose, per question and their
intersection, question-labelled confusion counts per purpose, and the 20 largest
absolute option-probability changes with state lines. Missing probabilities are
reported rather than treated as zero. Require full coverage, no transport
failures, and >=99% agreement per purpose before trusting a live baseline.

## Harvested data and offline validation

`w1-out/` is locally ignored and is not part of git. Seed 0, 600 idle stays per run:

| Purpose | Track S requests | Track P requests |
| --- | ---: | ---: |
| idle_frame | 945 | 616 |
| running_frame | 4,717 | 6,135 |
| default_result_frame | 395 | 122 |
| plan_result_frame | 180 | 140 |
| result_frame | 53 | 162 |
| return_place_frame | 2 | 0 |
| work_condition | 64 | 0 |
| Total | 6,356 | 7,175 |

The running counts include 2,249 standalone intent requests in S and 3,000 in P.
Idle includes 345 non-stay actions in S and 16 in P, plus the sampled stays.
S has five answered requests without recoverable wire order, from work-plan
branching with nested fact maps; these are audited exclusions. Another 64 plan
condition requests use the fixed constructor reconstruction above. S/P also have
84/94 wire requests with no `decision_response` event. No answered reflex request
was excluded for order recovery. The header audit reconciles the complete wire
request counts.

The full harvested sets were replayed through an injected `httpx.MockTransport`
returning recorded responses, using the same live client serialization path:
**100% agreement for every purpose and every question in both runs**, with full
coverage and zero failures. Reports are in `w1-out/offline-stub-track-{s,p}.md`.
These are transport/format checks; the live model baseline remains unmeasured
because the sandbox cannot access the endpoint.

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_replay_tools.py
```

The synthetic tests verify recursive order recovery, exact pairing with identical
timestamps and reused supervision identifiers, separate intent requests,
fixed plan-condition reconstruction, unrecoverable-source auditing, reproducible per-run sampling, bounded offline
replay with 100% agreement, retry/resume, configuration guards, explicit model
overrides, and comparison output. No sockets or external service are needed.
