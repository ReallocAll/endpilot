import asyncio
from contextlib import chdir, redirect_stderr, redirect_stdout
from importlib.util import find_spec
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import ANY, AsyncMock, patch

from endpilot import BedrockCredentials, LoginRefused, ResourcePackDecision, Skin, _cli_terminal
from endpilot import __main__ as cli
from endpilot.__main__ import main
from endpilot.authentication import OfflineIdentity
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession, request_network_settings
from endpilot.protocol import Protocol, write_batch
from test_join import FakeTransport
from test_skin import _png
from test_xbox_cache_storage import _msal_state
from test_xbox_device_flow import CODE, URI, public_key


class _CliTestCase(unittest.TestCase):
    def setUp(self):
        self.bot_factory = self.enterContext(patch("endpilot.Bot"))
        self.bot = self.bot_factory.return_value
        self.bot._skin = None
        self.bot.join_address = AsyncMock()
        self.bot.close = AsyncMock()
        self.stdout = self.enterContext(redirect_stdout(StringIO()))
        self.stderr = self.enterContext(redirect_stderr(StringIO()))


class GeneralCliTests(_CliTestCase):
    def test_join_forwards_the_original_address_and_join_settings(self):
        address = "https://play.example:8443/bedrock/path"
        pin = "a" * 64
        self.assertEqual(main(["join", address, "--hold", "0", "--accept-resource-packs",
                               "--server-key-sha256", pin]), 0)
        (original,), options = self.bot.join_address.await_args
        self.assertEqual(original, address)
        self.assertIsNone(options["credential_provider"])
        self.assertEqual(options["server_key_sha256"], pin)
        self.assertIs(options["on_resource_packs"](object()), ResourcePackDecision.ACCEPT)
        self.bot_factory.assert_called_once_with("EndPilot")
        self.bot.close.assert_awaited_once_with()
        self.assertEqual(self.stdout.getvalue().splitlines(), ["Joined.", "Disconnected."])

    def test_join_reports_login_refusal_and_closes_without_retrying(self):
        self.bot.join_address.side_effect = LoginRefused(
            "server rejected login: DisconnectPacket(reason=DisconnectFailReason.NOT_AUTHENTICATED, "
            "messages=reflected-credential)\n\x1b[31m",
        )
        self.assertEqual(main(["join", "play.example:19132", "--hold", "0"]), 1)
        self.bot.join_address.assert_awaited_once_with(
            "play.example:19132", credential_provider=None, server_key_sha256=None, on_resource_packs=None,
        )
        self.bot.close.assert_awaited_once_with()
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "Login refused by the server.\n")

    def test_protocol_refusals_preserve_sdk_error_text_and_use_fixed_cli_messages(self):
        protocol = Protocol(2225)
        b = protocol.binding
        details = b.DisconnectPacketMessages(
            message="fixture server allowlist denied this player\n reflected-credential '",
            filtered_message="another-reflected-credential",
        )
        allowlist = b.DisconnectPacket(reason=b.DisconnectFailReason.NOT_ALLOWED, messages=details)
        unauthenticated = b.DisconnectPacket(reason=b.DisconnectFailReason.NOT_AUTHENTICATED, messages=details)
        status = b.PlayStatus.LOGIN_FAILED_CLIENT_OLD
        version = b.PlayStatusPacket(status=status)
        unknown = b.PlayStatusPacket(status=12345)

        async def refuse(packet, *, during_settings):
            wire = FakeTransport()
            wire.received.put_nowait(write_batch([protocol.encode(packet)]))
            async with asyncio.timeout(1):
                if during_settings:
                    await request_network_settings(wire, protocol)
                else:
                    settings = b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE)
                    connection = PacketConnection(wire, protocol, settings)
                    await JoinSession(connection, OfflineIdentity("EndPilot"), "play.example:19132").join()

        for packet, during_settings, detail, expected in (
            (allowlist, False, f"server rejected login: {allowlist!r}",
             "Login refused. The server allowlist denied this player."),
            (unauthenticated, False, f"server rejected login: {unauthenticated!r}",
             "Login refused. The server rejected the player's identity."),
            (version, True, f"server refused network settings request: {version!r}",
             "Login refused. Client and server versions are incompatible."),
            (version, False, f"server refused login: {status}",
             "Login refused. Client and server versions are incompatible."),
            (unknown, False, "server refused login: 12345", "Login refused by the server."),
        ):
            with self.subTest(during_settings=during_settings, expected=expected), redirect_stderr(StringIO()) as output:
                with self.assertRaises(LoginRefused) as refused:
                    asyncio.run(refuse(packet, during_settings=during_settings))
                self.assertEqual(str(refused.exception), detail)
                self.assertEqual(refused.exception.args, (detail,))
                self.bot.join_address.side_effect = refused.exception
                self.assertEqual(main(["join", "play.example:19132", "--hold", "0"]), 1)
                self.assertEqual(output.getvalue(), expected + "\n")

    def test_join_timeout_has_a_fixed_message(self):
        self.bot.join_address.side_effect = TimeoutError("private server or account detail")
        self.assertEqual(main(["join", "play.example:19132", "--hold", "0"]), 1)
        self.assertEqual(self.stderr.getvalue(), "Join timed out.\n")
        self.bot.close.assert_awaited_once()

    @unittest.skipUnless(find_spec("nethernet"), "requires the NetherNet extra")
    def test_nethernet_identity_errors_have_fixed_messages(self):
        from nethernet import ConnectionFailed, InvalidIdentity, SessionError

        for error, expected in (
            (InvalidIdentity("private SDP details"), "NetherNet identity verification failed."),
            (ConnectionFailed(SessionError.IDENTITY_NOT_ALLOWED),
             "NetherNet connection refused. The player identity was rejected."),
            (ConnectionFailed(SessionError.NEGOTIATION_TIMEOUT_WAITING_FOR_ACCEPT),
             "NetherNet connection timed out during negotiation."),
        ):
            with self.subTest(expected=expected), redirect_stderr(StringIO()) as output:
                self.bot.join_address.side_effect = error
                self.assertEqual(main(["join", "https://play.example:19132", "--hold", "0"]), 1)
                self.assertEqual(output.getvalue(), expected + "\n")

    def test_invalid_join_options_fail_before_creating_a_bot(self):
        for options in (["--xuid", "123"], ["--name", "First Player"],
                        ["--xbox", "--xuid", "123", "--name", "First Player"],
                        ["--skin-model", "slim"], ["--skin", "skin.png", "--skin-model", "other"],
                        ["--hold", "nan"], ["--hold", "-1"],
                        ["--server-key-sha256", "not-a-digest"]):
            with self.subTest(options=options), self.assertRaises(SystemExit) as exited:
                main(["join", "play.example:19132", *options])
            self.assertEqual(exited.exception.code, 2)
        self.bot_factory.assert_not_called()

    def test_account_skin_selectors_are_mutually_exclusive(self):
        for command in (["set", "skin.png"], ["clear"]):
            with self.subTest(command=command), self.assertRaises(SystemExit) as exited:
                main(["account", "skin", *command, "--xuid", "123", "--name", "First Player"])
            self.assertEqual(exited.exception.code, 2)
        self.bot_factory.assert_not_called()

    @unittest.skipUnless(find_spec("PIL"), "requires the skin extra")
    def test_offline_join_passes_the_decoded_explicit_skin_to_both_transports(self):
        png = Path(self.enterContext(TemporaryDirectory())) / "skin.png"
        row = bytes(range(256))
        png.write_bytes(_png(64, 32, [row] * 32))
        for address, options, model in (
            ("raknet://play.example:19132", [], "wide"),
            ("http://play.example:19132", ["--skin-model", "slim"], "slim"),
        ):
            with self.subTest(address=address):
                self.bot_factory.reset_mock()
                self.assertEqual(main(["join", address, "--hold", "0", "--skin", str(png), *options]), 0)
                self.bot_factory.assert_called_once_with("EndPilot", skin=Skin(row * 32, model=model, height=32))

    def test_skin_decode_failures_have_fixed_messages_and_do_not_create_a_bot(self):
        for failure, message in (
            (ImportError("private module detail"), "PNG skins require Pillow. Install endpilot[skin]."),
            (OSError("private path"), "Could not load the skin PNG."),
            (ValueError("private parser detail"), "Could not load the skin PNG."),
        ):
            with (self.subTest(failure=type(failure)), redirect_stderr(StringIO()) as output,
                  patch.object(Skin, "from_png", side_effect=failure)):
                self.assertEqual(main(["join", "play.example:19132", "--skin", "private.png"]), 1)
                self.assertIn(message, output.getvalue())
                self.assertNotIn("private", output.getvalue())
        self.bot_factory.assert_not_called()

    def test_raknet_pin_is_rejected_before_creating_a_bot(self):
        with self.assertRaises(SystemExit) as exited:
            main(["join", "RAKNET://play.example:19132", "--xbox", "--server-key-sha256", "a" * 64])
        self.assertEqual(exited.exception.code, 2)
        self.assertEqual(self.stderr.getvalue(), "--server-key-sha256 requires a NetherNet address.\n")
        self.bot_factory.assert_not_called()

    def test_offline_entry_works_without_importing_the_xbox_package(self):
        src = Path(__file__).resolve().parents[1] / "src"
        probe = """
import runpy
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, sys.argv[1])

attempted = []

class NoExtras:
    def find_spec(self, fullname, *args):
        if fullname in {"endpilot.xbox", "httpx", "jwt", "msal", "nethernet"}:
            attempted.append(fullname)
            raise RuntimeError(f"unexpected import of {fullname}")

sys.meta_path.insert(0, NoExtras())
sys.argv = ["endpilot", "join", "raknet://play.example:19132", "--hold", "0"]
with patch("endpilot.Bot") as bot_factory:
    bot_factory.return_value.close = AsyncMock()
    for status, error in ((0, None), (1, ConnectionError("private context"))):
        bot_factory.return_value.join_address = AsyncMock(side_effect=error)
        try:
            runpy.run_module("endpilot", run_name="__main__", alter_sys=True)
        except SystemExit as result:
            assert result.code == status
assert not attempted, attempted
"""
        result = subprocess.run([sys.executable, "-B", "-c", probe, str(src)], capture_output=True, text=True,
                                timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["Joined.", "Disconnected."])
        self.assertEqual(result.stderr, "Connection failed. Check the server address, settings and transport dependencies.\n")


