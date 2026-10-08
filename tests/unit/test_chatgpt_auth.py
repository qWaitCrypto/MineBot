import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from authlib.integrations.httpx_client import OAuth2Client
from cryptography.hazmat.primitives.asymmetric import rsa

from minebot.app.chatgpt_auth import ChatGPTAuth, ChatGPTAuthError, DIRECT_SCOPE, ISSUER, RESOURCE
from minebot.app.config import AppConfigError, provider_registry_from_env


def test_login_validates_callback_and_identity_then_serializes_rotating_refresh(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", use="sig")
    attempts, grants, token_requests = {}, [], []

    def handler(request):
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]})
        form = parse_qs(request.content.decode())
        token_requests.append(form)
        if form.get("grant_type") == ["authorization_code"]:
            assert form["client_id"] == ["oaiapp_test"]
            assert form["resource"] == [RESOURCE]
            assert "client_secret" not in form
            claims = {"sub": "account", "iss": ISSUER, "aud": "oaiapp_test",
                      "iat": time.time(), "exp": time.time() + 3600, "nonce": attempts["nonce"][0]}
            return httpx.Response(200, json={"access_token": "first", "refresh_token": "rotate-1",
                "token_type": "Bearer", "expires_in": 3600, "scope": DIRECT_SCOPE,
                "id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})})
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["rotate-1"]
        assert "scope" not in form
        grants.append(form)
        return httpx.Response(200, json={"access_token": "second", "refresh_token": "rotate-2",
                                      "token_type": "Bearer", "expires_in": 3600})

    transport = httpx.MockTransport(handler)
    original_get = httpx.Client.get
    def get(client, url, **kw):
        if str(url).endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [jwk]}, request=httpx.Request("GET", url))
        return original_get(client, url, **kw)
    monkeypatch.setattr(httpx.Client, "get", get)
    auth = ChatGPTAuth(tmp_path / "chatgpt.json")
    metadata = {"authorization_endpoint": ISSUER + "/api/accounts/authorize",
                "token_endpoint": ISSUER + "/api/accounts/oauth/token",
                "jwks_uri": ISSUER + "/.well-known/jwks.json"}
    monkeypatch.setattr(auth, "_discovery", lambda: metadata)
    monkeypatch.setattr(auth, "_oauth", lambda client_id, **kw: OAuth2Client(
        client_id=client_id, token_endpoint_auth_method="none", transport=transport, **kw))
    callback_results = []
    callback_threads = []

    def authorize(url, **kw):
        attempts.update(parse_qs(urlsplit(url).query))
        assert attempts["client_id"] == ["dynamic_agent_client"]
        assert attempts["code_challenge_method"] == ["S256"]
        assert attempts["ext_agent_host_id"][0].startswith("urn:uuid:")
        redirect = attempts["redirect_uri"][0]
        assert urlsplit(redirect).hostname == "127.0.0.1"

        def callback():
            with httpx.Client(trust_env=False) as client:
                bad = client.get(redirect, params={"state": "wrong", "code": "do-not-redeem"})
                good = client.get(redirect, params={"state": attempts["state"][0],
                                                   "code": "fresh-code", "client_id": "oaiapp_test"})
                callback_results.extend([bad.status_code, good.status_code])
        thread = threading.Thread(target=callback, daemon=True)
        callback_threads.append(thread)
        thread.start()

    assert auth.login(open_browser=False, announce=authorize)["connected"]
    callback_threads[0].join(timeout=2)
    assert callback_results == [400, 200]
    record = auth._read()
    assert record["subject"] == "account"
    assert auth.path.stat().st_mode & 0o777 == 0o600
    record["session"]["expires_at"] = 0
    auth._save(record)
    with ThreadPoolExecutor(3) as executor:
        assert list(executor.map(lambda _: auth.access_token(), range(3))) == ["second"] * 3
    assert len(grants) == 1
    assert auth._read()["session"]["refresh_token"] == "rotate-2"
    assert "second" not in json.dumps(auth.status())
    with pytest.raises(ChatGPTAuthError, match="nonce"):
        auth._verify_identity(record["session"]["id_token"], "oaiapp_test", metadata, "other-nonce")
    with pytest.raises(jwt.InvalidAudienceError):
        auth._verify_identity(record["session"]["id_token"], "other-client", metadata, attempts["nonce"][0])


def test_logout_clears_local_session_when_remote_revocation_fails(tmp_path, monkeypatch):
    auth = ChatGPTAuth(tmp_path / "chatgpt.json")
    auth._save({"client_id": "registered", "ext_agent_host_id": "stable", "subject": "account",
                "session": {"refresh_token": "secret"}})
    monkeypatch.setattr(auth, "_discovery", lambda: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    with pytest.raises(ChatGPTAuthError, match="unconfirmed"):
        auth.logout()
    assert auth._read() == {"client_id": "registered", "ext_agent_host_id": "stable", "subject": "account"}


def test_chatgpt_provider_needs_no_key_and_never_sends_oauth_to_custom_endpoint(tmp_path):
    import asyncio
    env = {"MINEBOT_LLM_AUTH": "chatgpt", "MINEBOT_LLM_MODEL": "gpt-6.1-sol",
           "MINEBOT_CHATGPT_AUTH_FILE": str(tmp_path / "auth.json"),
           "MINEBOT_LLM_TEMPERATURE": "0.5", "MINEBOT_LLM_MAX_TOKENS": "123"}
    provider = provider_registry_from_env(env)
    client, model, settings, _ = provider.responses_connection_config()
    assert str(client.base_url) == RESOURCE + "/"
    assert model == "gpt-6.1-sol" and provider.responses_tool_namespace == "minebot"
    assert "temperature" not in settings and "max_output_tokens" not in settings
    assert settings["context_management"][0]["compact_threshold"] == 64_000
    assert provider.trace_configs()[0]["auth"] == "chatgpt"
    asyncio.run(provider.aclose())
    for patch in ({"MINEBOT_LLM_BASE_URL": "https://proxy.example/v1"}, {"MINEBOT_AGENT_KERNEL": "sdk"}):
        with pytest.raises(AppConfigError):
            provider_registry_from_env({**env, **patch})
    with pytest.raises(ChatGPTAuthError, match="sharing"):
        ChatGPTAuth(tmp_path / "auth.json")._session({"scope": "openid"})
