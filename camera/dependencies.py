from __future__ import annotations

import importlib
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class DependencyReport:
    numpy_version: str
    moderngl_version: str
    pillow_version: str
    ffmpeg_path: str
    ffmpeg_version: str
    gl_backend: str
    gl_renderer: str
    gl_vendor: str
    asset_client_jar: str | None = None
    asset_atlas_path: str | None = None


class DependencyError(RuntimeError):
    pass


def check_dependencies() -> DependencyReport:
    numpy = _import_required("numpy")
    moderngl = _import_required("moderngl")
    pillow = _import_required("PIL")
    imageio_ffmpeg = _import_required("imageio_ffmpeg")

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_version = _ffmpeg_version(ffmpeg_path)
    try:
        ctx = moderngl.create_standalone_context(backend="egl")
    except Exception as exc:  # pragma: no cover - environment-dependent diagnostic path.
        raise DependencyError(
            "failed to create ModernGL EGL context; install Mesa EGL/llvmpipe or provide a working EGL device"
        ) from exc
    try:
        info = ctx.info
        renderer = str(info.get("GL_RENDERER", "unknown"))
        vendor = str(info.get("GL_VENDOR", "unknown"))
    finally:
        ctx.release()

    return DependencyReport(
        numpy_version=str(getattr(numpy, "__version__", "unknown")),
        moderngl_version=str(getattr(moderngl, "__version__", "unknown")),
        pillow_version=str(getattr(pillow, "__version__", "unknown")),
        ffmpeg_path=ffmpeg_path,
        ffmpeg_version=ffmpeg_version,
        gl_backend="egl",
        gl_renderer=renderer,
        gl_vendor=vendor,
    )


def _import_required(name: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:  # pragma: no cover - environment-dependent diagnostic path.
        raise DependencyError(f"missing Python dependency: {name}") from exc


def _ffmpeg_version(ffmpeg_path: str) -> str:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover.
        raise DependencyError(f"ffmpeg is not runnable: {ffmpeg_path}") from exc
    return result.stdout.splitlines()[0] if result.stdout else "unknown"