_HAS_STORAGE = os.name != "nt" or find_spec("win32security") is not None


@unittest.skipUnless(_HAS_STORAGE, "requires local Xbox account storage")
class XboxCliTests(_CliTestCase):
    def setUp(self):
        super().setUp()
        from endpilot import xbox
        from endpilot.xbox import _cache

        self.xbox = xbox
        self.cache = _cache
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.path = directory / "auth" / f"{xbox.DEFAULT_CLIENT_ID}.json"
        self.enterContext(patch.object(xbox, "account_cache_path", return_value=self.path))
        self.enterContext(patch.object(sys, "stdin", None))
        self.terminal = self.enterContext(patch.object(cli, "_device_terminal", return_value=None))
        self.key = public_key()
        self.credentials = BedrockCredentials(("certificate-one", "certificate-two"), "login-token")
        self.auth = self.enterContext(patch.object(xbox.XboxCredentialProvider, "_credentials_for_account",
                                                  autospec=True))
        self.auth.return_value = self.credentials, ("9007199254740993", "First Player"), False

        async def join(_address, *, credential_provider, **_options):
            self.assertIs(await credential_provider(self.key), self.credentials)

        self.bot.join_address.side_effect = join

    def save(self, xuid, gamertag, *, skin=None):
        account = f"test-{xuid}"
        state = _msal_state(account, client_id=self.xbox.DEFAULT_CLIENT_ID)

        async def write():
            async with self.cache.locked_cache(self.path):
                self.cache.upsert_account(self.path, self.cache.AccountRecord(
                    account, json.dumps(state), xuid, gamertag, skin=skin,
                ))

        asyncio.run(write())

    def save_legacy(self):
        async def write():
            async with self.cache.locked_cache(self.path):
                self.cache.save_cache(self.path, "legacy-account", "{}")

        asyncio.run(write())

    def test_first_join_shows_the_notice_and_continues_with_one_authorization(self):
        output = StringIO()
        self.terminal.return_value = output

        async def auth(provider, key, *, xuid, add):
            self.assertEqual((key, xuid, add), (self.key, None, False))
            provider._on_device_code(URI, CODE)
            return self.auth.return_value

        self.auth.side_effect = auth
        with patch.object(_cli_terminal, "_is_terminal", side_effect=lambda stream, **_: stream is output):
            self.assertEqual(main(["join", "play.example:19132", "--xbox", "--hold", "0"]), 0)

        self.auth.assert_awaited_once()
        self.bot.join_address.assert_awaited_once()
        terminal_text = output.getvalue()
        self.assertLess(terminal_text.index("plaintext"), terminal_text.index(CODE))
        self.assertIn(json.dumps(str(self.path), ensure_ascii=False), terminal_text)
        self.assertNotIn(CODE, self.stdout.getvalue() + self.stderr.getvalue())

    def test_add_uses_a_valid_public_key_and_never_creates_a_bot(self):
        from endpilot.authentication import _read_public_key

        self.terminal.return_value = StringIO()
        self.assertEqual(main(["account", "add"]), 0)
        (_, key), options = self.auth.await_args
        _read_public_key(key, "test public key")
        self.assertEqual(options, {"xuid": None, "add": True})
        self.assertIn("Remembered Xbox account First Player (9007199254740993).", self.stdout.getvalue())
        self.bot_factory.assert_not_called()

    def test_device_authorization_cancellation_closes_before_joining(self):
        self.terminal.return_value = StringIO()
        self.auth.side_effect = asyncio.CancelledError
        self.assertEqual(main(["join", "play.example:19132", "--xbox", "--hold", "0"]), 130)
        self.auth.assert_awaited_once()
        self.assertTrue(callable(self.auth.await_args.args[0]._on_device_code))
        self.bot.close.assert_awaited_once()
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertFalse(self.path.exists())

    def test_list_and_forget_use_local_storage_without_authorizing(self):
        self.save("9007199254740993", "First Player")
        self.save("9007199254740995", "Second Player")
        self.assertEqual(main(["account", "list"]), 0)
        self.assertEqual(self.stdout.getvalue().splitlines(), ["9007199254740993\tFirst Player",
                                                               "9007199254740995\tSecond Player"])
        self.assertEqual(main(["account", "forget", "--xuid", "9007199254740995"]), 0)
        self.assertEqual([record.xuid for record in self.cache.load_accounts(self.path)], ["9007199254740993"])
        self.assertEqual(main(["account", "forget"]), 0)
        self.assertFalse(self.path.exists())
        self.bot_factory.assert_not_called()
        self.auth.assert_not_awaited()

    def test_multi_account_joins_route_an_exact_xuid_from_script_or_chooser(self):
        self.save("9007199254740993", "First Player")
        self.save("9007199254740995", "Second Player")
        join = ["join", "play.example:19132", "--xbox", "--hold", "0"]
        self.terminal.return_value = StringIO()
        self.assertEqual(main([*join, "--xuid", "9007199254740995"]), 0)
        self.auth.assert_awaited_once_with(ANY, self.key, xuid="9007199254740995", add=False)
        self.assertIsNone(self.auth.await_args.args[0]._on_device_code)
        self.auth.reset_mock()

        with patch.object(sys, "stdin", StringIO("1\n")), patch.object(cli, "_can_read_terminal", return_value=True):
            self.assertEqual(main(join), 0)
        self.auth.assert_awaited_once_with(ANY, self.key, xuid="9007199254740993", add=False)
        self.assertIn("2. Second Player (9007199254740995)", self.terminal.return_value.getvalue())

    def test_name_selection_matches_the_whole_verified_gamertag_and_detects_ambiguity(self):
        self.save("9007199254740993", "Straße Player")
        self.save("9007199254740995", "Second Player")
        join = ["join", "play.example:19132", "--xbox", "--hold", "0", "--name"]
        self.assertEqual(main([*join, "STRASSE PLAYER"]), 0)
        self.auth.assert_awaited_once_with(ANY, self.key, xuid="9007199254740993", add=False)
        self.auth.reset_mock()
        self.bot_factory.reset_mock()

        self.assertEqual(main([*join, "STRASSE"]), 1)
        self.assertIn("No remembered Xbox account matches --name.", self.stderr.getvalue())
        self.save("9007199254740997", "Strasse Player")
        self.assertEqual(main([*join, "STRASSE PLAYER"]), 1)
        self.assertIn("Multiple remembered Xbox accounts match --name. Select one with --xuid.", self.stderr.getvalue())
        self.auth.assert_not_awaited()
        self.bot_factory.assert_not_called()

    @unittest.skipUnless(find_spec("PIL"), "requires the skin extra")
    def test_skin_set_validates_and_resolves_a_local_png_and_clear_removes_only_the_binding(self):
        self.save("9007199254740993", "First Player")
        self.save("9007199254740995", "Second Player")
        png = self.path.parent / "custom.png"
        original = self.cache.load_accounts(self.path)
        set_skin = ["account", "skin", "set", png.name, "--name", "second player", "--model", "slim"]
        with chdir(png.parent):
            png.write_bytes(b"invalid PNG")
            self.assertEqual(main(set_skin), 1)
            self.assertEqual(self.cache.load_accounts(self.path), original)
            png.write_bytes(_png(64, 32, [bytes(range(256))] * 32))
            self.assertEqual(main(set_skin), 0)
        first, second = self.cache.load_accounts(self.path)
        self.assertEqual(first, original[0])
        self.assertEqual(second.skin, self.cache.SkinBinding(str(png.resolve()), "slim"))
        self.assertEqual(main(["account", "skin", "clear", "--xuid", second.xuid]), 0)
        self.assertEqual(self.cache.load_accounts(self.path), original)
        self.assertTrue(png.is_file())
        self.auth.assert_not_awaited()
        self.bot_factory.assert_not_called()

    def test_skin_commands_require_a_verified_saved_account_without_authorizing(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), redirect_stderr(StringIO()) as output:
                if legacy:
                    self.save_legacy()
                self.assertEqual(main(["account", "skin", "set", "missing.png", "--name", "Player"]), 1)
                self.assertEqual(main(["account", "skin", "clear"]), 1)
                self.assertIn("No verified Xbox accounts are remembered. Run python -m endpilot account add.",
                              output.getvalue())
                self.assertEqual(self.path.exists(), legacy)
        self.auth.assert_not_awaited()
        self.bot_factory.assert_not_called()

    def test_sole_account_uses_its_saved_skin_after_the_profile_is_verified(self):
        png = self.path.parent / "remembered.png"
        row = bytes(range(256))
        skin = Skin(row * 32, height=32)
        with patch.object(Skin, "from_png", return_value=skin) as load:
            self.save("9007199254740993", "First Player")
            self.assertEqual(main(["account", "skin", "set", str(png)]), 0)
            record, = self.cache.load_accounts(self.path)
            self.assertEqual(record.skin, self.cache.SkinBinding(str(png.resolve()), "wide"))
            load.reset_mock()

            async def authorize(_provider, key, *, xuid, add):
                self.assertEqual((key, xuid, add), (self.key, None, False))
                load.assert_not_called()
                self.assertIsNone(self.bot._skin)
                return self.auth.return_value

            self.auth.side_effect = authorize
            self.assertEqual(main(["join", "play.example:19132", "--xbox", "--hold", "0"]), 0)
            load.assert_called_once_with(str(png.resolve()), model="wide")
        self.bot_factory.assert_called_once_with("EndPilot")
        self.assertIs(self.bot._skin, skin)

    @unittest.skipUnless(find_spec("PIL"), "requires the skin extra")
    def test_unusable_binding_is_only_loaded_for_the_matching_profile_without_an_override(self):
        path = self.path.parent / "missing.png"
        self.save("9007199254740993", "First Player", skin=self.cache.SkinBinding(str(path), "slim"))
        join = ["join", "play.example:19132", "--xbox", "--hold", "0"]
        self.auth.return_value = self.credentials, ("9007199254740995", "Different Player"), False
        self.assertEqual(main(join), 0)
        self.assertIsNone(self.bot._skin)

        self.auth.return_value = self.credentials, ("9007199254740993", "First Player"), False
        for contents in (None, b"invalid PNG"):
            with self.subTest(contents=contents), redirect_stderr(StringIO()) as output:
                if contents is not None:
                    path.write_bytes(contents)
                self.assertEqual(main(join), 1)
                self.assertIn("Could not load the saved Xbox skin. Use join --skin to override it,", output.getvalue())
                self.assertNotIn(str(path), output.getvalue())

        png = self.path.parent / "override.png"
        row = bytes(range(256))
        png.write_bytes(_png(64, 32, [row] * 32))
        self.bot_factory.reset_mock()
        self.assertEqual(main([*join, "--skin", str(png), "--skin-model", "wide"]), 0)
        self.bot_factory.assert_called_once_with("EndPilot", skin=Skin(row * 32, height=32))

    def test_no_terminal_or_choice_cancellation_stops_before_join_or_forget(self):
        self.save_legacy()
        self.save("9007199254740995", "Second Player")
        original = self.path.read_bytes()
        join = ["join", "play.example:19132", "--xbox", "--hold", "0"]
        self.assertEqual(main(join), 1)
        self.assertIn("--name or --xuid", self.stderr.getvalue())
        self.terminal.return_value = StringIO()
        with patch.object(sys, "stdin", StringIO("\n")), patch.object(cli, "_can_read_terminal", return_value=True):
            self.assertEqual(main(join), 130)
            self.assertEqual(main(["account", "forget"]), 130)
        self.assertEqual(self.path.read_bytes(), original)
        self.auth.assert_not_awaited()
        self.bot_factory.assert_not_called()

    def test_one_account_is_reused_silently_and_preserved_after_an_auth_failure(self):
        self.save("9007199254740993", "First Player")
        original = self.path.read_bytes()
        join = ["join", "play.example:19132", "--xbox", "--hold", "0"]
        self.assertEqual(main(join), 0)
        self.auth.assert_awaited_once_with(ANY, self.key, xuid=None, add=False)
        self.assertEqual(self.stderr.getvalue(), "")
        self.auth.side_effect = self.xbox.XboxAuthorizationUnavailable("private service data")
        self.assertEqual(main(join), 1)
        self.assertIn("temporarily unavailable", self.stderr.getvalue())
        self.assertNotIn("private service data", self.stderr.getvalue())
        self.assertEqual(self.path.read_bytes(), original)

    def test_sole_legacy_record_is_automatic_and_mixed_cache_offers_it_for_interactive_selection(self):
        self.save_legacy()
        join = ["join", "play.example:19132", "--xbox", "--hold", "0"]
        self.assertEqual(main(join), 0)
        self.auth.assert_awaited_once_with(ANY, self.key, xuid=None, add=False)

        self.auth.reset_mock()
        self.save("9007199254740995", "Second Player")
        self.assertEqual(main(["account", "list"]), 0)
        self.assertIn("Legacy Xbox account (XUID unavailable)", self.stdout.getvalue())
        self.assertNotIn("legacy-account", self.stdout.getvalue())
        self.assertEqual(main([*join, "--xuid", "legacy-account"]), 1)
        self.assertNotIn("legacy-account", self.stderr.getvalue())
        self.auth.assert_not_awaited()

        self.assertEqual(main([*join, "--xuid", "9007199254740995"]), 0)
        self.auth.assert_awaited_once_with(ANY, self.key, xuid="9007199254740995", add=False)
        self.auth.reset_mock()

        self.terminal.return_value = StringIO()
        with patch.object(sys, "stdin", StringIO("1\n")), patch.object(cli, "_can_read_terminal", return_value=True):
            self.assertEqual(main(join), 0)
        self.auth.assert_awaited_once_with(ANY, self.key, legacy_account_id="legacy-account")
        menu = self.terminal.return_value.getvalue()
        self.assertIn("1. Legacy Xbox account (XUID unavailable)", menu)
        self.assertIn("2. Second Player (9007199254740995)", menu)
        self.assertNotIn("legacy-account", menu)


