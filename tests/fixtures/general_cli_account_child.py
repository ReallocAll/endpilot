"""Run the general entry point against local identity services and a RakNet peer."""

from contextlib import AsyncExitStack, ExitStack
import asyncio
import base64
from io import StringIO
import json
from pathlib import Path
import runpy
import sys
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from endpilot import Bot, _cli_terminal, xbox
from endpilot.xbox import _cache, _verify
from test_xbox_cache_storage import _msal_state
from test_xbox_device_flow import public_key
from test_xbox_provider_join import HttpIssuers, OAUTH_ENDPOINT, RETAIL_ISSUER, USER_CODE, wire


async def seed_mixed_cache(path):
    provider = xbox.XboxCredentialProvider(remember_account=True, on_device_code=lambda _uri, _code: None)
    await provider(public_key())
    account = "another-user.fixture-tenant"
    state = _msal_state(account, client_id=xbox.DEFAULT_CLIENT_ID, refresh_token="another-account-renewal")
    record = _cache.AccountRecord(account, json.dumps(state), "987654321", "Another Player")
    async with _cache.locked_cache(path):
        _cache.upsert_account(path, record)
        return next(saved for saved in _cache.load_accounts(path) if saved.xuid == record.xuid)


def main(phase, report):
    services = HttpIssuers()
    session = AsyncExitStack()
    peer = None
    mixed = phase in ("legacy", "expired", "unproven", "unproven-no-terminal")

    def microsoft(request):
        if str(request.url) != f"{OAUTH_ENDPOINT}/token":
            return services.oauth(request)
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["urn:ietf:params:oauth:grant-type:device_code"]:
            response = services.oauth(request)
            token = dict(response.json(), expires_in=0)
        else:
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == ["fixture-renewal"]
            services.calls.append("refresh-token")
            if phase == "expired":
                return httpx.Response(400, json={"error": "invalid_grant"})
            if phase in ("unproven", "unproven-no-terminal"):
                return httpx.Response(200, json={
                    "access_token": "unproven-msal-access", "expires_in": 600, "scope": "XboxLive.signin",
                    "refresh_token": "unproven-renewal",
                })
            token = {"access_token": "local-msal-access", "expires_in": 600, "scope": "XboxLive.signin"}
        client_info = base64.urlsafe_b64encode(b'{"uid":"fixture-user","utid":"fixture-tenant"}').decode()
        return httpx.Response(200, json=dict(token, refresh_token="fixture-renewal", client_info=client_info))

    sdk_join, sdk_close = Bot.join_address, Bot.close

    async def join(bot, address, **options):
        nonlocal peer
        assert address == "raknet://fixture.invalid:19132"
        peer = await session.enter_async_context(wire.LocalServer(services, "raknet").listen())
        await sdk_join(bot, f"raknet://{peer.address}", chunk_radius=7, locale=wire.LOCALE, **options)
        await peer.wait(peer.initialized)
        await bot.send_command("list")
        await bot.change_skin("general-cli-fixture")
        await peer.wait(peer.post_spawn_checked)

    async def close(bot):
        try:
            await sdk_close(bot)
            if peer and peer.initialized.is_set():
                await peer.wait(peer.disconnected)
        finally:
            await session.aclose()

    terminal = StringIO() if phase == "first" or mixed else None
    path = xbox.account_cache_path()
    other = None
    before = None

    def test_terminal(stream, **_options):
        return terminal is not None and stream is terminal

    with ExitStack() as patched:
        patched.enter_context(patch.object(Bot, "join_address", join))
        patched.enter_context(patch.object(Bot, "close", close))
        if phase == "unproven-no-terminal":
            patched.enter_context(patch.object(_cli_terminal, "_device_terminal", side_effect=(terminal, None)))
        else:
            patched.enter_context(patch.object(_cli_terminal, "_device_terminal", return_value=terminal))
        patched.enter_context(patch.object(_cli_terminal, "_is_terminal", side_effect=test_terminal))
        patched.enter_context(patch.object(_verify, "_MOJANG_ROOT", wire.der64(services.root.public_key())))
        patched.enter_context(patch.object(wire, "ISSUER", RETAIL_ISSUER))
        oauth = httpx.MockTransport(microsoft)
        exchange = httpx.MockTransport(services.exchange)
        patched.enter_context(patch.object(httpx.HTTPTransport, "handle_request", new=lambda _, request:
                                           oauth.handle_request(request)))
        patched.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", new=lambda _, request:
                                           exchange.handle_async_request(request)))
        if mixed:
            other = asyncio.run(seed_mixed_cache(path))
            before = path.read_bytes()
            services.calls.clear()
            services.token_requests.clear()
            patched.enter_context(patch.object(sys, "stdin", StringIO("1\n")))
            patched.enter_context(patch.object(_cli_terminal, "_can_read_terminal", return_value=True))
        sys.argv = ["endpilot", "join", "raknet://fixture.invalid:19132", "--xbox", "--hold", "0"]
        try:
            runpy.run_module("endpilot", run_name="__main__", alter_sys=True)
        except SystemExit as error:
            status = error.code

    if peer is not None and peer.failure is not None:
        raise peer.failure
    accounts = _cache.load_accounts(path)
    text = terminal.getvalue() if terminal is not None else ""
    Path(report).write_text(json.dumps({
        "device_grants": services.calls.count("device-grant"),
        "refreshes": services.calls.count("refresh-token"),
        "keys": services.token_requests,
        "prompted": USER_CODE in text,
        "warned_before_prompt": 0 <= text.find("plaintext") < text.find(USER_CODE),
        "listed_legacy": "1. Legacy Xbox account (XUID unavailable)" in text,
        "showed_microsoft_account": "fixture-user.fixture-tenant" in text,
        "other_account_unchanged": other in accounts,
        "cache_unchanged": before is not None and before == path.read_bytes(),
        "saved_accounts": [(account.xuid, account.gamertag) for account in accounts],
        "connections": peer.connections if peer else 0,
        "initialized": bool(peer and peer.initialized.is_set()),
        "disconnected": bool(peer and peer.disconnected.is_set()),
    }), encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
