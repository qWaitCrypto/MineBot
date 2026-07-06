from __future__ import annotations

import math
from dataclasses import dataclass

import moderngl
import numpy as np

from camera.control.follow import CameraPose
from camera.render.mesher import Mesh


VERTEX_SHADER = """
#version 330
in vec3 in_pos;
in vec3 in_color;
uniform mat4 mvp;
out vec3 v_color;
void main() {
    gl_Position = mvp * vec4(in_pos, 1.0);
    v_color = in_color;
}
"""

FRAGMENT_SHADER = """
#version 330
in vec3 v_color;
out vec4 fragColor;
void main() {
    fragColor = vec4(v_color, 1.0);
}
"""


@dataclass(frozen=True)
class RenderStats:
    draw_ms: float
    readback_ms: float


class GLRenderer:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.ctx = moderngl.create_standalone_context(backend="egl")
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.program = self.ctx.program(vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER)
        self.fbo = self.ctx.simple_framebuffer((width, height), components=3)
        self._buffer: moderngl.Buffer | None = None
        self._vao: moderngl.VertexArray | None = None
        self._vertex_count = 0
        self._mesh_signature: tuple[int, int] | None = None

    def render(self, mesh: Mesh, pose: CameraPose) -> tuple[np.ndarray, RenderStats]:
        import time

        draw_start = time.perf_counter()
        self.fbo.use()
        self.ctx.clear(0.52, 0.72, 0.92, 1.0)
        if mesh.vertices.size:
            self._ensure_geometry(mesh)
            self.program["mvp"].write(_mvp_matrix(pose, self.width / self.height).T.astype("f4").tobytes())
            if self._vao is not None:
                self._vao.render(moderngl.TRIANGLES, vertices=self._vertex_count)
        draw_ms = (time.perf_counter() - draw_start) * 1000.0

        read_start = time.perf_counter()
        raw = self.fbo.read(components=3, alignment=1)
        frame = np.frombuffer(raw, dtype=np.uint8).reshape((self.height, self.width, 3))
        frame = np.flipud(frame).copy()
        read_ms = (time.perf_counter() - read_start) * 1000.0
        return frame, RenderStats(draw_ms=draw_ms, readback_ms=read_ms)

    def close(self) -> None:
        if self._vao is not None:
            self._vao.release()
            self._vao = None
        if self._buffer is not None:
            self._buffer.release()
            self._buffer = None
        self.fbo.release()
        self.program.release()
        self.ctx.release()

    def _ensure_geometry(self, mesh: Mesh) -> None:
        signature = (id(mesh.vertices), int(mesh.vertices.shape[0]))
        if signature == self._mesh_signature:
            return
        if self._vao is not None:
            self._vao.release()
            self._vao = None
        if self._buffer is not None:
            self._buffer.release()
            self._buffer = None
        self._buffer = self.ctx.buffer(mesh.vertices.astype("f4", copy=False).tobytes())
        self._vao = self.ctx.vertex_array(self.program, [(self._buffer, "3f 3f", "in_pos", "in_color")])
        self._vertex_count = int(mesh.vertices.shape[0])
        self._mesh_signature = signature


def _mvp_matrix(pose: CameraPose, aspect: float) -> np.ndarray:
    projection = _perspective(math.radians(pose.fov_deg), aspect, 0.05, 512.0)
    view = _look_at(np.asarray(pose.eye, dtype=np.float64), np.asarray(pose.target, dtype=np.float64), np.asarray([0.0, 1.0, 0.0]))
    return projection @ view


def _perspective(fovy: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / math.tan(fovy / 2.0)
    matrix = np.zeros((4, 4), dtype=np.float32)
    matrix[0, 0] = f / aspect
    matrix[1, 1] = f
    matrix[2, 2] = (far + near) / (near - far)
    matrix[2, 3] = (2.0 * far * near) / (near - far)
    matrix[3, 2] = -1.0
    return matrix


def _look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    forward = target - eye
    forward = forward / max(1e-9, np.linalg.norm(forward))
    side = np.cross(forward, up)
    side = side / max(1e-9, np.linalg.norm(side))
    true_up = np.cross(side, forward)
    matrix = np.identity(4, dtype=np.float32)
    matrix[0, :3] = side
    matrix[1, :3] = true_up
    matrix[2, :3] = -forward
    matrix[0, 3] = -float(np.dot(side, eye))
    matrix[1, 3] = -float(np.dot(true_up, eye))
    matrix[2, 3] = float(np.dot(forward, eye))
    return matrix
