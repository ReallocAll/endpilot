import base64
import zlib
from pathlib import Path

import pytest
from peer import Peer, Peers, until

import endpilot as api


@pytest.mark.parametrize("version", [1001, 2193])
async def test_login_success_last_gates_ready_and_initializes_in_order(monkeypatch, version):
    def spawn(self):
        p = self.p
        game = self.codec.decode_packet(
            (Path(__file__).parent / "fixtures" / f"start_game_{self.version}.bin").read_bytes()
        ).payload
        self.send(game, p.ItemRegistryPacket(items=[]))
        self.send(p.ChunkRadiusUpdatedPacket(chunk_radius=16), p.PlayStatusPacket(status=p.PlayStatus.PLAYER_SPAWN))

    monkeypatch.setattr(Peer, "spawn", spawn)
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await until(lambda: bot._session is not None and bot._session.negotiation.spawn)
        peer = peers.connections[-1]
        assert not bot._session.negotiation.login
        assert not bot._session.negotiation.initialized
        assert not bot._session.ready.done()
        assert bot.get_status().state != api.BotState.ready
        assert not any(packet.packet_id in (113, 312) for packet in peer.received)

        peer.send(peer.p.PlayStatusPacket(status=peer.p.PlayStatus.LOGIN_SUCCESS))
        await bot.wait_ready(2)
        initialized = [packet for packet in peer.received if packet.packet_id in (113, 312)]
        assert [packet.packet_id for packet in initialized] == [312, 312, 113]
        assert [packet.loading_screen_packet_type for packet in initialized[:2]] == [
            peer.p.ServerboundLoadingScreenPacketType.START_LOADING_SCREEN,
            peer.p.ServerboundLoadingScreenPacketType.END_LOADING_SCREEN,
        ]

        peer.send(peer.p.PlayStatusPacket(status=peer.p.PlayStatus.LOGIN_SUCCESS))
        await until(lambda: bot.capture_statistics().packet_counts_in[2] == 3)
        assert [packet.packet_id for packet in peer.received if packet.packet_id in (113, 312)] == [312, 312, 113]


def read_varuint(data, offset):
    value = 0
    for shift in range(0, 35, 7):
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise AssertionError("invalid frame varuint")


def frame_ids(body):
    offset = 0
    ids = []
    while offset < len(body):
        length, offset = read_varuint(body, offset)
        assert length > 0 and offset + length <= len(body)
        packet_id, _ = read_varuint(body[offset : offset + length], 0)
        ids.append(packet_id & 0x3FF)
        offset += length
    assert offset == len(body)
    return ids


@pytest.mark.parametrize("version", [1001, 2193])
async def test_signed_server_handshake_keeps_dtls_application_batches_unciphered(monkeypatch, version):
    original_accept = Peer.accept
    checked = set()
    handshakes = []

    def accept(self, data):
        if self.login is not None and self not in checked:
            assert data == bytes.fromhex("ff0104")
            checked.add(self)
            handshakes.append(data)
        original_accept(self, data)

    monkeypatch.setattr(Peer, "accept", accept)
    peers = Peers(monkeypatch, compression_threshold=256)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        for connection in range(2):
            if connection:
                await bot.reconnect()
            await bot.wait_ready(2)
            peer = peers.connections[-1]
            assert len(handshakes) == connection + 1
            assert sum(packet.packet_id == 4 for packet in peer.received) == 1
            assert bot.get_status().state == api.BotState.ready

            before = len(peer.received_wire)
            bot.report_blob_status(range(80))
            await until(lambda: any(packet.packet_id == 135 for packet in peer.received))
            wire = peer.received_wire[before]
            assert wire[0] == 0
            inflater = zlib.decompressobj(wbits=-15)
            body = inflater.decompress(wire[1:]) + inflater.flush()
            assert inflater.eof and not inflater.unused_data and not inflater.unconsumed_tail
            assert len(body) > 256 and 135 in frame_ids(body)

            incoming = peer.batch.encode([peer.p.SetHealthPacket(health=19)])
            assert incoming[0] == 0xFF and frame_ids(incoming[1:]) == [42]
            peer.incoming.put_nowait(incoming)
            await until(lambda: bot.get_status().health == 19)
        await bot.disconnect()
        assert all(peer.closed for peer in peers.connections)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_bad_server_handshake_signature_closes_without_packet4(monkeypatch, version):
    import peer as private_peer

    original_offer = private_peer.offer

    def tampered_offer(public):
        token, key = original_offer(public)
        prefix, signature = token.rsplit(".", 1)
        signature = bytearray(private_peer.unbase(signature))
        signature[0] ^= 1
        damaged = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        return prefix + "." + damaged, key

    monkeypatch.setattr(private_peer, "offer", tampered_offer)
    peers = Peers(monkeypatch, compression_threshold=256)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        with pytest.raises(RuntimeError, match="invalid server handshake"):
            await bot.wait_ready(2)
        await bot.disconnect()
        peer = peers.connections[-1]
        assert peer.closed
        assert [packet.packet_id for packet in peer.received] == [193, 1]
        assert len(peer.received_wire) == 2
        assert bot.get_status().state == api.BotState.failed
        assert bot.capture_statistics().decode_errors == 1


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("incoming_id", [6, 7])
async def test_resource_pack_response_preserves_native_protocol_variant(version, incoming_id):
    from types import SimpleNamespace

    from test_respawn_retry import queued_packets, ready_bot

    _, session, _ = ready_bot(version)
    await session.handle(SimpleNamespace(packet_id=incoming_id))
    packets = queued_packets(session)
    assert len(packets) == 1
    response = packets[0]
    assert isinstance(response, session.p.ResourcePackClientResponsePacket)
    if version == 2193:
        assert response.response.index == (3 if incoming_id == 7 else 2)
    else:
        expected = (
            session.p.ResourcePackResponse.RESOURCE_PACK_STACK_FINISHED
            if incoming_id == 7
            else session.p.ResourcePackResponse.DOWNLOADING_FINISHED
        )
        assert response.response == expected
        assert not response.downloading_packs
