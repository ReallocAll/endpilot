import asyncio
from types import SimpleNamespace

import pytest
from bedrock_protocol.wire import Compression
from peer import Peers, submit, until

import endpilot as api
from endpilot import transport


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("stage", ["admission", "batch"])
async def test_encoding_failure_invalidates_session(monkeypatch, protocol, stage):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol))
        await bot.wait_ready(2)
        session = bot._session
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        error = ValueError("controlled encoding failure")

        def fail(*args):
            raise error

        if stage == "admission":
            session.codec = SimpleNamespace(prepare_packet=fail)
            with pytest.raises(ValueError) as caught:
                bot.request_chunk_radius(4)
            assert caught.value is error
            assert not session.usable()
        else:
            session.batch = SimpleNamespace(encode_prepared=fail, decode=session.batch.decode)
            bot.request_chunk_radius(4)
        await until(lambda: peer.closed)
        assert bot.get_status().state == api.BotState.failed
        assert bot.capture_statistics().packets_out == before.packets_out
        assert session.outbound.packets == session.outbound.bytes == 0


@pytest.mark.parametrize("failure", [OSError("send failed"), asyncio.CancelledError()])
async def test_send_failure_is_indeterminate_and_keeps_batch_accounted(monkeypatch, failure):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        operation = submit(bot)

        def at_send():
            assert operation.phase == api.ItemOperationPhase.submitted
            assert bot._session.outbound.packets >= 1
            assert bot._session.outbound.bytes > 0

        peer.before_send = at_send
        peer.send_error = failure
        result = await operation.wait(2)
        await until(lambda: peer.closed)
        if isinstance(failure, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await runtime.wait_closed()
        assert result.outcome == api.ItemRequestOutcome.indeterminate
        assert bot.get_status().state == api.BotState.failed
        assert not any(packet.packet_id == 147 for packet in peer.received)
        after = bot.capture_statistics()
        assert after.packets_out == before.packets_out
        assert after.batch_bytes_out == before.batch_bytes_out
        assert after.packet_counts_out.to_list() == before.packet_counts_out.to_list()
        assert after.play_packet_counts_out.to_list() == before.play_packet_counts_out.to_list()
        assert bot._session.outbound.packets == bot._session.outbound.bytes == 0


@pytest.mark.parametrize("direction", ["send", "receive"])
async def test_complete_wire_message_limit_invalidates_session(monkeypatch, direction):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        monkeypatch.setattr(transport, "MAX_WIRE_MESSAGE", 10)
        if direction == "send":
            bot._session.batch = SimpleNamespace(encode_prepared=lambda packets: b"x" * 11,
                                                decode=bot._session.batch.decode)
            bot.request_chunk_radius(4)
        else:
            peer.incoming.put_nowait(b"x" * 11)
        await until(lambda: peer.closed)
        assert bot.get_status().state == api.BotState.failed
        assert "wire message limit" in bot._session.reason


async def test_disconnect_resolves_pending_batch_boundary(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        boundary = asyncio.create_task(bot._session.flush())
        await asyncio.sleep(0)
        await bot.disconnect()
        assert boundary.done()
        with pytest.raises(asyncio.CancelledError):
            await boundary


async def test_outbound_message_limit_rejects_before_item_submission(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        monkeypatch.setattr(transport, "MAX_WIRE_MESSAGE", 1)
        operation = submit(bot)
        result = await operation.wait(2)
        await until(lambda: peer.closed)
        assert result.outcome == api.ItemRequestOutcome.cancelled
        assert not any(packet.packet_id == 147 for packet in peer.received)
        assert bot.capture_statistics().packets_out == before.packets_out


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_prepared_tokens_keep_exact_queue_byte_charges_until_cleanup(monkeypatch, protocol):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        session = bot._session
        packet = peer.p.RequestChunkRadiusPacket(chunk_radius=4, max_chunk_radius=4)
        size = len(session.codec.prepare_packet(packet)) + 5
        monkeypatch.setattr(transport, "COMMAND_BYTES", size * 2)
        session.enqueue(packet)
        session.enqueue(packet)
        assert session.outbound.bytes == size * 2 and session.outbound.packets == 2
        assert all(command.token.protocol == protocol and command.token.packet_id == 69
                   and command.size == len(command.token) + 5 for command in session.outbound.queue)
        with pytest.raises(RuntimeError, match="queue limit"):
            session.enqueue(packet)
        assert session.outbound.bytes == size * 2 and session.outbound.packets == 2
        await until(lambda: peer.closed)
        assert session.outbound.bytes == session.outbound.packets == 0


async def test_prepared_packets_follow_fifo_compression_boundary(monkeypatch):
    peers = Peers(monkeypatch, compression_threshold=1)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        session = bot._session
        peer.pause_sender(bot)
        before = len(peer.received_wire)
        bot.request_chunk_radius(4)

        def change_compression():
            session.batch.set_compression(Compression.ZLIB, 65535)
            peer.batch.set_compression(Compression.ZLIB, 65535)

        boundary = asyncio.create_task(session.flush(change_compression))
        await asyncio.sleep(0)
        bot.request_chunk_radius(5)
        peer.resume_sender()
        await boundary
        await until(lambda: any(packet.packet_id == 69 and packet.chunk_radius == 5 for packet in peer.received))
        wires = peer.received_wire[before:]
        assert wires[0][0] == 0 and wires[1][0] == 255
        assert [packet.chunk_radius for packet in peer.received if packet.packet_id == 69][-2:] == [4, 5]
