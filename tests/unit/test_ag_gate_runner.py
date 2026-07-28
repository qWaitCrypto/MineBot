import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tools.run_ag_interactive_gate import (
    ExternalInteractiveScenarioContext,
    FIXTURE_SENDER,
    MATERIAL_GOAL,
    SegmentResult,
    _parse_entity_scalar,
    _parse_entity_vector,
    _production_environment,
    _provider_manifest_summary,
    _quality_gate_passes,
)


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
                provider_manifest_valid=True,
            )
        )

    def test_quality_trace_passes_only_after_active_duration_is_met(self):
        self.assertTrue(
            _quality_gate_passes(
                self.segment,
                {"verdict": "pass"},
                active_duration_met=True,
                provider_manifest_valid=True,
            )
        )

    def test_quality_trace_cannot_pass_with_legacy_or_missing_provider_manifest(self):
        self.assertFalse(
            _quality_gate_passes(
                self.segment,
                {"verdict": "pass"},
                active_duration_met=True,
                provider_manifest_valid=False,
            )
        )

    def test_production_environment_forces_java_and_removes_rcon(self):
        result = _production_environment(
            {
                "MINEBOT_BODY_PROVIDER": "scarpet",
                "MINEBOT_REAL_RCON_HOST": "127.0.0.1",
                "MINEBOT_REAL_RCON_PORT": "25576",
                "MINEBOT_REAL_RCON_PASSWORD": "secret",
                "MINEBOT_REAL_RCON_TIMEOUT": "20",
            }
        )

        self.assertEqual(result["MINEBOT_BODY_PROVIDER"], "java")
        self.assertEqual(result["MINEBOT_JAVA_BODY_URL"], "ws://127.0.0.1:8767")
        self.assertFalse(any("RCON" in key for key in result))

    def test_full_frozen_goal_fits_public_fakeplayer_command(self):
        command = f"execute as {FIXTURE_SENDER} run me /goal {MATERIAL_GOAL}"
        self.assertLessEqual(len(command), 256)

    def test_provider_manifest_requires_every_production_segment_to_be_java_only(self):
        valid = {
            "event": "provider_manifest",
            "body_provider": "java",
            "legacy_rcon_constructed": False,
            "legacy_scarpet_body_constructed": False,
        }
        self.assertTrue(_provider_manifest_summary([valid, dict(valid)])["valid"])
        invalid = dict(valid, legacy_rcon_constructed=True)
        self.assertFalse(_provider_manifest_summary([valid, invalid])["valid"])
        self.assertFalse(_provider_manifest_summary([])["valid"])

    def test_external_fixture_parses_vanilla_entity_data(self):
        self.assertEqual(
            _parse_entity_vector(
                "Bot1 has the following entity data: [12.5d, 64.0d, -3.25d]"
            ),
            (12.5, 64.0, -3.25),
        )
        self.assertEqual(
            _parse_entity_scalar("Bot1 has the following entity data: 17.5f"),
            17.5,
        )

    def test_external_fixture_sends_public_fakeplayer_chat(self):
        class FakeRcon:
            def __init__(self):
                self.commands = []

            def request(self, command):
                self.commands.append(command)
                return "ok"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production_trace = root / "trace.jsonl"
            fixture_trace = root / "fixture.jsonl"
            production_trace.write_text(
                json.dumps(
                    {
                        "seq": 4,
                        "ts": 1.0,
                        "event": "chat_message",
                        "sender": "AGT",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            rcon = FakeRcon()
            context = ExternalInteractiveScenarioContext(
                bot_name="Bot1",
                chat_sender="AGT",
                rcon=rcon,
                production_trace_path=production_trace,
                fixture_trace_path=fixture_trace,
            )
            context._chat_sender_ready = True

            marker = asyncio.run(context.emit_chat("AGT", "/goal collect wood"))

            self.assertEqual(marker, 4)
            self.assertEqual(
                rcon.commands,
                ["execute as AGT run me /goal collect wood"],
            )
            fixture = json.loads(fixture_trace.read_text(encoding="utf-8"))
            self.assertEqual(fixture["event"], "scenario_chat_emitted")

    def test_external_fixture_waits_for_chat_sender_before_spectator_mode(self):
        class FakeRcon:
            def __init__(self):
                self.commands = []

            def request(self, command):
                self.commands.append(command)
                if command == "data get entity AGT Pos":
                    return "AGT has the following entity data: [256.5d, 70.0d, 0.5d]"
                return "ok"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ExternalInteractiveScenarioContext(
                bot_name="Bot1",
                chat_sender="AGT",
                rcon=FakeRcon(),
                production_trace_path=root / "trace.jsonl",
                fixture_trace_path=root / "fixture.jsonl",
            )
            context._require_bot_state = AsyncMock(
                return_value=SimpleNamespace(pos=(0.0, 70.0, 0.0))
            )

            asyncio.run(context._spawn_chat_sender())

            self.assertEqual(
                context._rcon.commands,
                [
                    "player AGT kill",
                    "player AGT spawn at 256 70 0",
                    "data get entity AGT Pos",
                    "gamemode spectator AGT",
                ],
            )


if __name__ == "__main__":
    unittest.main()
