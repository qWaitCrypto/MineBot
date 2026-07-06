global_events = [];
global_seq = 0;
global_tick = 0;
global_moves = {};
global_move_cancels = {};
global_navigations = {};
global_follows = {};
global_mines = {};
global_places = {};
global_uses = {};
global_ignites = {};
global_sows = {};
global_attacks = {};
global_ranged = {};
global_drops = {};
global_owners = {};
global_reflexes = {};
global_pending_reflexes = {};
global_watched = {};
global_reflex_scan = true;
global_water_reflex_air_threshold = 80;
global_water_reflex_damage_budget = null;
global_water_reflex_health_baselines = {};
global_combat_health_baselines = {};
global_engages = {};
global_hostile_types = l('minecraft:zombie', 'minecraft:husk', 'minecraft:drowned', 'minecraft:zombie_villager', 'minecraft:skeleton', 'minecraft:stray', 'minecraft:bogged', 'minecraft:creeper', 'minecraft:spider', 'minecraft:cave_spider', 'minecraft:witch', 'minecraft:silverfish', 'minecraft:endmite', 'minecraft:slime', 'minecraft:magma_cube', 'minecraft:enderman', 'minecraft:endermite', 'minecraft:blaze', 'minecraft:ghast', 'minecraft:pillager', 'minecraft:vindicator', 'minecraft:evoker', 'minecraft:ravager', 'minecraft:shulker', 'minecraft:phantom', 'minecraft:wither_skeleton', 'minecraft:zoglin', 'minecraft:hoglin', 'minecraft:piglin_brute');
global_ranged_types = l('minecraft:skeleton', 'minecraft:stray', 'minecraft:bogged', 'minecraft:witch', 'minecraft:blaze', 'minecraft:ghast', 'minecraft:pillager', 'minecraft:shulker');
global_response_char_budget = 2000;
global_pending_spawns = {};
global_respawn_notices = {};
global_missing_notices = {};
global_agent_chat_events = [];
global_agent_chat_seq = 0;
global_action_results = {};

json_bool(v) -> if(v, 'true', 'false');

json_null(v) -> if(v == null, 'null', str('"%s"', v));

json_string(v) -> (
  if(v == null,
    'null'
  ,
    encode_json(str('%s', v))
  )
);

json_pos(p) -> str('[%.3f,%.3f,%.3f]', p:0, p:1, p:2);

json_error(err) -> if(err == null, 'null', str('"%s"', err));

json_number_null(v) -> if(v == null, 'null', str('%.3f', v));

json_int_null(v) -> if(v == null, 'null', str('%d', v));

effects_json(pe) -> (
  effects = query(pe, 'effect');
  out = '';
  first = true;
  if(effects != null,
    loop(length(effects),
      eff = effects:_;
      if(first, first = false, out += ',');
      out += str('{"id":%s,"amplifier":%d,"duration":%d}',
        json_string(eff:0),
        floor(number(eff:1)),
        floor(number(eff:2)))
    )
  );
  str('[%s]', out)
);

json_waypoints(points) -> (
  out = '';
  first = true;
  loop(length(points),
    if(first, first = false, out += ',');
    out += json_pos(points:_)
  );
  str('[%s]', out)
);

json_waypoint_summary(points) -> (
  count = length(points);
  first = if(count > 0, points:0, l(0, 0, 0));
  last = if(count > 0, points:(count - 1), l(0, 0, 0));
  str('{"count":%d,"first":%s,"last":%s}', count, json_pos(first), json_pos(last))
);

json_movement_cancel_steps(steps) -> (
  out = '';
  first = true;
  if(steps != null,
    loop(length(steps),
      step = steps:_;
      if(first, first = false, out += ',');
      out += str('{"index":%d,"pos":%s,"move":%s,"policy":%s}',
        floor(number(step:'index')), json_pos(step:'pos'), json_string(step:'move'), json_string(step:'policy'))
    )
  );
  str('[%s]', out)
);

movement_cancel_json(profile) -> (
  if(profile == null,
    '{"safe_to_cancel":true,"unsafe_count":0,"unsafe_steps":[]}'
  ,
    str('{"safe_to_cancel":%s,"unsafe_count":%d,"unsafe_steps":%s}',
      json_bool(bool(profile:'safe_to_cancel')),
      floor(number(profile:'unsafe_count')),
      json_movement_cancel_steps(profile:'unsafe_steps'))
  )
);

result_json(id, name, ok, accepted, data, err) -> (
  str('{"type":"result","id":%s,"bot":"%s","ok":%s,"accepted":%s,"complete":true,"data":%s,"error":%s}',
    json_null(id), name, json_bool(ok), json_bool(accepted), data, json_error(err))
);

perception_json(name, scope, ok, complete, data, uncertainty, next, err) -> (
  str('{"type":"perception","bot":"%s","scope":"%s","ok":%s,"complete":%s,"data":%s,"uncertainty":%s,"next":%s,"error":%s}',
    name, scope, json_bool(ok), json_bool(complete), data, uncertainty, json_null(next), json_error(err))
);

missing_body_perception(name, scope) -> (
  perception_json(name, scope, false, true, '{}', '[{"reason":"missing_body"}]', null, 'missing_body')
);

finalize_pending_spawn(name) -> (
  pending = global_pending_spawns:name;
  if(pending != null,
    pe = player_entity(name);
    if(pe != null,
      pos = pending:0;
      yaw = pending:1;
      pitch = pending:2;
      gamemode = pending:3;
      emit_respawned = pending:4;
      phase = if(length(pending) > 5, pending:5, 0);
      if(phase == 0,
        if(pos != null,
          if(yaw != null && pitch != null,
            run(str('tp %s %d %d %d %.3f %.3f', name, floor(number(pos:0)), floor(number(pos:1)), floor(number(pos:2)), number(yaw), number(pitch)))
          ,
            run(str('tp %s %d %d %d', name, floor(number(pos:0)), floor(number(pos:1)), floor(number(pos:2))))
          )
        );
        if(gamemode != null,
          run('gamemode ' + gamemode + ' ' + name)
        );
        run('player ' + name + ' stop');
        watch_bot(name);
        global_pending_spawns:name = l(pos, yaw, pitch, gamemode, emit_respawned, 1)
      ,
        if(bool(emit_respawned),
          emit('respawned', name, l(bot_pos(name)))
        );
        global_pending_spawns:name = null
      )
    )
  )
);

state_json(name) -> (
  finalize_pending_spawn(name);
  pe = player_entity(name);
  if(pe == null,
    str('{"type":"state","bot":"%s","ok":true,"complete":true,"data":{"pos":[0.000,0.000,0.000],"yaw":null,"pitch":null,"health":0.000,"food":0,"oxygen":null,"inventory_raw":"","inventory_hash":%s,"effects":null,"time":%d,"weather":null,"dimension":null,"sleeping":null,"missing":true},"error":null}',
      name, json_string(''), floor(number(day_time()) % 24000))
  ,
    p = query(pe, 'pos');
    health = query(pe, 'health');
    nbt = query(pe, 'nbt');
    food = nbt:'foodLevel';
    air = if(nbt:'Air' == null, null, floor(number(nbt:'Air')));
    sleep_timer = if(nbt:'SleepTimer' == null, 0, number(nbt:'SleepTimer'));
    sleeping = sleep_timer > 0;
    inv = inventory_get(name);
    raw = str('%s', inv);
    str('{"type":"state","bot":"%s","ok":true,"complete":true,"data":{"pos":%s,"yaw":null,"pitch":null,"health":%.3f,"food":%d,"oxygen":%s,"inventory_raw":"","inventory_hash":%s,"effects":%s,"time":%d,"weather":null,"dimension":null,"sleeping":%s,"missing":false},"error":null}',
      name, json_pos(p), health, food, json_int_null(air), json_string(hash_code(raw)), effects_json(pe), floor(number(day_time()) % 24000), json_bool(sleeping))
  )
);

event_data_json(kind, data) -> (
  out = '{}';
  if(kind == 'death',
    out = str('{"pos":%s,"inventory_before":%s,"inventory_hash":%s,"inventory_counts_before":%s}',
      json_pos(data:0), json_string(''), json_string(data:2), data:3)
  );
  if(kind == 'respawned',
    out = str('{"final_pos":%s}', json_pos(data:0))
  );
  if(kind == 'bodyMissing',
    out = str('{"lastPos":%s}', json_pos(data:0))
  );
  if(kind == 'agentChat',
    out = str('{"sender":%s,"message":%s}', json_string(data:0), json_string(data:1))
  );
  if(kind == 'moveStarted',
    out = str('{"action_id":"%s","start_pos":%s,"target":%s,"waypoints":%s,"guard":%s,"movement_cancel":%s}', data:0, json_pos(data:1), json_pos(data:2), json_waypoint_summary(data:3), data:4, data:5)
  );
  if(kind == 'moveDone',
    out = str('{"action_id":"%s","arrived":%s,"final_pos":%s,"target":%s,"dist_to_target":%.3f,"stopped_reason":"%s","ticks":%d,"min_dist":%.3f,"stuck_ticks":%d,"deviation":%.3f,"waypoint_index":%d,"waypoint_count":%d,"guard":%s,"movement_cancel":%s}',
      data:0, json_bool(data:1), json_pos(data:2), json_pos(data:3), data:4, data:5, data:6, data:7, data:8, data:9, data:10, data:11, data:12, data:13)
  );
  if(kind == 'moveFinishTrace',
    out = str('{"action_id":"%s","reason":"%s","arrived":%s,"final_pos":%s,"target":%s,"dist_to_target":%.3f,"ticks":%d,"stuck_ticks":%d}',
      data:0, data:1, json_bool(data:2), json_pos(data:3), json_pos(data:4), data:5, data:6, data:7)
  );
  if(kind == 'navigateStartTrace',
    out = str('{"action_id":"%s","plan_status":"%s","goal":%s,"expanded":%d,"path_length":%d}',
      data:0, data:1, json_pos(data:2), data:3, data:4)
  );
  if(kind == 'navigateFinishTrace',
    out = str('{"action_id":"%s","arrived":%s,"reason":"%s","final_pos":%s,"goal":%s,"goal_dist":%.3f,"expanded":%d,"waypoints":%d}',
      data:0, json_bool(data:1), data:2, json_pos(data:3), json_pos(data:4), data:5, data:6, data:7)
  );
  if(kind == 'navigateDone',
    out = str('{"action_id":"%s","arrived":%s,"final_pos":%s,"goal":%s,"goal_dist":%.3f,"reason":"%s","expanded":%d,"waypoints":%d,"segments":%d,"nav_reason":"%s","move_ticks":%d,"move_min_dist":%.3f,"move_stuck_ticks":%d,"move_deviation":%.3f,"move_waypoint_index":%d,"move_waypoint_count":%d,"move_current_waypoint":%s}',
      data:0, json_bool(data:1), json_pos(data:2), json_pos(data:3), data:4, data:5, data:6, data:7, data:8, data:9, data:10, data:11, data:12, data:13, data:14, data:15, json_pos(data:16))
  );
  if(kind == 'mobilityBlocked',
    out = str('{"reason":"%s","pos":%s,"goal":%s,"expanded":%d}',
      data:0, json_pos(data:1), json_pos(data:2), data:3)
  );
  if(kind == 'followStarted',
    out = str('{"action_id":"%s","target":%s,"target_pos":%s,"keep_radius":%.3f}',
      data:0, json_string(data:1), json_pos(data:2), data:3)
  );
  if(kind == 'followDone',
    out = str('{"action_id":"%s","arrived":%s,"final_pos":%s,"reason":"%s"}',
      data:0, json_bool(data:1), json_pos(data:2), data:3)
  );
  if(kind == 'engageStarted',
    out = str('{"action_id":"%s","target":%s,"target_pos":%s,"attack_range":%.3f}',
      data:0, json_string(data:1), json_pos(data:2), data:3)
  );
  if(kind == 'engageDone',
    out = str('{"action_id":"%s","success":%s,"target":%s,"final_pos":%s,"reason":"%s","target_health":%s,"attacks":%d}',
      data:0, json_bool(data:1), json_string(data:2), json_pos(data:3), data:4, json_number_null(data:5), data:6)
  );
  if(kind == 'underAttack',
    out = str('{"attacker":%s,"health":%s,"baseline":%s}',
      json_string(data:0), json_number_null(data:1), json_number_null(data:2))
  );
  if(kind == 'moveCancelDelayed',
    out = str('{"action_id":"%s","stopped_reason":"%s","movement_cancel":%s,"requested_tick":%d}',
      data:0, data:1, data:2, data:3)
  );
  if(kind == 'lookDone',
    out = str('{"action_id":"%s","success":%s,"target":%s,"final_pos":%s,"stopped_reason":"%s"}',
      data:0, json_bool(data:1), json_pos(data:2), json_pos(data:3), data:4)
  );
  if(kind == 'jumpDone',
    out = str('{"action_id":"%s","success":%s,"final_pos":%s,"stopped_reason":"%s"}',
      data:0, json_bool(data:1), json_pos(data:2), data:3)
  );
  if(kind == 'selectSlotDone',
    out = str('{"action_id":"%s","success":%s,"slot":%d,"stopped_reason":"%s"}',
      data:0, json_bool(data:1), data:2, data:3)
  );
  if(kind == 'selectItemDone',
    out = str('{"action_id":"%s","success":%s,"item":"%s","slot":%d,"count":%d,"stopped_reason":"%s"}',
      data:0, json_bool(data:1), data:2, data:3, data:4, data:5)
  );
  if(kind == 'stopDone',
    out = str('{"action_id":"%s","success":%s,"final_pos":%s,"stopped_reason":"%s"}',
      data:0, json_bool(data:1), json_pos(data:2), data:3)
  );
  if(kind == 'mineDone',
    out = str('{"action_id":"%s","success":%s,"target":%s,"block_type":"%s","block_now":"%s","block_gone":%s,"final_pos":%s,"stopped_reason":"%s","ticks":%d}',
      data:0, json_bool(data:1), json_pos(data:2), data:3, data:4, json_bool(data:5), json_pos(data:6), data:7, data:8)
  );
  if(kind == 'placeDone',
    out = str('{"action_id":"%s","success":%s,"target":%s,"expected_type":"%s","block_at_target":"%s","face":"%s","final_pos":%s,"stopped_reason":"%s","ticks":%d}',
      data:0, json_bool(data:1), json_pos(data:2), data:3, data:4, data:5, json_pos(data:6), data:7, data:8)
  );
  if(kind == 'useDone',
    out = str('{"action_id":"%s","success":%s,"mode":"%s","item":"%s","start_pos":%s,"final_pos":%s,"inventory_before":%s,"inventory_after":%s,"stopped_reason":"%s","ticks":%d}',
      data:0, json_bool(data:1), data:2, data:3, json_pos(data:4), json_pos(data:5), json_string(data:6), json_string(data:7), data:8, data:9)
  );
  if(kind == 'igniteDone',
    out = str('{"action_id":"%s","success":%s,"target":%s,"expected_type":"%s","block_at_target":"%s","item":"%s","method":"%s","final_pos":%s,"stopped_reason":"%s","ticks":%d,"block_before":"%s"}',
      data:0, json_bool(data:1), json_pos(data:2), data:3, data:4, data:5, data:6, json_pos(data:7), data:8, data:9, data:10)
  );
  if(kind == 'sowDone',
    out = str('{"action_id":"%s","success":%s,"target":%s,"crop_pos":%s,"expected_type":"%s","block_at_crop":"%s","item":"%s","method":"%s","final_pos":%s,"stopped_reason":"%s","ticks":%d,"crop_before":"%s","inventory_before":%s,"inventory_after":%s}',
      data:0, json_bool(data:1), json_pos(data:2), json_pos(data:3), data:4, data:5, data:6, data:7, json_pos(data:8), data:9, data:10, data:11, json_string(data:12), json_string(data:13))
  );
  if(kind == 'attackDone',
    out = str('{"action_id":"%s","success":%s,"target_type":"%s","target_id":%s,"target_name":%s,"target_pos":%s,"target_health":%s,"target_initial_health":%s,"damage_observed":%s,"persistent_target":%s,"final_pos":%s,"stopped_reason":"%s","ticks":%d,"attacks":%d,"cooldown_ticks":%d,"min_attack_interval_ticks":%s,"max_attack_interval_ticks":%s}',
      data:0, json_bool(data:1), data:2, json_string(data:3), json_string(data:4), data:5, json_number_null(data:6), json_number_null(data:7), json_bool(data:8), json_bool(data:9), json_pos(data:10), data:11, data:12, data:13, data:14, json_number_null(data:15), json_number_null(data:16))
  );
  if(kind == 'rangedDone',
    out = str('{"action_id":"%s","success":%s,"weapon":"%s","target_type":"%s","target_id":%s,"target_name":%s,"target_pos":%s,"target_health":%s,"target_initial_health":%s,"damage_observed":%s,"fired_observed":%s,"final_pos":%s,"stopped_reason":"%s","ticks":%d,"use_interval_ticks":%d,"expected_shots":%d}',
      data:0, json_bool(data:1), data:2, data:3, json_string(data:4), json_string(data:5), data:6, json_number_null(data:7), json_number_null(data:8), json_bool(data:9), json_bool(data:10), json_pos(data:11), data:12, data:13, data:14, data:15)
  );
  if(kind == 'containerDone',
    out = str('{"action_id":"%s","success":%s,"direction":"%s","container_pos":%s,"container_slot":%d,"bot_slot":%d,"item":"%s","count":%d,"stopped_reason":"%s","container_before":%s,"container_after":%s,"bot_before":%s,"bot_after":%s}',
      data:0, json_bool(data:1), data:2, json_pos(data:3), data:4, data:5, data:6, data:7, data:8, data:9, data:10, data:11, data:12)
  );
  if(kind == 'dropDone',
    out = str('{"action_id":"%s","success":%s,"slot":%d,"mode":"%s","item":"%s","count_before":%d,"count_after":%d,"stopped_reason":"%s","slot_before":%s,"slot_after":%s}',
      data:0, json_bool(data:1), data:2, data:3, data:4, data:5, data:6, data:7, data:8, data:9)
  );
  if(kind == 'handoffDone',
    out = str('{"action_id":"%s","success":%s,"receiver":"%s","item":"%s","requested_count":%d,"spawned_count":%d,"source_slot":%d,"stopped_reason":"%s","slot_before":%s,"slot_after":%s,"receiver_pos":%s}',
      data:0, json_bool(data:1), data:2, data:3, data:4, data:5, data:6, data:7, data:8, data:9, json_pos(data:10))
  );
  if(kind == 'moveItemDone',
    out = str('{"action_id":"%s","success":%s,"from_slot":%d,"to_slot":%d,"item":"%s","count":%d,"stopped_reason":"%s","from_before":%s,"from_after":%s,"to_before":%s,"to_after":%s}',
      data:0, json_bool(data:1), data:2, data:3, data:4, data:5, data:6, data:7, data:8, data:9, data:10)
  );
  if(kind == 'craftDone',
    out = str('{"action_id":"%s","success":%s,"item":"%s","count":%d,"output_slot":%d,"stopped_reason":"%s","inputs_before":%s,"inputs_after":%s,"output_before":%s,"output_after":%s}',
      data:0, json_bool(data:1), data:2, data:3, data:4, data:5, data:6, data:7, data:8, data:9)
  );
  if(kind == 'furnaceDone',
    out = str('{"action_id":"%s","success":%s,"direction":"%s","furnace_pos":%s,"furnace_slot":"%s","furnace_slot_index":%d,"bot_slot":%d,"item":"%s","count":%d,"stopped_reason":"%s","furnace_before":%s,"furnace_after":%s,"bot_before":%s,"bot_after":%s}',
      data:0, json_bool(data:1), data:2, json_pos(data:3), data:4, data:5, data:6, data:7, data:8, data:9, data:10, data:11, data:12, data:13)
  );
  if(kind == 'itemPickup',
    out = str('{"player":"%s","item":%s,"count":%d,"stack":%s}',
      data:0, json_string(stack_item(data:1)), stack_count(data:1), stack_json(data:1))
  );
  if(kind == 'ownerPreempted',
    out = str('{"previous_owner":"%s","new_owner":"%s"}', data:0, data:1)
  );
  if(kind == 'reflexTriggered',
    out = str('{"kind":"%s","pos":%s,"target":%s,"target_is_dry_stand":%s,"target_block":"%s","target_below":"%s"}',
      data:0, json_pos(data:1), json_pos(data:2), json_bool(data:3), data:4, data:5)
  );
  if(kind == 'reflexCompleted',
    out = str('{"final_pos":%s,"dist_to_escape":%.3f,"ticks":%d,"escaped_hazard":%s,"escaped_lava":%s,"kind":"%s","target":%s,"target_is_dry_stand":%s,"final_is_dry_stand":%s,"target_block":"%s","target_below":"%s"}',
      json_pos(data:0), data:1, data:2, json_bool(data:3), json_bool(data:3 && data:4 == 'lava'), data:4, json_pos(data:5), json_bool(data:6), json_bool(data:7), data:8, data:9)
  );
  out
);

event_json(e) -> (
  if(length(e) == 1, e = e:0);
  str('{"type":"event","seq":%d,"tick":%d,"bot":"%s","name":"%s","data":%s}',
    e:0, e:1, e:3, e:2, event_data_json(e:2, e:4))
);

events_json(name, evs) -> (
  out = '';
  first = true;
  last_seq = null;
  complete = true;
  loop(length(evs),
    e = evs:_;
    if(length(e) == 1, e = e:0);
    if(complete && e:3 == name,
      item = event_json(e);
      candidate = if(first, item, out + ',' + item);
      if(length(candidate) > 2600,
        complete = false
      ,
        if(first, first = false, out += ',');
        out += item;
        last_seq = e:0
      )
    )
  );
  next_value = if(complete, null, last_seq);
  str('{"type":"events","bot":"%s","ok":true,"complete":true,"next":%s,"events":[%s],"error":null}', name, json_int_null(next_value), out)
);

