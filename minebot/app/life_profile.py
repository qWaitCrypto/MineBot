"""The Agent's own life profile: one JSON file per world and bot.

The Agent writes and revises it (``read_life`` / ``update_life`` /
``update_life_project``); the reflex loop reads it every idle frame.  The
program checks only shape and the write boundary of projects, never whether
the content is right (``docs/design-docs/life-g4-g5-20261015.md``).
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from minebot.app.skill_format import MAX_SKILL_NAME_CHARS, SKILL_NAME_PATTERN
from minebot.app.world_record import WorldRecord, block_name, dimension_id

PROFILE_FIELDS = ("home", "mine", "outposts", "belongings", "routine", "rules", "relationships", "personality",
                  "stance")
# Agent-confirmed stand cells at home (pro plan P5); the anchor ``pos`` stays.
MAX_HOME_ARRIVALS = 4
PROJECT_STATUSES = ("active", "paused", "done")
MAX_TEXT_CHARS = 1000
# The Agent's reading of the Body's situation, read first in every frame
# (closed-loop-architecture-20261107.md §3 A): a few sentences, not a plan.
MAX_STANCE_CHARS = 500
MAX_RELATIONSHIPS = 20
MAX_AREAS = 16
MAX_CONTAINERS = 32
# Home and outposts (vision-and-home-20261028.md §7.7): what each storage row may hold.
MAX_OUTPOSTS = 8
MAX_HOLDS = 32
# What players said is theirs or shared.
MAX_BELONGINGS = 32
_ITEM_PATTERN = re.compile(r"[a-z0-9_.*-]{1,64}")
_DIMENSION_ID = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+")
# The projects are the Agent's plan ladder (plan-ladder-20261028.md §7.1).
MAX_PROJECTS = 32
# An item's intent and when are one sentence each: the reflex layer reads
# them in every frame.  why and note are the Agent's own and keep
# MAX_TEXT_CHARS (s7-later-20261029.md §8 D7).
MAX_INTENT_CHARS = 300
_SENTENCE_HINT = ("intent and when are one sentence each, read by the reflex layer in every frame. Why, and what "
                  "to watch for, go in why or note; how each run went is kept in the item's account (LADDER, "
                  "read_life); a standing reaction is a rule (update_life); what you learned goes in write_memory.")
_NOTE_HINT = ("why and note are yours alone. How each run went is kept in the item's account (LADDER, read_life); "
              "what you learned goes in write_memory.")
MAX_RULES = 12
MAX_PROFILE_CHARS = 160_000
KEPT_CHANGES = 20
# Results kept in memory per project, and the run events since the last one;
# the run state is read from them.
KEPT_PROJECT_ROWS = 5
KEPT_PROJECT_EVENTS = 20
# Run states of a ladder item, written by the program, never by the Agent.
PROJECT_RUN_STATES = ("waiting", "running", "set_aside", "done")
# Finished items read_life lists, newest first (s7-later-20261029.md §8 D5).
SHOWN_FINISHED = 20
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_GENERATED_ID = re.compile(r"work-(\d+)")
# Skills mounted on ladder items (agent-skills-v2-20261105.md §2.1): the union
# over the items on the ladder, never more than this many or this much text.
MAX_MOUNTED_SKILLS = 4
MAX_MOUNTED_SKILL_CHARS = 40_000
_SKILLS_HINT = ("skills: names from AVAILABLE_SKILLS; each one's full text is added to your conversation once "
                "and stays in effect while the item is on the ladder; [] removes them.")


class LifeProfileError(ValueError):
    """The submitted profile or project does not have the required shape."""


class LifeRevisionConflict(LifeProfileError):
    """The profile changed since the writer read it."""


class SkillMountError(LifeProfileError):
    """A ladder item's skills cannot be mounted; ``code`` is the tool's reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LifeProfileUnreadable(RuntimeError):
    """The profile file exists but cannot be read; it is never taken as empty."""


def load_profile(path: str | None = None) -> dict[str, Any]:
    raw = path or os.environ.get("MINEBOT_LIFE_PROFILE")
    if not raw:
        return {}
    try:
        value = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def life_intent(profile: Mapping[str, Any]) -> str:
    parts = []
    for key in ("routine", "personality"):
        if isinstance(profile.get(key), str) and profile[key].strip():
            parts.append(profile[key].strip())
    relationships = profile.get("relationships")
    if isinstance(relationships, list):
        parts.extend(str(item.get("note")) for item in relationships
                     if isinstance(item, dict) and item.get("note"))
    return " ".join(parts)


def life_file_path(directory: Path, scope: Any) -> Path:
    """One file per server, world and bot; the name stays readable."""
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(scope.bot_id))[:48] or "bot"
    return Path(directory) / f"{readable}-{scope.key[:16]}.json"


def rebind_life_files(directory: Path, old: Any, new: Any, *, dry_run: bool) -> list[dict]:
    """Rename the profile, project log and world record, refusing any occupied destination.

    Hard-link then unlink implements a same-directory rename without the
    overwrite race of os.rename. Undo completed moves if a later move fails.
    """
    if old.server_id != new.server_id or old.bot_id != new.bot_id or old.world_id == new.world_id:
        raise ValueError("rebind requires the same server and bot, and different world ids")
    source = life_file_path(directory, old)
    target = life_file_path(directory, new)
    pairs = [(source, target)] + [(source.with_suffix(suffix), target.with_suffix(suffix))
                                  for suffix in (".project-log.jsonl", ".world.jsonl")]
    plan = []
    for before, after in pairs:
        if os.path.lexists(after):
            raise FileExistsError(f"refusing to overwrite life file: {after}")
        if os.path.lexists(before) and (before.is_symlink() or not before.is_file()):
            raise ValueError(f"life source must be a regular file: {before}")
        plan.append({"from": str(before), "to": str(after), "exists": before.exists()})
    if dry_run:
        return plan
    completed = []
    try:
        for before, after in pairs:
            if before.exists():
                os.link(before, after)
                completed.append((before, after))
                before.unlink()
    except OSError:
        for before, after in reversed(completed):
            if not before.exists():
                os.link(after, before)
            after.unlink()
        raise
    return plan


