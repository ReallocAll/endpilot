import asyncio
import base64
import ctypes
import json
import struct
import sys
import traceback
import unittest
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

if find_spec("httpx") is None:
    raise unittest.SkipTest("install the Xbox extra to test credential exchange")

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from endpilot.authentication import BedrockCredentials
from endpilot.xbox._exchange import ExchangeError, _has_windows_minecraft_store_package, _physical_memory_bytes, exchange


ACCESS_TOKEN = "fixture-microsoft-access"
AUTH_HEADER = "MCToken fixture-service-session"
FUTURE = "2099-07-12T14:00:00.1234567Z"
MEMORY_BYTES = 12 * 1024**3
ROUTES = (
    ("xbox-user", "https://user.auth.xboxlive.com/user/authenticate"),
    ("xsts-bedrock", "https://xsts.auth.xboxlive.com/xsts/authorize"),
    ("xsts-playfab", "https://xsts.auth.xboxlive.com/xsts/authorize"),
    ("playfab-login", "https://20ca2.playfabapi.com/Client/LoginWithXbox"),
    ("minecraft-session", "https://authorization.franchise.minecraft-services.net/api/v1.0/session/start"),
    ("bedrock-chain", "https://multiplayer.minecraft.net/authentication"),
    ("minecraft-multiplayer-token",
     "https://authorization.franchise.minecraft-services.net/api/v1.0/multiplayer/session/start"),
)


def _spki(curve):
    key = ec.generate_private_key(curve).public_key()
    return base64.b64encode(key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )).decode("ascii")


def _xbox_response(token, user_hash):
    return {"Token": token, "NotAfter": FUTURE, "DisplayClaims": {"xui": [{"uhs": user_hash}]}}