events_since_json(name, evs, since_seq) -> (
  out = '';
  first = true;
  last_seq = null;
  complete = true;
  loop(length(evs),
    e = evs:_;
    if(length(e) == 1, e = e:0);
    if(complete && e:3 == name && e:0 > since_seq,
      item = event_json(e);
      candidate = if(first, item, out + ',' + item);
      if(length(candidate) > 2600,
        complete = false
      ,
        if(first, first = false, out += ',');
        out += item;
        last_seq = e:0
      )
    )
  );
  next_value = if(complete, null, last_seq);
  str('{"type":"events","bot":"%s","ok":true,"complete":true,"next":%s,"events":[%s],"error":null}', name, json_int_null(next_value), out)
);

priority_value(priority) -> (
  if(priority == 'SURVIVAL', 100,
    if(priority == 'ACTION', 10, 0)
  )
);

player_entity(name) -> (
  found = entity_selector(str('@a[name=%s,limit=1]', name));
  if(length(found) == 0, null, found:0)
);

bot_pos(name) -> (
  pe = player_entity(name);
  if(pe == null, null, query(pe, 'pos'))
);

block_kind(bs) -> (
  if(bs == 'air' || bs == 'minecraft:air' || bs == 'cave_air' || bs == 'minecraft:cave_air' || bs == 'void_air' || bs == 'minecraft:void_air',
    'CLEAR'
  ,
    if(bs == 'water' || bs == 'minecraft:water' || bs == 'lava' || bs == 'minecraft:lava',
      'LIQUID'
    ,
      'SOLID'
    )
  )
);

los_clear(x, y, z, tx, ty, tz) -> (
  dx = tx - x;
  dy = ty - y;
  dz = tz - z;
  adx = abs(dx);
  ady = abs(dy);
  adz = abs(dz);
  steps = max(adx, max(ady, adz));
  if(steps < 1, true,
    clear = true;
    loop(steps,
      if(clear,
        f = (_ + 1) / steps;
        bx = floor(x + dx * f);
        by = floor(y + dy * f);
        bz = floor(z + dz * f);
        if((bx == floor(x) && by == floor(y) && bz == floor(z)) || (bx == floor(tx) && by == floor(ty) && bz == floor(tz)),
          null,
          if(block_kind('' + block(bx, by, bz)) == 'SOLID', clear = false)
        )
      )
    );
    clear
  )
);

block_properties_json(b) -> (
  state = block_state(b);
  props = keys(state);
  out = '';
  first = true;
  loop(length(props),
    pname = props:_;
    pvalue = state:pname;
    if(first, first = false, out += ',');
    out += str('%s:%s', json_string(pname), json_string(pvalue))
  );
  str('{%s}', out)
);

block_fact_json(x, y, z) -> (
  b = block(x, y, z);
  bs = '' + b;
  str('{"x":%d,"y":%d,"z":%d,"type":"%s","state":"%s","properties":%s}', x, y, z, bs, block_kind(bs), block_properties_json(b))
);

block_type_matches(bs, wanted) -> (
  bs == wanted || bs == 'minecraft:' + wanted || 'minecraft:' + bs == wanted
);

perceive_block_at(name, params) -> (
  x = floor(number(params:'x'));
  y = floor(number(params:'y'));
  z = floor(number(params:'z'));
  perception_json(name, 'blockAt', true, true, block_fact_json(x, y, z), '[]', null, null)
);

perceive_block_cells(name, params) -> (
  cells = params:'cells';
  if(cells == null || length(cells) == 0,
    perception_json(name, 'blockCells', true, true, '{"count":0,"total":0,"next":null,"cells":[]}', '[]', null, null)
  ,
    total = length(cells);
    start = 0;
    if(params:'start' != null, start = floor(number(params:'start')));
    if(start < 0, start = 0);
    if(start > total, start = total);
    limit = 64;
    if(params:'limit' != null, limit = floor(number(params:'limit')));
    if(limit < 1, limit = 1);
    if(limit > 256, limit = 256);
    out = '';
    count = 0;
    first = true;
    idx = start;
    overflow = false;
    loop(total - start,
      if(overflow,
        null
      ,
        cell = cells:(start + _);
        x = floor(number(cell:0));
        y = floor(number(cell:1));
        z = floor(number(cell:2));
        fact = block_fact_json(x, y, z);
        if(count >= limit || length(out) + length(fact) >= global_response_char_budget,
          overflow = true
        ,
          if(first, first = false, out += ',');
          out += fact;
          count += 1;
          idx += 1
        )
      )
    );
    next_start = if(idx >= total, null, idx);
    next_value = if(idx >= total, null, str('%d', idx));
    data = str('{"count":%d,"total":%d,"nextStart":%s,"cells":[%s]}', count, total, json_int_null(next_start), out);
    uncertainty = if(overflow, '[{"reason":"limit_exceeded"}]', '[]');
    if(overflow && count == 0,
      perception_json(name, 'blockCells', false, true, data, '[{"reason":"single_cell_exceeds_budget"}]', null, 'single_cell_exceeds_budget')
    ,
      perception_json(name, 'blockCells', true, !overflow, data, uncertainty, next_value, null)
    )
  )
);

perceive_debug_blocks(name, params) -> (
  p = bot_pos(name);
  if(p == null,
    missing_body_perception(name, 'debugBlocks')
  ,
    radius = floor(number(params:'radius'));
    if(radius < 0, radius = 0);
    if(radius > 4, radius = 4);
    limit = 64;
    if(params:'limit' != null, limit = floor(number(params:'limit')));
    if(limit < 1, limit = 1);
    if(limit > 256, limit = 256);
    cx = floor(p:0);
    cy = floor(p:1);
    cz = floor(p:2);
    side = radius * 2 + 1;
    total = side * side * side;
    start = 0;
    if(params:'start' != null, start = floor(number(params:'start')));
    if(start < 0, start = 0);
    if(start > total, start = total);
    out = '';
    count = 0;
    overflow = false;
    first = true;
    idx = start;
    plane = side * side;
    loop(total - start,
      if(!overflow,
        scan_i = start + _;
        ox_i = floor(scan_i / plane);
        rem_i = scan_i - ox_i * plane;
        oy_i = floor(rem_i / side);
        oz_i = rem_i - oy_i * side;
        x = cx + ox_i - radius;
        y = cy + oy_i - radius;
        z = cz + oz_i - radius;
        fact = block_fact_json(x, y, z);
        if(count >= limit || length(out) + length(fact) >= global_response_char_budget,
          overflow = true
        ,
          if(first, first = false, out += ',');
          out += fact;
          count += 1;
          idx += 1
        )
      )
    );
    complete = idx >= total;
    next_start = if(complete, null, idx);
    next_value = if(complete, null, str('%d', idx));
    cursor = block_fact_json(cx, cy, cz);
    feet = block_fact_json(cx, cy - 1, cz);
    head = block_fact_json(cx, cy + 1, cz);
    data = str('{"center":%s,"radius":%d,"start":%d,"limit":%d,"count":%d,"total":%d,"nextStart":%s,"cursor":%s,"feet":%s,"head":%s,"blocks":[%s]}',
      json_pos(p), radius, start, limit, count, total, json_int_null(next_start), cursor, feet, head, out);
    uncertainty = if(complete, '[]', '[{"reason":"page_limit"}]');
    if(!complete && count == 0,
      perception_json(name, 'debugBlocks', false, true, data, '[{"reason":"single_entry_exceeds_budget"}]', null, 'single_entry_exceeds_budget')
    ,
      perception_json(name, 'debugBlocks', true, complete, data, uncertainty, next_value, null)
    )
  )
);

perceive_nearby_blocks(name, params) -> (
  p = bot_pos(name);
  if(p == null,
    missing_body_perception(name, 'nearbyBlocks')
  ,
    radius = floor(number(params:'radius'));
    if(radius < 0, radius = 0);
    if(radius > 8, radius = 8);
    limit = 128;
    if(params:'limit' != null, limit = floor(number(params:'limit')));
    if(limit < 1, limit = 1);
    if(limit > 256, limit = 256);
    cx = floor(p:0);
    cy = floor(p:1);
    cz = floor(p:2);
    side = radius * 2 + 1;
    total = side * side * side;
    start = 0;
    if(params:'start' != null, start = floor(number(params:'start')));
    if(start < 0, start = 0);
    if(start > total, start = total);
    out = '';
    count = 0;
    overflow = false;
    first = true;
    idx = start;
    plane = side * side;
    loop(total - start,
      if(!overflow,
        scan_i = start + _;
        ox_i = floor(scan_i / plane);
        rem_i = scan_i - ox_i * plane;
        oy_i = floor(rem_i / side);
        oz_i = rem_i - oy_i * side;
        x = cx + ox_i - radius;
        y = cy + oy_i - radius;
        z = cz + oz_i - radius;
        bs = '' + block(x, y, z);
        if(block_kind(bs) != 'CLEAR',
          fact = block_fact_json(x, y, z);
          if(count >= limit || length(out) + length(fact) >= global_response_char_budget,
            overflow = true
          ,
            if(first, first = false, out += ',');
            out += fact;
            count += 1;
            idx += 1
          )
        ,
          idx += 1
        )
      )
    );
    complete = idx >= total;
    next_start = if(complete, null, idx);
    next_value = if(complete, null, str('%d', idx));
    data = str('{"center":%s,"radius":%d,"start":%d,"limit":%d,"count":%d,"total":%d,"nextStart":%s,"blocks":[%s]}', json_pos(p), radius, start, limit, count, total, json_int_null(next_start), out);
    uncertainty = if(complete, '[]', '[{"reason":"page_limit"}]');
    if(!complete && count == 0,
      perception_json(name, 'nearbyBlocks', false, true, data, '[{"reason":"single_entry_exceeds_budget"}]', null, 'single_entry_exceeds_budget')
    ,
      perception_json(name, 'nearbyBlocks', true, complete, data, uncertainty, next_value, null)
    )
  )
);

perceive_find_blocks(name, params) -> (
  p = bot_pos(name);
  if(p == null,
    missing_body_perception(name, 'findBlocks')
  ,
    wanted = if(params:'type' == null, '', params:'type');
    radius = floor(number(params:'radius'));
    if(radius < 0, radius = 0);
    if(radius > 128, radius = 128);
    y_radius = if(radius > 16, 16, radius);
    if(params:'y_radius' != null, y_radius = floor(number(params:'y_radius')));
    if(params:'yRadius' != null, y_radius = floor(number(params:'yRadius')));
    if(y_radius < 0, y_radius = 0);
    if(y_radius > 64, y_radius = 64);
    limit = 32;
    if(params:'limit' != null, limit = floor(number(params:'limit')));
    if(limit < 1, limit = 1);
    if(limit > 128, limit = 128);
    start = 0;
    if(params:'start' != null, start = floor(number(params:'start')));
    if(start < 0, start = 0);
    window_limit = start + limit;
    cx = floor(p:0);
    cy = floor(p:1);
    cz = floor(p:2);
    found = l();
    count = 0;
    matched = 0;
    r2 = radius * radius;
    loop(radius * 2 + 1,
      ox = _ - radius;
      loop(radius * 2 + 1,
        oz = _ - radius;
        if(ox*ox + oz*oz <= r2,
          loop(y_radius * 2 + 1,
            oy = _ - y_radius;
            x = cx + ox;
            y = cy + oy;
            z = cz + oz;
            bs = '' + block(x, y, z);
            if(block_type_matches(bs, wanted),
              matched += 1;
              dx = x + 0.5 - p:0;
              dy = y + 0.5 - p:1;
              dz = z + 0.5 - p:2;
              dist2 = dx*dx + dy*dy + dz*dz;
              entry = l(dist2, x, y, z, bs, block_kind(bs));
              insert_at = count;
              loop(count,
                cur = found:_;
                if(insert_at == count && dist2 < cur:0, insert_at = _)
              );
              put(found:insert_at, entry, 'insert');
              count += 1;
              if(count > window_limit,
                delete(found:window_limit);
                count = window_limit
              )
            )
          )
        )
      )
    );
    out = '';
    first = true;
    out_count = 0;
    out_idx = start;
    overflow = false;
    if(start < count,
      loop(count - start,
        if(!overflow,
          entry = found:(start + _);
          fact = str('{"x":%d,"y":%d,"z":%d,"type":"%s","state":"%s","dist2":%.3f}', entry:1, entry:2, entry:3, entry:4, entry:5, entry:0);
          if(out_count >= limit || length(out) + length(fact) >= global_response_char_budget,
            overflow = true
          ,
            if(first, first = false, out += ',');
            out += fact;
            out_count += 1;
            out_idx += 1
          )
        )
      )
    );
    complete = !overflow && matched <= out_idx;
    next_start = if(complete, null, out_idx);
    next_value = if(complete, null, str('%d', out_idx));
    data = str('{"center":%s,"type":"%s","radius":%d,"yRadius":%d,"start":%d,"limit":%d,"count":%d,"totalMatches":%d,"nextStart":%s,"blocks":[%s]}', json_pos(p), wanted, radius, y_radius, start, limit, out_count, matched, json_int_null(next_start), out);
    uncertainty = if(complete, '[]', '[{"reason":"page_limit"}]');
    if(!complete && out_count == 0,
      perception_json(name, 'findBlocks', false, true, data, '[{"reason":"single_entry_exceeds_budget"}]', null, 'single_entry_exceeds_budget')
    ,
      perception_json(name, 'findBlocks', true, complete, data, uncertainty, next_value, null)
    )
  )
);
entity_kind(e) -> (
  kind = query(e, 'type');
  if(kind == null, 'unknown', kind)
);

entity_health(e) -> (
  nbt = query(e, 'nbt');
  if(nbt:'Health' == null, null, nbt:'Health')
);

entity_matches_type(e, wanted) -> (
  kind = entity_kind(e);
  wanted == null || wanted == '' || kind == wanted || kind == 'minecraft:' + wanted || 'minecraft:' + kind == wanted
);

list_contains(lst, item) -> (
  found = false;
  loop(length(lst),
    if(!found && lst:_ == item, found = true)
  );
  found
);

is_hostile(e) -> (
  found = false;
  loop(length(global_hostile_types),
    if(!found && entity_matches_type(e, global_hostile_types:_), found = true)
  );
  found
);

is_ranged_hostile(e) -> (
  found = false;
  loop(length(global_ranged_types),
    if(!found && entity_matches_type(e, global_ranged_types:_), found = true)
  );
  found
);

is_flying_hostile(e) -> entity_matches_type(e, 'minecraft:phantom') || entity_matches_type(e, 'minecraft:ghast') || entity_matches_type(e, 'minecraft:shulker');

entity_fact_json(e, center) -> (
  p = query(e, 'pos');
  dx = p:0 - center:0;
  dy = p:1 - center:1;
  dz = p:2 - center:2;
  dist2 = dx*dx + dy*dy + dz*dz;
  hp = entity_health(e);
  eid = query(e, 'uuid');
  str('{"id":%s,"type":"%s","name":%s,"pos":%s,"health":%s,"dist2":%.3f}',
    json_string(eid), entity_kind(e), json_string(query(e, 'name')), json_pos(p), json_number_null(hp), dist2)
);

perceive_nearby_entities(name, params) -> (
  p = bot_pos(name);
  if(p == null,
    missing_body_perception(name, 'nearbyEntities')
  ,
    radius = floor(number(params:'radius'));
    if(radius < 1, radius = 1);
    if(radius > 32, radius = 32);
    limit = 32;
    if(params:'limit' != null, limit = floor(number(params:'limit')));
    if(limit < 1, limit = 1);
    if(limit > 128, limit = 128);
    selector = str('@e[x=%d,y=%d,z=%d,distance=..%d,limit=%d,sort=nearest]',
      floor(number(p:0)), floor(number(p:1)), floor(number(p:2)), radius, limit + 1);
    found = entity_selector(selector);
    out = '';
    count = 0;
    overflow = false;
    first = true;
    loop(length(found),
      e = found:_;
      if(e != player(name),
        if(count >= limit || length(out) >= global_response_char_budget,
          overflow = true
        ,
          if(first, first = false, out += ',');
          out += entity_fact_json(e, p);
          count += 1
        )
      )
    );
    data = str('{"center":%s,"radius":%d,"limit":%d,"count":%d,"entities":[%s]}', json_pos(p), radius, limit, count, out);
    uncertainty = if(overflow, '[{"reason":"limit_exceeded"}]', '[]');
    perception_json(name, 'nearbyEntities', true, !overflow, data, uncertainty, if(overflow, 'limit', null), null)
  )
);

perceive_hostiles(name, params) -> (
  p = bot_pos(name);
  if(p == null,
    missing_body_perception(name, 'nearbyHostiles')
  ,
    radius = floor(number(params:'radius'));
    if(radius < 1, radius = 1);
    if(radius > 32, radius = 32);
    limit = 32;
    if(params:'limit' != null, limit = floor(number(params:'limit')));
    if(limit < 1, limit = 1);
    if(limit > 128, limit = 128);
    selector = str('@e[x=%d,y=%d,z=%d,distance=..%d,limit=128,sort=nearest]',
      floor(number(p:0)), floor(number(p:1)), floor(number(p:2)), radius);
    found = entity_selector(selector);
    out = '';
    count = 0;
    overflow = false;
    first = true;
    loop(length(found),
      e = found:_;
      if(e != player(name) && is_hostile(e),
        if(count >= limit || length(out) >= global_response_char_budget,
          overflow = true
        ,
          if(first, first = false, out += ',');
          out += entity_fact_json(e, p);
          count += 1
        )
      )
    );
    data = str('{"center":%s,"radius":%d,"limit":%d,"count":%d,"entities":[%s]}', json_pos(p), radius, limit, count, out);
    uncertainty = if(overflow, '[{"reason":"limit_exceeded"}]', '[]');
    perception_json(name, 'nearbyHostiles', true, !overflow, data, uncertainty, if(overflow, 'limit', null), null)
  )
);

stack_item(stack) -> (
  if(stack == null || stack == 0 || length(stack) < 2, null, stack:0)
);

stack_count(stack) -> (
  if(stack == null || stack == 0 || length(stack) < 2, 0, floor(number(stack:1)))
);

stack_empty(stack) -> (
  item = stack_item(stack);
  count = stack_count(stack);
  item == null || item == 'air' || item == 'minecraft:air' || count <= 0
);

stack_components_raw(stack) -> (
  if(stack == null || stack == 0 || length(stack) < 3 || stack:2 == null,
    null
  ,
    str('%s', stack:2)
  )
);

inventory_slot_type(slot) -> (
  if(slot >= 0 && slot <= 8,
    'hotbar'
  ,
    if(slot >= 9 && slot <= 35,
      'inventory'
    ,
      if(slot >= 36 && slot <= 39,
        'armor'
      ,
        if(slot == 40,
          'offhand'
        ,
          'aux'
        )
      )
    )
  )
);

inventory_slot_label(slot) -> (
  if(slot >= 0 && slot <= 8,
    str('hotbar.%d', slot)
  ,
    if(slot >= 9 && slot <= 35,
      str('inventory.%d', slot - 9)
    ,
      if(slot == 36,
        'armor.feet'
      ,
        if(slot == 37,
          'armor.legs'
        ,
          if(slot == 38,
            'armor.chest'
          ,
            if(slot == 39,
              'armor.head'
            ,
              if(slot == 40,
                'offhand'
              ,
                str('aux.%d', slot - 41)
              )
            )
          )
        )
      )
    )
  )
);

inventory_slot_json(slot, stack) -> (
  if(stack_empty(stack),
    str('{"slot":%d,"slotType":"%s","slotLabel":"%s","empty":true,"item":null,"count":0,"stackRaw":null}', slot, inventory_slot_type(slot), inventory_slot_label(slot))
  ,
    str('{"slot":%d,"slotType":"%s","slotLabel":"%s","empty":false,"item":"%s","count":%d,"stackRaw":%s}',
      slot, inventory_slot_type(slot), inventory_slot_label(slot), stack_item(stack), stack_count(stack), json_string(stack_components_raw(stack)))
  )
);

stack_json(stack) -> (
  if(stack_empty(stack),
    '{"empty":true,"item":null,"count":0}'
  ,
    str('{"empty":false,"item":"%s","count":%d}', stack_item(stack), stack_count(stack))
  )
);

inventory_counts_json(name) -> (
  counts = m();
  loop(46,
    stack = inventory_get(name, _);
    if(!stack_empty(stack),
      item = stack_item(stack);
      previous_count = if(counts:item == null, 0, counts:item);
      counts:item = previous_count + stack_count(stack)
    )
  );
  out = '';
  first = true;
  for(counts,
    if(first, first = false, out += ',');
    out += str('%s:%d', json_string(_), counts:_)
  );
  str('{%s}', out)
);

entity_slot_path(slot) -> (
  if(slot >= 0 && slot <= 8,
    str('hotbar.%d', slot)
  ,
    if(slot >= 9 && slot <= 35,
      str('inventory.%d', slot - 9)
    ,
      if(slot == 36,
        'armor.feet'
      ,
        if(slot == 37,
          'armor.legs'
        ,
          if(slot == 38,
            'armor.chest'
          ,
            if(slot == 39,
              'armor.head'
            ,
              if(slot == 40,
                'weapon.offhand'
              ,
                null
              )
            )
          )
        )
      )
    )
  )
);

copy_full_stack(name, from_slot, to_slot) -> (
  from_path = entity_slot_path(from_slot);
  to_path = entity_slot_path(to_slot);
  if(from_path == null || to_path == null,
    false
  ,
    run(str('item replace entity %s %s from entity %s %s', name, to_path, name, from_path));
    run(str('item replace entity %s %s with air', name, from_path));
    true
  )
);

perceive_inventory(name, params) -> (
  if(player_entity(name) == null,
    missing_body_perception(name, 'inventory')
  ,
    total_slots = 46;
    start = 0;
    if(params:'start' != null, start = floor(number(params:'start')));
    if(start < 0, start = 0);
    if(start >= total_slots, start = total_slots - 1);
    limit = 46;
    if(params:'limit' != null, limit = floor(number(params:'limit')));
    if(limit < 1, limit = 1);
    if(limit > 46, limit = 46);
    end = start + limit;
    if(end > total_slots, end = total_slots);
    out = '';
    first = true;
    slot = start;
    loop(end - start,
      stack = inventory_get(name, slot);
      if(first, first = false, out += ',');
      out += inventory_slot_json(slot, stack);
      slot += 1
    );
    complete = end >= total_slots;
    next_start = if(complete, null, end);
    next_value = if(complete, null, str('%d', end));
    uncertainty = if(complete, '[]', '[{"reason":"page_limit"}]');
    data = str('{"start":%d,"limit":%d,"nextStart":%s,"totalSlots":%d,"slots":[%s]}', start, limit, json_int_null(next_start), total_slots, out);
    perception_json(name, 'inventory', true, complete, data, uncertainty, if(complete, null, next_value), null)
  )
);

