import asyncio
import base64
import builtins
import hashlib
import json
import sys
import zlib
from pathlib import Path

import bedrock_protocol as bp
import pytest
from bedrock_protocol.wire import BatchCodec, Compression
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from peer import Peer, submit, until

import endpilot as api
from endpilot import transport


def _adapter():
    pytest.importorskip("raknet")
    from endpilot import raknet_transport

    return raknet_transport


def _spki(jwt):
    encoded = jwt.split(".")[0]
    header = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    return base64.b64decode(header["x5u"])


def _make_offer(public, server_key):
    der = server_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    salt = b"RakNet test peer salt"
    header = {"alg": "ES384", "x5u": base64.b64encode(der).decode()}
    body = {"salt": base64.b64encode(salt).decode()}
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    data = encode(json.dumps(header).encode()) + b"." + encode(json.dumps(body).encode())
    r, s = utils.decode_dss_signature(server_key.sign(data, ec.ECDSA(hashes.SHA384())))
    token = (data + b"." + encode(r.to_bytes(48, "big") + s.to_bytes(48, "big"))).decode()
    client_key = serialization.load_der_public_key(base64.b64decode(public))
    session_key = hashlib.sha256(salt + server_key.exchange(ec.ECDH(), client_key)).digest()
    return token, session_key


def _raknet_options(server_url="raknet://127.0.0.1:19132", **kwargs):
    return api.BotOptions(server_url=server_url, transport="raknet", **kwargs)