class XboxService:
    def __init__(self, test, public_key, *, windows=True, memory=MEMORY_BYTES, store=False):
        self.test = test
        self.public_key = public_key
        self.windows = windows
        self.memory = memory
        self.store = store
        self.calls = []
        self.proof = None
        self.override = {}

    async def serve(self, request):
        step = len(self.calls)
        self.test.assertLess(step, len(ROUTES), "unexpected request, fallback, or retry")
        stage, url = ROUTES[step]
        self.calls.append(stage)
        self.test.assertEqual(str(request.url), url)
        self.test.assertEqual(request.method, "POST")
        self.test.assertEqual(request.headers.get("User-Agent"), "EndPilot")
        self.test.assertEqual(request.headers.get("Content-Type"), "application/json")
        self.test.assertNotIn("Cookie", request.headers)
        self.test.assertNotIn("X-Default-Secret", request.headers)
        body = json.loads(request.content)
        if step < 3:
            self._verify_xbox_proof(request, body["Properties"]["ProofKey"])
        else:
            self.test.assertNotIn("Signature", request.headers)
            self.test.assertNotIn("x-xbl-contract-version", request.headers)
        if stage == "bedrock-chain":
            self.test.assertEqual(request.headers["Authorization"], "XBL3.0 x=bedrock-hash;fixture-bedrock-xsts")
            self.test.assertEqual(request.headers["Client-Version"], "1.26.66")
        elif stage == "minecraft-multiplayer-token":
            self.test.assertEqual(request.headers["Authorization"], AUTH_HEADER)
        else:
            self.test.assertNotIn("Authorization", request.headers)

        if stage == "xbox-user":
            self.test.assertEqual(body, {
                "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT",
                "Properties": {
                    "AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": f"d={ACCESS_TOKEN}", "ProofKey": self.proof,
                },
            })
            result = _xbox_response("fixture-xbox-user", "user-hash")
        elif step in (1, 2):
            rp = ("https://multiplayer.minecraft.net/" if step == 1
                  else "https://b980a380.minecraft.playfabapi.com/")
            self.test.assertEqual(body, {
                "RelyingParty": rp, "TokenType": "JWT",
                "Properties": {
                    "SandboxId": "RETAIL", "UserTokens": ["fixture-xbox-user"], "ProofKey": self.proof,
                },
            })
            result = (_xbox_response("fixture-bedrock-xsts", "bedrock-hash") if step == 1
                      else _xbox_response("fixture-playfab-xsts", "playfab-hash"))
        elif stage == "playfab-login":
            self.test.assertEqual(body, {
                "TitleId": "20CA2", "CreateAccount": True, "XboxToken": "XBL3.0 x=playfab-hash;fixture-playfab-xsts",
            })
            result = {"data": {"SessionTicket": "fixture-playfab-ticket"}}
        elif stage == "minecraft-session":
            self.test.assertEqual(set(body), {"user", "device"})
            self.test.assertEqual(body["user"], {
                "tokenType": "PlayFab", "token": "fixture-playfab-ticket",
                "language": "en", "languageCode": "en-US", "regionCode": "US",
            })
            device = body["device"]
            self.test.assertEqual(UUID(device.pop("id")).version, 4)
            if self.windows:
                self.test.assertEqual((device.pop("platform"), device.pop("type")), ("Windows10", "Windows10"))
            if self.memory is not None:
                self.test.assertEqual(device.pop("memory"), str(self.memory))
            if self.store:
                self.test.assertEqual(device.pop("storePlatform"), "uwp.store")
            self.test.assertEqual(device, {
                "applicationType": "MinecraftPE", "capabilities": [], "gameVersion": "1.26.66",
                "playFabTitleId": "20CA2",
            })
            result = {"result": {"authorizationHeader": AUTH_HEADER, "validUntil": FUTURE}}
        elif stage == "bedrock-chain":
            self.test.assertEqual(body, {"identityPublicKey": self.public_key})
            result = {"chain": ["fixture-intermediate", "fixture-leaf"]}
        else:
            self.test.assertEqual(body, {"publicKey": self.public_key})
            result = {"result": {"signedToken": "fixture-multiplayer"}}

        replacement = self.override.get(stage)
        if isinstance(replacement, Exception):
            raise replacement
        if replacement is not None:
            return replacement
        return httpx.Response(200, json=result)

    def _verify_xbox_proof(self, request, jwk):
        self.test.assertEqual(request.headers.get("x-xbl-contract-version"), "1")
        self.test.assertEqual(set(jwk), {"kty", "crv", "alg", "use", "x", "y"})
        self.test.assertEqual((jwk["kty"], jwk["crv"], jwk["alg"], jwk["use"]), ("EC", "P-256", "ES256", "sig"))
        if self.proof is None:
            self.proof = jwk
        self.test.assertEqual(jwk, self.proof)
        coordinates = [base64.urlsafe_b64decode(jwk[axis] + "=") for axis in ("x", "y")]
        self.test.assertEqual(tuple(map(len, coordinates)), (32, 32))
        numbers = [int.from_bytes(coordinate, "big") for coordinate in coordinates]
        public = ec.EllipticCurvePublicNumbers(*numbers, ec.SECP256R1()).public_key()

        signature = base64.b64decode(request.headers["Signature"], validate=True)
        self.test.assertEqual(len(signature), 76)
        version, filetime = struct.unpack_from(">IQ", signature)
        self.test.assertEqual((version, filetime), (1, 133444736001234567))
        raw_r, raw_s = signature[12:44], signature[44:76]
        signed = (signature[:4] + b"\0" + signature[4:12] + b"\0POST\0"
                  + request.url.path.encode("ascii") + b"\0\0" + request.content + b"\0")
        public.verify(encode_dss_signature(int.from_bytes(raw_r, "big"), int.from_bytes(raw_s, "big")),
                      signed, ec.ECDSA(hashes.SHA256()))


class PhysicalMemoryTests(unittest.TestCase):
    def test_windows_reads_total_not_available_physical_memory(self):
        class WindowsError(Exception):
            pass

        status = Mock(return_value={"TotalPhys": MEMORY_BYTES, "AvailPhys": 1024**3})
        with patch.dict(sys.modules, {"win32api": SimpleNamespace(GlobalMemoryStatusEx=status, error=WindowsError)}):
            self.assertEqual(_physical_memory_bytes("Windows"), MEMORY_BYTES)
            status.side_effect = WindowsError()
            self.assertIsNone(_physical_memory_bytes("Windows"))

    def test_linux_uses_physical_pages_and_their_size(self):
        values = {"SC_PHYS_PAGES": MEMORY_BYTES // 4096, "SC_PAGE_SIZE": 4096}
        with patch("os.sysconf", create=True, side_effect=values.__getitem__) as sysconf:
            self.assertEqual(_physical_memory_bytes("Linux"), MEMORY_BYTES)
            values["SC_PHYS_PAGES"] = -1
            self.assertIsNone(_physical_memory_bytes("Linux"))
            sysconf.side_effect = OSError()
            self.assertIsNone(_physical_memory_bytes("Linux"))

    def test_macos_uses_the_complete_native_64_bit_value(self):
        def sysctl(name, value, length, incoming, incoming_length):
            self.assertEqual((name, incoming, incoming_length), (b"hw.memsize", None, 0))
            size = ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))
            self.assertEqual(size.contents.value, 8)
            size.contents.value = returned_size
            ctypes.cast(value, ctypes.POINTER(ctypes.c_uint64)).contents.value = MEMORY_BYTES
            return result

        native = Mock(side_effect=sysctl)
        with patch("ctypes.CDLL", return_value=SimpleNamespace(sysctlbyname=native)):
            for result, returned_size, expected in ((0, 8, MEMORY_BYTES), (0, 4, None), (-1, 8, None)):
                with self.subTest(result=result, returned_size=returned_size):
                    self.assertEqual(_physical_memory_bytes("Darwin"), expected)


