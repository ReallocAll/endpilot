import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import MiningAborted, MiningEvent
from endpilot.blocks import HASHED_AIR_RUNTIME_ID, Blocks, BlockObservation, BlockUpdate, MiningTarget
from endpilot.inventory import Inventory
from endpilot.movement import Movement
from endpilot.protocol import Protocol

from test_subchunks import solid_sub_chunk


class PacketStream:
    def __init__(self, protocol):
        self.protocol = protocol
        self.observers = []
        self.sent = []

    def subscribe_raw(self, observer):
        self.observers.append(observer)

        def unsubscribe():
            self.observers.remove(observer)

        return unsubscribe

    def subscribe_reader_end(self, observer):
        return lambda: None

    def _reader_end_result(self):
        return False, None

    def emit(self, raw):
        for observer in tuple(self.observers):
            observer(raw)

    async def wait_for_packet(self, packet_id, *, timeout, match=None):
        future = asyncio.get_running_loop().create_future()
        packet_ids = (packet_id,) if isinstance(packet_id, int) else packet_id

        def observe(raw):
            if not future.done() and raw.packet_id in packet_ids and (match is None or match(raw)):
                future.set_result(raw)

        unsubscribe = self.subscribe_raw(observe)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            unsubscribe()

    async def send(self, packet, *, _before_send=None):
        if _before_send is not None:
            _before_send()
        self.sent.append(packet)

    def _defer_receiving_until(self, _ready):
        # Tests drive packet delivery directly through emit().
        pass

    def start_receiving(self, *, _before_dispatch=None):
        pass


