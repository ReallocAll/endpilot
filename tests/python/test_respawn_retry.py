import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

import endpilot as api
from endpilot.session import Session


def ready_bot(protocol):
    events = []
    loop = asyncio.get_running_loop()
    runtime = SimpleNamespace(_loop=loop, _stopping=False, _guard=lambda **_kwargs: None,
                              _emit=lambda _bot, _kind, factory: events.append(factory()))
    bot = api.Bot.__new__(api.Bot)
    bot._runtime = runtime
    bot._options = api.BotOptions(server_url="http://peer", protocol=protocol)
    bot._generation = 1
    bot._token = api.BotToken()
    bot._session = session = Session(bot, 1, loop.create_future())
    session.start_game = True
    session.status = replace(session.status, state=api.BotState.ready, actor_runtime_id=2,
                             position=api.Vec3(4, 5, 6), health=20)
    return bot, session, events


def deliver(session, packet):
    decoded = session.codec.decode_packet(session.codec.encode_packet(packet))
    session.handle_state(decoded.payload)


def server_ready(session):
    p = session.p
    deliver(session, p.RespawnPacket(pos=p.Vec3(x=7, y=8, z=9),
                                     state=p.PlayerRespawnState.READY_TO_SPAWN, runtime_id=2))


def queued_packets(session):
    wire = session.batch.encode_prepared(command.token for command in session.outbound.queue)
    return [decoded.payload for decoded in session.batch.decode(wire)]


@pytest.mark.parametrize("protocol,first_retry", [(1001, False), (2193, True)])
async def test_respawn_retries_preserve_server_order_and_require_health(protocol, first_retry):
    bot, session, events = ready_bot(protocol)
    p = session.p
    for retry in (False, True):
        with pytest.raises(RuntimeError, match="alive"):
            bot.respawn(retry=retry)
    assert not session.outbound.queue
    deliver(session, p.SetHealthPacket(health=0))
    bot.respawn(retry=first_retry)
    assert session.respawn_phase == 1
    assert [command.token.packet_id for command in session.outbound.queue] == [45]
    bot.respawn()
    assert len(session.outbound.queue) == 1
    bot.respawn(retry=True)
    assert session.respawn_phase == 1
    assert [command.token.packet_id for command in session.outbound.queue] == [45, 45]
    assert all(packet.state == p.PlayerRespawnState.CLIENT_READY_TO_SPAWN for packet in queued_packets(session))
    deliver(session, p.SetHealthPacket(health=20))
    assert not bot.get_status().alive
    server_ready(session)
    assert session.respawn_phase == 2
    assert [command.token.packet_id for command in session.outbound.queue] == [45, 45, 36]
    before = bot.get_status()
    assert before.position == api.Vec3(7, 8, 9)
    bot.respawn()
    assert len(session.outbound.queue) == 3
    bot.respawn(retry=True)
    assert session.respawn_phase == 2 and bot.get_status() == before
    assert [command.token.packet_id for command in session.outbound.queue] == [45, 45, 36, 36]
    assert queued_packets(session)[-1].action == p.PlayerActionType.RESPAWN
    server_ready(session)
    assert len(session.outbound.queue) == 4
    assert not any(isinstance(event, api.RespawnEvent) for event in events)
    assert session.stats["respawns"] == 0
    deliver(session, p.SetHealthPacket(health=20))
    assert bot.get_status().alive and session.respawn_phase == 0
    assert sum(isinstance(event, api.RespawnEvent) for event in events) == session.stats["respawns"] == 1


@pytest.mark.parametrize("phase,retry", [(0, None), (1, 1), (2, "true")])
async def test_invalid_retry_rejected_without_mutation(phase, retry):
    bot, session, events = ready_bot(2193)
    deliver(session, session.p.SetHealthPacket(health=0))
    if phase:
        bot.respawn()
    if phase == 2:
        server_ready(session)
    status = bot.get_status()
    queued = tuple(session.outbound.queue)
    previous_events = tuple(events)
    with pytest.raises(ValueError, match="retry"):
        bot.respawn(retry=retry)
    assert bot.get_status() == status and session.respawn_phase == phase
    assert tuple(session.outbound.queue) == queued and tuple(events) == previous_events
