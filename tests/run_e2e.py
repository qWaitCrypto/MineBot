#!/usr/bin/env python3
"""Batch runner for the body-layer e2e tests.

Runs each ``tests/e2e_body_*.py`` as its **own process, serially, exclusively**.
This runner does not make the world isolated by itself: each e2e must own its
own bot cleanup, world region cleanup, and setup/reset contract. Treat a batch
failure as triage input until the same test has been rerun in isolation.

The script is pure orchestration: it discovers the tests, runs each with a
timeout, captures stdout+stderr to a per-test log file, classifies the result by
exit code, and prints a red-green matrix.

It touches **no** body/game/Scarpet code and adds **no** world setup. It only
wraps the existing tests so the whole batch runs in one command instead of one
``python3 tests/e2e_body_X.py`` at a time.

Exit-code convention (codex's, see ``tests/e2e_support.py``)::

    0  = pass
    77 = SKIP   (live server unavailable)
    other non-zero = fail
    killed by timeout = TIMEOUT

Runner exit code: 0 if no test failed or timed out; 1 otherwise. SKIP is NOT a
failure unless ``--fail-on-skip`` is given (use that when the live server is up
and you want to catch a test that should have run but silently skipped).

Usage::

    python3 tests/run_e2e.py                       # run all, 180s each
    python3 tests/run_e2e.py --filter navigation    # name substring match
    python3 tests/run_e2e.py --required --fail-on-skip   # hard gate (live up)
    python3 tests/run_e2e.py --list                  # just list discovered

Known long matrix tests get per-file timeout overrides. In particular,
``e2e_body_furnace.py`` contains many live smelting lifecycle cases; a generic
180s/300s timeout is too short for the full matrix.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Tests live next to this script; repo root is one level up.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_PATTERN = "e2e_body_*.py"
DEFAULT_TIMEOUT_S = 180.0
SKIP_EXIT_CODE = 77

TIMEOUT_OVERRIDES_S = {
    "e2e_body_furnace": 600.0,
}

# Classification buckets.
PASS = "PASS"
SKIP = "SKIP"
FAIL = "FAIL"
TIMEOUT = "TIMEOUT"


@dataclass
class TestResult:
    name: str                     # e.g. "e2e_body_mine"
    path: Path
    status: str                   # PASS / SKIP / FAIL / TIMEOUT
    elapsed_s: float
    exit_code: int | None = None  # None when killed by timeout
    log_path: Path | None = None
    note: str = ""                # extra (e.g. the exit code, or killed)


@dataclass
class Summary:
    results: list[TestResult] = field(default_factory=list)

    def by_status(self, status: str) -> list[TestResult]:
        return [r for r in self.results if r.status == status]

    @property
    def elapsed_total_s(self) -> float:
        return sum(r.elapsed_s for r in self.results)


# ---------------------------------------------------------------- discovery --


def discover(pattern: str, filter_str: str | None) -> list[Path]:
    """Return sorted e2e scripts matching ``pattern`` (and ``filter_str``)."""
    paths = sorted(HERE.glob(pattern))
    if not paths:
        return []
    if filter_str:
        paths = [p for p in paths if filter_str in p.stem]
    return paths


# ---------------------------------------------------------------- execution --


def timeout_for(path: Path, default_timeout_s: float) -> float:
    return max(default_timeout_s, TIMEOUT_OVERRIDES_S.get(path.stem, default_timeout_s))


def run_one(path: Path, log_path: Path, timeout_s: float, required: bool) -> TestResult:
    """Run a single e2e in its own process; return its classified result."""
    env = os.environ.copy()
    if required:
        env["MINEBOT_E2E_REQUIRED"] = "1"
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        log_path.write_text(
            f"[runner] TIMEOUT after {timeout_s:.0f}s\n\n"
            f"--- stdout (captured before kill) ---\n{out}\n\n"
            f"--- stderr ---\n{err}\n",
            encoding="utf-8",
        )
        return TestResult(
            name=path.stem, path=path, status=TIMEOUT, elapsed_s=elapsed,
            exit_code=None, log_path=log_path, note=f"killed after {timeout_s:.0f}s",
        )

    elapsed = time.monotonic() - started
    log_path.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode == 0:
        status = PASS
    elif proc.returncode == SKIP_EXIT_CODE:
        status = SKIP
    else:
        status = FAIL
    return TestResult(
        name=path.stem, path=path, status=status, elapsed_s=elapsed,
        exit_code=proc.returncode, log_path=log_path,
        note="" if status == PASS else f"exit {proc.returncode}",
    )


# ---------------------------------------------------------------- reporting --


def _ansi(code: str, text: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def status_label(status: str, color: bool) -> str:
    palette = {PASS: "32", SKIP: "33", FAIL: "31", TIMEOUT: "31"}
    label = status.ljust(7)
    return _ansi(palette[status], label, color)


def print_matrix(summary: Summary, color: bool, verbose: bool) -> None:
    print()
    print("=" * 78)
    for r in summary.results:
        line = f"{status_label(r.status, color)} {r.elapsed_s:6.1f}s  {r.name}"
        if r.note:
            line += f"  ({r.note})"
        if r.log_path and r.status in (FAIL, TIMEOUT):
            line += f"  -> {r.log_path}"
        print(line)
        # On failure (and --verbose), surface the tail so the cause is visible
        # without opening the log file.
        if verbose and r.status in (FAIL, TIMEOUT) and r.log_path and r.log_path.exists():
            tail = r.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
            for ln in tail:
                print(f"        │ {ln}")
    print("=" * 78)
    counts = {s: len(summary.by_status(s)) for s in (PASS, SKIP, FAIL, TIMEOUT)}
    total = len(summary.results)
    line = (
        f"{_ansi('32', 'PASS', color)} {counts[PASS]}   "
        f"{_ansi('33', 'SKIP', color)} {counts[SKIP]}   "
        f"{_ansi('31', 'FAIL', color)} {counts[FAIL]}   "
        f"{_ansi('31', 'TIMEOUT', color)} {counts[TIMEOUT]}"
        f"   ({total} total)   {summary.elapsed_total_s:6.1f}s"
    )
    print(line)
    if summary.results:
        log_root = summary.results[0].log_path.parent if summary.results[0].log_path else None
        if log_root:
            print(f"Logs: {log_root}")


# -------------------------------------------------------------------- main --


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run tests/e2e_body_*.py serially, one process each, "
                    "with a timeout and a red-green matrix.",
    )
    parser.add_argument("--pattern", default=DEFAULT_PATTERN,
                        help=f"glob under tests/ (default: {DEFAULT_PATTERN})")
    parser.add_argument("--filter", default=None,
                        help="only run tests whose name contains this substring")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help=f"per-test timeout in seconds (default: {DEFAULT_TIMEOUT_S:g})")
    parser.add_argument("--log-dir", default=None,
                        help="where to write per-test logs (default: tests/.e2e_runs/<ts>)")
    parser.add_argument("--required", action="store_true",
                        help="set MINEBOT_E2E_REQUIRED=1 so skips become hard failures")
    parser.add_argument("--fail-on-skip", action="store_true",
                        help="treat SKIP as a failure (use when the live server is up)")
    parser.add_argument("--list", action="store_true",
                        help="list discovered tests and exit without running")
    parser.add_argument("--verbose", action="store_true",
                        help="print the tail of the log on each failure")
    parser.add_argument("--no-color", action="store_true",
                        help="disable ANSI colors")
    args = parser.parse_args(argv)

    paths = discover(args.pattern, args.filter)
    if args.list:
        for p in paths:
            print(p.name)
        if not paths:
            print("(no tests matched)")
        return 0
    if not paths:
        print(f"no tests matched pattern={args.pattern!r} filter={args.filter!r}", file=sys.stderr)
        return 1

    color = (not args.no_color) and sys.stdout.isatty()
    log_dir = Path(args.log_dir) if args.log_dir else (
        HERE / ".e2e_runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"running {len(paths)} e2e test(s) serially, {args.timeout:g}s each")
    print(f"logs -> {log_dir}")
    if args.required:
        print("MINEBOT_E2E_REQUIRED=1 (skips will hard-fail)")
    print()

    summary = Summary()
    for idx, path in enumerate(paths, 1):
        log_path = log_dir / f"{path.stem}.log"
        print(f"[{idx}/{len(paths)}] {path.stem} ...", flush=True)
        timeout_s = timeout_for(path, args.timeout)
        result = run_one(path, log_path, timeout_s, args.required)
        summary.results.append(result)
        tail_hint = ""
        if result.status in (FAIL, TIMEOUT):
            tail_hint = f"  -> {result.log_path}"
        timeout_hint = ""
        if timeout_s != args.timeout:
            timeout_hint = f"  [timeout {timeout_s:g}s]"
        print(f"        {status_label(result.status, color)} {result.elapsed_s:5.1f}s"
              + timeout_hint + (f"  ({result.note})" if result.note else "") + tail_hint, flush=True)

    print_matrix(summary, color, args.verbose)

    failed = summary.by_status(FAIL) + summary.by_status(TIMEOUT)
    if args.fail_on_skip:
        failed = failed + summary.by_status(SKIP)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