perceive_container(name, params) -> (
  if(player_entity(name) == null,
    missing_body_perception(name, 'container')
  ,
    pos = params:'pos';
    cpos = l(floor(number(pos:0)), floor(number(pos:1)), floor(number(pos:2)));
    total_slots = if(params:'total_slots' == null, 27, floor(number(params:'total_slots')));
    if(total_slots < 1, total_slots = 1);
    if(total_slots > 54, total_slots = 54);
    start = 0;
    if(params:'start' != null, start = floor(number(params:'start')));
    if(start < 0, start = 0);
    if(start >= total_slots, start = total_slots - 1);
    limit = 27;
    if(params:'limit' != null, limit = floor(number(params:'limit')));
    if(limit < 1, limit = 1);
    if(limit > 27, limit = 27);
    end = start + limit;
    if(end > total_slots, end = total_slots);
    out = '';
    first = true;
    slot = start;
    loop(end - start,
      stack = inventory_get(cpos, slot);
      if(first, first = false, out += ',');
      out += inventory_slot_json(slot, stack);
      slot += 1
    );
    complete = end >= total_slots;
    next_start = if(complete, null, end);
    next_value = if(complete, null, str('%d', end));
    uncertainty = if(complete, '[]', '[{"reason":"page_limit"}]');
    data = str('{"pos":%s,"start":%d,"limit":%d,"nextStart":%s,"totalSlots":%d,"slots":[%s]}', json_pos(cpos), start, limit, json_int_null(next_start), total_slots, out);
    perception_json(name, 'container', true, complete, data, uncertainty, if(complete, null, next_value), null)
  )
);

perceive_recipe_data(name, params) -> (
  item = if(params:'item' == null, '', params:'item');
  recipe_type = params:'type';
  recipe = if(recipe_type == null, recipe_data(item), recipe_data(item, recipe_type));
  if(recipe == null,
    perception_json(name, 'recipeData', false, true, '{}', '[]', null, 'recipe_not_found')
  ,
    data = str('{"item":%s,"type":%s,"recipe_raw":%s}', json_string(item), if(recipe_type == null, 'null', json_string(recipe_type)), json_string(str('%s', l(recipe))));
    perception_json(name, 'recipeData', true, true, data, '[]', null, null)
  )
);

emit(kind, name, data) -> (
  global_seq += 1;
  global_events += l(l(global_seq, global_tick, kind, name, data));
  trim_events();
  global_seq
);

emit_watched(kind, data) -> (
  names = keys(global_watched);
  loop(length(names),
    emit(kind, names:_, data)
  );
  true
);

emit_agent_chat(name, sender, message) -> (
  global_agent_chat_seq += 1;
  global_agent_chat_events += l(l(global_agent_chat_seq, global_tick, 'agentChat', name, l(sender, message)));
  trim_chat_events();
  global_agent_chat_seq
);

trim_events() -> (
  loop(64,
    if(length(global_events) > 512,
      delete(global_events:0)
    )
  );
  true
);

trim_chat_events() -> (
  loop(32,
    if(length(global_agent_chat_events) > 256,
      delete(global_agent_chat_events:0)
    )
  );
  true
);

remember_action_result(name, action_id, result) -> (
  global_action_results:(name + ':' + action_id) = result;
  keys_list = keys(global_action_results);
  loop(64,
    if(length(keys_list) > 512,
      delete(global_action_results:(keys_list:0));
      delete(keys_list:0)
    )
  );
  result
);

remembered_action_result(name, action_id) -> (
  global_action_results:(name + ':' + action_id)
);

watch_bot(name) -> (
  global_watched:name = true;
  true
);

owner_of(name) -> global_owners:name;

acquire_owner(name, owner, priority) -> (
  cur = global_owners:name;
  if(cur == null || priority_value(priority) > priority_value(cur:1),
    if(cur != null,
      emit('ownerPreempted', name, l(cur:0, owner))
    );
    global_owners:name = l(owner, priority);
    true
  ,
    false
  )
);

release_owner(name, owner) -> (
  cur = global_owners:name;
  if(cur != null && cur:0 == owner,
    global_owners:name = null;
    true
  ,
    false
  )
);

stop_body(name) -> (
  run('player ' + name + ' stop');
  run('player ' + name + ' unsprint');
  true
);

finish_move(name, reason, arrived) -> (
  m = global_moves:name;
  p = bot_pos(name);
  if(m == null,
    null
  ,
    target = l(m:1, m:2, m:3);
    dx = target:0 - p:0;
    dy = target:1 - p:1;
    dz = target:2 - p:2;
    dist = sqrt(dx*dx + dy*dy + dz*dz);
    stop_body(name);
    deviation = distance_from_start_path(p, m:6, target);
    emit('moveFinishTrace', name, l(m:0, reason, arrived, p, target, dist, m:5, m:8));
    emit('moveDone', name, l(m:0, arrived, p, target, dist, reason, m:5, m:7, m:8, deviation, m:14, length(m:13), move_guard_json(m), movement_cancel_json(m:15)));
    global_moves:name = null;
    global_move_cancels:name = null;
    release_owner(name, 'moveTo');
    if(global_navigations:name != null,
      finish_navigate(name, l(m:0, arrived, p, target, dist, reason, m:5, m:7, m:8, deviation, m:14, length(m:13), current_waypoint(m)))
    );
    true
  )
);

finish_mine(name, reason) -> (
  m = global_mines:name;
  p = bot_pos(name);
  if(m == null,
    null
  ,
    target = l(m:1, m:2, m:3);
    block_now = '' + block(m:1, m:2, m:3);
    gone = block_kind(block_now) == 'CLEAR';
    stop_body(name);
    emit('mineDone', name, l(m:0, gone, target, m:4, block_now, gone, p, reason, m:5));
    global_mines:name = null;
    release_owner(name, 'mineBlock');
    true
  )
);

block_matches_expected(block_now, expected) -> (
  block_now == expected || block_now == 'minecraft:' + expected || 'minecraft:' + block_now == expected
);

face_value(params) -> if(params:'face' == null, 'up', params:'face');

place_aim(name, x, y, z, face) -> (
  if(face == 'down',
    run(str('player %s look at %.3f %.3f %.3f', name, x + 0.5, y + 1.2, z + 0.5))
  ,
    if(face == 'north',
      run(str('player %s look at %.3f %.3f %.3f', name, x + 0.5, y + 0.5, z + 1.2))
    ,
      if(face == 'south',
        run(str('player %s look at %.3f %.3f %.3f', name, x + 0.5, y + 0.5, z - 0.2))
      ,
        if(face == 'west',
          run(str('player %s look at %.3f %.3f %.3f', name, x + 1.2, y + 0.5, z + 0.5))
        ,
          if(face == 'east',
            run(str('player %s look at %.3f %.3f %.3f', name, x - 0.2, y + 0.5, z + 0.5))
          ,
            run(str('player %s look at %.3f %.3f %.3f', name, x + 0.5, y - 0.2, z + 0.5))
          )
        )
      )
    )
  )
);

finish_place(name, reason) -> (
  pstate = global_places:name;
  p = bot_pos(name);
  if(pstate == null,
    null
  ,
    target = l(pstate:1, pstate:2, pstate:3);
    block_now = '' + block(pstate:1, pstate:2, pstate:3);
    placed = block_matches_expected(block_now, pstate:4);
    emit('placeDone', name, l(pstate:0, placed, target, pstate:4, block_now, pstate:5, p, reason, pstate:6));
    global_places:name = null;
    release_owner(name, 'placeBlock');
    true
  )
);

dist_to_target(pos, x, y, z) -> (
  dx = number(x) - number(pos:0);
  dy = number(y) - number(pos:1);
  dz = number(z) - number(pos:2);
  sqrt(dx*dx + dy*dy + dz*dz)
);

distance_between(a, b) -> (
  dx = number(a:0) - number(b:0);
  dy = number(a:1) - number(b:1);
  dz = number(a:2) - number(b:2);
  sqrt(dx*dx + dy*dy + dz*dz)
);

distance_from_start_path(pos, start, target) -> (
  ax = pos:0 - start:0;
  ay = pos:1 - start:1;
  az = pos:2 - start:2;
  bx = target:0 - start:0;
  by = target:1 - start:1;
  bz = target:2 - start:2;
  denom = bx*bx + by*by + bz*bz;
  if(denom <= 0.0001,
    distance_between(pos, start)
  ,
    t = (ax*bx + ay*by + az*bz) / denom;
    if(t < 0, t = 0);
    if(t > 1, t = 1);
    px = start:0 + bx*t;
    py = start:1 + by*t;
    pz = start:2 + bz*t;
    distance_between(pos, l(px, py, pz))
  )
);

move_guard_json(m) -> (
  str('{"arrival_radius":%.3f,"timeout_ticks":%d,"no_progress_ticks":%d,"min_progress_delta":%.3f,"max_deviation":%.3f}',
    m:4, m:9, m:10, m:11, m:12)
);

param_number(params, key, fallback) -> (
  if(params:key == null, fallback, number(params:key))
);

normalize_waypoint_xz(v) -> (
  nv = number(v);
  if(nv == floor(nv), nv + 0.5, nv)
);

normalize_waypoint_point(point) -> (
  l(
    normalize_waypoint_xz(point:0),
    number(point:1),
    normalize_waypoint_xz(point:2)
  )
);

parse_waypoints(params, x, y, z) -> (
  raw = params:'waypoints';
  if(raw != null && length(raw) > 0,
    raw
  ,
    l(l(x, y, z))
  )
);

current_waypoint(m) -> (
  idx = m:14;
  points = m:13;
  if(idx >= length(points),
    normalize_waypoint_point(points:(length(points) - 1))
  ,
    normalize_waypoint_point(points:idx)
  )
);

advance_waypoint(name, m) -> (
  idx = m:14 + 1;
  points = m:13;
  p = bot_pos(name);
  if(idx >= length(points),
    true
  ,
    wp = normalize_waypoint_point(points:idx);
    dist = dist_to_target(p, wp:0, wp:1, wp:2);
    global_moves:name = l(m:0, m:1, m:2, m:3, m:4, m:5, p, dist, 0, m:9, m:10, m:11, m:12, points, idx, m:15);
    false
  )
);

movement_cancel_safe_now(name, m) -> (
  profile = m:15;
  if(profile == null || bool(profile:'safe_to_cancel'),
    true
  ,
    unsafe_steps = profile:'unsafe_steps';
    policy = if(unsafe_steps != null && length(unsafe_steps) > 0, unsafe_steps:0:'policy', 'unknown');
    if(policy == 'land_first' || policy == 'settle_on_support' || policy == 'surface_or_stable_water' || policy == 'after_step',
      p = bot_pos(name);
      if(p == null,
        false
      ,
        current = current_waypoint(m);
        dist_to_target(p, current:0, current:1, current:2) <= m:4
      )
    ,
      false
    )
  )
);

request_move_cancel(name, reason) -> (
  m = global_moves:name;
  if(m != null,
    stop_body(name);
    if(movement_cancel_safe_now(name, m),
      finish_move(name, reason, false)
    ,
      global_move_cancels:name = l(reason, global_tick, movement_cancel_json(m:15));
      emit('moveCancelDelayed', name, l(m:0, reason, movement_cancel_json(m:15), global_tick))
    )
  )
);

run_move_cancel_tick(name, m) -> (
  pending = global_move_cancels:name;
  if(pending != null,
    if(movement_cancel_safe_now(name, m),
      finish_move(name, pending:0, false)
    ,
      if(global_tick - pending:1 > 40,
        finish_move(name, pending:0, false)
      )
    )
  )
);

start_move_to(name, action_id, x, y, z, params) -> (
  acquired = acquire_owner(name, 'moveTo', 'ACTION');
  if(!acquired,
    emit('moveDone', name, l(action_id, false, bot_pos(name), l(x, y, z), 9999.0, 'blocked', 0, 9999.0, 0, 0.0, 0, 1, '{"arrival_radius":0.750,"timeout_ticks":0,"no_progress_ticks":0,"min_progress_delta":0.000,"max_deviation":0.000}', '{"safe_to_cancel":true,"unsafe_count":0,"unsafe_steps":[]}'));
    false
  ,
    watch_bot(name);
    p = bot_pos(name);
    if(p == null,
      emit('moveDone', name, l(action_id, false, l(0, 0, 0), l(x, y, z), 9999.0, 'missing_body', 0, 9999.0, 0, 0.0, 0, 1, '{"arrival_radius":0.750,"timeout_ticks":0,"no_progress_ticks":0,"min_progress_delta":0.000,"max_deviation":0.000}', '{"safe_to_cancel":true,"unsafe_count":0,"unsafe_steps":[]}'));
      release_owner(name, 'moveTo');
      false
    ,
      points = parse_waypoints(params, x, y, z);
      first_target = normalize_waypoint_point(points:0);
      arrival_radius = param_number(params, 'arrival_radius', 0.75);
      timeout_ticks = floor(param_number(params, 'timeout_ticks', 260));
      no_progress_ticks = floor(param_number(params, 'no_progress_ticks', 45));
      min_progress_delta = param_number(params, 'min_progress_delta', 0.03);
      max_deviation = param_number(params, 'max_deviation', 4.0);
      movement_cancel = params:'movement_cancel';
      start_dist = dist_to_target(p, first_target:0, first_target:1, first_target:2);
      global_moves:name = l(action_id, x, y, z, arrival_radius, 0, p, start_dist, 0, timeout_ticks, no_progress_ticks, min_progress_delta, max_deviation, points, 0, movement_cancel);
      emit('moveStarted', name, l(action_id, p, l(x, y, z), points, move_guard_json(global_moves:name), movement_cancel_json(movement_cancel)));
      run('player ' + name + ' sprint');
      true
    )
  )
);

run_look_at(name, action_id, x, y, z) -> (
  acquired = acquire_owner(name, 'lookAt', 'ACTION');
  if(!acquired,
    emit('lookDone', name, l(action_id, false, l(x, y, z), bot_pos(name), 'blocked'));
    false
  ,
    stop_body(name);
    run(str('player %s look at %.3f %.3f %.3f', name, x, y, z));
    stop_body(name);
    emit('lookDone', name, l(action_id, true, l(x, y, z), bot_pos(name), 'completed'));
    release_owner(name, 'lookAt');
    true
  )
);

run_jump_once(name, action_id) -> (
  acquired = acquire_owner(name, 'jump', 'ACTION');
  if(!acquired,
    emit('jumpDone', name, l(action_id, false, bot_pos(name), 'blocked'));
    false
  ,
    run(str('player %s jump once', name));
    emit('jumpDone', name, l(action_id, true, bot_pos(name), 'completed'));
    release_owner(name, 'jump');
    true
  )
);

run_select_slot(name, action_id, slot) -> (
  acquired = acquire_owner(name, 'selectSlot', 'ACTION');
  if(!acquired,
    emit('selectSlotDone', name, l(action_id, false, slot, 'blocked'));
    false
  ,
    if(slot < 0 || slot > 8,
      emit('selectSlotDone', name, l(action_id, false, slot, 'invalid_slot'));
      release_owner(name, 'selectSlot');
      false
    ,
      run(str('player %s hotbar %d', name, slot + 1));
      emit('selectSlotDone', name, l(action_id, true, slot, 'completed'));
      release_owner(name, 'selectSlot');
      true
    )
  )
);

item_matches(stack, wanted) -> (
  item = stack_item(stack);
  item != null && (item == wanted || item == 'minecraft:' + wanted || 'minecraft:' + item == wanted)
);

find_hotbar_item(name, item) -> (
  found = null;
  slot = 0;
  loop(9,
    stack = inventory_get(name, slot);
    if(found == null && item_matches(stack, item),
      found = l(slot, stack_item(stack), stack_count(stack))
    );
    slot += 1
  );
  found
);

find_inventory_item(name, item) -> (
  found = null;
  slot = 9;
  loop(37,
    stack = inventory_get(name, slot);
    if(found == null && item_matches(stack, item),
      found = l(slot, stack_item(stack), stack_count(stack))
    );
    slot += 1
  );
  found
);

find_empty_hotbar_slot(name) -> (
  found = null;
  slot = 0;
  loop(9,
    stack = inventory_get(name, slot);
    if(found == null && stack_empty(stack),
      found = slot
    );
    slot += 1
  );
  found
);

find_first_hotbar_slot(name, item) -> (
  found = find_hotbar_item(name, item);
  if(found == null, null, found:0)
);

run_select_item(name, action_id, item) -> (
  acquired = acquire_owner(name, 'selectItem', 'ACTION');
  if(!acquired,
    emit('selectItemDone', name, l(action_id, false, item, -1, 0, 'blocked'));
    false
  ,
    found = find_hotbar_item(name, item);
    if(found == null,
      inv_found = find_inventory_item(name, item);
      if(inv_found == null,
        emit('selectItemDone', name, l(action_id, false, item, -1, 0, 'not_in_inventory'));
        release_owner(name, 'selectItem');
        false
      ,
        hotbar_slot = find_empty_hotbar_slot(name);
        if(hotbar_slot == null,
          emit('selectItemDone', name, l(action_id, false, inv_found:1, -1, inv_found:2, 'hotbar_full'));
          release_owner(name, 'selectItem');
          false
        ,
          inventory_set(name, hotbar_slot, inv_found:2, inv_found:1);
          inventory_set(name, inv_found:0, 0);
          run(str('player %s hotbar %d', name, hotbar_slot + 1));
          emit('selectItemDone', name, l(action_id, true, inv_found:1, hotbar_slot, inv_found:2, 'moved_to_hotbar'));
          release_owner(name, 'selectItem');
          true
        )
      )
    ,
      run(str('player %s hotbar %d', name, found:0 + 1));
      emit('selectItemDone', name, l(action_id, true, found:1, found:0, found:2, 'completed'));
      release_owner(name, 'selectItem');
      true
    )
  )
);

drop_mode(params) -> (
  mode = if(params:'mode' == null, 'one', params:'mode');
  if(mode == 'all', 'all', 'one')
);

finish_drop(name, reason) -> (
  d = global_drops:name;
  if(d == null,
    null
  ,
    after = inventory_get(name, d:1);
    count_after = stack_count(after);
    emit('dropDone', name, l(d:0, count_after < d:4, d:1, d:2, d:3, d:4, count_after, if(count_after < d:4, reason, 'no_delta'), stack_json(d:5), stack_json(after)));
    global_drops:name = null;
    release_owner(name, 'dropItem');
    count_after < d:4
  )
);

start_drop_item(name, action_id, params) -> (
  acquired = acquire_owner(name, 'dropItem', 'ACTION');
  slot = if(params:'slot' == null, 0, floor(number(params:'slot')));
  mode = drop_mode(params);
  before = inventory_get(name, slot);
  item = if(stack_empty(before), 'empty', stack_item(before));
  count_before = stack_count(before);
  if(!acquired,
    emit('dropDone', name, l(action_id, false, slot, mode, item, count_before, count_before, 'blocked', stack_json(before), stack_json(before)));
    false
  ,
    if(slot < 0 || slot > 8,
      emit('dropDone', name, l(action_id, false, slot, mode, item, count_before, count_before, 'invalid_slot', stack_json(before), stack_json(before)));
      release_owner(name, 'dropItem');
      false
    ,
      if(stack_empty(before),
        emit('dropDone', name, l(action_id, false, slot, mode, item, count_before, count_before, 'source_empty', stack_json(before), stack_json(before)));
        release_owner(name, 'dropItem');
        false
      ,
        watch_bot(name);
        run(str('player %s hotbar %d', name, slot + 1));
        global_drops:name = l(action_id, slot, mode, item, count_before, before, 0, 6);
        if(mode == 'all',
          run('player ' + name + ' dropStack')
        ,
          run('player ' + name + ' drop')
        );
        true
      )
    )
  )
);

run_handoff_item(name, action_id, params) -> (
  acquired = acquire_owner(name, 'handoffItem', 'ACTION');
  receiver = if(params:'receiver' == null, '', params:'receiver');
  item = if(params:'item' == null, '', params:'item');
  requested = if(params:'count' == null, 1, floor(number(params:'count')));
  if(!acquired,
    emit('handoffDone', name, l(action_id, false, receiver, item, requested, 0, -1, 'blocked', '{}', '{}', l(null, null, null)));
    false
  ,
    pe = player_entity(receiver);
    if(pe == null || receiver == '',
      emit('handoffDone', name, l(action_id, false, receiver, item, requested, 0, -1, 'receiver_not_found', '{}', '{}', l(null, null, null)));
      release_owner(name, 'handoffItem');
      false
    ,
      if(item == '' || requested <= 0,
        emit('handoffDone', name, l(action_id, false, receiver, item, requested, 0, -1, 'invalid_request', '{}', '{}', bot_pos(receiver)));
        release_owner(name, 'handoffItem');
        false
      ,
        found = find_hotbar_item(name, item);
        if(found == null,
          found = find_inventory_item(name, item)
        );
        if(found == null,
          emit('handoffDone', name, l(action_id, false, receiver, item, requested, 0, -1, 'item_not_available', '{}', '{}', bot_pos(receiver)));
          release_owner(name, 'handoffItem');
          false
        ,
          source_slot = found:0;
          before = inventory_get(name, source_slot);
          count_before = stack_count(before);
          move_count = if(requested > count_before, count_before, requested);
          if(move_count <= 0,
            emit('handoffDone', name, l(action_id, false, receiver, item, requested, 0, source_slot, 'item_not_available', stack_json(before), stack_json(before), bot_pos(receiver)));
            release_owner(name, 'handoffItem');
            false
          ,
            remaining = count_before - move_count;
            if(remaining <= 0,
              inventory_set(name, source_slot, 0)
            ,
              inventory_set(name, source_slot, remaining, stack_item(before))
            );
            after = inventory_get(name, source_slot);
            rp = bot_pos(receiver);
            watch_bot(name);
            run(str('summon item %.3f %.3f %.3f {Item:{id:"%s",count:%d}}', rp:0, rp:1, rp:2, item, move_count));
            emit('handoffDone', name, l(action_id, true, receiver, item, requested, move_count, source_slot, 'spawned_item', stack_json(before), stack_json(after), rp));
            release_owner(name, 'handoffItem');
            true
          )
        )
      )
    )
  )
);

