# Contributing to MineBot

MineBot accepts contributions that make the agent more reliable. Code is one
way to contribute; reproducible failures, evaluation scenes, traces, platform
fixes, and documentation are equally useful.

The standard is simple: a model saying "done" is not proof. Behavior changes
must be checked against authoritative world, inventory, or server-event facts.

MineBot is currently a developer preview. Read [SUPPORT.md](SUPPORT.md) before
setting up an environment or promising support for a new platform.

## Find Your Area

| Area | Start here | Typical contributions |
|---|---|---|
| Agent Runtime | `minebot/app/`, `minebot/brain/`, `minebot/contract/` | lifecycle, task truth, persistence, progress, Memory/Skills |
| Body / Real Client | `minebot/body/`, `minebot/game/`, `minecraft/client/body/` | Baritone adaptation, native client actions, real results |
| Eval / Failure Worlds | `tests/`, fixtures, traces | reproducible false success, stalls, recovery failures, safety boundaries |
| Observability | `minebot/camera/`, trace and event surfaces | readable model -> Body -> server evidence |
| Developer Experience | setup, CI, public docs | clean-clone behavior, compatibility, configuration, focused tests |

The current route is a real Minecraft client with upstream Baritone. Keep the
usable Java FakePlayer backend; freeze non-essential self-built navigation
expansion. Read [Issue #1](https://github.com/qWaitCrypto/MineBot/issues/1) for
the full product and engineering principles.

## First Five Minutes

The default behavioral regression needs no Minecraft server, Java runtime or
model key:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

Expected result:

```text
69 passed
```

This checks current interfaces, cancellation, chat preemption and truthful
results using doubles. It does not prove a Minecraft task. Use the focused
regressions and real task commands in [TESTING.md](TESTING.md) for that evidence.

## Real Minecraft Body Work

Prepare a disposable server and the selected compatible real-client tuple.
Run one task relevant to the change and verify the resulting world/inventory
state. A fixed 30-minute scenario is not a universal acceptance requirement.
RCON may prepare and inspect experimental fixtures; the production Agent uses
the abstract Body adapter.

Do not run mutation tests against a shared or valuable world. Do not send real
RCON credentials, model keys, private world data, or player information in an
issue. Contributors who already have a safe test environment may submit a
[failure case](https://github.com/qWaitCrypto/MineBot/issues/new?template=failure_case.yml);
the setup must be reproducible without assuming access to a maintainer's local
`test-server` directory.

## Architecture Boundaries

- The Brain decides what; the Body resolves how.
- `minebot/brain/` must not import `minebot/game/`.
- Single-objective physical transactions belong in `minebot/body/`; cross-goal
  composition belongs in the agent layer.
- Server/world/inventory facts outrank model text and command acceptance.
- Never broaden block mutation merely to make a scenario pass. Player structure
  risk and protected claims must remain governed before every mutation.
- Keep backend details behind the abstract Body boundary.

Large changes to Memory, high-level capabilities, multi-bot behavior, mutation
permissions, public-server safety, or transport start as a
[design proposal](https://github.com/qWaitCrypto/MineBot/issues/new?template=design.yml),
not a large implementation PR.

## Report A Failure

A failure case is a complete contribution even when it has no fix. Use the
[failure-case form](https://github.com/qWaitCrypto/MineBot/issues/new?template=failure_case.yml)
and include:

- MineBot, Minecraft, Fabric, Carpet, and Java Body build versions;
- seed, dimension, coordinates, and minimal setup;
- initial authoritative state;
- goal or Body action;
- expected and observed results;
- final authoritative state;
- a minimal replay command or script;
- redacted trace, events, or screenshots;
- any player-build, shared-world, or safety implications.

Prefer seeds, setup commands, small fixtures, or structure descriptions over a
whole world archive.

## Pull Requests

Keep the change bounded. The pull request template asks for:

- the problem and explicit non-goals;
- architecture and safety impact;
- actual task results for behavior changes, including specific failures;
- a targeted regression for a known failure or necessary boundary;
- exact verification commands;
- anything still unproved.

Do not mix unrelated cleanup into a behavioral fix. Never include secrets,
runtime logs with credentials, local worlds, caches, or generated media unless
the issue explicitly requires a small sanitized artifact.

## Review

During developer preview, maintainers aim to acknowledge an actionable issue or
pull request within seven calendar days. This is a triage target, not a promise
of resolution. The first response should state what was understood, what evidence
is still needed, and the current review state.

If a contribution cannot be merged, review should explain why, what part remains
useful, and what smaller boundary would be acceptable.
