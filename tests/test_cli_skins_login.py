"""Verify CLI skin selection in signed login packets over RakNet loopback."""

import asyncio
import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from importlib.util import find_spec
from io import StringIO
import json
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from uuid import UUID

from cryptography.hazmat.primitives import serialization

from endpilot import __main__ as cli, _cli_terminal
import test_online_join as wire
from test_skin import SLIM, WIDE, _png, _texture
from test_xbox_cache_storage import _HAS_STORAGE, _msal_state
from test_xbox_provider_join import HttpIssuers, OAUTH_ENDPOINT, RETAIL_ISSUER, USER_CODE


KNIGHT = ("knight.tenant", "9007199254740993", "Knight")
OTHER = ("other.tenant", "9007199254740995", "Other Player")
DEFAULT_SKIN = (64, 32, "wide", bytes((0, 0, 0, 255)) * 64 * 32)


class LoginCapture:
    def __init__(self, services=None):
        self.services = services
        self.identity = None
        self.client = None

    def verify_login(self, request, address):
        auth_size, = struct.unpack_from("<i", request)
        auth_end = 4 + auth_size
        client_size, = struct.unpack_from("<i", request, auth_end)
        assert auth_size > 0 and client_size == len(request) - auth_end - 4
        auth = json.loads(request[4:auth_end])
        token = auth["Token"]
        if self.services is None:
            assert auth["AuthenticationType"] == 2
            unverified = json.loads(wire.unurl(token.split(".")[1]))
            public = serialization.load_der_public_key(base64.b64decode(unverified["cpk"], validate=True))
            identity = wire.claims_from(token, public)
            wire.verify_validity(identity)
        else:
            assert auth["AuthenticationType"] == 0
            issued, = self.services.issued
            assert token == issued.token
            identity = self.services.verify_multiplayer_token(token)
            public = issued.public
            assert identity["cpk"] == wire.der64(public)
        self.client = wire.claims_from(request[auth_end + 4:].decode(), public)
        assert self.client["ServerAddress"] == address
        assert self.client["ThirdPartyName"] == identity["xname"]
        self.identity = (identity.get("xid"), identity["xname"])
        return SimpleNamespace(spki=wire.der64(public))


