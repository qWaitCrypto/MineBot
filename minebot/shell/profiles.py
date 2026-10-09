"""Server profiles: ``~/.config/minebot/servers.toml``.

    default = "home"

    [home]
    address = "127.0.0.1:25566"
    account = "MineBotCombat"     # bot account: an offline name, or a name from `minebot login`
    world = "disposable"          # disposable | protected
    goal = ""                     # optional first goal

``world`` is the explicit form of ``MINEBOT_CLIENT_DISPOSABLE_WORLD``: ``disposable`` lets the
bot change the world (local test servers only), ``protected`` refuses world-changing tools.
A third ``shared`` level waits for the world-ownership design and is rejected for now.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

WORLD_MODES = ("disposable", "protected")
_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
_FIELDS = ("address", "account", "world", "goal", "port")


class ProfileError(ValueError):
    pass


@dataclass
class ServerProfile:
    name: str
    address: str
    account: str = ""
    world: str = "protected"
    goal: str = ""
    port: int | None = None  # client IPC port, when several bots share a machine

    @property
    def disposable(self) -> bool:
        return self.world == "disposable"


def default_path(environ: dict[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    root = Path(environment.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "minebot" / "servers.toml"


class ServerBook:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_path()).expanduser()
        self.default = ""
        self.profiles: dict[str, ServerProfile] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ProfileError(f"{self.path} 读不了：{exc}") from exc
        self.default = str(data.get("default") or "")
        for name, table in data.items():
            if name == "default":
                continue
            if not isinstance(table, dict):
                raise ProfileError(f"{self.path}: {name} 应该是一个 [表]")
            unknown = set(table) - set(_FIELDS)
            if unknown:
                raise ProfileError(f"{self.path}: [{name}] 有不认识的字段 {', '.join(sorted(unknown))}")
            self.profiles[name] = _validated(ServerProfile(
                name=name, address=str(table.get("address") or ""), account=str(table.get("account") or ""),
                world=str(table.get("world") or "protected"), goal=str(table.get("goal") or ""),
                port=int(table["port"]) if table.get("port") else None))
        if self.default and self.default not in self.profiles:
            raise ProfileError(f"{self.path}: default = {self.default!r} 没有对应的档案")

    def get(self, name: str | None) -> ServerProfile | None:
        """The named profile, the default one when ``name`` is None, or None."""
        if name is None:
            return self.profiles.get(self.default) if self.default else None
        return self.profiles.get(name)

    def put(self, profile: ServerProfile, *, make_default: bool = False) -> None:
        self.profiles[profile.name] = _validated(profile)
        if make_default or not self.default:
            self.default = profile.name

    def remove(self, name: str) -> None:
        if name not in self.profiles:
            raise ProfileError(f"没有档案 {name!r}")
        del self.profiles[name]
        if self.default == name:
            self.default = next(iter(self.profiles), "")

    def save(self) -> None:
        lines = ["# MineBot server profiles; edit freely or use `minebot servers`.", ""]
        if self.default:
            lines += [f"default = {_quote(self.default)}", ""]
        for profile in self.profiles.values():
            lines.append(f"[{profile.name}]")
            lines.append(f"address = {_quote(profile.address)}")
            if profile.account:
                lines.append(f"account = {_quote(profile.account)}")
            lines.append(f"world = {_quote(profile.world)}")
            if profile.goal:
                lines.append(f"goal = {_quote(profile.goal)}")
            if profile.port:
                lines.append(f"port = {profile.port}")
            lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".servers-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("\n".join(lines))
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _validated(profile: ServerProfile) -> ServerProfile:
    from minebot.shell.slp import PingError, split_address

    if not _NAME.fullmatch(profile.name):
        raise ProfileError(f"档案名 {profile.name!r} 只能用字母、数字、- 和 _")
    try:
        host, _ = split_address(profile.address)
    except PingError as exc:
        raise ProfileError(f"[{profile.name}] address：{exc}") from exc
    if profile.world not in WORLD_MODES:
        hint = "（shared 要等世界所有权设计落地）" if profile.world == "shared" else ""
        raise ProfileError(f"[{profile.name}] world 只能是 {' | '.join(WORLD_MODES)}{hint}")
    if profile.disposable and host.lower() not in ("127.0.0.1", "localhost", "::1"):
        raise ProfileError(f"[{profile.name}] world = \"disposable\" 只允许本地测试服")
    return profile


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = ["WORLD_MODES", "ProfileError", "ServerBook", "ServerProfile", "default_path"]
