import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from prefix_proxy import serve_prefix
from pydantic import ValidationError

from cloud_browser.config import Settings
from cloud_browser.store import Store


@pytest.mark.parametrize("prefix", ["", "/browser", "/apps/browser", "/v1.0/browser_~-"])
def test_canonical_prefix_settings(prefix):
    cfg = Settings(
        _env_file=None,
        _env_prefix="PREFIX_TEST_",
        development=True,
        public_origin="https://browser.example",
        control_origin="https://browser.example:9443",
        public_path_prefix=prefix,
    )
    assert cfg.public_origin == "https://browser.example"
    assert cfg.public_base == cfg.issuer == cfg.public_origin + prefix
    assert cfg.resource == cfg.public_base + "/mcp"
    assert cfg.authorization_path == prefix + "/authorize"
    assert (
        cfg.resource_metadata_url
        == cfg.public_origin + "/.well-known/oauth-protected-resource" + prefix + "/mcp"
    )
    assert (
        cfg.issuer_metadata_url
        == cfg.public_origin + "/.well-known/oauth-authorization-server" + prefix
    )
    assert cfg.control_origin == "https://browser.example:9443"


@pytest.mark.parametrize(
    "prefix",
    [
        "/",
        "browser",
        "/browser/",
        "//browser",
        "https://evil.test/browser",
        "/browser?x=1",
        "/browser?",
        "/browser#fragment",
        "/browser#",
        "/browser%2fmcp",
        "/%62rowser",
        "/browser%252f..",
        "/browser/.",
        "/browser/..",
        "/./browser",
        "/../browser",
        "/browser\\mcp",
        "/browser//mcp",
        "/browser\n",
        "/br owser",
        "/browser;param",
        "/브라우저",
        "/" + "x" * 128,
    ],
    ids=[
        "slash",
        "relative",
        "trailing",
        "authority",
        "absolute",
        "query",
        "empty-query",
        "fragment",
        "empty-fragment",
        "escaped-slash",
        "escaped-letter",
        "double-escape",
        "dot",
        "dotdot",
        "leading-dot",
        "leading-dotdot",
        "backslash",
        "double-slash",
        "newline",
        "space",
        "parameter",
        "unicode",
        "long",
    ],
)
def test_unsafe_prefix_rejected(prefix):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, _env_prefix="PREFIX_TEST_", development=True, public_path_prefix=prefix
        )


def test_origin_cannot_contain_the_public_prefix():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            _env_prefix="PREFIX_TEST_",
            development=True,
            public_origin="https://browser.example/browser",
        )


def authorize_params(cfg, **overrides):
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return {
        "client_id": cfg.oauth_client_id,
        "redirect_uri": cfg.oauth_redirect_uris[0],
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "resource": cfg.resource,
        "scope": "browser",
        "state": "prefix-local-test",
        **overrides,
    }, verifier


async def authorization(client, cfg):
    params, verifier = authorize_params(cfg)
    page = await client.get(cfg.public_base + "/authorize", params=params)
    assert page.status_code == 200
    assert f"action={cfg.authorization_path}>" in page.text
    assert f"Path={cfg.authorization_path}" in page.headers["set-cookie"]
    assert page.headers["referrer-policy"] == "same-origin"
    assert len(page.headers.get_list("content-security-policy")) == 1
    nonce = re.search("name=nonce value='([^']+)'", page.text)[1]
    response = await client.post(
        cfg.public_base + "/authorize",
        data={"nonce": nonce, "password": "test administrator password"},
        headers={"Origin": cfg.public_origin},
    )
    assert response.status_code == 303
    assert response.headers["referrer-policy"] == "no-referrer"
    assert f"Path={cfg.authorization_path}" in response.headers["set-cookie"]
    result = parse_qs(urlsplit(response.headers["location"]).query)
    assert result["iss"] == [cfg.issuer] and result["state"] == [params["state"]]
    return {
        "grant_type": "authorization_code",
        "code": result["code"][0],
        "code_verifier": verifier,
        "client_id": cfg.oauth_client_id,
        "resource": cfg.resource,
        "redirect_uri": params["redirect_uri"],
    }


