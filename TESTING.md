# Testing MineBot

Testing is split by the evidence it can produce. The default command is a
small contract smoke test. Real Minecraft tasks are explicit experiments, and
their result is the acceptance record.

## Fast default

```bash
python3 -m pytest
```

This runs the six `tests/quick/` checks for the current real-client Body seam.
They cover authoritative state mapping, one navigation terminal, cancellation,
one structured action, the Agent registry entry point, and a concrete refusal.
They use an IPC double and finish in a few seconds on the development
environment. They do not claim that a Minecraft task works.

## Focused Python regression

Run the affected contract tests when changing the adapter or wire protocol:

```bash
python3 -m pytest \
  tests/unit/test_real_client_body.py \
  tests/unit/test_real_client_ipc.py \
  tests/unit/test_real_client_protocol.py -q
```

The full `tests/unit` tree remains available for a deliberate broad regression,
but it is not the default feedback loop. Root-level `*_test.py` files are a
legacy mixed set and are also opt-in.

## Real task experiments

Run one disposable-world task at a time. The script prints each terminal,
asserts the authoritative server result, and writes its evidence JSON under
`logs/`:

```bash
python3 tests/e2e_real_client_body_transactions.py \
  --disposable-local-server --case menus
```

Available cases are `vertical`, `vertical_denied`, `vertical_tools`, `handoff`,
`handoff_tools`, `ranged`, `menus`, and `combat_tools`. Start the selected
compatible client
and disposable server first. A missing client, timeout, or assertion is a
failed experiment; record the printed exception and fixture state as the
reason. Do not convert it to a skip or a generic "not guaranteed" result.

The older Java/FakePlayer task scripts remain useful historical evidence. Run
one by name through the serial runner:

```bash
python3 tests/run_e2e.py --filter furnace --required --fail-on-skip
```

The runner refuses an unqualified full live matrix. Use `--all` only when the
whole legacy matrix is intentionally being measured.

## What counts as success

An interface test proves only the Python-to-Body contract. A task experiment
passes only when the intended Minecraft state changed and the terminal agrees
with that state. A failed experiment records the concrete reason, such as a
missing target, unsupported tuple, cancellation timeout, or server-verified
inventory mismatch. The record is the result; test count and framework
coverage are not substitutes for it.
