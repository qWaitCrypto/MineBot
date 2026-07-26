"""Chat egress decoration for the F6 expression policy.

brain-cognitive-framework.md §9. The cadence gate is enforced by wrapping the
speech sink, deliberately NOT by editing the runner: the spine never grows for
a faculty (framework §2). The runner keeps calling one opaque callable; this
module decides whether that call reaches the world.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from minebot.brain.expression import ExpressionPolicy, NarrationGate

SpeechSink = Callable[[str], None]
TraceEmit = Callable[..., None]


def expression_speech_sink(
    inner: SpeechSink,
    *,
    policy: ExpressionPolicy | None = None,
    trace: TraceEmit | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SpeechSink:
    """Wrap a speech sink with the F6 narration gate.

    The default policy is inert (no throttle, consecutive-duplicate
    suppression only), so wrapping changes nothing until an operator supplies
    a paced policy. Suppression is always traced — silence is a decision, and
    an untraced silence is indistinguishable from a bug.
    """

    gate = NarrationGate(policy)

    def sink(text: str) -> None:
        now = clock()
        decision = gate.decide(text, now=now)
        if not decision.emit:
            if trace is not None and decision.reason != "empty":
                trace("speech_suppressed", reason=decision.reason, chars=len(text))
            return
        inner(text)
        gate.record(text, now=now)

    return sink


__all__ = ["expression_speech_sink"]
