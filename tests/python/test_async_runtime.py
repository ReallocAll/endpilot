import asyncio
import dataclasses
import subprocess
import sys
import threading

import pytest
from peer import Peers, actor_packet, motion_packet, slot_packet, submit, trade_packet, until
from test_benchmark_hooks import dimension_ack

import endpilot as api


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("server_handshake", [False, True])
async def test_login_ready_identity_reconnect_and_join(monkeypatch, version, server_handshake):
    peers = Peers(monkeypatch, server_handshake=server_handshake)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(timeout=2)
        first = bot.get_status()
        assert first.state == api.BotState.ready
        assert first.position == api.Vec3(1, 2, 3)
        assert first.actor_runtime_id == 2
        assert not bot.count_player_item("minecraft:stone").complete
        p1 = peers.connections[0]
        ids = [x.packet_id for x in p1.received]
        assert ids.count(113) == 1 and ids.count(312) == 2
        await bot.reconnect()
        await bot.wait_ready(timeout=2)
        assert p1.closed
        p2 = peers.connections[1]
        assert p1.login[0]["x5u"] != p2.login[0]["x5u"]
        assert p1.login[1]["leguuid"] == p2.login[1]["leguuid"] == p2.login[2]["SelfSignedId"]
        assert bot.get_status().session_id == first.session_id + 1
        retained = bot.get_status()
        await bot.disconnect()
        assert p2.closed and bot.get_status().state == api.BotState.closed
    with pytest.raises(RuntimeError):
        bot.get_status()
    with pytest.raises(dataclasses.FrozenInstanceError):
        retained.health = 4


