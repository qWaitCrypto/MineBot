# From-zero evaluations

Use Python 3.13. These commands use only the standard library and never import
the Agent or a model client. Run from the repository root (or use script paths).

Before the series, draw random seeds and keep the registry with the results:

```sh
python -m tools.eval.seeds register --n 3 --out seeds.json
python -m tools.eval.seeds assign seeds.json --seed 123 --run logs/run-1
python -m tools.eval.seeds discard seeds.json --run logs/run-1 --reason 'server failed before spawn'
```

Replace `123` with a registered seed. The registry digest is SHA256 of the
ordered JSON integer list with compact separators and UTF-8 encoding. A discard
retains the assignment; it does not make a seed available again. Registration
refuses to overwrite. Assignment/discard updates hold an advisory file lock.

For every assigned run create a fresh world and bot with empty state DB and
life directory. Record the initial server stats in `RUN_DIR/stats-before.json`
(the player's normal Minecraft stats JSON) if a measured kill delta is wanted.
Give no help; log any intervention, keep every run, and explain every discard.
The pass criterion is an increase of at least one in the selected player's
`stats["minecraft:killed"]["minecraft:ender_dragon"]`. Without initial stats,
reports explicitly assume zero under the from-zero protocol. Tooling does not
certify freshness or preregistration retrospectively.

Start a sampler, then make reports after the world is saved:

```sh
python -m tools.eval.sampler EvalBot logs/run-1/samples.jsonl --server-dir /path/to/server
python -m tools.eval.report logs/run-1 --server-dir /path/to/server --world eval-world --bot EvalBot
python -m tools.eval.failures logs/run-1
```

The sampler reads RCON credentials from `server.properties` and emits only
exception class names on failure. It preserves the historical JSONL schema;
it parses inventory entries at the top level, so nested components do not hide
tools or inject contained items into the inventory count.

Optional `RUN_DIR/interventions.jsonl` has one object per line:

```json
{"ts":"2026-10-19T14:49:00+08:00","kind":"difficulty_change","note":"operator changed peaceful to easy"}
```

`ts` accepts Unix seconds or an ISO8601 timestamp with offset; `kind` and `note`
are strings. Absence means unrecorded, not zero interventions.

Both reporting commands accept `--out-dir DEST`, keeping original inputs
read-only. `report.py` writes `report.json` and `report.md`; `failures.py` streams
`failures.jsonl` and adds/replaces its marked summary in `report.md`. Run the
report command first, then failures. Neither command writes to the server.
A separate `--timezone` option on report defaults to Asia/Taipei for these runs.
Server log dates come from rotated filenames or latest.log's modification date,
with midnight rollover handling. Advancements use their embedded time offsets;
the completed time is the last required criterion, not the first.

Reports include Decision requests/choices by purpose, aggregate decision_request,
unknown answers and latency, and Agent outer-turn wake counts/reasons. An SDK
turn can contain many model requests. Completed llm_end counts reproduce the
historical eval-final.py figures; started/cancelled requests and summary-model
usage are shown separately. Token cache hit ratio is weighted by input tokens.
See [wakes.py](wakes.py) for all legacy inference rules. Raw queued events alone
do not prove a wake. Unmatched starts stay unknown. Native traces with no SDK
agent_start hooks use the new agent_wake events as their outer-turn boundaries.

Production emits agent_wake at the SDK runner or native kernel invocation,
after preparation has admitted a model turn. AgentSession supplies the intent
(including recovery confirmation); native reflex-fact successors supply their
fact metadata when bypassing that session. It adds reason and compact detail without changing
queueing or model inputs. Habit/life/heartbeat fact kinds are preserved; older
reflex_fact_wake records lack kind, so these are only reflex_result when their
specific origin is unavailable. A model's next request after a tool is not an
additional outer wake.

Failure records have run, ts, seq, layer, kind, tool, reason, context (at most 300
characters), and trace_seqs. Decision records retain purpose and question_options
outside the shortened context, plus request_seq. Native terminals retain their
body_seq and action_id separately from the containing trace seq. A terminal
failure and the tool failure that wraps it are different observations and have
different kinds; do not sum them as independent incidents. Protection followed
by death within 60 seconds is temporal evidence, not a claim of causation.
A plan rewrite records changed ordered step titles in a successful update_plan,
or an explicit replacement of a queued plan; status/evidence-only updates are
not rewrites. Adaptation is not necessarily an error.

Two kinds are omitted: abandonment not explicitly represented in the trace,
and repeated same-args failures after unchanged Body. args_hash establishes
argument identity only; sparse body samples cannot prove unchanged physical
state/world between calls. The corpus never substitutes repeated args alone
for that condition.

Busy time ports busy.py's six priority categories using exact interval
boundaries instead of one-second buckets. It closes cancelled turns and clips
open work/tools/plans to trace end. Busy/idle is the explicit reflex work state
and its complement, not inferred physical movement. Quantiles keep the old
sorted-index convention (upper median, index floor(0.9*n)). Trace MB is decimal.