async def test_real_http_root_and_stripped_prefix_registry_and_oauth_match(cfg, tmp_path):
    registries = []
    control_origin = "https://browser.example:9443"
    for name, prefix in (("root", ""), ("prefixed", "/browser")):
        config = cfg.model_copy(
            deep=True, update={"data_dir": tmp_path / name, "control_origin": control_origin}
        )
        async with serve_prefix(config, prefix) as stack:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                denied = await client.get(config.resource)
                assert denied.status_code == 401
                metadata_url = re.search(
                    'resource_metadata="([^"]+)"', denied.headers["www-authenticate"]
                )[1]
                assert metadata_url == config.resource_metadata_url
                resource = (await client.get(metadata_url)).json()
                assert resource["resource"] == config.resource
                assert resource["authorization_servers"] == [config.issuer]
                issuer = urlsplit(resource["authorization_servers"][0])
                discovery_url = f"{issuer.scheme}://{issuer.netloc}/.well-known/oauth-authorization-server{issuer.path}"
                metadata = (await client.get(discovery_url)).json()
                assert metadata["issuer"] == config.issuer
                for field, path in (
                    ("authorization_endpoint", "/authorize"),
                    ("token_endpoint", "/token"),
                    ("revocation_endpoint", "/revoke"),
                ):
                    assert metadata[field] == config.public_base + path
                assert metadata["code_challenge_methods_supported"] == ["S256"]
                assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
                assert metadata["scopes_supported"] == ["browser"]
                form = await authorization(client, config)
                wrong = await client.post(
                    metadata["token_endpoint"],
                    data=form | {"resource": config.resource + "?wrong=1"},
                )
                assert wrong.status_code == 400
                token_response = await client.post(metadata["token_endpoint"], data=form)
                assert token_response.status_code == 200
                token = token_response.json()
                assert (await client.post(metadata["token_endpoint"], data=form)).status_code == 400
                async with httpx2.AsyncClient(
                    trust_env=False,
                    headers={
                        "Authorization": "Bearer " + token["access_token"],
                        "Origin": config.public_origin,
                    },
                ) as http:
                    async with streamable_http_client(config.resource, http_client=http) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            tools = (await session.list_tools()).tools
                            registries.append(
                                sorted(
                                    (tool.model_dump() for tool in tools),
                                    key=lambda tool: tool["name"],
                                )
                            )
                            assert len(tools) == 12
                            opened = (
                                await session.call_tool("browser_open", {})
                            ).structured_content
                            assert opened["status"] == "ok"
                            target = {key: opened[key] for key in ("session_id", "tab_id")}
                            observed = await session.call_tool(
                                "browser_observe", target | {"mode": "visual"}
                            )
                            assert observed.structured_content["status"] == "ok"
                            assert any(item.type == "image" for item in observed.content)
                            closed = await session.call_tool(
                                "browser_close",
                                {"session_id": target["session_id"], "scope": "session"},
                            )
                            assert closed.structured_content["status"] == "ok"
                refresh = {
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                    "client_id": config.oauth_client_id,
                    "resource": config.resource,
                }
                rotated = await client.post(metadata["token_endpoint"], data=refresh)
                assert rotated.status_code == 200
                assert (
                    await client.post(metadata["token_endpoint"], data=refresh)
                ).status_code == 400
                assert (
                    await client.post(
                        metadata["revocation_endpoint"],
                        data={
                            "client_id": config.oauth_client_id,
                            "token": rotated.json()["refresh_token"],
                        },
                    )
                ).status_code == 200
                assert not stack.auth.bearer("Bearer " + token["access_token"])
                assert config.control_origin == control_origin
                assert (await client.get(config.public_base + "/login")).status_code == 404
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=stack.control), base_url=control_origin
                ) as private:
                    assert (await private.get("/login")).status_code == 200
                    assert (await private.get("/", follow_redirects=False)).headers[
                        "location"
                    ] == "/login"
    assert registries[0] == registries[1]


