"""Exercise public online credential joins against local protocol peers."""

import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
from importlib.util import find_spec
import json
import struct
import time
import unittest
from unittest.mock import patch
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from endpilot import BedrockCredentials, Bot, LoginRefused, Skin
from endpilot.protocol import Protocol, read_batch, write_batch
from endpilot.qa import PacketTimeline


PROFILE = "LocalAuthPlayer"
XUID = "1234567890123456"
PLAYER_UUID = UUID(bytes=hashlib.md5(b"pocket-auth-1-xuid:" + XUID.encode()).digest(), version=3)
ISSUER = "https://127.0.0.1/test-issuer/"
MULTIPLAYER_AUDIENCE = "api://auth-minecraft-services/multiplayer"
WIN32_DEVICE_OS = 8
RUNTIME_ID = 41
ACTOR_ID = 73
SPAWN = (4.5, 65.0, -3.5)
LOCALE = "fr_CA"


def der64(public):
    return base64.b64encode(public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )).decode("ascii")


def url64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unurl(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(key, header, claims):
    """Local authority's compact JWS encoder, independent of the SDK signers."""
    signed = ".".join(url64(json.dumps(value).encode()) for value in (header, claims))
    if isinstance(key, ec.EllipticCurvePrivateKey):
        r, s = decode_dss_signature(key.sign(signed.encode(), ec.ECDSA(hashes.SHA384())))
        signature = r.to_bytes(48, "big") + s.to_bytes(48, "big")
    else:
        signature = key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signed + "." + url64(signature)


def ec_token(key, claims):
    return issue(key, {"alg": "ES384", "x5u": der64(key.public_key())}, claims)


def verify_es384(public, signed, encoded_signature):
    signature = unurl(encoded_signature)
    assert len(signature) == 96
    r, s = (int.from_bytes(part, "big") for part in (signature[:48], signature[48:]))
    public.verify(encode_dss_signature(r, s), signed.encode(), ec.ECDSA(hashes.SHA384()))


def claims_from(token, public):
    header64, claims64, signature64 = token.split(".")
    header = json.loads(unurl(header64))
    signed = header64 + "." + claims64
    if isinstance(public, ec.EllipticCurvePublicKey):
        assert header["alg"] == "ES384" and header["x5u"] == der64(public)
        verify_es384(public, signed, signature64)
    else:
        assert header["alg"] == "RS256" and header["kid"] == "local-authority"
        public.verify(unurl(signature64), signed.encode(), padding.PKCS1v15(), hashes.SHA256())
    return json.loads(unurl(claims64))


def verify_validity(claims):
    start, end = claims.get("nbf"), claims.get("exp")
    assert type(start) is int and type(end) is int and start <= time.time() < end, (
        "credential is outside its validity interval"
    )


@dataclass(frozen=True)
class Issued:
    public: ec.EllipticCurvePublicKey
    spki: str
    chain: tuple[str, ...]
    token: str


class LocalAuthority:
    def __init__(self):
        self.root = ec.generate_private_key(ec.SECP384R1())
        self.delegate = ec.generate_private_key(ec.SECP384R1())
        self.multiplayer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.issued = []
        self.wrong = None
        self.client_data = None

    async def provide(self, spki):
        public = serialization.load_der_public_key(base64.b64decode(spki, validate=True))
        assert isinstance(public, ec.EllipticCurvePublicKey) and isinstance(public.curve, ec.SECP384R1)
        assert der64(public) == spki
        wrong_spki = der64(self.root.public_key())
        assert spki != wrong_spki
        interval = {"iat": int(time.time()), "nbf": int(time.time()) - 15, "exp": int(time.time()) + 300}
        chain = (
            ec_token(self.root, {**interval, "iss": "Mojang", "certificateAuthority": True,
                                 "identityPublicKey": der64(self.delegate.public_key())}),
            ec_token(self.delegate, {**interval, "iss": "Mojang",
                                     "identityPublicKey": wrong_spki if self.wrong == "chain" else spki,
                                     "extraData": {"displayName": PROFILE, "identity": str(PLAYER_UUID), "XUID": XUID}}),
        )
        token = issue(self.multiplayer, {"alg": "RS256", "kid": "local-authority"}, {
            **interval, "iss": ISSUER, "aud": MULTIPLAYER_AUDIENCE,
            "cpk": wrong_spki if self.wrong == "token" else spki,
            "xid": XUID, "xname": PROFILE, "ipt": "PlayFab", "mid": "12abcdef", "tid": "20CA2",
        })
        self.issued.append(Issued(public, spki, chain, token))
        return BedrockCredentials(chain, token)

    def verify_multiplayer_token(self, token):
        claims = claims_from(token, self.multiplayer.public_key())
        assert claims.get("iss") == ISSUER, "multiplayer token has an untrusted issuer"
        assert claims.get("aud") == MULTIPLAYER_AUDIENCE, "multiplayer token has the wrong audience"
        verify_validity(claims)
        return claims

    def verify_login(self, request, address):
        issued = self.issued[-1]
        auth_size, = struct.unpack_from("<i", request)
        auth_end = 4 + auth_size
        assert auth_size > 0 and auth_end + 4 < len(request)
        auth = json.loads(request[4:auth_end])
        client_size, = struct.unpack_from("<i", request, auth_end)
        assert client_size > 0 and auth_end + 4 + client_size == len(request)
        assert auth["AuthenticationType"] == 0
        identity = self.verify_multiplayer_token(auth["Token"])
        assert auth["Token"] == issued.token, "the wire multiplayer token differs from the issued token"
        supplied = json.loads(auth["Certificate"])["chain"]
        bridge, root, profile = supplied
        assert (root, profile) == issued.chain, "the issued certificate chain was not preserved"
        proof = claims_from(bridge, issued.public)
        verify_validity(proof)
        assert proof["certificateAuthority"] is True
        assert proof["identityPublicKey"] == der64(self.root.public_key())
        root_data = claims_from(root, self.root.public_key())
        assert root_data["identityPublicKey"] == der64(self.delegate.public_key())
        profile_data = claims_from(profile, self.delegate.public_key())
        for claims in (root_data, profile_data):
            assert claims.get("iss") == "Mojang", "certificate has an untrusted issuer"
            verify_validity(claims)
        assert profile_data["identityPublicKey"] == issued.spki
        assert profile_data["extraData"] == {"displayName": PROFILE, "identity": str(PLAYER_UUID), "XUID": XUID}
        assert (identity["cpk"], identity["xname"], identity["xid"]) == (issued.spki, PROFILE, XUID)
        client = claims_from(request[auth_end + 4:].decode(), issued.public)
        assert client["ThirdPartyName"] == PROFILE
        assert type(client["DeviceOS"]) is int and client["DeviceOS"] == WIN32_DEVICE_OS, (
            "online login did not advertise Win32 DeviceOS"
        )
        assert client["ServerAddress"] == address and client["LanguageCode"] == LOCALE
        assert client["GameVersion"] == "1.26.66" and client["MaxViewDistance"] == 7
        self.client_data = client
        return issued

    def verify_offer(self, offer):
        issued = self.issued[-1]
        identity = offer.identity
        assert identity is not None and identity.protocol == "default" and identity.domain == ISSUER
        claims = self.verify_multiplayer_token(identity.token)
        assert identity.token == issued.token, "the real SDP offer did not carry the matching issued token"
        assert claims["cpk"] == issued.spki
        fingerprints = []
        for line in offer.sdp.splitlines():
            if line.startswith("a=fingerprint:"):
                algorithm, digest = line.removeprefix("a=fingerprint:").split(" ", 1)
                fingerprints.append({"algorithm": algorithm, "digest": digest})
        assert fingerprints, "the offer had no DTLS fingerprints"
        body = json.dumps({"fingerprint": fingerprints}, sort_keys=True, separators=(",", ":")).encode()
        header, detached, signature = identity.fingerprints.split(".")
        assert detached == "" and json.loads(unurl(header)) == {"alg": "ES384"}
        verify_es384(issued.public, header + "." + url64(body), signature)
        return issued.spki


class BedrockPeer:
    """Decode genuine transport traffic, with a separate RakNet-side cipher."""

    def __init__(self, connection, is_raknet):
        self.connection = connection
        self.is_raknet = is_raknet
        self.protocol = Protocol(2225)
        self.pending = deque()
        self.key = None
        self.sent = self.received = 0

    def enable_cipher(self, key):
        self.key = key
        cipher = Cipher(algorithms.AES(key), modes.GCM(key[:12]))
        self.outbound = cipher.encryptor()
        self.inbound = cipher.decryptor()

    def checksum(self, counter, payload):
        return hashlib.sha256(counter.to_bytes(8, "little") + payload + self.key).digest()[:8]

    async def send(self, *packets):
        frame = write_batch(self.protocol.encode(packet) for packet in packets)
        if self.is_raknet:
            if self.key is not None:
                frame = self.outbound.update(frame + self.checksum(self.sent, frame))
                self.sent += 1
            frame = b"\xfe" + frame
        await self.connection.send(frame)

    async def expect(self, cls, *, after_spawn=False):
        while True:
            while not self.pending:
                frame = await self.connection.recv()
                if self.is_raknet:
                    assert frame[:1] == b"\xfe", "missing RakNet Bedrock envelope"
                    frame = frame[1:]
                    if self.key is not None:
                        payload = self.inbound.update(frame)
                        frame, checksum = payload[:-8], payload[-8:]
                        assert frame and checksum == self.checksum(self.received, frame)
                        self.received += 1
                self.pending.extend(read_batch(frame))
            packet = self.protocol.decode(self.pending.popleft())
            if not after_spawn or not isinstance(packet, self.protocol.binding.PlayerAuthInputPacket):
                assert isinstance(packet, cls), f"expected {cls.__name__}, received {type(packet).__name__}"
                return packet


@dataclass(frozen=True)
class SettingsGate:
    request_seen: asyncio.Event = field(default_factory=asyncio.Event)
    release_reply: asyncio.Event = field(default_factory=asyncio.Event)
    checked: asyncio.Event = field(default_factory=asyncio.Event)


class LocalServer:
    def __init__(self, authority, transport, *, reject=False, pause_after_login=False, settings_gate=None):
        self.authority = authority
        self.transport = transport
        self.reject = reject
        self.pause_after_login = pause_after_login
        self.settings_gate = settings_gate
        self.connections = 0
        self.offer_keys = []
        self.login_keys = []
        self.login_seen = asyncio.Event()
        self.initialized = asyncio.Event()
        self.post_spawn_checked = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.failure = None
        self.verified_ciphertext = False
        self.skin_update = None

    def offer(self, offer):
        try:
            self.offer_keys.append(self.authority.verify_offer(offer))
        except Exception as error:
            self.failure = error
            raise

    async def handle(self, connection):
        self.connections += 1
        wire = BedrockPeer(connection, self.transport == "raknet")
        b = wire.protocol.binding
        try:
            request = await wire.expect(b.RequestNetworkSettingsPacket)
            assert request.client_network_version == 2225
            if self.settings_gate is not None:
                await self.check_gated_settings(wire)
                return
            await wire.send(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
            login = await wire.expect(b.LoginPacket)
            assert login.client_network_version == 2225
            issued = self.authority.verify_login(login.connection_request, self.address)
            self.login_keys.append(issued.spki)
            if self.transport == "nethernet":
                assert self.offer_keys == self.login_keys
            self.login_seen.set()
            if self.reject:
                await wire.send(b.DisconnectPacket(reason=b.DisconnectFailReason.KICKED,
                                                   messages=b.DisconnectPacketMessages(message="local test refusal")))
                await self.wait_for_close(connection)
                return
            if self.pause_after_login:
                await self.wait_for_close(connection)
                return

            server_key = ec.generate_private_key(ec.SECP384R1())
            salt = b"local-only login handshake"
            token = ec_token(server_key, {"salt": base64.b64encode(salt).decode("ascii")})
            await wire.send(b.ServerToClientHandshakePacket(token=token))
            if self.transport == "raknet":
                shared = server_key.exchange(ec.ECDH(), issued.public)
                wire.enable_cipher(hashlib.sha256(salt + shared).digest())
            await wire.expect(b.ClientToServerHandshakePacket)
            self.verified_ciphertext = wire.received == 1

            response = b.ResourcePackClientResponsePacket
            await wire.send(b.PlayStatusPacket(status=b.PlayStatus.LOGIN_SUCCESS), b.ResourcePacksInfoPacket())
            downloaded = await wire.expect(response)
            assert isinstance(downloaded.response, response.DownloadingFinished)
            await wire.send(b.ResourcePackStackPacket())
            completed = await wire.expect(response)
            assert isinstance(completed.response, response.ResourcePackStackFinished)

            await wire.send(b.StartGamePacket(runtime_id=RUNTIME_ID, entity_id=ACTOR_ID,
                                              pos=b.Vec3(x=SPAWN[0], y=SPAWN[1], z=SPAWN[2])))
            radius = await wire.expect(b.RequestChunkRadiusPacket)
            assert radius.chunk_radius == 7
            await wire.send(b.ChunkRadiusUpdatedPacket(chunk_radius=7),
                            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
            loading = b.ServerboundLoadingScreenPacketType
            for phase in (loading.START_LOADING_SCREEN, loading.END_LOADING_SCREEN):
                packet = await wire.expect(b.ServerboundLoadingScreenPacket)
                assert packet.serverbound_loading_screen_packet_type == phase
            initialized = await wire.expect(b.SetLocalPlayerAsInitializedPacket)
            assert initialized.player_id == RUNTIME_ID
            await wire.send(b.MovePlayerPacket(player_id=RUNTIME_ID,
                                              pos=b.Vec3(x=SPAWN[0], y=SPAWN[1], z=SPAWN[2])))
            self.initialized.set()
            command = await wire.expect(b.CommandRequestPacket, after_spawn=True)
            assert command.command == "/list"
            assert command.origin.uuid == PLAYER_UUID and command.origin.player_id == ACTOR_ID
            skin = await wire.expect(b.PlayerSkinPacket, after_spawn=True)
            assert skin.uuid == PLAYER_UUID
            self.skin_update = skin
            self.post_spawn_checked.set()
            await self.wait_for_close(connection)
        except Exception as error:
            self.failure = error
            self.login_seen.set()
            self.initialized.set()
            self.post_spawn_checked.set()
            self.disconnected.set()

    async def check_gated_settings(self, wire):
        from nethernet import ConnectionClosed as NetherNetClosed
        from raknet import ConnectionClosed as RakNetClosed

        gate = self.settings_gate
        gate.request_seen.set()
        await gate.release_reply.wait()
        b = wire.protocol.binding
        try:
            await wire.send(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
            await wire.expect(b.LoginPacket)
        except (NetherNetClosed, RakNetClosed):
            self.disconnected.set()
        else:
            # Only the packet kind is retained for the abandoned connection.
            self.login_seen.set()
        finally:
            gate.checked.set()

    async def wait_for_close(self, connection):
        if self.transport == "raknet":
            from raknet import ConnectionClosed
        else:
            from nethernet import ConnectionClosed
        try:
            while True:
                await connection.recv()
        except ConnectionClosed:
            self.disconnected.set()

    @asynccontextmanager
    async def listen(self):
        # Bound the full scenario, including time outside the SDK's join timeout.
        async with asyncio.timeout(45):
            if self.transport == "raknet":
                from raknet.asyncio import create_server

                async with await create_server(("127.0.0.1", 0)) as server:
                    host, port = server.local_address
                    self.endpoint = host, port
                    self.address = f"{host}:{port}"

                    async def accept():
                        async with await server.accept() as connection:
                            await self.handle(connection)

                    running = asyncio.create_task(accept())
                    try:
                        yield self
                    finally:
                        if not running.done():
                            running.cancel()
                        await asyncio.gather(running, return_exceptions=True)
            else:
                from aioice import ice
                from aiortc import RTCIceGatherer
                from nethernet import serve_http

                # Keep both the WebRTC data path and HTTP signaling on loopback.
                with (
                    patch.object(RTCIceGatherer, "getDefaultIceServers", return_value=[]),
                    patch.object(ice, "get_host_addresses", return_value=["127.0.0.1"]),
                ):
                    async with serve_http(self.handle, host="127.0.0.1", validate_offer=self.offer,
                                          ice_servers=[]) as server:
                        self.address = f"http://127.0.0.1:{server.bound_port}"
                        self.endpoint = (self.address,)
                        yield self

    async def join(self, bot, timeline=None, *, provider=None):
        join = getattr(bot, f"join_{self.transport}")
        await join(*self.endpoint, credential_provider=provider or self.authority.provide,
                   locale=LOCALE, chunk_radius=7, timeline=timeline)

    async def wait(self, event, *, timeout=3):
        await asyncio.wait_for(event.wait(), timeout)
        if self.failure is not None:
            raise self.failure


@unittest.skipUnless(find_spec("raknet") and find_spec("nethernet"), "install RakNet and NetherNet extras")
class OnlineJoinTests(unittest.IsolatedAsyncioTestCase):
    async def check_spawn(self, bot, peer, skin_update="local-profile"):
        timeline = PacketTimeline()
        offline = bot.identity
        try:
            await peer.join(bot, timeline)
            await peer.wait(peer.initialized)
            self.assertEqual(await bot.wait_for_position_sync(timeout=3), SPAWN)
            self.assertEqual((bot.player_id, bot.position, bot.spawn_chunk_radius), (RUNTIME_ID, SPAWN, 7))
            self.assertEqual((bot.identity.name, bot.identity.uuid), (PROFILE, PLAYER_UUID))
            self.assertEqual(timeline.sent_count("LoginPacket"), 1)
            self.assertEqual(timeline.received_count("ServerToClientHandshakePacket"), 1)
            self.assertEqual(timeline.sent_count("ClientToServerHandshakePacket"), 1)
            self.assertEqual(timeline.sent_count("SetLocalPlayerAsInitializedPacket"), 1)
            await bot.send_command("list")
            await bot.change_skin(skin_update)
            await peer.wait(peer.post_spawn_checked, timeout=10)
        except Exception:
            if peer.failure is not None:
                raise peer.failure
            raise
        finally:
            await bot.close()
        await peer.wait(peer.disconnected)
        self.assertIs(bot.identity, offline)

    async def test_online_public_join_proves_key_and_spawns_over_both_transports(self):
        for transport in ("raknet", "nethernet"):
            with self.subTest(transport=transport):
                authority = LocalAuthority()
                bot = Bot("LocalFallback")
                self.addAsyncCleanup(bot.close)
                async with LocalServer(authority, transport).listen() as peer:
                    await self.check_spawn(bot, peer)
                    self.assertEqual(peer.connections, 1)
                    self.assertEqual(len(authority.issued), 1)
                    self.assertEqual(peer.login_keys, [authority.issued[0].spki])
                    self.assertEqual(peer.verified_ciphertext, transport == "raknet")
                    if transport == "nethernet":
                        self.assertEqual(peer.offer_keys, peer.login_keys)

    async def test_online_custom_skin_is_signed_by_the_session_key_on_both_transports(self):
        pixels = b"\x01\x2b\xa9\x00\x55\x7b\x09\xaf" * (128 * 64)
        skin = Skin(pixels, model="slim", width=128, height=128)
        geometry = "geometry.humanoid.customSlim"
        for transport in ("raknet", "nethernet"):
            with self.subTest(transport=transport):
                authority = LocalAuthority()
                bot = Bot("LocalFallback", skin=skin)
                offline_uuid = bot.identity.uuid
                async with LocalServer(authority, transport).listen() as peer:
                    await self.check_spawn(bot, peer, skin)
                    client = authority.client_data
                    packet = peer.skin_update
                    self.assertEqual((client["SkinImageWidth"], client["SkinImageHeight"]), (128, 128))
                    self.assertEqual(client["ArmSize"], "slim")
                    self.assertEqual(json.loads(base64.b64decode(client["SkinResourcePatch"]))["geometry"]["default"],
                                     geometry)
                    self.assertEqual(base64.b64decode(client["SkinData"]), pixels)
                    self.assertEqual((packet.uuid, packet.skin.id), (PLAYER_UUID, client["SkinId"]))
                    self.assertNotEqual(packet.uuid, offline_uuid)
                    self.assertEqual(packet.skin.arm_size_type, bot.protocol.binding.ArmSizeType.SLIM)
                    image = packet.skin.skin_image
                    self.assertEqual((image.width, image.height), (128, 128))
                    self.assertEqual(image.image_bytes, pixels)

    async def test_mismatched_issuer_key_is_rejected_before_transport_and_new_key_can_retry(self):
        for transport, mismatches in (("raknet", ("token", "chain")), ("nethernet", ("token", "chain"))):
            with self.subTest(transport=transport):
                authority = LocalAuthority()
                bot = Bot("LocalFallback")
                self.addAsyncCleanup(bot.close)
                async with LocalServer(authority, transport).listen() as peer:
                    for wrong in mismatches:
                        with self.subTest(bound_to_wrong_key=wrong):
                            authority.wrong = wrong
                            with self.assertRaises(ValueError):
                                await peer.join(bot)
                            self.assertEqual(peer.connections, 0)
                            self.assertEqual(peer.offer_keys, [])
                            with self.assertRaisesRegex(RuntimeError, "has not joined"):
                                _ = bot.player_id
                    authority.wrong = None
                    await self.check_spawn(bot, peer)
                    self.assertEqual(len(authority.issued), 3)
                    self.assertEqual(len({row.spki for row in authority.issued}), 3)

    async def test_refusal_closes_open_transport_and_same_bot_retries_with_new_key(self):
        for transport in ("raknet", "nethernet"):
            with self.subTest(transport=transport):
                authority = LocalAuthority()
                bot = Bot("LocalFallback")
                self.addAsyncCleanup(bot.close)
                async with LocalServer(authority, transport, reject=True).listen() as denied:
                    with self.assertRaisesRegex(LoginRefused, "local test refusal"):
                        await denied.join(bot)
                    await denied.wait(denied.disconnected)
                    self.assertFalse(denied.initialized.is_set())
                    self.assertEqual(denied.login_keys, [authority.issued[0].spki])
                with self.assertRaisesRegex(RuntimeError, "has not joined"):
                    _ = bot.player_id
                async with LocalServer(authority, transport).listen() as retry:
                    await self.check_spawn(bot, retry)
                    self.assertNotEqual(denied.login_keys, retry.login_keys)
                    self.assertEqual(len(authority.issued), 2)

    async def test_cancel_provider_ends_attempt_without_dial_and_same_bot_can_retry(self):
        authority = LocalAuthority()
        bot = Bot("LocalFallback")
        self.addAsyncCleanup(bot.close)
        entered = asyncio.Event()
        cleared = asyncio.Event()
        abandoned = []

        async def suspended(spki):
            abandoned.append(spki)
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleared.set()

        async with LocalServer(authority, "nethernet").listen() as peer:
            joining = asyncio.create_task(peer.join(bot, provider=suspended))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                joining.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await joining
                self.assertTrue(cleared.is_set())
                self.assertEqual((peer.connections, peer.offer_keys), (0, []))
                with self.assertRaisesRegex(RuntimeError, "has not joined"):
                    _ = bot.player_id
                await self.check_spawn(bot, peer)
                self.assertNotEqual(abandoned, peer.login_keys)
            finally:
                if not joining.done():
                    joining.cancel()
                    await asyncio.gather(joining, return_exceptions=True)

    async def test_cancel_after_login_closes_transport_without_initializing_player(self):
        authority = LocalAuthority()
        bot = Bot("LocalFallback")
        self.addAsyncCleanup(bot.close)
        async with LocalServer(authority, "raknet", pause_after_login=True).listen() as peer:
            joining = asyncio.create_task(peer.join(bot))
            try:
                await peer.wait(peer.login_seen)
                joining.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await joining
                await peer.wait(peer.disconnected)
                self.assertEqual(peer.login_keys, [authority.issued[0].spki])
                self.assertFalse(peer.initialized.is_set())
                with self.assertRaisesRegex(RuntimeError, "has not joined"):
                    _ = bot.player_id
            finally:
                if not joining.done():
                    joining.cancel()
                    await asyncio.gather(joining, return_exceptions=True)

    async def test_close_during_network_settings_prevents_old_login_and_allows_retry(self):
        for transport in ("raknet", "nethernet"):
            with self.subTest(transport=transport):
                authority = LocalAuthority()
                bot = Bot("LocalFallback")
                self.addAsyncCleanup(bot.close)
                gate = SettingsGate()
                timeline = PacketTimeline()
                async with LocalServer(authority, transport, settings_gate=gate).listen() as abandoned:
                    joining = asyncio.create_task(abandoned.join(bot, timeline))
                    try:
                        await abandoned.wait(gate.request_seen)
                        self.assertEqual(abandoned.connections, 1)
                        self.assertEqual(len(authority.issued), 1)
                        await asyncio.wait_for(bot.close(), 3)
                        gate.release_reply.set()
                        await abandoned.wait(gate.checked)
                        self.assertFalse(abandoned.login_seen.is_set(), "closed connection sent a LoginPacket")
                        await abandoned.wait(abandoned.disconnected)
                        with self.assertRaises((ConnectionError, asyncio.CancelledError)):
                            await asyncio.wait_for(asyncio.shield(joining), 3)
                        self.assertEqual(timeline.sent_count("LoginPacket"), 0)
                    finally:
                        gate.release_reply.set()
                        if not joining.done():
                            joining.cancel()
                        await asyncio.gather(joining, return_exceptions=True)

                async with LocalServer(authority, transport).listen() as replacement:
                    await self.check_spawn(bot, replacement)
                    self.assertEqual(replacement.connections, 1)
                    self.assertEqual(len(authority.issued), 2)
                    self.assertTrue(authority.issued[0].spki != authority.issued[1].spki,
                                    "replacement join reused the abandoned client's key")
                    self.assertTrue(replacement.login_keys == [authority.issued[1].spki],
                                    "replacement connection did not log in with the fresh key")
