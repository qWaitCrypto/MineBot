from pathlib import Path


SCARPET_SOURCE = Path("minecraft/server/scarpet/minebot.sc").read_text()


def test_scarpet_keeps_terminal_cache_separate_from_dispatch_cache():
    assert "global_action_results = {};" in SCARPET_SOURCE
    assert "global_action_terminals = {};" in SCARPET_SOURCE
    assert "global_action_seen = {};" in SCARPET_SOURCE
    assert "remember_action_terminal(name, kind, data);" in SCARPET_SOURCE


def test_scarpet_exposes_read_only_action_status_and_resets_both_caches():
    assert "minebot_action_status(name, action_id) -> (" in SCARPET_SOURCE
    assert '"epoch":%s,"dispatch":%s,"terminal":%s' in SCARPET_SOURCE
    reset_start = SCARPET_SOURCE.index("minebot_reset() -> (")
    reset_end = SCARPET_SOURCE.index("minebot_spawn(name, payload)", reset_start)
    reset_body = SCARPET_SOURCE[reset_start:reset_end]
    assert "global_action_results = {};" in reset_body
    assert "global_action_terminals = {};" in reset_body
    assert "global_action_seen = {};" in reset_body
    assert "global_event_epoch = null;" in reset_body


def test_scarpet_status_bounds_large_terminal_payloads_instead_of_truncating():
    assert "if(length(payload) > 1800," in SCARPET_SOURCE
    assert "terminal_payload_summary_json(name, kind, terminal:3, length(payload))" in SCARPET_SOURCE
    assert '"terminal_payload_complete":false' in SCARPET_SOURCE
    assert "action_terminal_status_json(name, action_id)" in SCARPET_SOURCE


def test_scarpet_status_exposes_bounded_receive_verdict_for_safe_same_id_replay():
    assert "remember_action_seen(name, action_id);" in SCARPET_SOURCE
    assert '"dispatch_state":"%s"' in SCARPET_SOURCE
    assert '"retention_complete":%s' in SCARPET_SOURCE
    assert '"epoch":%s' in SCARPET_SOURCE
