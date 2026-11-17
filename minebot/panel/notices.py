"""Operator notifications derived from the trace: what deserves a toast, merged and aged.

:class:`Notifier` watches the same records the panel model applies and turns the few
that need the operator's attention into :class:`Notice` objects. Repeats of the same
failure within :data:`MERGE_S` merge into one notice with a ×N count. The panel shows
the newest :data:`STACK` that are still young; ``N`` opens the full history.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

STACK = 3
SHOW_S = 6.0
MERGE_S = 30.0
HISTORY = 100
HEALTH_DROP = 4.0  # half a heart is noise; two hearts in one update is a fight or a fall


@dataclass
class Notice:
    ts: float
    tone: str  # ok | warn | fail | agent
    text: str
    key: str = ""
    count: int = 1
    shown_at: float = field(default_factory=time.monotonic)

    def label(self) -> str:
        return f"{self.text} ×{self.count}" if self.count > 1 else self.text


class Notifier:
    def __init__(self) -> None:
        self.history: deque[Notice] = deque(maxlen=HISTORY)
        self.unread = 0
        self._health: float | None = None

    def visible(self, now: float | None = None) -> list[Notice]:
        moment = time.monotonic() if now is None else now
        return [notice for notice in self.history if moment - notice.shown_at < SHOW_S][-STACK:]

    def read_all(self) -> None:
        self.unread = 0

    def push(self, ts: float, tone: str, text: str, key: str = "") -> None:
        if key:
            for notice in reversed(self.history):
                if notice.key == key and ts - notice.ts < MERGE_S:
                    notice.count += 1
                    notice.ts = ts
                    notice.shown_at = time.monotonic()
                    self.unread += 1
                    return
        self.history.append(Notice(ts, tone, text, key))
        self.unread += 1

    def observe(self, record: dict[str, Any]) -> None:
        event = record.get("event")
        ts = float(record.get("ts") or time.time())
        if event == "tool_result":
            full = record.get("full_result") if isinstance(record.get("full_result"), dict) else {}
            success = full.get("success", record.get("success"))
            if success is False:
                tool = str(record.get("tool") or "工具")
                reason = str(full.get("reason") or record.get("reason") or "")
                self.push(ts, "fail", f"✗ {tool}" + (f"：{reason[:40]}" if reason else ""), f"tool:{tool}:{reason}")
        elif event == "body_state":
            health = record.get("health")
            if isinstance(health, (int, float)):
                if self._health is not None and self._health - health >= HEALTH_DROP:
                    self.push(ts, "fail", f"♥ 掉血 {self._health:.0f} → {health:.0f}", "health")
                self._health = float(health)
        elif event == "world_edit" and record.get("target") is False:
            self.push(ts, "warn", "⚠ 改动了目标范围外的方块", "edit_outside")
        elif event == "responses_failed" or (event == "responses_terminal" and record.get("status") not in (None, "completed")):
            reason = str(record.get("message") or record.get("reason") or record.get("error_code") or "请求失败")
            self.push(ts, "fail", f"模型请求失败：{reason[:40]}", "request_failed")
        elif event == "session_terminal":
            status = str(record.get("status") or "")
            self.push(ts, "fail" if status == "failed" else "ok", f"会话结束：{status or '完成'}")
        elif event == "task_outcome":
            status = str(record.get("status") or "")
            summary = str(record.get("summary") or "")[:50]
            self.push(ts, "ok" if status in ("completed", "succeeded", "success") else "warn",
                      f"任务{('完成' if status in ('completed', 'succeeded', 'success') else '：' + status)}"
                      + (f" · {summary}" if summary else ""))


__all__ = ["HISTORY", "MERGE_S", "SHOW_S", "STACK", "Notice", "Notifier"]
