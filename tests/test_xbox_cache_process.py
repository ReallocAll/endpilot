"""Exercise sustained Xbox sign-in across independent CLI and SDK processes."""

import asyncio
import base64
from collections import Counter, defaultdict
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from importlib.util import find_spec
from ipaddress import ip_address
import json
import os
from pathlib import Path
import runpy
import signal
import stat
import struct
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.asymmetric import ec
import endpilot

import test_online_join as wire
from test_xbox_provider_join import (
    CLIENT_ID, DEVICE_CODE, JWKS, MS_TENANT, RETAIL_ISSUER, USER_CODE, USER_HASH, USER_TOKEN, HttpIssuers,
)


OTHER_CLIENT_ID = "2aa7dbda-e5a8-4e83-8b1c-38666f28bb42"
OTHER_APP_PROBE = "--fixture-other-application"
OTHER_APP_REAUTH = 23
SDK_TRANSIENT_PROBE = "--fixture-sdk-transient"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
LOCK_WAITING = b"fixture-lock-contended\n"
LOCK_ACQUIRED = b"fixture-lock-acquired\n"
PROCESS_PID = b"fixture-process-pid:"
ONE_TIME_NOTICE = b"Xbox account was not remembered. Continuing with this sign-in only."
INTERRUPT_STATUS = 499


def join_args(address):
    return ("join", f"raknet://{address}", "--xbox", "--hold", "0")


@dataclass(frozen=True, slots=True)
class RunningCLI:
    process: asyncio.subprocess.Process
    pid: int


class CacheIssuers(HttpIssuers):
    def __init__(self, expires_in, *, renewable=True):
        super().__init__()
        self.expires_in = expires_in
        self.renewable = renewable
        self.serial = 0
        self.expires_at = 0
        self.access_token = None
        self.refresh_token = None
        self.calls_by_pid = defaultdict(Counter)
        self.hold_next_refresh = False
        self.refresh_entered = asyncio.Event()
        self.release_refresh = asyncio.Event()

    def oauth(self, request):
        import httpx

        if request.method == "GET" or not request.url.path.endswith("/token"):
            return super().oauth(request)
        form = parse_qs(request.content.decode())
        app, = form["client_id"]
        assert app == CLIENT_ID
        grant, = form["grant_type"]
        if grant == DEVICE_GRANT:
            assert form.get("device_code") == [DEVICE_CODE]
            self.calls.append("device-redemption")
        elif grant == "refresh_token":
            assert self.refresh_token is not None and form.get("refresh_token") == [self.refresh_token]
            self.calls.append("refresh")
        else:
            raise AssertionError("unexpected OAuth grant")
        self.serial += 1
        self.access_token = f"fixture-access-{self.serial}"
        self.refresh_token = f"fixture-refresh-{self.serial}"
        self.expires_at = time.time() + self.expires_in
        now = int(time.time())
        identity = {"aud": app, "iss": MS_TENANT + "/v2.0", "sub": "fixture-person", "oid": "fixture-person",
                    "preferred_username": "fixture@example.invalid", "iat": now, "exp": now + 3600}
        reply = {
            "token_type": "Bearer", "access_token": self.access_token,
            "expires_in": self.expires_in, "scope": "XboxLive.signin",
            "client_info": wire.url64(json.dumps({"uid": "fixture-person", "utid": "fixture-tenant"}).encode()),
            "id_token": wire.ec_token(self.root, identity),
        }
        if self.renewable:
            reply["refresh_token"] = self.refresh_token
        return httpx.Response(200, json=reply)

    async def exchange(self, request):
        if request.url.host != "user.auth.xboxlive.com":
            return await super().exchange(request)
        import httpx

        properties = json.loads(request.content)["Properties"]
        assert properties["RpsTicket"] == f"d={self.access_token}"
        self.verify_xbox_proof(request, properties["ProofKey"])
        self.calls.append("xbox-user")
        return httpx.Response(200, json={
            "Token": USER_TOKEN, "NotAfter": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "DisplayClaims": {"xui": [{"uhs": USER_HASH}]},
        })


