"""Agent turn wake attribution, with explicit evidence rather than proximity.

Count outer turns, not llm_start (SDK tool continuations stay in the same turn).
For SDK traces the boundary is agent_start. An agent_wake describes that start
when available; for native Responses there is no SDK hook, so agent_wake itself
is the boundary. Legacy rules, consumed once at the next agent_start:

* user_message with start/message/replace_goal/continue/pause -> user_message;
* reflex_fact_wake status handoff_requested -> decision_handback; completed,
  needs_agent, blocked, cancelled etc. -> reflex_result (its original fact kind
  is absent from old wake records, so do not relabel it as life/habit/plan);
* body_event_wake -> death, respawned, world_ready, world_disconnected or
  body_event, using its explicit name;
* runtime_intent_wake -> its kind (recovery_reconcile, task_continue,
  task_boundary, completion_review), only when its decision is resume;
* reflection_maintenance_started -> maintenance;
* heartbeat_queued plus a matching reflex_fact_wake -> heartbeat;
* turn_decision_context supplies intent_kind to select a compatible wake cue.
  A task_continue/task_boundary context itself establishes self-continuation.

Queued facts, reflex_death and direct_call_handoff alone do NOT prove a wake:
these often arrive during a running turn or are superseded. They never create
an attributed start. Missing/ambiguous compatible cues give unknown. In
particular, old reflex_result cannot distinguish life or habit timers; future
agent_wake carries fact.kind/status/source explicitly. Never count the SDK's
next llm_start inside an existing turn as a separate wake.
"""
from __future__ import annotations

from collections import Counter

WAKE_EVENTS = {"agent_start", "agent_wake", "turn_decision_context", "user_message",
               "reflex_fact_wake", "body_event_wake", "runtime_intent_wake",
               "reflection_maintenance_started", "heartbeat_queued"}


class Wakes:
    def __init__(self, native: bool = False):
        self.native = native
        self.cues = {}
        self.intent = None
        self.explicit = None
        self.rows = []
        self.explicit_rows = []
        self.sdk_starts = 0
        self.context = {}
        self.heartbeat = False

    def consume(self, row: dict) -> None:
        event = row["event"]
        if event == "agent_wake":
            self.explicit = row
            self.explicit_rows.append({"ts": row["ts"], "seq": row.get("seq"),
                                       "reason": row.get("reason") or "unknown", "detail": row.get("detail", {}),
                                       "source": "agent_wake", "evidence_seq": row.get("seq")})
        elif event == "turn_decision_context":
            self.intent = row.get("intent_kind")
            self.context = row
        elif event == "user_message" and row.get("command") in {"start", "message", "replace_goal", "continue", "pause"}:
            self.cues[row["command"]] = ("user_message", row)
        elif event == "heartbeat_queued":
            self.heartbeat = True
        elif event == "reflex_fact_wake":
            reason = "decision_handback" if row.get("status") == "handoff_requested" else "reflex_result"
            if self.heartbeat:
                # Old wake rows omit fact.kind; activity identity establishes
                # a heartbeat, not just an earlier queued timer.
                aid = row.get("activity_id") or ""
                if aid.startswith("heartbeat-"):
                    reason = "heartbeat"
            self.cues["reflex_fact"] = (reason, row)
        elif event == "body_event_wake":
            name = row.get("name")
            reason = name if name in {"death", "respawned", "world_disconnected", "world_ready"} else "body_event"
            self.cues["body_event"] = (reason, row)
        elif event == "runtime_intent_wake" and row.get("decision") == "resume":
            self.cues[row.get("kind")] = (row.get("kind"), row)
        elif event == "reflection_maintenance_started":
            self.cues["maintenance"] = ("maintenance", row)
        elif event == "agent_start":
            self.sdk_starts += 1
            self.start(row)

    def start(self, row: dict) -> None:
        if self.explicit is not None:
            reason = self.explicit.get("reason") or "unknown"
            evidence = self.explicit
            detail = evidence.get("detail", {})
            source = "agent_wake"
        else:
            matched = self.cues.get(self.intent)
            if matched is None and self.intent in {"task_continue", "task_boundary"}:
                reason, evidence = self.intent, self.context
            elif matched is not None:
                reason, evidence = matched
            else:
                reason, evidence = "unknown", {}
            detail = {k: evidence[k] for k in ("command", "status", "name", "kind", "activity_id", "also_delivered") if k in evidence}
            source = "legacy_inference" if reason != "unknown" else "unknown"
        self.rows.append({"ts": row["ts"], "seq": row.get("seq"), "reason": reason,
                          "detail": detail, "source": source, "evidence_seq": evidence.get("seq")})
        self.cues.clear()
        self.explicit, self.intent, self.heartbeat = None, None, False

    def finish(self) -> dict:
        rows = self.rows if self.sdk_starts else self.explicit_rows
        return {"count": len(rows), "by_reason": dict(Counter(r["reason"] for r in rows)), "turns": rows}