run_move_item(name, action_id, params) -> (
  acquired = acquire_owner(name, 'moveItem', 'ACTION');
  from_slot = if(params:'from_slot' == null, -1, floor(number(params:'from_slot')));
  to_slot = if(params:'to_slot' == null, -1, floor(number(params:'to_slot')));
  requested = if(params:'count' == null, -1, floor(number(params:'count')));
  max_stack = if(params:'max_stack' == null, 64, floor(number(params:'max_stack')));
  from_before = if(from_slot >= 0 && from_slot <= 45, inventory_get(name, from_slot), 0);
  to_before = if(to_slot >= 0 && to_slot <= 45, inventory_get(name, to_slot), 0);
  if(!acquired,
    emit('moveItemDone', name, l(action_id, false, from_slot, to_slot, 'unknown', 0, 'blocked', stack_json(from_before), stack_json(from_before), stack_json(to_before), stack_json(to_before)));
    false
  ,
    if(from_slot < 0 || from_slot > 45 || to_slot < 0 || to_slot > 45 || from_slot == to_slot,
      emit('moveItemDone', name, l(action_id, false, from_slot, to_slot, 'unknown', 0, 'invalid_slot', stack_json(from_before), stack_json(from_before), stack_json(to_before), stack_json(to_before)));
      release_owner(name, 'moveItem');
      false
    ,
      if(stack_empty(from_before),
        emit('moveItemDone', name, l(action_id, false, from_slot, to_slot, 'empty', 0, 'source_empty', stack_json(from_before), stack_json(from_before), stack_json(to_before), stack_json(to_before)));
        release_owner(name, 'moveItem');
        false
      ,
        if(!stack_empty(to_before) && !item_matches(to_before, stack_item(from_before)),
          emit('moveItemDone', name, l(action_id, false, from_slot, to_slot, stack_item(from_before), stack_count(from_before), 'destination_occupied', stack_json(from_before), stack_json(from_before), stack_json(to_before), stack_json(to_before)));
          release_owner(name, 'moveItem');
          false
        ,
          source_count = stack_count(from_before);
          dest_count = stack_count(to_before);
          move_count = if(requested <= 0 || requested > source_count, source_count, requested);
          room = max_stack - dest_count;
          if(room <= 0,
            emit('moveItemDone', name, l(action_id, false, from_slot, to_slot, stack_item(from_before), move_count, 'destination_full', stack_json(from_before), stack_json(from_before), stack_json(to_before), stack_json(to_before)));
            release_owner(name, 'moveItem');
            false
          ,
            if(move_count > room, move_count = room);
            exact_full_stack_move = stack_empty(to_before) && move_count == source_count && copy_full_stack(name, from_slot, to_slot);
            if(!exact_full_stack_move,
              inventory_set(name, to_slot, dest_count + move_count, stack_item(from_before));
              remaining = source_count - move_count;
              if(remaining <= 0,
                inventory_set(name, from_slot, 0)
              ,
                inventory_set(name, from_slot, remaining, stack_item(from_before))
              )
            );
            from_after = inventory_get(name, from_slot);
            to_after = inventory_get(name, to_slot);
            emit('moveItemDone', name, l(action_id, true, from_slot, to_slot, stack_item(from_before), move_count, if(move_count == source_count, 'completed', 'partial'), stack_json(from_before), stack_json(from_after), stack_json(to_before), stack_json(to_after)));
            release_owner(name, 'moveItem');
            true
          )
        )
      )
    )
  )
);

slot_fact_json(slot, stack) -> (
  if(stack_empty(stack),
    str('{"slot":%d,"empty":true,"item":null,"count":0}', slot)
  ,
    str('{"slot":%d,"empty":false,"item":"%s","count":%d}', slot, stack_item(stack), stack_count(stack))
  )
);

craft_input_facts_json(name, inputs) -> (
  out = '';
  first = true;
  i = 0;
  loop(length(inputs),
    input = inputs:i;
    slot = floor(number(input:'slot'));
    stack = inventory_get(name, slot);
    if(first, first = false, out += ',');
    out += slot_fact_json(slot, stack);
    i += 1
  );
  str('[%s]', out)
);

craft_inputs_ready(name, inputs) -> (
  ready = true;
  i = 0;
  loop(length(inputs),
    input = inputs:i;
    slot = floor(number(input:'slot'));
    wanted = input:'item';
    count = floor(number(input:'count'));
    stack = inventory_get(name, slot);
    if(slot < 0 || slot > 45 || count <= 0 || !item_matches(stack, wanted) || stack_count(stack) < count,
      ready = false
    );
    i += 1
  );
  ready
);

craft_apply_inputs(name, inputs) -> (
  i = 0;
  loop(length(inputs),
    input = inputs:i;
    slot = floor(number(input:'slot'));
    count = floor(number(input:'count'));
    stack = inventory_get(name, slot);
    remaining = stack_count(stack) - count;
    if(remaining <= 0,
      inventory_set(name, slot, 0)
    ,
      inventory_set(name, slot, remaining, stack_item(stack))
    );
    i += 1
  )
);

craft_remainders_valid(name, inputs, remainders) -> (
  valid = true;
  i = 0;
  loop(length(remainders),
    remainder = remainders:i;
    slot = floor(number(remainder:'slot'));
    count = floor(number(remainder:'count'));
    wanted = remainder:'item';
    matched = false;
    j = 0;
    loop(length(inputs),
      input = inputs:j;
      input_slot = floor(number(input:'slot'));
      if(input_slot == slot,
        matched = true;
        stack = inventory_get(name, slot);
        input_count = floor(number(input:'count'));
        remaining = stack_count(stack) - input_count;
        if(count <= 0 || wanted == null || wanted == '' || remaining != 0,
          valid = false
        )
      );
      j += 1
    );
    if(!matched, valid = false);
    i += 1
  );
  valid
);

craft_apply_remainders(name, remainders) -> (
  i = 0;
  loop(length(remainders),
    remainder = remainders:i;
    slot = floor(number(remainder:'slot'));
    count = floor(number(remainder:'count'));
    item = remainder:'item';
    inventory_set(name, slot, count, item);
    i += 1
  )
);

run_craft_item(name, action_id, params) -> (
  acquired = acquire_owner(name, 'craftItem', 'ACTION');
  inputs = if(params:'inputs' == null, l(), params:'inputs');
  output = params:'output';
  remainders = if(params:'remainders' == null, l(), params:'remainders');
  output_slot = if(output == null || output:'slot' == null, -1, floor(number(output:'slot')));
  output_item = if(output == null || output:'item' == null, 'unknown', output:'item');
  output_count = if(output == null || output:'count' == null, 0, floor(number(output:'count')));
  max_stack = if(params:'max_stack' == null, 64, floor(number(params:'max_stack')));
  inputs_before = craft_input_facts_json(name, inputs);
  output_before = if(output_slot >= 0 && output_slot <= 45, stack_json(inventory_get(name, output_slot)), '{}');
  if(!acquired,
    emit('craftDone', name, l(action_id, false, output_item, output_count, output_slot, 'blocked', inputs_before, inputs_before, output_before, output_before));
    false
  ,
    if(length(inputs) == 0 || output_slot < 0 || output_slot > 45 || output_count <= 0,
      emit('craftDone', name, l(action_id, false, output_item, output_count, output_slot, 'invalid_recipe', inputs_before, inputs_before, output_before, output_before));
      release_owner(name, 'craftItem');
      false
    ,
      dest = inventory_get(name, output_slot);
      if(!stack_empty(dest) && !item_matches(dest, output_item),
        emit('craftDone', name, l(action_id, false, output_item, output_count, output_slot, 'destination_occupied', inputs_before, inputs_before, output_before, output_before));
        release_owner(name, 'craftItem');
        false
      ,
        if(stack_count(dest) + output_count > max_stack,
          emit('craftDone', name, l(action_id, false, output_item, output_count, output_slot, 'destination_full', inputs_before, inputs_before, output_before, output_before));
          release_owner(name, 'craftItem');
          false
        ,
          if(!craft_inputs_ready(name, inputs),
            emit('craftDone', name, l(action_id, false, output_item, output_count, output_slot, 'missing_inputs', inputs_before, inputs_before, output_before, output_before));
            release_owner(name, 'craftItem');
            false
          ,
            if(!craft_remainders_valid(name, inputs, remainders),
              emit('craftDone', name, l(action_id, false, output_item, output_count, output_slot, 'invalid_remainder', inputs_before, inputs_before, output_before, output_before));
              release_owner(name, 'craftItem');
              false
            ,
              craft_apply_inputs(name, inputs);
              craft_apply_remainders(name, remainders);
              inventory_set(name, output_slot, stack_count(dest) + output_count, output_item);
              inputs_after = craft_input_facts_json(name, inputs);
              output_after = stack_json(inventory_get(name, output_slot));
              emit('craftDone', name, l(action_id, true, output_item, output_count, output_slot, 'completed', inputs_before, inputs_after, output_before, output_after));
              release_owner(name, 'craftItem');
              true
            )
          )
        )
      )
    )
  )
);

hotbar_slot_item(name, slot) -> (
  stack = inventory_get(name, slot);
  if(stack_empty(stack), 'empty', stack_item(stack))
);

inventory_snapshot_hash(name) -> (
  str('%s', inventory_get(name))
);

use_mode(params) -> (
  mode = if(params:'mode' == null, 'once', params:'mode');
  if(mode == 'continuous', 'continuous', 'once')
);

finish_use(name, reason) -> (
  u = global_uses:name;
  if(u != null,
    stop_body(name);
    after = inventory_snapshot_hash(name);
    final_reason = reason;
    success = reason != 'timeout' && reason != 'preempted' && reason != 'interrupted';
    if(success && u:2 != 'unknown' && after == u:4,
      success = false;
      final_reason = 'no_effect'
    );
    emit('useDone', name, l(u:0, success, u:1, u:2, u:5, bot_pos(name), u:4, after, final_reason, u:3));
    global_uses:name = null;
    release_owner(name, 'useItem')
  )
);

target_entity_near(name, target_type, radius) -> (
  p = bot_pos(name);
  selector = str('@e[x=%d,y=%d,z=%d,distance=..%d,limit=32,sort=nearest]',
    floor(number(p:0)), floor(number(p:1)), floor(number(p:2)), radius);
  found = entity_selector(selector);
  result = null;
  loop(length(found),
    e = found:_;
    if(result == null && e != player(name) && entity_matches_type(e, target_type),
      result = e
    )
  );
  result
);

target_entity_named_near(name, target_name, radius) -> (
  p = bot_pos(name);
  selector = str('@e[x=%d,y=%d,z=%d,distance=..%d,limit=32,sort=nearest]',
    floor(number(p:0)), floor(number(p:1)), floor(number(p:2)), radius);
  found = entity_selector(selector);
  result = null;
  loop(length(found),
    e = found:_;
    if(result == null && e != player(name) && query(e, 'name') == target_name,
      result = e
    )
  );
  result
);

target_entity_uuid_near(name, target_uuid, radius) -> (
  p = bot_pos(name);
  selector = str('@e[x=%d,y=%d,z=%d,distance=..%d,limit=32,sort=nearest]',
    floor(number(p:0)), floor(number(p:1)), floor(number(p:2)), radius);
  found = entity_selector(selector);
  result = null;
  loop(length(found),
    e = found:_;
    if(result == null && e != player(name) && query(e, 'uuid') == target_uuid,
      result = e
    )
  );
  result
);

ranged_target_aim_pos(target_pos, target_type) -> (
  if(target_type == 'minecraft:end_crystal' || target_type == 'end_crystal',
    l(target_pos:0, target_pos:1 + 0.5, target_pos:2)
  ,
    l(target_pos:0, target_pos:1 + 1.0, target_pos:2)
  )
);

ballistic_low_arc_pitch(dx, dy, dz, speed, gravity) -> (
  horiz = sqrt(dx*dx + dz*dz);
  if(horiz < 0.001,
    null
  ,
    speed2 = speed * speed;
    root = speed2 * speed2 - gravity * (gravity * horiz * horiz + 2 * dy * speed2);
    if(root < 0,
      null
    ,
      tan_theta = (speed2 - sqrt(root)) / (gravity * horiz);
      -atan2(tan_theta, 1.0)
    )
  )
);

aim_ranged_target(name, target_pos, target_type) -> (
  p = bot_pos(name);
  if(p != null,
    aim = ranged_target_aim_pos(target_pos, target_type);
    dx = aim:0 - p:0;
    dy = aim:1 - (p:1 + 1.62);
    dz = aim:2 - p:2;
    horiz = sqrt(dx*dx + dz*dz);
    yaw = -atan2(dx, dz);
    ballistic_pitch = if(target_type == 'minecraft:end_crystal' || target_type == 'end_crystal',
      ballistic_low_arc_pitch(dx, dy, dz, 3.0, 0.05)
    ,
      null
    );
    pitch = if(ballistic_pitch != null, ballistic_pitch, if(horiz < 0.001, if(dy > 0, -90, 90), -atan2(dy, horiz)));
    run(str('player %s look %.3f %.3f', name, pitch, yaw))
  )
);

entity_persistent(e) -> (
  nbt = query(e, 'nbt');
  bool(nbt:'PersistenceRequired')
);

arrow_near_bot(name, radius) -> (
  p = bot_pos(name);
  if(p == null,
    false
  ,
    selector = str('@e[type=arrow,x=%d,y=%d,z=%d,distance=..%d,limit=1,sort=nearest]',
      floor(number(p:0)), floor(number(p:1)), floor(number(p:2)), radius);
    length(entity_selector(selector)) > 0
  )
);

finish_ranged(name, reason) -> (
  r = global_ranged:name;
  if(r != null,
    stop_body(name);
    final_reason = reason;
    target = target_entity_uuid_near(name, r:10, r:3);
    if(target == null,
      target_pos = r:8;
      hp = r:9;
      if(final_reason == 'target_lost' && r:13 && (r:2 == 'minecraft:end_crystal' || r:2 == 'end_crystal'),
        final_reason = 'target_destroyed'
      )
    ,
      target_pos = query(target, 'pos');
      hp = entity_health(target)
    );
    if(final_reason == 'timeout',
      if(r:13,
        final_reason = 'missed'
      ,
        final_reason = 'unknown'
      )
    );
    success = (r:12 || final_reason == 'target_destroyed') && final_reason != 'missed' && final_reason != 'unknown' && final_reason != 'preempted' && final_reason != 'interrupted' && final_reason != 'blocked';
    emit('rangedDone', name, l(r:0, success, r:1, r:2, r:10, r:11, target_pos, hp, r:7, r:12, r:13, bot_pos(name), final_reason, r:4, r:5, r:6));
    global_ranged:name = null;
    release_owner(name, 'rangedAttack')
  )
);

start_ranged_attack(name, action_id, params) -> (
  acquired = acquire_owner(name, 'rangedAttack', 'ACTION');
  weapon = if(params:'weapon' == null, 'bow', params:'weapon');
  target_type = if(params:'target_type' == null, '', params:'target_type');
  target_id = if(params:'target_id' == null, null, params:'target_id');
  target_name = if(params:'target_name' == null, null, params:'target_name');
  radius = if(params:'radius' == null, 24, floor(number(params:'radius')));
  if(radius < 2, radius = 2);
  if(radius > 48, radius = 48);
  timeout_ticks = if(params:'timeout_ticks' == null, 80, floor(number(params:'timeout_ticks')));
  if(timeout_ticks < 5, timeout_ticks = 5);
  if(timeout_ticks > 400, timeout_ticks = 400);
  use_interval_ticks = if(params:'use_interval_ticks' == null, if(weapon == 'crossbow', 26, 22), floor(number(params:'use_interval_ticks')));
  if(use_interval_ticks < 2, use_interval_ticks = 2);
  expected_shots = if(params:'expected_shots' == null, 1, floor(number(params:'expected_shots')));
  if(expected_shots < 1, expected_shots = 1);
  target_type_is_player = target_type == 'player' || target_type == 'minecraft:player';
  if(!acquired,
    emit('rangedDone', name, l(action_id, false, weapon, target_type, null, target_name, l(0, 0, 0), null, null, false, false, bot_pos(name), 'blocked', 0, use_interval_ticks, expected_shots));
    false
  ,
    if(target_name != null && target_name == name,
      emit('rangedDone', name, l(action_id, false, weapon, target_type, null, target_name, l(0, 0, 0), null, null, false, false, bot_pos(name), 'self_target_disallowed', 0, use_interval_ticks, expected_shots));
      release_owner(name, 'rangedAttack');
      false
    ,
      if(target_type_is_player && target_name == null,
        emit('rangedDone', name, l(action_id, false, weapon, target_type, null, null, l(0, 0, 0), null, null, false, false, bot_pos(name), 'player_target_requires_name', 0, use_interval_ticks, expected_shots));
        release_owner(name, 'rangedAttack');
        false
      ,
        target = if(target_id != null, target_entity_uuid_near(name, target_id, radius), if(target_name != null, target_entity_named_near(name, target_name, radius), target_entity_near(name, target_type, radius)));
        if(target == null,
          emit('rangedDone', name, l(action_id, false, weapon, target_type, null, target_name, l(0, 0, 0), null, null, false, false, bot_pos(name), 'target_not_found', 0, use_interval_ticks, expected_shots));
          release_owner(name, 'rangedAttack');
          false
        ,
          watch_bot(name);
          hp = entity_health(target);
          global_ranged:name = l(action_id, weapon, target_type, radius, 0, use_interval_ticks, expected_shots, hp, query(target, 'pos'), hp, query(target, 'uuid'), query(target, 'name'), false, false, timeout_ticks);
          run('player ' + name + ' stop');
          queue_immediate_lava_reflex(name);
          true
        )
      )
    )
  )
);

finish_attack(name, reason) -> (
  a = global_attacks:name;
  if(a != null,
    stop_body(name);
    final_reason = reason;
    target = target_entity_uuid_near(name, a:10, a:2);
    if(target == null,
      target_pos = a:8;
      hp = a:9;
      if(final_reason == 'target_lost' && a:6 > 0,
        if(a:18,
          final_reason = 'target_gone'
        ,
          if(a:14 && a:13,
            final_reason = 'killed'
          ,
            final_reason = 'target_gone'
          )
        )
      )
    ,
      target_pos = query(target, 'pos');
      hp = entity_health(target)
    );
    success = final_reason == 'killed' || final_reason == 'completed' || final_reason == 'target_gone';
    emit('attackDone', name, l(a:0, success, a:1, a:10, a:11, target_pos, hp, a:12, a:13, a:14, bot_pos(name), final_reason, a:3, a:6, a:5, if(a:16 < 999999, a:16, null), if(a:17 > 0, a:17, null)));
    global_attacks:name = null;
    release_owner(name, 'attackEntity')
  )
);

start_attack_entity(name, action_id, params) -> (
  acquired = acquire_owner(name, 'attackEntity', 'ACTION');
  target_type = if(params:'target_type' == null, if(params:'type' == null, '', params:'type'), params:'target_type');
  target_name = if(params:'target_name' == null, null, params:'target_name');
  radius = if(params:'radius' == null, 4, floor(number(params:'radius')));
  if(radius < 1, radius = 1);
  if(radius > 16, radius = 16);
  timeout_ticks = if(params:'timeout_ticks' == null, 100, floor(number(params:'timeout_ticks')));
  if(timeout_ticks < 1, timeout_ticks = 1);
  if(timeout_ticks > 400, timeout_ticks = 400);
  cooldown_ticks = if(params:'cooldown_ticks' == null, 10, floor(number(params:'cooldown_ticks')));
  if(cooldown_ticks < 1, cooldown_ticks = 1);
  attack_range = if(params:'attack_range' == null, 1.85, number(params:'attack_range'));
  if(attack_range < 1.2, attack_range = 1.2);
  if(attack_range > 3.0, attack_range = 3.0);
  target_type_is_player = target_type == 'player' || target_type == 'minecraft:player';
  if(!acquired,
    emit('attackDone', name, l(action_id, false, target_type, null, target_name, l(0, 0, 0), null, null, false, false, bot_pos(name), 'blocked', 0, 0, cooldown_ticks, null, null));
    false
  ,
    if(target_name != null && target_name == name,
      emit('attackDone', name, l(action_id, false, target_type, null, target_name, l(0, 0, 0), null, null, false, false, bot_pos(name), 'self_target_disallowed', 0, 0, cooldown_ticks, null, null));
      release_owner(name, 'attackEntity');
      false
    ,
      if(target_type_is_player && target_name == null,
        emit('attackDone', name, l(action_id, false, target_type, null, null, l(0, 0, 0), null, null, false, false, bot_pos(name), 'player_target_requires_name', 0, 0, cooldown_ticks, null, null));
        release_owner(name, 'attackEntity');
        false
      ,
        target = if(target_name != null, target_entity_named_near(name, target_name, radius), target_entity_near(name, target_type, radius));
        if(target == null,
          emit('attackDone', name, l(action_id, false, target_type, null, target_name, l(0, 0, 0), null, null, false, false, bot_pos(name), 'target_not_found', 0, 0, cooldown_ticks, null, null));
          release_owner(name, 'attackEntity');
          false
        ,
          watch_bot(name);
          hp = entity_health(target);
          global_attacks:name = l(action_id, target_type, radius, 0, timeout_ticks, cooldown_ticks, 0, attack_range, query(target, 'pos'), hp, query(target, 'uuid'), query(target, 'name'), hp, false, entity_persistent(target), 0, 999999, 0, entity_kind(target) == 'minecraft:player');
          queue_immediate_lava_reflex(name);
          true
        )
      )
    )
  )
);