class LifeStore:
    """Versioned life profile and project log for one ``RuntimeScope``.

    Writes go through a temporary file and a rename, so a crash never leaves a
    half-written profile.  ``changes`` keeps the reasons of the last
    ``KEPT_CHANGES`` revisions; project results are program-written facts and
    live in a separate append-only log beside the profile, and so does the
    Body's record of its places (``world``).
    """

    def __init__(self, directory: Path, scope: Any, *, seed_path: str | None = None) -> None:
        self.path = life_file_path(directory, scope)
        self.log_path = self.path.with_suffix(".project-log.jsonl")
        self.world = WorldRecord(self.path.with_suffix(".world.jsonl"))
        self.scope = scope
        self._lock = threading.Lock()
        self._results: dict[str, list[dict[str, Any]]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        # Finished items that left the ladder, by id: when, why and how they were written.
        self._archive: dict[str, dict[str, Any]] = {}
        # Each item's account over its whole log: runs, time, how they ended, rewrites.
        self._accounts: dict[str, dict[str, Any]] = {}
        self._load_recent()
        self._reconcile_restart()
        if not self.path.exists():
            seed = load_profile(seed_path)
            if seed:
                try:
                    profile = normalize_profile(seed, partial=True)
                except LifeProfileError:
                    profile = None
                if profile is not None:
                    self._write({"revision": 1, "profile": profile, "changes": [
                        {"revision": 1, "at": _now(), "author": "user_file",
                         "reason": "imported from MINEBOT_LIFE_PROFILE"}]})

    # -- reads -------------------------------------------------------------

    def version(self) -> tuple[int, int] | None:
        """A cheap change marker for per-frame reads: (mtime_ns, size)."""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    def profile(self) -> dict[str, Any]:
        return self.read()["profile"]

    def project_results(self, project_id: str | None = None, *, limit: int = 3) -> list[dict[str, Any]]:
        """The last results (rows with an outcome) of one project, oldest first."""
        if project_id is not None and limit <= KEPT_PROJECT_ROWS:
            with self._lock:
                rows = [dict(row) for row in self._results.get(project_id, ())]
            return rows[-limit:]
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (isinstance(row, dict) and "outcome" in row
                    and (project_id is None or row.get("project") == project_id)):
                rows.append(row)
                if len(rows) >= limit:
                    break
        return list(reversed(rows))

    def project_state(self, project: Mapping[str, Any]) -> dict[str, Any]:
        """The program's run state of one ladder item, from its log rows.

        waiting: never run, ended for a reason that is not the item's own
        (taken by the Agent, a restart), changed by the Agent since, or a
        standing item that completed.  running: started, not ended.
        set_aside: ended without completing; it stays out of the frames until
        the Agent changes it.  done: completed.  ``off_ladder`` says why a
        finished item leaves the ladder (§8 D5).
        """
        with self._lock:
            return self._state(project)

    def finished_items(self) -> list[dict[str, Any]]:
        """Items that left the ladder, newest first: id, intent, why, when, last result."""
        with self._lock:
            entries = sorted(self._archive.items(), key=lambda pair: str(pair[1].get("at") or ""), reverse=True)
            out = []
            for item_id, entry in entries[:SHOWN_FINISHED]:
                item = entry.get("item") if isinstance(entry.get("item"), Mapping) else {}
                row = {"id": item_id, "intent": item.get("intent"), "why": entry.get("why"), "at": entry.get("at")}
                last = (self._results.get(item_id) or [None])[-1]
                if last is not None:
                    row["last_result"] = dict(last)
                out.append(row)
        for row in out:
            row["account"] = self.project_account(row["id"])
        return out

    def project_account(self, project_id: str) -> dict[str, Any]:
        """One item's account from its whole log (s7-later-20261029.md §8 D6).

        runs and seconds over every run; outcomes by kind; the last three
        ends (outcome, the step it stopped on or the reason); same_end_twice
        when the last two runs ended at the same step for the same reason,
        compared word for word; rewrites: the Agent's writes after the first.
        It is for the Agent alone; Decision's frames never carry it.
        """
        with self._lock:
            kept = self._accounts.get(project_id)
            if kept is None:
                return {"runs": 0, "seconds": 0.0, "outcomes": {}, "ends": [], "rewrites": 0}
            account = copy.deepcopy(kept)
        account["rewrites"] = max(0, account.pop("writes") - 1)
        last_two = [_end_cause(end) for end in account["ends"][-2:]]
        if len(last_two) == 2 and last_two[0] is not None and last_two[0] == last_two[1]:
            account["same_end_twice"] = account["ends"][-1].get("step") or {"reason": last_two[1][1]}
        return account

    def _state(self, project: Mapping[str, Any]) -> dict[str, Any]:
        rows = self._rows(str(project.get("id")))
        name, since = _run_state(rows, standing=bool(project.get("standing")))
        state: dict[str, Any] = {"state": name}
        if since is not None:
            state["since"] = since
        result = next((row for row in reversed(rows) if "outcome" in row), None)
        if result is not None:
            state["last_result"] = dict(result)
            # Nothing has happened to the item since that end: no new start,
            # no Agent change, no restart.
            state["ended_last"] = rows[-1] is result
        off = off_ladder(project, state)
        if off is not None:
            state["off_ladder"] = off
        return state

    def _take_finished(self, projects: list[dict[str, Any]], *, keep: str | None = None,
                       seen: str | None = None
                       ) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
        """Split the ladder into live items and finished ones that leave it now (§8 D5).

        Called inside the Agent's own write, so the revision only moves when
        the Agent writes; ``keep`` is the item that write is about.  ``seen``
        is when the Agent last wrote before this write: an item that completed
        past a reported step stays until the Agent has seen it, and it has
        once it wrote after that end.  A ladder over its size makes room with
        the items set aside longest ago; the log keeps them, and writing one's
        id again brings it back (run-9 2026-11-09: 20 of 32 items set aside,
        most for 8-12 hours, untouched; the Agent had to clear one by hand).
        """
        live: list[dict[str, Any]] = []
        leaving: list[tuple[dict[str, Any], str]] = []
        aside: list[tuple[str, dict[str, Any]]] = []
        for item in projects:
            state = {} if item.get("id") == keep else self._state(item)
            why = state.get("off_ladder")
            if (why is None and state.get("state") == "done" and seen is not None
                    and str(state.get("since") or "") < seen):
                why = "completed"
            if why is None:
                live.append(item)
                if state.get("state") == "set_aside":
                    aside.append((str(state.get("since") or ""), item))
            else:
                leaving.append((item, why))
        for _since, item in sorted(aside, key=lambda pair: pair[0])[:max(0, len(live) - MAX_PROJECTS)]:
            live.remove(item)
            leaving.append((item, "set_aside_room"))
        return live, leaving

    @staticmethod
    def _seen(record: Mapping[str, Any]) -> str | None:
        """When the profile was last written before this write."""
        changes = record.get("changes") or []
        return str(changes[-1].get("at")) if changes and isinstance(changes[-1], Mapping) and changes[-1].get("at") else None

    @staticmethod
    def _left(leaving: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
        """What the Agent hears of the items that left with its write."""
        return [{"id": item.get("id"), "intent": item.get("intent"), "why": why} for item, why in leaving]

    def _archived(self, leaving: list[tuple[dict[str, Any], str]]) -> None:
        """The log keeps each finished item as it was written; the same id written again brings it back."""
        for item, why in leaving:
            row = {"at": _now(), "project": str(item["id"]), "event": "archived", "why": why,
                   "item": json.loads(json.dumps(item))}
            try:
                with self._lock:
                    self._append(row)
            except OSError:
                with self._lock:
                    self._remember(row)

    # -- writes ------------------------------------------------------------

    def update(self, changes: Mapping[str, Any], *, expected_revision: int, reason: str,
               author: str = "agent") -> dict[str, Any]:
        unknown = set(changes) - set(PROFILE_FIELDS)
        if unknown:
            raise LifeProfileError(
                f"unknown profile fields {sorted(unknown)}; use {list(PROFILE_FIELDS)} "
                "(projects change through update_life_project)")
        if not changes:
            raise LifeProfileError("no changes given")
        with self._lock:
            record = self._read()
            self._check_revision(record, expected_revision)
            profile = dict(record["profile"])
            for key, value in changes.items():
                if value is None:
                    profile.pop(key, None)
                else:
                    profile[key] = value
            if "stance" in changes:
                # A cleared stance ends a ladder item's stance too; a new one replaces it by its time.
                if changes["stance"] is None and record["profile"].get("stance") is not None:
                    profile["stance_cleared_at"] = _now()
                else:
                    profile.pop("stance_cleared_at", None)
            profile = normalize_profile(profile, partial=True)
            _stamp_belongings(profile, record["profile"])
            leaving: list[tuple[dict[str, Any], str]] = []
            if profile.get("projects"):
                profile["projects"], leaving = self._take_finished(profile["projects"])
                if not profile["projects"]:
                    profile.pop("projects")
            committed = self._commit(record, profile, reason=reason, author=author)
        self._archived(leaving)
        return committed

    def update_project(self, operation: str, project: Mapping[str, Any], *, expected_revision: int,
                       reason: str, check_write: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None,
                       author: str = "agent", before: str | None = None,
                       check_ladder: Callable[[list[dict[str, Any]], Mapping[str, Any] | None], None] | None = None,
                       ) -> dict[str, Any]:
        if operation not in {"upsert", "remove"}:
            raise LifeProfileError("operation must be 'upsert' or 'remove'")
        project_id = str(project.get("id") or "")
        if len(project_id) > 48:
            raise LifeProfileError(f"project.id is {len(project_id)} characters; the limit is 48")
        if not _PROJECT_ID.fullmatch(project_id):
            raise LifeProfileError("project id must be 1-48 lowercase letters, digits or '-'")
        with self._lock:
            record = self._read()
            self._check_revision(record, expected_revision)
            profile = dict(record["profile"])
            projects = [item for item in profile.get("projects") or [] if item.get("id") != project_id]
            if operation == "upsert":
                previous = next((item for item in profile.get("projects") or []
                                 if item.get("id") == project_id), None)
                given = {key: value for key, value in project.items() if value is not None}
                # One sentence each for what this write gives; items written
                # before the limit keep theirs until rewritten (§8 D7).
                for key in ("intent", "when"):
                    if isinstance(given.get(key), str) and len(given[key]) > MAX_INTENT_CHARS:
                        raise LifeProfileError(f"{project_id}.{key} is {len(given[key])} characters; the limit is "
                                               f"{MAX_INTENT_CHARS}. {_SENTENCE_HINT}")
                # A finished item written again comes back as it was, with its
                # history, unless the write marks it done (§8 D5).
                finished = previous is None or self._state(previous).get("off_ladder") is not None
                if previous is None:
                    archived = (self._archive.get(project_id) or {}).get("item")
                    previous = dict(archived) if isinstance(archived, Mapping) else {}
                if finished and previous and "status" not in given:
                    previous = {**previous, "status": "active"}
                merged = {**previous, **given}
                checked = normalize_project(merged, profile)
                if check_write is not None:
                    check_write(checked, profile)
                if before is not None:
                    # Order is priority: insert or move the item in front of another.
                    if before == project_id:
                        raise LifeProfileError("before must name another project, not this one")
                    position = next((index for index, item in enumerate(projects)
                                     if item.get("id") == before), None)
                    if position is None:
                        raise LifeProfileError(f"before names no project {before!r}")
                else:
                    position = next((index for index, item in enumerate(profile.get("projects") or [])
                                     if item.get("id") == project_id), len(projects))
                projects.insert(position, checked)
            elif before is not None:
                raise LifeProfileError("before only applies to upsert")
            elif len(projects) == len(profile.get("projects") or []):
                raise LifeProfileError(f"no project {project_id!r}")
            # Finished items leave with this write; one the Agent marks done leaves now.
            keep = project_id if operation == "upsert" and checked.get("status") != "done" else None
            projects, leaving = self._take_finished(projects, keep=keep, seen=self._seen(record))
            if len(projects) > MAX_PROJECTS:
                raise LifeProfileError(
                    f"at most {MAX_PROJECTS} projects on the ladder; finished and long set-aside ones leave "
                    "by themselves, so remove or mark done an item you no longer mean to do")
            if check_ladder is not None:
                # The ladder as it will be after this write: what stays mounted.
                check_ladder(projects, checked if operation == "upsert" else None)
            profile["projects"] = projects
            if not projects:
                profile.pop("projects")
            committed = self._commit(record, normalize_profile(profile, partial=True), reason=reason, author=author)
        self._archived(leaving)
        if leaving:
            committed["left_ladder"] = self._left(leaving)
        if operation == "upsert" and keep is not None:
            # A changed item waits again; a set-aside one comes back to the frames.
            self._changed(project_id, committed["revision"])
        return committed

    def put_delegated(self, plan: Mapping[str, Any], *, item_id: str | None = None,
                      after: str | None = None) -> dict[str, Any]:
        """A delegate_work plan becomes one ladder item (§7.2).

        It goes to the top, or right after ``after`` (the Agent's own item
        running now).  Without an id it gets ``work-<n>``, never one a
        finished item had.  Finished items leave the ladder with this write,
        as with every write of the Agent's (s7-later-20261029.md §8 D5).
        """
        item = {"intent": str(plan.get("goal") or "").strip(), "work": plan.get("work"),
                "status": "active", "timeout_s": plan.get("timeout_s"),
                "interrupt_when": plan.get("interrupt_when")}
        if plan.get("skills"):
            # Mounted while this plan runs, also when the reflex layer starts
            # it later from the ladder; not while it only waits there.
            item["plan_skills"] = list(plan["skills"])
        if len(item["intent"]) > MAX_INTENT_CHARS:
            raise LifeProfileError(
                f"goal is {len(item['intent'])} characters; the limit is {MAX_INTENT_CHARS}: the goal is the "
                f"intent of the plan's ladder item. {_SENTENCE_HINT}")
        with self._lock:
            record = self._read()
            profile = dict(record["profile"])
            projects = list(profile.get("projects") or [])
            if item_id is None:
                taken = {str(row.get("id")) for row in projects} | set(self._archive)
                number = 1 + max((int(match.group(1)) for name in taken
                                  if (match := _GENERATED_ID.fullmatch(name))), default=0)
                item_id = f"work-{number}"
            elif not _PROJECT_ID.fullmatch(item_id):
                raise LifeProfileError("id must be 1-48 lowercase letters, digits or '-'")
            projects = [row for row in projects if row.get("id") != item_id]
            checked = normalize_project({"id": item_id, **item}, profile)
            position = next((index + 1 for index, row in enumerate(projects) if row.get("id") == after), 0)
            projects.insert(position, checked)
            projects, leaving = self._take_finished(projects, keep=item_id, seen=self._seen(record))
            if len(projects) > MAX_PROJECTS:
                raise LifeProfileError(
                    f"your plan ladder is full ({MAX_PROJECTS} items); finished and long set-aside ones leave by "
                    "themselves, so remove or mark done an item you no longer mean to do with update_life_project")
            profile["projects"] = projects
            committed = self._commit(record, normalize_profile(profile, partial=True),
                                     reason=f"delegate_work: {item['intent']}", author="agent")
        self._archived(leaving)
        if leaving:
            committed["left_ladder"] = self._left(leaving)
        self._changed(item_id, committed["revision"])
        committed["item_id"] = item_id
        return committed

    def record_project_result(self, project_id: str, result: Mapping[str, Any]) -> None:
        row = {"at": _now(), **json.loads(json.dumps(result, default=str)), "project": project_id}
        with self._lock:
            self._append(row)

    def record_project_event(self, project_id: str, event: str, **fields: Any) -> None:
        """A run event without an outcome: started (with who), agent_changed, restart."""
        self.record_project_result(project_id, {"event": event, **fields})

    # -- internals ---------------------------------------------------------

    def _changed(self, project_id: str, revision: int) -> None:
        # The profile is already committed: a log that cannot be written must
        # not turn that into a failed call (the revision has moved on).  The
        # run state still learns of the change from memory.
        try:
            self.record_project_event(project_id, "agent_changed", revision=revision)
        except OSError:
            with self._lock:
                self._remember({"at": _now(), "event": "agent_changed", "revision": revision, "project": project_id})

    def _append(self, row: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        self._remember(row)

    def _remember(self, row: Mapping[str, Any]) -> None:
        project = str(row.get("project"))
        self._tally(project, row)
        if "outcome" in row:
            kept = self._results.setdefault(project, [])
            kept.append(dict(row))
            del kept[:-KEPT_PROJECT_ROWS]
            self._events.pop(project, None)
        else:
            if row.get("event") == "archived" and isinstance(row.get("item"), Mapping):
                self._archive[project] = {key: row.get(key) for key in ("at", "why", "item")}
            elif row.get("event") == "agent_changed":
                self._archive.pop(project, None)
            events = self._events.setdefault(project, [])
            if row.get("event") == "agent_changed" and _run_state(self._rows(project), standing=False)[0] == "running":
                return  # changes apply between runs; the log still has the row
            events.append(dict(row))
            del events[:-KEPT_PROJECT_EVENTS]

    def _tally(self, project: str, row: Mapping[str, Any]) -> None:
        account = self._accounts.setdefault(
            project, {"runs": 0, "seconds": 0.0, "outcomes": {}, "ends": [], "writes": 0})
        if "outcome" in row:
            outcome = str(row.get("outcome"))
            account["runs"] += 1
            if isinstance(row.get("duration_s"), (int, float)):
                account["seconds"] = round(account["seconds"] + float(row["duration_s"]), 1)
            account["outcomes"][outcome] = account["outcomes"].get(outcome, 0) + 1
            end: dict[str, Any] = {"outcome": outcome, "at": row.get("at")}
            if isinstance(row.get("stopped_at"), Mapping):
                end["step"] = dict(row["stopped_at"])
            elif row.get("reason"):
                end["reason"] = str(row["reason"])[:200]
            if isinstance(row.get("inventory_change"), Mapping):
                end["inventory_change"] = dict(row["inventory_change"])
            account["ends"] = [*account["ends"], end][-3:]
        elif row.get("event") == "agent_changed":
            account["writes"] += 1

    def _rows(self, project: str) -> list[dict[str, Any]]:
        """The last result and the run events after it, in log order."""
        results = self._results.get(project) or []
        return [*results[-1:], *self._events.get(project, ())]

    def _load_recent(self) -> None:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("project"):
                self._remember(row)

    def _reconcile_restart(self) -> None:
        """A store opens once per session start: an item the log shows started
        and never ended was cut off by the restart, and waits again."""
        with self._lock:
            running = [project for project in self._events
                       if _run_state(self._rows(project), standing=False)[0] == "running"]
            for project in running:
                self._append({"at": _now(), "project": project, "event": "restart"})

    def _read(self) -> dict[str, Any]:
        # Only a missing file is an empty profile.  Any other read or parse
        # error raises, so no write can replace a profile it failed to read.
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"revision": 0, "profile": {}, "changes": []}
        except OSError as exc:
            raise LifeProfileUnreadable(f"cannot read life profile {self.path}: {exc}") from exc
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise LifeProfileUnreadable(f"life profile {self.path} is not valid JSON: {exc}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("profile"), dict):
            raise LifeProfileUnreadable(f"life profile {self.path} has no profile object")
        try:
            return {"revision": int(value.get("revision") or 0), "profile": value["profile"],
                    "changes": list(value.get("changes") or [])}
        except (TypeError, ValueError) as exc:
            raise LifeProfileUnreadable(f"life profile {self.path} has a bad revision or changes: {exc}") from exc

    @staticmethod
    def _check_revision(record: Mapping[str, Any], expected: int) -> None:
        if int(expected) != int(record["revision"]):
            raise LifeRevisionConflict(
                f"the profile is at revision {record['revision']}, not {expected}; read_life and retry")

    def _commit(self, record: Mapping[str, Any], profile: dict[str, Any], *, reason: str,
                author: str) -> dict[str, Any]:
        reason = str(reason or "").strip()
        if not reason:
            raise LifeProfileError("say in one sentence why the profile changes")
        revision = int(record["revision"]) + 1
        changes = [*record["changes"], {"revision": revision, "at": _now(), "author": author,
                                        "reason": reason[:MAX_TEXT_CHARS]}][-KEPT_CHANGES:]
        value = {"revision": revision, "profile": profile, "changes": changes}
        self._write(value)
        return copy.deepcopy(value)

    def _write(self, value: Mapping[str, Any]) -> None:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        if len(text) > MAX_PROFILE_CHARS:
            raise LifeProfileError(
                f"serialized profile is {len(text)} characters; the limit is {MAX_PROFILE_CHARS}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


# -- shape checks ------------------------------------------------------------


def normalize_profile(profile: Mapping[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Check the shape of every known field; keep unknown seed fields out."""
    out: dict[str, Any] = {}
    if profile.get("home") is not None:
        out["home"] = _place(profile["home"], "home")
    if profile.get("mine") is not None:
        out["mine"] = _mine(profile["mine"])
    if profile.get("outposts") is not None:
        outposts = profile["outposts"]
        if not isinstance(outposts, list) or len(outposts) > MAX_OUTPOSTS:
            raise LifeProfileError(f"outposts must be a list of at most {MAX_OUTPOSTS}")
        out["outposts"] = [_place(item, f"outposts[{index}]", outpost=True) for index, item in enumerate(outposts)]
        names = [item["name"] for item in out["outposts"]]
        if len(set(names)) != len(names):
            raise LifeProfileError("two outposts have the same name")
    if profile.get("belongings") is not None:
        belongings = profile["belongings"]
        if not isinstance(belongings, list) or len(belongings) > MAX_BELONGINGS:
            raise LifeProfileError(f"belongings must be a list of at most {MAX_BELONGINGS}")
        out["belongings"] = [_belonging(item, f"belongings[{index}]") for index, item in enumerate(belongings)]
    for key in ("routine", "personality"):
        if profile.get(key) is not None:
            out[key] = _text(profile[key], key)
    if profile.get("relationships") is not None:
        relationships = profile["relationships"]
        if not isinstance(relationships, list) or len(relationships) > MAX_RELATIONSHIPS:
            raise LifeProfileError(f"relationships must be a list of at most {MAX_RELATIONSHIPS}")
        out["relationships"] = []
        for index, item in enumerate(relationships):
            if not isinstance(item, Mapping) or not str(item.get("player") or "").strip():
                raise LifeProfileError(f"relationships[{index}] needs a player name")
            row = {"player": _text(item["player"], f"relationships[{index}].player")}
            _optional_text(item, row, "note", f"relationships[{index}].note")
            out["relationships"].append(row)
    if profile.get("rules") is not None:
        out["rules"] = _rules(profile["rules"])
    if profile.get("stance") is not None:
        out["stance"] = _stance(profile["stance"])
    if profile.get("stance_cleared_at") is not None and _stance_time(profile["stance_cleared_at"]) is not None:
        out["stance_cleared_at"] = profile["stance_cleared_at"]
    if profile.get("projects") is not None:
        projects = profile["projects"]
        if not isinstance(projects, list):
            raise LifeProfileError("projects must be a list")
        out["projects"] = [normalize_project(item, {**profile, **out}) for item in projects]
    return out


def normalize_project(project: Mapping[str, Any], profile: Mapping[str, Any]) -> dict[str, Any]:
    from minebot.app.work_runtime import WorkPlanError, validate_plan

    if not isinstance(project, Mapping):
        raise LifeProfileError("a project must be an object")
    project_id = str(project.get("id") or "")
    if len(project_id) > 48:
        raise LifeProfileError(f"project.id is {len(project_id)} characters; the limit is 48")
    if not _PROJECT_ID.fullmatch(project_id):
        raise LifeProfileError("project id must be 1-48 lowercase letters, digits or '-'")
    out = {"id": project_id, "intent": _text(project.get("intent"), f"{project_id}.intent")}
    status = str(project.get("status") or "active")
    if status not in PROJECT_STATUSES:
        raise LifeProfileError(f"{project_id}.status must be one of {list(PROJECT_STATUSES)}")
    out["status"] = status
    # Maturity is what is written: id and intent (think), when or area (look),
    # work (walk).  why is for the Agent; when is read by Decision.
    for key in ("note", "why"):
        _optional_text(project, out, key, f"{project_id}.{key}", hint=_NOTE_HINT)
    _optional_text(project, out, "when", f"{project_id}.when")
    standing = project.get("standing")
    if standing is not None:
        if not isinstance(standing, bool):
            raise LifeProfileError(f"{project_id}.standing must be true or false")
        if standing:
            out["standing"] = True
    skills = project_skills(project, project_id)
    if skills:
        out["skills"] = skills
    if project.get("stance") is not None:
        out["stance"] = _item_stance(project["stance"], project_id)
    if project.get("rules") is not None:
        try:
            out["rules"] = _rules(project["rules"])
        except LifeProfileError as exc:
            raise LifeProfileError(f"{project_id}.{exc}") from None
    if project.get("area") is not None:
        areas = {area["name"] for area in (profile.get("mine") or {}).get("areas") or []
                 if isinstance(area, Mapping) and isinstance(area.get("name"), str)}
        area = str(project.get("area") or "")
        if area not in areas:
            raise LifeProfileError(
                f"{project_id}.area must name one of the profile's mine.areas {sorted(areas)}; "
                "mark the place as yours first with update_life")
        out["area"] = area
    work = project.get("work")
    if work is None:
        return out
    if not isinstance(work, Mapping):
        raise LifeProfileError(f"{project_id}.work must be one minebot-work/0.2 work node")
    if not str(work.get("intent") or "").strip():
        # As in delegate_work: the node's own intent, written as Body facts, is
        # what "is it done" checks; the project sentence is not.
        raise LifeProfileError(
            f"{project_id}.work.intent is required: the result as facts the Body reports "
            "(item ids and counts, held item, position)")
    # A delegated plan keeps its own time limit and interruption on the item.
    timeout_s = project.get("timeout_s")
    if timeout_s is not None:
        if not _number(timeout_s) or timeout_s <= 0:
            raise LifeProfileError(f"{project_id}.timeout_s must be a positive number")
        out["timeout_s"] = timeout_s
    if project.get("interrupt_when") is not None:
        out["interrupt_when"] = json.loads(json.dumps(project["interrupt_when"]))
    # A delegated plan's own skills (mounted while it runs, not while it waits).
    plan_skills = project_skills({"skills": project.get("plan_skills")}, f"{project_id}.plan")
    if plan_skills:
        out["plan_skills"] = plan_skills
    try:
        validate_plan(project_plan(out, work))
    except WorkPlanError as exc:
        raise LifeProfileError(f"{project_id}.work: {exc}") from exc
    out["work"] = json.loads(json.dumps(work))
    return out


def project_skills(project: Mapping[str, Any], project_id: str = "") -> list[str]:
    """The item's mounted Skill names, checked for shape only; [] or absent means none."""
    value = project.get("skills")
    if value is None:
        return []
    name = f"{project_id or project.get('id')}.skills"
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise LifeProfileError(f"{name} must be a list of Skill names")
    clean = [item.strip() for item in value]
    for item in clean:
        if len(item) > MAX_SKILL_NAME_CHARS or not SKILL_NAME_PATTERN.fullmatch(item):
            raise LifeProfileError(f"{name}: {item!r} is not a Skill name (lowercase words joined by '-')")
    if len(set(clean)) != len(clean):
        raise LifeProfileError(f"{name} names a Skill twice")
    if len(clean) > MAX_MOUNTED_SKILLS:
        raise SkillMountError("skill_mount_count_exceeded",
                              f"{name} has {len(clean)} Skills; at most {MAX_MOUNTED_SKILLS} Skills may be mounted")
    return clean


def mounted_skill_names(projects: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """(name, first item holding it) over the given items, in ladder order."""
    held: dict[str, str] = {}
    for item in projects:
        if not isinstance(item, Mapping):
            continue
        for name in item.get("skills") or ():
            held.setdefault(str(name), str(item.get("id")))
    return list(held.items())


def _run_state(rows: Iterable[Mapping[str, Any]], *, standing: bool) -> tuple[str, str | None]:
    """An Agent change does not stop a running item (§7.8); it only brings a
    set-aside or done item back to waiting."""
    state, since = "waiting", None
    for row in rows:
        event, before = row.get("event"), state
        if event == "agent_changed" and state == "running":
            continue
        if "outcome" in row:
            outcome = row.get("outcome")
            if outcome == "completed":
                state = "waiting" if standing else "done"
            elif outcome == "yielded" or (outcome == "cancelled"
                                          and str(row.get("reason") or "").startswith(
                                              "generation_invalidated:session_command")):
                # Yielded, or ended because the session itself went away (a
                # quit before a restart): not the item's fault, it waits
                # again like a restart row (§7.9).
                state = "waiting"
            else:
                state = "set_aside"
        elif event == "started":
            state = "running"
        elif event in ("restart", "agent_changed"):
            state = "waiting"
        if state != before or since is None or "outcome" in row:
            since = row.get("at")
    return state, since


def _end_cause(end: Mapping[str, Any]) -> tuple[Any, str] | None:
    """What one run ended on, for the word-for-word "same step, same reason" check."""
    if end.get("outcome") == "completed":
        return None
    step = end.get("step") if isinstance(end.get("step"), Mapping) else None
    if step is not None:
        cause = step.get("result") or step.get("why")
        return (step.get("node"), str(cause)) if cause else None
    return (None, str(end["reason"])) if end.get("reason") else None


def off_ladder(project: Mapping[str, Any], state: Mapping[str, Any]) -> str | None:
    """Why a finished item leaves the ladder, or None while it is still live.

    The Agent marked it done, or a one-shot item's last run completed with
    every step judged done.  An item that completed past a reported step
    stays until the Agent has seen it (s7-later-20261029.md §8 D5).
    """
    if project.get("status") == "done":
        return "done_by_you"
    if project.get("standing") or state.get("state") != "done":
        return None
    last = state.get("last_result") if isinstance(state.get("last_result"), Mapping) else {}
    return None if last.get("reported_steps") else "completed"


def project_plan(project: Mapping[str, Any], work: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The project as a delegate_work plan: same language, same executor."""
    node = dict(work if work is not None else project["work"])
    plan = {"language": "minebot-work/0.2", "goal": f"my project {project['id']}: {project['intent']}",
            "timeout_s": project.get("timeout_s", 1800), "work": node}
    if project.get("interrupt_when") is not None:
        plan["interrupt_when"] = copy.deepcopy(project["interrupt_when"])
    if project.get("plan_skills"):
        plan["skills"] = list(project["plan_skills"])
    return plan


def project_write_points(work: Any, is_world_write: Callable[[str], bool]) -> Iterable[tuple[str, list[float]]]:
    """Every coordinate a world-writing call in the tree names."""
    if isinstance(work, Mapping):
        if work.get("kind") == "call" and is_world_write(str(work.get("tool") or "")):
            for point in _points(work.get("arguments")):
                yield str(work.get("tool")), point
        for value in work.values():
            if isinstance(value, (Mapping, list)):
                yield from project_write_points(value, is_world_write)
    elif isinstance(work, list):
        for item in work:
            yield from project_write_points(item, is_world_write)


def inside_area(point: list[float], area: Mapping[str, Any]) -> bool:
    low = [min(a, b) for a, b in zip(area["from"], area["to"])]
    high = [max(a, b) for a, b in zip(area["from"], area["to"])]
    return all(lo <= value <= hi + 1 for lo, value, hi in zip(low, point, high))


def life_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """What the Agent sees each turn: where it lives and how.

    The plan ladder is not repeated here: LADDER is the one place that
    lists its items, in the Body's own words (s7-later-20261029.md §8 D4).
    """
    profile = record.get("profile") or {}
    summary: dict[str, Any] = {"revision": record.get("revision", 0)}
    if not profile:
        summary["note"] = ("You have no life profile in this world yet. Write one with update_life: "
                           "a home at coordinates you have read, a routine, how you treat players.")
        return summary
    for key in ("home", "routine", "rules", "stance", "personality", "relationships"):
        if key in profile:
            summary[key] = profile[key]
    if profile.get("mine"):
        summary["mine_areas"] = [area["name"] for area in profile["mine"].get("areas") or []]
    return summary


# -- Agent tools -------------------------------------------------------------

_LIFE_TEXT_RULE = (
    "Write everything in English. Use coordinates you have actually read, with the dimension. "
    "Follow or stand-by is not a switch: say it in a routine or relationship sentence; the reflex "
    "layer reads those sentences every second while you are not driving the Body."
)


def register_life_tools(registry: Any, store: LifeStore, *,
                        skill_check: Callable[[list[str], list[str]], None] | None = None) -> None:
    """``read_life`` / ``update_life`` / ``update_life_project``; none moves the Body.

    ``skill_check(written, mounted)`` raises ``SkillMountError`` when a Skill
    the write names cannot be mounted or the mounted set after the write
    breaks a cap; without it a write that names Skills is rejected.
    """
    from minebot.brain.registry import RegisteredTool, ToolSidecar
    from minebot.contract import ToolResult

    def sidecar(name: str, permission: str, tool_type: str) -> Any:
        return ToolSidecar(name, mutating=False, source="agent.life", tool_type=tool_type,
                           permission=permission, body_scope=(), terminal_truth=("LifeProfile.revision",))

    def shown(_reason: str, metrics: dict[str, object]) -> dict[str, object]:
        return dict(metrics)

    def read(_params: dict[str, object]) -> Any:
        try:
            record = store.read()
        except LifeProfileUnreadable as exc:
            return ToolResult(False, "life_profile_unreadable", True, metrics={"error": str(exc)})
        profile = dict(record["profile"])
        live, just_finished, states = [], [], {}
        for item in profile.get("projects") or []:
            state = states[item.get("id")] = store.project_state(item)
            if "off_ladder" not in state:
                live.append(item)
                continue
            # Off the ladder at once; the profile drops it with the next
            # write, and the log keeps it (s7-later-20261029.md §8 D5).
            row = {"id": item.get("id"), "intent": item.get("intent"), "why": state["off_ladder"],
                   "at": state.get("since"), "account": store.project_account(str(item.get("id")))}
            if "last_result" in state:
                row["last_result"] = state["last_result"]
            just_finished.append(row)
        if "projects" in profile:
            profile["projects"] = live
        return ToolResult(True, "life_read", False, metrics={
            "revision": record["revision"], "profile": profile,
            "recent_changes": record["changes"][-5:],
            # The Body's own record of each ladder item: waiting, running,
            # set_aside (ended without completing; back once you change it), done.
            "project_states": {item["id"]: states[item["id"]]["state"] for item in live if item.get("id")},
            "project_results": {item["id"]: store.project_results(item["id"]) for item in live if item.get("id")},
            # Each item's whole account: runs, time, how they ended, rewrites.
            "project_accounts": {item["id"]: store.project_account(item["id"]) for item in live if item.get("id")},
            # Newest first; writing one's id again brings it back with its history.
            "finished_items": [*just_finished, *store.finished_items()][:SHOWN_FINISHED],
        })

    def rejected(reason: str, exc: Exception) -> Any:
        if isinstance(exc, LifeProfileUnreadable):
            return ToolResult(False, "life_profile_unreadable", True, metrics={"error": str(exc)})
        return ToolResult(False, reason, True, metrics={"error": str(exc), "revision": store.read()["revision"]})

    def update(params: dict[str, object]) -> Any:
        changes = params.get("changes")
        try:
            if not isinstance(changes, dict):
                raise LifeProfileError("changes must be an object of profile fields")
            if changes.get("rules") is not None:
                check_rule_calls(_rules(changes["rules"]), registry)
            record = store.update(changes, expected_revision=int(params.get("expected_revision") or 0),
                                  reason=str(params.get("reason") or ""))
        except (LifeProfileError, LifeProfileUnreadable, TypeError, ValueError) as exc:
            return rejected("life_update_rejected", exc)
        return ToolResult(True, "life_updated", False, metrics={"revision": record["revision"],
                                                                 "profile": record["profile"]})

    def is_world_write(tool_name: str) -> bool:
        if tool_name not in registry:
            return False
        tool_sidecar = registry.get(tool_name).sidecar
        return tool_sidecar.can_mutate_body and "blocks" in tool_sidecar.body_scope

    def check_write(project: Mapping[str, Any], profile: Mapping[str, Any]) -> None:
        if "work" not in project:
            return
        unknown = sorted({str(node.get("tool")) for node in _calls(project["work"])} - set(registry.names()))
        if unknown:
            raise LifeProfileError(f"{project['id']}.work uses unregistered tools {unknown}")
        if "area" not in project:
            # Without an area the item writes where delegate_work could.
            return
        area = next(item for item in profile["mine"]["areas"] if item["name"] == project["area"])
        for tool_name, point in project_write_points(project["work"], is_world_write):
            if not inside_area(point, area):
                raise LifeProfileError(
                    f"{project['id']}: {tool_name} writes at {point}, outside your area "
                    f"{area['name']!r} {area['from']}..{area['to']}; a project only changes blocks in its own area")

    def check_ladder(projects: list[dict[str, Any]], written: Mapping[str, Any] | None) -> None:
        names = list((written or {}).get("skills") or [])
        mounted = [name for name, _item in mounted_skill_names(projects)]
        if not names and not any(item.get("skills") for item in projects):
            return
        if skill_check is None:
            if names:
                raise SkillMountError("skill_not_found", "this runtime has no Skill catalog to mount from")
            return
        skill_check(names, mounted)

    def update_project(params: dict[str, object]) -> Any:
        project = params.get("project")
        try:
            if not isinstance(project, dict):
                raise LifeProfileError("project must be an object with at least an id")
            if project.get("rules") is not None:
                check_rule_calls(_rules(project["rules"]), registry)
            record = store.update_project(
                str(params.get("operation") or ""), project,
                expected_revision=int(params.get("expected_revision") or 0),
                reason=str(params.get("reason") or ""), check_write=check_write,
                before=None if params.get("before") is None else str(params["before"]),
                check_ladder=check_ladder)
        except SkillMountError as exc:
            return rejected(exc.code, exc)
        except (LifeProfileError, LifeProfileUnreadable, TypeError, ValueError) as exc:
            return rejected("life_project_rejected", exc)
        metrics: dict[str, Any] = {
            "revision": record["revision"], "projects": record["profile"].get("projects") or []}
        if record.get("left_ladder"):
            # Off the ladder with this write, kept in the log: writing an id again brings it back.
            metrics["left_ladder"] = record["left_ladder"]
        if isinstance(project.get("skills"), list):
            metrics["mounted_skills"] = [name for name, _item in mounted_skill_names(metrics["projects"])]
            metrics["skills_note"] = ("A newly mounted Skill's full text arrives as a SKILL_MOUNTED message at the "
                                      "start of your next turn; one no longer held is announced as SKILL_UNMOUNTED.")
        return ToolResult(True, "life_project_updated", False, metrics=metrics)

    revision = {"type": "integer", "minimum": 0,
                "description": "The revision read_life returned; a stale revision is rejected."}
    reason = {"type": "string", "maxLength": MAX_TEXT_CHARS,
              "description": "One sentence: why your life changes."}
    text = {"type": ["string", "null"], "maxLength": MAX_TEXT_CHARS}
    stance = {"type": ["string", "null"], "maxLength": MAX_STANCE_CHARS}
    sentence = {"type": ["string", "null"], "maxLength": MAX_INTENT_CHARS, "description": _SENTENCE_HINT}
    point = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
    area = {"type": "object", "properties": {"from": point, "to": point}}
    place = {
        "pos": point, "name": text, "dimension": text, "area": area, "note": text,
        "arrival_positions": {"type": "array", "items": point, "maxItems": MAX_HOME_ARRIVALS},
        "storage": {"type": "array", "maxItems": MAX_CONTAINERS, "items": {"type": "object", "properties": {
            "pos": point, "note": text,
            "holds": {"type": "array", "maxItems": MAX_HOLDS, "items": {"type": "string", "maxLength": 64}},
        }}},
    }
    home = {"type": ["object", "null"], "properties": place}
    outposts = {"type": ["array", "null"], "maxItems": MAX_OUTPOSTS, "items": {"type": "object", "properties": place}}
    belongings = {"type": ["array", "null"], "maxItems": MAX_BELONGINGS, "items": {"type": "object", "properties": {
        "owner": text, "pos": point, "area": area, "dimension": text, "said_by": text, "said_at": text, "note": text,
    }}}
    mine = {"type": ["object", "null"], "properties": {
        "areas": {"type": "array", "maxItems": MAX_AREAS, "items": {
            "type": "object", "properties": {
                "name": text, "from": point, "to": point, "dimension": text,
            },
        }},
        "containers": {"type": "array", "items": point, "maxItems": MAX_CONTAINERS},
    }}
    registry.register(RegisteredTool(
        "read_life",
        "Read your own life profile in this world: home, outposts, the areas and containers that are yours, "
        "what players told you is theirs or shared, routine, personality, how you treat each player, the "
        "items on your plan ladder with the last results of each, and the items that finished and left it.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        read, sidecar("read_life", "read_life", "life_query"), projector=shown,
    ))
    registry.register(RegisteredTool(
        "update_life",
        "Write or revise your own life profile. It persists across sessions in this world. Give only the "
        "fields that change; null removes one. Fields: home {pos, name, dimension (overworld, the_nether or "
        "the_end; left out means the overworld), area {from, to}, note, arrival_positions: up to "
        f"{MAX_HOME_ARRIVALS} cells you have stood in at home (the feet cell), such as just inside the door; "
        "going home walks to the first one the Body can stand in; storage: [{pos, holds: [item ids, or "
        "patterns such as '*_log'], note}]: which container at home keeps what, sorted your own way}; "
        f"outposts: up to {MAX_OUTPOSTS} other places you keep, such as a base in the Nether, [{{name, pos, "
        "dimension, area, arrival_positions, storage, note}]; belongings: up to "
        f"{MAX_BELONGINGS} things players told you about, [{{owner: 'shared' or the player's name, pos or area "
        "{from, to}, dimension, said_by: who said it, said_at: when (left out, the time of this write), "
        "note}]: the Body does not open, store into, take from or break a player's things, and uses shared "
        "things but does not break them; mine {areas: [{name, from, to, dimension}], containers: [[x,y,z]]} — "
        "only places you made or claimed, never a player's; the Body may open and use them, and the blocks it "
        "placed itself, also outside the natural region; routine (text); rules [{when, then, call: {tool, arguments}}]: standing rules in your "
        "own words, e.g. when 'the Body is out in the open, not sealed in, and a zombie comes at it', then 'fight "
        "it with the sword', call {tool: 'engage_entity', arguments: {target: 'nearest_hostile'}}. Write the "
        "condition as the situation the rule belongs to, in words the Body's facts use (out in the open, sealed "
        "in, under water, a mob inside the cell, just hurt), not as numbers: the chooser reads words and does "
        "not compare numbers, and a rule is followed wherever its words still match. A rule with a call "
        "is offered to the Body's moment-to-moment chooser as that call, to choose when its condition holds. "
        "Without a call, it is a standing instruction in your words, read with every frame; the chooser "
        "picks among the concrete actions on offer. In arguments, 'nearest_hostile' stands for the nearest hostile "
        "mob (its position for a *_pos argument); a rule whose call the Body cannot make now (no hostile mob, "
        f"an item it does not own) is not offered. At most {MAX_RULES} rules; give the whole list. "
        "stance (text): your reading of the Body's situation now, in English body facts: where it is, what "
        "keeps it safe there or what it is doing, and what would end that, e.g. 'Sealed in a 1x2 cell dug into "
        "rock at [42, 80, 4] for the night; mobs on the surface cannot reach the Body through the rock. This ends "
        "if a block of the cell is broken, a mob is inside with the Body, the Body takes new damage, or it is "
        "day.' Say end conditions that are not already true when you write them. Write where the Body is, as "
        "you have read it, not where you are taking it; a call or ladder item of yours that moves the Body "
        "ends the stance, so write the next one, or null, in that same turn. The "
        "reflex layer reads it first in every frame, with how long ago you wrote it, and may then keep the Body "
        "where it is instead of fleeing or fighting; while it protects the Body it checks every second whether "
        "your reading still holds and tells you when it does not. Write a new one when the situation changes, "
        f"null when it is over; a death ends it. At most {MAX_STANCE_CHARS} characters. "
        "personality (text); relationships [{player, note}]. "
        f"Each profile text field is limited to {MAX_TEXT_CHARS} characters. "
        f"The serialized profile is limited to {MAX_PROFILE_CHARS} characters; "
        f"change reasons retain the first {MAX_TEXT_CHARS} characters. " + _LIFE_TEXT_RULE,
        {"type": "object", "properties": {
            "changes": {"type": "object", "properties": {
                "home": home, "mine": mine, "outposts": outposts, "belongings": belongings,
                "routine": text, "personality": text, "stance": stance,
                "rules": {"type": ["array", "null"], "maxItems": MAX_RULES, "items": {
                    "type": "object", "properties": {
                        "when": text, "then": text,
                        "call": {"type": "object", "properties": {
                            "tool": {"type": "string"}, "arguments": {"type": "object"}}},
                    }}},
                "relationships": {"type": ["array", "null"], "maxItems": MAX_RELATIONSHIPS,
                                  "items": {"type": "object", "properties": {"player": text, "note": text}}},
            }, "additionalProperties": False},
            "expected_revision": revision, "reason": reason,
        }, "required": ["changes", "expected_revision", "reason"], "additionalProperties": False},
        update, sidecar("update_life", "write_life", "life_write"), projector=shown,
    ))
    registry.register(RegisteredTool(
        "update_life_project",
        "Add, change, move or remove one item of your plan ladder: the projects of your life profile, in "
        "priority order. project: {id, intent, why, when, area, work, standing, status: active|paused|done, note, "
        "skills, stance, rules}. "
        "Only id and intent are needed for an idea; when or area make it an item to look into; work makes it an "
        "item the Body can walk. intent is one sentence on what the item is for. why is for you alone, any "
        "language. when is the condition under which the item fits, in English body facts (\"it is daytime and "
        "a stone_pickaxe or better is in the inventory\"); the reflex layer judges it. work is one "
        "minebot-work/0.2 work node, as in delegate_work, and its own intent (and each step's) is written as "
        "facts the Body reports (item ids and counts, held item, position): that is what is checked to see a "
        "step is done. Items with work must have their intent in English. area, if given, names one of your "
        "mine.areas, and every block the item's calls change must lie in it. standing true keeps the item on "
        "the ladder after it completes (a field to harvest again); any other item that completes, and any item you "
        "mark done, leaves the ladder by itself, read_life lists finished items, and writing one's id again "
        "brings it back with its history. An item that completed past a reported step stays until you mark it "
        "done, change it or remove it. Whenever the Body is free, the reflex layer "
        "may pick an active walk item that waits and fits now, and walk its tree from the top. It sees the items in "
        "your order, but when several fit it picks by what they say, not by place: put what must come first "
        "into the others' when. Use "
        "calls that are safe to repeat (farm_area harvests only ripe crops, build_schematic skips placed "
        "blocks). An item that ended without completing is set aside until you change it. before=<id> puts "
        "this item in front of that one (new or moved); without it a new item goes last and a changed one "
        "keeps its place. Upsert merges the given fields into the existing item; a field left out or null keeps "
        "its old value, so to drop an item's work or area, remove the item and write it again. "
        "skills mounts Skills on the item: each one's full text is added to your conversation once and stays in "
        f"effect while the item is on the ladder (at most {MAX_MOUNTED_SKILLS} Skills and "
        f"{MAX_MOUNTED_SKILL_CHARS} characters mounted in all; [] removes them). "
        f"Project intent and when are limited to {MAX_INTENT_CHARS} characters each, why and note to "
        f"{MAX_TEXT_CHARS}; "
        f"id is limited to 48 characters. The serialized profile is limited to {MAX_PROFILE_CHARS} "
        f"characters; change reasons retain the first {MAX_TEXT_CHARS} characters. " + _LIFE_TEXT_RULE,
        {"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["upsert", "remove"]},
            "project": {"type": "object", "properties": {
                "id": {"type": "string", "maxLength": 48},
                "intent": sentence, "when": sentence,
                "note": {**text, "description": _NOTE_HINT}, "why": {**text, "description": _NOTE_HINT},
                "area": {"type": ["string", "null"], "maxLength": MAX_TEXT_CHARS},
                "status": {"type": ["string", "null"], "enum": [*PROJECT_STATUSES, None]},
                "standing": {"type": ["boolean", "null"]},
                "work": {"type": ["object", "null"]},
                "skills": {"type": ["array", "null"], "maxItems": MAX_MOUNTED_SKILLS,
                           "items": {"type": "string", "maxLength": MAX_SKILL_NAME_CHARS},
                           "description": _SKILLS_HINT},
                "stance": {**stance, "description": (
                    "A stance for this item, as for update_life: where the Body will be and what would end it. "
                    "The item's first successful Body call sets it as the stance the reflex layer says, and it "
                    "stays after the item ends. It ends with a later item's stance, a death, "
                    "or your own update_life stance written after it. An item without one leaves the stance as it is.")},
                "rules": {"type": ["array", "null"], "maxItems": MAX_RULES, "description": (
                    "Rules for while this item runs, as for update_life rules. While it runs, only these are "
                    "offered to the hand, in place of your update_life rules; [] means none for that while. "
                    "Leave it out to keep your update_life rules."), "items": {
                    "type": "object", "properties": {
                        "when": text, "then": text,
                        "call": {"type": "object", "properties": {
                            "tool": {"type": "string"}, "arguments": {"type": "object"}}},
                    }}},
            }},
            "before": {"type": ["string", "null"], "maxLength": 48,
                       "description": "Upsert only: the id of the item this one goes in front of."},
            "expected_revision": revision, "reason": reason,
        }, "required": ["operation", "project", "expected_revision", "reason"], "additionalProperties": False},
        update_project, sidecar("update_life_project", "write_life", "life_write"), projector=shown,
    ))


def _calls(work: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(work, Mapping):
        if work.get("kind") == "call":
            yield work
        for value in work.values():
            if isinstance(value, (Mapping, list)):
                yield from _calls(value)
    elif isinstance(work, list):
        for item in work:
            yield from _calls(item)


def _rules(rules: Any) -> list[dict[str, Any]]:
    """A call-rule offers that Body call for when its condition holds.
    Without a call, the chooser reads the Agent's standing instruction in
    every frame and picks among the concrete actions on offer."""
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise LifeProfileError(f"rules must be a list of at most {MAX_RULES}")
    out = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            raise LifeProfileError(f"rules[{index}] must be an object with when, then and call")
        row = {"when": _text(rule.get("when"), f"rules[{index}].when"),
               "then": _text(rule.get("then"), f"rules[{index}].then")}
        call = rule.get("call")
        if call is not None:
            if not isinstance(call, Mapping) or not isinstance(call.get("tool"), str) or not call["tool"].strip():
                raise LifeProfileError(f"rules[{index}].call must be an object with tool and arguments")
            arguments = call.get("arguments", {})
            if not isinstance(arguments, Mapping):
                raise LifeProfileError(f"rules[{index}].call.arguments must be an object")
            row["call"] = {"tool": call["tool"].strip(), "arguments": json.loads(json.dumps(arguments))}
        out.append(row)
    return out


def check_rule_calls(rules: Iterable[Mapping[str, Any]], registry: Any) -> None:
    """Each rule calls a registered Body tool with arguments its schema accepts;
    "nearest_hostile" stands for the nearest hostile mob of the frame."""
    from jsonschema import Draft202012Validator
    from minebot.app.reflex_question import NEAREST_HOSTILE

    for index, rule in enumerate(rules):
        if "call" not in rule:
            continue
        tool = rule["call"]["tool"]
        if tool not in registry or not registry.get(tool).sidecar.can_mutate_body:
            raise LifeProfileError(f"rules[{index}].call.tool {tool!r} is not a Body tool")
        # A position bound from the frame is checked as one.
        bound = {key: [0.0, 0.0, 0.0] if value == NEAREST_HOSTILE and (key == "pos" or key.endswith("_pos")) else value
                 for key, value in rule["call"]["arguments"].items()}
        errors = sorted(Draft202012Validator(registry.get(tool).input_schema).iter_errors(bound), key=str)
        if errors:
            raise LifeProfileError(f"rules[{index}].call.arguments: {errors[0].message}")


def _mine(mine: Any) -> dict[str, Any]:
    if not isinstance(mine, Mapping):
        raise LifeProfileError("mine must be an object with areas and containers")
    out: dict[str, Any] = {}
    areas = mine.get("areas") or []
    if not isinstance(areas, list) or len(areas) > MAX_AREAS:
        raise LifeProfileError(f"mine.areas must be a list of at most {MAX_AREAS}")
    names = set()
    out["areas"] = []
    for index, area in enumerate(areas):
        if not isinstance(area, Mapping):
            raise LifeProfileError(f"mine.areas[{index}] must be an object")
        name = _text(area.get("name"), f"mine.areas[{index}].name")
        if name in names:
            raise LifeProfileError(f"mine.areas has two areas named {name!r}")
        names.add(name)
        row = {"name": name, "from": _point(area.get("from"), f"mine.areas[{index}].from"),
               "to": _point(area.get("to"), f"mine.areas[{index}].to")}
        _dimension(area, row, f"mine.areas[{index}].dimension")
        out["areas"].append(row)
    containers = mine.get("containers") or []
    if not isinstance(containers, list) or len(containers) > MAX_CONTAINERS:
        raise LifeProfileError(f"mine.containers must be a list of at most {MAX_CONTAINERS}")
    out["containers"] = [_point(item, f"mine.containers[{index}]") for index, item in enumerate(containers)]
    return out


def _place(place: Any, name: str, *, outpost: bool = False) -> dict[str, Any]:
    """The home or one outpost: where it is, and which container keeps what."""
    if not isinstance(place, Mapping):
        raise LifeProfileError(f"{name} must be an object with " + ("name, pos and dimension" if outpost else "pos"))
    out: dict[str, Any] = {"pos": _point(place.get("pos"), f"{name}.pos")}
    if outpost:
        out["name"] = _text(place.get("name"), f"{name}.name")
        if place.get("dimension") is None:
            raise LifeProfileError(f"{name}.dimension is needed: overworld, the_nether or the_end")
    else:
        _optional_text(place, out, "name", f"{name}.name")
    _dimension(place, out, f"{name}.dimension")
    if place.get("area") is not None:
        out["area"] = _area(place["area"], f"{name}.area")
    arrivals = place.get("arrival_positions")
    if arrivals is not None:
        if not isinstance(arrivals, list) or len(arrivals) > MAX_HOME_ARRIVALS:
            raise LifeProfileError(f"{name}.arrival_positions must be a list of at most {MAX_HOME_ARRIVALS} positions")
        out["arrival_positions"] = [
            _point(point, f"{name}.arrival_positions[{index}]") for index, point in enumerate(arrivals)]
    if place.get("storage") is not None:
        out["storage"] = _storage(place["storage"], f"{name}.storage")
    _optional_text(place, out, "note", f"{name}.note")
    return out


def _storage(rows: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) > MAX_CONTAINERS:
        raise LifeProfileError(f"{name} must be a list of at most {MAX_CONTAINERS} containers")
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"{name}[{index}]"
        if not isinstance(row, Mapping):
            raise LifeProfileError(f"{where} must be an object with pos and holds")
        entry: dict[str, Any] = {"pos": _point(row.get("pos"), f"{where}.pos")}
        if any(other["pos"] == entry["pos"] for other in out):
            raise LifeProfileError(f"{name} has two rows for {entry['pos']}; put all its items in one row")
        holds = row.get("holds")
        if not isinstance(holds, list) or not holds or len(holds) > MAX_HOLDS:
            raise LifeProfileError(
                f"{where}.holds must be a list of 1 to {MAX_HOLDS} item ids or patterns such as *_log")
        entry["holds"] = []
        for position, item in enumerate(holds):
            pattern = block_name(item) if isinstance(item, str) else ""
            if not _ITEM_PATTERN.fullmatch(pattern):
                raise LifeProfileError(
                    f"{where}.holds[{position}] must be an item id such as cobblestone or a pattern such as *_log")
            entry["holds"].append(pattern)
        _optional_text(row, entry, "note", f"{where}.note")
        out.append(entry)
    return out


def _belonging(item: Any, name: str) -> dict[str, Any]:
    """What a player said is theirs, or that you share: whose, where, and who said so."""
    if not isinstance(item, Mapping):
        raise LifeProfileError(f"{name} must be an object with owner, pos or area, dimension and said_by")
    out: dict[str, Any] = {"owner": _text(item.get("owner"), f"{name}.owner")}
    if (item.get("pos") is None) == (item.get("area") is None):
        raise LifeProfileError(f"{name} needs either pos or area, not both")
    if item.get("pos") is not None:
        out["pos"] = _point(item["pos"], f"{name}.pos")
    else:
        out["area"] = _area(item["area"], f"{name}.area")
    if item.get("dimension") is None:
        raise LifeProfileError(f"{name}.dimension is needed: overworld, the_nether or the_end")
    _dimension(item, out, f"{name}.dimension")
    out["said_by"] = _text(item.get("said_by"), f"{name}.said_by")
    _optional_text(item, out, "said_at", f"{name}.said_at")
    _optional_text(item, out, "note", f"{name}.note")
    return out


def _stamp_belongings(profile: dict[str, Any], before: Mapping[str, Any]) -> None:
    """A belonging written without said_at keeps the time it was first written."""
    def key(item: Mapping[str, Any]) -> str:
        return json.dumps([item.get(name) for name in ("owner", "pos", "area", "dimension", "said_by")])

    known = {key(item): item.get("said_at") for item in before.get("belongings") or []}
    for item in profile.get("belongings") or []:
        if "said_at" not in item:
            item["said_at"] = known.get(key(item)) or _now()


def _area(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeProfileError(f"{name} must be an object with from and to")
    return {"from": _point(value.get("from"), f"{name}.from"), "to": _point(value.get("to"), f"{name}.to")}


def _dimension(source: Mapping[str, Any], target: dict[str, Any], name: str) -> None:
    """``overworld`` is kept as ``minecraft:overworld``; left out stays out (the overworld)."""
    if source.get("dimension") is not None:
        value = dimension_id(_text(source["dimension"], name))
        if not value or not _DIMENSION_ID.fullmatch(value):
            raise LifeProfileError(f"{name} must be overworld, the_nether or the_end")
        target["dimension"] = value


def _points(value: Any) -> Iterable[list[float]]:
    if isinstance(value, list):
        if len(value) == 3 and all(_number(item) for item in value):
            yield [float(item) for item in value]
            return
        for item in value:
            yield from _points(item)
    elif isinstance(value, Mapping):
        if all(_number(value.get(axis)) for axis in ("x", "y", "z")):
            yield [float(value[axis]) for axis in ("x", "y", "z")]
        for item in value.values():
            if isinstance(item, (list, Mapping)):
                yield from _points(item)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _point(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(_number(item) for item in value):
        raise LifeProfileError(f"{name} must be three numbers [x, y, z]")
    return [float(item) if isinstance(item, float) else item for item in value]


def _text(value: Any, name: str, *, limit: int = MAX_TEXT_CHARS, hint: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifeProfileError(f"{name} must be non-empty text")
    if len(value) > limit:
        raise LifeProfileError(f"{name} is {len(value)} characters; the limit is {limit}" + (f". {hint}" if hint else ""))
    return value.strip()


def _stance(value: Any) -> dict[str, str]:
    """A stance the Agent writes is text; it is kept with when it was written,
    so a frame can say its age and a death can end it."""
    if isinstance(value, Mapping):
        at = value.get("at")
        if not isinstance(at, str) or _stance_time(at) is None:
            raise LifeProfileError("stance.at must be a UTC time such as 2026-11-07T12:00:00Z")
        return {"text": _text(value.get("text"), "stance.text", limit=MAX_STANCE_CHARS), "at": at}
    return {"text": _text(value, "stance", limit=MAX_STANCE_CHARS,
                          hint="Say where the Body is, what keeps it there and what would end it, briefly."),
            "at": _now()}


def _item_stance(value: Any, project_id: str) -> dict[str, str]:
    """A stance written on a ladder item: set in frames by the item's first
    successful Body result (ladder-item-consolidation §3)."""
    if isinstance(value, Mapping):
        value = value.get("text")
    return {"text": _text(value, f"{project_id}.stance", limit=MAX_STANCE_CHARS)}


def _stance_time(at: Any) -> datetime | None:
    try:
        return datetime.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def stance_written_at(profile: Mapping[str, Any] | None) -> tuple[str, datetime] | None:
    """The stance text and when it was written, if the profile holds one."""
    stance = (profile or {}).get("stance")
    if not isinstance(stance, Mapping) or not isinstance(stance.get("text"), str) or not stance["text"].strip():
        return None
    at = _stance_time(stance.get("at"))
    return None if at is None else (stance["text"].strip(), at)


def stance_cleared_at(profile: Mapping[str, Any] | None) -> datetime | None:
    """When the Agent last cleared its stance with update_life, if after its last write."""
    return _stance_time((profile or {}).get("stance_cleared_at"))


def _optional_text(source: Mapping[str, Any], target: dict[str, Any], key: str, name: str, *,
                   limit: int = MAX_TEXT_CHARS, hint: str = "") -> None:
    if source.get(key) is not None:
        target[key] = _text(source[key], name, limit=limit, hint=hint)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "LifeProfileError", "LifeProfileUnreadable", "LifeRevisionConflict", "LifeStore", "check_rule_calls", "inside_area", "life_file_path",
    "life_intent", "life_summary", "load_profile", "normalize_profile", "normalize_project", "stance_written_at",
    "MAX_STANCE_CHARS",
    "MAX_MOUNTED_SKILLS", "MAX_MOUNTED_SKILL_CHARS", "SkillMountError", "mounted_skill_names", "project_skills",
    "project_plan", "project_write_points", "register_life_tools",
]
