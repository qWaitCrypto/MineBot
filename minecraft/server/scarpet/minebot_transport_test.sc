global_events = [];
global_seq = 0;
global_walk_target = null;
global_walk_name = null;
global_walk_ticks = 0;
global_tick_counter = 0;
global_tick_probe = false;
global_reflex_enabled = false;
global_reflex_triggered = false;

reset_transport() -> (
  global_events = [];
  global_seq = 0;
  global_walk_target = null;
  global_walk_name = null;
  global_walk_ticks = 0;
  global_tick_counter = 0;
  global_tick_probe = false;
  global_reflex_enabled = false;
  global_reflex_triggered = false;
  true
);

big_payload(size) -> (
  s = '';
  loop(size, s += 'x');
  s
);

bot(name) -> player(name);

pos_of(name) -> query(bot(name), 'pos');

emit(kind, data) -> (
  global_seq += 1;
  global_events += l(l(global_seq, global_tick_counter, kind, data));
  global_seq
);

drain_events() -> (
  ev = global_events;
  global_events = [];
  ev
);

queue_len() -> length(global_events);

__on_player_takes_damage(player, amount, source, entity) -> (
  emit('damage', l(query(player, 'name'), amount, source))
);

__on_player_deals_damage(player, amount, entity) -> (
  emit('dealsDamage', l(query(player, 'name'), amount, entity))
);

__on_player_picks_up_item(player, item_tuple) -> (
  emit('itemPickup', l(query(player, 'name'), item_tuple))
);

__on_player_dies(player) -> (
  emit('death', l(query(player, 'name'), query(player, 'pos')))
);

__on_tick() -> (
  global_tick_counter += 1;
  if(global_tick_probe, dummy = global_tick_counter);
  if(global_walk_target != null,
    p = query(bot(global_walk_name), 'pos');
    dx = global_walk_target:0 - p:0;
    dz = global_walk_target:2 - p:2;
    d2 = dx*dx + dz*dz;
    global_walk_ticks += 1;
    if(global_reflex_enabled && !global_reflex_triggered && '' + block(floor(p:0), floor(p:1), floor(p:2)) == 'lava',
      global_reflex_triggered = true;
      emit('reflexTriggered', l(global_walk_name, 'lava'));
      run('player ' + global_walk_name + ' stop');
      run(str('player %s look at %.3f %.3f %.3f', global_walk_name, p:0 - 8, p:1, p:2));
      run('player ' + global_walk_name + ' move forward');
    );
    if(d2 < 1 || global_walk_ticks > 2400,
      run('player ' + global_walk_name + ' stop');
      emit('walkDone', l(global_walk_name, p, global_walk_ticks));
      global_walk_target = null;
      global_walk_name = null;
    ,
      run(str('player %s look at %.3f %.3f %.3f', global_walk_name, global_walk_target:0, global_walk_target:1, global_walk_target:2));
      run('player ' + global_walk_name + ' move forward')
    )
  )
);

start_walk(name, x, y, z) -> (
  global_walk_name = name;
  global_walk_target = l(x, y, z);
  global_walk_ticks = 0;
  emit('walkStarted', l(name, query(bot(name), 'pos'), global_walk_target));
  run('player ' + name + ' sprint');
  true
);

stop_walk(name) -> (
  run('player ' + name + ' stop');
  run('player ' + name + ' unsprint');
  global_walk_target = null;
  global_walk_name = null;
  emit('walkStopped', l(name, query(bot(name), 'pos')));
  true
);

enable_tick_probe(flag) -> (
  global_tick_probe = flag;
  global_tick_counter = 0;
  true
);

tick_count() -> global_tick_counter;

enable_reflex(flag) -> (
  global_reflex_enabled = flag;
  global_reflex_triggered = false;
  true
);

region_blocks(x0, y0, z0, sx, sy, sz) -> (
  palette = {};
  palette_list = [];
  entries = [];
  loop(sx,
    x = _;
    loop(sy,
      y = _;
      loop(sz,
        z = _;
        b = block(x0 + x, y0 + y, z0 + z);
        if(b != 'minecraft:air',
          bid = palette:b;
          if(bid == null,
            bid = length(palette_list);
            palette:b = bid;
            palette_list += l(b)
          );
          entries += l(l(x, y, z, bid))
        )
      )
    )
  );
  l(sx, sy, sz, palette_list, entries)
);

region_blocks_compact(x0, y0, z0, sx, sy, sz) -> (
  palette = {};
  palette_list = [];
  entries = '';
  count = 0;
  loop(sx,
    x = _;
    loop(sy,
      y = _;
      loop(sz,
        z = _;
        b = block(x0 + x, y0 + y, z0 + z);
        bs = '' + b;
        if(bs != 'air' && bs != 'minecraft:air',
          bid = palette:bs;
          if(bid == null,
            bid = length(palette_list);
            palette:bs = bid;
            palette_list += l(bs)
          );
          entries += str('%d,%d,%d,%d;', x, y, z, bid);
          count += 1
        )
      )
    )
  );
  str('%d,%d,%d|%s|%d|%s', sx, sy, sz, palette_list, count, entries)
);

chunk_probe(x, z) -> (
  // Reading a block forces or verifies access at that location from Scarpet.
  l(block(x, 60, z), block(x, 59, z), pos_of('TestBot'))
);
