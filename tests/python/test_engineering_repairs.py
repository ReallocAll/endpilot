import asyncio
import math
from dataclasses import dataclass, replace
from uuid import UUID

import bedrock_protocol as bp
import pytest
from peer import Peers, actor_packet, empty_item, slot_packet, until
from test_benchmark_hooks import dimension_ack
from test_respawn_retry import ready_bot
from trade_peer import deliver, inventory_packet, scene

import endpilot as api


@dataclass(slots=True)
class TaggedInput(api.Input):
    tag: str = "retained"


@pytest.mark.parametrize("input_type", [api.Input, TaggedInput])
async def test_input_copy_is_independent_and_keeps_all_fields_and_subclass_layout(input_type):
    bot, session, _ = ready_bot(2193)
    frame = input_type(position=api.Vec3(1, 2, 3), delta=api.Vec3(4, 5, 6), move=api.Vec2(-1, 1),
                       pitch=-0.0, yaw=75, vertical_collision=False)
    bot.set_input(frame)
    captured = session.control.input
    assert captured is not frame and type(captured) is input_type
    frame.position = frame.delta = api.Vec3(99, 99, 99)
    frame.move = api.Vec2()
    frame.pitch = frame.yaw = 999
    frame.vertical_collision = True
    if isinstance(frame, TaggedInput):
        frame.tag = "changed"
        assert captured.tag == "retained"
    assert captured.position == api.Vec3(1, 2, 3) and captured.delta == api.Vec3(4, 5, 6)
    assert captured.move == api.Vec2(-1, 1) and math.copysign(1, captured.pitch) == -1
    assert captured.yaw == 75 and captured.vertical_collision is False
    with pytest.raises(ValueError, match="finite"):
        bot.set_input(api.Input(position=api.Vec3(x=float("inf"))))
    assert session.control.input is captured


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("coordinates", [None, (-0.0, 8.0, -9.0)])
async def test_delta_actor_motion_preserves_axis_values_identity_and_retained_snapshot(protocol, coordinates):
    _, session, events = ready_bot(protocol)
    actor = api.ActorSnapshot(91, -92, "minecraft:player", api.Vec3(0.0, 2, 3))
    session.add_actor(actor)
    events.clear()
    values = dict(runtime_id=91, new_position_x=None, new_position_y=None, new_position_z=None,
                  rot_x=None, rot_y=None, rot_y_head=None)
    if coordinates is not None:
        values.update(zip(("new_position_x", "new_position_y", "new_position_z"), coordinates))
    if protocol == 1001:
        values["header"] = 0 if coordinates is None else 7
    else:
        values.update(is_on_ground=False, force_move=False, force_move_local_entity=False,
                      force_completion=False, ticks=1)
    p = session.p
    packet = p.MoveActorDeltaPacket(move_data=p.MoveActorDeltaData(**values))
    session.handle_state(session.codec.decode_packet(session.codec.encode_packet(packet)).payload)
    current = session.actor_state.get(91)
    if coordinates is None:
        assert current.position is actor.position
    else:
        assert current.position == api.Vec3(*coordinates)
        assert math.copysign(1, current.position.x) == -1
    assert actor.position == api.Vec3(0.0, 2, 3) and math.copysign(1, actor.position.x) == 1
    assert [type(event) for event in events] == [api.ActorMotionEvent, api.ActorEvent]


