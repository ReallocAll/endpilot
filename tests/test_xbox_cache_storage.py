"""Test private Xbox cache storage with temporary directories and local processes."""

import asyncio
from importlib.util import find_spec
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

_HAS_STORAGE = all(find_spec(name) is not None for name in ("httpx", "jwt", "msal")) and (
    os.name != "nt" or find_spec("win32security") is not None
)
if _HAS_STORAGE:
    from endpilot.xbox import _cache


_CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_OTHER_CLIENT_ID = "11111111-1111-4111-8111-111111111111"
_ACCOUNT = "synthetic-account"
_OTHER_ACCOUNT = "synthetic-other-account"
_SERIALIZED = '{"example":"synthetic-cache"}'
_XUID = "9007199254740993"
_OTHER_XUID = "9007199254740995"
_CHILD = r"""
import asyncio
import json
import os
from pathlib import Path
import sys
from threading import Event
from unittest.mock import patch

sys.path[:0] = json.loads(sys.argv[1])
from endpilot.xbox import _cache

async def main():
    path, phase = Path(sys.argv[2]), sys.argv[3]
    async with _cache.locked_cache(path):
        if phase == "write":
            account_id = "synthetic-account"
            serialized = next((account.serialized_cache for account in _cache.load_accounts(path)
                               if account.home_account_id == account_id), "{}")

            def before_replace(*args, **kwargs):
                print("ready", flush=True)
                Event().wait()
            with patch.object(os, "replace", before_replace):
                _cache.save_cache(path, account_id, serialized)
        else:
            print("ready", flush=True)
            await asyncio.Event().wait()

asyncio.run(main())
"""


def _msal_state(home_account_id, *, client_id=_CLIENT_ID, refresh_token="synthetic-renewal"):
    account = {"home_account_id": home_account_id, "environment": "login.microsoftonline.com"}
    token = {**account, "client_id": client_id}
    return {
        "Account": {home_account_id: {**account, "realm": "consumers", "authority_type": "MSSTS",
                                      "username": "synthetic-username"}},
        "RefreshToken": {f"{home_account_id}-{client_id}": {**token, "secret": refresh_token}},
    }