run_container_transfer(name, action_id, params) -> (
  acquired = acquire_owner(name, 'containerTransfer', 'ACTION');
  pos = params:'pos';
  cpos = l(floor(number(pos:0)), floor(number(pos:1)), floor(number(pos:2)));
  direction = if(params:'direction' == null, 'container_to_bot', params:'direction');
  container_slot = if(params:'container_slot' == null, 0, floor(number(params:'container_slot')));
  bot_slot = if(params:'bot_slot' == null, 0, floor(number(params:'bot_slot')));
  requested = if(params:'count' == null, -1, floor(number(params:'count')));
  max_stack = if(params:'max_stack' == null, 64, floor(number(params:'max_stack')));
  if(!acquired,
    emit('containerDone', name, l(action_id, false, direction, cpos, container_slot, bot_slot, 'unknown', 0, 'blocked', '{}', '{}', '{}', '{}'));
    false
  ,
    container_before = inventory_get(cpos, container_slot);
    bot_before = inventory_get(name, bot_slot);
    if(direction == 'bot_to_container',
      source = bot_before;
      dest = container_before
    ,
      source = container_before;
      dest = bot_before
    );
    if(stack_empty(source),
      emit('containerDone', name, l(action_id, false, direction, cpos, container_slot, bot_slot, 'empty', 0, 'source_empty', stack_json(container_before), stack_json(container_before), stack_json(bot_before), stack_json(bot_before)));
      release_owner(name, 'containerTransfer');
      false
    ,
      if(!stack_empty(dest) && !item_matches(dest, stack_item(source)),
        emit('containerDone', name, l(action_id, false, direction, cpos, container_slot, bot_slot, stack_item(source), stack_count(source), 'destination_occupied', stack_json(container_before), stack_json(container_before), stack_json(bot_before), stack_json(bot_before)));
        release_owner(name, 'containerTransfer');
        false
      ,
        source_count = stack_count(source);
        dest_count = stack_count(dest);
        move_count = if(requested <= 0 || requested > source_count, source_count, requested);
        room = max_stack - dest_count;
        if(room <= 0,
          emit('containerDone', name, l(action_id, false, direction, cpos, container_slot, bot_slot, stack_item(source), move_count, 'destination_full', stack_json(container_before), stack_json(container_before), stack_json(bot_before), stack_json(bot_before)));
          release_owner(name, 'containerTransfer');
          false
        ,
          if(move_count > room, move_count = room);
          if(direction == 'bot_to_container',
            inventory_set(cpos, container_slot, dest_count + move_count, stack_item(source));
            remaining = source_count - move_count;
            if(remaining <= 0, inventory_set(name, bot_slot, 0), inventory_set(name, bot_slot, remaining, stack_item(source)))
          ,
            inventory_set(name, bot_slot, dest_count + move_count, stack_item(source));
            remaining = source_count - move_count;
            if(remaining <= 0, inventory_set(cpos, container_slot, 0), inventory_set(cpos, container_slot, remaining, stack_item(source)))
          );
          container_after = inventory_get(cpos, container_slot);
          bot_after = inventory_get(name, bot_slot);
          emit('containerDone', name, l(action_id, true, direction, cpos, container_slot, bot_slot, stack_item(source), move_count, if(move_count == source_count, 'completed', 'partial'), stack_json(container_before), stack_json(container_after), stack_json(bot_before), stack_json(bot_after)));
          release_owner(name, 'containerTransfer');
          true
        )
      )
    )
  )
);

furnace_slot_index(slot_name) -> (
  if(slot_name == 'input', 0,
    if(slot_name == 'fuel', 1,
      if(slot_name == 'output', 2, -1)
    )
  )
);

run_furnace_transfer(name, action_id, params) -> (
  acquired = acquire_owner(name, 'furnaceTransfer', 'ACTION');
  pos = params:'pos';
  fpos = l(floor(number(pos:0)), floor(number(pos:1)), floor(number(pos:2)));
  direction = if(params:'direction' == null, 'furnace_to_bot', params:'direction');
  furnace_slot_name = if(params:'furnace_slot' == null, 'output', params:'furnace_slot');
  furnace_slot = furnace_slot_index(furnace_slot_name);
  bot_slot = if(params:'bot_slot' == null, 0, floor(number(params:'bot_slot')));
  requested = if(params:'count' == null, -1, floor(number(params:'count')));
  max_stack = if(params:'max_stack' == null, 64, floor(number(params:'max_stack')));
  if(!acquired,
    emit('furnaceDone', name, l(action_id, false, direction, fpos, furnace_slot_name, furnace_slot, bot_slot, 'unknown', 0, 'blocked', '{}', '{}', '{}', '{}'));
    false
  ,
    if(furnace_slot < 0,
      emit('furnaceDone', name, l(action_id, false, direction, fpos, furnace_slot_name, furnace_slot, bot_slot, 'unknown', 0, 'invalid_furnace_slot', '{}', '{}', '{}', '{}'));
      release_owner(name, 'furnaceTransfer');
      false
    ,
      furnace_before = inventory_get(fpos, furnace_slot);
      bot_before = inventory_get(name, bot_slot);
      if(direction == 'bot_to_furnace',
        source = bot_before;
        dest = furnace_before
      ,
        source = furnace_before;
        dest = bot_before
      );
      if(stack_empty(source),
        emit('furnaceDone', name, l(action_id, false, direction, fpos, furnace_slot_name, furnace_slot, bot_slot, 'empty', 0, 'source_empty', stack_json(furnace_before), stack_json(furnace_before), stack_json(bot_before), stack_json(bot_before)));
        release_owner(name, 'furnaceTransfer');
        false
      ,
        if(!stack_empty(dest) && !item_matches(dest, stack_item(source)),
          emit('furnaceDone', name, l(action_id, false, direction, fpos, furnace_slot_name, furnace_slot, bot_slot, stack_item(source), stack_count(source), 'destination_occupied', stack_json(furnace_before), stack_json(furnace_before), stack_json(bot_before), stack_json(bot_before)));
          release_owner(name, 'furnaceTransfer');
          false
        ,
          source_count = stack_count(source);
          dest_count = stack_count(dest);
          move_count = if(requested <= 0 || requested > source_count, source_count, requested);
          room = max_stack - dest_count;
          if(room <= 0,
            emit('furnaceDone', name, l(action_id, false, direction, fpos, furnace_slot_name, furnace_slot, bot_slot, stack_item(source), move_count, 'destination_full', stack_json(furnace_before), stack_json(furnace_before), stack_json(bot_before), stack_json(bot_before)));
            release_owner(name, 'furnaceTransfer');
            false
          ,
          if(move_count > room, move_count = room);
          if(direction == 'bot_to_furnace',
            inventory_set(fpos, furnace_slot, dest_count + move_count, stack_item(source));
            remaining = source_count - move_count;
            if(remaining <= 0, inventory_set(name, bot_slot, 0), inventory_set(name, bot_slot, remaining, stack_item(source)))
          ,
            inventory_set(name, bot_slot, dest_count + move_count, stack_item(source));
            remaining = source_count - move_count;
            if(remaining <= 0, inventory_set(fpos, furnace_slot, 0), inventory_set(fpos, furnace_slot, remaining, stack_item(source)))
          );
          furnace_after = inventory_get(fpos, furnace_slot);
          bot_after = inventory_get(name, bot_slot);
          emit('furnaceDone', name, l(action_id, true, direction, fpos, furnace_slot_name, furnace_slot, bot_slot, stack_item(source), move_count, if(move_count == source_count, 'completed', 'partial'), stack_json(furnace_before), stack_json(furnace_after), stack_json(bot_before), stack_json(bot_after)));
          release_owner(name, 'furnaceTransfer');
          true
          )
        )
      )
    )
  )
);

start_use_item(name, action_id, params) -> (
  acquired = acquire_owner(name, 'useItem', 'ACTION');
  mode = use_mode(params);
  ticks = if(params:'ticks' == null, 1, floor(number(params:'ticks')));
  if(ticks < 1, ticks = 1);
  if(ticks > 200, ticks = 200);
  slot = if(params:'slot' == null, -1, floor(number(params:'slot')));
  item = if(params:'item' == null, if(slot >= 0 && slot <= 8, hotbar_slot_item(name, slot), 'unknown'), params:'item');
  if(!acquired,
    emit('useDone', name, l(action_id, false, mode, item, bot_pos(name), bot_pos(name), inventory_snapshot_hash(name), inventory_snapshot_hash(name), 'blocked', 0));
    false
  ,
    if(slot >= 0,
      if(slot > 8,
        emit('useDone', name, l(action_id, false, mode, item, bot_pos(name), bot_pos(name), inventory_snapshot_hash(name), inventory_snapshot_hash(name), 'invalid_slot', 0));
        release_owner(name, 'useItem');
        false
      ,
        run(str('player %s hotbar %d', name, slot + 1));
        before = inventory_snapshot_hash(name);
        start = bot_pos(name);
        global_uses:name = l(action_id, mode, item, 0, before, start, ticks, 'selecting');
        queue_immediate_lava_reflex(name);
        true
      )
    ,
      before = inventory_snapshot_hash(name);
      start = bot_pos(name);
      global_uses:name = l(action_id, mode, item, 0, before, start, ticks, 'using');
      if(mode == 'continuous',
        run('player ' + name + ' use continuous')
      ,
        run('player ' + name + ' use once')
      );
      queue_immediate_lava_reflex(name);
      true
    )
  )
);

run_stop_action(name, action_id) -> (
  stop_body(name);
  if(global_moves:name != null,
    request_move_cancel(name, 'interrupted')
  );
  if(global_mines:name != null,
    finish_mine(name, 'interrupted')
  );
  if(global_places:name != null,
    finish_place(name, 'interrupted')
  );
  if(global_uses:name != null,
    finish_use(name, 'interrupted')
  );
  if(global_ignites:name != null,
    finish_ignite(name, 'interrupted')
  );
  if(global_sows:name != null,
    finish_sow(name, 'interrupted')
  );
  if(global_attacks:name != null,
    finish_attack(name, 'interrupted')
  );
  if(global_drops:name != null,
    finish_drop(name, 'interrupted')
  );
  emit('stopDone', name, l(action_id, true, bot_pos(name), 'completed'));
  true
);

start_mine_block(name, action_id, x, y, z, params) -> (
  acquired = acquire_owner(name, 'mineBlock', 'ACTION');
  block_type = if(params:'block_type' == null, '' + block(x, y, z), params:'block_type');
  if(!acquired,
    block_now = '' + block(x, y, z);
    emit('mineDone', name, l(action_id, false, l(x, y, z), block_type, block_now, false, bot_pos(name), 'blocked', 0));
    false
  ,
    watch_bot(name);
    timeout_ticks = floor(param_number(params, 'timeout_ticks', 180));
    block_now = '' + block(x, y, z);
    global_mines:name = l(action_id, x, y, z, block_type, 0, timeout_ticks);
    queue_immediate_lava_reflex(name);
    if(block_kind(block_now) == 'CLEAR',
      finish_mine(name, 'already_clear')
    ,
      run(str('player %s look at %.3f %.3f %.3f', name, x + 0.5, y + 0.5, z + 0.5));
      run('player ' + name + ' attack continuous')
    );
    true
  )
);

start_place_block(name, action_id, x, y, z, params) -> (
  acquired = acquire_owner(name, 'placeBlock', 'ACTION');
  block_type = if(params:'block_type' == null, 'unknown', params:'block_type');
  face = face_value(params);
  if(!acquired,
    block_now = '' + block(x, y, z);
    emit('placeDone', name, l(action_id, false, l(x, y, z), block_type, block_now, face, bot_pos(name), 'blocked', 0));
    false
  ,
    watch_bot(name);
    timeout_ticks = floor(param_number(params, 'timeout_ticks', 20));
    block_now = '' + block(x, y, z);
    global_places:name = l(action_id, x, y, z, block_type, face, 0, timeout_ticks);
    queue_immediate_lava_reflex(name);
    if(block_matches_expected(block_now, block_type),
      finish_place(name, 'already_placed')
    ,
      if(block_kind(block_now) != 'CLEAR',
        finish_place(name, 'occupied')
      ,
        place_aim(name, x, y, z, face);
        run('player ' + name + ' use once')
      )
    );
    true
  )
);

is_lava_at(x, y, z) -> (
  bs = '' + block(x, y, z);
  bs == 'lava' || bs == 'minecraft:lava'
);

is_water_at(x, y, z) -> (
  bs = '' + block(x, y, z);
  bs == 'water' || bs == 'minecraft:water'
);

is_clear_block_at(x, y, z) -> (
  block_kind('' + block(x, y, z)) == 'CLEAR'
);

is_solid_floor(x, y, z) -> (
  bs = '' + block(x, y - 1, z);
  bs != 'air' && bs != 'minecraft:air' &&
  bs != 'lava' && bs != 'minecraft:lava' &&
  bs != 'water' && bs != 'minecraft:water'
);

is_safe_cell(x, y, z) -> (
  here = '' + block(x, y, z);
  head = '' + block(x, y + 1, z);
  here != 'lava' && here != 'minecraft:lava' &&
  head != 'lava' && head != 'minecraft:lava' &&
  is_solid_floor(x, y, z)
);

is_dry_stand_cell(x, y, z) -> (
  is_clear_block_at(x, y, z) &&
  is_clear_block_at(x, y + 1, z) &&
  is_solid_floor(x, y, z)
);

lava_near_pos(p) -> (
  found = false;
  loop(3,
    ox = _ - 1;
    loop(2,
      oy = _ - 1;
      loop(3,
        oz = _ - 1;
        if(is_lava_at(floor(p:0) + ox, floor(p:1) + oy, floor(p:2) + oz),
          found = true
        )
      )
    )
  );
  found
);

fire_ticks(name) -> (
  pe = player_entity(name);
  if(pe == null,
    -20
  ,
    nbt = query(pe, 'nbt');
    if(nbt:'Fire' == null, -20, floor(number(nbt:'Fire')))
  )
);

on_fire_now(name) -> fire_ticks(name) > 0;

bot_air(name) -> (
  pe = player_entity(name);
  if(pe == null,
    null
  ,
    nbt = query(pe, 'nbt');
    if(nbt:'Air' == null, null, floor(number(nbt:'Air')))
  )
);

bot_health(name) -> (
  pe = player_entity(name);
  if(pe == null, null, query(pe, 'health'))
);

head_in_water_now(name) -> (
  p = bot_pos(name);
  if(p == null,
    false
  ,
    is_water_at(floor(p:0), floor(p:1) + 1, floor(p:2))
  )
);

in_water_now(name) -> (
  p = bot_pos(name);
  if(p == null,
    false
  ,
    is_water_at(floor(p:0), floor(p:1), floor(p:2)) ||
    is_water_at(floor(p:0), floor(p:1) + 1, floor(p:2))
  )
);

water_reflex_should_trigger(name) -> (
  if(!in_water_now(name),
    global_water_reflex_health_baselines:name = null;
    false
  ,
    hp = bot_health(name);
    if(global_water_reflex_health_baselines:name == null && hp != null,
      global_water_reflex_health_baselines:name = hp
    );
    air = bot_air(name);
    air_risk = head_in_water_now(name) && air != null && air <= global_water_reflex_air_threshold;
    damage_risk = false;
    if(global_water_reflex_damage_budget != null && hp != null && global_water_reflex_health_baselines:name != null,
      damage_risk = global_water_reflex_health_baselines:name - hp >= global_water_reflex_damage_budget
    );
    air_risk || damage_risk
  )
);

hazard_kind_near_name(name) -> (
  p = bot_pos(name);
  if(p == null,
    null
  ,
    if(lava_near_pos(p),
      'lava'
    ,
      if(on_fire_now(name),
        'fire'
      ,
        if(water_reflex_should_trigger(name),
          'water'
        ,
          null
        )
      )
    )
  )
);

safe_escape_target(p) -> (
  bx = floor(p:0);
  by = floor(p:1);
  bz = floor(p:2);
  candidates = l(l(1, 0), l(-1, 0), l(0, 1), l(0, -1), l(2, 0), l(-2, 0), l(0, 2), l(0, -2));
  best = null;
  loop(length(candidates),
    c = candidates:_;
    tx = bx + c:0;
    tz = bz + c:1;
    if(best == null && is_safe_cell(tx, by, tz),
      best = l(tx + 0.5, by, tz + 0.5)
    )
  );
  best
);

water_surface_target(p) -> (
  bx = floor(p:0);
  by = floor(p:1);
  bz = floor(p:2);
  best = null;
  loop(9,
    sy = by + _;
    if(best == null && is_water_at(bx, sy, bz) && !is_water_at(bx, sy + 1, bz) && is_clear_block_at(bx, sy + 1, bz),
      best = l(bx + 0.5, sy + 0.8, bz + 0.5)
    )
  );
  best
);

water_near_cell(x, y, z) -> (
  is_water_at(x + 1, y, z) ||
  is_water_at(x - 1, y, z) ||
  is_water_at(x, y, z + 1) ||
  is_water_at(x, y, z - 1)
);

water_near_escape_cell(x, y, z) -> (
  water_near_cell(x, y, z) ||
  is_water_at(x + 2, y, z) ||
  is_water_at(x - 2, y, z) ||
  is_water_at(x, y, z + 2) ||
  is_water_at(x, y, z - 2)
);

water_escape_corridor_clear(bx, bz, tx, tz, sy) -> (
  dx = tx - bx;
  dz = tz - bz;
  steps = max(abs(dx), abs(dz));
  step_x = if(dx == 0, 0, if(dx > 0, 1, -1));
  step_z = if(dz == 0, 0, if(dz > 0, 1, -1));
  clear = true;
  loop(steps,
    cx = bx + step_x * (_ + 1);
    cz = bz + step_z * (_ + 1);
    feet_kind = block_kind('' + block(cx, sy, cz));
    head_kind = block_kind('' + block(cx, sy + 1, cz));
    if(feet_kind == 'SOLID' || head_kind == 'SOLID',
      clear = false
    )
  );
  clear
);

water_shore_escape_target(p) -> (
  bx = floor(p:0);
  by = floor(p:1);
  bz = floor(p:2);
  candidates = l(
    l(1, 0), l(-1, 0), l(0, 1), l(0, -1),
    l(2, 0), l(-2, 0), l(0, 2), l(0, -2),
    l(3, 0), l(-3, 0), l(0, 3), l(0, -3),
    l(4, 0), l(-4, 0), l(0, 4), l(0, -4),
    l(5, 0), l(-5, 0), l(0, 5), l(0, -5),
    l(6, 0), l(-6, 0), l(0, 6), l(0, -6),
    l(7, 0), l(-7, 0), l(0, 7), l(0, -7),
    l(8, 0), l(-8, 0), l(0, 8), l(0, -8),
    l(1, 1), l(1, -1), l(-1, 1), l(-1, -1)
  );
  best = null;
  loop(11,
    sy = by - 2 + _;
    loop(length(candidates),
      c = candidates:_;
      tx = bx + c:0;
      tz = bz + c:1;
      if(best == null && is_dry_stand_cell(tx, sy, tz) && water_escape_corridor_clear(bx, bz, tx, tz, sy) && (water_near_escape_cell(tx, sy, tz) || water_near_escape_cell(tx, sy - 1, tz)),
        step_x = if(c:0 == 0, 0, if(c:0 > 0, 1, -1));
        step_z = if(c:1 == 0, 0, if(c:1 > 0, 1, -1));
        nx = tx + step_x;
        nz = tz + step_z;
        if(is_dry_stand_cell(nx, sy, nz) && water_escape_corridor_clear(bx, bz, nx, nz, sy),
          best = l(nx + 0.5, sy, nz + 0.5)
        ,
          best = l(tx + 0.5, sy, tz + 0.5)
        )
      )
    )
  );
  best
);

water_escape_target(p) -> (
  water_shore_escape_target(p)
);

reflex_target_is_dry_stand(target) -> (
  target != null && is_dry_stand_cell(floor(target:0), floor(target:1), floor(target:2))
);

reflex_target_block_type(target) -> (
  if(target == null, 'null', '' + block(floor(target:0), floor(target:1), floor(target:2)))
);

reflex_target_below_type(target) -> (
  if(target == null, 'null', '' + block(floor(target:0), floor(target:1) - 1, floor(target:2)))
);

water_hazard_clear(name) -> (
  p = bot_pos(name);
  if(p == null,
    false
  ,
    air = bot_air(name);
    (air != null && air > global_water_reflex_air_threshold) || !head_in_water_now(name)
  )
);

queue_immediate_lava_reflex(name) -> (
  if(global_reflex_scan && global_reflexes:name == null,
    p = bot_pos(name);
    if(p != null && lava_near_pos(p),
      global_pending_reflexes:name = 'lava'
    )
  );
  true
);

queue_immediate_fire_reflex(name) -> (
  if(global_reflex_scan && global_reflexes:name == null,
    if(on_fire_now(name),
      global_pending_reflexes:name = 'fire'
    )
  );
  true
);

queue_immediate_water_reflex(name) -> (
  if(global_reflex_scan && global_reflexes:name == null,
    if(water_reflex_should_trigger(name),
      global_pending_reflexes:name = 'water'
    )
  );
  true
);

movement_water_escape_ticks(m) -> (
  max(10, min(40, floor(m:10 / 3)))
);

movement_water_escape_should_trigger(name, m, stuck_ticks) -> (
  global_reflex_scan &&
  global_reflexes:name == null &&
  in_water_now(name) &&
  water_reflex_should_trigger(name) &&
  stuck_ticks >= movement_water_escape_ticks(m)
);

cancel_move_preempted(name) -> (
  if(global_moves:name != null,
    request_move_cancel(name, 'preempted')
  )
);

cancel_mine_preempted(name) -> (
  if(global_mines:name != null,
    finish_mine(name, 'preempted')
  )
);

cancel_place_preempted(name) -> (
  if(global_places:name != null,
    finish_place(name, 'preempted')
  )
);

cancel_use_preempted(name) -> (
  if(global_uses:name != null,
    finish_use(name, 'preempted')
  )
);

cancel_attack_preempted(name) -> (
  if(global_attacks:name != null,
    finish_attack(name, 'preempted')
  )
);

cancel_ranged_preempted(name) -> (
  if(global_ranged:name != null,
    finish_ranged(name, 'preempted')
  )
);

cancel_drop_preempted(name) -> (
  if(global_drops:name != null,
    finish_drop(name, 'preempted')
  )
);

cancel_ignite_preempted(name) -> (
  if(global_ignites:name != null,
    finish_ignite(name, 'preempted')
  )
);

cancel_sow_preempted(name) -> (
  if(global_sows:name != null,
    finish_sow(name, 'preempted')
  )
);