async def test_health_callbacks_observe_final_status_and_keep_death_respawn_event_order():
    bot, session, events = ready_bot(2193)
    metadata = replace(session.status, tick=7, chunk_radius=10, selected_hotbar=4)
    session.status = metadata
    observations = []

    def emit(_bot, _kind, factory):
        event = factory()
        events.append(event)
        observations.append((type(event), bot.get_status()))

    session.runtime._emit = emit
    session.health(0)
    assert [type(event) for event in events] == [api.DeathEvent, api.HealthEvent]
    assert all(not status.alive and status.health == 0 and status.selected_hotbar is None
               for _, status in observations)
    assert metadata.alive and metadata.health == 20 and metadata.selected_hotbar == 4
    assert session.status.tick == 7 and session.status.chunk_radius == 10
    retained = session.status
    events.clear()
    observations.clear()
    session.respawn_phase = 2
    session.health(20)
    assert [type(event) for event in events] == [api.RespawnEvent, api.HealthEvent]
    assert all(status.alive and status.health == 20 for _, status in observations)
    assert not retained.alive and retained.health == 0


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_inventory_slot_absent_and_present_fallbacks_keep_owned_captures(protocol):
    _, session, _ = ready_bot(protocol)
    p = session.p

    def deliver_slot(index):
        packet = slot_packet(p, index)
        session.handle_state(session.codec.decode_packet(session.codec.encode_packet(packet)).payload)

    deliver_slot(0)
    first = session.inventory.inventories[0]
    assert not first.complete and first.full_container_name is None and first.storage_item is None
    assert set(first.slots) == {0}
    name = p.FullContainerName(name=p.ContainerEnumName(7), dynamic_id=42)
    previous = api.InventorySnapshot({0: empty_item(p), 2: empty_item(p)}, True, name, empty_item(p))
    session.inventory.inventories[0] = previous
    deliver_slot(1)
    current = session.inventory.inventories[0]
    assert current.complete and set(current.slots) == {0, 1, 2}
    assert current.full_container_name is previous.full_container_name
    assert current.storage_item is previous.storage_item
    assert current.full_container_name.to_dict() == {"name": 7, "dynamic_id": 42}
    assert set(previous.slots) == {0, 2} and set(first.slots) == {0}


@pytest.mark.parametrize("invalidation", ["correction", "teleport", "dimension", "death"])
async def test_prepared_input_epoch_is_invalidated_before_transmission(monkeypatch, invalidation):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        peer.pause_sender(bot)
        bot.set_input(api.Input(position=api.Vec3(99, 99, 99)))
        bot._session.tick()
        stale_tick = bot.get_status().tick
        if invalidation == "correction":
            packet = correction(p)
        elif invalidation == "teleport":
            packet = p.MovePlayerPacket(
                player_id=bot.get_status().actor_runtime_id, pos=p.Vec3(x=4, y=5, z=6), rot=p.Vec2(x=0, y=0),
                y_head_rot=0, reset_position=p.PlayerPositionModeComponent.PositionMode.TELEPORT,
                on_ground=True, riding_id=0, teleport_data=p.MovePlayerTeleportData(cause=0, source_entity_type=0),
                tick=1,
            )
        elif invalidation == "dimension":
            packet = p.ChangeDimensionPacket(dimension_id=1, pos=p.Vec3(x=4, y=5, z=6),
                                             respawn=False, loading_screen_id=42)
        else:
            packet = p.SetHealthPacket(health=0)
        epoch = bot._session.control.input_epoch
        peer.send(packet)
        await until(lambda: bot._session.control.input_epoch != epoch)
        if invalidation == "death":
            bot.respawn()
            peer.send(
                p.RespawnPacket(pos=p.Vec3(x=4, y=5, z=6), state=p.PlayerRespawnState.READY_TO_SPAWN, runtime_id=2),
                p.SetHealthPacket(health=20),
            )
            await until(lambda: bot.get_status().alive)
        elif invalidation == "dimension":
            peer.send(dimension_ack(p, bot.get_status().actor_runtime_id))
            await until(lambda: bot._session.dimension_deadline is None)
            bot._session.tick()
        bot.set_input(api.Input(position=api.Vec3(7, 8, 9)))
        bot._session.tick()
        valid_tick = bot.get_status().tick
        peer.resume_sender()
        await bot._session.flush()
        inputs = [packet for packet in peer.received if packet.packet_id == 144]
        assert all(packet.client_tick != stale_tick and packet.pos.x != 99 for packet in inputs)
        assert any(packet.client_tick == valid_tick and packet.pos.x == 7 for packet in inputs)


