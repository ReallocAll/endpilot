"""Controlled bytes peer for protocol tests."""

import asyncio
import base64
import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import bedrock_protocol as bp
from bedrock_protocol.wire import BatchCodec, Compression
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


def unbase(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def token_part(token, index):
    return json.loads(unbase(token.split(".")[index]))


def verify_login_token(token):
    header, body, encoded_signature = token.split(".")
    claims = json.loads(unbase(header))
    assert claims["alg"] == "ES384"
    public = serialization.load_der_public_key(base64.b64decode(claims["x5u"]))
    assert isinstance(public.curve, ec.SECP384R1)
    signature = unbase(encoded_signature)
    assert len(signature) == 96
    der = utils.encode_dss_signature(int.from_bytes(signature[:48], "big"), int.from_bytes(signature[48:], "big"))
    public.verify(der, (header + "." + body).encode(), ec.ECDSA(hashes.SHA384()))


def offer(public):
    key = ec.generate_private_key(ec.SECP384R1())
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    salt = b"controlled peer salt"
    header = {"alg": "ES384", "x5u": base64.b64encode(der).decode()}
    body = {"salt": base64.b64encode(salt).decode()}
    def encode(x):
        return base64.urlsafe_b64encode(x).rstrip(b"=")
    data = encode(json.dumps(header).encode()) + b"." + encode(json.dumps(body).encode())
    r, s = utils.decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA384())))
    jwt = (data + b"." + encode(r.to_bytes(48, "big") + s.to_bytes(48, "big"))).decode()
    remote = serialization.load_der_public_key(base64.b64decode(public))
    return jwt, hashlib.sha256(salt + key.exchange(ec.ECDH(), remote)).digest()


class Peer:
    def __init__(self, version=2193, *, automatic=True, server_handshake=True, compression_threshold=1):

        self.version = version
        self.p = getattr(bp, f"v{version}")
        self.codec = bp.Codec(version)
        self.batch = BatchCodec(self.codec)
        self.incoming = asyncio.Queue()
        self.close_gate = asyncio.Event()
        self.close_gate.set()
        self.closing = asyncio.Event()
        self.closed = False
        self.automatic = automatic
        self.server_handshake = server_handshake
        self.compression_threshold = compression_threshold
        self.received = []
        self.received_wire = []
        self.login = None
        self.before_send = None
        self.send_error = None
        self.connection = SimpleNamespace(send=self.send_bytes, recv=self.recv, close=self.close)
        self._sender_event = None

    def pause_sender(self, bot):
        event = bot._session.send_event
        event.clear()
        self._sender_event = event
        self._wake_sender = event.set
        event.set = lambda: None

    def resume_sender(self):
        if self._sender_event is not None:
            self._sender_event.set = self._wake_sender
            self._wake_sender()
            self._sender_event = None

    async def recv(self):
        return await self.incoming.get()

    async def send_bytes(self, data):
        if self.before_send:
            self.before_send()
        if self.send_error:
            raise self.send_error
        self.received_wire.append(data)
        self.accept(data)

    async def close(self):
        self.resume_sender()
        self.closing.set()
        await self.close_gate.wait()
        self.closed = True

    def send(self, *packets):
        self.incoming.put_nowait(self.batch.encode(packets))

    def accept(self, data):
        packets = tuple(self.batch.decode(data))
        for decoded in packets:
            packet = decoded.payload
            self.received.append(packet)
            if not self.automatic:
                continue
            p = self.p
            if packet.packet_id == 193:
                assert self.received_wire[-1] == b"\x06\xc1\x01" + struct.pack(">i", self.version)
                self.send(
                    p.NetworkSettingsPacket(
                        compression_threshold=self.compression_threshold,
                        compression_algorithm=p.PacketCompressionAlgorithm.ZLIB,
                        client_throttle_enabled=False,
                        client_throttle_threshold=0,
                        client_throttle_scalar=0,
                    )
                )
                self.batch.set_compression(Compression.ZLIB, self.compression_threshold)
            elif packet.packet_id == 1:
                raw = packet.connection_request.to_bytes()
                size = struct.unpack_from("<I", raw)[0]
                envelope = json.loads(raw[4 : 4 + size])
                client_size = struct.unpack_from("<I", raw, 4 + size)[0]
                client_token = raw[8 + size : 8 + size + client_size].decode()
                verify_login_token(envelope["Token"])
                verify_login_token(client_token)
                self.login = (
                    token_part(envelope["Token"], 0),
                    token_part(envelope["Token"], 1),
                    token_part(client_token, 1),
                )
                if self.server_handshake:
                    jwt, _ = offer(self.login[0]["x5u"])
                    self.send(p.ServerToClientHandshakePacket(token=jwt))
                else:
                    self.spawn()
            elif packet.packet_id == 4:
                self.spawn()

    def spawn(self):
        p = self.p
        self.send(p.PlayStatusPacket(status=p.PlayStatus.LOGIN_SUCCESS))
        game = self.codec.decode_packet(
            (Path(__file__).parent / "fixtures" / f"start_game_{self.version}.bin").read_bytes()
        ).payload
        self.send(game, p.ItemRegistryPacket(items=[]))
        self.send(p.ChunkRadiusUpdatedPacket(chunk_radius=16), p.PlayStatusPacket(status=p.PlayStatus.PLAYER_SPAWN))


