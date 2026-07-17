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
| Body / Scarpet | `minebot/body/`, `minebot/game/`, `minecraft/server/scarpet/` | transactions, controllers, navigation, governance, server facts |
| Eval / Failure Worlds | `tests/`, fixtures, traces | reproducible false success, stalls, recovery failures, safety boundaries |
| Observability | `minebot/camera/`, trace and event surfaces | readable model -> Body -> server evidence |
| Developer Experience | setup, CI, public docs | clean-clone behavior, compatibility, configuration, focused tests |

The Fabric/Java bridge is an optional transport path, not the current game-logic
main line. Proposals there should begin with evidence that RCON payload, latency,
or push limitations require a transport change.

## First Five Minutes

The contributor truth gate needs no Minecraft server, Java, RCON endpoint, LLM
key, or external world:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -p 'test_contributor_truth_gate.py' -v
```

Expected result:

```text
Ran 3 tests

OK
```

The gate proves three narrow rules:

- a terminal server event with completion evidence can produce success;
- command acceptance or a model claim alone cannot produce success;
- a terminal failure remains retryable failure.

It does not prove the full Agent, Minecraft Body, or every unit test. Keep that
distinction explicit in issues and pull requests.

## Real Minecraft Body Work

The Body path currently requires a manually prepared disposable Minecraft
26.1.2 Fabric server with Carpet, the MineBot Scarpet app, and local RCON. The
repository does not yet provide a newcomer-ready server bootstrap or a verified
30-minute Body lab.

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
- Keep Java/WebSocket transport optional unless runtime evidence justifies it.

Large changes to Memory, high-level capabilities, multi-bot behavior, mutation
permissions, public-server safety, or transport start as a
[design proposal](https://github.com/qWaitCrypto/MineBot/issues/new?template=design.yml),
not a large implementation PR.

## Report A Failure

A failure case is a complete contribution even when it has no fix. Use the
[failure-case form](https://github.com/qWaitCrypto/MineBot/issues/new?template=failure_case.yml)
and include:

- MineBot, Minecraft, Fabric, Carpet, and Scarpet versions;
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
- authoritative positive evidence;
- a negative or regression case;
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
