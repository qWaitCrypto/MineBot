#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_DIR="${MINEBOT_CAMERA_PROFILE_DIR:?MINEBOT_CAMERA_PROFILE_DIR is required}"
RUNTIME_DIR="${MINEBOT_CAMERA_RUNTIME_DIR:?MINEBOT_CAMERA_RUNTIME_DIR is required}"
DISPLAY_NAME="${MINEBOT_CAMERA_DISPLAY:-:91}"
SERVER_ADDRESS="${MINEBOT_CAMERA_SERVER_ADDRESS:-127.0.0.1:25566}"
OBSERVER_NAME="${MINEBOT_CAMERA_OBSERVER_NAME:-MineBotCamera}"
CAMERA_PYTHON="${MINEBOT_CAMERA_PYTHON:-python3}"
WIDTH="${MINEBOT_CAMERA_WIDTH:-1280}"
HEIGHT="${MINEBOT_CAMERA_HEIGHT:-720}"
CLIENT_CONFIG="$ROOT/minecraft/.gradle/loom-cache/projects/camera/client/launch.cfg"
CLIENT_ARGS="$ROOT/minecraft/camera/client/build/loom-cache/argFiles/runClient"
XVFB_PID=""
CLIENT_PID=""

[[ "$DISPLAY_NAME" =~ ^:[0-9]+$ ]] || {
  echo "Camera display must look like :91" >&2
  exit 2
}
[[ "$SERVER_ADDRESS" =~ ^[^[:space:]]+:[0-9]+$ ]] || {
  echo "Camera server address must be host:port" >&2
  exit 2
}
[[ "$OBSERVER_NAME" =~ ^[A-Za-z0-9_]{1,16}$ ]] || {
  echo "Camera observer name is invalid" >&2
  exit 2
}

terminate_child() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -TERM "$pid" 2>/dev/null || true
}

kill_child() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -KILL "$pid" 2>/dev/null || true
}

cleanup() {
  trap - EXIT INT TERM
  terminate_child "$CLIENT_PID"
  terminate_child "$XVFB_PID"
  for _ in {1..50}; do
    if { [[ -z "$CLIENT_PID" ]] || ! kill -0 "$CLIENT_PID" 2>/dev/null; } &&
       { [[ -z "$XVFB_PID" ]] || ! kill -0 "$XVFB_PID" 2>/dev/null; }; then
      return 0
    fi
    sleep 0.1
  done
  kill_child "$CLIENT_PID"
  kill_child "$XVFB_PID"
}

trap 'exit 143' INT TERM
trap cleanup EXIT

mkdir -p "$PROFILE_DIR" "$RUNTIME_DIR"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$CAMERA_PYTHON" -m minebot.camera.profile "$PROFILE_DIR"
readarray -t JAVA_PROXY_ARGS < <(
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$CAMERA_PYTHON" -m minebot.camera.launcher
)
: >"$RUNTIME_DIR/client.log"
: >"$RUNTIME_DIR/xvfb.log"
if [[ -e "/tmp/.X11-unix/X${DISPLAY_NAME#:}" || -e "/tmp/.X${DISPLAY_NAME#:}-lock" ]]; then
  echo "Camera display $DISPLAY_NAME is already in use" >&2
  exit 2
fi

Xvfb "$DISPLAY_NAME" \
  -screen 0 "${WIDTH}x${HEIGHT}x24" \
  -nolisten tcp \
  -noreset \
  >"$RUNTIME_DIR/xvfb.log" 2>&1 &
XVFB_PID=$!

for _ in {1..100}; do
  if timeout 1s env DISPLAY="$DISPLAY_NAME" xdpyinfo >/dev/null 2>&1; then
    break
  fi
  kill -0 "$XVFB_PID" 2>/dev/null || {
    echo "Xvfb exited before display startup" >&2
    exit 3
  }
  sleep 0.1
done
timeout 1s env DISPLAY="$DISPLAY_NAME" xdpyinfo >/dev/null 2>&1 || {
  echo "Xvfb did not create $DISPLAY_NAME" >&2
  exit 3
}

export DISPLAY="$DISPLAY_NAME"
export LIBGL_ALWAYS_SOFTWARE=1
export vblank_mode=0

if [[ -r "$CLIENT_CONFIG" && -r "$CLIENT_ARGS" ]]; then
  java \
    "-Dfabric.dli.config=$CLIENT_CONFIG" \
    -Dfabric.dli.env=client \
    -Dfabric.dli.main=net.fabricmc.loader.impl.launch.knot.KnotClient \
    "${JAVA_PROXY_ARGS[@]}" \
    '-Dhttp.nonProxyHosts=localhost|127.*|[::1]' \
    @"$CLIENT_ARGS" \
    --sun-misc-unsafe-memory-access=allow \
    --enable-native-access=ALL-UNNAMED \
    -Dfile.encoding=UTF-8 \
    -Duser.country \
    -Duser.language=en \
    -Duser.variant \
    net.fabricmc.devlaunchinjector.Main \
    --quickPlayMultiplayer "$SERVER_ADDRESS" \
    --username "$OBSERVER_NAME" \
    --gameDir "$PROFILE_DIR" \
    --width "$WIDTH" \
    --height "$HEIGHT" \
    >"$RUNTIME_DIR/client.log" 2>&1 &
else
  "$ROOT/minecraft/gradlew" \
    -p "$ROOT/minecraft" \
    --offline \
    --no-daemon \
    --console=plain \
    :camera:client:runClient \
    --args="--quickPlayMultiplayer $SERVER_ADDRESS --username $OBSERVER_NAME --gameDir $PROFILE_DIR --width $WIDTH --height $HEIGHT" \
    >"$RUNTIME_DIR/client.log" 2>&1 &
fi
CLIENT_PID=$!
wait "$CLIENT_PID"
