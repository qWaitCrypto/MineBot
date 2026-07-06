from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from camera.assets.vanilla import build_atlas, resolve_client_jar
from camera.control.follow import FollowConfig, FollowController
from camera.dependencies import DependencyError, check_dependencies
from camera.model.world import SectionStore, TransformBuffer
from camera.output.ffmpeg_writer import FfmpegWriter
from camera.render.cache import SectionMeshCache
from camera.render.gl_renderer import GLRenderer
from camera.worldstream.protocol import SectionKey, WorldStreamReconnect, parse_keyframe, parse_transform, read_stream


@dataclass
class RecordSummary:
    output_path: str
    timing_log: str
    frames: int
    duration_s: float
    measured_fps: float
    sections_loaded: int
    face_count_last: int
    dependencies: dict[str, str]
    assets: dict[str, object]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a Stage-1 MineBot follow-camera mp4.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bot", default="Bot1")
    parser.add_argument("--dimension", default="minecraft:overworld")
    parser.add_argument("--output", default="artifacts/camera/stage1-follow.mp4")
    parser.add_argument("--timing-log", default="artifacts/camera/stage1-follow-timing.jsonl")
    parser.add_argument("--width", type=int, default=854)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--radius-chunks", type=int, default=4)
    parser.add_argument("--y-band-below", type=int, default=0)
    parser.add_argument("--y-band-above", type=int, default=0)
    parser.add_argument("--view-radius-chunks", type=int, default=4)
    parser.add_argument("--min-sections", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--client-jar", default=None)
    parser.add_argument("--asset-cache-dir", default=".camera-assets/26.1.2")
    args = parser.parse_args(argv)

    try:
        deps = check_dependencies()
    except DependencyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2

    store = SectionStore()
    transforms = TransformBuffer()
    hello_holder: dict[str, object] = {}
    ack_seen = threading.Event()
    ingest_error: list[BaseException] = []
    lock = threading.Lock()
    stop_ingest = threading.Event()

    def ingest() -> None:
        try:
            stream = read_stream(
                host=args.host,
                port=args.port,
                bot=args.bot,
                dimension=args.dimension,
                radius_chunks=args.radius_chunks,
                y_band_sections=(args.y_band_below, args.y_band_above),
                rate_hz=20,
                timeout_s=2.0,
            )
            for msg in stream:
                if stop_ingest.is_set():
                    return
                if isinstance(msg, WorldStreamReconnect):
                    with lock:
                        store.clear()
                        transforms.clear()
                    ack_seen.clear()
                    continue
                msg_type = msg.get("type")
                if msg_type == "HELLO_ACK":
                    hello_holder.update(msg)
                elif msg_type == "ACK":
                    ack_seen.set()
                elif msg_type == "TRANSFORM":
                    with lock:
                        transforms.add(parse_transform(msg))
                elif msg_type == "SECTION_KEYFRAME":
                    keyframe = parse_keyframe(msg)
                    with lock:
                        store.apply_keyframe(keyframe)
        except BaseException as exc:  # pragma: no cover - live diagnostic path.
            ingest_error.append(exc)

    ingest_thread = threading.Thread(target=ingest, name="camera-worldstream-ingest", daemon=True)
    ingest_thread.start()
    start_wait = time.monotonic()
    while time.monotonic() - start_wait < args.timeout:
        if ingest_error:
            raise RuntimeError(f"world-stream ingest failed: {ingest_error[0]}") from ingest_error[0]
        with lock:
            ready = len(store.sections) >= args.min_sections and transforms.latest() is not None
        if ready:
            break
        time.sleep(0.02)
    if not ack_seen.is_set():
        raise RuntimeError("world-stream subscribe ACK was not received")
    with lock:
        section_count = len(store.sections)
        has_transform = transforms.latest() is not None
    if section_count < args.min_sections:
        raise RuntimeError(f"insufficient sections for recording: got {section_count}, need {args.min_sections}")
    if not has_transform:
        raise RuntimeError("no transform sample received")

    controller = FollowController(FollowConfig(stiffness=0.22))
    client_jar = resolve_client_jar(args.client_jar)
    atlas = build_atlas(client_jar, Path(args.asset_cache_dir))
    timing_path = Path(args.timing_log)
    timing_path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = max(1, int(args.duration * args.fps))
    frame_interval = 1.0 / args.fps
    record_start = 0.0
    face_count_last = 0
    mesh_cache = SectionMeshCache()
    mesh_cache.set_atlas(atlas)
    renderer: GLRenderer | None = None
    writer: FfmpegWriter | None = None
    try:
        renderer = GLRenderer(args.width, args.height, atlas.image_rgba_u8())
        writer = FfmpegWriter(deps.ffmpeg_path, Path(args.output), args.width, args.height, args.fps)
        record_start = time.monotonic()
        with timing_path.open("w", encoding="utf-8") as timing_file:
            for frame_index in range(frame_count):
                frame_due = record_start + frame_index * frame_interval
                sleep_s = frame_due - time.monotonic()
                if sleep_s > 0:
                    time.sleep(sleep_s)
                if ingest_error:
                    raise RuntimeError(f"world-stream ingest failed: {ingest_error[0]}") from ingest_error[0]

                frame_start = time.perf_counter()
                with lock:
                    sample = transforms.interpolated(time.monotonic()) or transforms.latest()
                    sections = dict(store.sections)
                    dirty_sections = store.take_dirty_sections()
                if sample is None:
                    raise RuntimeError("transform buffer became empty")
                pose = controller.pose(sample)
                center = SectionKey(int(sample.pos[0]) >> 4, int(sample.pos[1]) >> 4, int(sample.pos[2]) >> 4)
                mesh_start = time.perf_counter()
                mesh_result = mesh_cache.mesh_visible(sections, dirty_sections, center, args.view_radius_chunks)
                mesh = mesh_result.mesh
                mesh_ms = (time.perf_counter() - mesh_start) * 1000.0
                frame, render_stats = renderer.render(mesh, pose)
                encode_start = time.perf_counter()
                writer.write(frame)
                encode_ms = (time.perf_counter() - encode_start) * 1000.0
                total_ms = (time.perf_counter() - frame_start) * 1000.0
                face_count_last = mesh.face_count
                timing_file.write(
                    json.dumps(
                        {
                            "frame": frame_index,
                            "mesh_ms": mesh_ms,
                            "draw_ms": render_stats.draw_ms,
                            "readback_ms": render_stats.readback_ms,
                            "encode_ms": encode_ms,
                            "total_ms": total_ms,
                            "sections": len(store.sections),
                            "dirty_sections": len(dirty_sections),
                            "rebuilt_sections": mesh_result.rebuilt_sections,
                            "cached_sections": mesh_result.cached_sections,
                            "faces": mesh.face_count,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
    finally:
        stop_ingest.set()
        if writer is not None:
            writer.close()
        if renderer is not None:
            renderer.close()

    elapsed = time.monotonic() - record_start
    summary = RecordSummary(
        output_path=str(Path(args.output).resolve()),
        timing_log=str(timing_path.resolve()),
        frames=frame_count,
        duration_s=elapsed,
        measured_fps=frame_count / elapsed if elapsed > 0 else 0.0,
        sections_loaded=len(store.sections),
        face_count_last=face_count_last,
        dependencies={
            "numpy": deps.numpy_version,
            "moderngl": deps.moderngl_version,
            "pillow": deps.pillow_version,
            "ffmpeg": deps.ffmpeg_version,
            "gl_backend": deps.gl_backend,
            "gl_renderer": deps.gl_renderer,
            "gl_vendor": deps.gl_vendor,
            "hello_mc_version": str(hello_holder.get("mc_version")),
        },
        assets={
            "client_jar": str(client_jar.resolve()),
            "atlas_path": str(atlas.atlas_path.resolve()),
            "texture_count": len(atlas.texture_uvs),
            "covered_blocks": list(atlas.covered_blocks),
            "approximate_blocks": list(atlas.approximate_blocks),
            "missing_blocks": list(atlas.missing_blocks),
        },
    )
    print(json.dumps(asdict(summary), indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