async def test_runtime_owner_external_stop_and_independence(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as one, api.Runtime() as two:
        a = one.add_bot(api.BotOptions(server_url="http://peer"))
        b = two.add_bot(api.BotOptions(server_url="http://peer"))
        await asyncio.gather(a.wait_ready(2), b.wait_ready(2))
        errors = []

        def foreign():
            try:
                a.get_status()
            except RuntimeError as exc:
                errors.append(exc)
            one.request_stop()

        t = threading.Thread(target=foreign)
        t.start()
        t.join()
        await one.wait_closed()
        assert len(errors) == 1
        assert a.get_status().state == api.BotState.closed
        assert b.get_status().state == api.BotState.ready
    assert all(peer.closed for peer in peers.connections)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_no_listener_state_input_and_scoped_interaction(monkeypatch, version):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        assert bot.capture_item_registry().observed
        peer.send(actor_packet(p), slot_packet(p), p.SetHealthPacket(health=19))
        await until(lambda: bot.get_status().health == 19)
        entity = bot.get_observed_entities()[0]
        retained = bot.capture_actor(91)
        assert entity.ref.unique_id == -92
        assert bot.get_item(bot.player_slot(0)).state == api.SlotState.empty
        bot.interact_actor(entity.ref)
        bot.set_look(12, 34)
        bot.set_input(api.Input(position=api.Vec3(2, 3, 4)))
        await until(lambda: any(x.packet_id == 144 for x in peer.received))
        frame = next(x for x in peer.received if x.packet_id == 144)
        assert (frame.rot.x, frame.rot.y, frame.pos.x) == (12, 34, 2)
        transaction = next(x for x in peer.received if x.packet_id == 30)
        assert transaction.transaction.value.runtime_id == 91
        bot.probe_latency()
        await until(lambda: any(x.packet_id == 115 for x in peer.received))
        old_slot = bot.player_slot(0)
        await bot.reconnect()
        await bot.wait_ready(2)
        assert retained.unique_id == -92
        with pytest.raises(RuntimeError):
            bot.interact_actor(entity.ref)
        with pytest.raises(RuntimeError):
            bot.get_item(old_slot)


@pytest.mark.parametrize("when", ["queued", "submitted"])
async def test_operation_commit_cancel_and_wait_independence(monkeypatch, when):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", item_request_timeout_ms=100))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        operation = submit(bot)
        assert operation.phase == api.ItemOperationPhase.queued
        with pytest.raises(TimeoutError):
            await operation.wait(0.01)
        assert operation.phase == api.ItemOperationPhase.queued
        waiter = asyncio.create_task(operation.wait())
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        if when == "queued":
            assert bot.cancel_item_operation(operation) == api.ItemCancellation.cancelled
        else:

            def at_commit():
                assert_submitted(operation)
                peer.before_send = None

            peer.before_send = at_commit
        peer.resume_sender()
        result = await operation.wait(2)
        assert result.outcome == (
            api.ItemRequestOutcome.indeterminate if when == "submitted" else api.ItemRequestOutcome.cancelled
        )
        assert bot.cancel_item_operation(operation) == api.ItemCancellation.already_terminal
        assert operation.result is result
        assert bool([p for p in peer.received if p.packet_id == 147]) == (when == "submitted")


def assert_submitted(operation):
    assert operation.phase == api.ItemOperationPhase.submitted


async def test_suspended_close_and_coalesced_reconnect_waiters(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        old = peers.connections[-1]
        old.close_gate.clear()
        old_id = bot.get_status().session_id
        first = bot.reconnect()
        second = bot.reconnect()
        ready = asyncio.create_task(bot.wait_ready(2))
        first.cancel()
        await old.closing.wait()
        assert not second.done() and not ready.done()
        old.close_gate.set()
        await second
        await ready
        assert len(peers.connections) == 2
        assert bot.get_status().session_id == old_id + 1


async def test_callback_failure_identity_and_operation_result(monkeypatch):
    peers = Peers(monkeypatch)
    error = ValueError("callback sentinel")

    def callback(bot, event):
        if isinstance(event, api.ItemRequestEvent):
            assert operation.result is not None
            with pytest.raises(RuntimeError):
                runtime.aclose()
            raise error

    async with api.Runtime(callback) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peers.connections[-1].pause_sender(bot)
        operation = submit(bot)
        with pytest.raises(ValueError) as caught:
            bot.cancel_item_operation(operation)
        assert caught.value is error
        assert (await operation.wait()).outcome == api.ItemRequestOutcome.cancelled
        await runtime.wait_closed()


async def test_background_callback_error_delivered_once_without_cancelling_context(monkeypatch):
    peers = Peers(monkeypatch)
    error = LookupError("background sentinel")

    def callback(bot, event):
        if isinstance(event, api.HealthEvent):
            raise error

    async with api.Runtime(callback, events=api.DEFAULT_EVENTS | api.event_mask(api.EventKind.health)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(peer.p.SetHealthPacket(health=19))
        with pytest.raises(LookupError) as caught:
            await asyncio.wait_for(runtime.wait_closed(), 2)
        assert caught.value is error
        assert peer.closed
        await runtime.wait_closed()


@pytest.mark.parametrize("version", [1001, 2193])
async def test_event_union_order_and_immediate_interest_change(monkeypatch, version):
    peers = Peers(monkeypatch)
    events = []
    change = False

    def callback(bot, event):
        events.append(event)
        if change and isinstance(event, api.ActorMotionEvent):
            runtime.set_event_interests(0)
            bot.set_event_interests(api.event_mask(api.EventKind.inventory_slot))

    mask = api.event_mask(api.EventKind.actor_motion) | api.event_mask(api.EventKind.inventory)
    async with api.Runtime(callback, events=mask) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        bot.set_event_interests(api.event_mask(api.EventKind.actor) | api.event_mask(api.EventKind.inventory_slot))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        peer.send(actor_packet(p), motion_packet(p), slot_packet(p))
        await until(lambda: len(events) == 5)
        assert [type(e) for e in events] == [
            api.ActorEvent,
            api.ActorMotionEvent,
            api.ActorEvent,
            api.InventorySlotEvent,
            api.InventoryEvent,
        ]
        assert events[1].yaw == 45
        change = True
        peer.send(motion_packet(p, 10), slot_packet(p, 1))
        await until(lambda: len(events) >= 7)
        assert [type(e) for e in events[5:]] == [api.ActorMotionEvent, api.InventorySlotEvent]


@pytest.mark.parametrize("reconnect", [False, True])
async def test_callback_lifecycle_invalidates_later_emissions(monkeypatch, reconnect):
    peers = Peers(monkeypatch)
    events = []
    armed = False
    completion = None

    def callback(bot, event):
        nonlocal completion
        if armed:
            events.append(event)
            completion = bot.reconnect() if reconnect else bot.disconnect()

    mask = api.event_mask(api.EventKind.actor_motion) | api.event_mask(api.EventKind.actor)
    async with api.Runtime(callback, events=mask) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(actor_packet(peer.p))
        await until(lambda: len(bot.get_observed_entities()) == 1)
        armed = True
        peer.send(motion_packet(peer.p), motion_packet(peer.p, 20))
        await until(lambda: completion is not None)
        await completion
        assert len(events) == 1
        assert isinstance(events[0], api.ActorMotionEvent)
        assert peer.closed


async def test_nested_caught_callback_failure_not_resurrected(monkeypatch):
    peers = Peers(monkeypatch)
    error = ValueError("nested")
    events = []

    def callback(bot, event):
        if isinstance(event, api.ItemRequestEvent):
            events.append("inner")
            raise error
        if isinstance(event, api.HealthEvent):
            events.append("outer")
            with pytest.raises(ValueError) as caught:
                bot.cancel_item_operation(operation)
            assert caught.value is error

    async with api.Runtime(callback, events=api.DEFAULT_EVENTS | api.event_mask(api.EventKind.health)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        operation = submit(bot)
        peer.send(peer.p.SetHealthPacket(health=19), peer.p.SetHealthPacket(health=18))
        await asyncio.wait_for(runtime.wait_closed(), 2)
        assert events == ["outer", "inner"]
        assert (await operation.wait()).outcome == api.ItemRequestOutcome.cancelled


@pytest.mark.parametrize("submitted", [False, True])
async def test_pending_shutdown_and_thread_read_result(monkeypatch, submitted):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        if not submitted:
            peer.pause_sender(bot)
        operation = submit(bot)
        if submitted:
            await until(lambda: operation.phase == api.ItemOperationPhase.submitted)
            assert bot.cancel_item_operation(operation) == api.ItemCancellation.already_submitted
        errors = []
        done = threading.Event()

        def read():
            while not done.is_set():
                if operation.phase == api.ItemOperationPhase.terminal and operation.result is None:
                    errors.append("terminal without result")

        thread = threading.Thread(target=read)
        thread.start()
        try:
            await bot.disconnect()
        finally:
            done.set()
            thread.join(2)
        result = await operation.wait()
        assert not errors and not thread.is_alive()
        assert result.outcome == (
            api.ItemRequestOutcome.indeterminate if submitted else api.ItemRequestOutcome.cancelled
        )


async def test_cancelled_close_waiter_cannot_cancel_cleanup(monkeypatch):
    peers = Peers(monkeypatch)
    runtime = await api.Runtime().__aenter__()
    bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
    await bot.wait_ready(2)
    peer = peers.connections[-1]
    peer.close_gate.clear()
    close = asyncio.create_task(runtime.aclose())
    await peer.closing.wait()
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert not peer.closed
    peer.close_gate.set()
    await runtime.aclose()
    assert peer.closed


async def test_reconnect_superseded_by_disconnect_and_ready_generation(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        old = peers.connections[-1]
        old.close_gate.clear()
        reconnect = bot.reconnect()
        ready = asyncio.create_task(bot.wait_ready(2))
        await old.closing.wait()
        disconnect = bot.disconnect()
        old.close_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await reconnect
        with pytest.raises(RuntimeError):
            await ready
        await disconnect
        assert len(peers.connections) == 1
        await bot.reconnect()
        await bot.wait_ready(2)
        assert bot.get_status().session_id == 3


@pytest.mark.parametrize("version", [1001, 2193])
async def test_window_identity_death_respawn_dimension_and_correction(monkeypatch, version):
    peers = Peers(monkeypatch)
    events = []
    async with api.Runtime(lambda bot, e: events.append(e)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        peer.send(actor_packet(p), slot_packet(p))
        await until(lambda: bot.capture_inventory(0) is not None)
        bot.interact_actor(bot.get_observed_entities()[0].ref)
        peer.send(trade_packet(p))
        await until(lambda: bot.get_trade_window() is not None)
        window = bot.get_trade_window().ref
        assert window.trader_unique_id == -92 and window.generation == 1
        bot.close_container(window)
        assert bot.capture_container().closing
        peer.send(
            p.ContainerClosePacket(
                container_id=p.ContainerID(5), container_type=p.ContainerType(15), server_initiated_close=True
            )
        )
        await until(lambda: bot.capture_container() is None)
        assert any(isinstance(e, api.ContainerEvent) and e.server_closed and e.reference == window for e in events)
        bot.set_look(15, 25)
        bot.set_input(api.Input(position=api.Vec3(99, 99, 99)))
        peer.send(
            p.CorrectPlayerMovePredictionPacket(
                prediction_type=p.RewindType.PLAYER,
                pos=p.Vec3(x=4, y=5, z=6),
                pos_delta=p.Vec3(x=0, y=0, z=0),
                vehicle_rotation=p.Vec2(x=0, y=0),
                vehicle_angular_velocity=None,
                on_ground=True,
                tick=0,
            )
        )
        await until(lambda: bot.capture_statistics().corrections == 1)
        start = len(peer.received)
        await until(lambda: any(x.packet_id == 144 for x in peer.received[start:]))
        frame = next(x for x in peer.received[start:] if x.packet_id == 144)
        assert (frame.pos.x, frame.rot.x, frame.rot.y) == (4, 15, 25)
        peer.send(p.SetHealthPacket(health=0))
        await until(lambda: not bot.get_status().alive)
        bot.respawn()
        bot.respawn()
        peer.send(
            p.RespawnPacket(pos=p.Vec3(x=1, y=2, z=3), state=p.PlayerRespawnState.READY_TO_SPAWN, runtime_id=2),
            p.RespawnPacket(pos=p.Vec3(x=1, y=2, z=3), state=p.PlayerRespawnState.READY_TO_SPAWN, runtime_id=2),
        )
        await until(lambda: any(x.packet_id == 36 for x in peer.received))
        assert not bot.get_status().alive
        peer.send(p.SetHealthPacket(health=20))
        await until(lambda: bot.get_status().alive)
        assert bot.capture_statistics().respawns == 1
        bot.set_look(2, 3)
        peer.send(
            p.ChangeDimensionPacket(dimension_id=1, pos=p.Vec3(x=7, y=8, z=9), respawn=False, loading_screen_id=42)
        )
        await until(lambda: bot.get_status().dimension == 1)
        peer.send(dimension_ack(p, bot.get_status().actor_runtime_id))
        await until(lambda: bot._session.dimension_deadline is None)
        assert bot.get_observed_entities() == [] and bot.get_status().selected_hotbar is None
        assert bot._session.look is None


@pytest.mark.parametrize("version", [1001, 2193])
async def test_server_operation_response_once_stale_session_ignored(monkeypatch, version):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        old = peers.connections[-1]
        operation = submit(bot)
        await until(lambda: operation.phase == api.ItemOperationPhase.submitted)
        p = old.p
        response = p.ItemStackResponsePacket(
            responses=[
                p.ItemStackResponseInfo(
                    result=p.ItemStackNetResult.SUCCESS,
                    client_request_id=p.ItemStackRequestId(raw_id=operation.key.request_id),
                    containers=[],
                )
            ]
        )
        old.send(response, response)
        result = await operation.wait(2)
        assert result.outcome == api.ItemRequestOutcome.completed
        await bot.reconnect()
        await bot.wait_ready(2)
        current = submit(bot)
        old.send(response)
        await until(lambda: current.phase == api.ItemOperationPhase.submitted)
        assert current.result is None and operation.result is result


async def test_partial_dial_cancellation_joins_opener_cleanup(monkeypatch):
    from endpilot import transport

    entered = asyncio.Event()
    cleanup = asyncio.Event()
    release = asyncio.Event()

    async def open(options, identity):
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleanup.set()
            await release.wait()

    monkeypatch.setattr(transport, "_open_connection", open)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await entered.wait()
        close = bot.disconnect()
        await cleanup.wait()
        assert not close.done()
        release.set()
        await close


async def test_context_cancellation_during_teardown_joins_before_exit(monkeypatch):
    peers = Peers(monkeypatch)
    started = asyncio.Event()

    async def owner():
        async with api.Runtime() as runtime:
            bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
            await bot.wait_ready(2)
            peers.connections[-1].close_gate.clear()
            started.set()

    task = asyncio.create_task(owner())
    await started.wait()
    peer = peers.connections[-1]
    await peer.closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    peer.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peer.closed


@pytest.mark.parametrize("version", [1001, 2193])
async def test_resource_pack_handshake_and_duplicate_ready_acknowledgements(monkeypatch, version):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        from uuid import UUID

        peer.send(
            p.ResourcePacksInfoPacket(
                resource_pack_required=False,
                has_addon_packs=False,
                has_scripts=False,
                force_disable_vibrant_visuals=False,
                world_template_id_and_version=p.PackIdVersion(id=UUID(int=0), version=p.SemVersion(version="0.0.0")),
                resource_packs=[],
            ),
            p.ResourcePackStackPacket(
                texture_pack_required=False,
                texture_pack_ids_and_versions=[],
                base_game_version="1.26.50",
                experiments=p.Experiments(toggles=[], experiments_ever_toggled=False),
                include_editor_packs=False,
            ),
            p.PlayStatusPacket(status=p.PlayStatus.PLAYER_SPAWN),
            p.ChunkRadiusUpdatedPacket(chunk_radius=10),
        )
        await until(lambda: len([x for x in peer.received if x.packet_id == 8]) == 2)
        assert len([x for x in peer.received if x.packet_id == 113]) == 1
        assert len([x for x in peer.received if x.packet_id == 312]) == 2


async def test_queue_limits_fail_visibly_and_bound_typed_payload(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        with pytest.raises(RuntimeError, match="queue limit"):
            for _ in range(4097):
                bot.request_chunk_radius(8)
        await until(lambda: peer.closed)
        assert bot.get_status().state == api.BotState.failed


def test_process_exits_within_twenty_seconds():
    code = """
import asyncio
from endpilot import Runtime, BotOptions
from endpilot import transport
import runpy, sys
Peer = runpy.run_path(sys.argv[1])["Peer"]
async def open(options, identity): return Peer().connection
transport._open_connection = open
async def main():
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        bot.set_look(10,20)
        await bot.reconnect()
        await bot.wait_ready(2)
        await bot.disconnect()
asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
"""
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(__file__).with_name("peer.py"))],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr


@pytest.mark.parametrize("version", [1001, 2193])
async def test_radius_blob_subchunk_and_chunk_notifications(monkeypatch, version):
    peers = Peers(monkeypatch)
    events = []
    async with api.Runtime(lambda b, e: events.append(e), events=api.event_mask(api.EventKind.chunk)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        bot.request_chunk_radius(96)
        bot.report_blob_status([1, 2], [3])
        bot.request_sub_chunks(0, 1, 2, [-1, 0, 1])
        await until(lambda: any(x.packet_id == 175 for x in peer.received))
        sub = next(x for x in peer.received if x.packet_id == 175)
        assert [x.y for x in sub.sub_chunk_pos_offsets] == [-1, 0, 1]
        peer.send(
            p.LevelChunkPacket(
                pos=p.ChunkPos(x=1, z=2),
                dimension_id=0,
                sub_chunks_count=0,
                client_request_sub_chunk_limit=None,
                cache_enabled=False,
                cache_metadata=[] if version == 2193 else None,
                serialized_chunk=b"",
            )
        )
        await until(lambda: len(events) == 1)
        assert events[0].kind == api.ChunkEvent.Kind.column
        assert (events[0].x, events[0].z) == (1, 2)
        assert bot.capture_statistics().column_packets == 1


async def test_no_listener_never_constructs_notification_payload(monkeypatch):
    from endpilot import events

    peers = Peers(monkeypatch)

    def forbidden(*args):
        raise AssertionError("optional payload constructed")

    monkeypatch.setattr(events, "HealthEvent", forbidden)
    async with api.Runtime(events=api.ALL_EVENTS) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(peer.p.SetHealthPacket(health=16))
        await until(lambda: bot.get_status().health == 16)


async def test_submitted_wait_cancellation_does_not_cancel_server_result(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        operation = submit(bot)
        await until(lambda: operation.phase == api.ItemOperationPhase.submitted)
        with pytest.raises(TimeoutError):
            await operation.wait(0.001)
        waiter = asyncio.create_task(operation.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert operation.phase == api.ItemOperationPhase.submitted
        peer = peers.connections[-1]
        p = peer.p
        peer.send(
            p.ItemStackResponsePacket(
                responses=[
                    p.ItemStackResponseInfo(
                        result=p.ItemStackNetResult.ERROR,
                        client_request_id=p.ItemStackRequestId(raw_id=operation.key.request_id),
                        containers=None,
                    )
                ]
            )
        )
        result = await operation.wait(2)
        assert result.outcome == api.ItemRequestOutcome.rejected
    assert operation.result is result


async def test_tick_callback_supplies_frame_and_skips_missed_intervals(monkeypatch):
    peers = Peers(monkeypatch)
    calls = 0

    def callback(bot, event):
        nonlocal calls
        if isinstance(event, api.TickEvent):
            calls += 1
            bot.set_input(api.Input(position=api.Vec3(10 + calls, 2, 3)))
            if calls == 1:
                import time

                time.sleep(0.22)

    async with api.Runtime(callback, events=api.event_mask(api.EventKind.tick)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        await until(lambda: calls >= 2)
        peer = peers.connections[-1]
        await until(lambda: len([x for x in peer.received if x.packet_id == 144]) >= 2)
        frames = [x for x in peer.received if x.packet_id == 144]
        assert (frames[0].pos.x, frames[1].pos.x) == (11, 12)
        assert calls == 2


async def test_operation_timeout_starts_only_at_submission(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", item_request_timeout_ms=30))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        operation = submit(bot)
        await asyncio.sleep(0.12)
        assert operation.phase == api.ItemOperationPhase.queued and operation.result is None
        peer.resume_sender()
        result = await operation.wait(2)
        assert result.outcome == api.ItemRequestOutcome.indeterminate


async def test_background_failure_from_ready_callback_reaches_wait_ready_once(monkeypatch):
    Peers(monkeypatch)
    error = RuntimeError("ready callback")

    def callback(bot, event):
        if isinstance(event, api.StateEvent) and event.state == api.BotState.ready:
            raise error

    async with api.Runtime(callback) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        with pytest.raises(RuntimeError) as caught:
            await bot.wait_ready(2)
        assert caught.value is error
        await runtime.wait_closed()


async def test_failed_dial_and_spawn_deadline_join_all_tasks(monkeypatch):
    from endpilot import transport

    async def refused(options, identity):
        raise OSError("dial failed")

    monkeypatch.setattr(transport, "_open_connection", refused)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        with pytest.raises(OSError):
            await bot.wait_ready(2)
        await bot.disconnect()
    peers = Peers(monkeypatch, automatic=False)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", spawn_timeout_ms=20))
        with pytest.raises(TimeoutError):
            await bot.wait_ready(2)
        await bot.disconnect()
        assert peers.connections[-1].closed


@pytest.mark.parametrize("reconnect", [False, True], ids=["initial", "reconnect"])
async def test_wait_ready_preserves_completed_dial_failure(monkeypatch, reconnect):
    from endpilot import transport

    error = OSError("dial failed before readiness wait")

    async def refused(options, identity):
        raise error

    Peers(monkeypatch)
    async with api.Runtime() as runtime:
        if reconnect:
            bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
            await bot.wait_ready(2)
        monkeypatch.setattr(transport, "_open_connection", refused)
        if reconnect:
            await bot.reconnect()
        else:
            bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await until(lambda: bot.get_status().state == api.BotState.failed)
        with pytest.raises(OSError) as caught:
            await bot.wait_ready(2)
        assert caught.value is error


async def test_explicit_disconnect_emits_joined_lifecycle_states(monkeypatch):
    peers = Peers(monkeypatch)
    states = []

    def callback(bot, event):
        if isinstance(event, api.StateEvent):
            states.append(event.state)
            if event.state == api.BotState.closed:
                assert peers.connections[-1].closed

    async with api.Runtime(callback) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        await bot.disconnect()
        assert states[-2:] == [api.BotState.closing, api.BotState.closed]


async def test_nonfinite_actor_packet_fails_without_publishing_motion(monkeypatch):
    peers = Peers(monkeypatch)
    events = []
    async with api.Runtime(lambda b, e: events.append(e), events=api.event_mask(api.EventKind.actor_motion)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(actor_packet(peer.p), motion_packet(peer.p, float("nan")))
        await until(lambda: peer.closed)
        assert not events
        assert bot.capture_statistics().decode_errors == 1
        assert bot.get_observed_entities() == []


@pytest.mark.parametrize("intent", ["disconnect", "reconnect", "stop"])
async def test_review_timeout_unwind_is_joined_without_second_cancellation(monkeypatch, intent):
    from peer import Peer

    from endpilot import transport

    unwinding = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    interrupted = asyncio.Event()
    calls = 0

    async def open_connection(options, identity):
        nonlocal calls
        calls += 1
        if calls > 1:
            return Peer().connection
        try:
            await asyncio.Future()
        finally:
            unwinding.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            finished.set()

    monkeypatch.setattr(transport, "_open_connection", open_connection)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", connect_timeout_ms=20))
        await asyncio.wait_for(unwinding.wait(), 2)
        if intent == "stop":
            runtime.request_stop()
            completion = asyncio.create_task(runtime.wait_closed())
        else:
            completion = getattr(bot, intent)()
        try:
            await asyncio.sleep(0.02)
            assert not interrupted.is_set()
            assert not completion.done()
            assert not finished.is_set()
        finally:
            release.set()
        await asyncio.wait_for(completion, 2)
        assert finished.is_set()
        if intent == "reconnect":
            await bot.wait_ready(2)


def _review_metadata(p, entry_id, value):
    import bedrock_protocol as bp

    return p.SynchedActorData.CopyableDataList(
        data=[p.DataItemEntry(id=entry_id, payload=bp.Variant(2, p.DataItemIntPayload(value=value)))]
    )


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("actor_kind", ["item", "player"])
async def test_review_actor_initial_values_and_optional_property_merge(monkeypatch, version, actor_kind):
    from uuid import UUID

    from peer import empty_item

    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        position = p.Vec3(x=1, y=2, z=3)
        metadata = _review_metadata(p, 7, 37)
        properties = p.PropertySyncData(
            int_entries=[], float_entries=[p.PropertySyncData.PropertySyncFloatEntry(property_index=2, data=1.5)]
        )
        item = (
            empty_item(p)
            if version == 2193
            else p.NetworkItemStackDescriptor(
                id=0, stack_size=None, aux_value=None, net_id=None, block_runtime_id=None, user_data_buffer=None
            )
        )
        if actor_kind == "item":
            packet = p.AddItemActorPacket(
                id=-92, runtime_id=91, item=item, pos=position, velocity=position, data=metadata, is_from_fishing=False
            )
        else:
            packet = p.AddPlayerPacket(
                uuid=UUID(int=1),
                name="observed",
                runtime_id=91,
                platform_online_id="",
                pos=position,
                velocity=position,
                rot=p.Vec2(x=0, y=0),
                y_head_rot=0,
                carried_item=item,
                player_game_type=p.GameType.SURVIVAL,
                unpack=metadata,
                synched_properties=properties,
                abilities_data=p.SerializedAbilitiesData(
                    target_player=-92,
                    player_permissions=p.PlayerPermissionLevel.MEMBER,
                    command_permissions=p.CommandPermissionLevel(0),
                    layers=[],
                ),
                links=[],
                device_id="",
                build_platform=p.BuildPlatform(1),
            )
        peer.send(packet)
        await until(lambda: bot.capture_actor(91) is not None)
        retained = bot.capture_actor(91)
        assert retained.metadata is not None
        assert retained.metadata.data[0].payload.value.value == 37
        if actor_kind == "item":
            assert retained.properties is None
        else:
            assert retained.properties is not None
            assert retained.properties.float_entries[0].data == 1.5
        peer.send(
            p.SetActorDataPacket(
                id=91,
                packed_items=_review_metadata(p, 8, 55),
                synched_properties=p.PropertySyncData(
                    int_entries=[p.PropertySyncData.PropertySyncIntEntry(property_index=3, data=9)], float_entries=[]
                ),
                tick=0,
            )
        )
        await until(
            lambda: (
                bot.get_status().state != api.BotState.ready
                or bot.capture_actor(91).properties is not None
                and len(bot.capture_actor(91).properties.int_entries) == 1
            )
        )
        assert bot.get_status().state == api.BotState.ready
        merged = bot.capture_actor(91)
        assert [entry.id for entry in merged.metadata.data] == [7, 8]
        assert merged.properties.int_entries[0].data == 9
        assert len(merged.properties.float_entries) == (0 if actor_kind == "item" else 1)
        assert len(retained.metadata.data) == 1
        assert retained.properties is None if actor_kind == "item" else len(retained.properties.int_entries) == 0


async def test_review_background_callback_cancelled_error_is_delivered_once(monkeypatch):
    peers = Peers(monkeypatch)
    error = asyncio.CancelledError("callback cancellation sentinel")

    def callback(bot, event):
        if isinstance(event, api.HealthEvent):
            raise error

    async with api.Runtime(callback, events=api.event_mask(api.EventKind.health)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(peer.p.SetHealthPacket(health=19))
        async with asyncio.timeout(2):
            with pytest.raises(asyncio.CancelledError) as caught:
                await runtime.wait_closed()
        assert caught.value is error
        assert asyncio.current_task().cancelling() == 0
        assert peer.closed
        await runtime.wait_closed()


async def test_review_default_events_preserve_historical_admission(monkeypatch):
    peers = Peers(monkeypatch)
    events = []
    async with api.Runtime(lambda bot, event: events.append(event)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(peer.p.SetHealthPacket(health=19), peer.p.SetHealthPacket(health=0))
        await until(lambda: not bot.get_status().alive)
        assert any(isinstance(event, api.DeathEvent) for event in events)
        assert not any(isinstance(event, (api.HealthEvent, api.TickEvent)) for event in events)
        assert api.DEFAULT_EVENTS == runtime.get_event_interests() == 0xA071
        assert api.LEGACY_EVENTS == 0xFFFF


@pytest.mark.parametrize("capture", ["after_disconnect", "before_disconnect", "before_reconnect"])
async def test_review_ready_success_requires_current_valid_generation(monkeypatch, capture):
    Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        waiting = None if capture == "after_disconnect" else bot.wait_ready(2)
        if capture == "before_reconnect":
            await bot.reconnect()
            await bot.wait_ready(2)
        else:
            await bot.disconnect()
        with pytest.raises(RuntimeError):
            if waiting is None:
                await bot.wait_ready(2)
            else:
                await waiting


async def test_review_cancelled_aclose_waiter_cannot_preserve_bot_access(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.close_gate.clear()
        closing = asyncio.create_task(runtime.aclose())
        await peer.closing.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        peer.close_gate.set()
        await runtime.wait_closed()
        assert peer.closed
        with pytest.raises(RuntimeError):
            bot.get_status()


@pytest.mark.parametrize("close_mode", ["disconnect", "runtime_close"])
async def test_wait_ready_timeout_then_close_has_no_loop_error(monkeypatch, close_mode):
    peers = Peers(monkeypatch, automatic=False)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    contexts = []
    loop.set_exception_handler(lambda loop, context: contexts.append(context))
    try:
        async with api.Runtime() as runtime:
            bot = runtime.add_bot(api.BotOptions(server_url="http://peer", spawn_timeout_ms=5000))
            ready = bot._ready
            with pytest.raises(TimeoutError):
                await bot.wait_ready(timeout=0.02)
            await asyncio.sleep(0)
            assert bot._ready is ready and not ready.done() and not ready.cancelled()
            if close_mode == "disconnect":
                await bot.disconnect()
            else:
                await runtime.aclose()
        await asyncio.sleep(0)
        assert ready.done() and not ready.cancelled() and peers.connections[-1].closed
        assert not contexts, contexts
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
@pytest.mark.parametrize("outcome", ["ready", "error"])
async def test_wait_ready_interrupted_waiter_preserves_later_result(monkeypatch, interruption, outcome):
    from peer import Peer

    from endpilot import transport

    entered = asyncio.Event()
    release = asyncio.Event()
    error = OSError("delayed connection failure")

    async def open_connection(options, identity):
        entered.set()
        await release.wait()
        if outcome == "error":
            raise error
        return Peer().connection

    monkeypatch.setattr(transport, "_open_connection", open_connection)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    contexts = []
    loop.set_exception_handler(lambda loop, context: contexts.append(context))
    try:
        async with api.Runtime() as runtime:
            bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
            ready = bot._ready
            waiting = asyncio.create_task(bot.wait_ready(timeout=0.02 if interruption == "timeout" else None))
            await entered.wait()
            if interruption == "cancel":
                waiting.cancel()
            with pytest.raises(TimeoutError if interruption == "timeout" else asyncio.CancelledError):
                await waiting
            assert bot._ready is ready and not ready.done() and not ready.cancelled()
            later = asyncio.create_task(bot.wait_ready(timeout=2))
            release.set()
            if outcome == "error":
                with pytest.raises(OSError) as caught:
                    await later
                assert caught.value is error
                with pytest.raises(OSError) as repeated:
                    await bot.wait_ready(timeout=2)
                assert repeated.value is error
            else:
                await later
                await bot.wait_ready(timeout=2)
                assert bot.get_status().state == api.BotState.ready
        await asyncio.sleep(0)
        assert not ready.cancelled()
        assert not contexts, contexts
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_independent_event_observers_and_failure_isolation(monkeypatch, version):
    peers = Peers(monkeypatch)
    loop = asyncio.get_running_loop()
    errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        events = []
        after_unsubscribe = []
        mask = api.event_mask(api.EventKind.health)

        def observe(_bot, event):
            events.append(event.health)

        def fail(_bot, _event):
            raise ValueError("observer sentinel")

        async with api.Runtime() as runtime:
            first = runtime.subscribe(observe, events=mask)
            second = runtime.subscribe(observe, events=mask)
            runtime.subscribe(fail, events=mask)
            runtime.subscribe(lambda _bot, e: after_unsubscribe.append(e.health), events=mask)
            bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
            await bot.wait_ready(2)
            peer = peers.connections[-1]

            peer.send(peer.p.SetHealthPacket(health=19))
            await until(lambda: events.count(19) == 2 and len(errors) == 1)
            assert errors[0]["message"] == "EndPilot event observer failed"
            assert isinstance(errors[0]["exception"], ValueError)
            assert bot.get_status().state == api.BotState.ready

            first()
            first()  # Unsubscribing twice is harmless, even with duplicate callbacks.
            peer.send(peer.p.SetHealthPacket(health=18))
            await until(lambda: events.count(18) == 1 and after_unsubscribe[-1:] == [18])
            assert errors[-1]["exception"].args == ("observer sentinel",)
            second()
            peer.send(peer.p.SetHealthPacket(health=17))
            await until(lambda: bot.get_status().health == 17 and after_unsubscribe[-1:] == [17])
            assert events == [19, 19, 18]
            assert len(errors) == 3
    finally:
        loop.set_exception_handler(previous_handler)


async def test_subscription_validation_and_async_return_isolation(monkeypatch):
    peers = Peers(monkeypatch)
    errors = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        async with api.Runtime() as runtime:
            async def coroutine_observer(_bot, _event):
                pass

            with pytest.raises(TypeError):
                runtime.subscribe(coroutine_observer)
            with pytest.raises(ValueError):
                runtime.subscribe(lambda *_: None, events=-1)

            def returns_coroutine(_bot, _event):
                return coroutine_observer(_bot, _event)

            runtime.subscribe(returns_coroutine, events=api.event_mask(api.EventKind.health))
            healthy = []
            runtime.subscribe(lambda _bot, event: healthy.append(event.health),
                              events=api.event_mask(api.EventKind.health))
            bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
            await bot.wait_ready(2)
            peers.connections[-1].send(peers.connections[-1].p.SetHealthPacket(health=19))
            await until(lambda: healthy == [19] and bool(errors))
            assert isinstance(errors[0]["exception"], TypeError)
            assert bot.get_status().state == api.BotState.ready
    finally:
        loop.set_exception_handler(old_handler)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_public_input_controls_and_native_flags(monkeypatch, version):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        enum = peer.p.PlayerAuthInputPacket.InputData

        def enabled(packet, name):
            flag = getattr(enum, name)
            return (
                bool(packet.input_data & (1 << int(flag)))
                if version == 1001 else flag in packet.input_data
            )

        start = len(peer.received)
        bot.set_input(api.Input(
            position=bot.get_status().position, move=api.Vec2(0.25, 0.5),
            jumping=True, sneaking=True, sprinting=True,
            flags=("START_JUMPING", "START_SNEAKING", "START_SPRINTING"),
        ))
        await until(lambda: any(packet.packet_id == 144 for packet in peer.received[start:]))
        packet = next(packet for packet in peer.received[start:] if packet.packet_id == 144)
        assert (packet.move.x, packet.move.y) == (0.25, 0.5)
        assert all(enabled(packet, name) for name in (
            "JUMPING", "SNEAKING", "SPRINTING",
            "START_JUMPING", "START_SNEAKING", "START_SPRINTING", "UP", "RIGHT",
        ))
        with pytest.raises(ValueError, match="state-owned"):
            bot.set_input(api.Input(flags=("HANDLED_TELEPORT",)))
        with pytest.raises(ValueError, match="state-owned"):
            bot.set_input(api.Input(flags=("PERFORM_ITEM_STACK_REQUEST",)))
        with pytest.raises(ValueError, match="this protocol"):
            bot.set_input(api.Input(flags=("NOT_AN_INPUT_FLAG",)))
        assert bot.get_status().state == api.BotState.ready

        # Player input is one-shot. A later frame must not inherit stale control flags.
        await until(lambda: sum(p.packet_id == 144 for p in peer.received[start:]) >= 2)
        later = [p for p in peer.received[start:] if p.packet_id == 144][1]
        assert not any(enabled(later, name) for name in (
            "JUMPING", "SNEAKING", "SPRINTING", "START_JUMPING", "START_SPRINTING",
        ))
