import gc
from pathlib import Path

import bedrock_protocol as bp
import pytest
from peer import Peer, Peers, until
from test_actor_attributes import attribute_data, attribute_values
from test_engineering_repairs import player_packet

import endpilot as api


def initial_peers(monkeypatch, early_packets):
    def spawn(peer):
        peer.start_game = peer.codec.decode_packet(
            (Path(__file__).parent / "fixtures" / f"start_game_{peer.version}.bin").read_bytes()
        ).payload
        peer.send(peer.p.PlayStatusPacket(status=peer.p.PlayStatus.LOGIN_SUCCESS))
        peer.send(*early_packets(peer, peer.start_game.runtime_id))

    monkeypatch.setattr(Peer, "spawn", spawn)
    return Peers(monkeypatch)


def finish_spawn(peer):
    p = peer.p
    peer.send(peer.start_game, p.ItemRegistryPacket(items=[]))
    peer.send(p.ChunkRadiusUpdatedPacket(chunk_radius=16), p.PlayStatusPacket(status=p.PlayStatus.PLAYER_SPAWN))


def teleport(p, runtime_id, x):
    data = {"teleport_data": p.MovePlayerTeleportData(cause=0, source_entity_type=0)} if p is bp.v2193 else {
        "cause": 0, "source_entity_type": 0
    }
    return p.MovePlayerPacket(player_id=runtime_id, pos=p.Vec3(x=x, y=8, z=9), rot=p.Vec2(x=0, y=0),
                              y_head_rot=0, reset_position=p.PlayerPositionModeComponent.PositionMode.TELEPORT,
                              on_ground=True, riding_id=0, tick=5, **data)


def death(p, runtime_id):
    return p.ActorEventPacket(runtime_id=runtime_id, event_id=p.ActorEvent.DEATH, data=0, fire_at_position=None)


def health_attributes(p, runtime_id, *values):
    return p.UpdateAttributesPacket(runtime_id=runtime_id, attribute_data=[
        attribute_data(p, "minecraft:health", 0, value, 20) for value in values
    ], tick=1)


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("own_move", [False, True])
async def test_early_moves_preserve_own_teleport_and_do_not_replay_remote_actor_updates(monkeypatch, protocol, own_move):
    def early(peer, runtime_id):
        packets = [player_packet(peer.p), teleport(peer.p, 91, 70)]
        if own_move:
            packets.append(teleport(peer.p, runtime_id, 40))
        return packets

    peers = initial_peers(monkeypatch, early)
    events, decoded_ids = [], []

    def callback(_bot, event):
        if isinstance(event, api.PacketDecodedEvent):
            decoded_ids.append(event.packet.header.packet_id)
        else:
            events.append(event)

    async with api.Runtime(callback, events=api.ALL_EVENTS) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol))
        await until(lambda: bot._session is not None and len(bot._session.player.pending) == 1 + own_move)
        peer = peers.connections[-1]
        assert bot.capture_actor(91).position == api.Vec3(70, 8, 9)
        assert not any(isinstance(event, api.PositionEvent) for event in events)
        # A later batch releases the reader's previous decoded-batch ownership.
        peer.send(peer.p.ChunkRadiusUpdatedPacket(chunk_radius=0))
        await until(lambda: 70 in decoded_ids)
        gc.collect()
        finish_spawn(peer)
        await bot.wait_ready(2)
        expected_position = api.Vec3(40, 8, 9) if own_move else api.Vec3(
            peer.start_game.pos.x, peer.start_game.pos.y, peer.start_game.pos.z
        )
        assert bot.get_status().position == expected_position
        assert not bot._session.player.pending
        assert sum(isinstance(event, api.PositionEvent) for event in events) == own_move
        assert sum(isinstance(event, api.ActorMotionEvent) for event in events) == 1
        assert sum(isinstance(event, api.ActorEvent) and event.kind == api.ActorEvent.Kind.moved
                   for event in events) == 1
        assert decoded_ids.count(19) == bot.capture_statistics().packet_counts_in[19] == 1 + own_move
        bot._session.tick()
        bot._session.tick()
        await until(lambda: sum(packet.packet_id == 144 for packet in peer.received) >= 2)
        inputs = [packet for packet in peer.received if packet.packet_id == 144][:2]
        flag = peer.p.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT
        acknowledgements = [bool(packet.input_data & (1 << int(flag))) if protocol == 1001
                            else flag in packet.input_data for packet in inputs]
        assert acknowledgements == [own_move, False]


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("ordering", ["death_then_health", "health_then_death", "attribute_health"])
async def test_early_health_and_death_fifo_emit_once_and_respawn_after_ready(monkeypatch, protocol, ordering):
    expected = {"death_then_health": [0, 20], "health_then_death": [20, 0],
                "attribute_health": [5, 0, 20]}[ordering]

    def early(peer, runtime_id):
        p = peer.p
        packets = [player_packet(p), health_attributes(p, 91, 13)]
        if ordering == "attribute_health":
            return packets + [health_attributes(p, runtime_id, 5, 0), p.SetHealthPacket(health=20)]
        own_death, positive = death(p, runtime_id), p.SetHealthPacket(health=20)
        return packets + ([own_death, positive] if ordering == "death_then_health" else [positive, own_death])

    peers = initial_peers(monkeypatch, early)
    events = []
    interests = api.ALL_EVENTS & ~api.event_mask(api.EventKind.packet_decoded)
    async with api.Runtime(lambda _bot, event: events.append(event), events=interests) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol))
        await until(lambda: bot._session is not None and len(bot._session.player.pending) == 1 + len(expected))
        peer = peers.connections[-1]
        assert not any(isinstance(event, (api.DeathEvent, api.HealthEvent)) for event in events)
        assert sum(isinstance(event, api.ActorSignalEvent) for event in events) == (ordering != "attribute_health")
        assert attribute_values(bot.capture_actor(91)) == [("minecraft:health", 0, 13, 20)]
        finish_spawn(peer)
        await bot.wait_ready(2)
        assert [event.health for event in events if isinstance(event, api.HealthEvent)] == expected
        assert bot.get_status().health == expected[-1] and not bot.get_status().alive
        assert bot.capture_statistics().deaths == 1
        assert sum(isinstance(event, api.DeathEvent) for event in events) == 1
        assert sum(isinstance(event, api.ActorSignalEvent) for event in events) == (ordering != "attribute_health")
        assert sum(isinstance(event, api.ActorEvent) and event.kind == api.ActorEvent.Kind.metadata
                   for event in events) == 1
        assert not bot._session.player.pending
        bot.respawn()
        p = peer.p
        peer.send(p.RespawnPacket(pos=p.Vec3(x=1, y=2, z=3), state=p.PlayerRespawnState.READY_TO_SPAWN,
                                  runtime_id=bot.get_status().actor_runtime_id), p.SetHealthPacket(health=20))
        await until(lambda: bot.get_status().alive)
        assert bot.capture_statistics().respawns == 1


