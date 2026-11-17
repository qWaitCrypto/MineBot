"""Explicitly rebind one bot's history after confirming two ids name one save."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from filelock import FileLock, Timeout

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minebot.app.life_profile import rebind_life_files
from minebot.app.runtime_state import RuntimeScope, RuntimeStateError, RuntimeStateStore


def rebind(args: argparse.Namespace) -> dict:
    db = Path(args.db).expanduser().resolve(strict=True)
    if not db.is_file():
        raise ValueError(f"database must be an existing file: {db}")
    old = RuntimeScope(args.server_id, args.from_world, args.bot)
    new = RuntimeScope(args.server_id, args.to_world, args.bot)
    # The scheduler holds this lock while a session runs and leaves the file
    # behind when it stops, so whether it can be taken says whether one runs.
    scheduler_lock = FileLock(db.with_name(f"{db.name}.{old.key}.scheduler.lock"))

    def check_scheduler() -> None:
        if scheduler_lock.is_locked:
            return
        try:
            scheduler_lock.acquire(timeout=0)
        except Timeout as exc:
            raise RuntimeStateError(
                f"session is running; scheduler lock is held: {scheduler_lock.lock_file}") from exc

    def hold_exclusive(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        try:
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()
        except sqlite3.OperationalError as exc:
            raise RuntimeStateError(f"session may be running; cannot take exclusive database lock: {exc}") from exc

    if args.confirm:
        check_scheduler()
        # Refuse an active database before the store can initialize/migrate it.
        # The store connection takes and retains the lock again below.
        with closing(sqlite3.connect(db, timeout=0)) as probe:
            hold_exclusive(probe)
    life_plan = rebind_life_files(Path(args.life_dir), old, new, dry_run=True)
    with closing(RuntimeStateStore(db)) as store:
        if args.confirm:
            # Retain the exclusive connection lock across backup, database move,
            # and file renames. Rollback ends the probe transaction, permitting
            # backup() on this same connection; EXCLUSIVE locking_mode retains
            # the lock until close (including in WAL mode).
            connection = store._connection
            hold_exclusive(connection)
            check_scheduler()
        plan = store.rebind_scope(old, new, dry_run=True)
        plan["life_files"] = life_plan
        if not args.confirm:
            return plan

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = db.with_name(f"{db.name}.before-rebind-{stamp}")
        # Reserve the name without overwriting any previous backup.
        with backup.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(backup)) as target:
                connection.backup(target)
        except Exception:
            backup.unlink()
            raise
        try:
            result = store.rebind_scope(old, new, dry_run=False)
            result["life_files"] = rebind_life_files(Path(args.life_dir), old, new, dry_run=False)
        except Exception as exc:
            raise RuntimeStateError(f"rebind failed: {exc}; pre-rebind database backup: {backup}") from exc
        finally:
            if scheduler_lock.is_locked:
                scheduler_lock.release()
        result["backup"] = str(backup)
        result["exclusive_lock"] = "held until database and life file moves finish"
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--life-dir", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--from-world", required=True)
    parser.add_argument("--to-world", required=True)
    parser.add_argument("--confirm", action="store_true",
                        help="confirm that both world ids refer to the same save and perform the move")
    args = parser.parse_args(argv)
    try:
        result = rebind(args)
    except (OSError, ValueError, RuntimeStateError, sqlite3.Error) as exc:
        print(f"rebind refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
