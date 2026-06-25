global_events = [];
global_seq = 0;
global_tick = 0;
global_moves = {};
global_owners = {};
global_reflexes = {};
global_watched = {};
global_reflex_scan = true;
global_scan_radius = 1;

priority_value(priority) -> (
  if(priority == 'SURVIVAL', 100,
    if(priority == 'ACTION', 10, 0)
  )
);

bot(name) -> player(name);

bot_pos(name) -> query(bot(name), 'pos');

emit(kind, name, data) -> (
  global_seq += 1;
  global_events += l(l(global_seq, global_tick, kind, name, data));
  global_seq
);

drain_events() -> (
  ev = global_events;
  global_events = [];
  ev
);

reset_spike() -> (
  global_events = [];
  global_seq = 0;
  global_tick = 0;
  global_moves = {};
  global_owners = {};
  global_reflexes = {};
  global_watched = {};
  global_reflex_scan = true;
  true
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
      emit('ownerPreempted', name, l(cur, l(owner, priority)))
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
    emit('ownerReleased', name, owner);
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
    dx = m:0 - p:0;
    dy = m:1 - p:1;
    dz = m:2 - p:2;
    dist = sqrt(dx*dx + dy*dy + dz*dz);
    stop_body(name);
    emit('moveDone', name, l('arrived', arrived, 'final_pos', p, 'target', l(m:0, m:1, m:2), 'dist_to_target', dist, 'stopped_reason', reason, 'ticks', m:4));
    global_moves:name = null;
    release_owner(name, 'moveTo');
    true
  )
);

spike_moveTo(name, x, y, z) -> (
  acquired = acquire_owner(name, 'moveTo', 'ACTION');
  if(!acquired,
    emit('moveRejected', name, l('owner', owner_of(name)));
    false
  ,
    watch_bot(name);
    p = bot_pos(name);
    global_moves:name = l(x, y, z, 0.75, 0, p);
    emit('moveStarted', name, l('start_pos', p, 'target', l(x, y, z)));
    run('player ' + name + ' sprint');
    true
  )
);

cancel_move_preempted(name) -> (
  if(global_moves:name != null,
    finish_move(name, 'preempted', false)
  )
);

is_lava_at(x, y, z) -> (
  bs = '' + block(x, y, z);
  bs == 'lava' || bs == 'minecraft:lava'
);

is_solid_floor(x, y, z) -> (
  bs = '' + block(x, y - 1, z);
  bs != 'air' && bs != 'minecraft:air' && bs != 'lava' && bs != 'minecraft:lava'
);

is_safe_cell(x, y, z) -> (
  here = '' + block(x, y, z);
  head = '' + block(x, y + 1, z);
  here != 'lava' && here != 'minecraft:lava' &&
  head != 'lava' && head != 'minecraft:lava' &&
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

start_lava_reflex(name) -> (
  if(acquire_owner(name, 'lavaReflex', 'SURVIVAL'),
    p = bot_pos(name);
    target = safe_escape_target(p);
    cancel_move_preempted(name);
    if(target == null,
      emit('reflexFailed', name, l('reason', 'no_safe_adjacent', 'pos', p));
      release_owner(name, 'lavaReflex');
      false
    ,
      global_reflexes:name = l(target:0, target:1, target:2, 0);
      emit('reflexTriggered', name, l('kind', 'lava', 'pos', p, 'target', target));
      true
    )
  ,
    false
  )
);

run_move_tick(name, m) -> (
  p = bot_pos(name);
  dx = m:0 - p:0;
  dy = m:1 - p:1;
  dz = m:2 - p:2;
  dist = sqrt(dx*dx + dy*dy + dz*dz);
  ticks = m:4 + 1;
  global_moves:name = l(m:0, m:1, m:2, m:3, ticks, m:5);
  if(dist <= m:3,
    finish_move(name, 'arrived', true)
  ,
    if(ticks > 260,
      finish_move(name, 'timeout', false)
    ,
      run(str('player %s look at %.3f %.3f %.3f', name, m:0, m:1 + 1.0, m:2));
      run('player ' + name + ' move forward')
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
  global_reflexes:name = l(r:0, r:1, r:2, ticks);
  if(dist <= 0.9 && !lava_near_pos(p),
    stop_body(name);
    emit('reflexCompleted', name, l('final_pos', p, 'dist_to_escape', dist, 'ticks', ticks, 'escaped_lava', true));
    global_reflexes:name = null;
    release_owner(name, 'lavaReflex')
  ,
    if(ticks > 100,
      stop_body(name);
      emit('reflexCompleted', name, l('final_pos', p, 'dist_to_escape', dist, 'ticks', ticks, 'escaped_lava', false, 'reason', 'timeout'));
      global_reflexes:name = null;
      release_owner(name, 'lavaReflex')
    ,
      run(str('player %s look at %.3f %.3f %.3f', name, r:0, r:1 + 1.0, r:2));
      run('player ' + name + ' move forward')
    )
  )
);

tick_bot(name) -> (
  p = bot_pos(name);
  if(global_reflex_scan && global_reflexes:name == null && lava_near_pos(p),
    start_lava_reflex(name)
  );
  r = global_reflexes:name;
  if(r != null,
    run_reflex_tick(name, r)
  ,
    m = global_moves:name;
    if(m != null,
      run_move_tick(name, m)
    )
  )
);

__on_tick() -> (
  global_tick += 1;
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
  reflex_names = keys(global_reflexes);
  loop(length(reflex_names),
    if(global_moves:(reflex_names:_) == null && global_watched:(reflex_names:_) == null,
      tick_bot(reflex_names:_)
    )
  )
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

tick_count() -> global_tick;