class WindowsStorePackageTests(unittest.TestCase):
    def test_requires_a_successful_store_origin_for_the_official_family(self):
        package = "Microsoft.MinecraftUWP_1.2.3.4_x64__8wekyb3d8bbwe"

        def get_packages(family, count, names, length, buffer):
            self.assertEqual(family, "Microsoft.MinecraftUWP_8wekyb3d8bbwe")
            if names is None:
                self.assertIsNone(buffer)
                count._obj.value = int(lookup_status == 122)
                length._obj.value = len(package) + 1
                return lookup_status
            if read_status == 0:
                buffer.value = package
                names[0] = ctypes.cast(buffer, ctypes.c_wchar_p)
            return read_status

        def get_origin(name, origin):
            self.assertEqual(name, package)
            origin._obj.value = origin_value
            return origin_status

        lookup = Mock(side_effect=get_packages)
        origin = Mock(side_effect=get_origin)
        api = SimpleNamespace(kernel32=SimpleNamespace(GetPackagesByPackageFamily=lookup),
                              kernelbase=SimpleNamespace(GetStagedPackageOrigin=origin))
        with patch("ctypes.windll", api, create=True):
            for lookup_status, read_status, origin_status, origin_value, expected in (
                (122, 0, 0, 3, True),
                (122, 0, 0, 5, False),
                (122, 0, 87, 3, False),
                (122, 122, 0, 3, False),
                (0, 0, 0, 3, False),
                (5, 0, 0, 3, False),
            ):
                with self.subTest(lookup=lookup_status, read=read_status, origin=origin_status, value=origin_value):
                    lookup.reset_mock()
                    origin.reset_mock()
                    self.assertIs(_has_windows_minecraft_store_package(), expected)
                    self.assertEqual(lookup.call_count, 2 if lookup_status == 122 else 1)
                    self.assertEqual(origin.call_count, int(lookup_status == 122 and read_status == 0))

    def test_unavailable_package_api_is_unknown(self):
        with patch("ctypes.windll", SimpleNamespace(), create=True):
            self.assertFalse(_has_windows_minecraft_store_package())


class XboxExchangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.public_key = _spki(ec.SECP384R1())
        self.enterContext(patch("endpilot.xbox._exchange.time.time_ns", return_value=1700000000123456700))
        self.platform = self.enterContext(patch("endpilot.xbox._exchange.platform"))
        self.platform.system.return_value = "Windows"
        self.platform.release.return_value = "11"
        self.memory = self.enterContext(patch("endpilot.xbox._exchange._physical_memory_bytes", return_value=MEMORY_BYTES))
        self.store_package = self.enterContext(patch(
            "endpilot.xbox._exchange._has_windows_minecraft_store_package", return_value=False,
        ))

    def client(self, service):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(service.serve), follow_redirects=True,
            auth=("fixture-default-user", "fixture-default-password"),
            headers={"Cookie": "default-cookie=secret", "X-Default-Secret": "fixture-client-secret"},
        )

    async def test_authorized_chain_and_multiplayer_use_the_same_spki_and_distinct_xsts(self):
        self.store_package.return_value = True
        service = XboxService(self, self.public_key, store=True)
        async with self.client(service) as client:
            credentials = await exchange(client, ACCESS_TOKEN, self.public_key)
        self.assertEqual(credentials, BedrockCredentials(("fixture-intermediate", "fixture-leaf"), "fixture-multiplayer"))
        self.assertEqual(service.calls, [stage for stage, _ in ROUTES])
        self.store_package.assert_called_once()

    async def test_unclassified_host_omits_optional_windows_fields(self):
        self.platform.system.return_value = "Linux"
        self.store_package.return_value = True
        service = XboxService(self, self.public_key, windows=False)
        async with self.client(service) as client:
            await exchange(client, ACCESS_TOKEN, self.public_key)
        self.assertEqual(service.calls, [stage for stage, _ in ROUTES])
        self.memory.assert_called_once_with("Linux")
        self.store_package.assert_not_called()

    async def test_unavailable_memory_is_omitted(self):
        self.memory.return_value = None
        service = XboxService(self, self.public_key, memory=None)
        async with self.client(service) as client:
            await exchange(client, ACCESS_TOKEN, self.public_key)
        self.assertEqual(service.calls, [stage for stage, _ in ROUTES])

    async def test_invalid_session_key_prevents_any_request(self):
        service = XboxService(self, self.public_key)
        async with self.client(service) as client:
            for bad_key in (_spki(ec.SECP256R1()), "invalid-base64!"):
                with self.subTest(key_length=len(bad_key)), self.assertRaises(ExchangeError) as failure:
                    await exchange(client, ACCESS_TOKEN, bad_key)
                self.assertEqual(failure.exception.stage, "input")
        self.assertEqual(service.calls, [])

    async def test_rejection_stops_at_its_stage_and_never_follows_a_redirect(self):
        for stage, status in (("xbox-user", 307), ("xsts-playfab", 403), ("minecraft-multiplayer-token", 401)):
            with self.subTest(stage=stage):
                secret = "fixture-do-not-print-response"
                service = XboxService(self, self.public_key)
                service.override[stage] = httpx.Response(
                    status, headers={"Location": "https://redirect.invalid/" + secret}, json={"detail": secret},
                )
                async with self.client(service) as client:
                    with self.assertRaises(ExchangeError) as failure:
                        await exchange(client, ACCESS_TOKEN, self.public_key)
                self.assertEqual((failure.exception.stage, failure.exception.status_code), (stage, status))
                self.assertNotIn(secret, "".join(traceback.format_exception(failure.exception)))
                self.assertEqual(service.calls.count(stage), 1)
                self.assertEqual(service.calls[-1], stage)

    async def test_malformed_or_expired_credentials_do_not_reach_the_next_service(self):
        for stage, payload in (
            ("xbox-user", {"Token": "fixture-user", "NotAfter": "2000-01-01T00:00:00Z"}),
            ("xsts-bedrock", {"Token": "fixture-xsts", "NotAfter": FUTURE, "DisplayClaims": {"xui": []}}),
            ("xsts-playfab", _xbox_response("fixture-xsts", "invalid;user-hash")),
            ("playfab-login", {"data": {"SessionTicket": None}}),
            ("minecraft-session", {"result": {"authorizationHeader": "unsafe\r\nheader", "validUntil": FUTURE}}),
            ("minecraft-session", {"result": {"authorizationHeader": AUTH_HEADER, "validUntil": "2000-01-01T00:00:00Z"}}),
            ("bedrock-chain", {"chain": ["fixture-intermediate"]}),
            ("bedrock-chain", {"chain": ["fixture-intermediate", ""]}),
            ("minecraft-multiplayer-token", {"result": {"signedToken": False}}),
        ):
            with self.subTest(stage=stage):
                service = XboxService(self, self.public_key)
                service.override[stage] = httpx.Response(200, json=payload)
                async with self.client(service) as client:
                    with self.assertRaises(ExchangeError) as failure:
                        await exchange(client, ACCESS_TOKEN, self.public_key)
                self.assertEqual((failure.exception.stage, service.calls[-1]), (stage, stage))

    async def test_transport_and_json_errors_disclose_only_fixed_diagnostics(self):
        for response in (httpx.ReadError(ACCESS_TOKEN), httpx.Response(200, content=ACCESS_TOKEN)):
            service = XboxService(self, self.public_key)
            service.override["xbox-user"] = response
            async with self.client(service) as client:
                with self.assertRaises(ExchangeError) as failure:
                    await exchange(client, ACCESS_TOKEN, self.public_key)
            self.assertEqual(failure.exception.stage, "xbox-user")
            self.assertNotIn(ACCESS_TOKEN, "".join(traceback.format_exception(failure.exception)))
            self.assertEqual(service.calls, ["xbox-user"])

    async def test_cancelled_exchange_releases_http_wait_and_a_retry_has_a_fresh_proof(self):
        first = service = XboxService(self, self.public_key)
        waiting, released = asyncio.Event(), asyncio.Event()

        async def handle(request):
            response = await service.serve(request)
            if service is first and service.calls == ["xbox-user", "xsts-bedrock"]:
                waiting.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    released.set()
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(exchange(client, ACCESS_TOKEN, self.public_key))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(released.is_set())
                self.assertEqual(first.calls, ["xbox-user", "xsts-bedrock"])
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            service = XboxService(self, self.public_key)
            await exchange(client, ACCESS_TOKEN, self.public_key)
            self.assertNotEqual(service.proof, first.proof)
            self.assertEqual(service.calls, [stage for stage, _ in ROUTES])