class BlocksTests(unittest.IsolatedAsyncioTestCase):
    def mining_fixture(self, *, hashed=True):
        protocol = Protocol(2225)
        b = protocol.binding
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        inventory = Inventory(stream, 7)
        blocks = Blocks(stream, movement, inventory, hashed_runtime_ids=hashed)
        blocks.start()
        self.addCleanup(inventory.close)
        self.addCleanup(blocks.close)

        def observe(position, runtime_id=0x80310E21, *, layer=0):
            x, y, z = position
            stream.emit(protocol.encode(b.UpdateBlockPacket(
                pos=b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=layer,
            )))

        return protocol, stream, blocks, observe

    async def test_phased_start_switch_and_release_share_one_target(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        a, b = (1, 100, 0), (2, 100, 0)
        observe(a)
        observe(b)
        self.assertEqual(await blocks.start_mining(a, face=4), 1)
        self.assertEqual(blocks.mining_target, MiningTarget(a, 4))
        self.assertEqual(await blocks.continue_mining(a, face=4), 2)
        self.assertEqual(await blocks.continue_mining(b, face=4), 3)
        self.assertEqual(blocks.mining_target, MiningTarget(b, 4))
        sent = [protocol.decode(protocol.encode(frame)) for frame in stream.sent]
        actions = [[action.player_action_type.name for action in frame.player_block_actions] for frame in sent]
        self.assertEqual(actions, [
            ["START_DESTROY_BLOCK"], ["CONTINUE_DESTROY_BLOCK"],
            ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK", "START_DESTROY_BLOCK"],
        ])
        self.assertEqual(sent[-1].player_block_actions[0].pos.x, a[0])
        self.assertEqual(sent[-1].player_block_actions[-1].pos.x, b[0])
        observe(b, HASHED_AIR_RUNTIME_ID)
        self.assertIsNone(blocks.mining_target)
        self.assertFalse(blocks.observed_block(a).is_air)
        observe(b)
        self.assertIsNone(blocks.mining_target)
        self.assertEqual(await blocks.stop_mining(), 4)

    async def test_mining_event_captures_first_sparse_air_even_when_the_same_packet_overwrites_it(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        observe(target)
        calls, errors = [], []

        def observer(event):
            calls.append((event, blocks.observed_block(target)))

        def broken(_event):
            raise ValueError("observer failed")

        unsubscribe = blocks.subscribe_mining_events(observer)
        remove_observer = blocks.subscribe_mining_events(observer)
        remove_broken = blocks.subscribe_mining_events(broken)
        unsubscribe()
        unsubscribe()
        await blocks.start_mining(target, face=4)
        observe((2, 100, 0), HASHED_AIR_RUNTIME_ID)
        observe(target, HASHED_AIR_RUNTIME_ID, layer=1)
        await asyncio.sleep(0)
        self.assertEqual(calls, [])

        pos = b.BlockPos(x=1, y=100, z=0)
        update = b.UpdateSubChunkBlocksPacket(
            sub_chunk_block_position=pos,
            blocks_changed=b.UpdateSubChunkBlocksChangedInfo(standards=[
                b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=HASHED_AIR_RUNTIME_ID),
                b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=0x80310E21),
            ]),
        )
        loop = asyncio.get_running_loop()
        before = loop.get_exception_handler()
        try:
            loop.set_exception_handler(lambda loop, context: errors.append(context["exception"]))
            stream.emit(protocol.encode(update))
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(before)
            remove_observer()
            remove_broken()
        (event, current), = calls
        self.assertEqual(event, MiningEvent("completed", 0, MiningTarget(target, 4),
                                            BlockObservation(target, HASHED_AIR_RUNTIME_ID, 0, True)))
        self.assertFalse(current.is_air)
        self.assertEqual(len(stream.sent), 1)  # No second client action caused the observation.
        self.assertEqual([str(error) for error in errors], ["observer failed"])
        with self.assertRaises(FrozenInstanceError):
            event.kind = "aborted"
        with self.assertRaises(FrozenInstanceError):
            event.block.is_air = False

    async def test_mining_events_use_real_air_from_the_target_section(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        events = []
        remove = blocks.subscribe_mining_events(events.append)
        self.addCleanup(remove)

        def section(x, dimension, data):
            result = b.SubChunkPacket.SubChunkRequestResult
            stream.emit(protocol.encode(b.SubChunkPacket(
                dimension_type=dimension, center_pos=b.SubChunkPos(x=x, y=6, z=0),
                sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                    sub_chunk_pos_offset=b.SubChunkPacket.SubChunkPosOffset(x=0, y=0, z=0),
                    result=result.SUCCESS_ALL_AIR if data is None else result.SUCCESS,
                    serialized_sub_chunk=data,
                )],
            )))

        for data in (None, solid_sub_chunk(HASHED_AIR_RUNTIME_ID)):
            observe(target)
            await blocks.start_mining(target, face=4)
            section(1, 0, None)
            section(0, 1, None)
            section(0, 0, bytes([9, 0, 6]))  # No main-layer storage.
            await asyncio.sleep(0)
            self.assertEqual(events, [])
            section(0, 0, data)
            await asyncio.sleep(0)
            self.assertEqual(events, [MiningEvent("completed", 0, MiningTarget(target, 4),
                                                 BlockObservation(target, HASHED_AIR_RUNTIME_ID, 0, True))])
            events.clear()

    async def test_mining_events_distinguish_continue_replacement_and_same_cell_restart(self):
        _, _, blocks, observe = self.mining_fixture()
        a, b = (1, 100, 0), (2, 100, 0)
        observe(a)
        observe(b)
        events = []
        self.addCleanup(blocks.subscribe_mining_events(events.append))
        await blocks.start_mining(a, face=4)
        await blocks.continue_mining(a, face=1)
        await asyncio.sleep(0)
        self.assertEqual(events, [])
        await blocks.continue_mining(b, face=4)
        await blocks.start_mining(b, face=1)
        observe(b, HASHED_AIR_RUNTIME_ID)
        await asyncio.sleep(0)
        self.assertEqual(events, [
            MiningEvent("aborted", 0, MiningTarget(a, 4), reason="replaced"),
            MiningEvent("aborted", 0, MiningTarget(b, 4), reason="replaced"),
            MiningEvent("completed", 0, MiningTarget(b, 1), BlockObservation(b, HASHED_AIR_RUNTIME_ID, 0, True)),
        ])
        self.assertEqual(await blocks.stop_mining(), 5)
        await asyncio.sleep(0)
        self.assertEqual(len(events), 3)

    async def test_creative_mining_event_observes_only_its_target_and_window(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        observe(target)
        stream.emit(protocol.encode(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE)))
        events = []
        self.addCleanup(blocks.subscribe_mining_events(events.append))

        with patch("endpilot.blocks._CREATIVE_MINING_OBSERVATION_TIMEOUT", 0):
            self.assertEqual(await blocks.finish_mining(target, face=4), 1)
        observe(target, HASHED_AIR_RUNTIME_ID)  # The deadline has already passed.
        await asyncio.sleep(0)
        self.assertEqual(events, [])

        observe(target)
        self.assertEqual(await blocks.finish_mining(target, face=4), 2)
        observe((2, 100, 0), HASHED_AIR_RUNTIME_ID)
        observe(target, HASHED_AIR_RUNTIME_ID, layer=1)
        await asyncio.sleep(0)
        self.assertEqual(events, [])  # Returning a tick is just submission.
        observe(target, HASHED_AIR_RUNTIME_ID)
        await asyncio.sleep(0)
        self.assertEqual(events, [MiningEvent("completed", 0, MiningTarget(target, 4),
                                             BlockObservation(target, HASHED_AIR_RUNTIME_ID, 0, True))])

    async def test_creative_finish_continues_an_existing_attempt_with_air_during_send(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        observe(target)
        stream.emit(protocol.encode(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE)))
        events = []
        self.addCleanup(blocks.subscribe_mining_events(events.append))
        await blocks.start_mining(target, face=4)
        send = stream.send

        async def finish_on_wire(frame):
            await send(frame)
            self.assertEqual([a.player_action_type.name for a in frame.player_block_actions],
                             ["CONTINUE_DESTROY_BLOCK", "PREDICT_DESTROY_BLOCK"])
            observe(target, HASHED_AIR_RUNTIME_ID)
            await asyncio.sleep(0)

        stream.send = finish_on_wire
        self.assertEqual(await blocks.finish_mining(target, face=4), 2)
        self.assertEqual(events, [MiningEvent("completed", 0, MiningTarget(target, 4),
                                             BlockObservation(target, HASHED_AIR_RUNTIME_ID, 0, True))])

    async def check_creative_finish_after_replacing_a_completed_cell(self, *, queued):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        observe(target)
        stream.emit(protocol.encode(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE)))
        events = []
        self.addCleanup(blocks.subscribe_mining_events(events.append))
        self.assertEqual(await blocks.start_mining(target, face=4), 1)

        if queued:
            async with blocks.movement._action_lock:
                finishing = asyncio.create_task(blocks.finish_mining(target, face=1))
                await asyncio.sleep(0)
                self.assertTrue(blocks._mining_lock.locked())
                self.assertFalse(finishing.done())
                observe(target, HASHED_AIR_RUNTIME_ID)
                observe(target)
            self.assertEqual(await finishing, 2)
        else:
            pos = b.BlockPos(x=1, y=100, z=0)
            stream.emit(protocol.encode(b.UpdateSubChunkBlocksPacket(
                sub_chunk_block_position=pos,
                blocks_changed=b.UpdateSubChunkBlocksChangedInfo(standards=[
                    b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=HASHED_AIR_RUNTIME_ID),
                    b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=0x80310E21),
                ]),
            )))
            self.assertEqual(await blocks.finish_mining(target, face=1), 2)

        observe(target, HASHED_AIR_RUNTIME_ID)
        await asyncio.sleep(0)
        air = BlockObservation(target, HASHED_AIR_RUNTIME_ID, 0, True)
        self.assertEqual(events, [MiningEvent("completed", 0, MiningTarget(target, 4), air),
                                  MiningEvent("completed", 0, MiningTarget(target, 1), air)])
        self.assertEqual(len(stream.sent), 2)
        actions = stream.sent[-1].player_block_actions
        self.assertEqual([action.player_action_type.name for action in actions], [
            "STOP_DESTROY_BLOCK", "START_DESTROY_BLOCK", "CONTINUE_DESTROY_BLOCK", "PREDICT_DESTROY_BLOCK",
        ])
        self.assertEqual([action.facing for action in actions[1:]], [1, 1, 1])
        self.assertEqual(stream.sent[-1].item_use_transaction.transaction.face, 1)

    async def test_creative_finish_starts_a_new_attempt_after_sparse_air_and_replacement(self):
        await self.check_creative_finish_after_replacing_a_completed_cell(queued=False)

    async def test_queued_creative_finish_starts_a_new_attempt_after_air_and_replacement(self):
        await self.check_creative_finish_after_replacing_a_completed_cell(queued=True)

    async def test_sparse_replacement_can_start_a_new_held_attempt_during_packet_dispatch(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        observe(target)
        events, restarted = [], []
        self.addCleanup(blocks.subscribe_mining_events(events.append))
        self.assertEqual(await blocks.start_mining(target, face=4), 1)

        def restart(change):
            if change.before is not None and change.before.is_air is True and change.after.is_air is False:
                restarted.append(asyncio.eager_task_factory(asyncio.get_running_loop(), blocks.start_mining(target, face=1)))

        self.addCleanup(blocks.subscribe_block_changes(restart, position=target, layer=0))
        pos = b.BlockPos(x=1, y=100, z=0)
        stream.emit(protocol.encode(b.UpdateSubChunkBlocksPacket(
            sub_chunk_block_position=pos,
            blocks_changed=b.UpdateSubChunkBlocksChangedInfo(standards=[
                b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=HASHED_AIR_RUNTIME_ID),
                b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=0x80310E21),
                b.UpdateSubChunkNetworkBlockInfo(pos=b.BlockPos(x=2, y=100, z=0), runtime_id=HASHED_AIR_RUNTIME_ID),
            ]),
        )))
        new_start, = restarted
        self.assertEqual(new_start.result(), 2)
        self.assertEqual(blocks.mining_target, MiningTarget(target, 1))
        self.assertEqual(await blocks.continue_mining(target, face=1), 3)
        continuation, = stream.sent[-1].player_block_actions
        self.assertEqual(continuation.player_action_type.name, "CONTINUE_DESTROY_BLOCK")
        observe(target, HASHED_AIR_RUNTIME_ID)
        self.assertEqual(await blocks.stop_mining(), 4)
        release, = stream.sent[-1].player_block_actions
        self.assertEqual(release.player_action_type.name, "STOP_DESTROY_BLOCK")
        await asyncio.sleep(0)
        air = BlockObservation(target, HASHED_AIR_RUNTIME_ID, 0, True)
        self.assertEqual(events, [MiningEvent("completed", 0, MiningTarget(target, 4), air),
                                  MiningEvent("completed", 0, MiningTarget(target, 1), air)])

    async def test_restarting_creative_finish_cancels_the_old_window(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        observe(target)
        stream.emit(protocol.encode(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE)))
        events = []
        self.addCleanup(blocks.subscribe_mining_events(events.append))
        await blocks.finish_mining(target, face=4)
        old_expiry = blocks._mining_attempt.expiry
        await blocks.finish_mining(target, face=1)
        self.assertTrue(old_expiry.cancelled())
        observe(target, HASHED_AIR_RUNTIME_ID)
        await asyncio.sleep(0)
        self.assertEqual(events, [
            MiningEvent("aborted", 0, MiningTarget(target, 4), reason="replaced"),
            MiningEvent("completed", 0, MiningTarget(target, 1), BlockObservation(target, HASHED_AIR_RUNTIME_ID, 0, True)),
        ])

    async def test_palette_non_air_can_settle_mine_block_without_a_completion_event(self):
        _, stream, blocks, observe = self.mining_fixture(hashed=False)
        target = (1, 100, 0)
        observe(target, 3317)
        events = []
        self.addCleanup(blocks.subscribe_mining_events(events.append))
        send = stream.send

        async def report_non_air(frame):
            await send(frame)
            if len(stream.sent) == 1:
                observe(target, 17896)

        stream.send = report_non_air
        self.assertEqual(await blocks.mine_block(target, face=4, timeout=1), BlockUpdate(target, 17896, 0))
        observe(target, 18894)
        await asyncio.sleep(0)
        self.assertEqual(events, [])

    async def test_mining_events_end_on_own_respawn_dimension_change_and_disconnect(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        changes = []

        def observe_event(event):
            changes.append((event, blocks.dimension_id, blocks.mining_target))

        self.addCleanup(blocks.subscribe_mining_events(observe_event))
        observe(target)
        await blocks.start_mining(target, face=4)
        stream.emit(protocol.encode(b.RespawnPacket(runtime_id=8, state=b.PlayerRespawnState.READY_TO_SPAWN)))
        await asyncio.sleep(0)
        self.assertEqual(changes, [])
        stream.emit(protocol.encode(b.RespawnPacket(runtime_id=7, state=b.PlayerRespawnState.READY_TO_SPAWN)))
        await asyncio.sleep(0)
        self.assertEqual(changes[-1], (MiningEvent("aborted", 0, MiningTarget(target, 4), reason="respawn"), 0, None))

        await blocks.start_mining(target, face=4)
        stream.emit(protocol.encode(b.ChangeDimensionPacket(dimension_id=1)))
        await asyncio.sleep(0)
        self.assertEqual(changes[-1], (MiningEvent("aborted", 0, MiningTarget(target, 4),
                                                 reason="dimension_changed"), 1, None))

        observe(target)
        stream.emit(protocol.encode(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE)))
        await blocks.finish_mining(target, face=4)
        timer = blocks._mining_attempt.expiry
        stream.emit(protocol.encode(b.DisconnectPacket()))
        await asyncio.sleep(0)
        self.assertEqual(changes[-1], (MiningEvent("aborted", 1, MiningTarget(target, 4),
                                                 reason="disconnected"), 1, None))
        self.assertEqual(len(changes), 3)
        self.assertTrue(timer.cancelled())
        with self.assertRaises(ConnectionError):
            blocks.subscribe_mining_events(observe_event)

    async def test_whole_mine_cancellation_cannot_stop_the_next_phased_target(self):
        _, stream, blocks, observe = self.mining_fixture()
        a, b = (1, 100, 0), (2, 100, 0)
        observe(a)
        observe(b)
        events = []
        self.addCleanup(blocks.subscribe_mining_events(events.append))
        started = asyncio.Event()
        original = stream.send

        async def send(packet):
            await original(packet)
            started.set()

        stream.send = send
        whole = asyncio.create_task(blocks.mine_block(a, face=4))
        await started.wait()
        later = asyncio.create_task(blocks.start_mining(b, face=4))
        whole.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await whole
        self.assertEqual(await later, 3)
        self.assertEqual([a.player_action_type.name for a in stream.sent[1].player_block_actions],
                         ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
        self.assertEqual([a.player_action_type.name for a in stream.sent[-1].player_block_actions],
                         ["START_DESTROY_BLOCK"])
        self.assertEqual(blocks.mining_target, MiningTarget(b, 4))
        self.assertEqual(events, [MiningEvent("aborted", 0, MiningTarget(a, 4), reason="cancelled")])

    async def test_stop_interrupts_blocked_start_and_never_stops_the_queued_break(self):
        async with asyncio.timeout(2):
            for next_position in ((2, 100, 0), (1, 100, 0)):
                _, stream, blocks, observe = self.mining_fixture()
                first = (1, 100, 0)
                observe(first)
                observe(next_position)
                on_wire = asyncio.Event()
                releasing = asyncio.Event()
                finish_release = asyncio.Event()
                original_send = stream.send

                async def send(packet):
                    await original_send(packet)
                    match len(stream.sent):
                        case 1:
                            on_wire.set()
                            await asyncio.Future()
                        case 2:
                            releasing.set()
                            await finish_release.wait()
                        case 3:
                            observe(next_position, HASHED_AIR_RUNTIME_ID)

                stream.send = send
                mining = asyncio.create_task(blocks.mine_block(first, face=4))
                await on_wire.wait()
                queued = asyncio.create_task(blocks.mine_block(next_position, face=4))
                action = asyncio.create_task(blocks.stop_mining())
                await releasing.wait()
                action.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await action
                second_stop = asyncio.create_task(blocks.stop_mining())
                await asyncio.sleep(0)
                self.assertFalse(second_stop.done())
                self.assertEqual(len(stream.sent), 2)
                finish_release.set()
                self.assertEqual(await second_stop, stream.sent[1].client_tick)
                with self.assertRaises(MiningAborted):
                    await mining
                self.assertEqual(await queued, BlockUpdate(next_position, HASHED_AIR_RUNTIME_ID, 0))
                self.assertEqual([[action.player_action_type.name for action in packet.player_block_actions]
                                  for packet in stream.sent], [
                    ["START_DESTROY_BLOCK"], ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"],
                    ["START_DESTROY_BLOCK"], ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"],
                ])

    async def test_update_and_stop_are_ordered_at_packet_match(self):
        async with asyncio.timeout(2):
            for update_first in (False, True):
                with self.subTest(update_first=update_first):
                    _, stream, blocks, observe = self.mining_fixture()
                    target = (1, 100, 0)
                    observe(target)
                    events = []
                    self.addCleanup(blocks.subscribe_mining_events(events.append))
                    on_wire = asyncio.Event()
                    finish_start = asyncio.Event()
                    original_send = stream.send

                    async def send(packet):
                        await original_send(packet)
                        if len(stream.sent) == 1:
                            on_wire.set()
                            await finish_start.wait()

                    stream.send = send
                    mining = asyncio.create_task(blocks.mine_block(target, face=4))
                    await on_wire.wait()
                    if update_first:
                        observe(target, HASHED_AIR_RUNTIME_ID)
                    stopped = asyncio.create_task(blocks.stop_mining())
                    await asyncio.sleep(0)
                    if not update_first:
                        observe(target, HASHED_AIR_RUNTIME_ID)
                    else:
                        finish_start.set()
                    self.assertEqual(await stopped, stream.sent[1].client_tick)
                    if update_first:
                        self.assertEqual(await mining, BlockUpdate(target, HASHED_AIR_RUNTIME_ID, 0))
                        self.assertEqual(events, [MiningEvent("completed", 0, MiningTarget(target, 4),
                                                             BlockObservation(target, HASHED_AIR_RUNTIME_ID, 0, True))])
                    else:
                        with self.assertRaises(MiningAborted):
                            await mining
                        self.assertEqual(events, [MiningEvent("aborted", 0, MiningTarget(target, 4), reason="stopped")])
                    self.assertEqual(len(stream.sent), 2)
                    self.assertEqual([action.player_action_type.name for action in stream.sent[-1].player_block_actions],
                                     ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])

    async def test_stop_before_start_sends_no_input_or_releases_the_previous_segment(self):
        async with asyncio.timeout(2):
            for hold in (False, True):
                with self.subTest(hold=hold):
                    _, stream, blocks, observe = self.mining_fixture()
                    previous, target = (1, 100, 0), (2, 100, 0)
                    observe(previous)
                    observe(target)
                    events = []
                    self.addCleanup(blocks.subscribe_mining_events(events.append))
                    if hold:
                        await blocks.start_mining(previous, face=4)
                    async with blocks.movement._action_lock:
                        mining = asyncio.create_task(blocks.mine_block(target, face=4))
                        await asyncio.sleep(0)
                        stopped = asyncio.create_task(blocks.stop_mining())
                        await asyncio.sleep(0)
                        self.assertEqual(len(stream.sent), int(hold))
                    tick = await stopped
                    with self.assertRaises(MiningAborted):
                        await mining
                    if hold:
                        release = stream.sent[1]
                        self.assertEqual(tick, release.client_tick)
                        self.assertEqual(len(stream.sent), 2)
                        abort, stop = release.player_block_actions
                        self.assertEqual(abort.player_action_type.name, "ABORT_DESTROY_BLOCK")
                        self.assertEqual((abort.pos.x, abort.pos.y, abort.pos.z), previous)
                        self.assertEqual(stop.player_action_type.name, "STOP_DESTROY_BLOCK")
                        self.assertEqual(events, [MiningEvent("aborted", 0, MiningTarget(previous, 4), reason="stopped")])
                    else:
                        self.assertIsNone(tick)
                        self.assertEqual(stream.sent, [])
                        self.assertEqual(events, [])

    async def test_session_invalidation_ends_blocked_start_without_releasing_into_the_next_world(self):
        async with asyncio.timeout(2):
            for event in ("dimension", "respawn", "disconnect", "close"):
                with self.subTest(event=event):
                    protocol, stream, blocks, observe = self.mining_fixture()
                    b = protocol.binding
                    target = (1, 100, 0)
                    observe(target)
                    on_wire = asyncio.Event()
                    original_send = stream.send

                    async def send(packet):
                        await original_send(packet)
                        if len(stream.sent) == 1:
                            on_wire.set()
                            await asyncio.Future()

                    stream.send = send
                    mining = asyncio.create_task(blocks.mine_block(target, face=4))
                    await on_wire.wait()
                    observe(target, HASHED_AIR_RUNTIME_ID)
                    if event == "close":
                        blocks.close()
                    else:
                        packet = {
                            "dimension": b.ChangeDimensionPacket(dimension_id=1),
                            "respawn": b.RespawnPacket(runtime_id=7, state=b.PlayerRespawnState.READY_TO_SPAWN),
                            "disconnect": b.DisconnectPacket(),
                        }[event]
                        stream.emit(protocol.encode(packet))
                    expected = ConnectionError if event in ("disconnect", "close") else RuntimeError
                    with self.assertRaises(expected) as caught:
                        await mining
                    self.assertIs(type(caught.exception), expected)
                    self.assertEqual(len(stream.sent), 1)
                    self.assertIsNone(blocks.mining_target)

    async def test_stop_reports_release_send_failure_without_repeating_release(self):
        async with asyncio.timeout(2):
            _, stream, blocks, observe = self.mining_fixture()
            target = (1, 100, 0)
            observe(target)
            on_wire = asyncio.Event()
            original_send = stream.send

            async def send(packet):
                await original_send(packet)
                if len(stream.sent) == 1:
                    on_wire.set()
                else:
                    raise OSError("release send failed")

            stream.send = send
            mining = asyncio.create_task(blocks.mine_block(target, face=4))
            await on_wire.wait()
            with self.assertRaisesRegex(OSError, "release send failed"):
                await blocks.stop_mining()
            with self.assertRaises(MiningAborted) as caught:
                await mining
            self.assertIn("release send failed", caught.exception.__notes__[0])
            self.assertIsNone(await blocks.stop_mining())
            self.assertEqual(len(stream.sent), 2)

    async def test_replacement_waits_for_one_release_and_rejects_unusable_successors(self):
        for outcome in ("invalid", "superseded", "stopped", "failed", "cancelled", "timeout", "stale",
                        "out_of_reach", "dimension", "respawn", "disconnect"):
            with self.subTest(outcome=outcome):
                async with asyncio.timeout(3):
                    protocol, stream, blocks, observe = self.mining_fixture()
                    a, b, c = (1, 100, 0), (2, 100, 0), (1, 100, 1)
                    for position in (a, b, c):
                        observe(position)
                    events = []
                    self.addCleanup(blocks.subscribe_mining_events(events.append))
                    started, releasing, allow_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
                    successor_started = asyncio.Event()
                    original_send = stream.send
                    write_error = OSError("old release write failed")
                    tasks = []

                    async def send(packet):
                        await original_send(packet)
                        if len(stream.sent) == 1:
                            started.set()
                        elif len(stream.sent) == 2:
                            releasing.set()
                            await allow_release.wait()
                            if outcome == "failed":
                                raise write_error
                        elif len(stream.sent) == 3:
                            successor_started.set()

                    stream.send = send
                    first = asyncio.create_task(blocks.mine_block(a, face=4))
                    tasks.append(first)
                    try:
                        await started.wait()
                        original = asyncio.create_task(blocks.mine_block(
                            b, face=4, replace_current=True, timeout=0.05 if outcome == "timeout" else 15,
                        ))
                        tasks.append(original)
                        await releasing.wait()
                        chosen = stop = None
                        if outcome == "invalid":
                            with self.assertRaisesRegex(ValueError, "mining face"):
                                await blocks.mine_block(c, face=8, replace_current=True)
                            with self.assertRaisesRegex(ValueError, "observed non-air"):
                                await blocks.mine_block((2, 100, 1), face=4, replace_current=True)
                            distant = (20, 100, 0)
                            observe(distant)
                            with self.assertRaisesRegex(ValueError, "reach"):
                                await blocks.mine_block(distant, face=4, replace_current=True)
                            with patch.object(blocks.movement, "_input_position", None):
                                with self.assertRaisesRegex(RuntimeError, "input is unavailable"):
                                    await blocks.mine_block(c, face=4, replace_current=True)
                            with self.assertRaises(TimeoutError):
                                await blocks.mine_block(c, face=4, timeout=0, replace_current=True)
                            chosen, position = original, b
                            self.assertFalse(original.done())
                        elif outcome == "superseded":
                            chosen = asyncio.create_task(blocks.mine_block(c, face=4, replace_current=True))
                            tasks.append(chosen)
                            position = c
                            with self.assertRaises(MiningAborted):
                                await original
                        elif outcome == "stopped":
                            stop = asyncio.create_task(blocks.stop_mining())
                            tasks.append(stop)
                            with self.assertRaises(MiningAborted):
                                await original
                            self.assertFalse(stop.done())
                        elif outcome == "cancelled":
                            original.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await original
                        elif outcome == "timeout":
                            with self.assertRaises(TimeoutError):
                                await original
                        elif outcome == "stale":
                            observe(b, 0x54A1D0FD)
                        elif outcome == "out_of_reach":
                            blocks.movement._input_position = (10.5, 101.62, 0.5)
                        elif outcome in ("dimension", "respawn", "disconnect"):
                            binding = protocol.binding
                            packet = {
                                "dimension": binding.ChangeDimensionPacket(dimension_id=1),
                                "respawn": binding.RespawnPacket(
                                    runtime_id=7, state=binding.PlayerRespawnState.READY_TO_SPAWN,
                                ),
                                "disconnect": binding.DisconnectPacket(),
                            }[outcome]
                            stream.emit(protocol.encode(packet))
                            expected = ConnectionError if outcome == "disconnect" else RuntimeError
                            with self.assertRaises(expected) as changed:
                                await original
                            self.assertIs(type(changed.exception), expected)

                        self.assertEqual(len(stream.sent), 2)
                        allow_release.set()
                        with self.assertRaises(MiningAborted):
                            await first
                        if stop is not None:
                            self.assertEqual(await stop, stream.sent[1].client_tick)
                        if outcome == "failed":
                            with self.assertRaises(OSError) as failure:
                                await original
                            self.assertIs(failure.exception, write_error)
                        elif outcome == "stale":
                            with self.assertRaisesRegex(ValueError, "target changed"):
                                await original
                        elif outcome == "out_of_reach":
                            with self.assertRaisesRegex(ValueError, "reach"):
                                await original
                        if chosen is not None:
                            await successor_started.wait()
                            action, = stream.sent[2].player_block_actions
                            self.assertEqual(action.player_action_type.name, "START_DESTROY_BLOCK")
                            self.assertEqual((action.pos.x, action.pos.y, action.pos.z), position)
                            observe(position, HASHED_AIR_RUNTIME_ID)
                            self.assertEqual((await chosen).position, position)
                        self.assertEqual(len(stream.sent), 4 if chosen is not None else 2)
                        release = stream.sent[1].player_block_actions
                        self.assertEqual([action.player_action_type.name for action in release],
                                         ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
                        self.assertEqual((release[0].pos.x, release[0].pos.y, release[0].pos.z), a)
                        self.assertEqual([(event.target.position, event.reason) for event in events],
                                         [(a, "replaced")] + ([(position, None)] if chosen is not None else []))
                        self.assertFalse(blocks._mining_lock.locked())
                    finally:
                        allow_release.set()
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_replacement_at_same_position_keeps_an_already_matched_result(self):
        async with asyncio.timeout(3):
            _, stream, blocks, observe = self.mining_fixture()
            target = (1, 100, 0)
            observe(target)
            events = []
            self.addCleanup(blocks.subscribe_mining_events(events.append))
            started, finish_start, successor_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_send = stream.send

            async def send(packet):
                await original_send(packet)
                if len(stream.sent) == 1:
                    started.set()
                    await finish_start.wait()
                if len(stream.sent) == 3:
                    successor_started.set()

            stream.send = send
            first = asyncio.create_task(blocks.mine_block(target, face=4))
            replacing = None
            try:
                await started.wait()
                observe(target, HASHED_AIR_RUNTIME_ID)
                observe(target)
                replacing = asyncio.create_task(blocks.mine_block(target, face=4, replace_current=True))
                await asyncio.sleep(0)
                finish_start.set()
                self.assertEqual(await first, BlockUpdate(target, HASHED_AIR_RUNTIME_ID, 0))
                await successor_started.wait()
                self.assertFalse(replacing.done())
                observe(target, HASHED_AIR_RUNTIME_ID, layer=1)
                await asyncio.sleep(0)
                self.assertFalse(replacing.done())
                observe(target, HASHED_AIR_RUNTIME_ID)
                self.assertEqual(await replacing, BlockUpdate(target, HASHED_AIR_RUNTIME_ID, 0))
                self.assertEqual([event.kind for event in events], ["completed", "completed"])
                self.assertEqual([[action.player_action_type.name for action in packet.player_block_actions]
                                  for packet in stream.sent], [
                    ["START_DESTROY_BLOCK"], ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"],
                    ["START_DESTROY_BLOCK"], ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"],
                ])
            finally:
                finish_start.set()
                tasks = (first,) if replacing is None else (first, replacing)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_new_request_supersedes_a_granted_replacement_or_its_started_break(self):
        for before_start in (True, False):
            with self.subTest(before_start=before_start):
                async with asyncio.timeout(3):
                    _, stream, blocks, observe = self.mining_fixture()
                    a, b, c = (1, 100, 0), (2, 100, 0), (1, 100, 1)
                    for target in (a, b, c):
                        observe(target)
                    events = []
                    self.addCleanup(blocks.subscribe_mining_events(events.append))
                    sent = asyncio.Queue()
                    original_send = stream.send

                    async def send(packet):
                        await original_send(packet)
                        sent.put_nowait(packet)

                    async def next_input(*expected):
                        frame = await sent.get()
                        self.assertEqual([action.player_action_type.name for action in frame.player_block_actions],
                                         list(expected))
                        position = frame.player_block_actions[0].pos
                        return position.x, position.y, position.z

                    third = None
                    finish_turn = blocks._finish_mining_turn

                    def finish_and_supersede():
                        nonlocal third
                        finish_turn()
                        if third is None and len(stream.sent) == 2:
                            third = asyncio.eager_task_factory(
                                asyncio.get_running_loop(), blocks.mine_block(c, face=4, replace_current=True),
                            )

                    stream.send = send
                    tasks = []
                    first = asyncio.create_task(blocks.mine_block(a, face=4))
                    tasks.append(first)
                    start, abort, stop = "START_DESTROY_BLOCK", "ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"
                    try:
                        self.assertEqual(await next_input(start), a)
                        with patch.object(blocks, "_finish_mining_turn",
                                          side_effect=finish_and_supersede if before_start else finish_turn):
                            second = asyncio.create_task(blocks.mine_block(b, face=4, replace_current=True))
                            tasks.append(second)
                            self.assertEqual(await next_input(abort, stop), a)
                            with self.assertRaises(MiningAborted):
                                await first
                            if not before_start:
                                self.assertEqual(await next_input(start), b)
                                third = asyncio.create_task(blocks.mine_block(c, face=4, replace_current=True))
                                self.assertEqual(await next_input(abort, stop), b)
                            with self.assertRaises(MiningAborted):
                                await second
                        self.assertEqual(await next_input(start), c)
                        observe(c, HASHED_AIR_RUNTIME_ID)
                        self.assertEqual((await third).position, c)
                        self.assertEqual(await next_input(abort, stop), c)
                        expected = [(a, "replaced")] + ([] if before_start else [(b, "replaced")]) + [(c, None)]
                        self.assertEqual([(event.target.position, event.reason) for event in events], expected)
                        self.assertTrue(sent.empty())
                    finally:
                        if third is not None:
                            tasks.append(third)
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_caller_can_cancel_while_successful_break_is_releasing(self):
        _, stream, blocks, observe = self.mining_fixture()
        target = (1, 100, 0)
        observe(target)
        releasing = asyncio.Event()
        finish_release = asyncio.Event()
        original_send = stream.send

        async def send(packet):
            await original_send(packet)
            if len(stream.sent) == 1:
                observe(target, HASHED_AIR_RUNTIME_ID)
            else:
                releasing.set()
                await finish_release.wait()

        stream.send = send
        mining = asyncio.create_task(blocks.mine_block(target, face=4))
        try:
            await asyncio.wait_for(releasing.wait(), 1)
            mining.cancel()
            done, _ = await asyncio.wait((mining,), timeout=1)
            self.assertIn(mining, done)
            with self.assertRaises(asyncio.CancelledError):
                await mining
        finally:
            finish_release.set()
            await asyncio.gather(mining, return_exceptions=True)
        self.assertIsNone(await blocks.stop_mining())
        self.assertEqual(len(stream.sent), 2)

    async def test_dimension_change_rejects_a_start_waiting_for_the_movement_lock(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        target = (1, 100, 0)
        observe(target)
        async with blocks.movement._action_lock:
            pending = asyncio.create_task(blocks.start_mining(target))
            await asyncio.sleep(0)
            stream.emit(protocol.encode(protocol.binding.ChangeDimensionPacket(dimension_id=1)))
        with self.assertRaisesRegex(RuntimeError, "world changed"):
            await pending
        self.assertIsNone(blocks.mining_target)
        self.assertEqual(stream.sent, [])

    async def test_dimension_change_rejects_a_start_queued_behind_a_mining_action(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        target = (1, 100, 0)
        observe(target)
        async with blocks._mining_lock:
            pending = asyncio.create_task(blocks.start_mining(target))
            await asyncio.sleep(0)
            stream.emit(protocol.encode(protocol.binding.ChangeDimensionPacket(dimension_id=1)))
            observe(target)
        with self.assertRaisesRegex(RuntimeError, "world changed"):
            await pending
        self.assertIsNone(blocks.mining_target)
        self.assertEqual(stream.sent, [])

    async def test_creative_finish_requires_the_observed_mode_and_preserves_block_observation(self):
        protocol, stream, blocks, observe = self.mining_fixture()
        b = protocol.binding
        target = (1, 100, 0)
        observe(target)
        with self.assertRaisesRegex(RuntimeError, "creative"):
            await blocks.finish_mining(target, face=4)
        self.assertEqual(stream.sent, [])
        stream.emit(protocol.encode(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE)))
        self.assertEqual(await blocks.finish_mining(target, face=4), 1)
        frame = protocol.decode(protocol.encode(stream.sent[-1]))
        self.assertEqual(frame.item_use_transaction.transaction.action_type,
                         b.ItemUseInventoryTransaction.ActionType.DESTROY)
        self.assertIsNone(blocks.mining_target)
        self.assertFalse(blocks.observed_block(target).is_air)

    async def test_ignores_other_positions_and_layers_then_returns_server_update(self):
        protocol = Protocol(2193)
        binding = protocol.binding
        stream = PacketStream(protocol)
        blocks = Blocks(stream, None, None)

        def emit(x, layer, runtime):
            packet = binding.UpdateBlockPacket()
            packet.pos = binding.BlockPos(x=x, y=100, z=0)
            packet.runtime_id = runtime
            packet.update_flags = 0
            packet.layer = layer
            stream.emit(protocol.encode(packet))

        pending = asyncio.create_task(blocks.wait_for_update((2, 100, 0), timeout=1))
        await asyncio.sleep(0)
        emit(3, 0, 17)
        emit(2, 1, 18)
        self.assertFalse(pending.done())
        emit(2, 0, 19)
        self.assertEqual(await pending, BlockUpdate((2, 100, 0), 19, 0))
        self.assertEqual(stream.observers, [])

    async def test_timeout_unsubscribes(self):
        stream = PacketStream(Protocol(2193))
        with self.assertRaises(TimeoutError):
            await Blocks(stream, None, None).wait_for_update((2, 100, 0), timeout=0.01)
        self.assertEqual(stream.observers, [])

    async def test_placement_submits_server_item_and_block_runtime_with_shared_tick(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        inventory = Inventory(stream, 7)
        blocks = Blocks(stream, movement, inventory)
        movement._input_position = (0.5, 101.62, 0.5)
        movement.position = movement._input_position
        movement._updated.set()
        item = b.SerializedNetworkItemStackDescriptor(
            id=1, stack_size=4, aux_value=0, net_id_variant=9,
            block_runtime_id=123, user_data_buffer=b"\x00" * 10,
        )
        packet = b.InventorySlotPacket(
            inventory_id=b.ContainerID.INVENTORY,
            slot=0,
            full_container_name=None,
            storage_item=None,
            item=item,
        )
        stream.emit(protocol.encode(packet))
        self.assertEqual(await blocks.place_against(BlockUpdate((2, 99, 0), 77, 0)), 1)
        self.assertEqual(len(stream.sent), 2)
        equipment, input_frame = stream.sent
        self.assertIsInstance(equipment, b.MobEquipmentPacket)
        self.assertEqual(int(equipment.runtime_id), 7)
        self.assertEqual(int(equipment.item.id), 1)
        self.assertEqual(int(equipment.selected_slot), 0)
        self.assertIn(b.PlayerAuthInputPacket.InputData.PERFORM_ITEM_INTERACTION, input_frame.input_data)
        self.assertEqual(int(input_frame.item_use_transaction.transaction.target_block_id), 77)
        self.assertEqual(int(input_frame.item_use_transaction.transaction.item.net_id_variant), 9)
        self.assertEqual(int(input_frame.item_use_transaction.transaction.pos.y), 99)
        decoded = protocol.decode(protocol.encode(input_frame))
        self.assertEqual(int(decoded.item_use_transaction.transaction.target_block_id), 77)
        self.assertEqual(decoded.client_tick, 1)
        self.assertEqual(int(decoded.item_use_transaction.transaction.face), 1)
        self.assertEqual(float(decoded.item_use_transaction.transaction.click_pos.y), 1.0)

        # A placement without an explicit hit point uses the clicked face center.
        face_centers = (
            (0.5, 0.0, 0.5), (0.5, 1.0, 0.5),
            (0.5, 0.5, 0.0), (0.5, 0.5, 1.0),
            (0.0, 0.5, 0.5), (1.0, 0.5, 0.5),
        )
        observed = BlockObservation((2, 99, 0), 77, 0, False)
        for face, expected in enumerate(face_centers):
            tick = await blocks.place_against(observed, face=face)
            frame = protocol.decode(protocol.encode(stream.sent[-1]))
            transaction = frame.item_use_transaction.transaction
            self.assertEqual(frame.client_tick, tick)
            self.assertEqual(int(transaction.face), face)
            self.assertEqual(int(transaction.target_block_id), 77)
            self.assertEqual(
                (float(transaction.click_pos.x), float(transaction.click_pos.y),
                 float(transaction.click_pos.z)), expected,
            )

        # An explicit hit point must survive the public transaction encoding.
        # An observed palette-air support without a runtime ID is sent as ID 0.
        tick = await blocks.place_against(
            BlockObservation((2, 99, 0), None, 0, True),
            face=4, click_pos=(0.25, 0.75, 0.125),
        )
        frame = protocol.decode(protocol.encode(stream.sent[-1]))
        transaction = frame.item_use_transaction.transaction
        self.assertEqual(frame.client_tick, tick)
        self.assertEqual(int(transaction.target_block_id), 0)
        self.assertEqual(
            (transaction.click_pos.x, transaction.click_pos.y, transaction.click_pos.z),
            (0.25, 0.75, 0.125),
        )
        inventory.close()

    async def test_mine_block_uses_auth_input_actions_and_waits_for_server_air(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = Blocks(stream, movement, None, hashed_runtime_ids=True)
        original_send = stream.send

        async def send(packet):
            await original_send(packet)
            if not isinstance(packet, b.PlayerAuthInputPacket) or not packet.player_block_actions:
                return
            if packet.player_block_actions[0].player_action_type != b.PlayerActionType.START_DESTROY_BLOCK:
                return
            # An unrelated change is not completion; only server-reported air is.
            for x, runtime_id in ((2, 123), (1, 0xDBF44120)):
                update = b.UpdateBlockPacket()
                update.pos = b.BlockPos(x=x, y=100, z=0)
                update.runtime_id = runtime_id
                update.update_flags = 0
                update.layer = 0
                stream.emit(protocol.encode(update))

        stream.send = send
        observed = await blocks.mine_block((1, 100, 0), face=4, timeout=1)
        self.assertEqual(observed, BlockUpdate((1, 100, 0), 0xDBF44120, 0))
        self.assertEqual([packet.client_tick for packet in stream.sent], [1, 2])
        start, stop = [protocol.decode(protocol.encode(p)) for p in stream.sent]
        self.assertIn(b.PlayerAuthInputPacket.InputData.PERFORM_BLOCK_ACTIONS, start.input_data)
        self.assertEqual(start.player_block_actions[0].player_action_type, b.PlayerActionType.START_DESTROY_BLOCK)
        self.assertEqual(tuple((start.player_block_actions[0].pos.x,
                                start.player_block_actions[0].pos.y,
                                start.player_block_actions[0].pos.z)), (1, 100, 0))
        self.assertEqual(start.player_block_actions[0].facing, 4)
        abort, release = stop.player_block_actions
        self.assertEqual(abort.player_action_type, b.PlayerActionType.ABORT_DESTROY_BLOCK)
        self.assertEqual((abort.pos.x, abort.pos.y, abort.pos.z), (1, 100, 0))
        self.assertEqual(release.player_action_type, b.PlayerActionType.STOP_DESTROY_BLOCK)
        self.assertEqual(stream.observers, [])

    async def test_mining_timeout_aborts_the_target(self):
        protocol = Protocol(2193)
        b = protocol.binding
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = Blocks(stream, movement, None, hashed_runtime_ids=True)
        target = (1, 100, 0)
        send = stream.send
        timeout_at = asyncio.timeout_at
        observation_timeout = None

        def capture_timeout(deadline):
            nonlocal observation_timeout
            observation_timeout = timeout_at(deadline)
            return observation_timeout

        async def send_on_wire(packet):
            frame = protocol.decode(protocol.encode(packet))
            await send(frame)
            if len(stream.sent) == 1:
                self.assertIsInstance(frame, b.PlayerAuthInputPacket)
                self.assertIn(b.PlayerAuthInputPacket.InputData.PERFORM_BLOCK_ACTIONS, frame.input_data)
                start, = frame.player_block_actions
                self.assertEqual(start.player_action_type, b.PlayerActionType.START_DESTROY_BLOCK)
                self.assertEqual((start.pos.x, start.pos.y, start.pos.z, start.facing), (*target, 4))
                self.assertIsNotNone(observation_timeout)
                # START is on the wire. Expire the wait on the next event loop turn.
                observation_timeout.reschedule(asyncio.get_running_loop().time())

        stream.send = send_on_wire
        with patch.object(asyncio, "timeout_at", capture_timeout), self.assertRaises(TimeoutError):
            await blocks.mine_block(target, face=4)

        self.assertEqual([frame.client_tick for frame in stream.sent], [1, 2])
        self.assertTrue(observation_timeout.expired())
        abort, stop = stream.sent[1].player_block_actions
        self.assertEqual(abort.player_action_type, b.PlayerActionType.ABORT_DESTROY_BLOCK)
        self.assertEqual((abort.pos.x, abort.pos.y, abort.pos.z), target)
        self.assertEqual(stop.player_action_type, b.PlayerActionType.STOP_DESTROY_BLOCK)
        self.assertIsNone(blocks.mining_target)
        self.assertEqual(stream.observers, [])

    async def test_mining_cancel_before_start_unsubscribes_waiter(self):
        protocol = Protocol(2193)
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = Blocks(stream, movement, None, hashed_runtime_ids=True)
        subscribe = stream.subscribe_raw
        task = None

        def cancel_on_subscribe(observer):
            unsubscribe = subscribe(observer)
            task.cancel()  # Cancel while the newly registered waiter is pending.
            return unsubscribe

        stream.subscribe_raw = cancel_on_subscribe
        task = asyncio.create_task(blocks.mine_block((1, 100, 0), timeout=1))
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertEqual(stream.observers, [])
        self.assertEqual(stream.sent, [])

    async def test_mining_disconnect_preserves_original_error_when_abort_fails(self):
        protocol = Protocol(2193)
        stream = PacketStream(protocol)
        movement = Movement(stream, 7)
        movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        blocks = Blocks(stream, movement, None, hashed_runtime_ids=True)
        original_send = stream.send

        async def send(packet):
            if stream.sent:
                raise OSError("connection already closed")
            await original_send(packet)

        async def disconnected(*args, **kwargs):
            raise ConnectionError("packet stream lost")

        stream.send = send
        stream.wait_for_packet = disconnected
        with self.assertRaisesRegex(ConnectionError, "packet stream lost") as caught:
            await blocks.mine_block((1, 100, 0), timeout=1)
        self.assertIn("mining abort failed", " ".join(caught.exception.__notes__))
        self.assertEqual(len(stream.sent), 1)
