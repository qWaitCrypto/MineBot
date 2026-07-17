# MineBot Support Status

MineBot is a developer preview. Support claims are intentionally narrower than
the project's long-term architecture.

## Platform Matrix

| Surface | Status | Evidence / limitation |
|---|---|---|
| Python contributor truth gate on Ubuntu Linux | Configured; hosted run pending | GitHub Actions workflow targets Python 3.13 and the isolated gate |
| WSL2 Linux | Maintainer development environment | Uses the Linux path; not a separate native-Windows target |
| Native Windows | Not currently claimed | Support begins only after a Windows CI contract passes; cross-platform queue locking alone is not sufficient |
| macOS | Not currently claimed | No CI or clean-clone proof yet |
| Full unit suite | Development signal | Broader than the newcomer gate and may expose active implementation work |
| Local Minecraft Body | Advanced/manual | Requires a disposable 26.1.2 Fabric + Carpet + Scarpet + RCON server |
| Newcomer Body lab | Not ready | No distributable server bootstrap or clean-world fixture contract yet |
| Fabric/Java bridge | Optional/experimental | Not required for the RCON-first Body path |

The supported contributor command is documented in
[CONTRIBUTING.md](CONTRIBUTING.md). Passing it proves only the shared terminal
truth contract, not full real-world autonomy.

## Where To Ask

- Use a bug report for a bounded code or setup defect.
- Use a failure case for a reproducible world-state mismatch, false success,
  stall, recovery failure, or governance boundary.
- Use a design proposal before changing architecture, safety, permissions,
  Memory, multi-bot behavior, or transport.

Issues are not a private support channel. Redact model keys, RCON passwords,
server addresses that are not already public, player data, and private world
content.

## Maintainer Triage

Maintainers aim to acknowledge actionable reports within seven calendar days
during developer preview. This target covers initial classification and next
steps, not a fix deadline or support SLA.
