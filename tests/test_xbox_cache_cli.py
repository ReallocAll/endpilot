import asyncio
from collections import deque
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from errno import ENOTTY
import hashlib
from importlib.util import find_spec
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch
from uuid import UUID

from endpilot import BedrockCredentials, Bot, LoginRefused, _cli_terminal, xbox
from endpilot.protocol import read_batch, write_batch
from endpilot import __main__ as cli
from endpilot.xbox import XboxCredentialProvider, _cache
from test_xbox_cache_storage import _msal_state
from test_xbox_device_flow import CODE, URI, public_key


CLIENT_ID = "01234567-abcd-4321-8100-0123456789ab"
SERVER = "raknet://127.0.0.1:19132"
PROFILE = ("9007199254740993", "Fixture Player")
HAS_STORAGE = os.name != "nt" or find_spec("win32security") is not None
HAS_XBOX = HAS_STORAGE and find_spec("msal") and find_spec("httpx")


class Terminal(StringIO):
    def __init__(self, interactive):
        super().__init__()
        self.interactive = interactive
        self.writes = []
        self.flushes = 0

    def isatty(self):
        return self.interactive

    def write(self, text):
        self.writes.append(text)
        return super().write(text)

    def flush(self):
        self.flushes += 1
        return super().flush()


class TerminalEnterTests(unittest.TestCase):
    def test_windows_discards_typeahead_from_the_stdin_console_handle(self):
        stdin = Mock()
        stdin.fileno.return_value = 17
        console = SimpleNamespace(get_osfhandle=Mock(return_value=0x1234))
        flush = Mock(side_effect=[1, 0])
        ctypes_loader = SimpleNamespace(kernel32=SimpleNamespace(FlushConsoleInputBuffer=flush))
        with (
            patch.object(_cli_terminal.sys, "platform", "win32"),
            patch.dict(sys.modules, {"msvcrt": console}),
            patch("ctypes.windll", ctypes_loader, create=True),
        ):
            self.assertTrue(_cli_terminal._discard_typeahead(stdin))
            self.assertFalse(_cli_terminal._discard_typeahead(stdin))
        self.assertEqual(console.get_osfhandle.call_args_list, [call(17), call(17)])
        self.assertEqual([args[0].value for args, _ in flush.call_args_list], [0x1234, 0x1234])

    def test_posix_discards_only_queued_input(self):
        stdin = Mock()
        stdin.fileno.return_value = 17
        flush = Mock()
        terminal = SimpleNamespace(error=OSError, tcflush=flush, TCIFLUSH=0)
        with (
            patch.object(_cli_terminal.sys, "platform", "linux"),
            patch.dict(sys.modules, {"termios": terminal}),
            patch.object(os, "getpgrp", return_value=21, create=True),
            patch.object(os, "tcgetpgrp", return_value=20, create=True) as foreground,
        ):
            self.assertFalse(_cli_terminal._discard_typeahead(stdin))
            flush.assert_not_called()
            foreground.return_value = 21
            self.assertTrue(_cli_terminal._discard_typeahead(stdin))
            flush.assert_called_once_with(17, terminal.TCIFLUSH)
            flush.side_effect = OSError
            self.assertFalse(_cli_terminal._discard_typeahead(stdin))

    def test_windows_reads_only_when_a_console_key_is_ready(self):
        console = SimpleNamespace(kbhit=Mock(return_value=False), getwch=Mock(side_effect=["x", "\r"]))
        with patch.object(_cli_terminal.sys, "platform", "win32"), patch.dict(sys.modules, {"msvcrt": console}):
            self.assertFalse(_cli_terminal._enter_pressed(None))
            console.getwch.assert_not_called()
            console.kbhit.return_value = True
            self.assertFalse(_cli_terminal._enter_pressed(None))
            self.assertTrue(_cli_terminal._enter_pressed(None))

    def test_posix_reads_one_character_only_after_readiness(self):
        stdin = Mock()
        stdin.fileno.return_value = 17
        with (
            patch.object(_cli_terminal.sys, "platform", "linux"),
            patch.object(_cli_terminal.select, "select", return_value=([], [], [])) as readiness,
            patch.object(_cli_terminal.os, "read", side_effect=[b"x", b"\n", b""]) as read,
            patch.object(os, "getpgrp", return_value=21, create=True),
            patch.object(os, "tcgetpgrp", return_value=20, create=True) as foreground,
        ):
            with self.assertRaises(PermissionError):
                _cli_terminal._enter_pressed(stdin)
            readiness.assert_not_called()
            read.assert_not_called()
            foreground.return_value = 21
            self.assertFalse(_cli_terminal._enter_pressed(stdin))
            readiness.assert_called_once_with((17,), (), (), 0)
            read.assert_not_called()
            readiness.return_value = ([17], [], [])
            self.assertFalse(_cli_terminal._enter_pressed(stdin))
            self.assertTrue(_cli_terminal._enter_pressed(stdin))
            with self.assertRaises(EOFError):
                _cli_terminal._enter_pressed(stdin)
            self.assertEqual(read.call_args_list, [call(17, 1)] * 3)

    def test_posix_background_prompt_offers_manual_entry_without_flushing_or_polling(self):
        stdin = Terminal(True)
        stdin.fileno = Mock(return_value=0)
        output = Terminal(True)
        termios = SimpleNamespace(TCIFLUSH=0, tcflush=Mock(), error=OSError)
        with (
            ExitStack() as prompt,
            patch.object(sys, "platform", "linux"),
            patch.object(sys, "stdin", stdin),
            patch.object(_cli_terminal, "_is_terminal", return_value=True),
            patch.dict(sys.modules, {"termios": termios}),
            patch.object(os, "getpgrp", return_value=21, create=True),
            patch.object(os, "tcgetpgrp", return_value=20, create=True),
            patch.object(_cli_terminal.asyncio, "create_task") as schedule,
        ):
            prompt.callback(xbox._device_prompt_scope.reset, xbox._device_prompt_scope.set((prompt, float("inf"))))
            _cli_terminal._show_device_code(output, URI, CODE)
        self.assertIn(CODE, output.getvalue())
        self.assertIn(f"Visit {URI} on another device", output.getvalue())
        self.assertNotIn("Press Enter", output.getvalue())
        termios.tcflush.assert_not_called()
        schedule.assert_not_called()


