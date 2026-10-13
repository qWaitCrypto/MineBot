"""One bounded Decision request channel shared by supervision and plans."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable

import httpx


class DecisionPriority(IntEnum):
    SUPERVISION = 100
    THREAT = 90
    PLAN = 50
    LIFE = 20


@dataclass
class DecisionAnswer:
    choice: str
    raw: dict[str, Any] | None
    elapsed_s: float
    request_id: int
    stale: bool = False


@dataclass(order=True)
class _PendingDecision:
    sort_key: tuple[int, int] = field(init=False)
    priority: int
    sequence: int
    payload: dict[str, Any] = field(compare=False)
    future: asyncio.Future[DecisionAnswer] = field(compare=False)
    epoch: int = field(compare=False)

    def __post_init__(self) -> None:
        self.sort_key = (-int(self.priority), self.sequence)


class DecisionClient:
    """Serialize semantic requests and keep stale results from taking effect."""

    def __init__(
        self,
        *,
        url: str | None = None,
        timeout_s: float = 2.0,
        emit: Callable[..., None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url or os.environ.get("MINEBOT_DECISION_URL", "http://117.176.14.165:10004/v1/systemone")
        self.timeout_s = timeout_s
        self.emit = emit or (lambda *_args, **_kwargs: None)
        self._client = client
        self._own_client = client is None
        self._condition = asyncio.Condition()
        self._pending: list[_PendingDecision] = []
        self._inflight: _PendingDecision | None = None
        self._worker: asyncio.Task[None] | None = None
        self._sequence = 0
        self._epoch = 0
        self._closed = False

    async def ask(
        self,
        payload: dict[str, Any],
        *,
        priority: DecisionPriority = DecisionPriority.PLAN,
    ) -> DecisionAnswer:
        if self._closed:
            return DecisionAnswer("unknown", None, 0.0, -1)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[DecisionAnswer] = loop.create_future()
        async with self._condition:
            self._sequence += 1
            if self._inflight is not None and int(priority) > self._inflight.priority:
                self._epoch += 1
            # A higher-priority question makes queued lower-priority questions
            # obsolete; callers receive unknown and may recompile later.
            retained: list[_PendingDecision] = []
            for queued in self._pending:
                if queued.priority < int(priority):
                    if not queued.future.done():
                        queued.future.set_result(DecisionAnswer("unknown", None, 0.0, queued.sequence, stale=True))
                else:
                    retained.append(queued)
            self._pending = retained
            request = _PendingDecision(
                priority=int(priority), sequence=self._sequence, payload=payload,
                future=future, epoch=self._epoch,
            )
            self._pending.append(request)
            self._pending.sort(key=lambda item: item.sort_key)
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(self._run())
            self._condition.notify_all()
        return await future

    async def _run(self) -> None:
        while True:
            # Transport cleanup may turn a cancellation into a final unknown
            # response. Do not then become an uncancellable empty-queue waiter
            # while asyncio is shutting the session down.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            async with self._condition:
                while not self._pending and not self._closed:
                    await self._condition.wait()
                if self._closed and not self._pending:
                    return
                request = self._pending.pop(0)
                self._inflight = request
            try:
                answer = await self._post(request)
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.set_result(DecisionAnswer("unknown", None, 0.0, request.sequence, stale=True))
                raise
            except Exception as exc:
                self.emit("decision_client_failed", request_id=request.sequence, error_type=type(exc).__name__)
                answer = DecisionAnswer("unknown", None, 0.0, request.sequence)
            async with self._condition:
                if request.epoch != self._epoch:
                    answer = DecisionAnswer("unknown", answer.raw, answer.elapsed_s, request.sequence, stale=True)
                if not request.future.done():
                    request.future.set_result(answer)
                self._inflight = None
                if self._closed and not self._pending:
                    return

    async def _post(self, request: _PendingDecision) -> DecisionAnswer:
        started = time.monotonic()
        if self._client is None:
            self._client = httpx.AsyncClient(trust_env=False, timeout=self.timeout_s)
        self.emit("decision_request", request_id=request.sequence, priority=request.priority, payload=request.payload)
        raw: dict[str, Any] | None = None
        choice = "unknown"
        try:
            response = await asyncio.wait_for(
                self._client.post(self.url, json=request.payload),
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            decoded = response.json()
            raw = decoded if isinstance(decoded, dict) else None
            answers = raw.get("answers", {}) if raw else {}
            answer = answers.get("q", {}) if isinstance(answers, dict) else {}
            if not isinstance(answer, dict) or not answer.get("choice"):
                for key in ("danger", "normal", "protective"):
                    candidate = answers.get(key) if isinstance(answers, dict) else None
                    if isinstance(candidate, dict) and candidate.get("choice"):
                        answer = candidate
                        break
            selected = answer.get("choice") if isinstance(answer, dict) else None
            if isinstance(selected, str) and selected:
                choice = selected
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        elapsed = time.monotonic() - started
        self.emit("decision_response", request_id=request.sequence, priority=request.priority,
                  choice=choice, response=raw, elapsed_s=elapsed)
        return DecisionAnswer(choice, raw, elapsed, request.sequence)

    async def close(self) -> None:
        self._closed = True
        async with self._condition:
            for request in self._pending:
                if not request.future.done():
                    request.future.set_result(DecisionAnswer("unknown", None, 0.0, request.sequence, stale=True))
            self._pending.clear()
            self._condition.notify_all()
        if self._worker is not None and not self._worker.done():
            await self._worker
        if self._own_client and self._client is not None:
            await self._client.aclose()


__all__ = ["DecisionAnswer", "DecisionClient", "DecisionPriority"]
