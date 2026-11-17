import json
from types import SimpleNamespace

from minebot.app.model_input_capture import capture


def test_capture_writes_each_call_and_tools_once_per_change(tmp_path, monkeypatch):
    path = tmp_path / "inputs.jsonl"
    tool = SimpleNamespace(name="read_state", description="d", params_json_schema={"type": "object"},
                           strict_json_schema=False)
    agent = SimpleNamespace(name="A", model="m", tools=[tool])
    capture("sys", [{"role": "user", "content": "hi"}], agent)       # off: no env
    assert not path.exists()
    monkeypatch.setenv("MINEBOT_CAPTURE_MODEL_INPUT", str(path))
    capture("sys", [{"role": "user", "content": "hi"}], agent)
    capture("sys2", [{"role": "user", "content": "again"}], agent)
    agent.tools = [tool, SimpleNamespace(name="move_to", description="m", params_json_schema={}, strict_json_schema=True)]
    capture("sys3", [], agent)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["system_prompt"] for row in rows] == ["sys", "sys2", "sys3"]
    assert "tools" in rows[0] and "tools" not in rows[1] and [t["name"] for t in rows[2]["tools"]] == ["read_state", "move_to"]
    assert rows[0]["tools_hash"] == rows[1]["tools_hash"] != rows[2]["tools_hash"]
    assert rows[1]["input"] == [{"role": "user", "content": "again"}]