@asynccontextmanager
async def issuers_http(services):
    import httpx
    from aiohttp import web

    async def relay(request):
        packet = await request.json()
        upstream = httpx.Request(packet["method"], packet["url"], headers=packet["headers"],
                                 content=base64.b64decode(packet["body"], validate=True))
        if (services.hold_next_refresh and upstream.url.host == "login.microsoftonline.com"
                and upstream.url.path.endswith("/token")
                and parse_qs(upstream.content.decode()).get("grant_type") == ["refresh_token"]):
            services.hold_next_refresh = False
            services.refresh_entered.set()
            await services.release_refresh.wait()
        before = len(services.calls)
        try:
            result = services.oauth(upstream) if upstream.url.host == "login.microsoftonline.com" else await services.exchange(upstream)
        except (AssertionError, KeyError, ValueError):
            services.calls.append("fixture-rejected")
            return web.json_response({}, status=400)
        finally:
            services.calls_by_pid[packet["pid"]].update(services.calls[before:])
        return web.Response(status=result.status_code, body=result.content, content_type="application/json")

    app = web.Application()
    app.router.add_post("/", relay)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}/"
    finally:
        await runner.cleanup()


@contextmanager
def pause_verification(services, *, cancel=False):
    waiting, release = asyncio.Event(), asyncio.Event()
    exchange = services.exchange

    async def pause_at_jwks(request):
        if str(request.url) == JWKS and not waiting.is_set():
            waiting.set()
            await release.wait()
            if cancel:
                import httpx

                return httpx.Response(INTERRUPT_STATUS)
        return await exchange(request)

    with patch.object(services, "exchange", new=pause_at_jwks):
        try:
            yield waiting, release
        finally:
            release.set()


