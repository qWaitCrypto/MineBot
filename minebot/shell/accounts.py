"""The bot's Minecraft accounts: offline names, or Microsoft (official) accounts.

Only the bot's own accounts live here; MineBot never stores the operator's account.
``minebot login`` runs the Microsoft device-code flow and the Xbox Live → XSTS →
Minecraft services chain, then keeps the refresh token in
``~/.config/minebot/accounts/<name>.json`` (directory 0700, file 0600), the same
convention as ``chatgpt.json``. Tokens never enter the trace, logs, ``launch.json`` or the
Agent session's environment; only the game client receives the Minecraft access token.

Mojang only accepts Azure app registrations it has approved for the Minecraft API, so
the client id is configuration: ``MINEBOT_MSA_CLIENT_ID`` or ``--client-id``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock

DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
SCOPE = "XboxLive.signin offline_access"
CLIENT_ID_VAR = "MINEBOT_MSA_CLIENT_ID"
REFRESH_MARGIN_S = 600.0
_NAME = re.compile(r"[A-Za-z0-9_]{1,16}")
_XSTS_ERRORS = {
    2148916233: "这个微软账号还没有 Xbox 档案；先在 minecraft.net 登录一次创建",
    2148916235: "Xbox Live 在这个国家或地区不可用",
    2148916236: "这个账号需要成人验证（韩国）",
    2148916237: "这个账号需要成人验证（韩国）",
    2148916238: "这是儿童账号，需要先加入家庭组",
}


class AccountError(RuntimeError):
    """Safe, actionable message; never contains a token."""


@dataclass
class Account:
    name: str
    uuid: str
    kind: str = "msa"  # msa | offline
    client_id: str = ""
    refresh_token: str = ""
    access_token: str = ""
    expires_at: float = 0.0

    def fresh(self, now: float | None = None) -> bool:
        return bool(self.access_token) and self.expires_at - REFRESH_MARGIN_S > (now or time.time())

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "uuid": self.uuid, "kind": self.kind, "expires_at": self.expires_at}


def default_dir(environ: dict[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    root = Path(environment.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "minebot" / "accounts"


class AccountStore:
    def __init__(self, directory: Path | None = None, *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.directory = (directory or default_dir()).expanduser().absolute()
        if Path(__file__).resolve().parents[2] in self.directory.resolve().parents:
            raise AccountError("账号令牌必须放在仓库外面")
        self.transport = transport
        self.clock = clock
        self.sleep = sleep

    # ------------------------------------------------------------ storage
    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise AccountError(f"账号名 {name!r} 不合法")
        return self.directory / f"{name}.json"

    def _lock(self) -> FileLock:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return FileLock(str(self.directory / ".lock"), timeout=40, mode=0o600)

    def names(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(path.stem for path in self.directory.glob("*.json"))

    def load(self, name: str) -> Account | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Account(**{key: data[key] for key in Account.__dataclass_fields__ if key in data})
        except (OSError, ValueError, TypeError):
            raise AccountError(f"账号文件 {path} 读不了；用 minebot logout {name} 删掉后重新登录") from None

    def save(self, account: Account) -> None:
        path = self._path(account.name)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".account-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(account), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove(self, name: str) -> bool:
        with self._lock():
            path = self._path(name)
            if not path.exists():
                return False
            path.unlink()
            return True

    # ------------------------------------------------------------ flows
    def _http(self) -> httpx.Client:
        return httpx.Client(timeout=30, transport=self.transport)

    def login(self, client_id: str, *, announce: Callable[[str, str, int], None]) -> Account:
        """Device-code sign-in. ``announce(url, code, expires_in_s)`` shows the code to the person."""
        if not client_id:
            raise AccountError(f"没有配置微软应用 client id：设置 {CLIENT_ID_VAR} 或用 --client-id")
        with self._http() as http:
            response = http.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": SCOPE})
            _check(response, "申请设备码")
            device = response.json()
            announce(device.get("verification_uri", "https://microsoft.com/link"), device["user_code"],
                     int(device.get("expires_in", 900)))
            interval = float(device.get("interval", 5))
            deadline = self.clock() + float(device.get("expires_in", 900))
            while True:
                if self.clock() > deadline:
                    raise AccountError("设备码已过期，重新运行 minebot login")
                self.sleep(interval)
                response = http.post(TOKEN_URL, data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": client_id, "device_code": device["device_code"]})
                if response.status_code == 200:
                    msa = response.json()
                    break
                error = _oauth_error(response)
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                if error == "authorization_declined":
                    raise AccountError("登录被拒绝了")
                if error == "expired_token":
                    raise AccountError("设备码已过期，重新运行 minebot login")
                raise AccountError(f"微软登录失败（{error or response.status_code}）")
            account = self._minecraft(http, client_id, msa)
        with self._lock():
            self.save(account)
        return account

    def ensure_fresh(self, name: str) -> Account | None:
        """The stored account with a usable Minecraft token, refreshing it when needed."""
        if not self._path(name).exists():
            return None  # offline bot: touch nothing on disk
        with self._lock():
            account = self.load(name)
            if account is None or account.kind != "msa" or account.fresh(self.clock()):
                return account
            if not account.refresh_token:
                raise AccountError(f"{name} 没有刷新令牌；运行 minebot login 重新登录")
            with self._http() as http:
                response = http.post(TOKEN_URL, data={
                    "grant_type": "refresh_token", "client_id": account.client_id,
                    "refresh_token": account.refresh_token, "scope": SCOPE})
                if response.status_code != 200:
                    raise AccountError(f"{name} 的登录已失效（{_oauth_error(response) or response.status_code}）；"
                                       "运行 minebot login 重新登录")
                refreshed = self._minecraft(http, account.client_id, response.json(), previous=account)
            if refreshed.name != name:
                raise AccountError(f"这个微软账号现在的游戏名是 {refreshed.name}，不是 {name}；重新 minebot login")
            self.save(refreshed)
            return refreshed

    def _minecraft(self, http: httpx.Client, client_id: str, msa: dict[str, Any], *,
                   previous: Account | None = None) -> Account:
        refresh = msa.get("refresh_token") or (previous.refresh_token if previous else "")
        response = http.post(XBL_URL, json={
            "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                           "RpsTicket": f"d={msa['access_token']}"},
            "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
        _check(response, "Xbox Live 登录")
        xbl = response.json()
        user_hash = xbl["DisplayClaims"]["xui"][0]["uhs"]
        response = http.post(XSTS_URL, json={
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
            "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
        if response.status_code == 401:
            code = _json(response).get("XErr")
            raise AccountError(_XSTS_ERRORS.get(code, f"Xbox 授权被拒绝（XErr {code}）"))
        _check(response, "Xbox 授权")
        xsts = response.json()["Token"]
        response = http.post(MC_LOGIN_URL, json={"identityToken": f"XBL3.0 x={user_hash};{xsts}"})
        if response.status_code == 403:
            raise AccountError("Minecraft 服务拒绝了这个应用：client id 需要先经 Mojang 批准才能访问 Minecraft API")
        _check(response, "Minecraft 登录")
        minecraft = response.json()
        response = http.get(MC_PROFILE_URL, headers={"Authorization": f"Bearer {minecraft['access_token']}"})
        if response.status_code == 404:
            raise AccountError("这个微软账号没有购买 Minecraft Java 版")
        _check(response, "读取游戏档案")
        profile = response.json()
        return Account(name=profile["name"], uuid=profile["id"], kind="msa", client_id=client_id,
                       refresh_token=refresh, access_token=minecraft["access_token"],
                       expires_at=self.clock() + float(minecraft.get("expires_in", 86400)))


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _oauth_error(response: httpx.Response) -> str:
    error = _json(response).get("error")
    return error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,64}", error) else ""


def _check(response: httpx.Response, what: str) -> None:
    if response.status_code >= 400:
        raise AccountError(f"{what}失败（HTTP {response.status_code}）")


__all__ = ["CLIENT_ID_VAR", "Account", "AccountError", "AccountStore", "default_dir"]
