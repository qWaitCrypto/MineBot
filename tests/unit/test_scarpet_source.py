import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MINEBOT_SC = ROOT / "minecraft" / "server" / "scarpet" / "minebot.sc"
RUNTIME_MIRROR_MINEBOT_SC = ROOT / "test-server" / "world" / "scripts" / "minebot.sc"


class ScarpetSourceTests(unittest.TestCase):
    def test_block_kind_uses_authoritative_replaceable_tag(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("if(block_tags(bs, 'replaceable'), 'CLEAR', 'SOLID')", source)
        self.assertLess(source.index("bs == 'water'"), source.index("block_tags(bs, 'replaceable')"))

    def test_deployable_minebot_script_is_present(self):
        self.assertTrue(MINEBOT_SC.is_file())

    def test_local_runtime_mirror_stays_in_sync_when_present(self):
        if not RUNTIME_MIRROR_MINEBOT_SC.exists():
            self.skipTest("local runtime mirror is not part of the clean-clone fixture")
        self.assertEqual(MINEBOT_SC.read_text(), RUNTIME_MIRROR_MINEBOT_SC.read_text())

    def test_source_remains_comment_free_for_scarpet_loader(self):
        source = MINEBOT_SC.read_text()
        self.assertNotRegex(source, r"(?m)^\s*#")

    def test_minebot_say_is_synchronous_outbound_chat_primitive(self):
        source = MINEBOT_SC.read_text()
        match = re.search(r"minebot_say\(name, text\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(match, "minebot_say function not found")
        body = match.group(1)
        action = re.search(r"minebot_action\(name, payload\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(action, "minebot_action function not found")

        self.assertIn("replace(str('%s', text), '\\n', ' ')", body)
        self.assertIn("str('%.240s'", body)
        self.assertIn("execute as %s run say %s", body)
        self.assertIn('"action":"say","said":true', body)
        self.assertIn('"action":"say","said":false', body)
        self.assertNotIn("minebot_say", action.group(1))
        self.assertNotIn("global_owners", body)
        self.assertNotIn("emit(", body)

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
        self.assertIn("scope == 'surfaceColumns'", source)
        self.assertIn("scope == 'nearbyBlocks'", source)
        self.assertIn("scope == 'findBlocks'", source)
        self.assertIn("scope == 'nearbyEntities'", source)
        self.assertIn("scope == 'inventory'", source)
        self.assertIn("scope == 'container'", source)
        self.assertIn("block_fact_json", source)
        self.assertIn("perceive_surface_columns", source)
        self.assertIn("top('surface', block(x, 0, z))", source)
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
        self.assertIn("perception_json(name, 'nearbyBlocks', true, complete", source)
        self.assertIn("perception_json(name, 'findBlocks', true, complete", source)
        self.assertIn("perception_json(name, 'debugBlocks', true, complete", source)
        self.assertIn("perception_json(name, 'nearbyEntities', true, !overflow", source)
        self.assertIn("perception_json(name, 'inventory', true, complete", source)
        self.assertIn("perception_json(name, 'container', true, complete", source)

    def test_camera_observer_is_absent_from_agent_semantics(self):
        source = MINEBOT_SC.read_text()

        player_lookup = re.search(r"player_entity\(name\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(player_lookup, "player_entity function not found")
        self.assertIn("tag=!minebot.camera.observer", player_lookup.group(1))

        nearby = re.search(r"perceive_nearby_entities\(name, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(nearby, "nearbyEntities function not found")
        self.assertIn("tag=!minebot.camera.observer", nearby.group(1))

        hostiles = re.search(r"perceive_hostiles\(name, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(hostiles, "nearbyHostiles function not found")
        self.assertIn("tag=!minebot.camera.observer", hostiles.group(1))

        for function_name in (
            "target_entity_near",
            "target_entity_named_near",
            "target_entity_uuid_near",
            "nearest_hostile_near",
        ):
            start = source.index("\n" + function_name + "(") + 1
            body = source[start : source.index("\n);", start)]
            self.assertIn("tag=!minebot.camera.observer", body)

        chat = re.search(r"__on_player_message\(player, message\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(chat, "player message handler not found")
        self.assertIn("if(!is_camera_observer(player)", chat.group(1))
        self.assertIn("if(is_camera_observer(player), true, emit_watched('itemPickup'", source)
        self.assertIn("tag=minebot.camera.observer", source)

    def test_state_json_emits_compact_inventory_hash_not_full_inventory_raw(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("json_string(v)", source)
        self.assertIn("encode_json(str('%s', v))", source)
        self.assertIn('"inventory_raw":""', source)
        self.assertIn('"inventory_hash":%s', source)
        self.assertIn('"oxygen":%s', source)
        self.assertIn('"sleeping":%s', source)
        self.assertIn("json_string(hash_code(raw))", source)
        self.assertNotIn('"inventory_raw":%s', source)
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
        # Referenced in each list-style perception. Static block scopes expose
        # resumable cursors; moving-entity scopes remain bounded top-k results.
        self.assertEqual(source.count("global_response_char_budget"), 8)  # 1 decl + 7 uses
        self.assertIn("length(out) + length(fact) >= global_response_char_budget", source)
        self.assertIn("length(out) + length(fact) >= global_response_char_budget", source)
        self.assertIn("entity_matches_filters(e, wanted_types, wanted_name)", source)

    def test_static_block_perceptions_use_numeric_resume_cursors(self):
        source = MINEBOT_SC.read_text()
        self.assertIn('"nextStart":%s', source)
        self.assertIn("perception_json(name, 'blockCells'", source)
        self.assertIn("data, uncertainty, next_value, null)", source)
        self.assertIn("next_value = if(complete, null, str('%d', idx));", source)
        self.assertIn("next_value = if(complete, null, str('%d', out_idx));", source)
        self.assertIn("perception_json(name, 'debugBlocks', true, complete, data, uncertainty, next_value, null)", source)
        self.assertIn("perception_json(name, 'nearbyBlocks', true, complete, data, uncertainty, next_value, null)", source)
        self.assertIn("perception_json(name, 'findBlocks', true, complete, data, uncertainty, next_value, null)", source)
        self.assertNotIn("perception_json(name, 'debugBlocks', true, !overflow", source)
        self.assertNotIn("perception_json(name, 'nearbyBlocks', true, !overflow", source)
        self.assertNotIn("perception_json(name, 'findBlocks', true, !overflow", source)
        self.assertNotIn("perception_json(name, 'debugBlocks', true, complete, data, uncertainty, if(overflow, 'limit', null)", source)
        self.assertNotIn("perception_json(name, 'nearbyBlocks', true, complete, data, uncertainty, if(overflow, 'limit', null)", source)
        self.assertNotIn("perception_json(name, 'findBlocks', true, complete, data, uncertainty, if(overflow, 'limit', null)", source)

    def test_moving_entity_perceptions_remain_bounded_top_k_not_cursor_paged(self):
        source = MINEBOT_SC.read_text()
        self.assertIn("perception_json(name, 'nearbyEntities', true, !overflow, data, uncertainty, if(overflow, 'limit', null), null)", source)
        self.assertIn("perception_json(name, 'nearbyHostiles', true, !overflow, data, uncertainty, if(overflow, 'limit', null), null)", source)
        self.assertNotIn('"nextStart":%s,"entities"', source)

    def test_find_blocks_scope_is_bounded_and_type_matched(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "block_type_matches",
            "block_type_matches_any",
            "wanted = if(params:'type' == null",
            "wanted_types = if(params:'types' == null, l(), params:'types');",
            "loop(min(length(wanted_types), 64),",
            "if(block_type_matches_any(bs, wanted, wanted_types),",
            "if(radius > 128, radius = 128)",
            "y_radius = if(radius > 16, 16, radius);",
            "if(params:'y_radius' != null, y_radius = floor(number(params:'y_radius')));",
            "if(y_radius > 64, y_radius = 64);",
            "r2 = radius * radius;",
            "if(ox*ox + oz*oz <= r2,",
            "if(limit > 128, limit = 128)",
            "found = l();",
            "entry = l(dist2, x, y, z, bs, block_kind(bs));",
            "put(found:insert_at, entry, 'insert');",
            "delete(found:window_limit);",
            "totalMatches",
            '"yRadius":%d',
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
                r".*?emit\('navigateDone'.*?'missing_body'.*?\);\s*true",
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
                r".*?emit\('navigateDone'.*?'move_start_failed'.*?\);\s*"
                r"global_navigations:name = null;\s*true",
                body,
                re.S,
            )
        )
        self.assertIn('"move_ticks":%d', source)
        self.assertIn('"move_min_dist":%.3f', source)
        self.assertIn('"move_stuck_ticks":%d', source)
        self.assertIn('"move_deviation":%.3f', source)
        self.assertIn('"move_waypoint_index":%d', source)
        self.assertIn('"move_waypoint_count":%d', source)
        self.assertIn('"move_current_waypoint":%s', source)
        self.assertIn("finish_navigate(name, l(m:0, arrived, p, target, dist, reason, m:5, m:7, m:8, deviation, m:14, length(m:13), current_waypoint(m)))", source)
        finish = re.search(r"finish_navigate\(name, move_event_data\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(finish, "finish_navigate function not found")
        self.assertIn("move_event_data:6", finish.group(1))
        self.assertIn("move_event_data:12", finish.group(1))

    def test_server_navigation_does_not_plan_through_no_floor_air_waypoints(self):
        source = MINEBOT_SC.read_text()
        self.assertIn("if(w == 'SOLID' || w == 'NO_FLOOR' || w == 'LAVA',", source)
        self.assertNotIn("if(w == 'LIQUID', 3.0, if(w == 'NO_FLOOR', 2.0, 1.0))", source)

    def test_server_navigation_requires_meaningful_partial_progress(self):
        source = MINEBOT_SC.read_text()
        self.assertIn(
            "navigate_to_plan(sx, sy, sz, gx, gy, gz, grid_radius, max_expand, y_below, y_above, cover_target, min_partial_progress, goal_radius)",
            source,
        )
        self.assertIn("navigation_partial_coefficients() -> l(1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 10.0)", source)
        self.assertIn("partial_score = h + new_g / coefficient", source)
        self.assertIn(
            "if(is_dry_stand_cell(nx, ny, nz) && best_partial_scores:(ci) - partial_score > 0.01,",
            source,
        )
        self.assertIn("candidate_distance >= min_partial_progress", source)
        self.assertIn("partial_key == null && candidate_key != null", source)
        self.assertIn("if(probe_walkability(x, y, z) == 'LIQUID', floor(number(p:1) + 0.5), y)", source)
        self.assertIn("min_partial_progress = floor(param_number(params, 'min_partial_progress', 5))", source)
        self.assertIn("if(min_partial_progress < 1, min_partial_progress = 1)", source)
        self.assertIn("partial_continuation = params:'partial_replans' != null", source)
        self.assertIn("params:'partial_replans' = partial_replans - 1", source)
        self.assertIn("params:'segment_index' = segment_index + 1", source)
        self.assertIn("start_navigate_to(name, action_id, gx, gy, gz, params)", source)
        self.assertIn("'partial_segment_budget_exhausted'", source)

    def test_server_navigation_honors_near_goal_radius(self):
        source = MINEBOT_SC.read_text()
        self.assertIn("navigation_goal_distance(x, y, z, goals) -> (", source)
        self.assertIn("d = max(0, probe_heuristic(x, y, z, goal:0, goal:1, goal:2) - goal:3)", source)
        self.assertIn("if(navigation_goal_distance(cx, cy, cz, goals) <= 0,", source)
        self.assertIn("goal_radius = floor(param_number(params, 'goal_radius', 0))", source)
        self.assertIn("if(goal_radius < 0, goal_radius = 0)", source)
        self.assertIn(
            "plan_result = navigate_to_goals_plan(sx, sy, sz, goals, grid_radius, max_expand, y_below, y_above, null, min_partial_progress, context)",
            source,
        )

    def test_server_navigation_preserves_bounded_goal_set_and_selected_goal(self):
        source = MINEBOT_SC.read_text()
        start = source.index("start_navigate_to(name, action_id, gx, gy, gz, params) -> (")
        end = source.index("finish_navigate(name, move_event_data) -> (")
        body = source[start:end]

        self.assertIn("navigation_goals_from_params(params, gx, gy, gz, goal_radius) -> (", source)
        self.assertIn("loop(min(length(raw), 32),", source)
        self.assertIn("goals = navigation_goals_from_params(params, gx, gy, gz, goal_radius)", body)
        self.assertIn("plan_result = navigate_to_goals_plan", body)
        self.assertNotIn("plan_result = navigate_to_plan", body)
        self.assertIn("selected_goal = plan_result:4", body)
        self.assertIn(
            "execution_arrival_radius = if(mutation_kind == 'downward', min(arrival_radius, 0.15)",
            body,
        )
        self.assertIn('"selected_goal":%s,"goal_count":%d', source)
        self.assertIn("json_pos(data:17), data:18", source)

    def test_server_navigation_uses_typed_non_mutating_movement_graph(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "navigation_neighbors(x, y, z, context) -> (",
            "navigation_fall_candidate(x, start_y, z, context) -> (",
            "if(w == 'LIQUID', 'swim', 'diagonal')",
            "if(w == 'LIQUID', 3.0, 1.4)",
            "if(w == 'LIQUID', 'egress_to_dry', 'immediate')",
            "navigation_candidate(nx, y + 1, nz",
            "'ascend'",
            "navigation_candidate(nx, y - 1, nz",
            "'descend'",
            "navigation_candidate(x, y, z, 'fall', 4.0 + depth, depth, 'land_first')",
            "navigation_candidate(x, y, z, 'swim', 4.0 + depth, depth, 'egress_to_dry')",
            "came_step:(nkey) = l(candidate:3, candidate:5, candidate:6, candidate:7)",
            "path += l(number(parts:0), number(parts:1), number(parts:2), movement:0, movement:1, movement:2, movement:3)",
            "'path_moves' -> execution_moves",
            "'path_fall_depths' -> execution_fall_depths",
            "'cancel_policies' -> execution_cancel_policies",
            "if(movement_kind == 'swim',",
            "look_y = if(movement_kind == 'swim', target:1 + 1.8",
            "run('player ' + name + ' jump continuous')",
            '"movement_counts":%s',
        ):
            self.assertIn(expected, source)

        self.assertIn("navigation_lava_unsafe(x, y, z) -> (", source)
        self.assertIn("lava_near_pos(l(x + 0.5, y, z + 0.5)) || is_lava_at(x, y + 1, z)", source)
        probe_body = source[source.index("probe_walkability(x, y, z) -> ("):source.index("probe_heuristic(")]
        self.assertIn("if(navigation_lava_unsafe(x, y, z),", probe_body)
        self.assertIn("feet_kind != 'SOLID' && head_kind != 'SOLID' && !navigation_lava_unsafe(x, y, z)", source)
        self.assertLess(
            probe_body.index("if(navigation_lava_unsafe(x, y, z),"),
            probe_body.index("if(feet_kind == 'SOLID' || head_kind == 'SOLID',"),
        )
        self.assertIn("'LAVA'", source)
        fall_body = source[source.index("navigation_fall_candidate"):source.index("navigation_neighbors(x, y, z, context)")]
        self.assertIn("loop(scan_depth,", fall_body)
        self.assertIn("depth <= floor(number(context:'max_fall_depth'))", fall_body)
        self.assertIn("depth <= floor(number(context:'max_water_drop_depth'))", fall_body)
        neighbors_body = source[source.index("navigation_neighbors(x, y, z, context)"):source.index("navigation_edge_valid(")]
        self.assertIn("source_kind = probe_walkability(x, y, z)", neighbors_body)
        self.assertGreaterEqual(neighbors_body.count("source_kind != 'LIQUID'"), 2)
        self.assertIn(
            "if(up == 'NO_FLOOR' && navigation_body_clear(x, y + 1, z), neighbors += navigation_candidate(x, y + 1, z, 'swim', 3.0, 0, 'egress_to_dry'))",
            neighbors_body,
        )
        self.assertIn("bool(context:'allow_ascend') && source_kind != 'NO_FLOOR'", neighbors_body)

    def test_server_navigation_bridge_uses_governed_proposal_handshake(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_navigation_mutations = {}",
            "navigation_mutation_candidate(nx, y, nz, 'place'",
            "navigation_mutation_denied(context, nx, y - 1, nz)",
            "stage_navigation_mutation(name, nav) -> (",
            "emit('navigateMutationProposed'",
            "decide_navigation_mutation(name, params) -> (",
            "params:'navigation_action_id' == mutation:'action_id'",
            "run_navigation_mutation_tick(name, mutation) -> (",
            "emit('navigateMutationDone'",
            "if(action_name == 'navigationMutationDecision'",
            "mutation:'status' = 'advancing'",
            "start_navigation_bridge_motion(name, mutation) -> (",
            "run('player ' + name + ' jump once')",
            "floor(number(p:0)) == floor(number(pos:0))",
            "run('player ' + name + ' use continuous')",
            "mutation:'status' = 'settling_success'",
            "navigation_mutation_safe_now(name) -> (",
            "request_navigation_mutation_cancel(name, reason) -> (",
        ):
            self.assertIn(expected, source)

        self.assertNotIn("setblock", source[source.index("decide_navigation_mutation(name, params)"):])
        interrupt_start = source.index("minebot_interrupt(name, payload) -> (")
        interrupt_end = source.index("minebot_action(name, payload) -> (")
        interrupt_body = source[interrupt_start:interrupt_end]
        self.assertIn("request_navigation_mutation_cancel(name, 'interrupted')", interrupt_body)
        self.assertNotIn("finish_navigation_mutation(name, false, 'interrupted')", interrupt_body)

    def test_server_navigation_breaks_governed_headroom_inside_navigation_owner(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "'allow_break' -> param_bool(params, 'allow_break', false)",
            "'break_budget' -> floor(param_number(params, 'break_budget', 0))",
            "navigation_break_tool(context, block_type) -> (",
            "'tool_item' -> navigation_break_tool(context, head_type)",
            "'tool_item' -> navigation_break_tool(context, feet_type)",
            "navigation_mutation_candidate(nx, y, nz, 'break'",
            "source_head_blocked = navigation_block_kind_at(x, y + 1, z) == 'SOLID'",
            "source_cap_blocked = navigation_block_kind_at(x, y + 2, z) == 'SOLID'",
            "source_clearance_y = if(source_head_blocked, y + 1, y + 2)",
            "'pos' -> l(x, source_clearance_y, z)",
            "navigation_mutation_candidate(nx, y + 1, nz, 'break'",
            "'purpose' -> 'headroom'",
            "'purpose' -> 'path'",
            "mutation:'status' = 'breaking'",
            "navigation_select_item(name, mutation:'tool_item')",
            "run_navigation_break_mutation_tick(name, mutation) -> (",
            "block_kind(block_now) == 'CLEAR'",
            "finish_navigation_mutation(name, true, 'broken')",
            "finish_navigation_mutation(name, false, 'break_timeout')",
            "run('player ' + name + ' attack continuous')",
            "mutation:'decision_reason' = params:'reason'",
        ):
            self.assertIn(expected, source)

        break_tick = re.search(
            r"run_navigation_break_mutation_tick\(name, mutation\) -> \((.*?)\n\);",
            source,
            re.S,
        )
        self.assertIsNotNone(break_tick, "navigation break mutation tick not found")
        self.assertNotIn("setblock", break_tick.group(1))
        self.assertNotIn("global_mines:name", break_tick.group(1))

        cancel = re.search(
            r"request_navigation_mutation_cancel\(name, reason\) -> \((.*?)\n\);",
            source,
            re.S,
        )
        self.assertIsNotNone(cancel, "navigation mutation cancel function not found")
        self.assertIn("if(mutation:'kind' == 'break' ||", cancel.group(1))
        self.assertIn("finish_navigation_mutation(name, false, reason)", cancel.group(1))

    def test_server_navigation_only_plans_pickaxe_edges_with_the_required_tier(self):
        source = MINEBOT_SC.read_text()
        neighbors = source[source.index("navigation_neighbors(x, y, z, context)"):source.index("navigation_edge_valid(")]

        for expected in (
            "navigation_pickaxe_tier(item) -> (",
            "navigation_required_pickaxe_tier(block_type) -> (",
            "block_tags(block_type, 'needs_diamond_tool')",
            "block_tags(block_type, 'needs_iron_tool')",
            "block_tags(block_type, 'needs_stone_tool')",
            "block_tags(block_type, 'mineable/pickaxe')",
            "navigation_break_available(context, block_type) -> (",
            "if(tool == null, false, navigation_pickaxe_tier(tool) >= required)",
        ):
            self.assertIn(expected, source)

        self.assertEqual(neighbors.count("navigation_break_available(context,"), 5)

    def test_server_navigation_pillar_uses_governed_physical_controller(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "'allow_pillar' -> param_bool(params, 'allow_pillar', false)",
            "'pillar_budget' -> floor(param_number(params, 'pillar_budget', 0))",
            "'kind' -> 'pillar'",
            "navigation_mutation_candidate(x, y + 1, z, 'pillar'",
            "best_mutation_key = null",
            "candidate:7 != null && h + new_g < best_mutation_score",
            "if(best_mutation_key != null, best_mutation_key, start_key)",
            "navigation_mutation_centered(name, mutation) -> (",
            "start_navigation_pillar_jump(name, mutation) -> (",
            "run_navigation_pillar_mutation_tick(name, mutation) -> (",
            "place_aim(name, pos:0, pos:1, pos:2, 'up')",
            "finish_navigation_mutation(name, true, 'pillared')",
            "p:1 >= source:1 + 0.8",
        ):
            self.assertIn(expected, source)

        pillar_tick = re.search(
            r"run_navigation_pillar_mutation_tick\(name, mutation\) -> \((.*?)\n\);",
            source,
            re.S,
        )
        self.assertIsNotNone(pillar_tick, "navigation pillar mutation tick not found")
        self.assertNotIn("setblock", pillar_tick.group(1))
        self.assertNotIn("global_places:name", pillar_tick.group(1))
        self.assertIn("navigation_mutation_safe_now(name)", pillar_tick.group(1))

    def test_server_navigation_downward_uses_governed_land_first_controller(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "'allow_downward' -> param_bool(params, 'allow_downward', false)",
            "'downward_budget' -> floor(param_number(params, 'downward_budget', 0))",
            "'kind' -> 'downward'",
            "navigation_mutation_candidate(x, y - 1, z, 'downward'",
            "'purpose' -> 'downward'",
            "mutation:'status' = if(mutation:'kind' == 'downward', 'centering', 'breaking')",
            "start_navigation_downward_break(name, mutation) -> (",
            "navigation_mutation_horizontally_stable(name) -> (",
            "if(navigation_mutation_centered(name, mutation)",
            "navigation_mutation_horizontally_stable(name) && navigation_mutation_safe_now(name)",
            "run_navigation_downward_mutation_tick(name, mutation) -> (",
            "if(navigation_mutation_centered(name, mutation)",
            "p:1 <= source:1 - 0.8 && navigation_mutation_safe_now(name)",
            "finish_navigation_mutation(name, true, 'descended')",
            "finish_navigation_mutation(name, false, mutation:'cancel_reason')",
        ):
            self.assertIn(expected, source)

        downward_tick = re.search(
            r"run_navigation_downward_mutation_tick\(name, mutation\) -> \((.*?)\n\);",
            source,
            re.S,
        )
        self.assertIsNotNone(downward_tick, "navigation downward mutation tick not found")
        self.assertNotIn("setblock", downward_tick.group(1))
        self.assertNotIn("global_mines:name", downward_tick.group(1))
        self.assertIn("navigation_mutation_safe_now(name)", downward_tick.group(1))

    def test_server_navigation_downward_refuses_liquid_exposed_floor_break(self):
        source = MINEBOT_SC.read_text()

        risk = re.search(
            r"navigation_downward_flood_risk\(x, y, z\) -> \((.*?)\n\);",
            source,
            re.S,
        )
        self.assertIsNotNone(risk, "navigation downward flood-risk predicate not found")
        self.assertIn("navigation_adjacent_fluid_break_risk(x, y + 1, z, true)", risk.group(1))
        for expected in (
            "navigation_adjacent_fluid_break_risk(x + 1, y, z, false)",
            "navigation_adjacent_fluid_break_risk(x - 1, y, z, false)",
            "navigation_adjacent_fluid_break_risk(x, y, z + 1, false)",
            "navigation_adjacent_fluid_break_risk(x, y, z - 1, false)",
        ):
            self.assertIn(expected, risk.group(1))
        self.assertNotIn("x, y - 1, z", risk.group(1))

        neighbors = source[source.index("navigation_neighbors(x, y, z, context)"):source.index("navigation_edge_valid(")]
        downward = neighbors[neighbors.index("downward_floor_type"):neighbors.index("pillar_used")]
        self.assertIn("!navigation_downward_flood_risk(x, y - 1, z)", downward)

    def test_server_navigation_downward_rechecks_flood_risk_before_break(self):
        source = MINEBOT_SC.read_text()

        starter = re.search(
            r"start_navigation_downward_break\(name, mutation\) -> \((.*?)\n\);",
            source,
            re.S,
        )
        self.assertIsNotNone(starter, "navigation downward break starter not found")
        body = starter.group(1)
        risk_check = "if(navigation_downward_flood_risk(pos:0, pos:1, pos:2),"
        attack = "run('player ' + name + ' attack continuous')"
        self.assertIn(risk_check, body)
        self.assertIn("finish_navigation_mutation(name, false, 'downward_flood_risk')", body)
        self.assertLess(body.index(risk_check), body.index(attack))

    def test_server_navigation_open_uses_governed_property_verified_controller(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "'allow_open' -> param_bool(params, 'allow_open', false)",
            "'open_budget' -> floor(param_number(params, 'open_budget', 0))",
            "navigation_manual_openable_type(bs) -> (",
            "replace(bs, '_door', '') != bs",
            "replace(bs, '_trapdoor', '') == bs",
            "replace(bs, '_fence_gate', '') != bs",
            "bs != 'iron_door'",
            "navigation_openable_open_at(x, y, z) -> (",
            "navigation_mutation_candidate(nx, y, nz, 'open'",
            "'purpose' -> 'open'",
            "run_navigation_open_mutation_tick(name, mutation) -> (",
            "finish_navigation_mutation(name, true, 'opened')",
            "finish_navigation_mutation(name, false, 'open_no_effect')",
        ):
            self.assertIn(expected, source)

        open_tick = re.search(
            r"run_navigation_open_mutation_tick\(name, mutation\) -> \((.*?)\n\);",
            source,
            re.S,
        )
        self.assertIsNotNone(open_tick, "navigation open mutation tick not found")
        self.assertNotIn("setblock", open_tick.group(1))
        self.assertNotIn("global_uses:name", open_tick.group(1))
        self.assertIn("navigation_openable_open_at", open_tick.group(1))

    def test_server_navigation_freezes_capabilities_and_live_rechecks_edges(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "navigation_context_from_params(params) -> (",
            "'allow_diagonal' -> param_bool(params, 'allow_diagonal', true)",
            "'allow_swim' -> param_bool(params, 'allow_swim', true)",
            "'max_fall_depth' -> max_fall_depth",
            "'max_water_drop_depth' -> max_water_drop_depth",
            "'allow_break' -> param_bool(params, 'allow_break', false)",
            "'break_timeout_ticks' -> break_timeout_ticks",
            "'break_pickaxe' -> params:'break_pickaxe'",
            "'break_axe' -> params:'break_axe'",
            "'break_shovel' -> params:'break_shovel'",
            "'allow_pillar' -> param_bool(params, 'allow_pillar', false)",
            "'pillar_budget' -> floor(param_number(params, 'pillar_budget', 0))",
            "'allow_downward' -> param_bool(params, 'allow_downward', false)",
            "'downward_budget' -> floor(param_number(params, 'downward_budget', 0))",
            "'allow_open' -> param_bool(params, 'allow_open', false)",
            "'open_budget' -> floor(param_number(params, 'open_budget', 0))",
            "navigation_edge_valid(sx, sy, sz, tx, ty, tz, movement_kind, fall_depth, context)",
            "navigation_move_recheck_reason(name, m) -> (",
            "first_index + floor(number(nav:16)) - 1",
            "emit('navigateRecheck'",
            "request_move_cancel(name, recheck_reason)",
        ):
            self.assertIn(expected, source)

    def test_delayed_move_cancel_keeps_unsafe_movement_running_until_safe(self):
        source = MINEBOT_SC.read_text()
        request_start = source.index("request_move_cancel(name, reason) -> (")
        request_end = source.index("run_move_cancel_tick(name, m) -> (")
        request_body = source[request_start:request_end]
        tick_start = source.index("tick_bot(name) -> (")
        tick_end = source.index("__on_tick() -> (")
        tick_body = source[tick_start:tick_end]

        self.assertNotIn("stop_body(name);\n    if(movement_cancel_safe_now", request_body)
        self.assertIn("if(global_move_cancels:name == null,", request_body)
        self.assertIn("run_move_cancel_tick(name, m);", tick_body)
        self.assertIn("if(m != null,\n                        run_move_tick(name, m)", tick_body)
        self.assertNotIn("global_tick - pending:1", source)

        interrupt_start = source.index("minebot_interrupt(name, payload) -> (")
        interrupt_end = source.index("minebot_action(name, payload) -> (")
        interrupt_body = source[interrupt_start:interrupt_end]
        self.assertIn("had_move = global_moves:name != null", interrupt_body)
        self.assertIn("request_move_cancel(name, 'interrupted')", interrupt_body)
        self.assertNotIn(
            "stop_body(name);\n  if(global_navigations:name != null",
            interrupt_body,
        )

    def test_settle_on_support_cancel_can_finish_on_stable_support_before_waypoint(self):
        source = MINEBOT_SC.read_text()
        settle_start = source.index("movement_settled_on_support(p, on_ground, nbt) -> (")
        settle_end = source.index("apply_movement_controls(name, movement_kind, p, target) -> (")
        settle_body = source[settle_start:settle_end]
        safe_start = source.index("movement_cancel_safe_now(name, m) -> (")
        safe_end = source.index("start_move_cancel_water_egress(name, m, reason) -> (")
        safe_body = source[safe_start:safe_end]

        self.assertIn("if(on_ground,", settle_body)
        self.assertIn("is_dry_stand_cell(floor(p:0), floor(p:1), floor(p:2))", settle_body)
        self.assertIn("p:1 - floor(p:1) <= 0.25", settle_body)
        self.assertIn("abs(number(motion:1))", settle_body)
        self.assertIn("vertical_speed <= 0.12", settle_body)
        self.assertIn("settled_on_support = movement_settled_on_support(p, on_ground, nbt);", safe_body)
        self.assertIn(
            "if(policy == 'settle_on_support', settled_on_support, at_waypoint && on_ground)",
            safe_body,
        )

    def test_delayed_swim_cancel_uses_bounded_dry_egress_and_always_terminates(self):
        source = MINEBOT_SC.read_text()
        request_start = source.index("start_move_cancel_water_egress(name, m, reason) -> (")
        request_end = source.index("run_move_cancel_tick(name, m) -> (")
        request_body = source[request_start:request_end]
        reflex_start = source.index("run_reflex_tick(name, r) -> (")
        reflex_end = source.index("run_mine_tick(name, m) -> (")
        reflex_body = source[reflex_start:reflex_end]

        self.assertIn("target = water_escape_target(p)", request_body)
        self.assertIn("target = water_surface_target(p)", request_body)
        self.assertIn("'water', 'moveTo'", request_body)
        self.assertIn("initialize_water_reflex_controls(name);", request_body)
        self.assertIn("reason + '_egress_unavailable'", request_body)
        self.assertNotIn("!in_water_now(name)", request_body)
        self.assertIn("on_dry_stand = p != null && is_dry_stand_cell", request_body)
        self.assertIn("if(policy == 'egress_to_dry', dry_stand,", source)
        self.assertIn(
            "if(policy == 'settle_on_support', settled_on_support, at_waypoint && on_ground)",
            source,
        )
        self.assertIn("start_move_cancel_water_egress(name, m, global_move_cancels:name:0)", request_body)
        self.assertIn("global_reflexes:name == null && controls_ready", source)
        self.assertIn("move_cancel_egress = kind == 'water' && owner_name == 'moveTo'", reflex_body)
        self.assertIn("retarget = water_escape_target(p)", reflex_body)
        self.assertIn("if(kind == 'water', initialize_water_reflex_controls(name));", reflex_body)
        self.assertNotIn("run('player ' + name + ' jump')", reflex_body)
        self.assertIn("if(move_cancel_egress,\n      emit('moveCancelEgress'", reflex_body)
        self.assertIn("finish_move(name, global_move_cancels:name:0, false)", reflex_body)
        self.assertIn("global_move_cancels:name:0 + '_egress_failed'", reflex_body)
        self.assertIn("if(kind == 'moveCancelEgress'", source)

    def test_follow_started_json_uses_json_value_for_target(self):
        source = MINEBOT_SC.read_text()

        self.assertIn('"target":%s,"target_pos":%s', source)
        self.assertNotIn('"target":"%s","target_pos":%s', source)
        self.assertIn("data:0, json_string(data:1), json_pos(data:2), data:3", source)

    def test_follow_entity_arrival_emits_terminal_success(self):
        source = MINEBOT_SC.read_text()
        start = source.index("run_follow_tick(name, f) -> (")
        end = source.index("finish_follow(name, reason) -> (")
        body = source[start:end]

        self.assertIn("if(dist <= keep_radius,", body)
        self.assertIn("finish_move(name, 'follow_hold', false)", body)
        self.assertIn("finish_follow(name, 'arrived')", body)

    def test_follow_replan_uses_tight_waypoint_arrival_not_keep_radius(self):
        source = MINEBOT_SC.read_text()
        match = re.search(r"follow_replan\(name, target_pos\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(match, "follow_replan function not found")
        body = match.group(1)

        self.assertIn("move_arrival_radius = 0.45", body)
        self.assertIn("'arrival_radius' -> move_arrival_radius", body)
        self.assertNotIn("'arrival_radius' -> keep_radius", body)
        self.assertIn("navigate_to_goals_plan(", body)
        self.assertIn("'path_moves' -> movement_kinds", body)
        self.assertIn("'path_fall_depths' -> fall_depths", body)
        self.assertIn("'cancel_policies' -> cancel_policies", body)
        self.assertNotIn("direct_wp", body)

    def test_engage_replan_uses_tight_waypoint_arrival_not_attack_range(self):
        source = MINEBOT_SC.read_text()
        match = re.search(r"engage_replan\(name, target_pos\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(match, "engage_replan function not found")
        body = match.group(1)

        self.assertIn("move_arrival_radius = 0.45", body)
        self.assertIn("'arrival_radius' -> move_arrival_radius", body)
        self.assertNotIn("'arrival_radius' -> attack_range", body)

    def test_nearby_entities_scope_is_bounded_and_reports_pos_health(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "entity_selector(selector)",
            "@e[x=%d,y=%d,z=%d,distance=..%d,tag=!minebot.camera.observer,sort=nearest]",
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
        self.assertIn("e != player(name) && entity_matches_filters(e, wanted_types, wanted_name)", source)
        self.assertIn("if(count >= limit || length(out) + length(fact) >= global_response_char_budget", source)

    def test_auto_combat_reflex_is_defensive_not_auto_pursuit(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("is_flying_hostile(e)", source)
        self.assertIn("entity_matches_type(e, 'minecraft:phantom')", source)
        self.assertIn("start_combat_flee_reflex(name, tp)", source)
        auto_reflex = re.search(r"start_combat_reflex\(name\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(auto_reflex)
        self.assertNotIn("start_engage(name, 'auto:combat:' + name", auto_reflex.group(1))

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

    def test_event_head_exposes_reload_epoch_and_both_monotonic_cursors(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("global_event_epoch = null;", source)
        self.assertIn("minebot_event_head(name, proposed_epoch) -> (", source)
        self.assertIn("if(global_event_epoch == null", source)
        self.assertIn('"eventSeq":%d,"chatSeq":%d,"tick":%d,"epoch":%s', source)
        self.assertIn("owner_name = if(owner == null, null, owner:0);", source)

    def test_event_drains_are_char_budgeted_and_pageable(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("if(complete && e:3 == name", source)
        self.assertIn("if(complete && e:3 == name && e:0 > since_seq", source)
        self.assertIn("if(length(candidate) > 2600", source)
        self.assertIn("oversized_event_json(e, payload_chars) -> (", source)
        self.assertEqual(source.count("out = oversized_event_json(e, length(item));"), 2)
        self.assertEqual(
            source.count(
                "out = oversized_event_json(e, length(item));\n"
                "          first = false;\n"
                "          last_seq = e:0"
            ),
            2,
        )
        self.assertIn('"name":"eventPayloadTooLarge"', source)
        self.assertIn('"payload_complete":false', source)
        self.assertIn('"next":%s', source)
        self.assertIn("next_value = if(complete, null, last_seq)", source)

    def test_death_and_respawn_events_are_emitted_in_production_app(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "__on_player_dies(player)",
            "emit('death', name",
            'if(kind == \'death\'',
            '"inventory_before":%s',
            '"inventory_hash":%s',
            '"inventory_counts_before":%s',
            "json_string('')",
            "inventory_counts_json(name)",
            "previous_count = if(counts:item == null, 0, counts:item);",
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

    def test_death_and_missing_body_clear_controller_and_owner_state(self):
        source = MINEBOT_SC.read_text()
        clear_start = source.index("clear_body_runtime(name) -> (")
        clear_end = source.index("finish_move(name, reason, arrived) -> (")
        clear_body = source[clear_start:clear_end]

        for expected in (
            "global_moves:name = null",
            "global_move_cancels:name = null",
            "global_move_control_inits:name = null",
            "global_navigations:name = null",
            "global_navigation_mutations:name = null",
            "global_follows:name = null",
            "global_mines:name = null",
            "global_places:name = null",
            "global_uses:name = null",
            "global_ignites:name = null",
            "global_sows:name = null",
            "global_attacks:name = null",
            "global_ranged:name = null",
            "global_drops:name = null",
            "global_reflexes:name = null",
            "global_pending_reflexes:name = null",
            "global_engages:name = null",
            "global_owners:name = null",
        ):
            self.assertIn(expected, clear_body)

        death_start = source.index("__on_player_dies(player) -> (")
        death_end = source.index("__on_player_connects(player) -> (")
        death_body = source[death_start:death_end]
        self.assertLess(death_body.index("clear_body_runtime(name)"), death_body.index("emit('death', name"))

        tick_start = source.index("tick_bot(name) -> (")
        tick_end = source.index("__on_tick() -> (")
        tick_body = source[tick_start:tick_end]
        self.assertLess(tick_body.index("clear_body_runtime(name)"), tick_body.index("emit('bodyMissing', name"))

    def test_interrupt_releases_only_orphaned_owner_state(self):
        source = MINEBOT_SC.read_text()
        active_start = source.index("body_runtime_active(name) -> (")
        active_end = source.index("clear_body_runtime(name) -> (")
        active_body = source[active_start:active_end]
        self.assertIn("if(!body_runtime_active(name), global_owners:name = null)", active_body)

        interrupt_start = source.index("minebot_interrupt(name, payload) -> (")
        interrupt_end = source.index("minebot_action(name, payload) -> (")
        interrupt_body = source[interrupt_start:interrupt_end]
        self.assertIn("release_orphan_owner(name)", interrupt_body)
        self.assertLess(
            interrupt_body.index("if(global_drops:name != null"),
            interrupt_body.index("release_orphan_owner(name)"),
        )

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
            "global_pending_spawns:name",
            "finalize_pending_spawn(name)",
            "run('gamemode ' + gamemode + ' ' + name)",
        ):
            self.assertIn(expected, source)
        self.assertNotIn("spawn_cmd = 'player ' + name + ' spawn in ' + params:'gamemode'", source)

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

    def test_recipe_data_perception_supports_optional_recipe_type(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "recipe_type = params:'type'",
            "recipe_data(item), recipe_data(item, recipe_type)",
            "compact_recipe_variant(raw)",
            "compact_recipe_variants(recipe)",
            "compact_outputs += l('' + output:0, floor(number(output:1)))",
            "compact_groups += compact_group",
            "if(seen:key == null",
            '"variantCount":%d',
            "json_string(str('%s', l(compact)))",
            '"type":%s',
        ):
            self.assertIn(expected, source)
        self.assertNotIn("json_string(str('%s', l(recipe)))", source)

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
            "@e[x=%d,y=%d,z=%d,distance=..%d,tag=!minebot.camera.observer,limit=32,sort=nearest]",
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

        self.assertIn("request_move_cancel(name, 'stuck')", source)
        self.assertIn("request_move_cancel(name, 'deviated')", source)
        self.assertIn("request_move_cancel(name, 'timeout')", source)
        self.assertRegex(source, r"global_moves:name = l\(action_id, x, y, z, arrival_radius")
        self.assertIn("mutation_kind = if(mutation_step == null, null, mutation_step:6:'kind')", source)
        self.assertIn(
            "execution_arrival_radius = if(mutation_kind == 'downward', min(arrival_radius, 0.15), if(plan_status == 'partial', min(arrival_radius, 0.45), arrival_radius))",
            source,
        )
        self.assertIn("'arrival_radius' -> execution_arrival_radius", source)
        self.assertIn('"min_dist":%.3f', source)
        self.assertIn('"stuck_ticks":%d', source)
        self.assertIn('"deviation":%.3f', source)
        self.assertIn('"guard":%s', source)

    def test_move_to_controller_executes_waypoint_lists_not_only_final_target(self):
        source = MINEBOT_SC.read_text()

        for helper in (
            "json_waypoints",
            "json_waypoint_summary",
            "parse_waypoints",
            "normalize_waypoint_point",
            "current_waypoint",
            "swim_waypoint_reached",
            "current_waypoint_reached",
            "apply_movement_controls",
            "initialize_movement_controls",
            "advance_waypoint",
        ):
            self.assertIn(helper, source)

        self.assertIn("raw = params:'waypoints'", source)
        self.assertIn("points = parse_waypoints(params, x, y, z)", source)
        self.assertIn("first_target = normalize_waypoint_point(points:0)", source)
        self.assertIn("target = current_waypoint(m)", source)
        self.assertIn("navigation_node_y(p) == floor(target:1)", source)
        self.assertIn(
            "transition_ready = horizontal_dist <= min(m:4, 0.17) && p:1 >= target:1 - min(m:4, 0.17)",
            source,
        )
        self.assertIn("next_kind == null || next_kind == 'swim' || transition_ready", source)
        self.assertIn("if(current_movement_kind(m) == 'swim', swim_waypoint_reached(m, p, target), dist <= m:4)", source)
        self.assertIn("if(next_kind != current_movement_kind(m),", source)
        self.assertIn("global_move_control_inits:name = l(m:0, idx, global_tick)", source)
        self.assertIn("pending_control_init:0 == m:0 && pending_control_init:1 == m:14", source)
        self.assertIn("global_tick > pending_control_init:2 + 1", source)
        self.assertIn("if(controls_ready, apply_movement_controls(name, movement_kind, p, target))", source)
        self.assertIn("initialize_movement_controls(name, current_movement_kind(global_moves:name), p, first_target)", source)
        self.assertIn("apply_movement_controls(name, movement_kind, p, target)", source)
        self.assertIn("advance_waypoint(name, updated_move)", source)
        self.assertIn('"waypoints":%s', source)
        self.assertIn("json_waypoint_summary(data:3)", source)
        self.assertIn('{"count":%d,"first":%s,"last":%s}', source)
        self.assertNotIn("json_waypoints(data:3)", source)
        self.assertIn('"waypoint_index":%d', source)
        self.assertIn('"waypoint_count":%d', source)

    def test_move_to_controller_preserves_movement_cancel_facts(self):
        source = MINEBOT_SC.read_text()

        for expected in (
            "global_movement_cancel_step_sample_limit = 6",
            "json_movement_cancel_steps",
            "movement_cancel_json",
            "movement_cancel = params:'movement_cancel'",
            "movement_cancel_json(movement_cancel)",
            "movement_cancel_json(m:15)",
            '"movement_cancel":%s',
            '"unsafe_steps_complete":%s',
            '"omitted_count":%d',
            "_ < edge_count || _ >= tail_start",
        ):
            self.assertIn(expected, source)

        self.assertIn("path_moves = parse_path_moves(params, points)", source)
        self.assertIn("path_fall_depths = parse_path_fall_depths(params, points)", source)
        self.assertIn("cancel_policies = parse_cancel_policies(params, path_moves)", source)
        self.assertIn("points, 0, movement_cancel, path_moves, path_fall_depths, cancel_policies)", source)
        self.assertIn("m:13, m:14, m:15, m:16, m:17, m:18)", source)

    def test_move_to_controller_pulses_jump_for_ascending_waypoints(self):
        source = MINEBOT_SC.read_text()

        self.assertIn("movement_kind = current_movement_kind(m)", source)
        self.assertIn("movement_kind == 'swim' || movement_kind == 'ascend'", source)
        self.assertIn("run('player ' + name + ' jump continuous')", source)
        self.assertIn("movement_kind == 'fall' || movement_kind == 'descend'", source)
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
        self.assertIn("policy == 'land_first' || policy == 'settle_on_support' || policy == 'egress_to_dry' || policy == 'after_step'", source)
        self.assertIn("if(kind == 'swim', 'egress_to_dry'", source)
        self.assertIn("dry_stand = is_dry_stand_cell(floor(p:0), floor(p:1), floor(p:2))", source)
        self.assertNotIn("surface_or_stable_water", source)

        move_tick = re.search(r"run_move_tick\(name, m\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(move_tick, "run_move_tick function not found")
        for reason in ("timeout", "stuck", "deviated"):
            self.assertIn(f"request_move_cancel(name, '{reason}')", move_tick.group(1))
            self.assertNotIn(f"finish_move(name, '{reason}'", move_tick.group(1))
        self.assertEqual(move_tick.group(1).count("continue_delayed_move_controls("), 4)

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
        self.assertIn("mine_trace_matches(name, x, y, z)", source)
        self.assertIn("mine_aim_candidate(name, x, y, z, 0)", source)
        self.assertIn("finish_mine(name, 'target_occluded')", source)
        self.assertIn("if(!mine_trace_matches(name, m:1, m:2, m:3),", source)
        self.assertIn("if(!m:8,", source)
        self.assertIn("mine_aim_candidate(name, m:1, m:2, m:3, 0)", source)
        start = re.search(r"start_mine_block\(name, action_id, x, y, z, params\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(start, "start_mine_block function not found")
        self.assertNotIn("x + 0.5, y + 0.5, z + 0.5", start.group(1))
        self.assertNotIn("attack continuous", start.group(1))
        self.assertNotIn("setblock", start.group(1))
        run_tick = re.search(r"run_mine_tick\(name, m\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(run_tick, "run_mine_tick function not found")
        self.assertNotIn("look at", run_tick.group(1))
        self.assertEqual(run_tick.group(1).count("attack continuous"), 1)
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
            "active_move_owns_water_egress(name)",
            "current_cancel_policy(m) != 'egress_to_dry'",
            "m:8 < movement_water_escape_ticks(m)",
            "is_dry_stand_cell(floor(target:0), floor(target:1), floor(target:2))",
            "air_risk = head_in_water_now(name) && air != null && air <= global_water_reflex_air_threshold;",
            "damage_risk = global_water_reflex_health_baselines:name - hp >= global_water_reflex_damage_budget",
            "water_surface_target(p)",
            "water_near_cell(x, y, z)",
            "water_near_escape_cell(x, y, z)",
            "water_shore_candidate_offsets()",
            "water_shore_escape_target(p)",
            "water_escape_target(p)",
            "water_hazard_clear(name)",
            "bs != 'water' && bs != 'minecraft:water'",
            "queue_immediate_water_reflex(name)",
            "movement_water_escape_should_trigger(name, m, stuck_ticks)",
            "start_water_reflex(name)",
            "water_reflex_should_trigger(name) &&",
            "start_water_reflex(name) -> start_hazard_reflex(name, 'water');",
            "initialize_water_reflex_controls(name) -> (",
            "run('player ' + name + ' jump continuous');",
            "if(kind == 'water', initialize_water_reflex_controls(name));",
            "if(kind == 'water', water_escape_target(p), safe_escape_target(p))",
            "if(kind == 'water' && target == null,",
            "target = water_surface_target(p)",
            "if(kind == 'fire', 'fireReflex', if(kind == 'water', 'waterReflex', 'lavaReflex'))",
            "if(kind == 'fire', !on_fire_now(name), if(kind == 'water', water_hazard_clear(name), !lava_near_pos(p)))",
            "move_cancel_egress = kind == 'water' && owner_name == 'moveTo'",
            "water_surface_reached = kind == 'water' && !water_target_is_shore && dist <= 0.9 && clear_of_hazard",
            "escaped = if(kind == 'water', water_target_is_shore && dist <= 0.9 && water_on_dry_stand",
            "retarget = water_escape_target(p)",
            "global_pending_reflexes:name = 'water'",
            "global_pending_reflexes:name = 'fire'",
            "global_pending_reflexes:name = 'lava'",
            "release_owner(name, owner_name)",
        ):
            self.assertIn(expected, source)

        hazard = re.search(r"hazard_kind_near_name\(name\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(hazard, "hazard_kind_near_name function not found")
        self.assertIn("water_reflex_should_trigger(name)", hazard.group(1))
        self.assertIn("if(active_move_owns_water_egress(name), null, 'water')", hazard.group(1))
        self.assertNotIn("if(in_water_now(name),", hazard.group(1))
        escape = re.search(r"water_escape_target\(p\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(escape, "water_escape_target function not found")
        self.assertIn("water_shore_escape_target(p)", escape.group(1))
        self.assertNotIn("water_surface_target(p)", escape.group(1))
        shore = re.search(r"water_shore_escape_target\(p\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(shore, "water_shore_escape_target function not found")
        self.assertIn("candidates = water_shore_candidate_offsets();", shore.group(1))
        corridor = re.search(r"water_escape_corridor_clear\(bx, bz, tx, tz, sy\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(corridor, "water_escape_corridor_clear function not found")
        self.assertIn("cx = bx + floor(dx * (_ + 1) / steps);", corridor.group(1))
        self.assertIn("cz = bz + floor(dz * (_ + 1) / steps);", corridor.group(1))
        move_tick = re.search(r"run_move_tick\(name, m\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(move_tick, "run_move_tick function not found")
        self.assertIn("movement_water_escape_should_trigger(name, updated_move, stuck_ticks)", move_tick.group(1))
        self.assertIn("start_water_reflex(name)", move_tick.group(1))
        movement_water = re.search(r"movement_water_escape_should_trigger\(name, m, stuck_ticks\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(movement_water, "movement_water_escape_should_trigger function not found")
        self.assertIn("water_reflex_should_trigger(name)", movement_water.group(1))

    def test_lava_escape_target_requires_an_occupiable_non_lava_cell(self):
        source = MINEBOT_SC.read_text()

        escape = re.search(r"safe_escape_target\(p\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(escape, "safe_escape_target function not found")
        self.assertIn("is_reflex_escape_cell(tx, by, tz)", escape.group(1))
        candidate = re.search(r"is_reflex_escape_cell\(x, y, z\) -> \((.*?)\n\);", source, re.S)
        self.assertIsNotNone(candidate, "is_reflex_escape_cell function not found")
        self.assertIn("here_kind != 'SOLID' && head_kind != 'SOLID'", candidate.group(1))
        self.assertIn("here != 'lava' && here != 'minecraft:lava'", candidate.group(1))
        self.assertIn("is_solid_floor(x, y, z)", candidate.group(1))

    def test_survival_reflex_cancels_waiting_navigation_mutations(self):
        source = MINEBOT_SC.read_text()
        hazard = re.search(r"start_hazard_reflex\(name, kind\) -> \((.*?)\n\);", source, re.S)

        self.assertIsNotNone(hazard, "start_hazard_reflex function not found")
        body = hazard.group(1)
        self.assertIn("cancel_move_preempted(name);", body)
        self.assertIn("request_navigation_mutation_cancel(name, 'preempted');", body)
        self.assertLess(
            body.index("cancel_move_preempted(name);"),
            body.index("request_navigation_mutation_cancel(name, 'preempted');"),
        )

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
