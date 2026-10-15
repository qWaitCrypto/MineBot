# Step 6 ºw^~)Þt interrupt_when and owned-tool fallback

Status: implementation is present in commit 6e2b0c8 (current branch also contains 1d6c5f2). This report records the targeted tests and the real run; it does not claim the real acceptance passed.

## Offline evidence

Command:

    /home/qwait/MineBot/.venv/bin/pytest -q tests/unit/test_body_loop_step6.py tests/unit/test_work_language_runtime.py

Result: 30 passed in 0.81s.

The tests cover plan-root interrupt validation and propagation, handler execution and resume, user cancellation, generation preservation, unknown handler results, equipment candidate filtering, equipment execution before original-call re-entry, deadline handling, and the delegate_work schema/help text.

## Real run

Run directory:

    /home/qwait/MineBot-always-on/logs/reflex-always-on-20261014-step6-real/

Trace:

    logs/reflex-always-on-20261014-step6-real/trace.jsonl

The run used disposable world step6-real-20261014 and ports 25569 (game), 25579 (RCON), and 8771 (IPC). The model/provider configuration was supplied through the private env-file path and no credentials are copied here.

The Agent submitted five plans containing a plan-root interrupt_when. The first was invalid because the Agent put interrupt_when under work; the second and later plans were structurally accepted. In accepted running frames, the compiler emitted the concrete interrupt option; 127 running-frame requests contained it. No request selected interrupt, so no handler start/finish or work_interrupt_result event exists in this run. The accepted plans ended because the chosen path/target reached a terminal failure or completed normally before the injected threat became a plan interrupt. The injected hostile was observed by idle supervision and produced an ordinary move_away reaction in one interval; that is separate from interrupt_when evidence.

The real trace therefore proves option compilation and live plan propagation, but not the required end-to-end selection of an interrupt handler. The equipment fallback was not exercised by an Agent session in this run. Its offline candidate and execution behavior are covered by the 30 passing tests; native broken-tool captures are retained under logs/reflex-always-on-20261014c-step6-native/.

At teardown, the client transport emitted body_interrupt_failed with BrokenPipeError (trace seq 6842) after the fixture processes were stopped. This is teardown evidence, not a runtime acceptance result.

## Verdict

Step 6 is code-complete and unit-tested. Real acceptance remains open for two items: one Decision-selected interrupt that executes its handler and resumes the original work, and one Agent session where a failed tool selects an owned replacement without waking the Agent. The run and trace are intentionally preserved for the next attempt.
