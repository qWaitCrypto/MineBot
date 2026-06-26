# MineBot

Ender Dragon, I will kill you.

First we teach the body to walk, work, fight, and survive.

Then we settle the bill in the End.

## Real Model E2E

Run the local Carpet/RCON test server first, then provide a provider config via
environment variables:

```bash
export MINEBOT_LLM_MODEL=<model>
export MINEBOT_LLM_API_KEY=<key>
# optional for OpenAI-compatible endpoints:
export MINEBOT_LLM_BASE_URL=<https://.../v1>
python3 tests/e2e_agent_real_model_collect.py
```

Without the key/model env vars, the test exits with SKIP 77.

For the actual playable loop, use the console entrypoint:

```bash
python3 -m minebot.app.console
```

Then type a natural-language goal, for example `collect 3 dirt`. MineBot injects
that as the active goal, lets the real model choose tools, switches runtime
profile through the state machine, and drives the local server body.