@unittest.skipUnless(_HAS_STORAGE, "requires the Xbox extra")
class XboxCacheStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.path = self.root / "state" / "auth" / f"{_CLIENT_ID}.json"

    async def start_child(self, phase, path=None):
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", "-c", _CHILD,
            json.dumps([str(Path(item).absolute()) for item in sys.path if item]), str(path or self.path), phase,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        self.addAsyncCleanup(self.stop_child, child)
        self.assertEqual((await asyncio.wait_for(child.stdout.readline(), 8)).strip(), b"ready")
        return child

    @staticmethod
    async def stop_child(child):
        if child.returncode is None:
            child.kill()
        await child.communicate()

    def assert_private(self, path, *, directory=False):
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if directory else 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
            return
        import win32security

        description = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = description.GetSecurityDescriptorDacl()
        self.assertTrue(description.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED)
        self.assertEqual(dacl.GetAceCount(), 1)
        self.assertEqual(dacl.GetAce(0)[0][0], win32security.ACCESS_ALLOWED_ACE_TYPE)
        self.assertTrue(dacl.GetAce(0)[2] == description.GetSecurityDescriptorOwner())

    async def test_paths_are_normalized_and_initial_checks_are_read_only(self):
        with patch.object(Path, "home", return_value=self.root):
            for platform, environment, app_root in (
                ("win32", {"LOCALAPPDATA": str(self.root)}, self.root / "EndPilot"),
                ("darwin", {}, self.root / "Library" / "Application Support" / "EndPilot"),
                ("linux", {"XDG_STATE_HOME": "relative"}, self.root / ".local" / "state" / "endpilot"),
                ("linux", {"XDG_STATE_HOME": str(self.root / "xdg")}, self.root / "xdg" / "endpilot"),
            ):
                with self.subTest(platform=platform, environment=environment):
                    with patch.object(sys, "platform", platform), patch.dict(os.environ, environment):
                        self.assertEqual(_cache.cache_path(_CLIENT_ID.upper()), app_root / "auth" / f"{_CLIENT_ID}.json")
        self.assertFalse(_cache.cache_exists(self.path))
        self.assertIsNone(_cache.load_cache(self.path))
        self.assertEqual(_cache.load_accounts(self.path), ())
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_v1_lists_without_writing_and_upgrades_only_the_verified_account(self):
        async with _cache.locked_cache(self.path):
            _cache.save_cache(self.path, _ACCOUNT, _SERIALIZED)
        original = self.path.read_bytes()
        legacy, = _cache.load_accounts(self.path)
        self.assertEqual(legacy, _cache.AccountRecord(_ACCOUNT, _SERIALIZED))
        self.assertEqual(self.path.read_bytes(), original)

        async with _cache.locked_cache(self.path):
            other = _cache.AccountRecord(_OTHER_ACCOUNT, json.dumps(_msal_state(_OTHER_ACCOUNT)), _OTHER_XUID, "Other")
            _cache.upsert_account(self.path, other)
            self.assertEqual(json.loads(self.path.read_bytes())["version"], 2)
            still_legacy, saved_other = _cache.load_accounts(self.path)
            self.assertEqual(still_legacy, legacy)
            self.assertEqual((saved_other.home_account_id, saved_other.xuid), (_OTHER_ACCOUNT, _OTHER_XUID))

            verified = _cache.AccountRecord(_ACCOUNT, json.dumps(_msal_state(_ACCOUNT)), _XUID, "Verified")
            _cache.upsert_account(self.path, verified)
            saved, other_after = _cache.load_accounts(self.path)
            self.assertEqual((saved.home_account_id, saved.xuid, saved.gamertag), (_ACCOUNT, _XUID, "Verified"))
            self.assertEqual(other_after, saved_other)

    async def test_skin_binding_requires_both_verified_identity_fields_and_is_removed_with_account(self):
        skin = _cache.SkinBinding(str(self.root / "first.png"), "slim")
        other_skin = _cache.SkinBinding(str(self.root / "other.png"), "wide")
        async with _cache.locked_cache(self.path):
            self.assertFalse(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, skin))
            self.assertFalse(self.path.exists())

            _cache.save_cache(self.path, _ACCOUNT, _SERIALIZED)
            original = self.path.read_bytes()
            self.assertFalse(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, skin))
            self.assertEqual(self.path.read_bytes(), original)

            first = _cache.AccountRecord(_ACCOUNT, json.dumps(_msal_state(_ACCOUNT)), _XUID, "First")
            other = _cache.AccountRecord(_OTHER_ACCOUNT, json.dumps(_msal_state(_OTHER_ACCOUNT)), _OTHER_XUID, "Other")
            _cache.upsert_account(self.path, other)
            self.assertFalse(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, skin))
            _cache.upsert_account(self.path, first)
            original = self.path.read_bytes()
            self.assertNotIn("skin", json.loads(original)["accounts"][0])
            self.assertTrue(all(account.skin is None for account in _cache.load_accounts(self.path)))

            self.assertFalse(_cache.set_account_skin(self.path, _ACCOUNT, _OTHER_XUID, skin))
            self.assertFalse(_cache.set_account_skin(self.path, _OTHER_ACCOUNT, _XUID, skin))
            self.assertFalse(_cache.set_account_skin(self.path, "missing", _XUID, skin))
            self.assertEqual(self.path.read_bytes(), original)

            self.assertTrue(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, skin))
            first_saved, other_saved = _cache.load_accounts(self.path)
            self.assertEqual(first_saved.skin, skin)
            self.assertIsNone(other_saved.skin)
            first_entry, other_entry = json.loads(self.path.read_bytes())["accounts"]
            self.assertEqual(first_entry["skin"], {"path": skin.path, "model": "slim"})
            self.assertNotIn("skin", other_entry)

            self.assertTrue(_cache.set_account_skin(self.path, _OTHER_ACCOUNT, _OTHER_XUID, other_skin))
            self.assertTrue(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, None))
            self.assertTrue(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, None))
            first_saved, other_saved = _cache.load_accounts(self.path)
            self.assertIsNone(first_saved.skin)
            self.assertEqual(other_saved.skin, other_skin)
            self.assertNotIn("skin", json.loads(self.path.read_bytes())["accounts"][0])

            self.assertTrue(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, skin))
            self.assertTrue(_cache.delete_account(self.path, _ACCOUNT))
            self.assertEqual(_cache.load_accounts(self.path), (other_saved,))
            _cache.upsert_account(self.path, first)
            self.assertIsNone(_cache.load_accounts(self.path)[1].skin)

    async def test_failed_skin_replacement_keeps_the_previous_binding(self):
        account = _cache.AccountRecord(_ACCOUNT, json.dumps(_msal_state(_ACCOUNT)), _XUID, "First")
        previous = _cache.SkinBinding(str(self.root / "first.png"), "slim")
        replacement = _cache.SkinBinding(str(self.root / "replacement.png"), "wide")
        async with _cache.locked_cache(self.path):
            _cache.upsert_account(self.path, account)
            self.assertTrue(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, previous))
            original = self.path.read_bytes()

            with (
                patch.object(os, "replace", side_effect=OSError("replacement interrupted")),
                self.assertRaisesRegex(OSError, "replacement interrupted"),
            ):
                _cache.set_account_skin(self.path, _ACCOUNT, _XUID, replacement)

            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(_cache.load_accounts(self.path)[0].skin, previous)
            self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])
            self.assertTrue(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, replacement))
            self.assertEqual(_cache.load_accounts(self.path)[0].skin, replacement)

    async def test_upsert_filters_msal_state_and_rejects_identity_conflicts(self):
        own = _msal_state(_ACCOUNT)
        foreign = _msal_state(_OTHER_ACCOUNT, refresh_token="synthetic-other-secret")
        wrong_app = _msal_state(_ACCOUNT, client_id=_OTHER_CLIENT_ID, refresh_token="synthetic-app-secret")
        mixed = {kind: own[kind] | foreign[kind] | wrong_app[kind] for kind in own}
        mixed["IdToken"] = {"ignored": {"secret": "synthetic-identity-secret"}}
        unrelated = _cache.AccountRecord(_OTHER_ACCOUNT, json.dumps(foreign), _OTHER_XUID, "Other")

        async with _cache.locked_cache(self.path):
            _cache.upsert_account(self.path, _cache.AccountRecord(_ACCOUNT, json.dumps(mixed), _XUID, "First"))
            _cache.upsert_account(self.path, unrelated)
            skin = _cache.SkinBinding(str(self.root / "first.png"), "slim")
            self.assertTrue(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, skin))
            _cache.upsert_account(self.path, _cache.AccountRecord(_ACCOUNT, json.dumps(mixed), _XUID, "New Name"))

            saved, saved_other = _cache.load_accounts(self.path)
            self.assertEqual((saved.xuid, saved.gamertag), (_XUID, "New Name"))
            self.assertEqual(saved.skin, skin)
            self.assertEqual(saved_other.home_account_id, _OTHER_ACCOUNT)
            self.assertIsNone(saved_other.skin)
            state = json.loads(saved.serialized_cache)
            self.assertEqual(set(state), {"Account", "RefreshToken"})
            self.assertEqual(set(state["Account"]), {_ACCOUNT})
            self.assertNotIn("username", state["Account"][_ACCOUNT])
            self.assertEqual(state["RefreshToken"], own["RefreshToken"])
            self.assertNotIn("synthetic-renewal", repr(saved))

            original = self.path.read_bytes()
            for entry, message in (
                (_cache.AccountRecord(_ACCOUNT, json.dumps(own), "9007199254740997", "Changed"), "different XUID"),
                (_cache.AccountRecord("new-home", json.dumps(_msal_state("new-home")), _XUID, "Changed"),
                 "different account"),
                (_cache.AccountRecord(_ACCOUNT, json.dumps(foreign), _XUID, "Changed"), "renewable MSAL cache"),
                (_cache.AccountRecord(_ACCOUNT, json.dumps(wrong_app), _XUID, "Changed"), "renewable MSAL cache"),
                (_cache.AccountRecord(_ACCOUNT, json.dumps(own)), "verified Xbox profile"),
            ):
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    _cache.upsert_account(self.path, entry)
                self.assertEqual(self.path.read_bytes(), original)

    async def test_cancelled_upgrade_keeps_v1_and_cleans_the_temporary_file(self):
        async with _cache.locked_cache(self.path):
            _cache.save_cache(self.path, _ACCOUNT, _SERIALIZED)
            original = self.path.read_bytes()
            record = _cache.AccountRecord(_ACCOUNT, json.dumps(_msal_state(_ACCOUNT)), _XUID, "Verified")

            def cancelled(source, destination, **options):
                self.assert_private(self.path.parent / Path(source).name)
                raise asyncio.CancelledError

            with patch.object(os, "replace", cancelled), self.assertRaises(asyncio.CancelledError):
                _cache.upsert_account(self.path, record)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])
            self.assertEqual(_cache.load_accounts(self.path), (_cache.AccountRecord(_ACCOUNT, _SERIALIZED),))

    async def test_targeted_forget_preserves_other_accounts_and_removes_forgotten_orphans(self):
        async with _cache.locked_cache(self.path):
            _cache.save_cache(self.path, _ACCOUNT, _SERIALIZED)
            record = _cache.AccountRecord(_OTHER_ACCOUNT, json.dumps(_msal_state(_OTHER_ACCOUNT)), _OTHER_XUID, "Other")
            _cache.upsert_account(self.path, record)
            saved_other = _cache.load_accounts(self.path)[1]

        child = await self.start_child("write")
        await self.stop_child(child)
        orphan, = self.path.parent.glob(f".{self.path.name}.*.tmp")
        async with _cache.locked_cache(self.path):
            self.assertTrue(_cache.delete_account(self.path, _ACCOUNT))
            self.assertFalse(orphan.exists())
            self.assertFalse(_cache.delete_account(self.path, _ACCOUNT))
            self.assertEqual(_cache.load_accounts(self.path), (saved_other,))
            self.assertEqual(_cache.load_cache(self.path), (_OTHER_ACCOUNT, saved_other.serialized_cache))
            self.assertTrue(_cache.delete_account(self.path, _OTHER_ACCOUNT))
            self.assertFalse(self.path.exists())
            self.assertTrue(self.path.with_suffix(".lock").exists())

    @unittest.skipUnless(os.name == "nt", "requires Windows file sharing")
    async def test_targeted_forget_can_retry_last_account_after_windows_sharing_conflict(self):
        import win32con
        import win32file
        import winerror

        async with _cache.locked_cache(self.path):
            record = _cache.AccountRecord(_ACCOUNT, json.dumps(_msal_state(_ACCOUNT)), _XUID, "Verified")
            _cache.upsert_account(self.path, record)
            original = self.path.read_bytes()

        child = await self.start_child("write")
        await self.stop_child(child)
        orphan, = self.path.parent.glob(f".{self.path.name}.*.tmp")
        self.assertEqual(orphan.read_bytes(), original)
        handle = win32file.CreateFile(str(orphan), win32con.GENERIC_READ,
                                      win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                                      None, win32con.OPEN_EXISTING, 0, None)
        try:
            async with _cache.locked_cache(self.path):
                with self.assertRaises(OSError) as failure:
                    _cache.delete_account(self.path, _ACCOUNT)
                self.assertEqual(failure.exception.winerror, winerror.ERROR_SHARING_VIOLATION)
                self.assertTrue(orphan.is_file())
                self.assertEqual(self.path.read_bytes(), original)
        finally:
            handle.Close()

        async with _cache.locked_cache(self.path):
            self.assertTrue(_cache.delete_account(self.path, _ACCOUNT))
            self.assertFalse(orphan.exists())
            self.assertFalse(self.path.exists())
            self.assertFalse(_cache.delete_account(self.path, _ACCOUNT))

    async def test_legacy_save_and_load_can_renew_v2_without_replacing_the_profile(self):
        async with _cache.locked_cache(self.path):
            _cache.save_cache(self.path, _OTHER_ACCOUNT, _SERIALIZED)
            _cache.save_cache(self.path, _ACCOUNT, _SERIALIZED)
            self.assertEqual(_cache.load_cache(self.path), (_ACCOUNT, _SERIALIZED))
            self.assertEqual(json.loads(self.path.read_bytes())["version"], 1)

            state = json.dumps(_msal_state(_ACCOUNT))
            _cache.upsert_account(self.path, _cache.AccountRecord(_ACCOUNT, state, _XUID, "Verified"))
            self.assertEqual(_cache.load_cache(self.path)[0], _ACCOUNT)
            skin = _cache.SkinBinding(str(self.root / "renewed.png"), "wide")
            self.assertTrue(_cache.set_account_skin(self.path, _ACCOUNT, _XUID, skin))
            renewed = json.dumps(_msal_state(_ACCOUNT, refresh_token="synthetic-rotated"))
            _cache.save_cache(self.path, _ACCOUNT, renewed)
            record, = _cache.load_accounts(self.path)
            self.assertEqual((record.xuid, record.gamertag), (_XUID, "Verified"))
            self.assertEqual(record.skin, skin)
            self.assertEqual(json.loads(record.serialized_cache)["RefreshToken"], json.loads(renewed)["RefreshToken"])

            original = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "existing account"):
                _cache.save_cache(self.path, _OTHER_ACCOUNT, _SERIALIZED)
            self.assertEqual(self.path.read_bytes(), original)
            other = _cache.AccountRecord(_OTHER_ACCOUNT, json.dumps(_msal_state(_OTHER_ACCOUNT)), _OTHER_XUID, "Other")
            _cache.upsert_account(self.path, other)
            saved_other = _cache.load_accounts(self.path)[1]
            _cache.save_cache(self.path, _ACCOUNT, state)
            saved, other_after = _cache.load_accounts(self.path)
            self.assertEqual(saved.skin, skin)
            self.assertEqual(other_after, saved_other)
            with self.assertRaisesRegex(ValueError, "multiple accounts"):
                _cache.load_cache(self.path)
            self.assertTrue(_cache.delete_cache(self.path))
            self.assertEqual(_cache.load_accounts(self.path), ())

    async def test_invalid_v2_metadata_is_rejected_as_a_cache_error(self):
        async with _cache.locked_cache(self.path):
            state = json.dumps(_msal_state(_ACCOUNT))
            _cache.upsert_account(self.path, _cache.AccountRecord(_ACCOUNT, state, _XUID, "Verified"))
            entry, = json.loads(self.path.read_bytes())["accounts"]
            skin = {"path": str(self.root / "first.png"), "model": "wide"}
            for accounts in (
                [dict(entry, xuid=int(_XUID))],
                [dict(entry, gamertag=None)],
                [entry, dict(entry, xuid=_OTHER_XUID)],
                [entry, dict(entry, home_account_id=_OTHER_ACCOUNT)],
                [dict(entry, skin=None)],
                [dict(entry, skin=[])],
                [dict(entry, skin={"path": skin["path"]})],
                [dict(entry, skin=dict(skin, extra=True))],
                [dict(entry, skin=dict(skin, path=100))],
                [dict(entry, skin=dict(skin, path="relative.png"))],
                [dict(entry, skin=dict(skin, path=skin["path"] + "\0"))],
                [dict(entry, skin=dict(skin, model="invalid"))],
                [dict(entry, skin=dict(skin, model=None))],
                [dict(entry, xuid=None, gamertag=None, skin=skin)],
            ):
                raw = json.dumps({"version": 2, "accounts": accounts})
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "^Xbox cache file is invalid$"):
                    _cache.load_accounts(self.path)
                with self.assertRaisesRegex(ValueError, "^Xbox cache file is invalid$"):
                    _cache.delete_account(self.path, _ACCOUNT)
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    async def test_private_files_and_atomic_failure_preserve_previous_data(self):
        async with _cache.locked_cache(self.path):
            self.assert_private(self.path.parent, directory=True)
            self.assert_private(self.path.with_suffix(".lock"))
            _cache.save_cache(self.path, _ACCOUNT, _SERIALIZED)
            self.assert_private(self.path)
            self.assertTrue(_cache.cache_exists(self.path))
            self.assertEqual(_cache.load_cache(self.path), (_ACCOUNT, _SERIALIZED))

            def interrupted(source, destination, **options):
                self.assert_private(self.path.parent / Path(source).name)
                raise OSError("replacement interrupted")

            with patch.object(os, "replace", interrupted):
                with self.assertRaisesRegex(OSError, "replacement interrupted"):
                    _cache.save_cache(self.path, _ACCOUNT, "{}")
            self.assertEqual(_cache.load_cache(self.path), (_ACCOUNT, _SERIALIZED))
            self.assertEqual(set(self.path.parent.iterdir()), {self.path, self.path.with_suffix(".lock")})

    async def test_forget_removes_only_this_apps_orphaned_private_write(self):
        other = self.path.with_name("11111111-1111-4111-8111-111111111111.json")
        async with _cache.locked_cache(other):
            _cache.save_cache(other, _ACCOUNT, _SERIALIZED)
        other_child = await self.start_child("write", other)
        await self.stop_child(other_child)
        other_orphan, = self.path.parent.glob(f".{other.name}.*.tmp")
        child = await self.start_child("write")
        await self.stop_child(child)
        orphans = list(self.path.parent.glob(f".{self.path.name}.*.tmp"))
        self.assertEqual(len(orphans), 1)
        self.assert_private(orphans[0])
        self.assertFalse(_cache.cache_exists(self.path))
        async with _cache.locked_cache(self.path):
            self.assertTrue(_cache.delete_cache(self.path))
            self.assertFalse(_cache.delete_cache(self.path))
        self.assertFalse(orphans[0].exists())
        self.assertTrue(self.path.with_suffix(".lock").is_file())
        self.assertEqual(self.path.with_suffix(".lock").stat().st_size, 0)
        self.assertTrue(other_orphan.is_file())
        async with _cache.locked_cache(other):
            self.assertEqual(_cache.load_cache(other), (_ACCOUNT, _SERIALIZED))

    async def test_waiting_lock_can_be_cancelled_and_crash_releases_stable_inode(self):
        owner = await self.start_child("lock")
        lock = self.path.with_suffix(".lock")
        identity = lock.stat().st_ino

        async def contender():
            async with _cache.locked_cache(self.path):
                return True

        waiter = asyncio.create_task(contender())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertIsNone(owner.returncode)
        await self.stop_child(owner)
        self.assertTrue(await asyncio.wait_for(contender(), 4))
        self.assertEqual(lock.stat().st_ino, identity)
        self.assertEqual(list(self.path.parent.iterdir()), [lock])

    async def test_existing_unprivate_directory_is_rejected_without_repair(self):
        self.path.parent.mkdir(parents=True)
        if os.name != "nt":
            self.path.parent.chmod(0o755)
        with self.assertRaises(PermissionError):
            _cache.cache_exists(self.path)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    async def test_hardlink_and_corrupt_file_are_distinguished_from_absence(self):
        async with _cache.locked_cache(self.path):
            _cache.save_cache(self.path, _ACCOUNT, _SERIALIZED)
            link = self.path.with_name("hardlink.json")
            os.link(self.path, link)
            with self.assertRaises(PermissionError):
                _cache.cache_exists(self.path)
            link.unlink()
            self.path.write_text("{invalid-synthetic-secret", encoding="utf-8")
            self.assertTrue(_cache.cache_exists(self.path))
            with self.assertRaisesRegex(ValueError, "^Xbox cache file is invalid$") as failure:
                _cache.load_cache(self.path)
            self.assertTrue(failure.exception.__suppress_context__)
            self.assertTrue(_cache.delete_cache(self.path))

    async def test_symlinked_directory_cannot_open_cache(self):
        other = self.root / "other" / "auth" / self.path.name
        async with _cache.locked_cache(other):
            _cache.save_cache(other, _ACCOUNT, _SERIALIZED)
        try:
            self.path.parent.parent.symlink_to(other.parent.parent, target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("creating Windows symlinks requires developer mode or a privilege")
            raise
        with self.assertRaises(OSError):
            _cache.cache_exists(self.path)
        with self.assertRaises(OSError):
            async with _cache.locked_cache(self.path):
                self.fail("an unsafe cache path was locked")
        async with _cache.locked_cache(other):
            self.assertEqual(_cache.load_cache(other), (_ACCOUNT, _SERIALIZED))
