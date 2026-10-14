#!/usr/bin/env bash
set -euo pipefail
export JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"
cd /home/qwait/MineBot-always-on/minecraft
./gradlew :client:body:test --console=plain > ../logs/connection-java.log 2>&1
tail -25 ../logs/connection-java.log
