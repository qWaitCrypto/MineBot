"""Test-only helper: synthesize a ``blockCells`` perception from a fake body.

Production ``ScarpetBody`` answers ``blockCells`` server-side in one round-trip.
The unit-test fake bodies were built around per-cell ``blockAt`` responses
(either a spatial dict or a prepared-response queue). Rather than rewrite every
fake to carry a native batch response, this helper builds a ``blockCells``
``PerceptionResult`` by looping the fake's OWN ``blockAt`` handler once per
requested cell. The ``wanted`` positions are emitted in ``[feet, below, head]``
order by the stand-point callers, matching the legacy per-cell query order, so
queue-based fakes that prepared ``blockAt`` responses in that order still align.
"""

from __future__ import annotations

from minebot.contract import PerceptionResult


def batch_block_cells_from_blockat(body, params, *, bot: str = "Bot1") -> PerceptionResult:
    cells = params.get("cells") or []
    start = int(params.get("start") or 0)
    limit = int(params.get("limit") or 64)
    page = cells[start : start + limit]
    facts = []
    for c in page:
        pos = (int(c[0]), int(c[1]), int(c[2]))
        pr = body.perceive("blockAt", {"x": pos[0], "y": pos[1], "z": pos[2]})
        facts.append(
            {
                "x": pos[0],
                "y": pos[1],
                "z": pos[2],
                "type": pr.data.get("type", "air"),
                "state": pr.data.get("state", "CLEAR"),
                "properties": dict(pr.data.get("properties") or {}),
            }
        )
    next_idx = start + len(page)
    nxt = None if next_idx >= len(cells) else next_idx
    return PerceptionResult(
        bot=bot,
        scope="blockCells",
        type="perception",
        ok=True,
        complete=nxt is None,
        data={"count": len(page), "total": len(cells), "next": nxt, "cells": facts},
        uncertainty=[] if nxt is None else [{"reason": "limit_exceeded"}],
        next=None,
        error=None,
    )
