global_results = {};
global_move_target = null;
global_move_mode = null;
global_move_ticks = 0;
global_events = {};

record(key, value) -> (
  global_results:key = value;
  value
);

result(key) -> global_results:key;

reset_results() -> (
  global_results = {};
  global_move_target = null;
  global_move_mode = null;
  global_move_ticks = 0;
  global_events = {};
  true
);

bot() -> player('TestBot');

bot_pos() -> query(bot(), 'pos');

bot_health() -> query(bot(), 'health');

test_ping() -> record('ping', 'pong');

__on_tick() -> (
  if(global_move_target != null,
    p = query(bot(), 'pos');
    dx = global_move_target:0 - p:0;
    dy = global_move_target:1 - p:1;
    dz = global_move_target:2 - p:2;
    d2 = dx*dx + dz*dz;
    global_move_ticks += 1;
    if(d2 < 0.35*0.35 || global_move_ticks > 220,
      run('player TestBot stop');
      record(global_move_mode + '_final_pos', p);
      record(global_move_mode + '_ticks', global_move_ticks);
      global_move_target = null;
      global_move_mode = null;
    ,
      run(str('player TestBot look at %.3f %.3f %.3f', global_move_target:0, global_move_target:1, global_move_target:2));
      if(global_move_mode == 'swim', run('player TestBot jump once'));
      if(global_move_mode == 'ladder', run('player TestBot jump once'));
      run('player TestBot move forward')
    )
  )
);

start_move(mode, x, y, z) -> (
  global_move_mode = mode;
  global_move_target = l(x, y, z);
  global_move_ticks = 0;
  record(mode + '_start_pos', bot_pos());
  true
);

craft_oak_planks() -> (
  inventory_set('TestBot', 0, 0);
  inventory_set('TestBot', 1, 8, 'oak_planks');
  record('craft_oak_planks_slot0', inventory_get('TestBot', 0));
  record('craft_oak_planks_slot1', inventory_get('TestBot', 1))
);

recipe_probe() -> (
  record('recipe_oak_planks', recipe_data('minecraft:oak_planks'))
);

same_tick_attack() -> (
  z = entity_selector('@e[type=zombie,limit=1,sort=nearest]'):0;
  run(str('player TestBot look at %.3f %.3f %.3f', query(z, 'pos'):0, query(z, 'pos'):1 + 1, query(z, 'pos'):2));
  run('player TestBot attack once');
  record('same_tick_attack_done', true)
);

cooldown_probe() -> (
  record('cooldown_probe_note', 'query these fields individually from RCON: attack_cooldown, attack_cooldown_progress, cooldown, last_attack_time, nbt')
);

container_transfer(x, y, z) -> (
  stack = inventory_get(l(x, y, z), 0);
  inventory_set('TestBot', 0, stack:1, stack:0);
  inventory_set(l(x, y, z), 0, 0);
  record('container_bot_slot0', inventory_get('TestBot', 0));
  record('container_chest_slot0', inventory_get(l(x, y, z), 0))
);

network_probe() -> (
  record('network_probe_note', 'query candidate network functions individually from RCON')
);

__on_player_takes_damage(player, amount, source, entity) -> (
  if(query(player, 'name') == 'TestBot',
    global_events:'damage' = l(amount, source, entity);
    record('damage_event', global_events:'damage')
  )
);

__on_player_deals_damage(player, amount, entity) -> (
  if(query(player, 'name') == 'TestBot',
    global_events:'deals_damage' = l(amount, entity);
    record('deals_damage_event', global_events:'deals_damage')
  )
);

__on_player_dies(player) -> (
  if(query(player, 'name') == 'TestBot',
    global_events:'dies' = true;
    record('death_event', true);
    schedule(20, 'respawn_bot')
  )
);

respawn_bot() -> (
  run('player TestBot spawn');
  record('respawn_scheduled', true)
);