class TerminalDetectionTests(unittest.TestCase):
    def test_linux_rejects_both_pty_master_device_types_for_input_and_output(self):
        stream = Mock()
        stream.fileno.return_value = 17
        fcntl = SimpleNamespace(F_GETFL=3, fcntl=Mock(return_value=os.O_RDWR))
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict(sys.modules, {"fcntl": fcntl}),
            patch.object(os, "isatty", return_value=True),
            patch.object(os, "fstat") as stat,
            patch.object(os, "major", side_effect=lambda dev: dev[0], create=True),
            patch.object(os, "makedev", side_effect=lambda major, minor: (major, minor), create=True),
            patch.object(os, "O_ACCMODE", 3, create=True),
            patch.object(os, "tcgetpgrp", side_effect=OSError(ENOTTY, "no controlling tty"), create=True),
            patch.dict(sys.modules, {"termios": SimpleNamespace()}),
        ):
            for device, expected in (((5, 2), False), ((2, 0), False), ((136, 0), True), ((5, 0), True)):
                for writable in (False, True):
                    with self.subTest(device=device, writable=writable):
                        stat.return_value = SimpleNamespace(st_rdev=device)
                        self.assertEqual(_cli_terminal._is_terminal(stream, writable=writable), expected)

    def test_darwin_uses_the_master_only_ioctl_for_input_and_output(self):
        stream = Mock()
        stream.fileno.return_value = 17
        fcntl = SimpleNamespace(F_GETFL=3, fcntl=Mock(return_value=os.O_RDWR), ioctl=Mock())
        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict(sys.modules, {"fcntl": fcntl}),
            patch.object(os, "isatty", return_value=True),
            patch.object(os, "O_ACCMODE", 3, create=True),
            patch.object(os, "tcgetpgrp", side_effect=OSError(ENOTTY, "no controlling tty"), create=True),
            patch.dict(sys.modules, {"termios": SimpleNamespace()}),
        ):
            for writable in (False, True):
                with self.subTest(writable=writable):
                    fcntl.ioctl.side_effect = [bytes(128), OSError(ENOTTY, "not a master")]
                    self.assertFalse(_cli_terminal._is_terminal(stream, writable=writable))
                    self.assertTrue(_cli_terminal._is_terminal(stream, writable=writable))
                    fcntl.ioctl.assert_called_with(17, 0x40807453, bytes(128))

    def test_posix_tostop_blocks_the_background_job_but_allows_foreground_writes(self):
        fcntl = SimpleNamespace(F_GETFL=3, fcntl=Mock(return_value=os.O_RDWR))
        termios = SimpleNamespace(TOSTOP=0x100, tcgetattr=Mock(), error=OSError)
        for group, attrs, writable in ((21, 0x100, True), (20, 0, True), (20, 0x100, False)):
            with self.subTest(group=group, attrs=attrs), ExitStack() as owned:
                output = Terminal(True)
                output.fileno = Mock(return_value=2)
                termios.tcgetattr.return_value = (0, 0, 0, attrs)
                with (
                    patch.dict(sys.modules, {"fcntl": fcntl, "termios": termios}),
                    patch.object(sys, "platform", "linux"),
                    patch.object(sys, "stderr", output),
                    patch.object(sys, "stdout", None),
                    patch.object(sys, "stdin", None),
                    patch.object(os, "isatty", return_value=True),
                    patch.object(_cli_terminal, "_is_pty_master", return_value=False),
                    patch.object(os, "O_ACCMODE", 3, create=True),
                    patch.object(os, "getpgrp", return_value=21, create=True),
                    patch.object(os, "tcgetpgrp", return_value=group, create=True),
                ):
                    self.assertIs(_cli_terminal._device_terminal(owned), output if writable else None)
                    _cli_terminal._print_status("status", file=output)
                self.assertEqual(output.getvalue(), "status\n" if writable else "")