start_hazard_reflex(name, kind) -> (
  owner_name = if(kind == 'fire', 'fireReflex', if(kind == 'water', 'waterReflex', 'lavaReflex'));
  if(acquire_owner(name, owner_name, 'SURVIVAL'),
    p = bot_pos(name);
    target = if(kind == 'water', water_escape_target(p), safe_escape_target(p));
    if(kind == 'water' && target == null,
      target = water_surface_target(p)
    );
    cancel_move_preempted(name);
    cancel_mine_preempted(name);
    cancel_place_preempted(name);
    cancel_use_preempted(name);
    cancel_ranged_preempted(name);
    cancel_attack_preempted(name);
    cancel_drop_preempted(name);
    cancel_ignite_preempted(name);
    cancel_sow_preempted(name);
    if(target == null,
      emit('reflexCompleted', name, l(p, 0.0, 0, false, kind, l(0, 0, 0), false, false, 'null', 'null'));
      release_owner(name, owner_name);
      false
    ,
      global_reflexes:name = l(target:0, target:1, target:2, 0, kind, owner_name);
      emit('reflexTriggered', name, l(kind, p, target, reflex_target_is_dry_stand(target), reflex_target_block_type(target), reflex_target_below_type(target)));
      true
    )
  ,
    false
  )
);

start_lava_reflex(name) -> start_hazard_reflex(name, 'lava');

start_fire_reflex(name) -> start_hazard_reflex(name, 'fire');

start_water_reflex(name) -> start_hazard_reflex(name, 'water');

combat_reflex_scan(name) -> (
  if(!global_reflex_scan || global_reflexes:name != null || global_engages:name != null,
    true
  ,
    hp = bot_health(name);
    if(hp == null,
      true
    ,
      baseline = global_combat_health_baselines:name;
      if(baseline == null || hp >= baseline,
        global_combat_health_baselines:name = hp;
        true
      ,
        if(baseline - hp >= 2.0 && nearest_hostile_near(name, 16) != null,
          start_combat_reflex(name)
        ,
          true
        )
      )
    )
  )
);

start_combat_reflex(name) -> (
  hp = bot_health(name);
  baseline = global_combat_health_baselines:name;
  global_combat_health_baselines:name = hp;
  nearest = nearest_hostile_near(name, 16);
  nearest_kind = if(nearest != null, entity_kind(nearest), null);
  emit('underAttack', name, l(nearest_kind, hp, baseline));
  if(nearest == null,
    true
  ,
    tp = query(nearest, 'pos');
    start_combat_flee_reflex(name, tp)
  );
  true
);

start_combat_flee_reflex(name, hostile_pos) -> (
  minebot_interrupt(name, '{}');
  if(acquire_owner(name, 'combatReflex', 'SURVIVAL'),
    p = bot_pos(name);
    dx = p:0 - hostile_pos:0;
    dz = p:2 - hostile_pos:2;
    norm = sqrt(dx*dx + dz*dz);
    if(norm < 0.1, dx = 1.0; norm = 1.0);
    fx = p:0 + dx / norm * 8.0;
    fz = p:2 + dz / norm * 8.0;
    global_reflexes:name = l(fx, p:1, fz, 0, 'combat_flee', 'combatReflex');
    target = l(fx, p:1, fz);
    emit('reflexTriggered', name, l('combat_flee', p, target, reflex_target_is_dry_stand(target), reflex_target_block_type(target), reflex_target_below_type(target)))
  )
);

run_move_tick(name, m) -> (
  p = bot_pos(name);
  target = current_waypoint(m);
  dist = dist_to_target(p, target:0, target:1, target:2);
  ticks = m:5 + 1;
  min_dist = m:7;
  stuck_ticks = m:8;
  if(dist < min_dist - m:11,
    min_dist = dist;
    stuck_ticks = 0
  ,
    stuck_ticks += 1
  );
  deviation = distance_from_start_path(p, m:6, target);
  updated_move = l(m:0, m:1, m:2, m:3, m:4, ticks, m:6, min_dist, stuck_ticks, m:9, m:10, m:11, m:12, m:13, m:14, m:15);
  global_moves:name = updated_move;
  if(dist <= m:4,
    if(advance_waypoint(name, updated_move),
      finish_move(name, 'arrived', true)
    )
  ,
      if(ticks > m:9,
        finish_move(name, 'timeout', false)
      ,
        if(movement_water_escape_should_trigger(name, updated_move, stuck_ticks),
          start_water_reflex(name)
        ,
          if(stuck_ticks >= m:10,
            finish_move(name, 'stuck', false)
          ,
            if(deviation > m:12,
              finish_move(name, 'deviated', false)
            ,
              run(str('player %s look at %.3f %.3f %.3f', name, target:0, target:1 + 1.0, target:2));
              if(target:1 > p:1 + 0.35,
                run('player ' + name + ' jump once')
              );
              run('player ' + name + ' move forward')
            )
          )
        )
      )
    )
);

run_reflex_tick(name, r) -> (
  p = bot_pos(name);
  dx = r:0 - p:0;
  dy = r:1 - p:1;
  dz = r:2 - p:2;
  dist = sqrt(dx*dx + dy*dy + dz*dz);
  ticks = r:3 + 1;
  kind = if(r:4 == null, 'lava', r:4);
  owner_name = if(r:5 == null, 'lavaReflex', r:5);
  global_reflexes:name = l(r:0, r:1, r:2, ticks, kind, owner_name);
  clear_of_hazard = if(kind == 'fire', !on_fire_now(name), if(kind == 'water', water_hazard_clear(name), !lava_near_pos(p)));
  water_target_is_shore = kind == 'water' && is_dry_stand_cell(floor(r:0), floor(r:1), floor(r:2));
  water_on_dry_stand = kind == 'water' && is_dry_stand_cell(floor(p:0), floor(p:1), floor(p:2));
  escaped = if(kind == 'water', water_target_is_shore && dist <= 0.9 && water_on_dry_stand, dist <= 0.9 && clear_of_hazard);
  if(escaped,
    stop_body(name);
    target = l(r:0, r:1, r:2);
    emit('reflexCompleted', name, l(p, dist, ticks, true, kind, target, reflex_target_is_dry_stand(target), if(kind == 'water', water_on_dry_stand, reflex_target_is_dry_stand(p)), reflex_target_block_type(target), reflex_target_below_type(target)));
    global_reflexes:name = null;
    release_owner(name, owner_name)
  ,
    if(ticks > 100,
      stop_body(name);
      target = l(r:0, r:1, r:2);
      emit('reflexCompleted', name, l(p, dist, ticks, false, kind, target, reflex_target_is_dry_stand(target), if(kind == 'water', water_on_dry_stand, reflex_target_is_dry_stand(p)), reflex_target_block_type(target), reflex_target_below_type(target)));
      global_reflexes:name = null;
      release_owner(name, owner_name)
    ,
      look_y = if(water_target_is_shore, p:1 + 0.8, r:1 + 1.0);
      run(str('player %s look at %.3f %.3f %.3f', name, r:0, look_y, r:2));
      if(kind == 'water' && water_target_is_shore,
        run('player ' + name + ' sprint');
        run('player ' + name + ' jump')
      ,
        if((kind == 'water' && !water_target_is_shore) || r:1 > p:1 + 0.35,
          run('player ' + name + ' jump once')
        )
      );
      run('player ' + name + ' move forward')
    )
  )
);

run_mine_tick(name, m) -> (
  ticks = m:5 + 1;
  global_mines:name = l(m:0, m:1, m:2, m:3, m:4, ticks, m:6);
  block_now = '' + block(m:1, m:2, m:3);
  if(block_kind(block_now) == 'CLEAR',
    finish_mine(name, 'completed')
  ,
    if(ticks > m:6,
      finish_mine(name, 'timeout')
    )
  )
);

run_place_tick(name, pstate) -> (
  ticks = pstate:6 + 1;
  global_places:name = l(pstate:0, pstate:1, pstate:2, pstate:3, pstate:4, pstate:5, ticks, pstate:7);
  block_now = '' + block(pstate:1, pstate:2, pstate:3);
  if(block_matches_expected(block_now, pstate:4),
    finish_place(name, 'completed')
  ,
    if(ticks > pstate:7,
      finish_place(name, 'timeout')
    ,
      place_aim(name, pstate:1, pstate:2, pstate:3, pstate:5);
      run('player ' + name + ' use once')
    )
  )
);

start_ignite_block(name, action_id, x, y, z, params) -> (
  acquired = acquire_owner(name, 'igniteBlock', 'ACTION');
  item = if(params:'item' == null, 'unknown', params:'item');
  allow_substitute = if(params:'allow_server_substitute' == null, false, params:'allow_server_substitute');
  if(!acquired,
    emit('igniteDone', name, l(action_id, false, l(x, y, z), 'fire', '' + block(x, y, z), item, 'failed', bot_pos(name), 'blocked', 0, '' + block(x, y, z)));
    false
  ,
    watch_bot(name);
    timeout_ticks = floor(param_number(params, 'timeout_ticks', 20));
    before = '' + block(x, y, z);
    global_ignites:name = l(action_id, x, y, z, item, 0, timeout_ticks, before, 'physical', allow_substitute, false);
    if(block_matches_expected(before, 'fire'),
      finish_ignite(name, 'already_lit')
    ,
      place_aim(name, x, y, z, 'up');
      run('player ' + name + ' use once');
      true
    )
  )
);

run_ignite_tick(name, ig) -> (
  ticks = ig:5 + 1;
  block_now = '' + block(ig:1, ig:2, ig:3);
  if(block_matches_expected(block_now, 'fire'),
    global_ignites:name = l(ig:0, ig:1, ig:2, ig:3, ig:4, ticks, ig:6, ig:7, ig:8, ig:9, ig:10);
    finish_ignite(name, 'completed')
  ,
    if(!ig:10 && ig:9 && ticks >= 2,
      run(str('setblock %d %d %d fire', ig:1, ig:2, ig:3));
      block_after_substitute = '' + block(ig:1, ig:2, ig:3);
      global_ignites:name = l(ig:0, ig:1, ig:2, ig:3, ig:4, ticks, ig:6, ig:7, 'substitute', ig:9, true);
      if(block_matches_expected(block_after_substitute, 'fire'),
        finish_ignite(name, 'completed')
      )
    ,
    if(ticks > ig:6,
      global_ignites:name = l(ig:0, ig:1, ig:2, ig:3, ig:4, ticks, ig:6, ig:7, ig:8, ig:9, ig:10);
      finish_ignite(name, 'timeout')
    ,
      global_ignites:name = l(ig:0, ig:1, ig:2, ig:3, ig:4, ticks, ig:6, ig:7, ig:8, ig:9, ig:10)
    )
    )
  )
);

finish_ignite(name, reason) -> (
  ig = global_ignites:name;
  if(ig == null, null,
    p = bot_pos(name);
    target = l(ig:1, ig:2, ig:3);
    block_now = '' + block(ig:1, ig:2, ig:3);
    on_fire = block_matches_expected(block_now, 'fire');
    method = if(on_fire, ig:8, 'failed');
    success = on_fire && reason != 'interrupted' && reason != 'preempted' && reason != 'blocked';
    stop_body(name);
    emit('igniteDone', name, l(ig:0, success, target, 'fire', block_now, ig:4, method, p, reason, ig:5, ig:7));
    global_ignites:name = null;
    release_owner(name, 'igniteBlock');
    true
  )
);

start_sow_crop(name, action_id, x, y, z, params) -> (
  acquired = acquire_owner(name, 'sowCrop', 'ACTION');
  seed_item = if(params:'seed_item' == null, 'unknown', params:'seed_item');
  crop_block = if(params:'crop_block' == null, '', params:'crop_block');
  allow_substitute = if(params:'allow_server_substitute' == null, false, params:'allow_server_substitute');
  crop_y = y + 1;
  if(!acquired,
    emit('sowDone', name, l(action_id, false, l(x, y, z), l(x, crop_y, z), crop_block, '' + block(x, crop_y, z), seed_item, 'failed', bot_pos(name), 'blocked', 0, '' + block(x, crop_y, z), inventory_snapshot_hash(name), inventory_snapshot_hash(name)));
    false
  ,
    if(crop_block == '',
      emit('sowDone', name, l(action_id, false, l(x, y, z), l(x, crop_y, z), crop_block, '' + block(x, crop_y, z), seed_item, 'failed', bot_pos(name), 'invalid_crop', 0, '' + block(x, crop_y, z), inventory_snapshot_hash(name), inventory_snapshot_hash(name)));
      release_owner(name, 'sowCrop');
      false
    ,
      watch_bot(name);
      timeout_ticks = floor(param_number(params, 'timeout_ticks', 20));
      crop_before = '' + block(x, crop_y, z);
      inv_before = inventory_snapshot_hash(name);
      global_sows:name = l(action_id, x, y, z, crop_y, crop_block, seed_item, 0, timeout_ticks, crop_before, inv_before, 'physical', allow_substitute, false);
      if(block_matches_expected(crop_before, crop_block),
        finish_sow(name, 'already_sown')
      ,
        place_aim(name, x, y, z, 'up');
        run('player ' + name + ' use once');
        true
      )
    )
  )
);

run_sow_tick(name, sw) -> (
  ticks = sw:7 + 1;
  crop_now = '' + block(sw:1, sw:4, sw:3);
  inv_now = inventory_snapshot_hash(name);
  consumed = inv_now != sw:10;
  if(block_matches_expected(crop_now, sw:5) && consumed,
    global_sows:name = l(sw:0, sw:1, sw:2, sw:3, sw:4, sw:5, sw:6, ticks, sw:8, sw:9, sw:10, sw:11, sw:12, sw:13);
    finish_sow(name, 'completed')
  ,
    if(!sw:13 && sw:12 && ticks >= 2,
      run(str('setblock %d %d %d %s', sw:1, sw:4, sw:3, sw:5));
      seed_slot = find_first_hotbar_slot(name, sw:6);
      if(seed_slot != null,
        seed_stack = inventory_get(name, seed_slot);
        remaining = stack_count(seed_stack) - 1;
        if(remaining <= 0,
          inventory_set(name, seed_slot, 0)
        ,
          inventory_set(name, seed_slot, remaining, stack_item(seed_stack))
        )
      );
      crop_after_substitute = '' + block(sw:1, sw:4, sw:3);
      global_sows:name = l(sw:0, sw:1, sw:2, sw:3, sw:4, sw:5, sw:6, ticks, sw:8, sw:9, sw:10, 'substitute', sw:12, true);
      if(block_matches_expected(crop_after_substitute, sw:5) && inventory_snapshot_hash(name) != sw:10,
        finish_sow(name, 'completed')
      )
    ,
    if(ticks > sw:8,
      global_sows:name = l(sw:0, sw:1, sw:2, sw:3, sw:4, sw:5, sw:6, ticks, sw:8, sw:9, sw:10, sw:11, sw:12, sw:13);
      finish_sow(name, 'timeout')
    ,
      global_sows:name = l(sw:0, sw:1, sw:2, sw:3, sw:4, sw:5, sw:6, ticks, sw:8, sw:9, sw:10, sw:11, sw:12, sw:13)
    )
    )
  )
);

finish_sow(name, reason) -> (
  sw = global_sows:name;
  if(sw == null, null,
    p = bot_pos(name);
    target = l(sw:1, sw:2, sw:3);
    crop_pos = l(sw:1, sw:4, sw:3);
    crop_now = '' + block(sw:1, sw:4, sw:3);
    inv_after = inventory_snapshot_hash(name);
    consumed = inv_after != sw:10;
    sown = block_matches_expected(crop_now, sw:5) && consumed;
    method = if(sown, sw:11, 'failed');
    success = sown && reason != 'interrupted' && reason != 'preempted' && reason != 'blocked';
    stop_body(name);
    emit('sowDone', name, l(sw:0, success, target, crop_pos, sw:5, crop_now, sw:6, method, p, reason, sw:7, sw:9, sw:10, inv_after));
    global_sows:name = null;
    release_owner(name, 'sowCrop');
    true
  )
);

run_use_tick(name, u) -> (
  phase = if(length(u) > 7, u:7, 'using');
  if(phase == 'selecting',
    global_uses:name = l(u:0, u:1, u:2, u:3, u:4, u:5, u:6, 'using');
    if(u:1 == 'continuous',
      run('player ' + name + ' use continuous')
    ,
      run('player ' + name + ' use once')
    )
  ,
  ticks = u:3 + 1;
  global_uses:name = l(u:0, u:1, u:2, ticks, u:4, u:5, u:6, phase);
  after = inventory_snapshot_hash(name);
  if(u:2 != 'unknown' && after != u:4,
    finish_use(name, 'completed')
  ,
    if(ticks >= u:6,
      finish_use(name, 'completed')
    ,
      if(u:1 != 'continuous',
        run('player ' + name + ' use once')
      )
    )
  )
  )
);

run_drop_tick(name, d) -> (
  ticks = d:6 + 1;
  global_drops:name = l(d:0, d:1, d:2, d:3, d:4, d:5, ticks, d:7);
  after = inventory_get(name, d:1);
  count_after = stack_count(after);
  if(count_after < d:4,
    finish_drop(name, 'completed')
  ,
    if(ticks >= d:7,
      finish_drop(name, 'no_delta')
    )
  )
);

run_attack_tick(name, a) -> (
  ticks = a:3 + 1;
  attacks = a:6;
  target = target_entity_uuid_near(name, a:10, a:2);
  if(target == null,
    global_attacks:name = l(a:0, a:1, a:2, ticks, a:4, a:5, attacks, a:7, a:8, a:9, a:10, a:11, a:12, a:13, a:14, a:15, a:16, a:17, a:18);
    finish_attack(name, 'target_lost')
  ,
    hp = entity_health(target);
    if(hp != null && hp <= 0,
      global_attacks:name = l(a:0, a:1, a:2, ticks, a:4, a:5, attacks, a:7, query(target, 'pos'), hp, a:10, a:11, a:12, true, a:14, a:15, a:16, a:17, a:18);
      finish_attack(name, 'killed')
    ,
      if(ticks > a:4,
        damage_seen = a:13 || (a:12 != null && hp != null && hp < a:12) || (a:9 != null && hp != null && hp < a:9);
        global_attacks:name = l(a:0, a:1, a:2, ticks, a:4, a:5, attacks, a:7, query(target, 'pos'), hp, a:10, a:11, a:12, damage_seen, a:14, a:15, a:16, a:17, a:18);
        finish_attack(name, 'timeout')
      ,
        p = query(target, 'pos');
        bp = bot_pos(name);
        dx = p:0 - bp:0;
        dz = p:2 - bp:2;
        horiz_dist = sqrt(dx*dx + dz*dz);
        damage_seen = a:13 || (a:12 != null && hp != null && hp < a:12) || (a:9 != null && hp != null && hp < a:9);
        min_interval = a:16;
        max_interval = a:17;
        last_attack_tick = a:15;
        if(horiz_dist > a:7,
          if(ticks % 2 == 1,
            run(str('player %s look at %.3f %.3f %.3f', name, p:0, bp:1, p:2))
          ,
            run('player ' + name + ' sprint');
            run('player ' + name + ' move forward')
          )
        ,
          stop_body(name);
          if(ticks % 2 == 1,
            run(str('player %s look at %.3f %.3f %.3f', name, p:0, p:1 + 1.0, p:2))
          ,
            if(ticks % a:5 == 0,
              interval = if(a:15 > 0, ticks - a:15, 0);
              run('player ' + name + ' attack once');
              attacks += 1;
              min_interval = if(interval > 0 && interval < a:16, interval, a:16);
              max_interval = if(interval > a:17, interval, a:17);
              last_attack_tick = ticks
            )
          )
        );
        global_attacks:name = l(a:0, a:1, a:2, ticks, a:4, a:5, attacks, a:7, p, hp, a:10, a:11, a:12, damage_seen, a:14, last_attack_tick, min_interval, max_interval, a:18)
      )
    )
  )
);

run_ranged_tick(name, r) -> (
  ticks = r:4 + 1;
  target = target_entity_uuid_near(name, r:10, r:3);
  if(target == null,
    global_ranged:name = l(r:0, r:1, r:2, r:3, ticks, r:5, r:6, r:7, r:8, r:9, r:10, r:11, r:12, r:13, r:14);
    finish_ranged(name, 'target_lost')
  ,
    hp = entity_health(target);
    damage_seen = r:12 || (r:7 != null && hp != null && hp < r:7) || (r:9 != null && hp != null && hp < r:9);
    if(damage_seen,
      global_ranged:name = l(r:0, r:1, r:2, r:3, ticks, r:5, r:6, r:7, query(target, 'pos'), hp, r:10, r:11, true, r:13, r:14);
      finish_ranged(name, 'completed')
    ,
      if(ticks > r:14,
        global_ranged:name = l(r:0, r:1, r:2, r:3, ticks, r:5, r:6, r:7, query(target, 'pos'), hp, r:10, r:11, false, r:13, r:14);
        finish_ranged(name, 'timeout')
      ,
        p = query(target, 'pos');
        fired_observed = r:13;
        if(r:1 == 'crossbow',
          if(ticks == 1,
            aim_ranged_target(name, p, r:2);
            run('player ' + name + ' use continuous')
          ,
            if(ticks < r:5,
              aim_ranged_target(name, p, r:2)
            ,
              if(ticks == r:5,
                aim_ranged_target(name, p, r:2);
                run('player ' + name + ' use once');
              )
            )
          )
        ,
          if(ticks == 1,
            aim_ranged_target(name, p, r:2);
            run('player ' + name + ' use continuous')
          ,
            if(ticks < r:5,
              aim_ranged_target(name, p, r:2)
            ,
              if(ticks == r:5,
                aim_ranged_target(name, p, r:2);
                run('player ' + name + ' stop');
              )
            )
          )
        );
        if(!fired_observed && arrow_near_bot(name, 8),
          fired_observed = true
        );
        global_ranged:name = l(r:0, r:1, r:2, r:3, ticks, r:5, r:6, r:7, p, hp, r:10, r:11, damage_seen, fired_observed, r:14)
      )
    )
  )
);