async def test_real_http_prefix_rejects_wrong_origin_host_resource_and_paths(cfg):
    async with serve_prefix(cfg, "/browser") as stack:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            for value in (cfg.public_origin, cfg.resource + "/", cfg.resource + "?a=b"):
                params, _ = authorize_params(cfg, resource=value)
                assert (
                    await client.get(cfg.public_base + "/authorize", params=params)
                ).status_code == 400
            for origin in ("null", cfg.public_base, "https://evil.example"):
                assert (
                    await client.post(cfg.public_base + "/authorize", headers={"Origin": origin})
                ).status_code == 403
            assert (
                await client.get(cfg.resource, headers={"Host": "evil.example"})
            ).status_code == 421
            assert (
                await client.get(
                    stack.backend_url + "/mcp",
                    headers={
                        "Host": "evil.example",
                        "X-Forwarded-Host": urlsplit(cfg.public_origin).netloc,
                    },
                )
            ).status_code == 421
            poisoned = await client.get(
                cfg.issuer_metadata_url,
                headers={
                    "Forwarded": "host=evil.example;proto=http",
                    "X-Forwarded-Host": "evil.example",
                    "X-Forwarded-Prefix": "/evil",
                    "X-Forwarded-Proto": "http",
                },
            )
            assert poisoned.json()["issuer"] == cfg.issuer
            for path in (
                "/mcp",
                "/wrong/mcp",
                "/browserish/mcp",
                "/.well-known/oauth-authorization-server/wrong",
            ):
                assert (await client.get(cfg.public_origin + path)).status_code == 404
            stack.auth.store.put("grant", "test-grant", {"active": True})
            token = stack.auth.issue("test-grant")["access_token"]
            for path in (
                "/mcp/",
                "/authorize/",
                "/token/",
                "/revoke/",
                "/.well-known/oauth-authorization-server/",
                "/browser/mcp",
            ):
                result = await client.get(
                    cfg.public_base + path, headers={"Authorization": "Bearer " + token}
                )
                assert result.status_code == 404 and "location" not in result.headers
            assert (
                await client.head(cfg.issuer_metadata_url)
            ).status_code == 404  # No HEAD feature added.
            assert (
                await client.post(
                    cfg.resource,
                    json={},
                    headers={"Authorization": "Bearer " + token, "Origin": cfg.public_base},
                )
            ).status_code == 403


async def test_old_audiences_cannot_be_upgraded_and_grants_are_not_deleted(cfg):
    old_resource = cfg.public_origin + "/mcp"
    grant = {"active": True}
    stale = {
        "resource": old_resource,
        "grant": "retained-grant",
        "client_id": cfg.oauth_client_id,
    }
    params, verifier = authorize_params(cfg, resource=old_resource)
    # Persist records before app startup, just as a previous deployment would.
    existing = Store(cfg.data_dir / "state.sqlite3")
    try:
        existing.put("grant", "retained-grant", grant)
        existing.put("access", "old-access", stale)
        existing.put("refresh", "old-refresh", stale)
        existing.put("code", "old-code", params)
    finally:
        existing.close()
    async with serve_prefix(cfg, "/browser") as stack:
        assert (
            stack.auth.store.get("refresh", "old-refresh") == stale
        )  # Startup did not migrate/delete.
        assert stack.auth.store.get("code", "old-code") == params
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            assert (
                await client.get(cfg.resource, headers={"Authorization": "Bearer old-access"})
            ).status_code == 401
            common = {"client_id": cfg.oauth_client_id, "resource": cfg.resource}
            refreshed = await client.post(
                cfg.public_base + "/token",
                data=common | {"grant_type": "refresh_token", "refresh_token": "old-refresh"},
            )
            assert refreshed.status_code == 400
            coded = await client.post(
                cfg.public_base + "/token",
                data=common
                | {
                    "grant_type": "authorization_code",
                    "code": "old-code",
                    "code_verifier": verifier,
                    "redirect_uri": params["redirect_uri"],
                },
            )
            assert coded.status_code == 400
            valid, _ = authorize_params(cfg)
            page = await client.get(cfg.public_base + "/authorize", params=valid)
            nonce = re.search("name=nonce value='([^']+)'", page.text)[1]
            stack.auth.store.put("authorize", nonce, params)  # Simulate pending old-origin form.
            denied = await client.post(
                cfg.public_base + "/authorize",
                data={"nonce": nonce, "password": "test administrator password"},
                headers={"Origin": cfg.public_origin},
            )
            assert denied.status_code == 403
        assert stack.auth.store.get("grant", "retained-grant") == grant
        assert stack.auth.store.get("access", "old-access") == stale