class XboxCacheCliTests(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(TemporaryDirectory())
        self.path = Path(self.home) / "auth" / f"{xbox.DEFAULT_CLIENT_ID}.json"
        self.path_lookup = self.enterContext(patch.object(xbox, "account_cache_path", return_value=self.path))
        self.provider = self.enterContext(patch.object(xbox, "XboxCredentialProvider"))
        self.key = public_key()
        self.credentials = BedrockCredentials(("fixture-one", "fixture-two"), "fixture-bedrock-token")
        self.auth = AsyncMock(return_value=(self.credentials, PROFILE, False))
        self.provider.return_value._credentials_for_account = self.auth
        self.enterContext(patch.object(
            _cli_terminal, "_is_terminal", side_effect=lambda stream, **_: stream is not None and stream.isatty(),
        ))
        self.browser = self.enterContext(patch.object(_cli_terminal.webbrowser, "open"))
        self.keyboard = self.enterContext(patch.object(_cli_terminal, "_enter_pressed", return_value=False))
        self.discard_typeahead = self.enterContext(patch.object(_cli_terminal, "_discard_typeahead", return_value=True))
        self.bot_factory = self.enterContext(patch("endpilot.Bot"))
        self.bot = self.bot_factory.return_value
        self.bot.join_address = AsyncMock(side_effect=self.authorize_join)
        self.bot.close = AsyncMock()

    def invoke(self, *arguments, interactive=False, stdout_terminal=False, stdin=None, stale_client_id=None):
        self.stdout = Terminal(stdout_terminal)
        self.stderr = Terminal(interactive)
        home = Path(self.home)
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, LOCALAPPDATA=str(home / "local"),
                   APPDATA=str(home / "roaming"), XDG_STATE_HOME=str(home / "state"))
        env.pop("ENDPILOT_XBOX_CLIENT_ID", None)
        if stale_client_id is not None:
            env["ENDPILOT_XBOX_CLIENT_ID"] = stale_client_id
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(sys, "stdin", stdin),
            redirect_stdout(self.stdout),
            redirect_stderr(self.stderr),
        ):
            try:
                return cli.main(list(arguments))
            except SystemExit as error:
                return error.code

    def invoke_join(self, address=SERVER, *options, **terminal):
        return self.invoke("join", address, "--xbox", "--hold", "0", *options, **terminal)

    async def authorize_join(self, _address, *, credential_provider, **_options):
        self.assertEqual(await credential_provider(self.key), self.credentials)

    def remember_notice(self):
        path = json.dumps(str(self.path), ensure_ascii=False)
        return (f"Remembering this account stores renewable sign-in credentials in plaintext at {path}. "
                "Anyone who can read the file may reuse the authorization.\n")

    def mock_device_authority(self, verification_uri, user_code, *, lifetime=30):
        self.provider.side_effect = XboxCredentialProvider
        app = Mock()
        app.get_accounts.return_value = []
        app.initiate_device_flow.return_value = {
            "verification_uri": verification_uri, "user_code": user_code, "device_code": "device-fixture",
            "interval": 1, "expires_in": lifetime,
        }
        self.enterContext(patch.object(xbox, "_microsoft_app", AsyncMock(return_value=app)))
        self.exchange = AsyncMock(return_value=self.credentials)
        self.enterContext(patch.object(xbox, "_exchange", SimpleNamespace(exchange=self.exchange), create=True))
        self.enterContext(patch.object(xbox, "_verify", SimpleNamespace(
            verify_account=AsyncMock(return_value=PROFILE),
        ), create=True))
        return app

    def seed_remembered_account(self):
        account = "fixture-user.fixture-tenant"
        record = _cache.AccountRecord(account, json.dumps(_msal_state(account, client_id=xbox.DEFAULT_CLIENT_ID)),
                                      *PROFILE)

        async def save():
            async with _cache.locked_cache(self.path):
                _cache.upsert_account(self.path, record)

        asyncio.run(save())

    @unittest.skipUnless(HAS_STORAGE, "requires local Xbox account storage")
    def test_forget_twice_without_server_uses_the_default_application(self):
        self.seed_remembered_account()
        self.assertEqual(self.invoke("account", "forget", stale_client_id=CLIENT_ID), 0)
        self.assertEqual(self.stdout.getvalue(), f"Forgot {PROFILE[1]} ({PROFILE[0]}).\n")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.invoke("account", "forget", stale_client_id=CLIENT_ID), 0)
        self.assertEqual(self.stdout.getvalue(), "No remembered Xbox accounts.\n")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.path_lookup.call_args_list, [call(), call()])
        self.provider.assert_not_called()
        self.bot_factory.assert_not_called()
        self.browser.assert_not_called()

    def test_join_warns_on_the_device_code_terminal_before_finishing_authorization(self):
        for stderr_mode, stdout_mode, terminal_mode, target in (
            ("tty", "tty", "tty", "stderr"),
            ("pipe", "tty", "tty", "stdout"),
            ("blocked", "other-tty", "blocked", "stdout"),
            ("blocked", "pipe", "blocked", None),
            ("broken", "tty", "tty", "stdout"),
            ("closed", "closed", "tty", "terminal"),
            ("blocked", "blocked", "blocked", None),
        ):
            with self.subTest(stderr=stderr_mode, stdout=stdout_mode, terminal=terminal_mode):
                modes = {0: terminal_mode, 1: stdout_mode, 2: stderr_mode, 9: terminal_mode}

                def make_stream(descriptor):
                    mode = modes[descriptor]
                    stream = Terminal(mode in {"tty", "other-tty", "blocked"})
                    stream.fileno = Mock(return_value=descriptor)
                    if mode == "closed":
                        stream.close()
                    elif mode == "broken":
                        stream.write = Mock(side_effect=BrokenPipeError)
                    return stream

                stdin = make_stream(0)
                streams = {"stderr": make_stream(2), "stdout": make_stream(1), "terminal": make_stream(9)}
                tostop = 0x100

                def terminal_group(descriptor):
                    mode = modes[descriptor]
                    if mode in {"pipe", "broken", "other-tty"}:
                        raise OSError(ENOTTY, "no controlling tty")
                    return 20 if mode == "blocked" else 21

                def is_terminal(stream, *, writable=False):
                    return stream.isatty() and not (writable and modes[stream.fileno()] == "blocked")

                termios = SimpleNamespace(
                    TOSTOP=tostop, error=OSError,
                    tcgetattr=lambda fd: (0, 0, 0, tostop if modes[fd] == "blocked" else 0),
                )
                during_authorization = {}

                async def authorize(_key, *, xuid, add):
                    self.assertIsNone(xuid)
                    self.assertFalse(add)
                    show_code = self.provider.call_args.kwargs["on_device_code"]
                    self.assertTrue(callable(show_code))
                    show_code(URI, CODE)
                    during_authorization.update((name, ("".join(stream.writes), stream.flushes))
                                                for name, stream in streams.items())
                    return self.auth.return_value

                self.provider.reset_mock()
                self.auth.side_effect = authorize
                self.bot.join_address.reset_mock()
                with (
                    patch.object(sys, "platform", "linux"),
                    patch.object(sys, "stdin", stdin),
                    patch.object(sys, "stderr", streams["stderr"]),
                    patch.object(sys, "stdout", streams["stdout"]),
                    patch.object(os, "getpgrp", return_value=21, create=True),
                    patch.object(os, "tcgetpgrp", side_effect=terminal_group, create=True),
                    patch.dict(sys.modules, {"termios": termios}),
                    patch.object(_cli_terminal, "_is_terminal", side_effect=is_terminal),
                    patch.object(_cli_terminal, "open", return_value=streams["terminal"], create=True),
                ):
                    status = cli.main(["join", SERVER, "--xbox", "--hold", "0"])
                self.bot.join_address.assert_awaited_once()
                if target is None:
                    self.assertEqual(status, 1)
                    self.provider.assert_not_called()
                    self.auth.assert_not_awaited()
                    self.assertFalse(self.path.parent.exists())
                    self.assertTrue(all(not stream.writes for stream in streams.values()))
                else:
                    self.assertEqual(status, 0)
                    self.auth.assert_awaited_once()
                    warning, flushes = during_authorization[target]
                    self.assertTrue(warning.startswith(self.remember_notice()))
                    self.assertIn(f"  {CODE}\n", warning)
                    self.assertGreaterEqual(flushes, 2)
                    self.assertTrue(all(not content for name, (content, _) in during_authorization.items()
                                        if name != target))

    @unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
    def test_prefers_stderr_and_borrows_the_selected_standard_stream(self):
        for stderr_terminal in (True, False):
            with self.subTest(stderr_terminal=stderr_terminal):
                app = self.mock_device_authority(URI, CODE)
                app.acquire_token_by_device_flow.return_value = {"access_token": "fixture-access-token"}
                result = self.invoke_join(SERVER, interactive=stderr_terminal, stdout_terminal=True)
                self.assertEqual(result, 0)
                selected = self.stderr if stderr_terminal else self.stdout
                other = self.stdout if stderr_terminal else self.stderr
                self.assertIn(f"  {CODE}\n", selected.getvalue())
                self.assertNotIn(CODE, other.getvalue())
                self.assertFalse(selected.closed)
                self.assertFalse(other.closed)
        self.keyboard.assert_not_called()
        self.browser.assert_not_called()

    def test_owned_terminal_is_closed_on_success_cancellation_and_failure(self):
        stdin = Terminal(True)
        stdin.fileno = Mock(return_value=0)
        for error, status in ((None, 0), (asyncio.CancelledError(), 130), (RuntimeError(), 1)):
            with self.subTest(status=status):
                terminal = Terminal(True)
                terminal.fileno = Mock(return_value=9)

                async def authorize(*args, **kwargs):
                    self.assertFalse(terminal.closed)
                    if error is not None:
                        raise error
                    return self.auth.return_value

                self.auth.side_effect = authorize
                with (
                    patch.object(_cli_terminal, "open", return_value=terminal, create=True) as open_terminal,
                    patch.object(os, "tcgetpgrp", return_value=1, create=True),
                ):
                    self.assertEqual(self.invoke_join(stdin=stdin), status)
                self.assertTrue(terminal.closed)
                self.assertFalse(stdin.closed)
                self.assertTrue(callable(self.provider.call_args.kwargs["on_device_code"]))
                open_terminal.assert_called_once()
                self.assertEqual(open_terminal.call_args.args[0], "CONOUT$" if sys.platform == "win32" else "/dev/tty")

    @unittest.skipUnless(HAS_STORAGE, "requires local Xbox account storage")
    def test_open_failure_leaves_the_provider_noninteractive(self):
        self.seed_remembered_account()
        stdin = Terminal(True)
        stdin.fileno = Mock(return_value=0)
        with (
            patch.object(_cli_terminal, "open", side_effect=OSError, create=True),
            patch.object(os, "tcgetpgrp", return_value=1, create=True),
        ):
            self.assertEqual(self.invoke_join(SERVER, stdin=stdin), 0)
        self.provider.assert_called_once_with(on_device_code=None)

    def test_posix_fallback_requires_a_matching_controlling_terminal(self):
        stdin = Terminal(True)
        stdin.fileno = Mock(return_value=0)
        for groups, opens_terminal in (((0,), False), ((12, 14), True)):
            with self.subTest(groups=groups):
                terminal = Terminal(True)
                terminal.fileno = Mock(return_value=9)
                with (
                    ExitStack() as owned,
                    patch.object(sys, "platform", "linux"),
                    patch.object(sys, "stdin", stdin),
                    patch.object(sys, "stderr", Terminal(False)),
                    patch.object(sys, "stdout", Terminal(False)),
                    patch.object(_cli_terminal, "open", return_value=terminal, create=True) as open_terminal,
                    patch.object(os, "tcgetpgrp", side_effect=groups, create=True) as tcgetpgrp,
                ):
                    self.assertIsNone(_cli_terminal._device_terminal(owned))
                self.assertEqual(open_terminal.call_count, int(opens_terminal))
                self.assertEqual(tcgetpgrp.call_args_list, [call(0), call(9)] if opens_terminal else [call(0)])
                self.assertEqual(terminal.closed, opens_terminal)

    @unittest.skipUnless(HAS_STORAGE, "requires local Xbox account storage")
    def test_explicit_nethernet_urls_join_with_closed_input_and_no_tty(self):
        self.seed_remembered_account()
        pin = "01" * 32
        for endpoint, options, expected_pin in (
            ("http://127.0.0.1:19132", (), None),
            ("http://bedrock.example:19132/signaling", (), None),
            ("https://bedrock.example:19132/signaling", ("--server-key-sha256", pin), pin),
        ):
            with self.subTest(endpoint=endpoint):
                self.provider.reset_mock()
                self.auth.reset_mock()
                self.bot.join_address.reset_mock()
                self.bot.close.reset_mock()
                self.assertEqual(self.invoke_join(endpoint, *options), 0)
                self.provider.assert_called_once_with(on_device_code=None)
                (address,), settings = self.bot.join_address.await_args
                self.bot.join_address.assert_awaited_once()
                self.assertEqual(address, endpoint)
                self.assertTrue(callable(settings["credential_provider"]))
                self.assertEqual(settings["server_key_sha256"], expected_pin)
                self.assertIsNone(settings["on_resource_packs"])
                self.auth.assert_awaited_once_with(self.key, xuid=None, add=False)
                self.bot.close.assert_awaited_once()
                self.assertEqual(self.stderr.getvalue(), "")
        self.assertEqual(self.path_lookup.call_args_list, [call()] * 3)
        self.keyboard.assert_not_called()
        self.discard_typeahead.assert_not_called()
        self.browser.assert_not_called()

    @unittest.skipUnless(HAS_STORAGE, "requires local Xbox account storage")
    def test_https_failure_uses_only_the_supplied_url(self):
        from test_join import _online_identity

        self.seed_remembered_account()
        endpoint = "https://bedrock.example:19132"
        bot = self.bot_factory.return_value = Bot("EndPilot")

        async def acquire(credential_provider):
            await self.authorize_join(endpoint, credential_provider=credential_provider)
            return _online_identity()

        with (
            patch.object(bot, "close", wraps=bot.close) as close,
            patch("endpilot.bot.acquire_online_identity", side_effect=acquire),
            patch("endpilot.transport.nethernet.connect", side_effect=OSError("fixture handshake failure")) as nethernet,
            patch("endpilot.transport.raknet.connect") as raknet,
        ):
            self.assertEqual(self.invoke_join(endpoint), 1)
        self.provider.assert_called_once_with(on_device_code=None)
        self.auth.assert_awaited_once_with(self.key, xuid=None, add=False)
        nethernet.assert_awaited_once()
        self.assertEqual(nethernet.await_args.args, (endpoint,))
        raknet.assert_not_called()
        close.assert_awaited_once()
        self.assertNotIn("fixture handshake failure", self.stderr.getvalue())

    @unittest.skipUnless(HAS_STORAGE, "requires local Xbox account storage")
    def test_resource_pack_acceptance_is_opt_in_for_each_join_without_a_tty(self):
        from test_join import FakeTransport, _online_identity
        from test_resource_packs import archive_bytes

        self.seed_remembered_account()
        pack_id = UUID(int=1)
        pack_name = f"{pack_id}_1.0.0"
        data = archive_bytes(pack_id, b"CLI fixture")
        for transport, endpoint in (("raknet", SERVER), ("nethernet", "http://127.0.0.1:19132/signaling")):
            for accept in (True, False):
                with self.subTest(transport=transport, accept=accept):
                    bot = Bot("EndPilot")
                    self.bot_factory.return_value = bot
                    protocol = bot.protocol
                    b = protocol.binding
                    responses = b.ResourcePackClientResponsePacket
                    wire = FakeTransport()
                    settings = b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE)
                    offer = b.ResourcePacksInfoPacket(resource_pack_required=True, resource_packs=[b.PackInfoData(
                        pack_id_version=b.PackIdVersion(id=pack_id, version=b.SemVersion(version="1.0.0")),
                        pack_size=len(data),
                    )])
                    if accept:
                        options = ("--accept-resource-packs",)
                        reply = (
                            b.ResourcePackDataInfoPacket(
                                resource_name=pack_name, file_size=len(data), chunk_size=len(data), nb_chunks=1,
                                pack_type=b.PackType.RESOURCES, file_hash=hashlib.sha256(data).digest(),
                            ),
                            b.ResourcePackChunkDataPacket(
                                resource_name=pack_name, chunk_id=0, byte_offset=0, data=data,
                            ),
                            b.ResourcePackStackPacket(texture_pack_ids_and_versions=[
                                b.PackInstanceId(pack_id=pack_id, version="1.0.0"),
                            ]),
                            b.StartGamePacket(runtime_id=11, entity_id=45),
                            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
                        )
                    else:
                        options = ()
                        reply = (b.DisconnectPacket(),)
                    for packet in (settings, offer, *reply):
                        wire.received.put_nowait(write_batch([protocol.encode(packet)]))

                    async def acquire(credential_provider):
                        self.assertEqual(await credential_provider(self.key), self.credentials)
                        return _online_identity()

                    self.provider.reset_mock()
                    self.auth.reset_mock()
                    with (
                        patch("endpilot.bot.acquire_online_identity", side_effect=acquire),
                        patch(f"endpilot.transport.{transport}.connect", return_value=wire),
                    ):
                        status = self.invoke_join(endpoint, *options)
                    self.assertEqual(status, 0 if accept else 1, self.stderr.getvalue())
                    self.provider.assert_called_once_with(on_device_code=None)
                    self.auth.assert_awaited_once_with(self.key, xuid=None, add=False)
                    self.assertTrue(wire.closed)
                    sent = [raw for batch in wire.sent for raw in read_batch(batch)]
                    decisions = [protocol.decode(raw).response for raw in sent if raw.packet_id == responses.ID]
                    initialized = any(raw.packet_id == b.SetLocalPlayerAsInitializedPacket.ID for raw in sent)
                    self.assertEqual(initialized, accept)
                    if accept:
                        self.assertEqual(self.stdout.getvalue(), "Joined.\nDisconnected.\n")
                        self.assertEqual(self.stderr.getvalue(), "")
                        self.assertEqual([type(decision) for decision in decisions], [
                            responses.Downloading, responses.DownloadingFinished, responses.ResourcePackStackFinished,
                        ])
                        self.assertEqual(decisions[0].downloading_packs, [pack_name])
                        requests = [
                            protocol.decode(raw) for raw in sent if raw.packet_id == b.ResourcePackChunkRequestPacket.ID
                        ]
                        self.assertEqual([(request.resource_name, request.chunk) for request in requests], [(pack_name, 0)])
                    else:
                        self.assertEqual(self.stdout.getvalue(), "")
                        self.assertEqual([type(decision) for decision in decisions], [responses.Cancel])
        self.assertEqual(self.path_lookup.call_args_list, [call()] * 4)
        self.keyboard.assert_not_called()
        self.discard_typeahead.assert_not_called()
        self.browser.assert_not_called()

    def test_invalid_server_urls_and_pins_are_rejected_before_creating_the_bot(self):
        endpoint = "http://bedrock.example:19132"
        for arguments in (
            ("bedrock.example",),
            ("ftp://bedrock.example:19132",),
            ("http:///signaling",),
            ("http://user:password@bedrock.example:19132",),
            ("http://bedrock.example:0",),
            ("http://bedrock.example:65536",),
            (f"{endpoint}?query=1",),
            (f"{endpoint}#fragment",),
            (f"{SERVER}/signaling",),
            (endpoint, "--server-key-sha256", "ab" * 31),
            (endpoint, "--server-key-sha256", "AB" * 32),
            (SERVER, "--server-key-sha256", "ab" * 32),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke_join(*arguments), 2)
                self.assertNotIn(arguments[0], self.stderr.getvalue())
        self.path_lookup.assert_not_called()
        self.assertFalse(self.path.parent.exists())
        self.provider.assert_not_called()
        self.bot_factory.assert_not_called()

    @unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
    def test_enter_opens_original_address_once_while_authorization_is_pending(self):
        code = "TEST-1234"
        marker = "private browser diagnostic fixture"
        for uri, outcome in (
            ("https://www.microsoft.com/link", False),
            ("https://microsoft.com/devicelogin", OSError(marker)),
            ("https://www.microsoft.com/devicelogin", True),
        ):
            with self.subTest(uri=uri):
                self.browser.reset_mock()
                app = self.mock_device_authority(uri, code)
                polling, opened, finish_browser, finished = Event(), Event(), Event(), Event()
                printed = []
                enter_reads = []

                def enter_pressed(_stdin):
                    if not polling.is_set():
                        return False
                    enter_reads.append(True)
                    return True

                def browse(_url):
                    printed.append(self.stderr.getvalue())
                    opened.set()
                    try:
                        finish_browser.wait(5)
                        if isinstance(outcome, Exception):
                            raise outcome
                        return outcome
                    finally:
                        finished.set()

                def poll(*args, **kwargs):
                    try:
                        self.browser.assert_not_called()
                        polling.set()
                        self.assertTrue(opened.wait(5))
                        self.assertFalse(finished.is_set(), "browser must not block device polling")
                        # Keep the round pending with repeated Enter available.
                        finished.wait(0.15)
                        self.assertEqual(enter_reads, [True])
                        return {"access_token": "fixture-access-token"}
                    finally:
                        finish_browser.set()

                self.browser.side_effect = browse
                self.keyboard.side_effect = enter_pressed
                app.acquire_token_by_device_flow.side_effect = poll
                self.assertEqual(self.invoke_join(SERVER, interactive=True, stdin=Terminal(True)), 0)
                self.assertTrue(finished.wait(5))
                self.browser.assert_called_once_with(uri)
                app.acquire_token_by_device_flow.assert_called_once()
                prompt = (
                    "! Copy your one-time code to sign in to Microsoft\n"
                    f"  {code}\n\n"
                    f"Press Enter to open {uri} here, or visit it on another device.\n"
                )
                self.assertEqual(printed, [self.remember_notice() + prompt])
                self.assertEqual(self.stderr.getvalue(), self.remember_notice() + prompt +
                                 "Xbox account was not remembered. Continuing with this sign-in only.\n")
                self.assertEqual(self.stderr.writes.count(prompt), 1)
                self.assertEqual(self.stderr.flushes, 3)

    @unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
    def test_repeat_and_preparation_enter_do_not_open_the_next_round(self):
        stdin = Terminal(True)
        keys = deque()
        discarded = []

        def discard(stream):
            self.assertIs(stream, stdin)
            self.assertEqual(self.stderr.getvalue(), self.remember_notice())
            discarded.append(tuple(keys))
            keys.clear()
            return True

        self.discard_typeahead.side_effect = discard
        for uri in ("https://www.microsoft.com/link", "https://microsoft.com/devicelogin"):
            with self.subTest(uri=uri):
                self.browser.reset_mock()
                app = self.mock_device_authority(uri, "TEST-1234")
                response = app.initiate_device_flow.return_value
                empty_after_prompt, browser_opened = Event(), Event()

                def prepare(*args, **kwargs):
                    keys.append("\r")
                    return response

                def read_enter(_stdin):
                    self.assertIn("Press Enter", self.stderr.getvalue())
                    if keys:
                        return keys.popleft() == "\r"
                    empty_after_prompt.set()
                    return False

                def poll(*args, **kwargs):
                    self.assertTrue(empty_after_prompt.wait(5))
                    self.browser.assert_not_called()
                    keys.extend(("\r", "\r"))
                    self.assertTrue(browser_opened.wait(5))
                    return {"access_token": "fixture-access-token"}

                app.initiate_device_flow.side_effect = prepare
                app.acquire_token_by_device_flow.side_effect = poll
                self.keyboard.side_effect = read_enter
                self.browser.side_effect = lambda _url: browser_opened.set()
                self.assertEqual(self.invoke_join(SERVER, interactive=True, stdin=stdin), 0)
                self.browser.assert_called_once_with(uri)
                self.assertEqual(list(keys), ["\r"])
        self.assertEqual(discarded, [("\r",), ("\r", "\r")])

    @unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
    def test_approval_without_enter_stops_listening_and_preserves_later_input(self):
        uri, code = "https://www.microsoft.com/link", "TEST-1234"
        for stdin in (Terminal(True), Terminal(False), None):
            with self.subTest(interactive_stdin=stdin is not None and stdin.isatty()):
                self.keyboard.reset_mock()
                self.discard_typeahead.reset_mock()
                app = self.mock_device_authority(uri, code)
                app.acquire_token_by_device_flow.return_value = {"access_token": "fixture-access-token"}
                late_keys = []
                approved = Event()

                def enter_pressed(_stdin):
                    if approved.is_set():
                        late_keys.append(True)
                        return True
                    return False

                async def exchange_after_approval(*args):
                    approved.set()
                    await asyncio.sleep(0.12)
                    return self.exchange.return_value

                self.keyboard.side_effect = enter_pressed
                self.exchange.side_effect = exchange_after_approval
                self.assertEqual(self.invoke_join(SERVER, interactive=True, stdin=stdin), 0)
                app.acquire_token_by_device_flow.assert_called_once()
                self.browser.assert_not_called()
                self.assertEqual(late_keys, [])
                if stdin is not None and stdin.isatty():
                    instruction = f"Press Enter to open {uri} here, or visit it on another device."
                else:
                    instruction = f"Visit {uri} on another device and enter the code."
                    self.keyboard.assert_not_called()
                    self.discard_typeahead.assert_not_called()
                prompt = f"! Copy your one-time code to sign in to Microsoft\n  {code}\n\n{instruction}\n"
                self.assertEqual(self.stderr.getvalue(), self.remember_notice() + prompt +
                                 "Xbox account was not remembered. Continuing with this sign-in only.\n")
                self.assertEqual(self.stderr.writes.count(prompt), 1)
                self.assertEqual(self.stderr.flushes, 3)

    @unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
    def test_cancel_stops_listening_before_inflight_poll_drains(self):
        app = self.mock_device_authority("https://www.microsoft.com/link", "TEST-1234")
        polling, allow_result = Event(), Event()

        def poll(*args, **kwargs):
            polling.set()
            allow_result.wait(5)
            return {"access_token": "fixture-access-token"}

        async def join_and_cancel(_address, *, credential_provider, **_options):
            auth = asyncio.create_task(credential_provider(self.key))
            try:
                async with asyncio.timeout(5):
                    while not polling.is_set():
                        await asyncio.sleep(0.01)
                auth.cancel()
                self.keyboard.reset_mock()
                self.keyboard.return_value = True
                await asyncio.sleep(0.12)
                self.assertFalse(auth.done(), "provider must drain its in-flight poll")
            finally:
                allow_result.set()
                await auth

        app.acquire_token_by_device_flow.side_effect = poll
        self.bot.join_address.side_effect = join_and_cancel
        self.assertEqual(self.invoke_join(SERVER, interactive=True, stdin=Terminal(True)), 130)
        self.keyboard.assert_not_called()
        self.browser.assert_not_called()
        self.exchange.assert_not_awaited()

    @unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
    def test_denial_and_expiry_stop_listening_before_shutdown(self):
        for error in ("authorization_declined", "expired_token"):
            with self.subTest(error=error):
                app = self.mock_device_authority("https://www.microsoft.com/link", "TEST-1234")
                app.acquire_token_by_device_flow.return_value = {"error": error}
                self.keyboard.return_value = False

                async def join_then_pause(_address, *, credential_provider, **_options):
                    try:
                        await credential_provider(self.key)
                    finally:
                        self.keyboard.reset_mock()
                        self.keyboard.return_value = True
                        await asyncio.sleep(0.12)

                self.bot.join_address.side_effect = join_then_pause
                self.assertEqual(self.invoke_join(SERVER, interactive=True, stdin=Terminal(True)), 1)
                app.acquire_token_by_device_flow.assert_called_once()
                self.keyboard.assert_not_called()
                self.browser.assert_not_called()

    @unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
    def test_local_deadline_stops_listening_while_poll_is_still_pending(self):
        app = self.mock_device_authority("https://www.microsoft.com/link", "TEST-1234", lifetime=1)
        polling, late_enter, allow_result = Event(), Event(), Event()
        late_reads = []

        def enter_pressed(_stdin):
            if late_enter.is_set():
                late_reads.append(True)
                return True
            return False

        def poll(*args, **kwargs):
            polling.set()
            allow_result.wait(5)
            return {"error": "authorization_pending"}

        join = self.bot.join_address.side_effect

        async def join_and_timeout(*args, **kwargs):
            auth = asyncio.create_task(join(*args, **kwargs))
            try:
                async with asyncio.timeout(5):
                    while not polling.is_set():
                        await asyncio.sleep(0.01)
                await asyncio.sleep(1.05)
                late_enter.set()
                await asyncio.sleep(0.12)
            finally:
                allow_result.set()
                await auth

        self.keyboard.side_effect = enter_pressed
        app.acquire_token_by_device_flow.side_effect = poll
        self.bot.join_address.side_effect = join_and_timeout
        self.assertEqual(self.invoke_join(SERVER, interactive=True, stdin=Terminal(True)), 1)
        self.assertTrue(late_enter.is_set())
        self.assertEqual(late_reads, [])
        self.browser.assert_not_called()

    @unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
    def test_untrusted_prompt_never_reaches_browser_or_terminal(self):
        for uri, code in (
            ("https://www.microsoft.com.link.example.invalid/link", "TEST-1234"),
            ("https://www.microsoft.com/link?unexpected=1", "TEST-1234"),
            ("https://www.microsoft.com/link", "TEST\n1234"),
        ):
            with self.subTest(uri=uri, code=code):
                app = self.mock_device_authority(uri, code)
                self.assertEqual(self.invoke_join(SERVER, interactive=True, stdin=Terminal(True)), 1)
                output = self.stdout.getvalue() + self.stderr.getvalue()
                self.assertNotIn(uri, output)
                self.assertNotIn(code, output)
                self.assertFalse(self.path.exists())
                self.browser.assert_not_called()
                self.keyboard.assert_not_called()
                app.initiate_device_flow.assert_called_once()
                app.acquire_token_by_device_flow.assert_not_called()

    def test_removed_application_option_is_rejected_without_echoing_input(self):
        marker = "private application argument fixture"
        for arguments in (
            ("--client-id",),
            ("--client-id", marker),
            ("join", SERVER, "--xbox", "--client-id", marker),
            ("account", "forget", f"--client-id={marker}"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(*arguments), 2)
                self.assertIn("unrecognized arguments", self.stderr.getvalue())
                self.assertNotIn(marker, self.stderr.getvalue())
        for command in ((), ("join",), ("account", "forget")):
            self.assertEqual(self.invoke(*command, "--help"), 0)
            self.assertNotIn("--client-id", self.stdout.getvalue())
        self.provider.assert_not_called()
        self.path_lookup.assert_not_called()
        self.assertFalse(self.path.parent.exists())
        self.bot_factory.assert_not_called()

    def test_invalid_subcommand_arguments_stop_before_cache_or_network_actions(self):
        for arguments in (
            ("join", SERVER, "--xbox", "--remember-account"),
            ("join", SERVER, "--xbox", "--forget-account"),
            ("account", "forget", SERVER),
            ("account", "forget", "--xbox"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(*arguments), 2)
                self.assertIn("unrecognized arguments", self.stderr.getvalue())
        self.provider.assert_not_called()
        self.path_lookup.assert_not_called()
        self.assertFalse(self.path.parent.exists())
        self.bot_factory.assert_not_called()

    def test_join_failure_and_cancellation_close_the_bot_without_printing_error_details(self):
        marker = "private diagnostic fixture"
        for error, status in ((RuntimeError(marker), 1), (asyncio.CancelledError(marker), 130)):
            with self.subTest(status=status):
                cancelling = status == 130
                self.bot.join_address.side_effect = self.authorize_join if cancelling else error
                self.auth.side_effect = error if cancelling else None
                self.bot.close.reset_mock()
                self.assertEqual(self.invoke_join(interactive=cancelling), status)
                self.bot.close.assert_awaited_once()
                self.assertNotIn(marker, self.stderr.getvalue())
        self.auth.assert_awaited_once_with(self.key, xuid=None, add=False)
        self.assertTrue(callable(self.provider.call_args.kwargs["on_device_code"]))
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(HAS_STORAGE, "requires local Xbox account storage")
    def test_required_authorization_has_a_specific_safe_message_without_an_interactive_retry(self):
        self.seed_remembered_account()
        marker = "private authorization fixture"
        self.auth.side_effect = xbox.XboxReauthenticationRequired(marker)
        self.assertEqual(self.invoke_join(), 1)
        message = self.stderr.getvalue()
        self.assertIn("Xbox authorization is required.", message)
        self.assertIn("Run python -m endpilot account add in an interactive terminal.", message)
        self.assertNotIn(marker, message)
        self.provider.assert_called_once_with(on_device_code=None)
        self.auth.assert_awaited_once_with(self.key, xuid=None, add=False)
        self.bot.close.assert_awaited_once()

        self.auth.reset_mock(side_effect=True)
        self.assertEqual(self.invoke("account", "add", interactive=True), 0)
        self.assertIn(f"Remembered Xbox account {PROFILE[1]} ({PROFILE[0]}).", self.stdout.getvalue())
        self.assertTrue(callable(self.provider.call_args.kwargs["on_device_code"]))
        self.assertEqual(self.auth.await_args.kwargs, {"xuid": None, "add": True})
        self.bot_factory.assert_called_once()

    def test_cache_network_and_server_failures_have_distinct_safe_messages(self):
        marker = "private failure fixture"
        cases = (
            (xbox.XboxAccountCacheError, "The local Xbox account cache is unreadable, invalid, or inaccessible."),
            (xbox.XboxAuthorizationUnavailable, "Xbox authorization is temporarily unavailable."),
            (LoginRefused, "Login refused by the server."),
        )
        for kind, message in cases:
            with self.subTest(kind=kind.__name__):
                self.bot.join_address.side_effect = kind(marker)
                self.bot.close.reset_mock()
                self.assertEqual(self.invoke_join(), 1)
                self.assertTrue(self.stderr.getvalue().startswith(message))
                self.assertNotIn(marker, self.stderr.getvalue())
                self.bot.close.assert_awaited_once()


@unittest.skipUnless(HAS_XBOX, "requires the Xbox extra")
class XboxCacheCliTerminalProcessTests(unittest.TestCase):
    def test_routes_prompts_to_a_real_terminal_and_reuses_cache_headless(self):
        tests = Path(__file__).resolve().parent
        for scenario, status, requests, has_prompt in (
            ("stdout", 0, (1, 1), True),
            ("device", 0, (1, 1), True),
            ("null_stderr", 0, (1, 1), True),
            ("null", 1, (0, 0), False),
            ("pipe", 1, (0, 0), False),
            ("cached", 0, (0, 0), False),
        ):
            with self.subTest(scenario=scenario), TemporaryDirectory() as home, ExitStack() as opened:
                env = dict(os.environ, HOME=home, USERPROFILE=home, LOCALAPPDATA=home, APPDATA=home, XDG_STATE_HOME=home,
                           PYTHONPATH=os.pathsep.join((str(tests.parent / "src"), str(tests))))
                command = [sys.executable, "-B", str(tests / "fixtures" / "xbox_cli_terminal_child.py"), home, scenario]
                if sys.platform == "win32":
                    console = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW,
                                                     wShowWindow=subprocess.SW_HIDE)
                    options = dict(startupinfo=console, creationflags=subprocess.CREATE_NEW_CONSOLE)
                else:
                    import pty

                    master, slave = pty.openpty()
                    opened.callback(os.close, master)
                    opened.callback(os.close, slave)
                    command.append(str(slave))
                    options = dict(start_new_session=True, pass_fds=(slave,))
                if scenario in {"stdout", "pipe"}:
                    options["input"] = b"\n"
                else:
                    options["stdin"] = subprocess.DEVNULL
                result = subprocess.run(command, env=env, capture_output=True, timeout=20, **options)
                stdout, stderr = result.stdout.decode(), result.stderr.decode()
                self.assertEqual(result.returncode, status, (stdout, stderr))
                report = json.loads((Path(home) / "terminal-report.json").read_text(encoding="utf-8"))
                if sys.platform == "win32":
                    terminal = report.pop("terminal")
                else:
                    os.set_blocking(master, False)
                    try:
                        terminal = os.read(master, 65536).decode()
                    except BlockingIOError:
                        terminal = ""

                self.assertEqual(report["requests"], list(requests))
                self.assertEqual(report["exchange"], int(status == 0))
                self.assertEqual(report["browser"], [])
                self.assertEqual(report["accounts"], [list(PROFILE)] if status == 0 else [])
                self.assertEqual(report["cache_unchanged"], not has_prompt)
                self.assertEqual("renewable sign-in credentials in plaintext" in terminal, has_prompt)
                if has_prompt:
                    self.assertLess(terminal.index("plaintext"), terminal.index(CODE))
                self.assertEqual(terminal.count(CODE), int(has_prompt))
                self.assertEqual("Press Enter" in terminal, scenario in {"device", "null_stderr"})
                self.assertEqual(f"Visit {URI} on another device" in terminal, scenario == "stdout")
                self.assertNotIn(CODE, stdout + stderr + report["logs"])
                self.assertNotIn(f"{URI}?otc=", terminal + stdout + stderr + report["logs"])
                if scenario == "cached":
                    self.assertEqual(stdout.splitlines(), ["Joined.", "Disconnected."])
                    self.assertEqual(stderr, "")
                if not has_prompt and status == 1:
                    self.assertIn("Xbox authorization is required.", stderr)

    @unittest.skipUnless(sys.platform == "win32", "uses Windows console handles and native pipes")
    def test_device_code_and_notice_stay_on_the_console_with_broken_windows_pipes(self):
        tests = Path(__file__).resolve().parent
        fixture = tests / "fixtures" / "xbox_cli_terminal_child.py"
        for stdout_available in (True, False):
            with self.subTest(stdout_available=stdout_available), TemporaryDirectory() as home, ExitStack() as opened:
                local = Path(home) / "合成"
                env = dict(os.environ, HOME=home, USERPROFILE=home, LOCALAPPDATA=str(local), APPDATA=home,
                           XDG_STATE_HOME=home, PYTHONIOENCODING="utf-8",
                           PYTHONPATH=os.pathsep.join((str(tests.parent / "src"), str(tests))))
                reader, writer = os.pipe()
                opened.callback(os.close, writer)
                os.close(reader)
                scenario = "broken_stderr" if stdout_available else "broken_outputs"
                console = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW,
                                                 wShowWindow=subprocess.SW_HIDE)
                result = subprocess.run(
                    [sys.executable, "-B", str(fixture), home, scenario],
                    env=env, timeout=20, stdin=subprocess.DEVNULL, stderr=writer,
                    stdout=subprocess.PIPE if stdout_available else writer,
                    startupinfo=console, creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                status = 0 if stdout_available else 1
                self.assertEqual(result.returncode, status)
                report_path = Path(home) / "terminal-report.json"
                self.assertTrue(report_path.is_file(), "CLI exited before returning a controlled status")
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["cli_status"], status)
                self.assertEqual(report["requests"], [1, 1] if stdout_available else [0, 0])
                self.assertEqual(report["exchange"], int(stdout_available))
                self.assertEqual(report["accounts"], [list(PROFILE)] if stdout_available else [])
                self.assertEqual(report["cache_unchanged"], not stdout_available)
                self.assertEqual(report["browser"], [])
                terminal = report["terminal"]
                self.assertEqual(terminal.count(CODE), int(stdout_available))
                self.assertEqual("plaintext" in terminal, stdout_available)
                stdout = (result.stdout or b"").decode("utf-8")
                self.assertNotIn(CODE, stdout + report["logs"])
                self.assertNotIn("plaintext", stdout + report["logs"])
                if stdout_available:
                    self.assertEqual(stdout.splitlines(), ["Joined.", "Disconnected."])
                    self.assertIn("renewable sign-in credentials in plaintext", terminal)
                    self.assertLess(terminal.index("plaintext"), terminal.index(CODE))
                    cache = local / "EndPilot" / "auth" / f"{xbox.DEFAULT_CLIENT_ID}.json"
                    self.assertIn(json.dumps(str(cache), ensure_ascii=False), terminal)
                else:
                    self.assertFalse(local.exists())