async def test_close_before_start_game_releases_pending_effects(monkeypatch):
    peers = initial_peers(monkeypatch, lambda peer, runtime_id: [death(peer.p, runtime_id),
                                                               peer.p.SetHealthPacket(health=20)])
    events = []
    async with api.Runtime(lambda _bot, event: events.append(event), events=api.ALL_EVENTS) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await until(lambda: bot._session is not None and len(bot._session.player.pending) == 2)
        await bot.disconnect()
        assert peers.connections[-1].closed and not bot._session.player.pending
        assert not any(isinstance(event, (api.DeathEvent, api.HealthEvent)) for event in events)


async def test_pending_bound_is_checked_for_each_health_attribute_and_fails_visibly(monkeypatch):
    initial_peers(monkeypatch, lambda peer, runtime_id: [health_attributes(peer.p, runtime_id, *([0] * 4097))])
    events = []
    async with api.Runtime(lambda _bot, event: events.append(event), events=api.ALL_EVENTS) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        with pytest.raises(RuntimeError, match="self-state limit"):
            await bot.wait_ready(2)
        await until(lambda: bot._session.finished)
        assert bot.get_status().state == api.BotState.failed and not bot._session.player.pending
        assert not any(isinstance(event, api.HealthEvent) for event in events)
        errors = [event for event in events if isinstance(event, api.DecodeErrorEvent)]
        assert len(errors) == 1 and errors[0].packet_id == 29 and not errors[0].recoverable


async def test_start_game_drain_stops_after_callback_disconnect(monkeypatch):
    peers = initial_peers(monkeypatch, lambda peer, runtime_id: [death(peer.p, runtime_id),
                                                               peer.p.SetHealthPacket(health=20)])
    health = []

    def callback(bot, event):
        if isinstance(event, api.HealthEvent):
            health.append(event.health)
            bot.disconnect()

    async with api.Runtime(callback, events=api.event_mask(api.EventKind.health)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await until(lambda: bot._session is not None and len(bot._session.player.pending) == 2)
        finish_spawn(peers.connections[-1])
        await until(lambda: peers.connections[-1].closed)
        assert health == [0] and not bot._session.player.pending
        assert not bot.get_status().alive and bot.get_status().health == 0
