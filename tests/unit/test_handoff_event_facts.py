from minebot.app.session import _missing_result_facts


def test_handoff_projects_observed_missing_counts_without_advice():
    # inventory.py:_choose_craft_plan emits metrics.missing with this shape.
    assert _missing_result_facts({"reason": "craft_plan_not_available", "metrics": {
        "item": "stone_pickaxe", "requested_count": 1,
        "missing": [{"need": 3, "have": 1, "any_of": ["oak_planks"]}],
    }}) == ["needs 3 oak_planks, has 1"]
    # block_work.py:_ensure_required_tool emits this reason and metrics.
    assert _missing_result_facts({"reason": "missing_required_tool", "metrics": {
        "required_tier": "stone", "best_owned": {"item": "wooden_pickaxe", "tier": "wooden"},
    }}) == ["needs a stone_pickaxe or better: missing_required_tool; has wooden_pickaxe"]
    # resource_collection.py:_terminal carries these fields on short collection.
    assert _missing_result_facts({"reason": "target_domain_exhausted", "metrics": {
        "expected_drops": ["oak_log"], "requested_delta": 3, "collected_total": 1,
        "remaining_delta": 2,
    }}) == ["needs 3 oak_log, has 1"]
    # inventory.py:craft_item reports requested_count/crafted_count on partial craft.
    assert _missing_result_facts({"reason": "craft_plan_not_available", "metrics": {
        "item": "torch", "requested_count": 4, "crafted_count": 2,
    }}) == ["needs 4 torch, has 2"]
    # cluster_collection.py:_collect_native reports requested_count/collected/remaining_count.
    assert _missing_result_facts({"reason": "candidate_domain_exhausted", "metrics": {
        "item": "oak_log", "requested_count": 3, "collected": 1, "remaining_count": 2,
    }}) == ["needs 3 oak_log, has 1"]
    # protection.py:pillar_up emits these counts on not_enough_blocks.
    assert _missing_result_facts({"reason": "not_enough_blocks", "metrics": {
        "blocks_requested": 3, "blocks_needed": 3, "blocks_held": 1,
    }}) == ["needs 3 blocks, has 1"]
    assert _missing_result_facts({"reason": "future", "metrics": {"missing_items": ["oak_planks"]}}) == []