tick_bot(name) -> (
  p = bot_pos(name);
  if(p == null,
    if(global_missing_notices:name == null,
      global_missing_notices:name = l(0, 0, 0);
      emit('bodyMissing', name, l(l(0, 0, 0)))
    );
    null
  ,
  global_missing_notices:name = null;
  if(global_pending_reflexes:name != null && global_reflexes:name == null,
    pending_kind = global_pending_reflexes:name;
    global_pending_reflexes:name = null;
    start_hazard_reflex(name, pending_kind)
  );
  if(global_reflex_scan && global_reflexes:name == null,
    hazard_kind = hazard_kind_near_name(name);
    if(hazard_kind != null,
      start_hazard_reflex(name, hazard_kind)
    )
  );
  combat_reflex_scan(name);
  r = global_reflexes:name;
  if(r != null,
    run_reflex_tick(name, r)
  ,
    mine = global_mines:name;
    if(mine != null,
      run_mine_tick(name, mine)
    ,
      u = global_uses:name;
      if(u != null,
        run_use_tick(name, u)
      ,
        ranged = global_ranged:name;
        if(ranged != null,
          run_ranged_tick(name, ranged)
        ,
        drop_state = global_drops:name;
        if(drop_state != null,
          run_drop_tick(name, drop_state)
        ,
          attack = global_attacks:name;
          if(attack != null,
            run_attack_tick(name, attack)
          ,
            pstate = global_places:name;
            if(pstate != null,
              run_place_tick(name, pstate)
            ,
              ig = global_ignites:name;
              if(ig != null,
                run_ignite_tick(name, ig)
              ,
                sw = global_sows:name;
                if(sw != null,
                  run_sow_tick(name, sw)
                ,
                  eg = global_engages:name;
                  if(eg != null,
                    run_engage_tick(name, eg)
                  ,
                  fl = global_follows:name;
                  if(fl != null,
                    run_follow_tick(name, fl)
                  ,
                    m = global_moves:name;
                    if(m != null,
                      run_move_cancel_tick(name, m);
                      m = global_moves:name;
                      if(m != null,
                        if(global_move_cancels:name == null,
                          run_move_tick(name, m)
                        )
                      )
                    )
                  )
                  )
                )
              )
            )
          )
        )
        )
      )
    )
  ))
);

__on_tick() -> (
  global_tick += 1;
  pending_names = keys(global_pending_spawns);
  loop(length(pending_names),
    finalize_pending_spawn(pending_names:_)
  );
  respawn_names = keys(global_respawn_notices);
  loop(length(respawn_names),
    rname = respawn_names:_;
    if(global_respawn_notices:rname != null && player_entity(rname) != null,
      emit('respawned', rname, l(bot_pos(rname)));
      global_respawn_notices:rname = null
    )
  );
  watched_names = keys(global_watched);
  loop(length(watched_names),
    tick_bot(watched_names:_)
  );
  move_names = keys(global_moves);
  loop(length(move_names),
    if(global_watched:(move_names:_) == null,
      tick_bot(move_names:_)
    )
  );
  mine_names = keys(global_mines);
  loop(length(mine_names),
    if(global_moves:(mine_names:_) == null && global_watched:(mine_names:_) == null,
      tick_bot(mine_names:_)
    )
  );
  place_names = keys(global_places);
  loop(length(place_names),
    if(global_moves:(place_names:_) == null && global_mines:(place_names:_) == null && global_watched:(place_names:_) == null,
      tick_bot(place_names:_)
    )
  );
  ignite_names = keys(global_ignites);
  loop(length(ignite_names),
    if(global_moves:(ignite_names:_) == null && global_mines:(ignite_names:_) == null && global_places:(ignite_names:_) == null && global_uses:(ignite_names:_) == null && global_watched:(ignite_names:_) == null,
      tick_bot(ignite_names:_)
    )
  );
  sow_names = keys(global_sows);
  loop(length(sow_names),
    if(global_moves:(sow_names:_) == null && global_mines:(sow_names:_) == null && global_places:(sow_names:_) == null && global_uses:(sow_names:_) == null && global_ignites:(sow_names:_) == null && global_watched:(sow_names:_) == null,
      tick_bot(sow_names:_)
    )
  );
  use_names = keys(global_uses);
  loop(length(use_names),
    if(global_moves:(use_names:_) == null && global_mines:(use_names:_) == null && global_places:(use_names:_) == null && global_ignites:(use_names:_) == null && global_watched:(use_names:_) == null,
      tick_bot(use_names:_)
    )
  );
  ranged_names = keys(global_ranged);
  loop(length(ranged_names),
    if(global_moves:(ranged_names:_) == null && global_mines:(ranged_names:_) == null && global_places:(ranged_names:_) == null && global_uses:(ranged_names:_) == null && global_ignites:(ranged_names:_) == null && global_watched:(ranged_names:_) == null,
      tick_bot(ranged_names:_)
    )
  );
  attack_names = keys(global_attacks);
  loop(length(attack_names),
    if(global_moves:(attack_names:_) == null && global_mines:(attack_names:_) == null && global_places:(attack_names:_) == null && global_uses:(attack_names:_) == null && global_ignites:(attack_names:_) == null && global_watched:(attack_names:_) == null,
      tick_bot(attack_names:_)
    )
  );
  reflex_names = keys(global_reflexes);
  loop(length(reflex_names),
    if(global_moves:(reflex_names:_) == null && global_mines:(reflex_names:_) == null && global_places:(reflex_names:_) == null && global_uses:(reflex_names:_) == null && global_attacks:(reflex_names:_) == null && global_ignites:(reflex_names:_) == null && global_watched:(reflex_names:_) == null,
      tick_bot(reflex_names:_)
    )
  )
);

__on_player_picks_up_item(player, item_tuple) -> (
  emit_watched('itemPickup', l(query(player, 'name'), item_tuple))
);

__on_player_dies(player) -> (
  name = query(player, 'name');
  if(global_watched:name != null,
    inv = inventory_get(name);
    raw = str('%s', inv);
    emit('death', name, l(query(player, 'pos'), raw, hash_code(raw), inventory_counts_json(name)))
  )
);

__on_player_connects(player) -> (
  name = query(player, 'name');
  if(global_respawn_notices:name != null,
    emit('respawned', name, l(query(player, 'pos')));
    global_respawn_notices:name = null
  )
);

__on_player_message(player, message) -> (
  sender = query(player, 'name');
  names = keys(global_watched);
  loop(length(names),
    if(sender != names:_,
      emit_agent_chat(names:_, sender, message)
    )
  );
  true
);

probe_node_key(x, y, z) -> str('%d_%d_%d', x, y, z);

probe_walkability(x, y, z) -> (
  feet = '' + block(x, y, z);
  head = '' + block(x, y + 1, z);
  feet_kind = block_kind(feet);
  head_kind = block_kind(head);
  floor_bs = '' + block(x, y - 1, z);
  floor_kind = block_kind(floor_bs);
  if(feet_kind == 'SOLID' || head_kind == 'SOLID',
    'SOLID'
  ,
    if(feet_kind == 'LIQUID' || head_kind == 'LIQUID',
      'LIQUID'
    ,
      if(floor_kind == 'CLEAR' || floor_kind == 'LIQUID',
        'NO_FLOOR'
      ,
        'WALK'
      )
    )
  )
);

probe_heuristic(x, y, z, gx, gy, gz) -> (
  abs(x - gx) + abs(y - gy) + abs(z - gz)
);

probe_open_insert(open_set, f, g, x, y, z) -> (
  entry = l(f, g, x, y, z);
  count = length(open_set);
  insert_at = count;
  loop(count,
    if(insert_at == count && f < open_set:_:0, insert_at = _)
  );
  put(open_set:insert_at, entry, 'insert');
  open_set
);

minebot_pathfind_probe(name, payload) -> (
  params = if(length(payload) == 0, {}, decode_json(payload));
  start = params:'start';
  goal = params:'goal';
  grid_radius = floor(number(params:'grid_radius'));
  if(grid_radius < 1, grid_radius = 8);
  if(grid_radius > 64, grid_radius = 64);
  max_expand = 5000;
  if(params:'max_expand' != null, max_expand = floor(number(params:'max_expand')));
  sx = floor(number(start:0));
  sy = floor(number(start:1));
  sz = floor(number(start:2));
  gx = floor(number(goal:0));
  gy = floor(number(goal:1));
  gz = floor(number(goal:2));
  y_below = 3;
  y_above = 3;
  g_costs = {};
  came_from = {};
  start_key = probe_node_key(sx, sy, sz);
  g_costs:(start_key) = 0;
  h0 = probe_heuristic(sx, sy, sz, gx, gy, gz);
  open_set = l(l(h0, 0, sx, sy, sz));
  closed = {};
  expanded = 0;
  found = false;
  found_key = null;
  neighbors_dx = l(-1, 1, 0, 0, 0, 0);
  neighbors_dy = l(0, 0, -1, 1, 0, 0);
  neighbors_dz = l(0, 0, 0, 0, -1, 1);
  loop(max_expand,
    if(length(open_set) == 0 || found,
      null
    ,
      best = open_set:0;
      delete(open_set:0);
      cx = best:2;
      cy = best:3;
      cz = best:4;
      cur_g = best:1;
      cur_key = probe_node_key(cx, cy, cz);
      if(closed:(cur_key) != null,
        null
      ,
        closed:(cur_key) = true;
        expanded += 1;
        if(cx == gx && cy == gy && cz == gz,
          found = true;
          found_key = cur_key
        ,
          loop(6,
            ni = _;
            nx = cx + neighbors_dx:ni;
            ny = cy + neighbors_dy:ni;
            nz = cz + neighbors_dz:ni;
            nkey = probe_node_key(nx, ny, nz);
            if(closed:(nkey) != null,
              null
            ,
              if(abs(nx - sx) > grid_radius || abs(nz - sz) > grid_radius || ny < sy - y_below || ny > sy + y_above,
                null
              ,
                w = probe_walkability(nx, ny, nz);
                if(w == 'SOLID' || w == 'NO_FLOOR',
                  null
                ,
                  step_cost = if(w == 'LIQUID', 3.0, 1.0);
                  new_g = cur_g + step_cost;
                  old_g = g_costs:(nkey);
                  if(old_g == null || new_g < old_g,
                    g_costs:(nkey) = new_g;
                    came_from:(nkey) = cur_key;
                    h = probe_heuristic(nx, ny, nz, gx, gy, gz);
                    open_set = probe_open_insert(open_set, new_g + h, new_g, nx, ny, nz)
                  )
                )
              )
            )
          )
        )
      )
    )
  );
  path_length = 0;
  if(found,
    trace_key = found_key;
    loop(max_expand,
      if(trace_key == null || trace_key == start_key,
        null
      ,
        path_length += 1;
        trace_key = came_from:(trace_key)
      )
    )
  );
  reason = if(found, 'path_found', if(length(open_set) == 0, 'no_path', 'budget_exceeded'));
  str('{"ok":%s,"reason":"%s","nodes_expanded":%d,"path_length":%d,"grid_radius":%d,"start":[%d,%d,%d],"goal":[%d,%d,%d]}',
    json_bool(found), reason, expanded, path_length, grid_radius, sx, sy, sz, gx, gy, gz)
);

navigate_to_plan(sx, sy, sz, gx, gy, gz, grid_radius, max_expand, y_below, y_above, cover_target, min_partial_progress, goal_radius) -> (
  g_costs = {};
  came_from = {};
  start_key = probe_node_key(sx, sy, sz);
  g_costs:(start_key) = 0;
  h0 = probe_heuristic(sx, sy, sz, gx, gy, gz);
  open_set = l(l(h0, 0, sx, sy, sz));
  closed = {};
  los_cache = {};
  expanded = 0;
  found = false;
  found_key = null;
  best_key = start_key;
  best_h = h0;
  best_g = 0;
  neighbors_dx = l(-1, 1, 0, 0, 0, 0);
  neighbors_dy = l(0, 0, -1, 1, 0, 0);
  neighbors_dz = l(0, 0, 0, 0, -1, 1);
  loop(max_expand,
    if(length(open_set) == 0 || found,
      null
    ,
      best_entry = open_set:0;
      delete(open_set:0);
      cx = best_entry:2;
      cy = best_entry:3;
      cz = best_entry:4;
      cur_g = best_entry:1;
      cur_key = probe_node_key(cx, cy, cz);
      if(closed:(cur_key) != null,
        null
      ,
        closed:(cur_key) = true;
        expanded += 1;
        cur_h = probe_heuristic(cx, cy, cz, gx, gy, gz);
        if(cur_h < best_h || (cur_h == best_h && cur_g < best_g),
          best_h = cur_h;
          best_g = cur_g;
          best_key = cur_key
        );
        if(probe_heuristic(cx, cy, cz, gx, gy, gz) <= goal_radius,
          found = true;
          found_key = cur_key
        ,
          loop(6,
            ni = _;
            nx = cx + neighbors_dx:ni;
            ny = cy + neighbors_dy:ni;
            nz = cz + neighbors_dz:ni;
            nkey = probe_node_key(nx, ny, nz);
            if(closed:(nkey) != null,
              null
            ,
              if(abs(nx - sx) > grid_radius || abs(nz - sz) > grid_radius || ny < sy - y_below || ny > sy + y_above,
                null
              ,
                w = probe_walkability(nx, ny, nz);
                if(w == 'SOLID' || w == 'NO_FLOOR',
                  null
                ,
                  step_cost = if(w == 'LIQUID', 3.0, 1.0);
                  if(cover_target != null,
                    los_key = probe_node_key(nx, ny, nz);
                    exposed = los_cache:(los_key);
                    if(exposed == null,
                      exposed = if(los_clear(nx, ny + 1, nz, cover_target:0, cover_target:1, cover_target:2), 1, 0);
                      los_cache:(los_key) = exposed
                    );
                    if(exposed == 1, step_cost += 6.0)
                  );
                  new_g = cur_g + step_cost;
                  old_g = g_costs:(nkey);
                  if(old_g == null || new_g < old_g,
                    g_costs:(nkey) = new_g;
                    came_from:(nkey) = cur_key;
                    h = probe_heuristic(nx, ny, nz, gx, gy, gz);
                    open_set = probe_open_insert(open_set, new_g + h, new_g, nx, ny, nz)
                  )
                )
              )
            )
          )
        )
      )
  )
  );
  partial_progress = h0 - best_h;
  end_key = if(found || partial_progress >= min_partial_progress, if(found, found_key, best_key), start_key);
  if(end_key == start_key,
    l('result', if(found, 'arrived', if(length(open_set) == 0, 'no_path', 'budget_exceeded')), expanded, l())
  ,
    path = l();
    trace_key = end_key;
    loop(max_expand,
      if(trace_key == null || trace_key == start_key,
        null
      ,
        parts = split('_', trace_key);
        path += l(number(parts:0), number(parts:1), number(parts:2));
        trace_key = came_from:(trace_key)
      )
    );
    reversed_path = l();
    loop(length(path),
      reversed_path += path:(length(path) - 1 - _)
    );
    status = if(found, 'arrived', 'partial');
    l('result', status, expanded, reversed_path)
  )
);

start_navigate_to(name, action_id, gx, gy, gz, params) -> (
  watch_bot(name);
  p = bot_pos(name);
  if(p == null,
    emit('navigateStartTrace', name, l(action_id, 'missing_body', l(gx, gy, gz), 0, 0));
        emit('navigateDone', name, l(action_id, false, l(0, 0, 0), l(gx, gy, gz), 9999.0, 'missing_body', 0, 0, 0, 'missing_body', 0, 9999.0, 0, 0.0, 0, 0, l(0, 0, 0)));
    true
  ,
    (
      sx = floor(p:0);
      sy = floor(p:1);
      sz = floor(p:2);
      grid_radius = floor(param_number(params, 'grid_radius', 32));
      if(grid_radius < 1, grid_radius = 1);
      if(grid_radius > 64, grid_radius = 64);
      max_expand = floor(param_number(params, 'max_expand', 200));
      if(max_expand < 10, max_expand = 10);
      if(max_expand > 5000, max_expand = 5000);
      y_below = floor(param_number(params, 'y_below', 8));
      y_above = floor(param_number(params, 'y_above', 8));
      arrival_radius = param_number(params, 'arrival_radius', 0.75);
      timeout_ticks = floor(param_number(params, 'timeout_ticks', 400));
      no_progress_ticks = floor(param_number(params, 'no_progress_ticks', 60));
      min_partial_progress = floor(param_number(params, 'min_partial_progress', 5));
      if(min_partial_progress < 1, min_partial_progress = 1);
      goal_radius = floor(param_number(params, 'goal_radius', 0));
      if(goal_radius < 0, goal_radius = 0);
      plan_result = navigate_to_plan(sx, sy, sz, gx, gy, gz, grid_radius, max_expand, y_below, y_above, null, min_partial_progress, goal_radius);
      plan_status = plan_result:1;
      plan_expanded = plan_result:2;
      plan_path = plan_result:3;
      emit('navigateStartTrace', name, l(action_id, plan_status, l(gx, gy, gz), plan_expanded, length(plan_path)));
      if(plan_status == 'no_path' || plan_status == 'budget_exceeded' || length(plan_path) == 0,
        emit('navigateDone', name, l(action_id, false, p, l(gx, gy, gz), dist_to_target(p, gx, gy, gz), plan_status, plan_expanded, 0, 0, plan_status, 0, 9999.0, 0, 0.0, 0, 0, l(0, 0, 0)));
        if(plan_status == 'no_path',
          emit('mobilityBlocked', name, l('no_path', p, l(gx, gy, gz), plan_expanded))
        );
        true
      ,
        waypoints = l();
        loop(length(plan_path),
          wp = plan_path:_;
          waypoints += l(wp:0 + 0.5, wp:1, wp:2 + 0.5)
        );
        global_navigations:name = l(action_id, gx, gy, gz, plan_status, plan_expanded, length(waypoints), arrival_radius);
        last_wp = waypoints:(length(waypoints) - 1);
        move_ok = start_move_to(name, action_id, last_wp:0, last_wp:1, last_wp:2,
          {'waypoints' -> waypoints, 'arrival_radius' -> arrival_radius,
           'timeout_ticks' -> timeout_ticks, 'no_progress_ticks' -> no_progress_ticks,
           'max_deviation' -> 8.0});
        if(!move_ok,
          emit('navigateFinishTrace', name, l(action_id, false, 'move_start_failed', p, l(gx, gy, gz), dist_to_target(p, gx, gy, gz), plan_expanded, length(waypoints)));
          emit('navigateDone', name, l(action_id, false, p, l(gx, gy, gz), dist_to_target(p, gx, gy, gz), 'move_start_failed', plan_expanded, length(waypoints), 0, 'move_start_failed', 0, 9999.0, 0, 0.0, 0, length(waypoints), if(length(waypoints) > 0, waypoints:0, l(0, 0, 0))));
          global_navigations:name = null;
          true
        ,
          true
        )
      )
    )
  )
);

finish_navigate(name, move_event_data) -> (
  nav = global_navigations:name;
  if(nav == null,
    null
  ,
    action_id = nav:0;
    gx = nav:1;
    gy = nav:2;
    gz = nav:3;
    plan_status = nav:4;
    plan_expanded = nav:5;
    plan_waypoints = nav:6;
    p = bot_pos(name);
    goal_dist = if(p != null, dist_to_target(p, gx, gy, gz), 9999.0);
    move_arrived = move_event_data:1;
    move_reason = move_event_data:5;
    nav_arrived = move_arrived && plan_status == 'arrived';
    nav_reason = if(nav_arrived, 'arrived',
      if(plan_status == 'partial' && move_arrived, 'partial',
        if(move_reason == 'stuck', 'stuck',
          if(move_reason == 'timeout', 'timeout',
            if(move_reason == 'deviated', 'deviated', move_reason)))));
    emit('navigateFinishTrace', name, l(action_id, nav_arrived, nav_reason, p, l(gx, gy, gz), goal_dist, plan_expanded, plan_waypoints));
    emit('navigateDone', name, l(action_id, nav_arrived, p, l(gx, gy, gz), goal_dist, nav_reason, plan_expanded, plan_waypoints, 0, nav_reason, move_event_data:6, move_event_data:7, move_event_data:8, move_event_data:9, move_event_data:10, move_event_data:11, move_event_data:12));
    if(!nav_arrived && (nav_reason == 'stuck' || nav_reason == 'no_path'),
      emit('mobilityBlocked', name, l(nav_reason, p, l(gx, gy, gz), plan_expanded))
    );
    global_navigations:name = null
  )
);

resolve_follow_target(name, target_spec, radius) -> (
  if(length(target_spec) == 0,
    null
  ,
    pe = player_entity(target_spec);
    if(pe != null,
      pe
    ,
      target_entity_named_near(name, target_spec, radius)
    )
  )
);

follow_replan(name, target_pos) -> (
  f = global_follows:name;
  action_id = f:0;
  keep_radius = f:2;
  grid_radius = f:8;
  max_expand = f:9;
  p = bot_pos(name);
  sx = floor(p:0);
  sy = floor(p:1);
  sz = floor(p:2);
  gx = floor(target_pos:0);
  gy = floor(target_pos:1);
  gz = floor(target_pos:2);
  plan_result = navigate_to_plan(sx, sy, sz, gx, gy, gz, grid_radius, max_expand, 8, 8, null, 1, keep_radius);
  plan_status = plan_result:1;
  plan_expanded = plan_result:2;
  plan_path = plan_result:3;
  global_follows:name = l(f:0, f:1, f:2, f:3, f:4, f:5, target_pos, plan_expanded, f:8, f:9, f:10);
  direct_wp = l(gx + 0.5, gy, gz + 0.5);
  if(plan_status == 'no_path' || plan_status == 'budget_exceeded' || length(plan_path) == 0,
    start_move_to(name, action_id, direct_wp:0, direct_wp:1, direct_wp:2,
      {'waypoints' -> l(direct_wp), 'arrival_radius' -> keep_radius, 'timeout_ticks' -> 60, 'no_progress_ticks' -> 20, 'max_deviation' -> 8.0})
  ,
    waypoints = l();
    loop(length(plan_path),
      wp = plan_path:_;
      waypoints += l(wp:0 + 0.5, wp:1, wp:2 + 0.5)
    );
    if(length(waypoints) == 0,
      start_move_to(name, action_id, direct_wp:0, direct_wp:1, direct_wp:2,
        {'waypoints' -> l(direct_wp), 'arrival_radius' -> keep_radius, 'timeout_ticks' -> 60, 'no_progress_ticks' -> 20, 'max_deviation' -> 8.0})
    ,
      last_wp = waypoints:(length(waypoints) - 1);
      start_move_to(name, action_id, last_wp:0, last_wp:1, last_wp:2,
        {'waypoints' -> waypoints, 'arrival_radius' -> keep_radius, 'timeout_ticks' -> 60, 'no_progress_ticks' -> 20, 'max_deviation' -> 8.0})
    )
  )
);

