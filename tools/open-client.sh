#!/usr/bin/env bash
# Start MineBot's client Body (Fabric client + patched Baritone) from this checkout.
# `minebot` runs this unless MINEBOT_CLIENT_LAUNCH names another command.
set -eu
if ss -ltnH "sport = :${MINEBOT_CLIENT_IPC_PORT:-8768}" | read -r line; then
    echo 'Minecraft client is already running.'
    exit 0
fi
# WSL2 with WSLg: show the game window on the Windows desktop.
if [ -z "${DISPLAY:-}" ] && [ -d /mnt/wslg ]; then
    export DISPLAY=:0 WAYLAND_DISPLAY=wayland-0
    export XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir PULSE_SERVER=unix:/mnt/wslg/PulseServer
fi
if [ -z "${JAVA_HOME:-}" ] && [ -d /usr/lib/jvm/java-25-openjdk-amd64 ]; then
    export JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64
fi
if [ -n "${JAVA_HOME:-}" ]; then
    export PATH="$JAVA_HOME/bin:$PATH"
fi
cd "$(dirname "${BASH_SOURCE[0]}")/../minecraft"
# MINEBOT_GRADLE_OFFLINE=1 skips dependency checks once everything is cached.
exec ./gradlew :client:body:runClient --console=plain ${MINEBOT_GRADLE_OFFLINE:+--offline}