@unittest.skipUnless(
    _HAS_STORAGE and all(find_spec(name) for name in ("httpx", "jwt", "msal", "raknet")),
    "requires the Xbox and RakNet extras",
)
class XboxCliProcessTests(unittest.TestCase):
    def run_join(self, phase, account_directory):
        tests = Path(__file__).resolve().parent
        fixture = tests / "fixtures" / "general_cli_account_child.py"
        env = dict(os.environ, LOCALAPPDATA=account_directory, XDG_STATE_HOME=account_directory,
                   HOME=account_directory, PYTHONPATH=os.pathsep.join((str(tests.parent / "src"), str(tests))))
        report = Path(account_directory) / f"{phase}.json"
        result = subprocess.run([sys.executable, "-B", str(fixture), phase, str(report)], env=env,
                                capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=55)
        self.assertTrue(report.is_file(), result.stdout + result.stderr)
        return result, json.loads(report.read_text(encoding="utf-8"))

    def test_first_join_is_remembered_and_a_new_process_refreshes_without_a_terminal(self):
        from test_xbox_provider_join import USER_CODE, wire

        with TemporaryDirectory() as account_directory:
            runs = []
            for phase in ("first", "cached"):
                result, observed = self.run_join(phase, account_directory)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["Joined.", "Disconnected."])
                self.assertEqual(result.stderr, "")
                self.assertNotIn(USER_CODE, result.stdout)
                self.assertEqual(observed["saved_accounts"], [[wire.XUID, wire.PROFILE]])
                self.assertTrue(observed["initialized"] and observed["disconnected"])
                self.assertEqual(len(observed["keys"]), 1)
                runs.append(observed)

        first, cached = runs
        self.assertEqual((first["device_grants"], first["refreshes"]), (1, 0))
        self.assertTrue(first["prompted"] and first["warned_before_prompt"])
        self.assertEqual((cached["device_grants"], cached["refreshes"]), (0, 1))
        self.assertFalse(cached["prompted"])
        self.assertNotEqual(first["keys"], cached["keys"])

    def test_selected_legacy_record_upgrades_after_silent_or_terminal_authorization(self):
        from test_xbox_provider_join import USER_CODE, wire

        for phase, device_grants in (("legacy", 0), ("expired", 1), ("unproven", 1)):
            with self.subTest(phase=phase), TemporaryDirectory() as account_directory:
                result, observed = self.run_join(phase, account_directory)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["Joined.", "Disconnected."])
                self.assertEqual(result.stderr, "")
                self.assertNotIn(USER_CODE, result.stdout)
                self.assertTrue(observed["listed_legacy"])
                self.assertTrue(observed["other_account_unchanged"])
                self.assertFalse(observed["showed_microsoft_account"])
                self.assertEqual((observed["device_grants"], observed["refreshes"]), (device_grants, 1))
                self.assertEqual(observed["prompted"], bool(device_grants))
                self.assertEqual(observed["warned_before_prompt"], bool(device_grants))
                self.assertEqual(observed["saved_accounts"], [[wire.XUID, wire.PROFILE], ["987654321", "Another Player"]])
                self.assertTrue(observed["initialized"] and observed["disconnected"])
                self.assertEqual(observed["connections"], 1)
                self.assertEqual(len(observed["keys"]), 1)

    def test_selected_legacy_record_requires_authorization_when_the_terminal_is_unavailable(self):
        with TemporaryDirectory() as account_directory:
            result, observed = self.run_join("unproven-no-terminal", account_directory)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("Xbox authorization is required.", result.stderr)
        self.assertTrue(observed["listed_legacy"] and observed["cache_unchanged"])
        self.assertFalse(observed["prompted"])
        self.assertEqual((observed["device_grants"], observed["refreshes"], observed["connections"]), (0, 1, 0))


if __name__ == "__main__":
    unittest.main()
