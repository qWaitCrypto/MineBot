import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MINEBOT_SC = ROOT / "test-server" / "world" / "scripts" / "minebot.sc"
ASSET_MINEBOT_SC = ROOT / "assets" / "carpet" / "scripts" / "minebot.sc"


class ScarpetSourceTests(unittest.TestCase):
    def test_test_server_and_deployable_minebot_scripts_stay_in_sync(self):
        self.assertEqual(MINEBOT_SC.read_text(), ASSET_MINEBOT_SC.read_text())

    def test_minebot_action_uses_decode_json_not_regex_payload_parser(self):
        source = MINEBOT_SC.read_text()
        match = re.search(r"minebot_action\(name, payload\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(match, "minebot_action function not found")
        body = match.group(1)

        self.assertIn("decode_json(payload)", body)
        self.assertNotIn("replace(payload", body)
        self.assertIn("action:'id'", body)
        self.assertIn("params:'target'", body)

    def test_minebot_action_dispatches_initial_canonical_actions(self):
        source = MINEBOT_SC.read_text()
        match = re.search(r"minebot_action\(name, payload\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(match, "minebot_action function not found")
        body = match.group(1)

        for action_name in ("moveTo", "lookAt", "jump", "selectSlot", "selectItem", "stop", "useItem", "rangedAttack", "attackEntity", "dropItem", "handoffItem", "moveItem", "craftItem", "furnaceTransfer", "containerTransfer", "mineBlock", "placeBlock"):
            self.assertIn(f"action_name == '{action_name}'", body)

        self.assertIn("run_look_at", body)
        self.assertIn("run_jump_once", body)
        self.assertIn("run_select_slot", body)
        self.assertIn("run_select_item", body)
        self.assertIn("run_stop_action", body)
        self.assertIn("start_use_item", body)
        self.assertIn("start_ranged_attack", body)
        self.assertIn("start_attack_entity", body)
        self.assertIn("start_drop_item", body)
        self.assertIn("run_handoff_item", body)
        self.assertIn("run_move_item", body)
        self.assertIn("run_craft_item", body)
        self.assertIn("run_furnace_transfer", body)
        self.assertIn("run_container_transfer", body)
        self.assertIn("start_mine_block", body)
        self.assertIn("start_place_block", body)

    def test_instant_actions_emit_terminal_events_with_action_ids(self):
        source = MINEBOT_SC.read_text()

        for event_name in ("lookDone", "jumpDone", "selectSlotDone", "selectItemDone", "useDone", "rangedDone", "attackDone", "dropDone", "handoffDone", "moveItemDone", "craftDone", "furnaceDone", "containerDone", "stopDone"):
            self.assertIn(event_name, source)

        self.assertRegex(source, r"emit\('lookDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('jumpDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('selectSlotDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('selectItemDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('useDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('rangedDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('attackDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('dropDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('handoffDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('moveItemDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('craftDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('furnaceDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('containerDone', name, l\(action_id,")
        self.assertRegex(source, r"emit\('stopDone', name, l\(action_id,")

    def test_minebot_perceive_exposes_first_authoritative_block_scopes(self):
        source = MINEBOT_SC.read_text()
        self.assertIn("minebot_perceive(name, scope, payload)", source)
        self.assertIn("scope == 'blockAt'", source)
        self.assertIn("scope == 'nearbyBlocks'", source)
        self.assertIn("scope == 'findBlocks'", source)
        self.assertIn("scope == 'nearbyEntities'", source)
        self.assertIn("scope == 'inventory'", source)
        self.assertIn("scope == 'container'", source)
        self.assertIn("block_fact_json", source)
        self.assertIn("perceive_find_blocks", source)
        self.assertIn("perceive_nearby_entities", source)
        self.assertIn("perceive_inventory", source)
        self.assertIn("perceive_container", source)
        self.assertIn("limit_exceeded", source)
        self.assertIn("block_properties_json", source)
        self.assertIn("block_properties_json(b)", source)
        self.assertIn("block_state(b)", source)
        self.assertIn("state:pname", source)
        self.assertIn('"properties":%s', source)
        self.assertIn("perception_json(name, 'nearbyBlocks', true, !overflow", source)
        self.assertIn("perception_json(name, 'findBlocks', true, !overflow", source)
        self.assertIn("perception_json(name, 'nearbyEntities', true, !overflow", source)
        self.assertIn("perception_json(name, 'inventory', true, complete", source)
        self.assertIn("perception_json(name, 'container', true, complete", source)

    def test_state_json_escapes_inventory_raw_and_emits_inventory_hash(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("json_string(v)", source)
        self.assertIn("encode_json(str('%s', v))", source)
        self.assertIn('"inventory_raw":%s', source)
        self.assertIn('"inventory_hash":%s', source)
        self.assertIn('"oxygen":%s', source)
        self.assertIn('"sleeping":%s', source)
        self.assertIn("json_string(raw)", source)
        self.assertIn("json_string(hash_code(raw))", source)
        self.assertIn("nbt:'Air'", source)
        self.assertIn("json_int_null(air)", source)
        self.assertIn("nbt:'SleepTimer'", source)
        self.assertIn("day_time()", source)

    def test_tick_bot_and_bot_pos_guard_missing_body(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("bot_pos(name) -> (", source)
        self.assertIn("if(pe == null, null, query(pe, 'pos'))", source)
        self.assertIn("if(p == null,", source)
        self.assertIn("'missing_body'", source)
        self.assertIn("release_owner(name, 'moveTo')", source)

    def test_list_perceptions_have_char_budget_guard_against_rcon_truncation(self):
        # RCON silently truncates at 4096 chars and a truncated list response can
        # desync the stream. The three list-style perceptions must cap their `out`
        # string at a char budget (well under 4096) and fall to the existing
        # `limit_exceeded` overflow path. Pinned in source; the actual <4096
        # behavior is validated live by the collect-64 rerun.
        source = MINEBOT_SC.read_text()

        self.assertIn("global_response_char_budget = 2000;", source)
        # Referenced once in each of nearbyBlocks / findBlocks / nearbyEntities.
        self.assertEqual(source.count("global_response_char_budget"), 4)  # 1 decl + 3 uses
        self.assertIn("length(out) >= global_response_char_budget", source)

    def test_find_blocks_scope_is_bounded_and_type_matched(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "block_type_matches",
            "wanted = if(params:'type' == null",
            "if(radius > 16, radius = 16)",
            "if(limit > 128, limit = 128)",
            "found = l();",
            "entry = l(dist2, x, y, z, bs, block_kind(bs));",
            "put(found:insert_at, entry, 'insert');",
            "delete(found:limit);",
            '"dist2":%.3f',
            '"blocks":[%s]',
        ):
            self.assertIn(expected, source)

    def test_navigate_to_immediate_terminals_are_accepted_for_event_truth(self):
        source = MINEBOT_SC.read_text()
        match = re.search(r"start_navigate_to\(name, action_id, gx, gy, gz, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(match, "start_navigate_to function not found")
        body = match.group(1)

        self.assertIsNotNone(
            re.search(
                r"if\(p == null,\s*"
                r"emit\('navigateDone'.*?'missing_body'.*?\);\s*true",
                body,
                re.S,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"emit\('navigateDone'.*?plan_status.*?\);\s*"
                r"if\(plan_status == 'no_path'.*?\);\s*true",
                body,
                re.S,
            )
        )
        self.assertIsNotNone(
            re.search(
                r"emit\('navigateDone'.*?'move_start_failed'.*?\);\s*"
                r"global_navigations:name = null;\s*true",
                body,
                re.S,
            )
        )

    def test_nearby_entities_scope_is_bounded_and_reports_pos_health(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "entity_selector(selector)",
            "@e[x=%d,y=%d,z=%d,distance=..%d,limit=%d,sort=nearest]",
            "if(radius > 32, radius = 32)",
            "if(limit > 128, limit = 128)",
            "entity_fact_json",
            "query(e, 'uuid')",
            '"id":%s',
            '"name":%s',
            "query(e, 'pos')",
            "query(e, 'name')",
            "entity_health",
            '"entities":[%s]',
            '"health":%s',
        ):
            self.assertIn(expected, source)

    def test_item_pickup_event_is_emitted_in_production_app(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "__on_player_picks_up_item(player, item_tuple)",
            "emit_watched('itemPickup'",
            "if(kind == 'itemPickup'",
            '"player":"%s"',
            '"item":%s',
            '"count":%d',
            '"stack":%s',
            "stack_item(data:1)",
            "stack_count(data:1)",
            "stack_json(data:1)",
        ):
            self.assertIn(expected, source)

    def test_reset_does_not_rewind_event_cursors(self):
        source = MINEBOT_SC.read_text()
        match = re.search(r"minebot_reset\(\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(match, "minebot_reset function not found")
        body = match.group(1)

        self.assertIn("global_events = [];", body)
        self.assertIn("global_agent_chat_events = [];", body)
        self.assertNotIn("global_seq = 0;", body)
        self.assertNotIn("global_agent_chat_seq = 0;", body)

    def test_death_and_respawn_events_are_emitted_in_production_app(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "__on_player_dies(player)",
            "emit('death', name",
            'if(kind == \'death\'',
            '"inventory_before":%s',
            '"inventory_hash":%s',
            "minebot_spawn(name, payload)",
            "decode_json(payload)",
            "global_respawn_notices",
            "emit('respawned', rname",
            'if(kind == \'respawned\'',
            '"final_pos":%s',
            "params:'emit_respawned'",
            "global_missing_notices",
            "emit('bodyMissing', name",
            'if(kind == \'bodyMissing\'',
            '"lastPos":%s',
        ):
            self.assertIn(expected, source)

    def test_spawn_supports_positioned_payload_fields(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "spawn_cmd = 'player ' + name + ' spawn'",
            "params:'pos'",
            "spawn_cmd += str(' at %d %d %d'",
            "params:'yaw'",
            "params:'pitch'",
            "spawn_cmd += str(' facing %.3f %.3f'",
            "params:'dimension'",
            "params:'gamemode'",
            "spawn_cmd += ' in ' + params:'dimension'",
            "spawn_cmd += ' in ' + params:'gamemode'",
        ):
            self.assertIn(expected, source)

    def test_inventory_scope_is_paged_and_structured(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "perceive_inventory",
            "inventory_slot_json",
            "inventory_slot_type",
            "inventory_slot_label",
            "stack_components_raw",
            "inventory_get(name, slot)",
            "if(limit > 46, limit = 46)",
            '"slotType":"%s"',
            '"slotLabel":"%s"',
            '"stackRaw":%s',
            '"nextStart":%s',
            '"totalSlots":%d',
            '"slots":[%s]',
            '"empty":false',
            "page_limit",
        ):
            self.assertIn(expected, source)

    def test_container_scope_is_paged_and_structured(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "perceive_container",
            "pos = params:'pos'",
            "inventory_get(cpos, slot)",
            "inventory_slot_json(slot, stack)",
            "if(total_slots > 54, total_slots = 54)",
            "if(limit > 27, limit = 27)",
            '"pos":%s',
            '"nextStart":%s',
            '"totalSlots":%d',
            '"slots":[%s]',
            "page_limit",
            "perception_json(name, 'container', true, complete",
        ):
            self.assertIn(expected, source)

    def test_perception_scopes_report_missing_body_instead_of_crashing(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "missing_body_perception(name, scope)",
            "missing_body_perception(name, 'nearbyBlocks')",
            "missing_body_perception(name, 'findBlocks')",
            "missing_body_perception(name, 'nearbyEntities')",
            "missing_body_perception(name, 'inventory')",
            "missing_body_perception(name, 'container')",
            "'missing_body'",
            '[{"reason":"missing_body"}]',
            "if(player_entity(name) == null,",
            "if(p == null,",
        ):
            self.assertIn(expected, source)

    def test_select_item_uses_inventory_truth_and_can_stage_to_hotbar(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "run_select_item",
            "find_hotbar_item",
            "find_inventory_item",
            "find_empty_hotbar_slot",
            "item_matches",
            "inventory_get(name, slot)",
            "inventory_set(name, hotbar_slot",
            "inventory_set(name, inv_found:0, 0)",
            "player %s hotbar %d",
            "not_in_inventory",
            "hotbar_full",
            "moved_to_hotbar",
            "selectItemDone",
        ):
            self.assertIn(expected, source)

    def test_use_item_controller_uses_physical_use_and_reports_inventory_delta_facts(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_uses",
            "start_use_item",
            "run_use_tick",
            "finish_use",
            "inventory_snapshot_hash",
            "player ' + name + ' use once",
            "player ' + name + ' use continuous",
            "inventory_before",
            "inventory_after",
            '"inventory_before":%s',
            "json_string(data:6)",
            "json_string(data:7)",
            "final_reason = 'no_effect'",
            "cancel_use_preempted",
            "global_uses:name != null",
        ):
            self.assertIn(expected, source)
        use_tick = source[source.index("run_use_tick(name, u) -> (") : source.index("run_attack_tick(name, a) -> (")]
        self.assertIn("after = inventory_snapshot_hash(name)", use_tick)
        using_phase = use_tick[use_tick.index("ticks = u:3 + 1") :]
        self.assertNotIn("use continuous", using_phase)

    def test_attack_entity_controller_tracks_target_and_emits_combat_facts(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_attacks",
            "start_attack_entity",
            "run_attack_tick",
            "finish_attack",
            "target_entity_near",
            "target_entity_named_near",
            "target_entity_uuid_near",
            "entity_matches_type",
            "@e[x=%d,y=%d,z=%d,distance=..%d,limit=32,sort=nearest]",
            "attack_range",
            "dist > a:7",
            "move forward",
            "stop_body(name);",
            "player ' + name + ' attack once",
            "target_health",
            "target_initial_health",
            "target_id",
            "target_name",
            "damage_observed",
            "persistent_target",
            "attacks",
            "cooldown_ticks",
            "min_attack_interval_ticks",
            "max_attack_interval_ticks",
            "player_target_requires_name",
            "self_target_disallowed",
            "cancel_attack_preempted",
            "global_attacks:name != null",
        ):
            self.assertIn(expected, source)

    def test_ranged_attack_controller_tracks_fired_observation_and_miss_unknown_split(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            '"fired_observed":%s',
            "arrow_near_bot(name, 8)",
            "final_reason = 'target_destroyed'",
            "final_reason = 'missed'",
            "final_reason = 'unknown'",
            "use once');",
            "stop');",
            "global_ranged:name = l(action_id, weapon, target_type, radius, 0, use_interval_ticks, expected_shots, hp",
            "finish_ranged(name, 'timeout')",
        ):
            self.assertIn(expected, source)
        run_tick = re.search(r"run_ranged_tick\(name, r\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(run_tick, "run_ranged_tick function not found")
        run_tick_body = run_tick.group(1)
        self.assertIn("if(!fired_observed && arrow_near_bot(name, 8),", run_tick_body)
        self.assertNotIn("run('player ' + name + ' use once');\n                fired_observed = true", run_tick_body)
        self.assertNotIn("run('player ' + name + ' stop');\n                fired_observed = true", run_tick_body)

    def test_container_transfer_uses_non_gui_slot_mutation_and_reports_deltas(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "run_container_transfer",
            "containerTransfer",
            "inventory_get(cpos, container_slot)",
            "inventory_get(name, bot_slot)",
            "inventory_set(cpos, container_slot",
            "inventory_set(name, bot_slot",
            "requested",
            "max_stack",
            "move_count",
            "dest_count + move_count",
            "destination_full",
            "'partial'",
            "source_empty",
            "destination_occupied",
            "container_before",
            "container_after",
            "bot_before",
            "bot_after",
            "containerDone",
        ):
            self.assertIn(expected, source)

    def test_drop_item_uses_physical_drop_and_reports_slot_delta(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "start_drop_item",
            "run_drop_tick",
            "finish_drop",
            "global_drops",
            "drop_mode",
            "dropItem",
            "dropDone",
            "player ' + name + ' drop",
            "player ' + name + ' dropStack",
            "count_before",
            "count_after",
            "source_empty",
            "no_delta",
            "stack_json(before)",
            "stack_json(after)",
        ):
            self.assertIn(expected, source)

    def test_handoff_item_spawns_world_item_and_does_not_directly_credit_receiver_inventory(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "run_handoff_item",
            "handoffItem",
            "handoffDone",
            "find_hotbar_item(name, item)",
            "find_inventory_item(name, item)",
            "inventory_set(name, source_slot, 0)",
            "inventory_set(name, source_slot, remaining",
            "summon item",
            "watch_bot(name)",
            "spawned_item",
            "receiver_not_found",
            "item_not_available",
        ):
            self.assertIn(expected, source)

        match = re.search(r"run_handoff_item\(name, action_id, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(match, "run_handoff_item function not found")
        body = match.group(1)
        self.assertIn("inventory_set(name, source_slot", body)
        self.assertIn("summon item", body)
        self.assertNotIn("inventory_set(receiver", body)
        self.assertNotIn("give " , body)

    def test_move_item_uses_bot_inventory_transaction_and_reports_deltas(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "run_move_item",
            "moveItem",
            "moveItemDone",
            "from_slot",
            "to_slot",
            "inventory_get(name, from_slot)",
            "inventory_get(name, to_slot)",
            "entity_slot_path",
            "copy_full_stack",
            "item replace entity %s %s from entity %s %s",
            "inventory_set(name, to_slot",
            "inventory_set(name, from_slot, 0)",
            "requested",
            "max_stack",
            "move_count",
            "exact_full_stack_move",
            "dest_count + move_count",
            "destination_full",
            "'partial'",
            "source_empty",
            "destination_occupied",
            "invalid_slot",
            "from_before",
            "from_after",
            "to_before",
            "to_after",
        ):
            self.assertIn(expected, source)

    def test_craft_item_uses_inventory_transaction_and_reports_deltas(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "run_craft_item",
            "craftItem",
            "craftDone",
            "craft_inputs_ready",
            "craft_apply_inputs",
            "craft_input_facts_json",
            "inventory_get(name, slot)",
            "inventory_set(name, slot, remaining",
            "inventory_set(name, output_slot",
            "missing_inputs",
            "destination_occupied",
            "destination_full",
            "invalid_recipe",
            "inputs_before",
            "inputs_after",
            "output_before",
            "output_after",
        ):
            self.assertIn(expected, source)

    def test_furnace_transfer_uses_named_furnace_slots_and_reports_deltas(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "run_furnace_transfer",
            "furnaceTransfer",
            "furnaceDone",
            "furnace_slot_index",
            "if(slot_name == 'input', 0",
            "if(slot_name == 'fuel', 1",
            "if(slot_name == 'output', 2",
            "inventory_get(fpos, furnace_slot)",
            "inventory_set(fpos, furnace_slot",
            "inventory_set(name, bot_slot",
            "invalid_furnace_slot",
            "source_empty",
            "destination_occupied",
            "furnace_before",
            "furnace_after",
            "bot_before",
            "bot_after",
        ):
            self.assertIn(expected, source)

    def test_move_to_controller_has_progress_deviation_and_stuck_guards(self):
        source = MINEBOT_SC.read_text()

        for helper in (
            "dist_to_target",
            "distance_from_start_path",
            "move_guard_json",
            "param_number",
        ):
            self.assertIn(helper, source)
        self.assertIn("dx = number(x) - number(pos:0)", source)
        self.assertIn("dx = number(a:0) - number(b:0)", source)

        for param in (
            "arrival_radius",
            "timeout_ticks",
            "no_progress_ticks",
            "min_progress_delta",
            "max_deviation",
        ):
            self.assertIn(param, source)

        self.assertIn("finish_move(name, 'stuck', false)", source)
        self.assertIn("finish_move(name, 'deviated', false)", source)
        self.assertIn("finish_move(name, 'timeout', false)", source)
        self.assertRegex(source, r"global_moves:name = l\(action_id, x, y, z, arrival_radius")
        self.assertIn('"min_dist":%.3f', source)
        self.assertIn('"stuck_ticks":%d', source)
        self.assertIn('"deviation":%.3f', source)
        self.assertIn('"guard":%s', source)

    def test_move_to_controller_executes_waypoint_lists_not_only_final_target(self):
        source = MINEBOT_SC.read_text()

        for helper in (
            "json_waypoints",
            "parse_waypoints",
            "normalize_waypoint_point",
            "current_waypoint",
            "advance_waypoint",
        ):
            self.assertIn(helper, source)

        self.assertIn("raw = params:'waypoints'", source)
        self.assertIn("points = parse_waypoints(params, x, y, z)", source)
        self.assertIn("first_target = normalize_waypoint_point(points:0)", source)
        self.assertIn("target = current_waypoint(m)", source)
        self.assertIn("advance_waypoint(name, updated_move)", source)
        self.assertIn('"waypoints":%s', source)
        self.assertIn('"waypoint_index":%d', source)
        self.assertIn('"waypoint_count":%d', source)

    def test_move_to_controller_preserves_movement_cancel_facts(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "json_movement_cancel_steps",
            "movement_cancel_json",
            "movement_cancel = params:'movement_cancel'",
            "movement_cancel_json(movement_cancel)",
            "movement_cancel_json(m:15)",
            '"movement_cancel":%s',
        ):
            self.assertIn(expected, source)

        self.assertIn("global_moves:name = l(action_id, x, y, z, arrival_radius, 0, p, start_dist, 0, timeout_ticks, no_progress_ticks, min_progress_delta, max_deviation, points, 0, movement_cancel)", source)
        self.assertIn("updated_move = l(m:0, m:1, m:2, m:3, m:4, ticks, m:6, min_dist, stuck_ticks, m:9, m:10, m:11, m:12, m:13, m:14, m:15)", source)

    def test_move_to_controller_pulses_jump_for_ascending_waypoints(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("if(target:1 > p:1 + 0.35,", source)
        self.assertIn("run('player ' + name + ' jump once')", source)

    def test_move_to_controller_has_delayed_cancel_state_for_unsafe_movements(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_move_cancels = {}",
            "movement_cancel_safe_now",
            "request_move_cancel",
            "run_move_cancel_tick",
            "moveCancelDelayed",
            "global_move_cancels:name = l(reason, global_tick, movement_cancel_json(m:15))",
            "if(global_move_cancels:name == null,",
            "global_move_cancels:name = null",
        ):
            self.assertIn(expected, source)

        self.assertIn("request_move_cancel(name, 'preempted')", source)
        self.assertIn("request_move_cancel(name, 'interrupted')", source)
        self.assertNotIn("finish_move(name, 'interrupted', false)", source)
        self.assertIn("run_move_cancel_tick(name, m)", source)
        self.assertIn("policy == 'land_first' || policy == 'settle_on_support' || policy == 'surface_or_stable_water' || policy == 'after_step'", source)

    def test_mine_block_controller_uses_physical_attack_and_authoritative_block_truth(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_mines = {}",
            "start_mine_block",
            "run_mine_tick",
            "finish_mine",
            "mineDone",
            "block_gone",
            "block_now",
            "stopped_reason",
        ):
            self.assertIn(expected, source)

        self.assertRegex(source, r"global_mines:name = l\(action_id, x, y, z, block_type")
        self.assertIn("run('player ' + name + ' attack continuous')", source)
        self.assertIn("block_kind(block_now) == 'CLEAR'", source)
        start = re.search(r"start_mine_block\(name, action_id, x, y, z, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(start, "start_mine_block function not found")
        self.assertNotIn("setblock", start.group(1))
        run_tick = re.search(r"run_mine_tick\(name, m\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(run_tick, "run_mine_tick function not found")
        self.assertNotIn("look at", run_tick.group(1))
        self.assertNotIn("attack continuous", run_tick.group(1))
        self.assertNotIn("setblock", run_tick.group(1))

    def test_mine_block_controller_is_preempted_by_survival_reflex_and_interrupt(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("cancel_mine_preempted(name)", source)
        self.assertIn("finish_mine(name, 'preempted')", source)
        self.assertIn("finish_mine(name, 'interrupted')", source)
        self.assertIn("release_owner(name, 'mineBlock')", source)

    def test_water_reflex_uses_oxygen_risk_and_distinct_escape_strategy(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_water_reflex_air_threshold = 80",
            "global_water_reflex_damage_budget = null",
            "global_water_reflex_health_baselines = {}",
            "bot_air(name)",
            "head_in_water_now(name)",
            "water_reflex_should_trigger(name)",
            "air_risk = head_in_water_now(name) && air != null && air <= global_water_reflex_air_threshold;",
            "damage_risk = global_water_reflex_health_baselines:name - hp >= global_water_reflex_damage_budget",
            "water_surface_target(p)",
            "water_near_cell(x, y, z)",
            "water_near_escape_cell(x, y, z)",
            "water_shore_escape_target(p)",
            "water_escape_target(p)",
            "water_hazard_clear(name)",
            "bs != 'water' && bs != 'minecraft:water'",
            "queue_immediate_water_reflex(name)",
            "start_water_reflex(name) -> start_hazard_reflex(name, 'water');",
            "if(kind == 'water', water_escape_target(p), safe_escape_target(p))",
            "if(kind == 'fire', 'fireReflex', if(kind == 'water', 'waterReflex', 'lavaReflex'))",
            "if(kind == 'fire', !on_fire_now(name), if(kind == 'water', water_hazard_clear(name), !lava_near_pos(p)))",
            "global_pending_reflexes:name = 'water'",
            "global_pending_reflexes:name = 'fire'",
            "global_pending_reflexes:name = 'lava'",
            "release_owner(name, owner_name)",
        ):
            self.assertIn(expected, source)

        hazard = re.search(r"hazard_kind_near_name\(name\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(hazard, "hazard_kind_near_name function not found")
        self.assertIn("water_reflex_should_trigger(name)", hazard.group(1))
        self.assertNotIn("if(in_water_now(name),", hazard.group(1))

    def test_ranged_attack_controller_uses_weapon_specific_fire_and_authoritative_damage_truth(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_ranged = {}",
            "start_ranged_attack",
            "run_ranged_tick",
            "finish_ranged",
            "rangedDone",
            "run('player ' + name + ' use continuous')",
            "run('player ' + name + ' stop')",
            "run('player ' + name + ' use once')",
            "if(r:1 == 'crossbow',",
            "ranged_target_aim_pos(",
            "aim_ranged_target(",
            "ballistic_low_arc_pitch(",
            "target_pos:1 + 0.5",
            "ballistic_low_arc_pitch(dx, dy, dz, 3.0, 0.05)",
            "run(str('player %s look %.3f %.3f', name, pitch, yaw))",
            "damage_seen = r:12 || (r:7 != null && hp != null && hp < r:7) || (r:9 != null && hp != null && hp < r:9);",
            "release_owner(name, 'rangedAttack')",
        ):
            self.assertIn(expected, source)

        start = re.search(r"start_ranged_attack\(name, action_id, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(start, "start_ranged_attack function not found")
        start_body = start.group(1)
        self.assertIn("weapon = if(params:'weapon' == null, 'bow', params:'weapon');", start_body)
        self.assertIn("target_id = if(params:'target_id' == null, null, params:'target_id');", start_body)
        self.assertIn("target_type_is_player", start_body)
        self.assertIn("target_entity_uuid_near(name, target_id, radius)", start_body)
        self.assertIn("'player_target_requires_name'", start_body)
        self.assertIn("watch_bot(name);", start_body)

        run_tick = re.search(r"run_ranged_tick\(name, r\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(run_tick, "run_ranged_tick function not found")
        run_tick_body = run_tick.group(1)
        self.assertIn("aim_ranged_target(name, p, r:2);", run_tick_body)
        self.assertIn("if(ticks < r:5,", run_tick_body)
        self.assertIn("if(ticks == r:5,", run_tick_body)
        self.assertIn("run('player ' + name + ' use once')", run_tick_body)
        self.assertIn("run('player ' + name + ' stop')", run_tick_body)
        self.assertIn("finish_ranged(name, 'completed')", run_tick_body)
        self.assertIn("finish_ranged(name, 'timeout')", run_tick_body)
        self.assertNotIn("look at", run_tick_body)

        ballistic = re.search(r"ballistic_low_arc_pitch\(dx, dy, dz, speed, gravity\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(ballistic, "ballistic_low_arc_pitch function not found")
        ballistic_body = ballistic.group(1)
        self.assertIn("speed2 * speed2 - gravity * (gravity * horiz * horiz + 2 * dy * speed2)", ballistic_body)
        self.assertIn("tan_theta = (speed2 - sqrt(root)) / (gravity * horiz);", ballistic_body)
        self.assertIn("-atan2(tan_theta, 1.0)", ballistic_body)

    def test_ranged_attack_controller_is_preempted_by_survival_reflex_and_interrupt(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("finish_ranged(name, 'preempted')", source)
        self.assertIn("finish_ranged(name, 'interrupted')", source)
        self.assertIn("release_owner(name, 'rangedAttack')", source)

    def test_place_block_controller_uses_physical_use_and_authoritative_block_truth(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_places = {}",
            "start_place_block",
            "run_place_tick",
            "finish_place",
            "placeDone",
            "block_at_target",
            "expected_type",
            "face",
            "block_matches_expected",
        ):
            self.assertIn(expected, source)

        self.assertRegex(source, r"global_places:name = l\(action_id, x, y, z, block_type")
        self.assertIn("run('player ' + name + ' use once')", source)
        self.assertIn("block_matches_expected(block_now, pstate:4)", source)
        start = re.search(r"start_place_block\(name, action_id, x, y, z, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(start, "start_place_block function not found")
        self.assertNotIn("setblock", start.group(1))
        run_tick = re.search(r"run_place_tick\(name, pstate\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(run_tick, "run_place_tick function not found")
        self.assertNotIn("setblock", run_tick.group(1))

    def test_place_block_controller_is_preempted_by_survival_reflex_and_interrupt(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("cancel_place_preempted(name)", source)
        self.assertIn("finish_place(name, 'preempted')", source)
        self.assertIn("finish_place(name, 'interrupted')", source)
        self.assertIn("release_owner(name, 'placeBlock')", source)

    def test_ignite_block_controller_prefers_physical_and_authoritatively_verifies_fire(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_ignites = {}",
            "start_ignite_block",
            "run_ignite_tick",
            "finish_ignite",
            "igniteDone",
            "block_matches_expected(block_now, 'fire')",
            "allow_substitute = if(params:'allow_server_substitute' == null, false, params:'allow_server_substitute');",
            "run(str('setblock %d %d %d fire', ig:1, ig:2, ig:3));",
            "method = if(on_fire, ig:8, 'failed');",
            "release_owner(name, 'igniteBlock')",
        ):
            self.assertIn(expected, source)

        start = re.search(r"start_ignite_block\(name, action_id, x, y, z, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(start, "start_ignite_block function not found")
        start_body = start.group(1)
        self.assertIn("place_aim(name, x, y, z, 'up');", start_body)
        self.assertIn("run('player ' + name + ' use once')", start_body)
        self.assertIn("allow_substitute", start_body)

        run_tick = re.search(r"run_ignite_tick\(name, ig\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(run_tick, "run_ignite_tick function not found")
        run_tick_body = run_tick.group(1)
        self.assertIn("finish_ignite(name, 'completed')", run_tick_body)
        self.assertIn("finish_ignite(name, 'timeout')", run_tick_body)
        self.assertIn("run(str('setblock %d %d %d fire', ig:1, ig:2, ig:3));", run_tick_body)
        self.assertIn("'substitute'", run_tick_body)

        finish = re.search(r"finish_ignite\(name, reason\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(finish, "finish_ignite function not found")
        finish_body = finish.group(1)
        self.assertIn("on_fire = block_matches_expected(block_now, 'fire')", finish_body)
        self.assertIn("success = on_fire && reason != 'interrupted' && reason != 'preempted' && reason != 'blocked'", finish_body)
        self.assertIn("method = if(on_fire, ig:8, 'failed');", finish_body)

    def test_sow_crop_controller_prefers_physical_and_requires_crop_truth(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_sows = {}",
            "start_sow_crop",
            "run_sow_tick",
            "finish_sow",
            "sowDone",
            "release_owner(name, 'sowCrop')",
        ):
            self.assertIn(expected, source)


if __name__ == "__main__":
    unittest.main()