class CLIGame(wire.LocalServer):
    """Real RakNet game peer, using the CLI's default locale and view distance."""

    def validate_login(self, data):
        auth_len, = struct.unpack_from("<i", data)
        client_offset = auth_len + 4
        auth = json.loads(data[4:client_offset])
        client_len, = struct.unpack_from("<i", data, client_offset)
        assert client_len == len(data) - client_offset - 4
        claims = self.authority.verify_multiplayer_token(auth["Token"])
        issued = self.authority.sessions[claims["cpk"]]
        assert auth["AuthenticationType"] == 0 and auth["Token"] == issued.token
        bridge, first, last = json.loads(auth["Certificate"])["chain"]
        assert (first, last) == issued.chain
        assert wire.claims_from(bridge, issued.public)["identityPublicKey"] == wire.der64(self.authority.root.public_key())
        client = wire.claims_from(data[client_offset + 4:].decode(), issued.public)
        assert (client["ThirdPartyName"], client["ServerAddress"], client["LanguageCode"], client["MaxViewDistance"]) == (
            wire.PROFILE, self.address, "en_US", 8,
        )
        return issued

    async def handle(self, connection):
        self.connections += 1
        peer = wire.BedrockPeer(connection, True)
        b = peer.protocol.binding
        try:
            settings = await peer.expect(b.RequestNetworkSettingsPacket)
            assert settings.client_network_version == 2225
            await peer.send(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
            login = await peer.expect(b.LoginPacket)
            assert login.client_network_version == 2225
            issued = self.validate_login(login.connection_request)
            self.login_keys.append(issued.spki)
            key = ec.generate_private_key(ec.SECP384R1())
            salt = b"cache-process-loopback"
            await peer.send(b.ServerToClientHandshakePacket(token=wire.ec_token(key, {"salt": base64.b64encode(salt).decode()})))
            peer.enable_cipher(sha256(salt + key.exchange(ec.ECDH(), issued.public)).digest())
            await peer.expect(b.ClientToServerHandshakePacket)
            self.verified_ciphertext = peer.received == 1
            response = b.ResourcePackClientResponsePacket
            await peer.send(b.PlayStatusPacket(status=b.PlayStatus.LOGIN_SUCCESS), b.ResourcePacksInfoPacket())
            assert isinstance((await peer.expect(response)).response, response.DownloadingFinished)
            await peer.send(b.ResourcePackStackPacket())
            assert isinstance((await peer.expect(response)).response, response.ResourcePackStackFinished)
            x, y, z = wire.SPAWN
            await peer.send(b.StartGamePacket(runtime_id=wire.RUNTIME_ID, entity_id=wire.ACTOR_ID, pos=b.Vec3(x=x, y=y, z=z)))
            radius = await peer.expect(b.RequestChunkRadiusPacket)
            assert radius.chunk_radius == 8
            await peer.send(b.ChunkRadiusUpdatedPacket(chunk_radius=8), b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
            loading = b.ServerboundLoadingScreenPacketType
            for phase in (loading.START_LOADING_SCREEN, loading.END_LOADING_SCREEN):
                packet = await peer.expect(b.ServerboundLoadingScreenPacket)
                assert packet.serverbound_loading_screen_packet_type == phase
            initialized = await peer.expect(b.SetLocalPlayerAsInitializedPacket)
            assert initialized.player_id == wire.RUNTIME_ID
            self.initialized.set()
            await self.wait_for_close(connection)
        except Exception as error:
            self.failure = error
            self.disconnected.set()


async def probe_other_application(address):
    from endpilot.xbox import XboxCredentialProvider, XboxReauthenticationRequired

    host, port = address.rsplit(":", 1)
    assert ip_address(host).is_loopback
    bot = endpilot.Bot("OtherApplication")
    try:
        await bot.join_raknet(host, int(port), credential_provider=XboxCredentialProvider(OTHER_CLIENT_ID))
    except XboxReauthenticationRequired:
        return OTHER_APP_REAUTH
    finally:
        await bot.close()
    raise AssertionError("the other application joined with the default application's account")


async def probe_sdk_transient(address):
    from endpilot import _cli_terminal
    from endpilot.xbox import XboxCredentialProvider

    with ExitStack() as owned:
        terminal = _cli_terminal._device_terminal(owned)
        assert terminal is not None
        provider = XboxCredentialProvider(on_device_code=partial(_cli_terminal._show_device_code, terminal))
        bot = endpilot.Bot("EndPilot")
        try:
            await bot.join_address(f"raknet://{address}", credential_provider=provider)
        finally:
            await bot.close()
    return 0


def child(relay, signing_root, interactive, observe_wait, cli_args):
    import httpx
    from endpilot import _cli_terminal
    from endpilot.xbox import _verify

    assert sys.stdin.read(1) == "" and not sys.stderr.isatty()
    sys.stdout.buffer.write(PROCESS_PID + str(os.getpid()).encode("ascii") + b"\n")
    sys.stdout.flush()

    def check_loopback(event, args):
        if event in ("socket.connect", "socket.sendto", "socket.getaddrinfo"):
            key = args[0] if event == "socket.getaddrinfo" else args[1]
            host = key[0] if isinstance(key, tuple) else key
            if isinstance(host, bytes):
                host = host.decode("ascii")
            if not ip_address(host).is_loopback:
                raise PermissionError("external address in the CLI cache fixture")

    sys.addaudithook(check_loopback)
    sync = httpx.HTTPTransport.handle_request
    asynchronous = httpx.AsyncHTTPTransport.handle_async_request

    def forwarded(request):
        return httpx.Request("POST", relay, json={
            "method": request.method, "url": str(request.url), "headers": dict(request.headers),
            "body": base64.b64encode(request.content).decode(), "pid": os.getpid(),
        })

    def oauth(transport, request):
        return sync(transport, forwarded(request))

    async def exchange(transport, request):
        response = await asynchronous(transport, forwarded(request))
        if response.status_code == INTERRUPT_STATUS:
            await response.aclose()
            # Interrupt the running command while it owns the application lock.
            signal.raise_signal(signal.SIGINT)
            await asyncio.Future()
        return response

    # Simulate terminal detection with the real stderr pipe as the output stream.
    terminal = nullcontext()
    if interactive:
        terminal = patch.object(_cli_terminal, "_is_terminal", side_effect=lambda stream, **_: stream is sys.stderr)
    if observe_wait:
        from endpilot.xbox import _cache

        lock_code = _cache._try_lock.__code__
        contended = False

        def observe_lock(frame, event, result):
            nonlocal contended
            if event != "return" or frame.f_code is not lock_code:
                return
            if result is False and not contended:
                contended = True
                sys.stdout.buffer.write(LOCK_WAITING)
                sys.stdout.flush()
            elif result is True and contended:
                sys.setprofile(None)
                sys.stdout.buffer.write(LOCK_ACQUIRED)
                sys.stdout.flush()

        # Report genuine OS lock results without wrapping the cache call.
        sys.setprofile(observe_lock)
    with (
        patch.object(sys, "argv", ["endpilot", *cli_args]),
        terminal,
        patch.object(_cli_terminal, "_open_verification_page", return_value=None),
        patch.object(httpx.HTTPTransport, "handle_request", new=oauth),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", new=exchange),
        patch.object(_verify, "_MOJANG_ROOT", signing_root),
    ):
        if cli_args[:1] == [OTHER_APP_PROBE]:
            _, address = cli_args
            return asyncio.run(probe_other_application(address), debug=False)
        if cli_args[:1] == [SDK_TRANSIENT_PROBE]:
            _, address = cli_args
            return asyncio.run(probe_sdk_transient(address), debug=False)
        runpy.run_module("endpilot", run_name="__main__", alter_sys=True)
        raise AssertionError("the command did not exit")


@unittest.skipUnless(all(find_spec(p) for p in ("msal", "httpx", "jwt", "aiohttp", "raknet")),
                     "install the xbox, raknet and aiohttp extras")
class XboxCacheProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def record(self, label, pid, status, counts, signature=None, *, prompts=0, tty=False, stderr_visible=False,
               lock_contended=False):
        report = os.getenv("ENDPILOT_XBOX_CACHE_PROCESS_REPORT")
        if report:
            row = {"case": self._testMethodName, "phase": label, "pid": pid, "exit": status, "calls": dict(counts),
                   "game_spki_sha256": signature, "device_prompts": prompts, "player_tty": tty,
                   "stderr_visible": stderr_visible, "stdin_closed": True, "lock_contended": lock_contended}
            with Path(report).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")

    @asynccontextmanager
    async def spawn_cli(self, home, services, relay, *args, interactive=False, observe_wait=False,
                        stderr=asyncio.subprocess.PIPE):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ENDPILOT_", "MSAL_", "XBOX_"))
               and not k.upper().endswith("_PROXY")}
        env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "local"),
                   APPDATA=str(home / "roaming"), XDG_STATE_HOME=str(home / "state"),
                   PYTHONPATH=str(Path(endpilot.__file__).resolve().parents[1]))
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", __file__, "--cache-child", relay, wire.der64(services.root.public_key()),
            str(int(interactive)), str(int(observe_wait)), *args,
            env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=stderr,
        )
        try:
            marker = await asyncio.wait_for(process.stdout.readline(), 8)
            pid = marker.removeprefix(PROCESS_PID).strip()
            self.assertTrue(marker.startswith(PROCESS_PID) and pid.isdigit(), "the Python worker did not report its PID")
            yield RunningCLI(process, int(pid))
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def collect_cli(self, running):
        stdout, stderr = await asyncio.wait_for(running.process.communicate(), timeout=12)
        self.assertNotIn(ONE_TIME_NOTICE, stderr)
        return (running.pid, running.process.returncode, stdout.count(b"Joined."), stdout.count(b"Disconnected."),
                stderr.count(USER_CODE.encode()), bool(stderr))

    async def run_cli(self, home, services, relay, *args, interactive=False):
        async with self.spawn_cli(home, services, relay, *args, interactive=interactive) as process:
            return await self.collect_cli(process)

    async def require_lock_marker(self, running, marker):
        observed = await asyncio.wait_for(running.process.stdout.readline(), 8)
        if not observed:
            observed = await asyncio.wait_for(running.process.stderr.read(), 8)
        self.assertEqual(observed, marker)

    async def require_lock_wait(self, running):
        await self.require_lock_marker(running, LOCK_WAITING)
        self.assertIsNone(running.process.returncode)

    async def game_result(self, peer, result, *, interactive=False):
        _, status, joined, disconnected, _, visible = result
        self.assertEqual((status, joined, disconnected), (0, 1, 1))
        if not interactive:
            self.assertFalse(visible, "silent login wrote to the player terminal")
        return await self.game_key(peer)

    async def game_key(self, peer):
        await peer.wait(peer.disconnected)
        self.assertTrue(peer.initialized.is_set() and peer.verified_ciphertext)
        self.assertEqual(peer.connections, 1)
        self.assertEqual(len(peer.login_keys), 1)
        return sha256(base64.b64decode(peer.login_keys[0])).hexdigest()

    async def join(self, home, services, relay, *, label, interactive=False, expect=True):
        start = len(services.calls)
        async with CLIGame(services, "raknet").listen() as peer:
            with patch.object(wire, "ISSUER", RETAIL_ISSUER):
                result = await self.run_cli(
                    home, services, relay, *join_args(peer.address), interactive=interactive,
                )
            pid, status, joined, disconnected, prompts, visible = result
            self.assertFalse("fixture-rejected" in services.calls[start:], "a local issuer rejected the fixture request")
            if expect:
                key_digest = await self.game_result(peer, result, interactive=interactive)
            else:
                self.assertEqual((status, joined, disconnected, peer.connections), (1, 0, 0, 0))
                self.assertTrue(visible)
                key_digest = None
        calls = Counter(services.calls[start:])
        self.record(label, pid, status, calls, key_digest, prompts=prompts, tty=interactive, stderr_visible=visible)
        return calls, prompts, key_digest

    async def seed_expired_session(self, home, services, relay):
        calls, prompts, key = await self.join(home, services, relay, label="remember", interactive=True)
        self.assertEqual((calls["device-grant"], calls["device-redemption"], prompts), (1, 1, 1))
        await asyncio.sleep(max(0, services.expires_at - time.time()) + .05)
        services.expires_in = 3600
        cache, = home.rglob(CLIENT_ID + ".json")
        return cache, key

    @asynccontextmanager
    async def paused_refresh(self, home, services, relay):
        services.hold_next_refresh = True
        with patch.object(wire, "ISSUER", RETAIL_ISSUER):
            async with CLIGame(services, "raknet").listen() as peer:
                async with self.spawn_cli(home, services, relay, *join_args(peer.address)) as process:
                    try:
                        await asyncio.wait_for(services.refresh_entered.wait(), 8)
                        yield peer, process
                    finally:
                        services.release_refresh.set()

    async def record_silent_game(self, label, services, peer, result, refreshes, *, contended=False):
        pid, status, _, _, prompts, visible = result
        key = await self.game_result(peer, result)
        calls = services.calls_by_pid[pid]
        self.assertEqual((calls["refresh"], calls["device-grant"], calls["device-redemption"], prompts),
                         (refreshes, 0, 0, 0))
        self.assertEqual(calls["fixture-rejected"], 0)
        self.record(label, pid, status, calls, key, prompts=prompts, stderr_visible=visible, lock_contended=contended)
        return key

    async def exercise_first_authorization(self, *, cancel):
        with TemporaryDirectory(prefix="endpilot-xbox-user-") as directory, patch.object(wire, "ISSUER", RETAIL_ISSUER):
            home = Path(directory)
            services = CacheIssuers(3600)
            with pause_verification(services, cancel=cancel) as (waiting, release):
                async with (
                    issuers_http(services) as relay,
                    CLIGame(services, "raknet").listen() as first_peer,
                    CLIGame(services, "raknet").listen() as second_peer,
                    self.spawn_cli(home, services, relay, *join_args(first_peer.address), interactive=True) as first,
                ):
                    await asyncio.wait_for(waiting.wait(), 8)
                    lock, = home.rglob(CLIENT_ID + ".lock")
                    cache = lock.with_suffix(".json")
                    self.assertFalse(cache.exists())
                    async with self.spawn_cli(
                        home, services, relay, *join_args(second_peer.address), observe_wait=True,
                    ) as second:
                        await self.require_lock_wait(second)
                        self.assertIsNone(first.process.returncode)
                        self.assertFalse(services.calls_by_pid[second.pid])
                        self.assertFalse(cache.exists())
                        release.set()
                        await self.require_lock_marker(second, LOCK_ACQUIRED)
                        first_result, second_result = await asyncio.gather(self.collect_cli(first), self.collect_cli(second))
                    _, status, joined, disconnected, prompts, visible = first_result
                    calls = services.calls_by_pid[first.pid]
                    self.assertEqual((calls["device-grant"], calls["device-redemption"], calls["refresh"]), (1, 1, 0))
                    self.assertEqual(calls["fixture-rejected"], 0)
                    self.assertEqual(prompts, 1)
                    self.assertTrue(visible)
                    if cancel:
                        self.assertEqual((status, joined, disconnected), (130, 0, 0))
                        self.assertEqual(second_result[1:], (1, 0, 0, 0, True))
                        self.assertEqual((first_peer.connections, second_peer.connections), (0, 0))
                        self.assertFalse(services.calls_by_pid[second.pid])
                        self.assertFalse(cache.exists())
                        self.assertFalse(list(cache.parent.glob(f".{cache.name}.*.tmp")))
                    else:
                        first_key = await self.game_result(first_peer, first_result, interactive=True)
                        second_key = await self.record_silent_game(
                            "join-after-first-authorization", services, second_peer, second_result, 0, contended=True,
                        )
                        self.assertNotEqual(first_key, second_key)
                        self.assertTrue(cache.is_file())

    async def test_headless_join_waits_for_first_authorization(self):
        await self.exercise_first_authorization(cancel=False)

    async def test_headless_join_waits_for_cancelled_first_authorization(self):
        await self.exercise_first_authorization(cancel=True)

    async def test_concurrent_logins_share_one_refresh(self):
        with TemporaryDirectory(prefix="endpilot-xbox-user-") as directory:
            home = Path(directory)
            services = CacheIssuers(1)
            async with issuers_http(services) as relay:
                _, seed_key = await self.seed_expired_session(home, services, relay)
                async with self.paused_refresh(home, services, relay) as (first_peer, first):
                    async with CLIGame(services, "raknet").listen() as second_peer:
                        async with self.spawn_cli(home, services, relay, *join_args(second_peer.address),
                                                  observe_wait=True) as second:
                            await self.require_lock_wait(second)
                            self.assertFalse(services.calls_by_pid[second.pid])
                            self.assertIsNone(first.process.returncode)
                            services.release_refresh.set()
                            await self.require_lock_marker(second, LOCK_ACQUIRED)
                            first_result, second_result = await asyncio.gather(self.collect_cli(first), self.collect_cli(second))
                            first_key = await self.record_silent_game("parallel-refresh", services, first_peer, first_result, 1)
                            second_key = await self.record_silent_game("parallel-cache", services, second_peer, second_result, 0,
                                                                       contended=True)
                self.assertEqual(len({seed_key, first_key, second_key}), 3)

    async def test_forget_waits_for_refresh_and_old_process_cannot_restore_cache(self):
        with TemporaryDirectory(prefix="endpilot-xbox-user-") as directory:
            home = Path(directory)
            services = CacheIssuers(1)
            async with issuers_http(services) as relay:
                cache, seed_key = await self.seed_expired_session(home, services, relay)
                async with self.paused_refresh(home, services, relay) as (peer, first):
                    async with self.spawn_cli(
                        home, services, relay, "account", "forget", "--xuid", wire.XUID, observe_wait=True,
                    ) as forget:
                        await self.require_lock_wait(forget)
                        self.assertTrue(cache.is_file())
                        self.assertIsNone(first.process.returncode)
                        services.release_refresh.set()
                        await self.require_lock_marker(forget, LOCK_ACQUIRED)
                        first_result, forget_result = await asyncio.gather(self.collect_cli(first), self.collect_cli(forget))
                        key = await self.record_silent_game("refresh-before-forget", services, peer, first_result, 1)
                        self.assertNotEqual(key, seed_key)
                        pid, status, joined, disconnected, prompts, visible = forget_result
                        self.assertEqual((status, joined, disconnected, prompts), (0, 0, 0, 0))
                        self.assertFalse(visible)
                        self.assertFalse(services.calls_by_pid[pid])
                        self.record("contended-forget", pid, status, {}, lock_contended=True, stderr_visible=visible)
                self.assertFalse(cache.exists())
                calls, prompts, _ = await self.join(home, services, relay, label="after-contended-forget", expect=False)
                self.assertFalse(calls)
                self.assertEqual(prompts, 0)
                self.assertFalse(cache.exists())
                self.assertFalse(list(cache.parent.glob(f".{cache.name}.*.tmp")))

    async def exercise(self, *, expires_in, refreshes):
        with TemporaryDirectory(prefix="endpilot-xbox-user-") as directory:
            home = Path(directory)
            services = CacheIssuers(expires_in)
            async with issuers_http(services) as relay:
                calls, prompts, first_key = await self.join(home, services, relay, label="remember", interactive=True)
                self.assertEqual((calls["device-grant"], calls["device-redemption"], calls["refresh"], prompts), (1, 1, 0, 1))
                cache, = list(home.rglob(CLIENT_ID + ".json"))
                if os.name == "nt":
                    relative = Path("local/EndPilot/auth")
                elif sys.platform == "darwin":
                    relative = Path("Library/Application Support/EndPilot/auth")
                else:
                    relative = Path("state/endpilot/auth")
                self.assertEqual(cache.parent, home / relative)
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(cache.parent.stat().st_mode), 0o700)
                material = cache.read_bytes()
                for secret in (USER_CODE, DEVICE_CODE, *services.issued[0].chain, services.issued[0].token):
                    self.assertTrue(secret.encode() not in material, "game or device-flow material was persisted")
                keys = {first_key}
                for attempt in range(max(1, refreshes)):
                    if refreshes:
                        await asyncio.sleep(max(0, services.expires_at - time.time()) + .05)
                    label = f"silent-refresh-{attempt + 1}" if refreshes else "silent-access"
                    calls, prompts, key = await self.join(home, services, relay, label=label)
                    self.assertEqual((calls["device-grant"], calls["device-redemption"], calls["refresh"], prompts),
                                     (0, 0, int(bool(refreshes)), 0))
                    self.assertNotIn(key, keys)
                    keys.add(key)
                if not refreshes:
                    await self.assert_app_isolation_and_forget(home, cache, services, relay)

    async def assert_app_isolation_and_forget(self, home, cache, services, relay):
        material = cache.read_bytes()
        start = len(services.calls)
        async with CLIGame(services, "raknet").listen() as peer:
            pid, status, joined, disconnected, prompts, visible = await self.run_cli(
                home, services, relay, OTHER_APP_PROBE, peer.address,
            )
            self.assertEqual((status, joined, disconnected, prompts, peer.connections), (OTHER_APP_REAUTH, 0, 0, 0, 0))
        self.assertEqual(len(services.calls), start)
        self.assertTrue(cache.read_bytes() == material, "the other application changed the default application cache")
        self.assertFalse(list(home.rglob(OTHER_CLIENT_ID + ".json")))
        self.record("different-app", pid, status, {}, prompts=prompts, stderr_visible=visible)
        start = len(services.calls)
        pid, status, joined, disconnected, prompts, visible = await self.run_cli(home, services, relay, "account", "forget")
        self.assertEqual((status, joined, disconnected, prompts), (0, 0, 0, 0))
        self.assertFalse(visible)
        self.assertEqual(len(services.calls), start)
        self.assertFalse(cache.exists())
        self.record("forget", pid, status, {}, prompts=prompts, stderr_visible=visible)
        calls, prompts, _ = await self.join(home, services, relay, label="after-forget", expect=False)
        self.assertFalse(calls)
        self.assertEqual(prompts, 0)

    async def test_fresh_process_uses_remembered_access_token_without_tty(self):
        await self.exercise(expires_in=3600, refreshes=0)

    async def test_fresh_process_refreshes_and_next_process_uses_rotated_token(self):
        await self.exercise(expires_in=1, refreshes=2)

    async def test_interactive_default_does_not_persist(self):
        # The SDK's default provider supports transient authorization.
        with TemporaryDirectory(prefix="endpilot-xbox-user-") as directory, patch.object(wire, "ISSUER", RETAIL_ISSUER):
            home = Path(directory)
            services = CacheIssuers(3600)
            async with issuers_http(services) as relay, CLIGame(services, "raknet").listen() as peer:
                pid, status, joined, disconnected, prompts, visible = await self.run_cli(
                    home, services, relay, SDK_TRANSIENT_PROBE, peer.address, interactive=True,
                )
                self.assertEqual((status, joined, disconnected, prompts), (0, 0, 0, 1))
                key = await self.game_key(peer)
            calls = Counter(services.calls)
            self.assertEqual((calls["device-grant"], calls["device-redemption"], calls["refresh"]), (1, 1, 0))
            self.assertEqual(calls["fixture-rejected"], 0)
            self.assertFalse(list(home.rglob("*.json")))
            self.record("default", pid, status, calls, key, prompts=prompts, tty=True, stderr_visible=visible)

    @asynccontextmanager
    async def one_time_join(self):
        services = CacheIssuers(3600, renewable=False)
        with TemporaryDirectory(prefix="endpilot-xbox-user-") as directory, patch.object(wire, "ISSUER", RETAIL_ISSUER):
            home = Path(directory)
            async with issuers_http(services) as relay, CLIGame(services, "raknet").listen() as peer:
                yield home, services, relay, peer
                await self.game_key(peer)
                self.assertEqual(services.chain_requests, peer.login_keys)
                self.assertEqual(services.token_requests, peer.login_keys)
            self.assertFalse(list(home.rglob("*.json")))
        self.assertEqual(services.calls.count("device-redemption"), 1)
        self.assertEqual(services.calls.count("device-grant"), 1)
        self.assertEqual(services.calls.count("bedrock-jwks"), 1)

    async def test_remember_without_refresh_token_spawns_and_warns_once(self):
        async with self.one_time_join() as (home, services, relay, peer):
            async with self.spawn_cli(
                home, services, relay, *join_args(peer.address),
                interactive=True,
            ) as running:
                stdout, stderr = await asyncio.wait_for(running.process.communicate(), timeout=12)
                self.assertEqual(running.process.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(stdout.splitlines(), [b"Joined.", b"Disconnected."])
        self.assertEqual(stderr.splitlines().count(ONE_TIME_NOTICE), 1)
        self.assertEqual(stderr.count(USER_CODE.encode()), 1)
        self.assertLess(stderr.index(b"renewable sign-in credentials in plaintext"), stderr.index(USER_CODE.encode()))

    async def test_remember_without_refresh_token_spawns_after_stderr_disconnects(self):
        async with self.one_time_join() as (home, services, relay, peer):
            reader_fd, writer_fd = os.pipe()
            with os.fdopen(reader_fd, "rb", buffering=0) as reader, os.fdopen(writer_fd, "wb", buffering=0) as writer:
                with pause_verification(services) as (verification_waiting, release_verification):
                    async with self.spawn_cli(
                        home, services, relay, *join_args(peer.address),
                        interactive=True, stderr=writer,
                    ) as running:
                        writer.close()
                        try:
                            await asyncio.wait_for(verification_waiting.wait(), 8)
                            os.set_blocking(reader.fileno(), False)
                            before = reader.read(4096) or b""
                            self.assertIn(b"renewable sign-in credentials in plaintext", before)
                            self.assertEqual(before.count(USER_CODE.encode()), 1)
                            self.assertLess(
                                before.index(b"renewable sign-in credentials in plaintext"), before.index(USER_CODE.encode()),
                            )
                            self.assertNotIn(ONE_TIME_NOTICE, before)
                        finally:
                            reader.close()
                            release_verification.set()
                        stdout, _ = await asyncio.wait_for(running.process.communicate(), timeout=12)
                        self.assertEqual(stdout.splitlines(), [b"Joined.", b"Disconnected."])
                        self.assertEqual(running.process.returncode, 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cache-child":
        _, _, relay, root, interactive, observe_wait, *cli_args = sys.argv
        raise SystemExit(child(relay, root, interactive == "1", observe_wait == "1", cli_args))
    unittest.main()