async def test_disconnect_before_session_and_after_runtime_stop(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.disconnect()
        assert not peers.connections
        assert bot.get_status().state == api.BotState.closed
        await asyncio.wait_for(bot.disconnect(), 1)
        await bot.reconnect()
        await bot.wait_ready(2)
        assert len(peers.connections) == 1
        retained = bot.capture_snapshot()
        peer = peers.connections[-1]
        peer.close_gate.clear()
        runtime.request_stop()
        await asyncio.wait_for(peer.closing.wait(), 2)
        cancelled = bot.disconnect()
        completion = bot.disconnect()
        cancelled.cancel()
        assert not completion.done()
        peer.close_gate.set()
        await asyncio.wait_for(completion, 2)
        await runtime.wait_closed()
        await asyncio.wait_for(bot.disconnect(), 1)
        assert len(peers.connections) == 1
        assert bot.get_status().state == api.BotState.closed
        assert bot.capture_snapshot().state == api.BotState.closed
        assert retained.state == api.BotState.ready


async def test_never_started_bot_disconnect_waiters_after_stop(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        first = bot.disconnect()
        first.cancel()
        await bot.disconnect()
        runtime.request_stop()
        await runtime.wait_closed()
        await asyncio.wait_for(bot.disconnect(), 1)
        assert bot.get_status().state == api.BotState.closed
        assert bot.capture_statistics().packets_out == 0
        assert not peers.connections


@pytest.mark.parametrize("intent", ["disconnect", "reconnect"])
async def test_connecting_callback_invalidates_before_identity_and_dial(monkeypatch, intent):
    from endpilot import session

    peers = Peers(monkeypatch)
    identities = []
    original = session.create_identity

    def create_identity(*args, **kwargs):
        identities.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(session, "create_identity", create_identity)
    changed = asyncio.Event()
    completion = None

    def callback(bot, event):
        nonlocal completion
        if isinstance(event, api.StateEvent) and event.state == api.BotState.connecting and not changed.is_set():
            completion = getattr(bot, intent)()
            changed.set()

    async with api.Runtime(callback) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await changed.wait()
        await asyncio.wait_for(completion, 2)
        if intent == "reconnect":
            await bot.wait_ready(2)
            assert len(peers.connections) == len(identities) == 1
            assert bot.get_status().session_id == 2
        else:
            assert not peers.connections and not identities
            assert bot.get_status().state == api.BotState.closed


def correction(p):
    return p.CorrectPlayerMovePredictionPacket(
        prediction_type=p.RewindType.PLAYER,
        pos=p.Vec3(x=4, y=5, z=6),
        pos_delta=p.Vec3(x=0, y=0, z=0),
        vehicle_rotation=p.Vec2(x=0, y=0),
        vehicle_angular_velocity=None,
        on_ground=True,
        tick=0,
    )


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("correction_kind", ["prediction", "position"])
async def test_hotbar_selection_survives_correction_before_interaction(monkeypatch, version, correction_kind):
    async with scene(monkeypatch, version) as (bot, peer, _):
        await deliver(bot, peer, actor_packet(peer.p))
        peer.pause_sender(bot)
        bot.select_hotbar(1)
        packet = (
            correction(peer.p)
            if correction_kind == "prediction"
            else player_motion(peer.p, bot.get_status().actor_runtime_id)
        )
        await deliver(bot, peer, packet)
        bot.interact_actor(bot.get_observed_entities()[0].ref)
        start = len(peer.received)
        peer.resume_sender()
        await until(lambda: any(packet.packet_id == 30 for packet in peer.received[start:]))
        commands = [packet for packet in peer.received[start:] if packet.packet_id in (30, 31)]
        assert [packet.packet_id for packet in commands] == [31, 30]
        assert commands[0].selected_slot == commands[1].transaction.value.slot == 1
        assert bot.get_status().selected_hotbar == 1
        assert bot.capture_inventory(0).slots[9].stack_size == 10


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("invalidation", ["death", "dimension"])
async def test_stale_equipment_is_not_revived_by_new_selection(monkeypatch, version, invalidation):
    async with scene(monkeypatch, version) as (bot, peer, _):
        p = peer.p
        peer.pause_sender(bot)
        bot.select_hotbar(1)
        if invalidation == "death":
            peer.send(p.SetHealthPacket(health=0))
            await until(lambda: not bot.get_status().alive)
            bot.respawn()
            await deliver(
                bot,
                peer,
                p.RespawnPacket(pos=p.Vec3(x=1, y=2, z=3), state=p.PlayerRespawnState.READY_TO_SPAWN, runtime_id=2),
                p.SetHealthPacket(health=20),
            )
            assert bot.get_status().alive
        else:
            await deliver(
                bot,
                peer,
                p.ChangeDimensionPacket(dimension_id=1, pos=p.Vec3(x=7, y=8, z=9), respawn=False, loading_screen_id=42),
            )
            peer.send(dimension_ack(p, bot.get_status().actor_runtime_id))
            await until(lambda: bot._session.dimension_deadline is None)
            bot._session.tick()
        await deliver(bot, peer, inventory_packet(p, {}))
        bot.select_hotbar(2)
        start = len(peer.received)
        peer.resume_sender()
        await until(lambda: any(packet.packet_id == 31 for packet in peer.received[start:]))
        assert [packet.selected_slot for packet in peer.received[start:] if packet.packet_id == 31] == [2]


@pytest.mark.parametrize("version", [1001, 2193])
async def test_reconnect_discards_queued_equipment(monkeypatch, version):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        old = peers.connections[-1]
        await deliver(bot, old, inventory_packet(old.p, {}))
        old.pause_sender(bot)
        bot.select_hotbar(1)
        await bot.reconnect()
        await bot.wait_ready(2)
        assert old.closed and not any(packet.packet_id == 31 for packet in old.received)
        peer = peers.connections[-1]
        await deliver(bot, peer, inventory_packet(peer.p, {}))
        bot.select_hotbar(2)
        await until(lambda: any(packet.packet_id == 31 for packet in peer.received))
        assert [packet.selected_slot for packet in peer.received if packet.packet_id == 31] == [2]


def player_packet(p):
    item = (
        empty_item(p)
        if p is bp.v2193
        else p.NetworkItemStackDescriptor(
            id=0, stack_size=None, aux_value=None, net_id=None, block_runtime_id=None, user_data_buffer=None
        )
    )
    return p.AddPlayerPacket(
        uuid=UUID(int=1),
        name="observed",
        runtime_id=91,
        platform_online_id="",
        pos=p.Vec3(x=1, y=2, z=3),
        velocity=p.Vec3(x=4, y=5, z=6),
        rot=p.Vec2(x=17, y=29),
        y_head_rot=43,
        carried_item=item,
        player_game_type=p.GameType.SURVIVAL,
        unpack=p.SynchedActorData.CopyableDataList(data=[]),
        synched_properties=p.PropertySyncData(int_entries=[], float_entries=[]),
        abilities_data=p.SerializedAbilitiesData(
            target_player=-92,
            player_permissions=p.PlayerPermissionLevel.MEMBER,
            command_permissions=p.CommandPermissionLevel(0),
            layers=[],
        ),
        links=[
            p.ActorLink(
                a=-92,
                b=-93,
                type=p.ActorLinkType.RIDING,
                immediate=True,
                passenger_initiated=False,
                vehicle_angular_velocity=1.5,
            )
        ],
        device_id="",
        build_platform=p.BuildPlatform(1),
    )


@pytest.mark.parametrize("version", [1001, 2193])
async def test_add_player_preserves_observed_motion_links_and_captures(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, _):
        await deliver(bot, peer, player_packet(peer.p))
        retained = bot.capture_actor(91)
        assert retained.velocity == api.Vec3(4, 5, 6)
        assert (retained.pitch, retained.yaw, retained.head_yaw, retained.body_yaw) == (17, 29, 43, 0)
        assert retained.item is None
        assert retained.links.to_list() == [
            {
                "a": -92,
                "b": -93,
                "type": 1,
                "immediate": True,
                "passenger_initiated": False,
                "vehicle_angular_velocity": 1.5,
            }
        ]
        await bot.reconnect()
        await bot.wait_ready(2)
        assert bot.capture_actor(91) is None
    assert retained.position == api.Vec3(1, 2, 3)
    assert retained.links[0].b == -93


def player_motion(p, runtime_id=91, *, x=7, pitch=31, yaw=47, head=59):
    teleport = {"teleport_data": None} if p is bp.v2193 else {"cause": None, "source_entity_type": None}
    return p.MovePlayerPacket(
        player_id=runtime_id,
        pos=p.Vec3(x=x, y=8, z=9),
        rot=p.Vec2(x=pitch, y=yaw),
        y_head_rot=head,
        reset_position=p.PlayerPositionModeComponent.PositionMode(0),
        on_ground=True,
        riding_id=0,
        tick=5,
        **teleport,
    )


@pytest.mark.parametrize("version", [1001, 2193])
async def test_remote_player_motion_updates_observations_and_keeps_captures(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, events):
        p = peer.p
        await deliver(bot, peer, player_packet(p))
        retained = bot.capture_actor(91)
        events.clear()
        await deliver(bot, peer, player_motion(p), player_motion(p, 999))
        current = bot.capture_actor(91)
        assert current.position == api.Vec3(7, 8, 9)
        assert (current.pitch, current.yaw, current.head_yaw) == (31, 47, 59)
        assert current.velocity == retained.velocity
        assert current.body_yaw == retained.body_yaw
        assert current.links.to_list() == retained.links.to_list()
        assert current.metadata is retained.metadata and current.properties is retained.properties
        assert current.item is None and current.unique_id == retained.unique_id
        assert retained.position == api.Vec3(1, 2, 3)
        assert (retained.pitch, retained.yaw, retained.head_yaw) == (17, 29, 43)
        motions = [event for event in events if isinstance(event, api.ActorMotionEvent)]
        moved = [event for event in events if isinstance(event, api.ActorEvent)]
        assert len(motions) == len(moved) == 1
        assert motions[0].runtime_id == current.runtime_id
        assert motions[0].position == current.position
        assert motions[0].velocity == current.velocity
        assert (motions[0].pitch, motions[0].yaw, motions[0].head_yaw) == (31, 47, 59)
        assert moved[0].kind == api.ActorEvent.Kind.moved and moved[0].actor is current
        assert bot.capture_actor(999) is None
        await deliver(bot, peer, player_motion(p, bot.get_status().actor_runtime_id, x=13))
        assert bot.get_status().position == api.Vec3(13, 8, 9)
        assert bot.capture_actor(91) is current
        assert len([event for event in events if isinstance(event, api.ActorMotionEvent)]) == 1


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("field", ["x", "pitch", "yaw", "head"])
async def test_nonfinite_remote_player_motion_fails_before_publication(monkeypatch, version, field):
    async with scene(monkeypatch, version) as (bot, peer, events):
        await deliver(bot, peer, player_packet(peer.p))
        retained = bot.capture_actor(91)
        events.clear()
        peer.send(player_motion(peer.p, **{field: float("nan")}))
        await until(lambda: peer.closed)
        assert bot.get_status().state == api.BotState.failed
        assert bot.capture_statistics().decode_errors == 1
        assert not any(isinstance(event, (api.ActorMotionEvent, api.ActorEvent)) for event in events)
        assert retained.position == api.Vec3(1, 2, 3)
        assert (retained.pitch, retained.yaw, retained.head_yaw) == (17, 29, 43)


@pytest.mark.parametrize("collection", [api.ActorLinks, api.Actors])
def test_public_collection_copy_detaches_ordinary_nested_containers(collection):
    nested = [1]
    payload = {"tuple": ({"list": nested},)}
    original = collection([payload] if collection is api.ActorLinks else {91: payload})
    copied = original.copy()
    nested.append(2)
    projected = copied[0] if collection is api.ActorLinks else copied[91]
    assert projected.to_dict() == {"tuple": [{"list": [1]}]}
    token = api.BotToken()
    ref = api.InventorySlot(token, 1, api.InventoryArea.player, 0)
    assert ref.copy().bot is token and ref.copy().area is api.InventoryArea.player

@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_actor_id_collisions_remove_previous_snapshot_before_replacement(protocol):
    bot, session, events = ready_bot(protocol)
    first = api.ActorSnapshot(91, -92, "minecraft:player", api.Vec3(1, 2, 3))
    changed_runtime = api.ActorSnapshot(92, -92, "minecraft:player", api.Vec3(4, 5, 6))
    changed_unique = api.ActorSnapshot(92, -93, "minecraft:player", api.Vec3(7, 8, 9))

    session.add_actor(first)
    retained = bot.capture_actor(91)
    events.clear()
    session.add_actor(changed_runtime)
    assert bot.capture_actor(91) is None and bot.capture_actor(92) is changed_runtime
    assert [event.kind for event in events] == [api.ActorEvent.Kind.removed, api.ActorEvent.Kind.added]
    assert events[0].actor is retained
    assert [(entry.ref.runtime_id, entry.ref.unique_id) for entry in bot.get_observed_entities()] == [(92, -92)]

    events.clear()
    session.add_actor(changed_unique)
    assert bot.capture_actor(92) is changed_unique
    assert [event.kind for event in events] == [api.ActorEvent.Kind.removed, api.ActorEvent.Kind.added]
    assert [(entry.ref.runtime_id, entry.ref.unique_id) for entry in bot.get_observed_entities()] == [(92, -93)]
    assert retained.position == api.Vec3(1, 2, 3)


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_actor_replacement_does_not_resurrect_after_removed_callback_invalidation(protocol):
    bot, session, events = ready_bot(protocol)
    first = api.ActorSnapshot(91, -92, "minecraft:player", api.Vec3(1, 2, 3))
    next_actor = api.ActorSnapshot(92, -92, "minecraft:player", api.Vec3(4, 5, 6))
    session.add_actor(first)
    events.clear()

    def callback(_bot, _kind, factory):
        event = factory()
        events.append(event)
        if isinstance(event, api.ActorEvent) and event.kind == api.ActorEvent.Kind.removed:
            session.invalidate("actor removed callback")

    session.runtime._emit = callback
    session.add_actor(next_actor)
    assert not session.usable()
    assert bot.capture_actor(91) is None and bot.capture_actor(92) is None
    assert [event.kind for event in events] == [api.ActorEvent.Kind.removed]


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_player_control_frame_consumption_look_override_and_epoch_invalidation(protocol):
    from test_respawn_retry import queued_packets

    bot, session, _ = ready_bot(protocol)
    bot.set_input(api.Input(position=api.Vec3(7, 8, 9), jumping=True))
    bot.set_look(12, 34)
    session.control.acknowledge_teleport()
    session.tick()
    queued = session.queue[-1]
    assert session.command_valid(queued)
    session.tick()
    first, second = queued_packets(session)
    assert (first.client_tick, second.client_tick) == (1, 2)
    assert (first.rot.x, first.rot.y) == (12, 34)
    assert (second.rot.x, second.rot.y) == (12, 34)
    assert first.pos.x == second.pos.x == 7
    data = session.p.PlayerAuthInputPacket.InputData
    has_flag = (
        (lambda packet, flag: bool(packet.input_data & (1 << int(flag))))
        if protocol == 1001 else (lambda packet, flag: flag in packet.input_data)
    )
    assert has_flag(first, data.JUMPING) and not has_flag(second, data.JUMPING)
    assert has_flag(first, data.HANDLED_TELEPORT) and not has_flag(second, data.HANDLED_TELEPORT)

    epoch = session.control.epoch
    session.position(api.Vec3(10, 11, 12), True, True, False)
    assert session.control.epoch == epoch + 1
    assert not session.command_valid(queued)
    assert session.control.look == api.Vec2(12, 34)
