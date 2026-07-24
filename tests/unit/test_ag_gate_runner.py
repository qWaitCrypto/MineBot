import unittest

from tools.run_ag_interactive_gate import SegmentResult, _quality_gate_passes


class QualityGateRunnerTests(unittest.TestCase):
    def setUp(self):
        self.segment = SegmentResult(
            exit_code=0,
            elapsed_s=1801.0,
            active_elapsed_s=1800.0,
            ready_elapsed_s=1.0,
            body_ready=True,
            terminated_at_deadline=False,
        )

    def test_short_clean_quality_trace_cannot_pass_configured_gate(self):
        self.assertFalse(
            _quality_gate_passes(
                self.segment,
                {"verdict": "pass"},
                active_duration_met=False,
            )
        )

    def test_quality_trace_passes_only_after_active_duration_is_met(self):
        self.assertTrue(
            _quality_gate_passes(
                self.segment,
                {"verdict": "pass"},
                active_duration_met=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
