"""Verify MCP account tooling with private temporary stores and synthetic issuance."""

import asyncio
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from importlib import import_module
from importlib.util import find_spec
from io import StringIO
import json
import logging
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from endpilot import Bot, LoginRefused, xbox
from endpilot._cli_terminal import _print_remember_notice
from endpilot._mcp_accounts import AccountError, MCPAccounts
from test_cli_skins_login import LoginCapture
from test_skin import _png, _texture
from test_xbox_device_flow import ACCESS_TOKEN, CODE, DEVICE, MACHINE_CODE, URI, _MsalTestCase
from test_xbox_provider_join import HttpIssuers, RETAIL_ISSUER, wire


_HAS_XBOX = all(find_spec(name) for name in ("httpx", "jwt", "msal")) and (
    os.name != "nt" or find_spec("win32security") is not None
)
_ACCOUNT = {"xuid": wire.XUID, "gamertag": wire.PROFILE}
_REFRESH = "local-mcp-refresh-secret"
_ACCESS = "local-msal-access"
_SECRETS = (ACCESS_TOKEN, MACHINE_CODE, _REFRESH, _ACCESS)
_getaddrinfo = socket.getaddrinfo


@unittest.skipUnless(_HAS_XBOX, "requires the Xbox extra")
class MCPAccountTests(_MsalTestCase):
    def setUp(self):
        super().setUp()
        self.accounts = MCPAccounts()
        self.addAsyncCleanup(self.accounts.close)
        self.path = xbox.account_cache_path()

    def token(self, refresh=_REFRESH):
        body = {
            "access_token": _ACCESS, "expires_in": 3600, "token_type": "Bearer", "scope": "XboxLive.signin",
            "client_info": wire.url64(json.dumps({"uid": "mcp", "utid": "fixture"}).encode()),
        }
        if refresh is not None:
            body["refresh_token"] = refresh
        return self.httpx.Response(200, json=body)

    def services(self):
        # The parent fixture installs stubs. Use the production exchange and verification.
        exchange = import_module("endpilot.xbox._exchange")
        verify = import_module("endpilot.xbox._verify")
        services = HttpIssuers()
        self.enterContext(patch.multiple(xbox, _exchange=exchange, _verify=verify))
        self.enterContext(patch.object(verify, "_MOJANG_ROOT", wire.der64(services.root.public_key())))
        self.enterContext(patch.object(wire, "ISSUER", RETAIL_ISSUER))
        construct = self.httpx.AsyncClient
        self.enterContext(patch.object(self.httpx, "AsyncClient", side_effect=lambda **options: construct(
            transport=self.httpx.MockTransport(services.exchange), **options,
        )))
        return services

    async def status_when(self, authorization_id, predicate):
        async with asyncio.timeout(5):
            while True:
                status = await self.accounts.account_authorization_status(authorization_id)
                if predicate(status):
                    return status
                await asyncio.sleep(0.01)

    async def final_status(self, authorization_id):
        return await self.status_when(authorization_id, lambda status: status["status"] in (
            "cancelled", "failed", "succeeded",
        ))

    async def run_cli(self, *args):
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "endpilot", "account", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await child.communicate()
        self.assertEqual(child.returncode, 0, err.decode())
        self.assertEqual(err, b"")
        return out.decode()

    @unittest.skipUnless(find_spec("PIL") and find_spec("raknet"), "requires the skin and RakNet extras")
    async def test_cli_interoperability_and_fresh_signed_join_with_saved_skin(self):
        services = self.services()
        requests, _, _ = self.authority(self.token())
        self.assertEqual(await self.accounts.account_list(), [])
        started = await self.accounts.account_add()
        self.assertEqual(started["status"], "pending")
        final = await self.final_status(started["authorization_id"])
        self.assertEqual(final, {
            "authorization_id": started["authorization_id"], "status": "succeeded", "account": _ACCOUNT,
        })
        self.assertEqual(await self.accounts.account_list(), [_ACCOUNT])
        self.assertEqual((await self.run_cli("list")).strip(), f"{wire.XUID}\t{wire.PROFILE}")

        rows = _texture(11, 64, 64)
        path = Path(os.environ["HOME"]) / "account skin.png"
        path.write_bytes(_png(64, 64, rows))
        await self.run_cli("skin", "set", str(path), "--xuid", wire.XUID, "--model", "slim")

        # Force consumption of the skin by the public SDK and inspect the signed login at the peer.
        services.issued.clear()
        capture = LoginCapture(services)

        def resolve_loopback(host, port, *, type):
            self.assertEqual(host, "127.0.0.1")
            return _getaddrinfo(host, port, type=type, flags=socket.AI_NUMERICHOST)

        with patch.object(socket, "getaddrinfo", new=resolve_loopback):
            async with wire.LocalServer(capture, "raknet", reject=True).listen() as peer:
                bot = Bot("Local test")
                try:
                    # The verified gamertag lookup is case-insensitive, like CLI --name.
                    provider = self.accounts.credential_provider(bot, gamertag=wire.PROFILE.swapcase())
                    with self.assertRaises(LoginRefused):
                        await bot.join_address(f"raknet://{peer.address}", credential_provider=provider)
                    if peer.failure is not None:
                        raise peer.failure
                    self.assertTrue(peer.login_seen.is_set())
                finally:
                    await bot.close()
        self.assertEqual(capture.identity, (wire.XUID, wire.PROFILE))
        self.assertEqual(capture.client["ArmSize"], "slim")
        self.assertEqual(wire.unurl(capture.client["SkinData"]), b"".join(rows))
        add_key, join_key = services.chain_requests
        self.assertNotEqual(add_key, join_key)
        self.assertEqual(services.token_requests, [add_key, join_key])
        self.assertEqual(sum(str(request.url) == DEVICE for request in requests), 1)

        await self.accounts.account_skin_clear(xuid=wire.XUID)
        from endpilot.xbox import _cache

        self.assertIsNone(_cache.load_accounts(self.path)[0].skin)
        await self.accounts.account_skin_set(str(path), "slim", gamertag=wire.PROFILE)
        self.assertEqual(_cache.load_accounts(self.path)[0].skin, _cache.SkinBinding(str(path), "slim"))
        await self.run_cli("skin", "clear")
        self.assertIsNone(_cache.load_accounts(self.path)[0].skin)
        self.assertEqual(await self.accounts.account_forget(), {"forgotten": True, **_ACCOUNT})
        self.assertEqual(await self.accounts.account_forget(), {"forgotten": False})
        self.assertEqual((await self.run_cli("list")).strip(), "No remembered Xbox accounts.")

    async def test_legacy_account_can_be_listed_and_forgotten_without_a_verified_profile(self):
        from endpilot.xbox import _cache
        from test_xbox_device_flow import public_key

        account_id = "legacy.fixture"
        async with _cache.locked_cache(self.path):
            _cache.save_cache(self.path, account_id, json.dumps({"tag": _REFRESH}))
        previous = self.path.read_bytes()
        self.assertEqual((await self.run_cli("list")).strip(), "Legacy Xbox account (XUID unavailable)")
        row, = await self.accounts.account_list()
        self.assertIsNone(row["xuid"])
        self.assertIsNone(row["gamertag"])
        for secret in (account_id, _REFRESH):
            self.assertNotIn(secret, json.dumps(row))
        with self.assertRaisesRegex(AccountError, "No matching verified Xbox account"):
            await self.accounts.account_skin_clear()
        provider = self.accounts.credential_provider(Bot("Local test"), xuid=row["legacy_selector"])
        with self.assertRaisesRegex(AccountError, "No matching verified Xbox account"):
            await provider(public_key())
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(await self.accounts.account_forget(), {"forgotten": True, **row})
        self.assertEqual((await self.run_cli("list")).strip(), "No remembered Xbox accounts.")

    async def test_forget_uses_opaque_host_local_selectors_for_multiple_legacy_records(self):
        from endpilot.xbox import _cache

        first_id, second_id = "microsoft.first", "microsoft.second"
        persisted = json.dumps({"tag": _REFRESH})
        async with _cache.locked_cache(self.path):
            _cache._write_accounts(self.path, (
                _cache.AccountRecord(first_id, persisted),
                _cache.AccountRecord(second_id, persisted),
                _cache.AccountRecord("microsoft.verified", persisted, wire.XUID, wire.PROFILE),
            ))
        previous = self.path.read_bytes()
        first, second, verified = await self.accounts.account_list()
        self.assertEqual(verified, _ACCOUNT)
        self.assertNotEqual(first["legacy_selector"], second["legacy_selector"])
        self.assertEqual(await self.accounts.account_list(), [first, second, verified])
        for secret in (first_id, second_id, _REFRESH):
            self.assertNotIn(secret, json.dumps([first, second, verified]))
        with self.assertRaisesRegex(AccountError, "Multiple Xbox accounts"):
            await self.accounts.account_forget()
        with self.assertRaisesRegex(AccountError, "No matching legacy"):
            await self.accounts.account_forget(legacy_selector=second_id)
        with self.assertRaisesRegex(AccountError, "No matching legacy"):
            await MCPAccounts().account_forget(legacy_selector=second["legacy_selector"])
        with self.assertRaisesRegex(AccountError, "single Xbox account selector"):
            await self.accounts.account_forget(xuid=wire.XUID, legacy_selector=second["legacy_selector"])
        self.assertEqual(self.path.read_bytes(), previous)

        self.assertEqual(
            await self.accounts.account_forget(legacy_selector=second["legacy_selector"]),
            {"forgotten": True, **second},
        )
        self.assertEqual([row.home_account_id for row in _cache.load_accounts(self.path)],
                         [first_id, "microsoft.verified"])
        with self.assertRaisesRegex(AccountError, "No matching legacy"):
            await self.accounts.account_forget(legacy_selector=second["legacy_selector"])
        self.assertEqual(await self.accounts.account_forget(xuid=wire.XUID), {"forgotten": True, **verified})
        self.assertEqual(await self.accounts.account_list(), [first])

    async def test_provider_unavailable_and_unrememberable_results_are_sanitized(self):
        self.authority(
            self.httpx.Response(503, json={"error_description": ACCESS_TOKEN}), self.token(refresh=None),
        )
        services = self.services()
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("fixture").debug("capturing provider logs")
            results = []
            for message in ("temporarily unavailable", "could not be remembered"):
                started = await self.accounts.account_add()
                final = await self.final_status(started["authorization_id"])
                self.assertEqual(final["status"], "failed")
                self.assertIn(message, final["error"])
                self.assertNotIn("challenge", final)
                results.append(final)
        captured = out.getvalue() + err.getvalue() + "\n".join(logs.output) + json.dumps(results)
        for secret in (*_SECRETS, CODE, *(issued.token for issued in services.issued)):
            self.assertNotIn(secret, captured)
        self.assertFalse(self.path.exists())

    async def test_cancel_during_msal_poll_discards_late_approval_and_drains_client(self):
        entered, approved = threading.Event(), threading.Event()

        def late_approval():
            entered.set()
            if not approved.wait(5):
                raise RuntimeError("test never released the token poll")
            return self.token()

        requests, transports, _ = self.authority(late_approval)
        services = self.services()
        started = await self.accounts.account_add()
        authorization_id = started["authorization_id"]
        try:
            terminal = StringIO()
            self.assertTrue(_print_remember_notice(json.dumps(str(self.path), ensure_ascii=False), terminal=terminal))
            self.assertEqual(started["storage_notice"], terminal.getvalue().rstrip())
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            observed = await self.accounts.account_authorization_status(authorization_id)
            self.assertEqual(observed, {**started, "challenge": {"verification_uri": URI, "user_code": CODE}})
            self.assertEqual(await self.accounts.account_list(), [])
            cancelling = await self.accounts.account_authorization_cancel(authorization_id)
            self.assertEqual(cancelling, {"authorization_id": authorization_id, "status": "cancelling"})
            self.assertFalse(transports[0].closed)
            with self.assertRaisesRegex(AccountError, "already in progress"):
                await self.accounts.account_add()
        finally:
            approved.set()
        self.assertEqual(await self.final_status(authorization_id), {
            "authorization_id": authorization_id, "status": "cancelled",
        })
        self.assertTrue(transports[0].closed)
        self.assertEqual(sum(str(request.url) == DEVICE for request in requests), 1)
        self.assertEqual(services.calls, [])
        self.assertFalse(self.path.exists())