start_follow(name, action_id, target_spec, params) -> (
  watch_bot(name);
  p = bot_pos(name);
  if(p == null,
    emit('followDone', name, l(action_id, false, l(0, 0, 0), 'missing_body'));
    true
  ,
    acquire_radius = floor(param_number(params, 'acquire_radius', 32));
    if(acquire_radius < 1, acquire_radius = 1);
    if(acquire_radius > 64, acquire_radius = 64);
    target = resolve_follow_target(name, target_spec, acquire_radius);
    if(target == null,
      emit('followDone', name, l(action_id, false, p, 'target_not_found'));
      true
    ,
      target_pos = query(target, 'pos');
      keep_radius = param_number(params, 'keep_radius', 3.0);
      if(keep_radius < 0, keep_radius = 0);
      replan_distance = param_number(params, 'replan_distance', 2.0);
      if(replan_distance < 0.5, replan_distance = 0.5);
      timeout_ticks = floor(param_number(params, 'timeout_ticks', 600));
      grid_radius = floor(param_number(params, 'grid_radius', 32));
      if(grid_radius < 1, grid_radius = 1);
      if(grid_radius > 64, grid_radius = 64);
      max_expand = floor(param_number(params, 'max_expand', 200));
      if(max_expand < 10, max_expand = 10);
      if(max_expand > 5000, max_expand = 5000);
      global_follows:name = l(action_id, target_spec, keep_radius, replan_distance, timeout_ticks, 0, target_pos, 0, grid_radius, max_expand, acquire_radius);
      emit('followStarted', name, l(action_id, target_spec, target_pos, keep_radius));
      if(dist_to_target(p, target_pos:0, target_pos:1, target_pos:2) > keep_radius,
        follow_replan(name, target_pos)
      );
      true
    )
  )
);

run_follow_tick(name, f) -> (
  action_id = f:0;
  target_spec = f:1;
  keep_radius = f:2;
  replan_distance = f:3;
  timeout_ticks = f:4;
  ticks = f:5 + 1;
  acquire_radius = f:10;
  if(timeout_ticks > 0 && ticks > timeout_ticks,
    p = bot_pos(name);
    target = resolve_follow_target(name, target_spec, acquire_radius);
    if(target == null,
      finish_follow(name, 'target_lost')
    ,
      tp = query(target, 'pos');
      if(dist_to_target(p, tp:0, tp:1, tp:2) <= keep_radius,
        finish_follow(name, 'arrived')
      ,
        finish_follow(name, 'timeout')
      )
    )
  ,
    p = bot_pos(name);
    target = resolve_follow_target(name, target_spec, acquire_radius);
    if(target == null,
      finish_follow(name, 'target_lost')
    ,
      tp = query(target, 'pos');
      dist = dist_to_target(p, tp:0, tp:1, tp:2);
      if(dist <= keep_radius,
        if(global_moves:name != null,
          finish_move(name, 'follow_hold', false)
        );
        stop_body(name);
        global_follows:name = l(f:0, f:1, f:2, f:3, f:4, ticks, f:6, f:7, f:8, f:9, f:10)
      ,
        last_plan_pos = f:6;
        need_replan = false;
        if(global_moves:name == null,
          need_replan = true
        ,
          if(last_plan_pos == null,
            need_replan = true
          ,
            if(dist_to_target(tp, last_plan_pos:0, last_plan_pos:1, last_plan_pos:2) >= replan_distance,
              need_replan = true
            )
          )
        );
        global_follows:name = l(f:0, f:1, f:2, f:3, f:4, ticks, f:6, f:7, f:8, f:9, f:10);
        if(need_replan,
          if(global_moves:name != null,
            finish_move(name, 'follow_replan', false)
          );
          follow_replan(name, tp)
        );
        m = global_moves:name;
        if(m != null && global_move_cancels:name == null,
          run_move_tick(name, m)
        )
      )
    )
  )
);

finish_follow(name, reason) -> (
  f = global_follows:name;
  if(f == null,
    null
  ,
    action_id = f:0;
    p = bot_pos(name);
    if(global_moves:name != null,
      finish_move(name, reason, false)
    );
    stop_body(name);
    emit('followDone', name, l(action_id, reason != 'timeout' && reason != 'target_lost' && reason != 'interrupted', p, reason));
    global_follows:name = null
  )
);

resolve_engage_target(name, target_spec, radius) -> (
  if(target_spec == 'nearest_hostile',
    nearest_hostile_near(name, radius)
  ,
    pe = player_entity(target_spec);
    if(pe != null,
      pe
    ,
      named = target_entity_named_near(name, target_spec, radius);
      if(named != null,
        named
      ,
        target_entity_near(name, target_spec, radius)
      )
    )
  )
);

nearest_hostile_near(name, radius) -> (
  p = bot_pos(name);
  if(p == null,
    null
  ,
    selector = str('@e[x=%d,y=%d,z=%d,distance=..%d,limit=32,sort=nearest]',
      floor(number(p:0)), floor(number(p:1)), floor(number(p:2)), radius);
    found = entity_selector(selector);
    result = null;
    loop(length(found),
      e = found:_;
      if(result == null && e != player(name) && is_hostile(e), result = e)
    );
    result
  )
);

engage_replan(name, target_pos) -> (
  e = global_engages:name;
  action_id = e:0;
  attack_range = e:2;
  grid_radius = e:8;
  max_expand = e:9;
  p = bot_pos(name);
  sx = floor(p:0);
  sy = floor(p:1);
  sz = floor(p:2);
  gx = floor(target_pos:0);
  gy = floor(target_pos:1);
  gz = floor(target_pos:2);
  cover = null;
  rt = target_entity_uuid_near(name, e:13, e:10);
  if(rt != null && is_ranged_hostile(rt), cover = target_pos);
  engage_max_expand = if(cover != null && max_expand > 120, 120, max_expand);
  plan_result = navigate_to_plan(sx, sy, sz, gx, gy, gz, grid_radius, engage_max_expand, 8, 8, cover, 1, attack_range);
  plan_status = plan_result:1;
  plan_expanded = plan_result:2;
  plan_path = plan_result:3;
  global_engages:name = l(e:0, e:1, e:2, e:3, e:4, e:5, target_pos, e:7, e:8, e:9, e:10, e:11, e:12, e:13);
  direct_wp = l(gx + 0.5, gy, gz + 0.5);
  if(plan_status == 'no_path' || plan_status == 'budget_exceeded' || length(plan_path) == 0,
    start_move_to(name, action_id, direct_wp:0, direct_wp:1, direct_wp:2,
      {'waypoints' -> l(direct_wp), 'arrival_radius' -> attack_range, 'timeout_ticks' -> 60, 'no_progress_ticks' -> 20, 'max_deviation' -> 8.0})
  ,
    waypoints = l();
    loop(length(plan_path),
      wp = plan_path:_;
      waypoints += l(wp:0 + 0.5, wp:1, wp:2 + 0.5)
    );
    if(length(waypoints) == 0,
      start_move_to(name, action_id, direct_wp:0, direct_wp:1, direct_wp:2,
        {'waypoints' -> l(direct_wp), 'arrival_radius' -> attack_range, 'timeout_ticks' -> 60, 'no_progress_ticks' -> 20, 'max_deviation' -> 8.0})
    ,
      last_wp = waypoints:(length(waypoints) - 1);
      start_move_to(name, action_id, last_wp:0, last_wp:1, last_wp:2,
        {'waypoints' -> waypoints, 'arrival_radius' -> attack_range, 'timeout_ticks' -> 60, 'no_progress_ticks' -> 20, 'max_deviation' -> 8.0})
    )
  )
);

start_engage(name, action_id, target_spec, params) -> (
  watch_bot(name);
  p = bot_pos(name);
  if(p == null,
    emit('engageDone', name, l(action_id, false, target_spec, l(0, 0, 0), 'missing_body', null, 0));
    true
  ,
    acquire_radius = floor(param_number(params, 'acquire_radius', 32));
    if(acquire_radius < 1, acquire_radius = 1);
    if(acquire_radius > 64, acquire_radius = 64);
    target = resolve_engage_target(name, target_spec, acquire_radius);
    if(target == null,
      emit('engageDone', name, l(action_id, false, target_spec, p, 'target_not_found', null, 0));
      true
    ,
      target_pos = query(target, 'pos');
      attack_range = param_number(params, 'attack_range', 2.0);
      if(attack_range < 1.2, attack_range = 1.2);
      if(attack_range > 3.0, attack_range = 3.0);
      cooldown_ticks = floor(param_number(params, 'cooldown_ticks', 10));
      if(cooldown_ticks < 1, cooldown_ticks = 1);
      timeout_ticks = floor(param_number(params, 'timeout_ticks', 400));
      if(timeout_ticks < 1, timeout_ticks = 1);
      grid_radius = floor(param_number(params, 'grid_radius', 32));
      if(grid_radius < 1, grid_radius = 1);
      if(grid_radius > 64, grid_radius = 64);
      max_expand = floor(param_number(params, 'max_expand', 200));
      if(max_expand < 10, max_expand = 10);
      if(max_expand > 5000, max_expand = 5000);
      disengage_health = param_number(params, 'disengage_health', 6.0);
      if(disengage_health < 0, disengage_health = 0);
      target_uuid = query(target, 'uuid');
      global_engages:name = l(action_id, target_spec, attack_range, cooldown_ticks, timeout_ticks, 0, target_pos, 0, grid_radius, max_expand, acquire_radius, disengage_health, 0, target_uuid);
      emit('engageStarted', name, l(action_id, target_spec, target_pos, attack_range));
      if(dist_to_target(p, target_pos:0, target_pos:1, target_pos:2) > attack_range,
        engage_replan(name, target_pos)
      );
      true
    )
  )
);

run_engage_tick(name, e) -> (
  action_id = e:0;
  target_spec = e:1;
  attack_range = e:2;
  cooldown_ticks = e:3;
  timeout_ticks = e:4;
  ticks = e:5 + 1;
  acquire_radius = e:10;
  disengage_health = e:11;
  attacks = e:12;
  if(timeout_ticks > 0 && ticks > timeout_ticks,
    finish_engage(name, 'timeout')
  ,
    p = bot_pos(name);
    target = target_entity_uuid_near(name, e:13, acquire_radius);
    if(target == null,
      if(attacks > 0,
        finish_engage(name, 'killed')
      ,
        finish_engage(name, 'target_lost')
      )
    ,
      tp = query(target, 'pos');
      thp = entity_health(target);
      if(thp != null && thp <= 0,
        finish_engage(name, 'killed')
      ,
        bhp = bot_health(name);
        if(bhp != null && disengage_health > 0 && bhp <= disengage_health,
          finish_engage(name, 'disengaged_low_health')
        ,
          dist = dist_to_target(p, tp:0, tp:1, tp:2);
          if(dist <= attack_range && los_clear(p:0, p:1 + 1.0, p:2, tp:0, tp:1, tp:2),
            if(global_moves:name != null,
              finish_move(name, 'engage_hold', false)
            );
            stop_body(name);
            run(str('player %s look at %.3f %.3f %.3f', name, tp:0, tp:1 + 1.0, tp:2));
            last_attack_tick = e:7;
            if(last_attack_tick == 0 || ticks - last_attack_tick >= cooldown_ticks,
              run('player ' + name + ' attack once');
              attacks += 1;
              last_attack_tick = ticks
            );
            global_engages:name = l(e:0, e:1, e:2, e:3, e:4, ticks, e:6, last_attack_tick, e:8, e:9, e:10, e:11, attacks, e:13)
          ,
            last_plan_pos = e:6;
            need_replan = false;
            if(global_moves:name == null,
              need_replan = true
            ,
              if(last_plan_pos == null,
                need_replan = true
              ,
                if(dist_to_target(tp, last_plan_pos:0, last_plan_pos:1, last_plan_pos:2) >= 2.0,
                  need_replan = true
                )
              )
            );
            global_engages:name = l(e:0, e:1, e:2, e:3, e:4, ticks, e:6, e:7, e:8, e:9, e:10, e:11, e:12, e:13);
            if(need_replan,
              if(global_moves:name != null,
                finish_move(name, 'engage_replan', false)
              );
              engage_replan(name, tp)
            );
            m = global_moves:name;
            if(m != null && global_move_cancels:name == null,
              run_move_tick(name, m)
            )
          )
        )
      )
    )
  )
);

finish_engage(name, reason) -> (
  e = global_engages:name;
  if(e == null,
    null
  ,
    action_id = e:0;
    target_spec = e:1;
    p = bot_pos(name);
    if(global_moves:name != null,
      finish_move(name, reason, false)
    );
    stop_body(name);
    success = reason == 'killed';
    thp = null;
    target = target_entity_uuid_near(name, e:13, e:10);
    if(target != null, thp = entity_health(target));
    emit('engageDone', name, l(action_id, success, target_spec, p, reason, thp, e:12));
    global_engages:name = null
  )
);

minebot_reset() -> (
  global_events = [];
  global_tick = 0;
  global_moves = {};
  global_move_cancels = {};
  global_navigations = {};
  global_follows = {};
  global_mines = {};
  global_places = {};
  global_uses = {};
  global_ignites = {};
  global_sows = {};
  global_attacks = {};
  global_ranged = {};
  global_drops = {};
  global_owners = {};
  global_reflexes = {};
  global_pending_reflexes = {};
  global_watched = {};
  global_reflex_scan = true;
  global_water_reflex_air_threshold = 80;
  global_water_reflex_damage_budget = null;
  global_water_reflex_health_baselines = {};
  global_combat_health_baselines = {};
  global_engages = {};
  global_pending_spawns = {};
  global_respawn_notices = {};
  global_missing_notices = {};
  global_agent_chat_events = [];
  global_action_results = {};
  result_json(null, 'server', true, true, '{}', null)
);

minebot_spawn(name, payload) -> (
  params = if(length(payload) == 0, {}, decode_json(payload));
  spawn_cmd = 'player ' + name + ' spawn';
  pos = params:'pos';
  if(pos != null,
    spawn_cmd += str(' at %d %d %d',
      floor(number(pos:0)),
      floor(number(pos:1)),
      floor(number(pos:2)))
  );
  if(params:'yaw' != null && params:'pitch' != null,
    spawn_cmd += str(' facing %.3f %.3f', number(params:'yaw'), number(params:'pitch'))
  );
  if(params:'dimension' != null,
    spawn_cmd += ' in ' + params:'dimension'
  );
  global_pending_spawns:name = l(params:'pos', params:'yaw', params:'pitch', params:'gamemode', params:'emit_respawned');
  run(spawn_cmd);
  finalize_pending_spawn(name);
  result_json(null, name, true, true, '{"action":"spawn"}', null)
);

minebot_despawn(name) -> (
  run('player ' + name + ' kill');
  result_json(null, name, true, true, '{"action":"despawn"}', null)
);

minebot_state(name) -> state_json(name);

minebot_perceive(name, scope, payload) -> (
  params = if(length(payload) == 0, {}, decode_json(payload));
  if(scope == 'blockAt',
    perceive_block_at(name, params)
  ,
    if(scope == 'blockCells',
      perceive_block_cells(name, params)
    ,
    if(scope == 'nearbyBlocks',
      perceive_nearby_blocks(name, params)
    ,
      if(scope == 'findBlocks',
        perceive_find_blocks(name, params)
      ,
        if(scope == 'nearbyEntities',
          perceive_nearby_entities(name, params)
        ,
          if(scope == 'debugBlocks',
            perceive_debug_blocks(name, params)
          ,
          if(scope == 'inventory',
            perceive_inventory(name, params)
          ,
            if(scope == 'container',
              perceive_container(name, params)
            ,
              if(scope == 'recipeData',
                perceive_recipe_data(name, params)
              ,
                if(scope == 'nearbyHostiles',
                  perceive_hostiles(name, params)
                ,
                  perception_json(name, scope, false, true, '{}', '[]', null, 'unknown perception scope')
                )
              )
            )
          )
          )
        )
      )
    )
    )
  )
);

minebot_drain_events(name) -> (
  events_json(name, global_events)
);

minebot_events_since(name, since_seq) -> (
  events_since_json(name, global_events, number(since_seq))
);

minebot_drain_chat(name) -> (
  events_json(name, global_agent_chat_events)
);

minebot_chat_since(name, since_seq) -> (
  events_since_json(name, global_agent_chat_events, number(since_seq))
);

minebot_interrupt(name, payload) -> (
  stop_body(name);
  if(global_navigations:name != null,
    global_navigations:name = null
  );
  if(global_follows:name != null,
    finish_follow(name, 'interrupted')
  );
  if(global_engages:name != null,
    finish_engage(name, 'interrupted')
  );
  if(global_moves:name != null,
    request_move_cancel(name, 'interrupted')
  );
  if(global_mines:name != null,
    finish_mine(name, 'interrupted')
  );
  if(global_places:name != null,
    finish_place(name, 'interrupted')
  );
  if(global_uses:name != null,
    finish_use(name, 'interrupted')
  );
  if(global_ignites:name != null,
    finish_ignite(name, 'interrupted')
  );
  if(global_sows:name != null,
    finish_sow(name, 'interrupted')
  );
  if(global_ranged:name != null,
    finish_ranged(name, 'interrupted')
  );
  if(global_attacks:name != null,
    finish_attack(name, 'interrupted')
  );
  if(global_drops:name != null,
    finish_drop(name, 'interrupted')
  );
  result_json(null, name, true, true, '{"action":"interrupt"}', null)
);

minebot_action(name, payload) -> (
  if(length(payload) == 0,
    result_json('unknown', name, false, false, '{}', 'empty payload')
  ,
    action = decode_json(payload);
    action_id = action:'id';
    action_name = action:'name';
    params = action:'params';
    remembered = remembered_action_result(name, action_id);
    if(remembered != null,
      remembered
    ,
      out = result_json(action_id, name, false, false, '{}', 'unknown action');
      if(action_name == 'moveTo',
        target = params:'target';
        ok = start_move_to(name, action_id, number(target:0), number(target:1), number(target:2), params);
        out = result_json(action_id, name, true, ok, '{"action":"moveTo"}', null)
      );
      if(action_name == 'navigateTo',
        target = params:'target';
        ok = start_navigate_to(name, action_id, number(target:0), number(target:1), number(target:2), params);
        out = result_json(action_id, name, true, ok, '{"action":"navigateTo"}', null)
      );
      if(action_name == 'followEntity',
        target_spec = params:'target_spec';
        ok = start_follow(name, action_id, target_spec, params);
        out = result_json(action_id, name, true, ok, '{"action":"followEntity"}', null)
      );
      if(action_name == 'engageEntity',
        target_spec = params:'target_spec';
        ok = start_engage(name, action_id, target_spec, params);
        out = result_json(action_id, name, true, ok, '{"action":"engageEntity"}', null)
      );
      if(action_name == 'lookAt',
        target = params:'target';
        ok = run_look_at(name, action_id, number(target:0), number(target:1), number(target:2));
        out = result_json(action_id, name, true, ok, '{"action":"lookAt"}', null)
      );
      if(action_name == 'jump',
        ok = run_jump_once(name, action_id);
        out = result_json(action_id, name, true, ok, '{"action":"jump"}', null)
      );
      if(action_name == 'selectSlot',
        slot = number(params:'slot');
        ok = run_select_slot(name, action_id, slot);
        out = result_json(action_id, name, true, ok, '{"action":"selectSlot"}', null)
      );
      if(action_name == 'selectItem',
        item = params:'item';
        ok = run_select_item(name, action_id, item);
        out = result_json(action_id, name, true, ok, '{"action":"selectItem"}', null)
      );
      if(action_name == 'stop',
        ok = run_stop_action(name, action_id);
        out = result_json(action_id, name, true, ok, '{"action":"stop"}', null)
      );
      if(action_name == 'useItem',
        ok = start_use_item(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"useItem"}', null)
      );
      if(action_name == 'rangedAttack',
        ok = start_ranged_attack(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"rangedAttack"}', null)
      );
      if(action_name == 'attackEntity',
        ok = start_attack_entity(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"attackEntity"}', null)
      );
      if(action_name == 'dropItem',
        ok = start_drop_item(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"dropItem"}', null)
      );
      if(action_name == 'handoffItem',
        ok = run_handoff_item(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"handoffItem"}', null)
      );
      if(action_name == 'moveItem',
        ok = run_move_item(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"moveItem"}', null)
      );
      if(action_name == 'craftItem',
        ok = run_craft_item(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"craftItem"}', null)
      );
      if(action_name == 'furnaceTransfer',
        ok = run_furnace_transfer(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"furnaceTransfer"}', null)
      );
      if(action_name == 'containerTransfer',
        ok = run_container_transfer(name, action_id, params);
        out = result_json(action_id, name, true, ok, '{"action":"containerTransfer"}', null)
      );
      if(action_name == 'mineBlock',
        target = params:'target';
        ok = start_mine_block(name, action_id, floor(number(target:0)), floor(number(target:1)), floor(number(target:2)), params);
        out = result_json(action_id, name, true, ok, '{"action":"mineBlock"}', null)
      );
      if(action_name == 'placeBlock',
        target = params:'target';
        ok = start_place_block(name, action_id, floor(number(target:0)), floor(number(target:1)), floor(number(target:2)), params);
        out = result_json(action_id, name, true, ok, '{"action":"placeBlock"}', null)
      );
      if(action_name == 'igniteBlock',
        target = params:'target';
        ok = start_ignite_block(name, action_id, floor(number(target:0)), floor(number(target:1)), floor(number(target:2)), params);
        out = result_json(action_id, name, true, ok, '{"action":"igniteBlock"}', null)
      );
      if(action_name == 'sowCrop',
        target = params:'target';
        ok = start_sow_crop(name, action_id, floor(number(target:0)), floor(number(target:1)), floor(number(target:2)), params);
        out = result_json(action_id, name, true, ok, '{"action":"sowCrop"}', null)
      );
      remember_action_result(name, action_id, out)
    );
  )
);
