#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT/test-server"
WORLD="$SERVER_DIR/world"
GOLDEN="$SERVER_DIR/world-golden"
LOG="$SERVER_DIR/logs/minebot-reset-world.log"
RCON_HOST="${MINEBOT_REAL_RCON_HOST:-127.0.0.1}"
RCON_PORT="${MINEBOT_REAL_RCON_PORT:-25576}"
RCON_PASSWORD="${MINEBOT_REAL_RCON_PASSWORD:-test}"
STOP_TIMEOUT_S="${MINEBOT_RESET_STOP_TIMEOUT_S:-180}"

if [[ ! -d "$GOLDEN" ]]; then
  echo "missing golden world: $GOLDEN" >&2
  exit 2
fi

python3 - "$RCON_HOST" "$RCON_PORT" "$RCON_PASSWORD" <<'PY' || true
import sys, time
from minebot.game.rcon import RconClient, RconConfig
host, port, password = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    with RconClient(RconConfig(host=host, port=port, password=password, timeout_s=5, reconnect_attempts=0)) as r:
        r.command("stop")
except Exception:
    pass
PY

for ((i = 0; i < STOP_TIMEOUT_S; i++)); do
  if ! pgrep -f "fabric-server-launch.jar nogui" >/dev/null; then
    break
  fi
  sleep 1
done
if pgrep -f "fabric-server-launch.jar nogui" >/dev/null; then
  python3 - "$SERVER_DIR" <<'PY'
import os
import signal
import sys
from pathlib import Path

server_dir = Path(sys.argv[1]).resolve()
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    try:
        cwd = proc.joinpath("cwd").resolve()
        cmdline = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
    if cwd == server_dir and "fabric-server-launch.jar" in cmdline and "nogui" in cmdline:
        os.kill(int(proc.name), signal.SIGTERM)
PY
  for _ in {1..30}; do
    if ! pgrep -f "fabric-server-launch.jar nogui" >/dev/null; then
      break
    fi
    sleep 1
  done
fi
if pgrep -f "fabric-server-launch.jar nogui" >/dev/null; then
  echo "server did not stop within ${STOP_TIMEOUT_S}s" >&2
  exit 3
fi

rm -rf "$WORLD"
cp -a "$GOLDEN" "$WORLD"

mkdir -p "$(dirname "$LOG")"
(
  cd "$SERVER_DIR"
  setsid -f bash -c 'tail -f /dev/null | exec java -Xmx2G -jar fabric-server-launch.jar nogui' >> "$LOG" 2>&1 < /dev/null
  : > "$SERVER_DIR/minebot-server.pid"
)

ready_count=0
for _ in {1..10}; do
  if pgrep -f "fabric-server-launch.jar nogui" >/dev/null; then
    break
  fi
  sleep 1
done
for _ in {1..120}; do
  if ! pgrep -f "fabric-server-launch.jar nogui" >/dev/null; then
    echo "server process exited during startup" >&2
    exit 4
  fi
  if python3 - "$RCON_HOST" "$RCON_PORT" "$RCON_PASSWORD" <<'PY' >/dev/null 2>&1
import sys
from minebot.game.rcon import RconClient, RconConfig
host, port, password = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with RconClient(RconConfig(host=host, port=port, password=password, timeout_s=3, reconnect_attempts=0)) as r:
    r.command("difficulty peaceful")
    r.command("gamerule spawn_mobs false")
    r.command("gamerule advance_time false")
    r.command("gamerule advance_weather false")
    r.command("gamerule random_tick_speed 0")
    r.command("gamerule fire_spread_radius_around_player 0")
    r.command("weather clear")
    r.command("time set day")
    r.command("kill @e[type=!minecraft:player,type=!minecraft:item]")
    r.command("script load minebot global")
    r.command("script load w1_probe global")
    reset = r.command("script in minebot run minebot_reset()")
    if "true" not in reset.lower():
        raise RuntimeError(reset)
    difficulty = r.command("difficulty").lower()
    spawn_mobs = r.command("gamerule spawn_mobs").lower()
    advance_time = r.command("gamerule advance_time").lower()
    random_tick_speed = r.command("gamerule random_tick_speed").lower()
    hostile_types = (
        "blaze",
        "cave_spider",
        "creeper",
        "drowned",
        "elder_guardian",
        "enderman",
        "endermite",
        "evoker",
        "ghast",
        "guardian",
        "hoglin",
        "husk",
        "magma_cube",
        "phantom",
        "piglin_brute",
        "pillager",
        "ravager",
        "shulker",
        "silverfish",
        "skeleton",
        "slime",
        "spider",
        "stray",
        "vex",
        "vindicator",
        "warden",
        "witch",
        "wither_skeleton",
        "zoglin",
        "zombie",
        "zombie_villager",
    )
    if "peaceful" not in difficulty:
        raise RuntimeError(difficulty)
    if "false" not in spawn_mobs:
        raise RuntimeError(spawn_mobs)
    if "false" not in advance_time:
        raise RuntimeError(advance_time)
    if "0" not in random_tick_speed:
        raise RuntimeError(random_tick_speed)
    for entity_type in hostile_types:
        found = r.command(f"execute as @e[type=minecraft:{entity_type}] run data get entity @s Pos")
        if found.strip():
            raise RuntimeError(f"{entity_type}: {found[:500]}")
PY
  then
    ready_count=$((ready_count + 1))
    if [[ "$ready_count" -ge 3 ]]; then
      sleep 5
      if pgrep -f "fabric-server-launch.jar nogui" >/dev/null; then
        echo "world reset complete"
        exit 0
      fi
      echo "server exited after initial RCON readiness" >&2
      exit 4
    fi
  else
    ready_count=0
  fi
  sleep 2
done

echo "server did not become RCON-ready" >&2
exit 4