def _serve_eof_fixture(home):
    """Run a real MCP transport over stdio with the real provider against local responses."""
    from endpilot.mcp_server import MCPPlayer, create_server
    from endpilot.xbox import _cache

    connect = socket.socket.connect

    def connect_loopback(sock, address):
        # Windows asyncio opens a loopback TCP socket for its wakeup pipe.
        assert address[0] in ("127.0.0.1", "::1"), "unexpected socket connect"
        return connect(sock, address)

    fixture = _MsalTestCase()
    fixture.setUp()
    fixture.enterContext(patch.object(socket.socket, "connect", new=connect_loopback))
    fixture.enterContext(patch.dict(os.environ, {
        "HOME": home, "USERPROFILE": home, "LOCALAPPDATA": home, "XDG_STATE_HOME": home,
    }))
    pending = fixture.httpx.Response(400, json={"error": "authorization_pending"})
    _, clients, _ = fixture.authority(*([pending] * 20))
    original = MCPPlayer.lifespan

    async def seed_legacy_account():
        path = xbox.account_cache_path()
        async with _cache.locked_cache(path):
            _cache.save_cache(path, "stdio.legacy.fixture", json.dumps({"tag": _REFRESH}))

    @asynccontextmanager
    async def lifespan(player, server):
        async with original(player, server):
            yield
        evidence = {
            "clients_closed": bool(clients) and all(client.closed for client in clients),
            "cache_exists": xbox.account_cache_path().exists(),
        }
        (Path(home) / "eof.json").write_text(json.dumps(evidence), encoding="utf-8")

    try:
        asyncio.run(seed_legacy_account())
        with patch.object(MCPPlayer, "lifespan", lifespan):
            create_server().run(transport="stdio")
    finally:
        fixture.doCleanups()


