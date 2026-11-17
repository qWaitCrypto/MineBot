"""Register random seeds before a series; retain assignments and discards.

The digest is SHA256 of UTF-8 JSON of the ordered integer list, with no spaces
(``json.dumps(seeds, separators=(",", ":"))``). Discarding never frees a seed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_digest(seeds: list[int]) -> str:
    return hashlib.sha256(json.dumps(seeds, separators=(",", ":")).encode()).hexdigest()


def register(n: int, out: Path) -> dict:
    if n < 1:
        raise ValueError("n must be positive")
    seeds = []
    while len(seeds) < n:
        seed = secrets.randbits(64) - (1 << 63)
        if seed not in seeds:
            seeds.append(seed)
    data = {"registered_at": now(), "seeds": seeds, "sha256": seed_digest(seeds),
            "assignments": [], "discards": []}
    with out.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    return data


def update(path: Path, run: Path, *, seed: int | None = None, reason: str | None = None) -> dict:
    # Hold one advisory lock across read/validate/write so simultaneous assigns
    # cannot both claim the same seed. This tool runs on the Linux eval host.
    import fcntl

    with path.open("r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        data = json.load(stream)
        if seed_digest(data["seeds"]) != data["sha256"]:
            raise ValueError("seed list digest mismatch")
        run_name = str(run.resolve())
        assigned = data.setdefault("assignments", [])
        if seed is not None:
            if seed not in data["seeds"]:
                raise ValueError("seed was not registered")
            if any(a["seed"] == seed for a in assigned):
                raise ValueError("seed was already assigned")
            if any(a["run"] == run_name for a in assigned):
                raise ValueError("run was already assigned")
            assigned.append({"seed": seed, "run": run_name, "assigned_at": now()})
        else:
            if not reason or not reason.strip():
                raise ValueError("discard requires a nonempty reason")
            if not any(a["run"] == run_name for a in assigned):
                raise ValueError("run has no seed assignment")
            if any(a["run"] == run_name for a in data["discards"]):
                raise ValueError("run was already discarded")
            data["discards"].append({"run": run_name, "reason": reason.strip(), "ts": now()})
        stream.seek(0)
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.truncate()
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("--n", type=int, required=True)
    reg.add_argument("--out", type=Path, required=True)
    assign = sub.add_parser("assign")
    assign.add_argument("file", type=Path)
    assign.add_argument("--seed", type=int, required=True)
    assign.add_argument("--run", type=Path, required=True)
    discard = sub.add_parser("discard")
    discard.add_argument("file", type=Path)
    discard.add_argument("--run", type=Path, required=True)
    discard.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        if args.command == "register":
            register(args.n, args.out)
        else:
            update(args.file, args.run, seed=getattr(args, "seed", None), reason=getattr(args, "reason", None))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
