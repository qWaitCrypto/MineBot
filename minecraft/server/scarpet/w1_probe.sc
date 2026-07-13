__config() -> {'scope' -> 'global'};

w1_is_log(b) -> (
  b == 'oak_log' || b == 'minecraft:oak_log' ||
  b == 'spruce_log' || b == 'minecraft:spruce_log' ||
  b == 'birch_log' || b == 'minecraft:birch_log' ||
  b == 'jungle_log' || b == 'minecraft:jungle_log' ||
  b == 'acacia_log' || b == 'minecraft:acacia_log' ||
  b == 'dark_oak_log' || b == 'minecraft:dark_oak_log'
);

w1_is_leaf(b) -> (
  b == 'oak_leaves' || b == 'minecraft:oak_leaves' ||
  b == 'spruce_leaves' || b == 'minecraft:spruce_leaves' ||
  b == 'birch_leaves' || b == 'minecraft:birch_leaves' ||
  b == 'jungle_leaves' || b == 'minecraft:jungle_leaves' ||
  b == 'acacia_leaves' || b == 'minecraft:acacia_leaves' ||
  b == 'dark_oak_leaves' || b == 'minecraft:dark_oak_leaves'
);

w1_clear(b) -> b == 'air' || b == 'minecraft:air';

w1_liquid(b) -> b == 'water' || b == 'minecraft:water' || b == 'lava' || b == 'minecraft:lava';

w1_support(b) -> !w1_clear(b) && !w1_liquid(b);

w1_ground_support(b) -> w1_support(b) && !w1_is_log(b) && !w1_is_leaf(b);

w1_surface_y(x, z, ymin, ymax) -> (
  surf = null;
  loop(ymax - ymin + 1,
    y = ymax - _;
    b = '' + block(x, y, z);
    a = '' + block(x, y + 1, z);
    if(surf == null && w1_support(b) && w1_clear(a), surf = y + 1)
  );
  surf
);

w1_ground_y_below_log(x, z, miny, ymin) -> (
  gy = null;
  loop(miny - ymin + 1,
    y = miny - 1 - _;
    if(gy == null,
      b = '' + block(x, y, z);
      if(w1_ground_support(b), gy = y + 1)
    )
  );
  gy
);

w1_safe_spawn_y(cx, cz, ymin, ymax) -> (
  candidate = null;
  loop(ymax - ymin + 1,
    y = ymax - _;
    if(candidate == null,
      feet = '' + block(cx, y, cz);
      head = '' + block(cx, y + 1, cz);
      support = '' + block(cx, y - 1, cz);
      if(w1_clear(feet) && w1_clear(head) && w1_ground_support(support), candidate = y)
    )
  );
  candidate
);

w1_probe(cx, cz, r, ymin, ymax, sy) -> (
  cols = 0;
  grounded = 0;
  samples = '';
  gsamples = '';
  sample_count = 0;
  gsample_count = 0;
  loop(2*r + 1,
    dx = _ - r;
    loop(2*r + 1,
      dz = _ - r;
      x = cx + dx;
      z = cz + dz;
      if(dx*dx + dz*dz <= r*r,
        found = false;
        miny = 0;
        mintype = '';
        loop(ymax - ymin + 1,
          y = ymin + _;
          b = '' + block(x, y, z);
          if(!found && w1_is_log(b),
            found = true;
            miny = y;
            mintype = b
          )
        );
        if(found,
          cols += 1;
          surf = w1_ground_y_below_log(x, z, miny, ymin);
          if(sample_count < 12,
            if(sample_count > 0, samples += ',');
            samples += str('{"pos":[%d,%d,%d],"type":"%s","surface_y":%s}', x, miny, z, mintype, if(surf == null, 'null', str('%d', surf)));
            sample_count += 1
          );
          if(surf != null && miny >= surf - 1 && miny <= surf + 2,
            grounded += 1;
            if(gsample_count < 12,
              if(gsample_count > 0, gsamples += ',');
              gsamples += str('{"pos":[%d,%d,%d],"type":"%s","surface_y":%d}', x, miny, z, mintype, surf);
              gsample_count += 1
            )
          )
        )
      )
    )
  );
  auto_sy = w1_safe_spawn_y(cx, cz, ymin, ymax);
  if(auto_sy != null, sy = auto_sy);
  feet = '' + block(cx, sy, cz);
  head = '' + block(cx, sy + 1, cz);
  support = '' + block(cx, sy - 1, cz);
  liq = 0;
  minh = null;
  maxh = null;
  loop(5,
    dx = _ - 2;
    loop(5,
      dz = _ - 2;
      x = cx + dx;
      z = cz + dz;
      top = w1_surface_y(x, z, sy - 16, sy + 16);
      if(top != null,
        if(minh == null || top < minh, minh = top);
        if(maxh == null || top > maxh, maxh = top)
      );
      loop(4,
        y = sy - 2 + _;
        b = '' + block(x, y, z);
        if(w1_liquid(b), liq += 1)
      )
    )
  );
  height_delta = if(minh == null || maxh == null, null, maxh - minh);
  spawn_safe = w1_clear(feet) && w1_clear(head) && w1_ground_support(support) && liq == 0 && height_delta != null && height_delta <= 8;
  str('{"center":[%d,%d],"radius":%d,"columns":%d,"grounded_columns":%d,"column_samples":[%s],"grounded_samples":[%s],"spawn":{"pos":[%d,%d,%d],"feet":"%s","head":"%s","support":"%s","local_height_delta":%s,"liquid_count_5x5":%d,"safe":%s}}',
    cx, cz, r, cols, grounded, samples, gsamples, cx, sy, cz, feet, head, support, if(height_delta == null, 'null', str('%d', height_delta)), liq, if(spawn_safe, 'true', 'false'))
);