class EncryptedPeer(Peer):
    def __init__(
        self,
        keys,
        *args,
        trailing_handshake=None,
        pre_handshake_health=False,
        spawn_without_login=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.keys = keys
        self.session_key = None
        self.encrypted_client_wires = []
        self.handshake_token = None
        self.trailing_handshake = trailing_handshake
        self.pre_handshake_health = pre_handshake_health
        self.spawn_without_login = spawn_without_login

    def send(self, *packets):
        if self.pre_handshake_health and any(packet.packet_id == 143 for packet in packets):
            packets = (*packets, self.p.SetHealthPacket(health=99))
        if self.trailing_handshake == "login_success" and any(packet.packet_id == 3 for packet in packets):
            packets = (*packets, self.p.PlayStatusPacket(status=self.p.PlayStatus.LOGIN_SUCCESS))
        wire = self.batch.encode(packets)
        if self.trailing_handshake == "unknown" and any(packet.packet_id == 3 for packet in packets):
            # A valid unknown packet header stays in BatchCodec.headers even when
            # the application does not decode that packet body.
            if wire[0] == 0:
                inflater = zlib.decompressobj(wbits=-15)
                body = inflater.decompress(wire[1:]) + inflater.flush() + b"\x02\xfe\x07"
                compressor = zlib.compressobj(wbits=-15)
                wire = b"\x00" + compressor.compress(body) + compressor.flush()
            else:
                wire += b"\x02\xfe\x07"
        self.incoming.put_nowait(wire)
        for packet in packets:
            if packet.packet_id == 3:
                self.handshake_token = packet.token
                self.session_key = self.keys[packet.token]
                # Queue the server handshake in plaintext, then arm the independent
                # peer codec before the client can consume it or send packet 4.
                self.batch.enable_encryption(self.session_key)

    def spawn(self):
        if not self.spawn_without_login:
            return super().spawn()
        game = self.codec.decode_packet((Path(__file__).parent / "fixtures" / "start_game_2193.bin").read_bytes()).payload
        self.send(game, self.p.ItemRegistryPacket(items=[]))
        self.send(
            self.p.ChunkRadiusUpdatedPacket(chunk_radius=16),
            self.p.PlayStatusPacket(status=self.p.PlayStatus.PLAYER_SPAWN),
        )

    def accept(self, data):
        if self.session_key is not None:
            self.encrypted_client_wires.append(data)
        super().accept(data)


class RakNetPeers:
    def __init__(
        self,
        monkeypatch,
        *,
        server_handshake=True,
        compression_threshold=1,
        trailing_handshake=None,
        pre_handshake_health=False,
        spawn_without_login=False,
        bad_signature=False,
    ):
        self.keys = {}
        self.connections = []
        self.server_key = ec.generate_private_key(ec.SECP384R1())
        spki = self.server_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.pin = hashlib.sha256(spki).hexdigest()
        peer_module = sys.modules[Peer.__module__]
        def offer(public):
            token, key = _make_offer(public, self.server_key)
            if bad_signature:
                prefix, signature = token.rsplit(".", 1)
                raw_signature = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
                raw_signature[0] ^= 1
                damaged = base64.urlsafe_b64encode(raw_signature).rstrip(b"=").decode("ascii")
                token = prefix + "." + damaged
            self.keys[token] = key
            return token, key

        monkeypatch.setattr(peer_module, "offer", offer)

        async def open_connection(options):
            assert options.transport == "raknet"
            peer = EncryptedPeer(
                self.keys,
                options.protocol,
                server_handshake=server_handshake,
                compression_threshold=compression_threshold,
                trailing_handshake=trailing_handshake,
                pre_handshake_health=pre_handshake_health,
                spawn_without_login=spawn_without_login,
            )
            self.connections.append(peer)
            return peer.connection

        monkeypatch.setattr(_adapter(), "open_connection", open_connection)


def test_raknet_options_require_explicit_url_and_supported_protocol():
    for url in ("", "127.0.0.1:19132", "raknet://host", "raknet://host:0", "raknet://host:65536",
                "raknet://user@host:19132", "raknet://host:19132/path", "raknet://host:19132?x=1"):
        with pytest.raises(ValueError):
            _raknet_options(server_url=url)._validate()
    for protocol in (1001, 2193):
        _raknet_options(protocol=protocol)._validate()
    with pytest.raises(ValueError, match="supported protocols"):
        _raknet_options(protocol=1)._validate()
    with pytest.raises(ValueError, match="transport"):
        api.BotOptions(server_url="http://peer", transport="udp")._validate()


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("pinned", [False, True])
async def test_encrypted_login_spawn_and_reconnect_use_fresh_cipher(monkeypatch, protocol, pinned):
    peers = RakNetPeers(monkeypatch, compression_threshold=256)
    async with api.Runtime() as runtime:
        pin = peers.pin if pinned else ""
        bot = runtime.add_bot(_raknet_options(protocol=protocol, server_key_sha256=pin))
        session_keys = []
        for index in range(2):
            if index:
                await bot.reconnect()
            await bot.wait_ready(2)
            peer = peers.connections[-1]
            await until(lambda: any(packet.packet_id == 4 for packet in peer.received))
            assert bot.get_status().state == api.BotState.ready
            assert sum(packet.packet_id == 4 for packet in peer.received) == 1
            assert hashlib.sha256(_spki(peer.handshake_token)).hexdigest() == peers.pin
            session_keys.append(peer.session_key)

            before = len(peer.encrypted_client_wires)
            bot.report_blob_status(range(80))
            await until(lambda: any(packet.packet_id == 135 for packet in peer.received))
            assert len(peer.encrypted_client_wires) > before
            blob_status = next(packet for packet in peer.received if packet.packet_id == 135)
            assert len(peer.codec.encode_packet(blob_status)) > 256

            # Independently decode captured post-handshake client traffic with a
            # fresh native codec. A plaintext packet 4 or stale reconnect key fails.
            encrypted = BatchCodec(bp.Codec(protocol))
            encrypted.set_compression(Compression.ZLIB, 256)
            encrypted.enable_encryption(peer.session_key)
            decoded = [encrypted.decode(wire) for wire in peer.encrypted_client_wires]
            packet_ids = [packet.header.packet_id for batch in decoded for packet in batch]
            assert packet_ids[0] == 4
            assert 4 in packet_ids and 135 in packet_ids
            assert peer.encrypted_client_wires[0] != bytes.fromhex("ff0104")

            damaged = bytearray(peer.encrypted_client_wires[0])
            damaged[-1] ^= 1
            verifier = BatchCodec(bp.Codec(protocol))
            verifier.set_compression(Compression.ZLIB, 256)
            verifier.enable_encryption(peer.session_key)
            with pytest.raises(bp.ProtocolError, match="checksum mismatch"):
                verifier.decode(damaged)

        assert session_keys[0] != session_keys[1]
        await bot.disconnect()
    assert all(peer.closed for peer in peers.connections)


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_unpinned_plaintext_login_spawn_and_reconnect_without_handshake(monkeypatch, protocol):
    peers = RakNetPeers(monkeypatch, server_handshake=False, compression_threshold=256)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(_raknet_options(protocol=protocol))
        for index in range(2):
            if index:
                await bot.reconnect()
            await bot.wait_ready(2)
            peer = peers.connections[-1]
            assert bot.get_status().state == api.BotState.ready
            assert peer.handshake_token is None
            assert peer.session_key is None
            assert not any(packet.packet_id == 4 for packet in peer.received)
        await bot.disconnect()
    assert all(peer.closed for peer in peers.connections)


async def test_tampered_encrypted_server_batch_is_rejected(monkeypatch):
    peers = RakNetPeers(monkeypatch, compression_threshold=256)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(_raknet_options(server_key_sha256=peers.pin))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        await until(peer.incoming.empty)
        peer.send(peer.p.SetHealthPacket(health=19))
        damaged = bytearray(peer.incoming.get_nowait())
        damaged[-1] ^= 1
        peer.incoming.put_nowait(bytes(damaged))
        await until(lambda: peer.closed)
        assert bot.get_status().state == api.BotState.failed
        assert bot.get_status().health is None
        assert bot._session.reason


@pytest.mark.parametrize("trailing", ["login_success", "unknown"])
async def test_server_handshake_must_be_last_batch_header(monkeypatch, trailing):
    peers = RakNetPeers(monkeypatch, compression_threshold=65535, trailing_handshake=trailing)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(_raknet_options(server_key_sha256=peers.pin))
        with pytest.raises(RuntimeError):
            await bot.wait_ready(2)
        peer = peers.connections[-1]
        await bot.disconnect()
        assert peer.closed
        assert not any(packet.packet_id == 4 for packet in peer.received)
        assert bot.get_status().state == api.BotState.failed


@pytest.mark.parametrize("bypass", ["spawn_without_login", "pre_handshake_health"])
async def test_pinned_session_rejects_pre_handshake_state_and_spawn_bypass(monkeypatch, bypass):
    peers = RakNetPeers(
        monkeypatch,
        server_handshake=False,
        compression_threshold=65535,
        spawn_without_login=bypass == "spawn_without_login",
        pre_handshake_health=bypass == "pre_handshake_health",
    )
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(_raknet_options(server_key_sha256=peers.pin))
        with pytest.raises(RuntimeError):
            await bot.wait_ready(2)
        peer = peers.connections[-1]
        await bot.disconnect()
        assert peer.closed
        assert not any(packet.packet_id == 4 for packet in peer.received)
        assert bot.get_status().state != api.BotState.ready
        assert bot.get_status().health is None
        assert not bot._session.start_game


@pytest.mark.parametrize("failure", ["signature", "pin", "pinned_without_handshake"])
async def test_raknet_rejects_bad_or_bypassed_pinned_handshake(monkeypatch, failure):
    if failure == "signature":
        peers = RakNetPeers(monkeypatch, compression_threshold=256, bad_signature=True)
        options = _raknet_options()
        expected = "invalid server handshake"
    elif failure == "pin":
        peers = RakNetPeers(monkeypatch, compression_threshold=256)
        options = _raknet_options(server_key_sha256="00" * 32)
        expected = "invalid server handshake"
    else:
        peers = RakNetPeers(monkeypatch, server_handshake=False)
        options = _raknet_options(server_key_sha256="00" * 32)
        expected = "pinned server handshake"

    async with api.Runtime() as runtime:
        bot = runtime.add_bot(options)
        with pytest.raises(RuntimeError, match=expected):
            await bot.wait_ready(2)
        peer = peers.connections[-1]
        await bot.disconnect()
        assert peer.closed
        assert not any(packet.packet_id == 4 for packet in peer.received)
        assert bot.get_status().state == api.BotState.failed


async def test_encrypted_item_send_failure_is_indeterminate(monkeypatch):
    peers = RakNetPeers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(_raknet_options(server_key_sha256=peers.pin))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        failure = OSError("RakNet send failed")
        peer.send_error = failure
        operation = submit(bot)
        result = await operation.wait(2)
        await until(lambda: peer.closed)
        assert result.outcome == api.ItemRequestOutcome.indeterminate
        assert operation.phase == api.ItemOperationPhase.terminal
        assert bot.get_status().state == api.BotState.failed
        assert not any(packet.packet_id == 147 for packet in peer.received)
        assert bot._session.outbound.packets == bot._session.outbound.bytes == 0


@pytest.mark.parametrize("reason", ["timeout", "disconnect"])
async def test_cancelled_connection_acquisition_is_drained_and_closed(monkeypatch, reason):
    adapter = _adapter()
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class LateConnection:
        async def close(self):
            closed.set()

    connection = LateConnection()

    async def acquire(*args, **kwargs):
        entered.set()
        await release.wait()
        return connection

    monkeypatch.setattr(adapter.raknet.asyncio, "create_connection", acquire)
    timeout = 20 if reason == "timeout" else 5000
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(_raknet_options(connect_timeout_ms=timeout))
        ready = asyncio.create_task(bot.wait_ready(2))
        await asyncio.wait_for(entered.wait(), 2)
        if reason == "timeout":
            await asyncio.sleep(0.04)
            assert not closed.is_set()
            release.set()
            with pytest.raises(TimeoutError):
                await ready
        else:
            disconnect = asyncio.ensure_future(bot.disconnect())
            await asyncio.sleep(0.02)
            assert not disconnect.done() and not closed.is_set()
            release.set()
            await disconnect
            with pytest.raises(RuntimeError):
                await ready
        await asyncio.wait_for(closed.wait(), 2)


async def test_connection_failure_propagates_and_runtime_closes_session(monkeypatch):
    adapter = _adapter()

    async def fail(*args, **kwargs):
        raise OSError("RakNet connect failed")

    monkeypatch.setattr(adapter.raknet.asyncio, "create_connection", fail)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(_raknet_options())
        with pytest.raises(OSError, match="RakNet connect failed"):
            await bot.wait_ready(2)
        assert bot.get_status().state == api.BotState.failed


async def test_raknet_transport_import_is_optional(monkeypatch):
    monkeypatch.delitem(sys.modules, "endpilot.raknet_transport", raising=False)
    real_import = builtins.__import__

    def without_raknet(name, *args, **kwargs):
        if name == "raknet" or name.startswith("raknet."):
            raise ModuleNotFoundError("No module named 'raknet'", name="raknet")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_raknet)
    with pytest.raises(RuntimeError, match="optional dependency"):
        await transport._open_connection(_raknet_options(), identity=None)