@unittest.skipUnless(find_spec("PIL") and find_spec("raknet"), "requires the skin and RakNet extras")
class CliSkinLoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        home = self.enterContext(TemporaryDirectory())
        self.home = Path(home)
        self.enterContext(patch.dict(os.environ, {
            "HOME": home, "USERPROFILE": home, "LOCALAPPDATA": home, "XDG_STATE_HOME": home,
        }))

    async def run_cli(self, *argv, answer=None):
        out, err = StringIO(), StringIO()
        terminal = StringIO() if answer is not None else None
        with (
            redirect_stdout(out), redirect_stderr(err),
            patch.object(sys, "stdin", StringIO(answer or "")),
            patch.object(cli, "_can_read_terminal", return_value=terminal is not None),
            patch.object(cli, "_device_terminal", return_value=terminal),
            patch.object(_cli_terminal, "_is_terminal", side_effect=lambda stream, **_: (
                terminal is not None and stream is terminal
            )),
        ):
            status = await asyncio.to_thread(cli.main, list(argv))
        return status, out.getvalue(), err.getvalue(), terminal.getvalue() if terminal else ""

    def make_skin(self, seed, width, height, model):
        rows = _texture(seed, width, height)
        path = self.home / f"skin {seed}.png"
        path.write_bytes(_png(width, height, rows))
        return path, (width, height, model, b"".join(rows))

    async def assert_login(self, identity, expected_skin, *options, services=None, answer=None):
        capture = LoginCapture(services)
        # Refuse only after receiving the signed login so the CLI exits before spawn.
        async with wire.LocalServer(capture, "raknet", reject=True).listen() as peer:
            status, out, err, terminal = await self.run_cli(
                "join", f"raknet://{peer.address}", "--hold", "0", *options, answer=answer,
            )
            if peer.failure is not None:
                raise peer.failure
            self.assertEqual((status, out, err.strip()), (1, "", "Login refused by the server."))
            self.assertTrue(peer.login_seen.is_set())
            self.assertEqual(len(peer.login_keys), 1)
        self.assertEqual(capture.identity, identity)
        width, height, model, rgba = expected_skin
        client = capture.client
        self.assertEqual((client["SkinImageWidth"], client["SkinImageHeight"]), (width, height))
        self.assertEqual(client["ArmSize"], model)
        geometry = SLIM if model == "slim" else WIDE
        self.assertEqual(json.loads(base64.b64decode(client["SkinResourcePatch"], validate=True)),
                         {"geometry": {"default": geometry}})
        self.assertEqual(base64.b64decode(client["SkinData"], validate=True), rgba)
        return terminal

    async def seed_account(self, account):
        from msal import SerializableTokenCache

        from endpilot import xbox
        from endpilot.xbox import _cache

        home_id, xuid, name = account
        path = xbox.account_cache_path()
        state = _msal_state(home_id, client_id=xbox.DEFAULT_CLIENT_ID, refresh_token=f"refresh-{home_id}")
        cache = SerializableTokenCache()
        for kind, entries in state.items():
            for entry in entries.values():
                cache.modify(kind, entry, dict(entry, credential_type=kind))
        async with _cache.locked_cache(path):
            _cache.upsert_account(path, _cache.AccountRecord(home_id, cache.serialize(), xuid, name))

    async def account_skin(self, *arguments):
        status, out, err, _ = await self.run_cli("account", "skin", *arguments)
        self.assertEqual(status, 0, out + err)

    async def assert_xbox_login(self, account, skin, *arguments, answer=None, expired=None):
        import httpx
        from endpilot.xbox import _verify

        home_id, xuid, name = account
        services = HttpIssuers()
        refreshes = []

        def oauth(_transport, request):
            if str(request.url) != f"{OAUTH_ENDPOINT}/token":
                return services.oauth(request)
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                refreshes.extend(form["refresh_token"])
                if expired:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                # Make every saved-account join reveal which refresh token it selected.
                token = {"access_token": "local-msal-access", "expires_in": 0, "scope": "XboxLive.signin"}
            else:
                token = services.oauth(request).json()
            uid, utid = home_id.split(".")
            client_info = wire.url64(json.dumps({"uid": uid, "utid": utid}).encode())
            return httpx.Response(200, json=dict(token, refresh_token=f"refresh-{home_id}", client_info=client_info))

        async def exchange(_transport, request):
            return await services.exchange(request)

        uuid_bytes = hashlib.md5(f"pocket-auth-1-xuid:{xuid}".encode(), usedforsecurity=False).digest()
        with (
            patch.object(httpx.HTTPTransport, "handle_request", new=oauth),
            patch.object(httpx.AsyncHTTPTransport, "handle_async_request", new=exchange),
            patch.object(_verify, "_MOJANG_ROOT", wire.der64(services.root.public_key())),
            patch.multiple(wire, XUID=xuid, PROFILE=name, PLAYER_UUID=UUID(bytes=uuid_bytes, version=3),
                           ISSUER=RETAIL_ISSUER),
        ):
            terminal = await self.assert_login((xuid, name), skin, "--xbox", *arguments,
                                               services=services, answer=answer)
        self.assertEqual(refreshes, [f"refresh-{expired or home_id}"])
        self.assertEqual(services.calls.count("device-grant"), int(expired is not None))
        return terminal

    async def test_offline_join_sends_explicit_png_once(self):
        path, skin = self.make_skin(11, 128, 128, "slim")
        await self.assert_login((None, "EndPilot"), skin, "--skin", str(path), "--skin-model", "slim")
        await self.assert_login((None, "EndPilot"), DEFAULT_SKIN)

    @unittest.skipUnless(_HAS_STORAGE, "requires the Xbox extra")
    async def test_saved_selection_override_and_clear(self):
        bound_path, bound = self.make_skin(17, 64, 64, "slim")
        other_path, other = self.make_skin(23, 64, 32, "wide")
        temporary_path, temporary = self.make_skin(29, 128, 128, "wide")

        await self.seed_account(KNIGHT)
        await self.account_skin("set", str(bound_path), "--name", "Knight", "--model", "slim")
        await self.assert_xbox_login(KNIGHT, bound)

        await self.seed_account(OTHER)
        await self.account_skin("set", str(other_path), "--xuid", OTHER[1])
        menu = await self.assert_xbox_login(OTHER, other, answer="2\n")
        self.assertIn("1. Knight", menu)
        self.assertIn("2. Other Player", menu)
        await self.assert_xbox_login(KNIGHT, temporary, "--name", "kNiGhT", "--skin", str(temporary_path))
        await self.assert_xbox_login(KNIGHT, bound, "--xuid", KNIGHT[1])

        await self.account_skin("clear", "--name", "KNIGHT")
        await self.assert_xbox_login(KNIGHT, DEFAULT_SKIN, "--name", "Knight")
        await self.assert_xbox_login(OTHER, other, "--xuid", OTHER[1])

    @unittest.skipUnless(_HAS_STORAGE, "requires the Xbox extra")
    async def test_sole_saved_skin_does_not_follow_a_different_device_account(self):
        bound_path, bound = self.make_skin(31, 64, 64, "slim")
        await self.seed_account(KNIGHT)
        await self.account_skin("set", str(bound_path), "--name", "Knight", "--model", "slim")

        prompt = await self.assert_xbox_login(OTHER, DEFAULT_SKIN, answer="", expired=KNIGHT[0])
        self.assertIn(USER_CODE, prompt)
        await self.assert_xbox_login(KNIGHT, bound, "--xuid", KNIGHT[1])