@unittest.skipUnless(_HAS_XBOX and find_spec("mcp"), "requires the Xbox and MCP extras")
class MCPAccountStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_authorization_leaves_stdio_available_and_eof_closes_provider(self):
        from mcp.types import LATEST_PROTOCOL_VERSION

        home = self.enterContext(TemporaryDirectory())
        import endpilot

        source = Path(endpilot.__file__).resolve().parents[1]
        tests = Path(__file__).resolve().parent
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(source), str(tests)))}
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-c", "import sys; from test_mcp_accounts import _serve_eof_fixture; "
            "_serve_eof_fixture(sys.argv[1])", home, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        request_id = 0
        lines = []

        async def send(method, params, *, notification=False):
            nonlocal request_id
            request = {"jsonrpc": "2.0", "method": method, "params": params}
            if not notification:
                request_id += 1
                request["id"] = request_id
            child.stdin.write(json.dumps(request).encode() + b"\n")
            await child.stdin.drain()
            if notification:
                return None
            line = await asyncio.wait_for(child.stdout.readline(), 5)
            if not line:
                stderr = await asyncio.wait_for(child.stderr.read(), 5)
                self.fail(f"MCP fixture closed before responding: {stderr.decode()}")
            lines.append(line)
            response = json.loads(line)
            self.assertEqual(response["id"], request_id)
            return response["result"]

        async def call(name, **arguments):
            result = await send("tools/call", {"name": name, "arguments": arguments})
            self.assertFalse(result.get("isError", False), result)
            return result["structuredContent"]

        async def rejected(name, message, **arguments):
            result = await send("tools/call", {"name": name, "arguments": arguments})
            self.assertTrue(result["isError"])
            content, = result["content"]
            self.assertEqual(content["type"], "text")
            self.assertEqual(content["text"].removeprefix(f"Error executing tool {name}: "), message)

        try:
            await send("initialize", {
                "protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "account-tests", "version": "1"},
            })
            await send("notifications/initialized", {}, notification=True)
            legacy, = (await call("account_list"))["result"]
            self.assertIsNone(legacy["xuid"])
            self.assertIsNone(legacy["gamertag"])
            self.assertEqual(await call("account_forget", legacy_selector=legacy["legacy_selector"]),
                             {"forgotten": True, **legacy})
            await rejected("account_forget", "No matching legacy Xbox account. Use account_list.",
                           legacy_selector=legacy["legacy_selector"])
            await rejected("account_authorization_status",
                           "Unknown Xbox authorization ID. Use the ID returned by account_add.",
                           authorization_id=_REFRESH)
            pending = await call("account_add")
            self.assertEqual(pending["status"], "pending")
            self.assertIn("renewable sign-in credentials in plaintext", pending["storage_notice"])
            async with asyncio.timeout(5):
                while True:
                    status = await call("account_authorization_status", authorization_id=pending["authorization_id"])
                    if status.get("challenge"):
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(status["challenge"], {"verification_uri": URI, "user_code": CODE})
            self.assertEqual(status["storage_notice"], pending["storage_notice"])
            self.assertEqual(await call("account_list"), {"result": []})
            child.stdin.close()
            await child.stdin.wait_closed()
            tail_out, stderr = await asyncio.wait_for(child.communicate(), 8)
            self.assertEqual(child.returncode, 0, stderr.decode())
            self.assertEqual(json.loads((Path(home) / "eof.json").read_text(encoding="utf-8")), {
                "clients_closed": True, "cache_exists": False,
            })
            output = b"".join(lines) + tail_out + stderr
            for secret in (*_SECRETS, "stdio.legacy.fixture"):
                self.assertNotIn(secret.encode(), output)
            self.assertNotIn(CODE.encode(), tail_out + stderr)
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()
