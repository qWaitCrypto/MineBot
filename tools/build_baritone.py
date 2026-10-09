#!/usr/bin/env python3
"""Build the pinned upstream Fabric executor with MineBot's narrow execution fix."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
BODY = ROOT / "minecraft/client/body"
SOURCE = BODY / "build/upstream-baritone"
REVISION = "016cc210434894f371baf0c327ac94907d12dcd3"
VERSION = "1.18.0+minebot.1"
PATCH = BODY / "baritone-patches/0001-preserve-descend-preparation.patch"
OUTPUT = BODY / f"build/baritone/baritone-fabric-{VERSION}.jar"


def run(*args, cwd=SOURCE, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def main():
    if not SOURCE.exists():
        SOURCE.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "--depth", "1", "--branch", "v1.18.0",
            "https://github.com/cabaletta/baritone.git", str(SOURCE), cwd=ROOT)
    if run("git", "rev-parse", "HEAD", capture=True).strip() != REVISION:
        raise SystemExit("Unexpected Baritone revision; refusing to alter this checkout")
    diff = run("git", "diff", "--unified=0", "--", capture=True)
    if not diff:
        run("git", "apply", "--unidiff-zero", "--check", str(PATCH))
        run("git", "apply", "--unidiff-zero", str(PATCH))
    elif diff != PATCH.read_text():
        raise SystemExit("Unexpected upstream changes; refusing to overwrite them")

    # Use upstream's supported, unoptimized Fabric jar, without ProGuard names.
    init = SOURCE.parent / "baritone-version.gradle"
    init.write_text("""gradle.allprojects { p ->
    p.afterEvaluate {
        p.version = '1.18.0+minebot.1'
        p.tasks.withType(Jar).configureEach { archiveVersion.set('1.18.0+minebot.1') }
        p.tasks.matching { it.name == 'processResources' }.configureEach {
            inputs.property('minebotPatchVersion', '1.18.0+minebot.1')
        }
    }
}
""")
    env = dict(os.environ)
    # JVM downloads do not automatically honor HTTP_PROXY, unlike Python/git.
    proxy = urlsplit(env.get("HTTPS_PROXY") or env.get("https_proxy") or "")
    if proxy.hostname:
        if proxy.username or proxy.password:
            raise SystemExit("Authenticated JVM proxy needs private configuration")
        flags = (f"-Dhttps.proxyHost={proxy.hostname} -Dhttps.proxyPort={proxy.port or 80} "
                 f"-Dhttp.proxyHost={proxy.hostname} -Dhttp.proxyPort={proxy.port or 80} "
                 "-Dhttp.nonProxyHosts=localhost|127.*")
        env["JAVA_TOOL_OPTIONS"] = (env.get("JAVA_TOOL_OPTIONS", "") + " " + flags).strip()
    subprocess.run([str(SOURCE / "gradlew"), ":fabric:remapJar", "-Pavailable_loaders=fabric",
                    "--no-daemon", "--console=plain", "--init-script", str(init)],
                   cwd=SOURCE, env=env, check=True)
    artifact = SOURCE / f"fabric/build/libs/baritone-fabric-{VERSION}.jar"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(artifact, OUTPUT)
    OUTPUT.with_suffix(".json").write_text(json.dumps({
        "upstream_revision": REVISION, "version": VERSION,
        "patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest(),
        "jar_sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
    }, indent=2) + "\n")
    print(f"Built {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
