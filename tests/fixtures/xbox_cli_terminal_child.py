"""Run the general CLI with a native terminal and synthetic Xbox authorization."""

import asyncio
import base64
from contextlib import ExitStack
from io import StringIO
import json
import logging
import os
from pathlib import Path
from runpy import run_module
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import webbrowser

import httpx

from endpilot import BedrockCredentials, xbox
from endpilot.xbox import _cache
from test_xbox_device_flow import ACCESS_TOKEN, CODE, DEVICE, MACHINE_CODE, METADATA, TENANT, TOKEN, URI, public_key


PROFILE = ("9007199254740993", "Fixture Player")


def console_reader(opened):
    import win32con
    import win32console
    import win32file

    handle = win32file.CreateFile("CONOUT$", win32con.GENERIC_READ, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                                 None, win32con.OPEN_EXISTING, 0, None)
    screen = win32console.PyConsoleScreenBufferType(handle.Detach())
    opened.callback(screen.Close)
    size = screen.GetConsoleScreenBufferInfo()["Size"]
    return lambda: screen.ReadConsoleOutputCharacter(size.X * min(10, size.Y), win32console.PyCOORDType(0, 0))


def main(home, scenario, *terminal_fds):
    requests = []

    def respond(request):
        url = str(request.url)
        requests.append(url)
        if url == METADATA:
            return httpx.Response(200, json={
                "issuer": TENANT + "/v2.0", "authorization_endpoint": TENANT + "/oauth2/v2.0/authorize",
                "device_authorization_endpoint": DEVICE, "token_endpoint": TOKEN,
            })
        if url == DEVICE:
            complete = f"{URI}?otc={CODE}"
            return httpx.Response(200, json={
                "verification_uri": URI, "verification_uri_complete": complete, "user_code": CODE,
                "device_code": MACHINE_CODE, "interval": 1, "expires_in": 30, "message": complete,
            })
        if url == TOKEN:
            identity = json.dumps({"uid": "fixture-user", "utid": "fixture-tenant"}).encode()
            return httpx.Response(200, json={
                "access_token": ACCESS_TOKEN, "refresh_token": "fixture-renewal", "token_type": "Bearer",
                "scope": "XboxLive.signin", "expires_in": 3600, "client_info": base64.urlsafe_b64encode(identity).decode(),
            })
        raise AssertionError(f"unexpected request to {url}")

    client = httpx.Client

    def fixture_client(**kwargs):
        return client(transport=httpx.MockTransport(respond), **kwargs)

    async def join(_address, *, credential_provider, **_options):
        await credential_provider(public_key())

    exchange = AsyncMock(return_value=BedrockCredentials(("fixture-one", "fixture-two"), "fixture-bedrock-token"))
    bot = SimpleNamespace(join_address=join, close=AsyncMock())
    logs = StringIO()
    handler = logging.StreamHandler(logs)
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    with ExitStack() as opened:
        opened.callback(logger.removeHandler, handler)
        opened.enter_context(patch.object(httpx, "Client", side_effect=fixture_client))
        opened.enter_context(patch.object(xbox, "_exchange", SimpleNamespace(exchange=exchange), create=True))
        opened.enter_context(patch.object(xbox, "_verify",
                                          SimpleNamespace(verify_account=AsyncMock(return_value=PROFILE)),
                                          create=True))
        opened.enter_context(patch("endpilot.Bot", return_value=bot))
        browser = opened.enter_context(patch.object(webbrowser, "open"))

        if sys.platform == "win32":
            read_console = console_reader(opened)
            input_device, output_device = "CONIN$", "CONOUT$"
        else:
            from fcntl import ioctl
            from termios import TIOCSCTTY

            slave = int(terminal_fds[0])
            opened.callback(os.close, slave)
            ioctl(slave, TIOCSCTTY)
            read_console = lambda: ""
            input_device = output_device = "/dev/tty"
        if scenario == "stdout":
            output = opened.enter_context(open(output_device, "w", encoding="utf-8"))
            opened.enter_context(patch.object(sys, "stdout", output))
        elif scenario in {"device", "null_stderr", "broken_stderr"}:
            source = opened.enter_context(open(input_device, "r", encoding="utf-8"))
            opened.enter_context(patch.object(sys, "stdin", source))
        if scenario == "null_stderr":
            null = opened.enter_context(open(os.devnull, "w", encoding="utf-8"))
            opened.enter_context(patch.object(sys, "stderr", null))

        cache = xbox.account_cache_path()
        if scenario == "cached":
            provider = xbox.XboxCredentialProvider(on_device_code=lambda *_: None)
            asyncio.run(provider._credentials_for_account(public_key()))
            requests.clear()
            exchange.reset_mock()
        before = cache.read_bytes() if cache.exists() else None
        sys.argv = ["endpilot", "join", "raknet://127.0.0.1:19132", "--xbox", "--hold", "0"]
        try:
            run_module("endpilot", run_name="__main__", alter_sys=True)
        except SystemExit as error:
            status = error.code
        after = cache.read_bytes() if cache.exists() else None
        report = {
            "terminal": read_console(), "logs": logs.getvalue(),
            "requests": [requests.count(DEVICE), requests.count(TOKEN)], "exchange": exchange.await_count,
            "cli_status": status,
            "accounts": [(record.xuid, record.gamertag) for record in _cache.load_accounts(cache)],
            "cache_unchanged": before == after, "browser": [args[0] for args, _ in browser.call_args_list],
        }
        (Path(home) / "terminal-report.json").write_text(json.dumps(report), encoding="utf-8")
        return status


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
