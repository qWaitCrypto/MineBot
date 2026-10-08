"""Local ChatGPT plan sign-in. Credentials never enter Agent context or traces.

Uses the documented public-client OAuth flow, Authlib for PKCE/token grants and
PyJWT for OIDC verification. One account registration per protected auth file.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import secrets
import tempfile
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from authlib.integrations.httpx_client import OAuth2Client
from filelock import FileLock

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + DIRECT_SCOPE


class ChatGPTAuthError(RuntimeError):
    """Safe, actionable authentication error without provider credential data."""


def _failure_label(exc: Exception) -> str:
    label = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        label += f", HTTP {exc.response.status_code}"
    code = getattr(exc, "error", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code):
        label += ", " + code
    return label


class ChatGPTAuth:
    def __init__(self, path: Path | None = None, *, proxy: str | None = None):
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.path = (path or root / "minebot" / "chatgpt.json").expanduser().absolute()
        if Path(__file__).resolve().parents[2] in self.path.resolve().parents:
            raise ChatGPTAuthError("Store ChatGPT credentials outside the repository")
        self.proxy = proxy

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (ValueError, OSError):
            raise ChatGPTAuthError("Cannot read ChatGPT auth file; run minebot-auth status") from None

    def _save(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".chatgpt-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _lock(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return FileLock(str(self.path) + ".lock", timeout=40, mode=0o600)

    def _discovery(self) -> dict:
        with httpx.Client(proxy=self.proxy, timeout=30) as client:
            response = client.get(ISSUER + "/.well-known/openid-configuration")
            response.raise_for_status()
            metadata = response.json()
        if metadata.get("issuer") != ISSUER:
            raise ChatGPTAuthError("Unexpected ChatGPT OIDC issuer")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint"):
            if metadata.get(name) and (urlsplit(metadata[name]).scheme != "https"
                                      or urlsplit(metadata[name]).netloc != "auth.openai.com"):
                raise ChatGPTAuthError("Unexpected ChatGPT OIDC endpoint")
        return metadata

    def _oauth(self, client_id: str, **kwargs) -> OAuth2Client:
        return OAuth2Client(client_id=client_id, token_endpoint_auth_method="none",
                            proxy=self.proxy, timeout=30, **kwargs)

    def _verify_identity(self, raw: str, client_id: str, metadata: dict, nonce: str | None) -> dict:
        with httpx.Client(proxy=self.proxy, timeout=30) as client:
            response = client.get(metadata["jwks_uri"])
            response.raise_for_status()
            keys = jwt.PyJWKSet.from_dict(response.json())
        header = jwt.get_unverified_header(raw)
        key = next((k for k in keys.keys if k.key_id == header.get("kid")), None)
        if key is None:
            raise ChatGPTAuthError("ChatGPT ID token signing key was not found")
        claims = jwt.decode(raw, key.key, algorithms=["RS256"], issuer=ISSUER,
                            audience=client_id, leeway=5,
                            options={"require": ["sub", "exp", "iat", "iss", "aud"]})
        if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ChatGPTAuthError("ChatGPT ID token nonce mismatch")
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ChatGPTAuthError("ChatGPT ID token has no subject")
        return claims

    def _session(self, token: dict, *, previous: dict | None = None) -> dict:
        # Missing scope on refresh retains the already granted permission;
        # missing scope on initial authorization grants nothing locally.
        scope = token.get("scope", " ".join((previous or {}).get("scopes", [])))
        scopes = scope.split() if isinstance(scope, str) else list(scope)
        if DIRECT_SCOPE not in scopes:
            raise ChatGPTAuthError("ChatGPT plan usage was not granted; sign in and enable plan sharing")
        if not token.get("access_token") or token.get("token_type", "").lower() != "bearer":
            raise ChatGPTAuthError("ChatGPT did not return a Bearer access token")
        refresh = token.get("refresh_token") or (previous or {}).get("refresh_token")
        if not refresh:
            raise ChatGPTAuthError("ChatGPT did not return a refresh token")
        expiry = token.get("expires_at") or time.time() + float(token["expires_in"])
        return {"access_token": token["access_token"], "refresh_token": refresh,
                "id_token": token.get("id_token") or (previous or {}).get("id_token"),
                "expires_at": float(expiry), "scopes": scopes}

    def login(self, *, port: int = 0, open_browser: bool = True, announce=print) -> dict:
        # Serialize login/logout/rotating refresh across processes. No token is
        # read before locking, so a waiting refresh cannot reuse an old token.
        with self._lock():
            record = self._read()
            record.setdefault("ext_agent_host_id", "urn:uuid:" + str(uuid.uuid4()))
            self._save(record)
            metadata = self._discovery()
            verifier, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            callback: dict[str, str] = {}
            pending_state = ""

            class Callback(BaseHTTPRequestHandler):
                def setup(self):
                    self.request.settimeout(10)
                    super().setup()

                def log_message(self, *args):
                    pass

                def do_GET(self):
                    parsed = urlsplit(self.path)
                    query = parse_qs(parsed.query)
                    valid = (parsed.path == "/auth/callback"
                             and len(query.get("state", [])) == 1
                             and len(query.get("code", [])) <= 1
                             and len(query.get("client_id", [])) <= 1
                             and hmac.compare_digest(query.get("state", [""])[0], pending_state))
                    if valid:
                        callback.update({k: v[0] for k, v in query.items()})
                    self.send_response(200 if valid else 400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(b"Authorization received. Return to MineBot for the result."
                                     if valid else b"Invalid sign-in callback.")

            with HTTPServer(("127.0.0.1", port), Callback) as server:
                redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
                with self._oauth(record.get("client_id", "dynamic_agent_client"), scope=SCOPES,
                                 redirect_uri=redirect, code_challenge_method="S256") as oauth:
                    url, pending_state = oauth.create_authorization_url(
                        metadata["authorization_endpoint"], code_verifier=verifier, nonce=nonce,
                        resource=RESOURCE, agent_name_hint="MineBot",
                        ext_agent_host_id=record["ext_agent_host_id"])
                # No id_token_hint in this printable URL. Returning registration
                # still uses the issued client ID and persistent host identity.
                announce(url, flush=True)
                if open_browser:
                    webbrowser.open(url)
                server.timeout = 1
                deadline = time.monotonic() + 600
                while not callback and time.monotonic() < deadline:
                    server.handle_request()
            if not callback:
                raise ChatGPTAuthError("ChatGPT sign-in timed out after 10 minutes; run login again")
            if callback.get("error"):
                code = callback["error"]
                code = code if re.fullmatch(r"[a-z_]{1,80}", code) else "unknown_error"
                raise ChatGPTAuthError("ChatGPT authorization failed: " + code)
            client_id = callback.get("client_id") or record.get("client_id")
            if (not client_id or client_id == "dynamic_agent_client" or not callback.get("code")
                    or (record.get("client_id") and client_id != record["client_id"])):
                raise ChatGPTAuthError("ChatGPT callback has no valid matching issued client ID/code")
            # Retain the issued registration even if exchange fails; the next
            # login gets a new code for this registration instead of duplicating it.
            record["client_id"] = client_id
            self._save(record)
            with self._oauth(client_id, redirect_uri=redirect) as oauth:
                token = oauth.fetch_token(metadata["token_endpoint"], grant_type="authorization_code",
                                          code=callback["code"], code_verifier=verifier, resource=RESOURCE)
            claims = self._verify_identity(token["id_token"], client_id, metadata, nonce)
            if record.get("subject") and claims["sub"] != record["subject"]:
                raise ChatGPTAuthError("ChatGPT sign-in returned a different account for this registration")
            record.update(subject=claims["sub"], email=claims.get("email"), session=self._session(token))
            self._save(record)
        return self.status()

    def access_token(self) -> str:
        with self._lock():
            record = self._read()
            session = record.get("session", {})
            if DIRECT_SCOPE not in session.get("scopes", []):
                raise ChatGPTAuthError("ChatGPT is not connected; run minebot-auth login")
            if session["expires_at"] > time.time() + 90:
                return session["access_token"]
            metadata = self._discovery()
            with self._oauth(record["client_id"]) as oauth:
                token = oauth.refresh_token(metadata["token_endpoint"],
                                            refresh_token=session["refresh_token"], resource=RESOURCE)
            if token.get("id_token"):
                claims = self._verify_identity(token["id_token"], record["client_id"], metadata, None)
                if claims["sub"] != record["subject"]:
                    raise ChatGPTAuthError("Refreshed ChatGPT account does not match")
            record["session"] = self._session(token, previous=session)
            self._save(record)
            return record["session"]["access_token"]

    async def __call__(self) -> str:
        try:
            return await asyncio.to_thread(self.access_token)
        except ChatGPTAuthError:
            raise
        except Exception as exc:
            raise ChatGPTAuthError(f"ChatGPT token renewal failed ({_failure_label(exc)}); "
                                   "retry minebot-auth login") from None

    def status(self) -> dict:
        record = self._read()
        session = record.get("session", {})
        return {"connected": DIRECT_SCOPE in session.get("scopes", []), "email": record.get("email"),
                "expires_at": session.get("expires_at"), "usage_url": "https://chatgpt.com/settings/usage"}

    def models(self) -> list[dict]:
        token = self.access_token()
        with httpx.Client(proxy=self.proxy, timeout=30) as client:
            response = client.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            return [{"slug": m["slug"], "display_name": m.get("display_name", m["slug"])}
                    for m in response.json()["models"] if m.get("visibility") == "list"]

    def logout(self) -> dict:
        revoked = False
        with self._lock():
            record = self._read()
            session = record.pop("session", None)
            try:
                if session:
                    endpoint = self._discovery()["revocation_endpoint"]
                    with self._oauth(record["client_id"]) as oauth:
                        response = oauth.revoke_token(endpoint, token=session["refresh_token"],
                                                      token_type_hint="refresh_token")
                        response.raise_for_status()
                    revoked = True
            except Exception as exc:
                raise ChatGPTAuthError("Signed out locally; remote token revocation is unconfirmed "
                                       f"({_failure_label(exc)})") from None
            finally:
                self._save(record)
        return {"connected": False, "remote_revoked": revoked}


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect MineBot to your ChatGPT plan")
    parser.add_argument("command", choices=["login", "status", "models", "logout"])
    parser.add_argument("--auth-file", type=Path, default=os.environ.get("MINEBOT_CHATGPT_AUTH_FILE"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    auth = ChatGPTAuth(args.auth_file, proxy=os.environ.get("MINEBOT_LLM_PROXY") or None)
    try:
        result = (auth.login(port=args.port, open_browser=not args.no_browser) if args.command == "login"
                  else getattr(auth, args.command)())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ChatGPTAuthError as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception as exc:
        # OAuth error descriptions and HTTP bodies may contain credentials.
        parser.exit(1, f"ChatGPT {args.command} failed ({_failure_label(exc)}); "
                    "no credentials were printed. Retry login if authorization expired.\n")


if __name__ == "__main__":
    main()