class Peers:
    def __init__(self, monkeypatch, *, automatic=True, server_handshake=True, compression_threshold=1):
        from endpilot import transport

        self.connections = []
        self.automatic = automatic
        self.server_handshake = server_handshake
        self.compression_threshold = compression_threshold
        monkeypatch.setattr(transport, "_open_connection", self.open)

    async def open(self, options, identity):
        peer = Peer(
            options.protocol,
            automatic=self.automatic,
            server_handshake=self.server_handshake,
            compression_threshold=self.compression_threshold,
        )
        self.connections.append(peer)
        return peer.connection


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


def empty_item(p):
    if p is bp.v1001:
        return p.SerializedNetworkItemStackDescriptor(
            id=0, stack_size=0, aux_value=0, net_id_variant=None, block_runtime_id=0, user_data_buffer=b""
        )
    return p.SerializedNetworkItemStackDescriptor(
        id=0, stack_size=0, aux_value=0, net_id_variant=None, block_runtime_id=0, user_data_buffer=b""
    )


def slot_packet(p, slot=0):
    return p.InventorySlotPacket(
        inventory_id=p.ContainerID.INVENTORY, slot=slot, full_container_name=None, storage_item=None, item=empty_item(p)
    )


def actor_packet(p):
    return p.AddActorPacket(
        entity_id=-92,
        runtime_id=91,
        actor_type="minecraft:villager_v2",
        pos=p.Vec3(x=1, y=2, z=3),
        velocity=p.Vec3(x=0, y=5, z=0),
        rot=p.Vec2(x=10, y=20),
        y_head_rotation=30,
        y_body_rotation=40,
        attributes=[],
        data=p.SynchedActorData.CopyableDataList(data=[]),
        synched_properties=p.PropertySyncData(int_entries=[], float_entries=[]),
        links=[],
    )


def submit(bot):
    s = bot._session
    p = s.p

    def packet(request_id):
        cls = p.ItemStackRequestData if p is bp.v1001 else p.ItemStackRequestCereal.RequestData
        request = cls(
            client_request_id=p.ItemStackRequestId(raw_id=request_id),
            actions=[],
            strings_to_filter=[],
            strings_to_filter_origin=p.TextProcessingEventOrigin(0),
        )
        return p.ItemStackRequestPacket(requests=[request])

    return s.submit_item_request(packet)


def motion_packet(p, x=7):
    return p.MoveActorAbsolutePacket(
        move_data=p.MoveActorAbsoluteData(
            runtime_id=91, header=0, pos=p.Vec3(x=x, y=8, z=9), rot_x=0, rot_y=32, rot_y_head=0
        )
    )


def trade_packet(p):
    return p.UpdateTradePacket(
        container_id=p.ContainerID(5),
        type=p.ContainerType(15),
        size=3,
        trader_tier=0,
        entity_unique_id=-92,
        last_trading_player=1,
        display_name="Trader",
        use_new_trade_screen=True,
        using_economy_trade=True,
        data=bp.Nbt(bp.NbtType.COMPOUND, {}),
    )
