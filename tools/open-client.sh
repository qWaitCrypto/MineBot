#!/usr/bin/env bash
set -eu
if ss -ltnH "sport = :$MINEBOT_CLIENT_IPC_PORT" | read -r line; then
    echo 'Minecraft client is already running; use its Windows taskbar window.'
    exit 0
fi
export DISPLAY=:0
export WAYLAND_DISPLAY=wayland-0
export XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir
export PULSE_SERVER=unix:/mnt/wslg/PulseServer
export JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"
cd minecraft
exec ./gradlew :client:body:runClient --offline --console=plain
