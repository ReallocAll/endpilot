import pytest
from peer import Peers, until

import endpilot as api
from endpilot import transport


@pytest.mark.parametrize("version", [1001, 2193])
async def test_typed_packet_send_reuses_session_queue_and_rejects_bad_input(monkeypatch, version):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        bot.send_packet(peer.p.NetworkStackLatencyPacket(create_time=41, from_server=False))
        with pytest.raises(Exception):
            bot.send_packet(object())
        bot.send_packet(peer.p.NetworkStackLatencyPacket(create_time=42, from_server=False))
        assert bot.get_status().state == api.BotState.ready
        assert not any(p.packet_id == 115 and p.create_time in (41, 42) for p in peer.received)
        peer.resume_sender()
        await until(lambda: sum(p.packet_id == 115 and p.create_time in (41, 42) for p in peer.received) == 2)
        assert [p.create_time for p in peer.received if p.packet_id == 115 and p.create_time in (41, 42)] == [41, 42]
        assert bot.get_status().state == api.BotState.ready


async def test_full_external_queue_rejects_without_failing_session(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        monkeypatch.setattr(transport, "COMMAND_PACKETS", 1)
        bot.send_packet(peer.p.NetworkStackLatencyPacket(create_time=43, from_server=False))
        with pytest.raises(RuntimeError, match="queue limit"):
            bot.send_packet(peer.p.NetworkStackLatencyPacket(create_time=44, from_server=False))
        assert bot.get_status().state == api.BotState.ready
        peer.resume_sender()
        await until(lambda: any(p.packet_id == 115 and p.create_time == 43 for p in peer.received))
        assert not any(p.packet_id == 115 and p.create_time == 44 for p in peer.received)
        assert bot.get_status().state == api.BotState.ready


async def test_external_packet_requires_ready_phase(monkeypatch):
    Peers(monkeypatch, automatic=False)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await until(lambda: bot._session is not None and bot.get_status().state == api.BotState.negotiating)
        with pytest.raises(RuntimeError, match="not ready"):
            bot.send_packet(bot._session.p.NetworkStackLatencyPacket(create_time=45, from_server=False))
        assert bot.get_status().state == api.BotState.negotiating
