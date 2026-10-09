"""Exercise the client's failure counter without launching Minecraft."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_navigation_counts_failed_execution_without_confusing_waiting_or_progress(tmp_path):
    javac = shutil.which("javac")
    java = shutil.which("java")
    if not javac or not java:
        pytest.skip("Java toolchain is needed for the client progress counter")
    source = Path(__file__).resolve().parents[2] / (
        "minecraft/client/body/src/main/java/dev/minebot/client/body/NavigationProgress.java")
    harness = tmp_path / "ProgressCheck.java"
    harness.write_text("""package dev.minebot.client.body;
public final class ProgressCheck {
    static void check(boolean value) { if (!value) throw new AssertionError(); }
    public static void main(String[] args) {
        NavigationProgress p = new NavigationProgress();
        p.observePosition(6.137, 125, -38.496, 0);
        check(!p.executionFailed("ascend:6,124,-39:7,125,-39:pane"));
        // Collision jitter and a new path executor/index are not displacement.
        p.observePosition(6.137, 125, -38.498, 5300);
        check(p.executionFailed("ascend:6,124,-39:7,125,-39:pane"));
        check(p.repeatedFailures() == 2 && p.idleMs(10600) == 10600);

        NavigationProgress waiting = new NavigationProgress();
        waiting.observePosition(0, 100, 0, 0);
        waiting.observePosition(0, 100, 0, 120000);
        check(waiting.repeatedFailures() == 0); // Waiting alone never fails.
        check(!waiting.executionFailed("traverse:a:b"));
        waiting.progressed(120001); // Stationary mining/world progress.
        check(!waiting.executionFailed("traverse:a:b"));
        waiting.observePosition(1, 100, 0, 120002);
        check(!waiting.executionFailed("traverse:a:b"));
        check(!waiting.executionFailed("ascend:b:c")); // Different failed step.
        check(waiting.executionFailed("ascend:b:c"));
    }
}
""")
    subprocess.run([javac, "-d", str(tmp_path), str(source), str(harness)], check=True)
    subprocess.run([java, "-cp", str(tmp_path), "dev.minebot.client.body.ProgressCheck"], check=True)
