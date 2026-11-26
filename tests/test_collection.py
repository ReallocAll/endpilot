import asyncio
from functools import partial
import math
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import BlockCollectionBatch, Bot, CountedBlockCollection, DropCollection, MixedCollectionBatch, NavigationRules
from endpilot.block_data import _catalog
from endpilot.block_raycast import trace_blocks
from endpilot.blocks import HASHED_AIR_RUNTIME_ID, Blocks
from endpilot.collection import _observe_mined_block
from endpilot.compression import PacketConnection
from endpilot.entities import Entities
from endpilot.harvest import HarvestToolChest
from endpilot.inventory import Inventory
from endpilot.item_registry import ItemRegistrySnapshot
from endpilot.login import JoinSession
from endpilot.movement import Movement
from endpilot.navigation import _StandingGraph
from endpilot.protocol import Protocol, read_batch, write_batch

from test_container_take import item as stack_descriptor
from test_scheduled_input import ManualClock


TARGET = (-2, 100, 3)
SECOND_TARGET = (-3, 100, 4)
THIRD_TARGET = (-3, 100, 2)
RAISED_TARGET = (0, 101, 3)
PLAYER = 7
DIRT = 13
ORE = 0x54A1D0FD
OAK_LOG_BLOCK = 4065577688
OAK_LOG_ITEM = 17
IRON_AXE = 446
REFILL_CHEST = (-3, 100, 2)


class Wire:
    def __init__(self, protocol):
        self.protocol = protocol
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.failure = None
        self.pause_start = False

    async def send(self, data):
        packet = self.protocol.decode(next(read_batch(data)))
        self.outgoing.put_nowait(packet)
        if self.pause_start and any(
            action.player_action_type.name == "START_DESTROY_BLOCK"
            for action in getattr(packet, "player_block_actions", None) or ()
        ):
            await asyncio.Future()

    async def recv(self):
        data = await self.incoming.get()
        if isinstance(data, Exception):
            self.failure = data
            raise data
        return data

    async def close(self):
        pass

    def emit(self, *packets):
        self.incoming.put_nowait(write_batch(self.protocol.encode(packet) for packet in packets))


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        protocol = Protocol()
        b = self.b = protocol.binding
        self.wire = Wire(protocol)
        self.packets = PacketConnection(self.wire, protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.NONE,
        ))
        self.inventory = Inventory(self.packets, PLAYER)
        movement = Movement(self.packets, PLAYER)
        movement._input_position = (-2.5, 101.62, 3.5)
        self.blocks = Blocks(self.packets, movement, self.inventory, hashed_runtime_ids=True)
        self.blocks.start()
        self.entities = Entities(self.packets, item_names={DIRT: "minecraft:dirt"})
        self.entities.start()
        self.packets.start_receiving()
        self.addAsyncCleanup(self.close_packets)
        await self.emit(self.block(900))

    async def close_packets(self):
        self.entities.close()
        self.blocks.close()
        self.inventory.close()
        try:
            await self.packets.close()
        except OSError as error:
            self.assertIs(error, self.wire.failure)

    async def emit(self, *packets):
        self.wire.emit(*packets)
        await asyncio.sleep(0)

    def block(self, runtime_id, *, position=TARGET, layer=0):
        x, y, z = position
        return self.b.UpdateBlockPacket(
            pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=layer,
        )

    def pose(self, eye=(-2.5, 101.62, 3.5)):
        b = self.b
        return b.MovePlayerPacket(
            player_id=PLAYER, pos=b.Vec3(x=eye[0], y=eye[1], z=eye[2]),
            rot=b.Vec2(x=0, y=0), on_ground=True, reset_position=0,
        )

    def item(self, item_id=0, count=0):
        return self.b.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=count)

    def slot(self, slot, item_id=0, count=0, *, container=None, net_id=None):
        b = self.b
        return b.InventorySlotPacket(
            inventory_id=b.ContainerID.INVENTORY if container is None else container,
            slot=slot, item=stack_descriptor(b, item_id, count, net_id),
        )

    def content(self, *slots):
        b = self.b
        return b.InventoryContentPacket(
            inventory_id=b.ContainerID.INVENTORY, slots=slots,
            full_container_name=b.FullContainerName(
                name=b.ContainerEnumName.COMBINED_HOTBAR_AND_INVENTORY_CONTAINER,
            ),
            storage_item=self.item(),
        )

    def inventory_transaction(self, *actions):
        b = self.b
        source = b.InventorySource(
            type=b.InventorySourceType.CONTAINER_INVENTORY, container_id=b.ContainerID.INVENTORY,
        )
        return b.InventoryTransactionPacket(
            legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
            variant_transaction=b.NormalTransactionData(transaction=b.InventoryTransaction(actions=[
                b.InventoryAction(
                    source=source, slot=slot,
                    from_item_descriptor=self.item(DIRT if old else 0, old),
                    to_item_descriptor=self.item(DIRT if new else 0, new),
                )
                for slot, old, new in actions
            ])),
        )

    def drop(self, runtime_id=101, *, entity_id=None, position=(-1.5, 100.5, 3.5),
             count=1, item_id=DIRT):
        x, y, z = position
        return self.b.AddItemActorPacket(
            id=runtime_id + 1000 if entity_id is None else entity_id,
            runtime_id=runtime_id, item=self.item(item_id, count),
            pos=self.b.Vec3(x=x, y=y, z=z),
        )

    def take(self, runtime_id=101, *, player=PLAYER):
        return self.b.TakeItemActorPacket(item_id=runtime_id, actor_id=player)

    async def next_actions(self):
        packet = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        return [action.player_action_type.name for action in packet.player_block_actions]

    def collect(self, *, face=4, timeout=0.1, follow_ticks=0):
        return asyncio.create_task(_observe_mined_block(
            self.blocks, self.entities, TARGET, face=face, timeout=timeout, follow_ticks=follow_ticks,
        ))

    async def start(self, *, timeout=0.1):
        task = self.collect(timeout=timeout)
        self.assertEqual(await self.next_actions(), ["START_DESTROY_BLOCK"])
        return task

    async def wait_for_collection_subscribers(self):
        async with asyncio.timeout(0.5):
            while len(self.packets._subscribers) < 5:
                await asyncio.sleep(0)

    def assert_released(self):
        self.assertEqual(len(self.packets._subscribers), 3)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_same_batch_spawn_inventory_and_pickup_survive_entity_removal(self):
        await self.emit(self.slot(0, DIRT, 4))
        task = await self.start()
        self.wire.emit(
            self.drop(count=2),
            self.block(HASHED_AIR_RUNTIME_ID),
            self.inventory_transaction((0, 4, 6)),
            self.take(),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(result.status, "collected")
        self.assertEqual(result.block_before.runtime_id, 900)
        self.assertTrue(result.block_after.is_air)
        self.assertEqual(result.drops[0].item_id, "minecraft:dirt")
        self.assertEqual(result.drops[0].position, (-1.5, 100.5, 3.5))
        self.assertEqual((result.collector_id, result.inventory_delta), (PLAYER, 2))
        self.assertIsNone(self.entities.get(101))
        self.assert_released()

    async def test_delayed_slot_update_can_confirm_known_empty(self):
        await self.emit(self.slot(0))
        task = await self.start(timeout=3)
        self.wire.emit(self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take())
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertFalse(task.done())
        self.wire.emit(self.slot(0, DIRT, 1))
        result = await task
        self.assertEqual((result.status, result.inventory_delta), ("collected", 1))
        self.assertFalse(result.timed_out)
        self.assert_released()

    async def test_late_candidate_in_same_batch_makes_pickup_ambiguous(self):
        await self.emit(self.slot(0))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(),
            self.slot(0, DIRT, 1), self.drop(102, item_id=DIRT + 1),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(result.status, "ambiguous_drop")
        self.assertEqual([drop.runtime_id for drop in result.drops], [101, 102])
        self.assert_released()

    async def test_excludes_previous_drops_and_birth_outside_the_block(self):
        await self.emit(self.slot(0), self.drop())
        task = await self.start()
        self.wire.emit(
            self.drop(), self.drop(102, position=(-2.1, 100.5, 3.5)),
            self.take(), self.take(102), self.slot(0, DIRT, 2),
            self.block(HASHED_AIR_RUNTIME_ID),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(result.status, "drop_unobserved")
        self.assertTrue(result.timed_out)
        self.assertEqual(result.drops, ())
        self.assert_released()

    async def test_existing_unique_actor_is_not_new_when_runtime_id_changes(self):
        await self.emit(self.slot(0), self.drop(entity_id=2001))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(102, entity_id=2001),
            self.take(102), self.slot(0, DIRT, 1),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(result.status, "drop_unobserved")
        self.assertEqual(result.drops, ())
        self.assert_released()

    async def test_new_unique_actor_keeps_its_birth_evidence_when_runtime_id_changes(self):
        await self.emit(self.slot(0))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(entity_id=2001),
            self.drop(102, entity_id=2001),
            self.take(102), self.slot(0, DIRT, 1),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(result.status, "collected")
        self.assertEqual([(drop.entity_id, drop.runtime_id) for drop in result.drops], [(2001, 101)])
        self.assert_released()

    async def test_follow_uses_current_runtime_for_the_original_unique_actor(self):
        await self.emit(self.slot(0))
        task = self.collect(timeout=1, follow_ticks=12)
        self.assertEqual(await self.next_actions(), ["START_DESTROY_BLOCK"])
        self.wire.emit(self.block(HASHED_AIR_RUNTIME_ID), self.drop(entity_id=2001))
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        first_frame = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        forward = self.b.PlayerAuthInputPacket.InputData.UP
        self.assertIn(forward, first_frame.input_data)
        await self.emit(self.drop(102, entity_id=2001))
        next_frame = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        self.assertIn(forward, next_frame.input_data)
        self.assertGreater(next_frame.pos.x, first_frame.pos.x)

        self.wire.emit(self.take(102), self.slot(0, DIRT, 1))
        result = await task
        self.assertEqual((result.status, result.collector_id, result.inventory_delta),
                         ("collected", PLAYER, 1))
        self.assertEqual(result.drops[0].runtime_id, 101)
        release_frames = []
        while not self.wire.outgoing.empty():
            release_frames.append(self.wire.outgoing.get_nowait())
        self.assertEqual(len(release_frames), 2)
        self.assertTrue(all(forward not in frame.input_data for frame in release_frames))
        self.assert_released()

    async def public_bot(self, *, tool_slot=1, clock=None):
        self.enterContext((clock or ManualClock()).patch_scheduler())
        bot = Bot("CollectionTest")
        session = JoinSession(self.packets, bot.identity, "127.0.0.1")
        session.player_id = session.player_unique_id = PLAYER
        session.block_network_ids_are_hashes = True
        session.item_names = {DIRT: "minecraft:dirt", 444: "minecraft:golden_pickaxe",
                              445: "minecraft:iron_pickaxe", IRON_AXE: "minecraft:iron_axe",
                              OAK_LOG_ITEM: "minecraft:oak_log"}
        bot._start_session(session)
        self.addAsyncCleanup(bot.close)
        await self.emit(
            self.pose(),
            *(self.block(HASHED_AIR_RUNTIME_ID, position=(x, y, z))
              for x in range(-5, 2) for y in range(99, 104) for z in range(1, 6)),
            self.block(0x54A1D0FD), self.block(0x80310E21, position=(-3, 99, 3)),
            self.block(HASHED_AIR_RUNTIME_ID, position=(-3, 100, 3), layer=1),
            self.block(HASHED_AIR_RUNTIME_ID, position=(-3, 101, 3), layer=1),
            self.slot(0, 444, 1), self.slot(tool_slot, 445, 1), self.slot(2),
        )
        return bot

    async def test_queued_continue_cannot_hold_a_replacement_block_at_the_same_position(self):
        bot = await self.public_bot()
        await bot.start_mining(TARGET, face=4)
        self.wire.outgoing.get_nowait()

        for name, lock in (("movement", bot._movement._action_lock), ("mining", bot._blocks._mining_lock)):
            with self.subTest(lock=name):
                async with lock:
                    pending = asyncio.create_task(bot.continue_mining(TARGET, face=4))
                    await asyncio.sleep(0)
                    await self.emit(self.block(HASHED_AIR_RUNTIME_ID), self.block(0x54A1D0FD))
                    self.assertIsNone(bot.mining_target)

                with self.assertRaisesRegex(RuntimeError, "mining target changed"):
                    await pending
                self.assertTrue(self.wire.outgoing.empty())
                self.assertIsNone(bot.mining_target)
                await bot.start_mining(TARGET, face=4)
                frame = self.wire.outgoing.get_nowait()
                self.assertEqual(frame.player_block_actions[-1].player_action_type.name, "START_DESTROY_BLOCK")
                self.assertEqual(bot.mining_target.position, TARGET)

    async def test_mine_block_timeout_while_waiting_for_mining_preserves_the_holder(self):
        bot = await self.public_bot()
        other = (-1, 100, 3)
        await self.emit(self.block(0x54A1D0FD, position=other))
        holder = asyncio.create_task(bot.mine_block(TARGET, face=4, timeout=1))
        await asyncio.wait_for(self.wire.outgoing.get(), 1)
        target = bot.mining_target

        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.5) as limit:
                await bot.mine_block(other, face=4, timeout=0.02)
        held_after_queue = bot.mining_target
        no_queued_frame = self.wire.outgoing.empty()

        await self.emit(self.block(HASHED_AIR_RUNTIME_ID))
        self.assertEqual((await holder).runtime_id, HASHED_AIR_RUNTIME_ID)
        release = self.wire.outgoing.get_nowait()
        self.assertEqual([action.player_action_type.name for action in release.player_block_actions],
                         ["STOP_DESTROY_BLOCK"])
        self.assertFalse(limit.expired())
        self.assertIsNotNone(target)
        self.assertIs(held_after_queue, target)
        self.assertTrue(no_queued_frame)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_mine_block_does_not_renew_its_deadline_between_locks(self):
        bot = await self.public_bot()
        async with bot._movement._action_lock:
            async with bot._blocks._mining_lock:
                pending = asyncio.create_task(bot.mine_block(TARGET, face=4, timeout=0.25))
                await asyncio.sleep(0.15)
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(0.18) as limit:
                    await pending
            self.assertFalse(limit.expired())

        self.assertTrue(self.wire.outgoing.empty())
        self.assertIsNone(bot.mining_target)

    async def test_mine_block_timeout_while_waiting_for_movement_sends_no_input(self):
        bot = await self.public_bot()
        async with bot._movement._action_lock:
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(0.5) as limit:
                    await bot.mine_block(TARGET, face=4, timeout=0.02)
            self.assertFalse(limit.expired())

        self.assertTrue(self.wire.outgoing.empty())
        self.assertIsNone(bot.mining_target)

    async def test_mine_block_timeout_during_submitted_start_releases_input(self):
        bot = await self.public_bot()
        self.wire.pause_start = True
        timeout_at = asyncio.timeout_at
        deadlines = []

        def capture_timeout(when):
            deadline = timeout_at(when)
            deadlines.append(deadline)
            return deadline

        with patch("endpilot.blocks.asyncio.timeout_at", new=capture_timeout):
            pending = asyncio.create_task(bot.mine_block(TARGET, face=4))
            try:
                self.assertEqual(await self.next_actions(), ["START_DESTROY_BLOCK"])
                self.assertEqual(bot.mining_target.position, TARGET)
                deadline, = deadlines
                deadline.reschedule(asyncio.get_running_loop().time())
                with self.assertRaises(TimeoutError):
                    async with asyncio.timeout(1) as watchdog:
                        await pending
                self.assertFalse(watchdog.expired())
                self.assertTrue(deadline.expired())
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

        self.assertEqual(await self.next_actions(), ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
        self.assertTrue(self.wire.outgoing.empty())
        self.assertIsNone(bot.mining_target)

    async def test_other_players_respawn_preserves_phased_and_whole_mining(self):
        clock = ManualClock()
        bot = await self.public_bot(clock=clock)
        b = self.b
        flags = b.PlayerAuthInputPacket.InputData
        start = (b.PlayerActionType.START_DESTROY_BLOCK, TARGET, 4)
        abort = (b.PlayerActionType.ABORT_DESTROY_BLOCK, TARGET, 0)
        stop = (b.PlayerActionType.STOP_DESTROY_BLOCK, (0, 0, 0), 0)

        def ready(runtime_id):
            return b.RespawnPacket(pos=b.Vec3(x=0, y=0, z=0), runtime_id=runtime_id,
                                   state=b.PlayerRespawnState.READY_TO_SPAWN)

        def assert_input(frame, actions):
            self.assertIsInstance(frame, b.PlayerAuthInputPacket)
            self.assertIsNone(frame.item_use_transaction)
            self.assertIsNone(frame.item_stack_request)
            self.assertNotIn(flags.PERFORM_ITEM_INTERACTION, frame.input_data)
            self.assertNotIn(flags.PERFORM_ITEM_STACK_REQUEST, frame.input_data)
            self.assertEqual(flags.PERFORM_BLOCK_ACTIONS in frame.input_data, bool(actions))
            self.assertEqual(tuple(
                (action.player_action_type, (action.pos.x, action.pos.y, action.pos.z), action.facing)
                for action in frame.player_block_actions or ()
            ), actions)

        async def expect_input(*actions):
            outgoing = self.wire.outgoing
            assert_input(await asyncio.wait_for(outgoing.get(), 1), actions)
            while not outgoing.empty():
                assert_input(outgoing.get_nowait(), ())

        await bot.start_mining(TARGET, face=4)
        await expect_input(start)
        await self.emit(ready(PLAYER + 1))
        self.assertEqual(bot.mining_target.position, TARGET)

        whole = asyncio.create_task(bot.mine_block(TARGET, face=4, timeout=1))
        await expect_input(abort, stop, start)
        await self.emit(ready(PLAYER + 1), self.block(HASHED_AIR_RUNTIME_ID))
        self.assertEqual((await whole).runtime_id, HASHED_AIR_RUNTIME_ID)
        await expect_input(stop)

        for runtime_id, tick_ms in ((0, 50), (PLAYER, 100)):
            with self.subTest(runtime_id=runtime_id):
                await self.emit(self.block(0x54A1D0FD))
                whole = asyncio.create_task(bot.mine_block(TARGET, face=4, timeout=1))
                await expect_input(start)
                await self.emit(ready(runtime_id))
                with self.assertRaisesRegex(RuntimeError, "world changed"):
                    await whole
                self.assertIsNone(bot.mining_target)
                await clock.wake_at(tick_ms)
                await expect_input()

    async def test_public_collection_serializes_calls_through_selection_and_break(self):
        bot = await self.public_bot()
        first = asyncio.create_task(bot.collect_block(TARGET, timeout=1))
        selected = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        self.assertIsInstance(selected, self.b.MobEquipmentPacket)
        self.assertEqual(selected.selected_slot, 1)
        self.assertEqual(await self.next_actions(), ["START_DESTROY_BLOCK"])
        second = asyncio.create_task(bot.collect_block(TARGET, timeout=1))
        await asyncio.sleep(0)
        self.assertTrue(self.wire.outgoing.empty())
        self.wire.emit(self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(),
                       self.slot(2, DIRT, 1))
        first_result, second_result = await asyncio.gather(first, second)
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(first_result.status, "collected")
        self.assertEqual(first_result.tool.item_name, "minecraft:iron_pickaxe")
        self.assertEqual(first_result.approach.face, 4)
        self.assertEqual(second_result.status, "target_air")
        self.assertTrue(self.wire.outgoing.empty())

    async def test_collect_uses_latest_pose_to_aim_at_the_raised_ore_when_starting(self):
        bot = await self.public_bot()
        await self.emit(self.block(ORE, position=RAISED_TARGET))
        eye = (-2.6, 101.62, 3.75)
        async with bot._blocks._mining_lock:
            task = asyncio.create_task(bot.collect_block(RAISED_TARGET, ticks=0, search_radius=0, timeout=1))
            equipped = await asyncio.wait_for(self.wire.outgoing.get(), 1)
            self.assertIsInstance(equipped, self.b.MobEquipmentPacket)
            await self.emit(self.pose(eye))
            bot._movement.set_rotation(0, 0)
        frame = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        action, = frame.player_block_actions
        self.assertEqual((action.player_action_type.name, action.facing), ("START_DESTROY_BLOCK", 4))
        self.assertEqual((action.pos.x, action.pos.y, action.pos.z), RAISED_TARGET)
        origin = (frame.pos.x, frame.pos.y, frame.pos.z)
        self.assertLess(math.dist(origin, eye), 1e-5)
        hit = trace_blocks(origin, (frame.rot.x, frame.rot.y), dimension_id=0, max_distance=4.5,
                           outline_at=bot._blocks._outline_at)
        self.assertEqual((hit.status, hit.position, hit.face), ("hit", RAISED_TARGET, 4))
        self.assertLess(math.dist(hit.click_pos, (0, 0.5, 0.5)), 1e-5)
        await self.finish_batch_break(RAISED_TARGET, runtime_id=101, total=1)
        result = await task
        self.assertEqual((result.status, result.approach.frames, result.approach.face, result.inventory_delta),
                         ("collected", 0, 4, 1))
        self.assertTrue(self.wire.outgoing.empty())

    async def test_collect_prefers_an_adjacent_stance_over_an_already_visible_farther_ray(self):
        bot = await self.public_bot()
        target = (0, 100, 3)
        await self.emit(
            self.block(ORE, position=target), self.block(HASHED_AIR_RUNTIME_ID),
            *(self.block(0x80310E21, position=(x, 99, 3)) for x in (-2, -1)),
            *(self.block(HASHED_AIR_RUNTIME_ID, position=(x, y, 3), layer=1)
              for x in (-2, -1) for y in (100, 101)),
        )
        self.assertEqual(bot.raycast_block_towards((0, 100.5, 3.5), max_distance=4.5).position, target)
        task = asyncio.create_task(bot.collect_block(target, search_radius=3, timeout=4))
        await self.expect_batch_start(target, timeout=4)
        await self.finish_batch_break(target, runtime_id=101, total=1)
        result = await task
        self.assertEqual((result.status, result.approach.stand_position), ("collected", (-1, 100, 3)))
        self.assertGreater(result.approach.frames, 0)

    async def queue_collection(self, bot, target, *updates):
        lock = bot._movement._action_lock
        async with lock:
            task = asyncio.create_task(bot.collect_block(target, search_radius=2, timeout=5))
            async with asyncio.timeout(1):
                while not lock._waiters:
                    await asyncio.sleep(0)
            self.assertTrue(self.wire.outgoing.empty())
            await self.emit(*updates)
        return task

    async def test_queued_collection_can_use_a_visible_ray_after_adjacent_support_disappears(self):
        bot = await self.public_bot()
        target = (0, 100, 3)
        adjacent_floor = (-1, 99, 3)
        await self.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.block(ORE, position=target),
            *(self.block(0x80310E21, position=(x, 99, 3)) for x in (-2, -1)),
            *(self.block(HASHED_AIR_RUNTIME_ID, position=(x, y, 3), layer=1)
              for x in (-2, -1) for y in (100, 101)),
        )
        task = await self.queue_collection(bot, target, self.block(HASHED_AIR_RUNTIME_ID, position=adjacent_floor))
        waiting_for_start = asyncio.create_task(self.expect_batch_start(target, timeout=4))
        try:
            done, _ = await asyncio.wait((task, waiting_for_start), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                self.fail(f"collection ended before START: {(await task).status}")
            frame = await waiting_for_start
            self.assertEqual(math.floor(frame.pos.x), -3)
            await self.finish_batch_break(target, runtime_id=101, total=1)
            result = await task
            self.assertEqual((result.status, result.approach.stand_position, result.approach.frames),
                             ("collected", (-3, 100, 3), 0))
        finally:
            for pending in (task, waiting_for_start):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, waiting_for_start, return_exceptions=True)

    async def test_queued_collection_prefers_adjacency_from_the_corrected_pose(self):
        bot = await self.public_bot()
        b = self.b
        target = (0, 100, 3)
        supported = ((-2, 4), (-1, 4), (-1, 3))
        await self.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.block(ORE, position=target),
            *(self.block(0x80310E21, position=(x, 99, z)) for x, z in supported),
            *(self.block(HASHED_AIR_RUNTIME_ID, position=(x, y, z), layer=1)
              for x, z in supported for y in (100, 101)),
        )
        tick = await bot.set_sneaking(False)
        self.assertEqual(self.wire.outgoing.get_nowait().client_tick, tick)
        correction = b.CorrectPlayerMovePredictionPacket(
            pos=b.Vec3(x=-1.5, y=101.62, z=4.5), pos_delta=b.Vec3(x=0, y=0, z=0),
            on_ground=True, tick=tick,
        )
        task = await self.queue_collection(bot, target, correction)
        frame = await self.expect_batch_start(target, timeout=4)
        await self.finish_batch_break(target, runtime_id=101, total=1)
        result = await task
        self.assertEqual((math.floor(frame.pos.x), math.floor(frame.pos.z)), (-1, 3))
        self.assertEqual((result.status, result.approach.stand_position), ("collected", (-1, 100, 3)))
        self.assertGreater(result.approach.frames, 0)

    async def test_collect_rejects_a_changed_ray_between_equipment_and_start(self):
        bot = await self.public_bot()
        for update, status in (
            (self.block(0x80310E21, position=(-1, 101, 3)), "unreachable"),
            (self.block(0xFFFFFFFE, position=(-1, 101, 3)), "unobserved"),
            (self.pose((-4.7, 101.62, 3.5)), "unreachable"),
            (self.block(0x80310E21, position=RAISED_TARGET), "target_changed"),
        ):
            with self.subTest(status=status, update=type(update).__name__):
                await self.emit(self.pose(), self.block(ORE, position=RAISED_TARGET),
                                self.block(HASHED_AIR_RUNTIME_ID, position=(-1, 101, 3)))
                async with bot._blocks._mining_lock:
                    task = asyncio.create_task(bot.collect_targets(
                        (RAISED_TARGET, TARGET), ticks=0, search_radius=0, timeout=1,
                    ))
                    equipped = await asyncio.wait_for(self.wire.outgoing.get(), 1)
                    self.assertIsInstance(equipped, self.b.MobEquipmentPacket)
                    await self.emit(update)
                batch = await task
                result, = batch.results
                self.assertEqual((result.status, result.position), (status, RAISED_TARGET))
                self.assertEqual((result.approach.status, result.approach.face, result.tool.item_name),
                                 ("reached", 4, "minecraft:iron_pickaxe"))
                self.assertIsNone(result.block_after)
                self.assertIsNone(bot.mining_target)
                self.assertTrue(self.wire.outgoing.empty())

    async def test_collect_preference_consumes_the_route_expansion_budget(self):
        bot = await self.public_bot()
        await self.emit(self.block(ORE, position=RAISED_TARGET))
        with patch("endpilot.collection.NavigationRules", partial(NavigationRules, max_expansions=1)):
            result = await bot.collect_block(RAISED_TARGET, ticks=0, search_radius=0, timeout=1)
        self.assertEqual((result.status, result.approach.frames), ("budget_exhausted", 0))
        self.assertIsNone(result.tool)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_collect_does_not_use_a_stationary_ray_for_a_moving_start_frame(self):
        bot = await self.public_bot()
        await self.emit(self.block(ORE, position=RAISED_TARGET))
        away = (-4.48, 101.62, 3.5)
        await self.emit(*(self.block(HASHED_AIR_RUNTIME_ID, position=(-6, y, z))
                          for y in range(100, 103) for z in range(2, 5)))
        async with bot._blocks._mining_lock:
            task = asyncio.create_task(bot.collect_block(RAISED_TARGET, ticks=0, search_radius=0, timeout=0.5))
            equipped = await asyncio.wait_for(self.wire.outgoing.get(), 1)
            self.assertIsInstance(equipped, self.b.MobEquipmentPacket)
            await self.emit(self.pose(away))
            self.assertEqual(bot.raycast_block_towards((0, 101.5, 3.5), max_distance=4.5).status, "hit")
            await bot.set_move_vector(0, -1)
        result = await task
        self.assertEqual((result.status, result.approach.status), ("unobserved", "reached"))
        self.assertIsNone(bot.mining_target)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_collect_preference_and_route_share_the_approach_deadline(self):
        bot = await self.public_bot()
        loop = asyncio.get_running_loop()
        now = loop.time()
        plan = _StandingGraph.plan

        def inspect(*args, **kwargs):
            nonlocal now
            result = plan(*args, **kwargs)
            now += 1
            return result

        with patch.object(loop, "time", lambda: now), patch.object(_StandingGraph, "plan", inspect):
            result = await bot.collect_block(TARGET, ticks=0, search_radius=0, timeout=1)
        self.assertEqual((result.status, result.approach.frames), ("budget_exhausted", 0))
        self.assertIsNone(result.tool)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_cancel_before_aimed_start_preserves_equipment_without_mining_frames(self):
        bot = await self.public_bot()
        await self.emit(self.block(ORE, position=RAISED_TARGET))
        async with bot._blocks._mining_lock:
            task = asyncio.create_task(bot.collect_targets(
                (RAISED_TARGET, TARGET), ticks=0, search_radius=0, timeout=1,
            ))
            equipped = await asyncio.wait_for(self.wire.outgoing.get(), 1)
            self.assertIsInstance(equipped, self.b.MobEquipmentPacket)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as interrupted:
                await task
        progress = interrupted.exception.collection_progress
        self.assertEqual(progress.results, ())
        self.assertEqual((progress.active.position, progress.active.approach.face, progress.active.tool.item_name),
                         (RAISED_TARGET, 4, "minecraft:iron_pickaxe"))
        self.assertIsNone(progress.active.block_after)
        self.assertIsNone(bot.mining_target)
        self.assertTrue(self.wire.outgoing.empty())

    async def batch_bot(self):
        bot = await self.public_bot()
        await self.emit(self.block(ORE, position=SECOND_TARGET), self.block(ORE, position=THIRD_TARGET))
        return bot

    async def expect_batch_start(self, position, *, timeout=1):
        async with asyncio.timeout(timeout):
            while True:
                packet = await self.wire.outgoing.get()
                if isinstance(packet, self.b.PlayerAuthInputPacket) and packet.player_block_actions:
                    action, = packet.player_block_actions
                    self.assertEqual(action.player_action_type.name, "START_DESTROY_BLOCK")
                    self.assertEqual((action.pos.x, action.pos.y, action.pos.z), position)
                    return packet

    async def finish_batch_break(
        self, position, *, runtime_id, total=None, item_id=DIRT, count=1, player=PLAYER, net_id=None,
    ):
        packets = [self.block(HASHED_AIR_RUNTIME_ID, position=position),
                   self.drop(runtime_id, position=tuple(value + 0.5 for value in position),
                             count=count, item_id=item_id)]
        if total is not None:
            packets.extend((self.take(runtime_id, player=player), self.slot(2, item_id, total, net_id=net_id)))
        self.wire.emit(*packets)
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])

    async def test_batch_counts_distinct_positions_in_input_order(self):
        bot = await self.batch_bot()
        batch = asyncio.create_task(bot.collect_targets(
            [TARGET, list(TARGET), SECOND_TARGET, THIRD_TARGET], count=2, ticks=0, search_radius=0, timeout=1,
        ))
        for index, position in enumerate((TARGET, SECOND_TARGET), 1):
            await self.expect_batch_start(position)
            await self.finish_batch_break(position, runtime_id=100 + index, total=index)

        result = await asyncio.wait_for(batch, 1)
        self.assertIsInstance(result, BlockCollectionBatch)
        self.assertEqual((result.dimension_id, result.positions, result.active), (0, (TARGET, SECOND_TARGET), None))
        self.assertEqual(tuple(item.position for item in result.results), result.positions)
        self.assertEqual([(item.status, item.collector_id, item.inventory_delta) for item in result.results],
                         [("collected", PLAYER, 1), ("collected", PLAYER, 1)])
        self.assertEqual([item.drops[0].runtime_id for item in result.results], [101, 102])
        self.assertEqual(bot.observed_block(THIRD_TARGET).runtime_id, ORE)
        self.assertEqual(bot.inventory_count(DIRT), 2)
        self.assertTrue(self.wire.outgoing.empty())

    async def refill_bot(self, *, chest=REFILL_CHEST):
        bot = await self.public_bot()
        bot._session.item_registry = ItemRegistrySnapshot(bot._session.item_names)
        palette, hashes = _catalog()
        chest_id = next(runtime for runtime, index in hashes.items()
                        if palette[index] is not None and palette[index].name == "minecraft:chest")
        await self.emit(
            self.block(OAK_LOG_BLOCK), self.block(ORE, position=SECOND_TARGET),
            self.block(chest_id, position=chest),
            self.content(stack_descriptor(self.b, 444, 1, 10), *(self.item() for _ in range(35))),
        )
        return bot

    async def refill_batch(self, bot, *, tool_chest):
        batch = asyncio.create_task(bot.collect_targets(
            [TARGET, SECOND_TARGET], ticks=0, search_radius=0, timeout=1, tool_chest=tool_chest,
        ))

        async def settle_batch():
            batch.cancel()
            await asyncio.gather(batch, return_exceptions=True)

        self.addAsyncCleanup(settle_batch)
        await self.expect_batch_start(TARGET)
        await self.finish_batch_break(TARGET, runtime_id=101, total=1, item_id=OAK_LOG_ITEM, net_id=15)
        return batch

    def refill_window(self, *, position=REFILL_CHEST, iron_slot=4):
        b = self.b
        x, y, z = position
        open_packet = b.ContainerOpenPacket(
            container_id=2, type=b.ContainerType.CONTAINER,
            pos=b.BlockPos(x=x, y=y, z=z), entity_unique_id=-1,
        )
        stacks = {iron_slot: stack_descriptor(b, 445, 1, 14)}
        contents = b.InventoryContentPacket(
            inventory_id=2, slots=[stacks.get(index, self.item()) for index in range(27)],
            full_container_name=b.FullContainerName(name=b.ContainerEnumName.LEVEL_ENTITY_CONTAINER),
            storage_item=self.item(),
        )
        return open_packet, contents

    async def refill_click(self, *, position=REFILL_CHEST):
        b = self.b
        async with asyncio.timeout(1):
            click = await self.wire.outgoing.get()
            while isinstance(click, b.PlayerAuthInputPacket):
                self.assertFalse(click.player_block_actions)
                click = await self.wire.outgoing.get()
        self.assertIsInstance(click, b.InventoryTransactionPacket)
        interaction = click.variant_transaction
        self.assertEqual((interaction.pos.x, interaction.pos.y, interaction.pos.z), position)
        return interaction

    async def refill_request(self, *, position=REFILL_CHEST, expected_hit=None):
        b = self.b
        interaction = await self.refill_click(position=position)
        if expected_hit is not None:
            face, point = expected_hit
            hit = interaction.click_pos
            self.assertEqual(interaction.face, face)
            self.assertLess(math.dist((hit.x, hit.y, hit.z), point), 1e-6)
        await self.emit(*self.refill_window(position=position))
        take = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        self.assertIsInstance(take, b.ItemStackRequestPacket)
        request, = take.requests
        action, = request.actions
        self.assertIsInstance(action, b.ItemStackRequestCereal.TakeActionData)
        self.assertEqual((action.source.slot, action.destination.slot, action.amount), (4, 3, 1))
        return b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[group]),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=slot, slot=slot, amount=count,
                    item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                    custom_name=b.RedactableString(unredacted_string="", redacted_string=None),
                    durability_correction=0,
                )],
            ) for group, slot, count, net_id in (
                ("LEVEL_ENTITY_CONTAINER", 4, 0, None), ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 3, 1, 20),
            )],
        )])

    async def refill_close(self, *, confirm=True):
        packet = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        self.assertIsInstance(packet, self.b.ContainerClosePacket)
        if confirm:
            await self.emit(packet)
        return packet

    async def test_batch_without_refill_stops_after_collecting_the_prefix(self):
        bot = await self.refill_bot()
        result = await (await self.refill_batch(bot, tool_chest=None))
        self.assertEqual([(row.status, row.tool_acquisition) for row in result.results],
                         [("collected", None), ("tool_unavailable", None)])
        self.assertTrue(self.wire.outgoing.empty())

    async def test_batch_refills_between_confirmed_pickups_and_waits_for_owned_close(self):
        await self.check_batch_refill(await self.refill_bot(), REFILL_CHEST)

    async def test_batch_refills_from_the_top_of_a_chest_below_the_stance(self):
        chest = (-3, 99, 2)
        bot = await self.refill_bot(chest=chest)
        await self.emit(*(self.block(0x80310E21, position=(x, 98, z))
                          for x in range(-4, -1) for z in range(1, 4)))
        await self.check_batch_refill(bot, chest, expected_hit=(1, (0.5, 0.95, 0.5)))

    async def check_batch_refill(self, bot, position, *, expected_hit=None):
        config = HarvestToolChest(position, range(4, 5), range(3, 4))
        batch = await self.refill_batch(bot, tool_chest=config)
        await self.emit(await self.refill_request(position=position, expected_hit=expected_hit))
        equipped = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        self.assertIsInstance(equipped, self.b.MobEquipmentPacket)
        self.assertEqual((equipped.item.id, equipped.item.net_id_variant), (445, 20))
        close = await self.refill_close(confirm=False)
        self.assertTrue(self.wire.outgoing.empty())
        await self.emit(close)
        await self.expect_batch_start(SECOND_TARGET)
        await self.finish_batch_break(SECOND_TARGET, runtime_id=102, total=1)
        result = await batch
        first, second = result.results
        self.assertEqual([(row.position, row.status) for row in result.results],
                         [(TARGET, "collected"), (SECOND_TARGET, "collected")])
        self.assertIsNone(first.tool_acquisition)
        self.assertEqual(second.tool, second.tool_acquisition.tool)
        self.assertEqual(second.tool_acquisition.transfer.confirmed_count, 1)
        self.assertIsNone(bot.open_window)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_batch_refill_shortage_keeps_prefix_and_ignores_unauthorized_slot(self):
        bot = await self.refill_bot()
        batch = await self.refill_batch(bot, tool_chest=HarvestToolChest(REFILL_CHEST, range(5, 6)))
        await self.refill_click()
        await self.emit(*self.refill_window(iron_slot=4))
        await self.refill_close()
        first, second = (await batch).results
        self.assertEqual((first.status, second.status, second.tool_acquisition.status),
                         ("collected", "tool_unavailable", "tool_unavailable"))
        self.assertIsNone(second.tool_acquisition.transfer)
        self.assertIsNone(bot.open_window)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_target_change_during_refill_keeps_confirmed_transfer_without_start(self):
        bot = await self.refill_bot()
        batch = await self.refill_batch(bot, tool_chest=HarvestToolChest(REFILL_CHEST, range(4, 5), range(3, 4)))
        await self.emit(await self.refill_request(), self.block(OAK_LOG_BLOCK, position=SECOND_TARGET))
        await self.refill_close()
        first, second = (await batch).results
        self.assertEqual((first.status, second.status), ("collected", "target_changed"))
        self.assertEqual(second.tool_acquisition.transfer.confirmed_count, 1)
        self.assertIsNone(bot.open_window)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_cancelled_batch_refill_keeps_confirmed_transfer_in_active_progress(self):
        bot = await self.refill_bot()
        batch = await self.refill_batch(bot, tool_chest=HarvestToolChest(REFILL_CHEST, range(4, 5), range(3, 4)))
        await self.emit(await self.refill_request())
        equipped = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        self.assertIsInstance(equipped, self.b.MobEquipmentPacket)
        await self.refill_close(confirm=False)
        batch.cancel("stop before chest close confirmation")
        with self.assertRaisesRegex(asyncio.CancelledError, "stop before chest close confirmation") as caught:
            await batch
        progress = caught.exception.collection_progress
        first, = progress.results
        self.assertEqual(first.status, "collected")
        self.assertEqual(progress.active.position, SECOND_TARGET)
        self.assertEqual(progress.active.tool_acquisition.transfer.confirmed_count, 1)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_batch_refill_leaves_caller_window_open_and_skips_the_detour(self):
        bot = await self.refill_bot()
        await self.emit(*self.refill_window())
        caller_window = bot.open_window
        with patch.object(bot._movement, "_navigate_goal", wraps=bot._movement._navigate_goal) as navigate:
            batch = await self.refill_batch(bot, tool_chest=HarvestToolChest(REFILL_CHEST, range(4, 5)))
            first, second = (await batch).results
        self.assertEqual([call.args[1].position for call in navigate.await_args_list], [TARGET, SECOND_TARGET])
        self.assertEqual((first.status, second.status, second.tool_acquisition.status),
                         ("collected", "tool_refill_failed", "busy"))
        self.assertIs(bot.open_window, caller_window)
        self.assertTrue(self.wire.outgoing.empty())

    async def log_bot(self, *positions):
        bot = await self.public_bot()
        await self.emit(self.slot(3, IRON_AXE, 1),
                        *(self.block(OAK_LOG_BLOCK, position=position) for position in positions))
        return bot

    def collect_logs(self, bot, *, count, radius=2, ticks=0, search_radius=0, timeout=1):
        return asyncio.create_task(bot.collect_by_count(
            "oak_log", item_id=OAK_LOG_ITEM, count=count, radius=radius, ticks=ticks,
            search_radius=search_radius, timeout=timeout,
        ))

    async def test_counted_collection_reselects_log_removed_during_approach_and_counts_the_actual_gain(self):
        first, second, third = (-1, 100, 3), (0, 100, 3), (1, 100, 3)
        outside = (2, 100, 6)
        late = (-4, 100, 3)
        bot = await self.log_bot(third, second, outside, first)
        await self.emit(
            self.block(HASHED_AIR_RUNTIME_ID),
            *(self.block(0x80310E21, position=(x, 99, 3)) for x in (-2, -1)),
            *(self.block(HASHED_AIR_RUNTIME_ID, position=(x, y, 3), layer=1)
              for x in (-2, -1) for y in (100, 101)),
        )
        task = self.collect_logs(bot, count=1, radius=5, ticks=160, search_radius=3, timeout=4)
        frame = await asyncio.wait_for(self.wire.outgoing.get(), 1)
        self.assertIsInstance(frame, self.b.PlayerAuthInputPacket)
        self.assertIn(self.b.PlayerAuthInputPacket.InputData.UP, frame.input_data)
        self.assertFalse(frame.player_block_actions)
        await self.emit(
            self.block(HASHED_AIR_RUNTIME_ID, position=first), self.block(OAK_LOG_BLOCK, position=late),
            self.drop(101, position=(-0.5, 100.5, 3.5), item_id=OAK_LOG_ITEM),
            self.take(), self.slot(2, OAK_LOG_ITEM, 1),
        )

        await self.expect_batch_start(second, timeout=4)
        await self.finish_batch_break(second, runtime_id=102, total=3, item_id=OAK_LOG_ITEM, count=2)
        result = await asyncio.wait_for(task, 1)
        self.assertIsInstance(result, CountedBlockCollection)
        self.assertEqual(result.positions, (first, second, third))
        skipped, collected = result.results
        self.assertEqual((skipped.position, skipped.status, skipped.drops), (first, "target_changed", ()))
        self.assertGreater(skipped.approach.frames, 0)
        self.assertEqual((collected.position, collected.status, collected.collector_id, collected.inventory_delta),
                         (second, "collected", PLAYER, 2))
        self.assertEqual((result.item_id, result.requested_count, result.collected_count, result.active),
                         (OAK_LOG_ITEM, 1, 2, None))
        self.assertEqual(bot.inventory_count(OAK_LOG_ITEM), 3)
        self.assertEqual(bot.observed_block(late).runtime_id, OAK_LOG_BLOCK)
        self.assertEqual(bot.observed_block(third).runtime_id, OAK_LOG_BLOCK)
        self.assert_no_more_collection_inputs()

    async def test_counted_collection_has_a_finite_snapshot_when_start_rejects_the_last_candidate(self):
        bot = await self.log_bot(TARGET)
        async with bot._blocks._mining_lock:
            task = self.collect_logs(bot, count=2)
            equipped = await asyncio.wait_for(self.wire.outgoing.get(), 1)
            self.assertIsInstance(equipped, self.b.MobEquipmentPacket)
            await self.emit(self.block(HASHED_AIR_RUNTIME_ID), self.block(OAK_LOG_BLOCK, position=SECOND_TARGET))
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.positions, (TARGET,))
        attempt, = result.results
        self.assertEqual((attempt.position, attempt.status, attempt.approach.status),
                         (TARGET, "target_changed", "reached"))
        self.assertEqual(result.collected_count, 0)
        self.assert_no_more_collection_inputs()

    async def test_counted_collection_keeps_partial_gain_and_stops_on_another_players_pickup(self):
        first, second, third = THIRD_TARGET, SECOND_TARGET, TARGET
        bot = await self.log_bot(first, second, third)
        task = self.collect_logs(bot, count=3)
        await self.expect_batch_start(first)
        await self.finish_batch_break(first, runtime_id=101, total=1, item_id=OAK_LOG_ITEM)
        await self.expect_batch_start(second)
        await self.finish_batch_break(second, runtime_id=102, total=2, item_id=OAK_LOG_ITEM, player=12)

        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.positions, (first, second, third))
        self.assertEqual([(attempt.position, attempt.status, attempt.collector_id) for attempt in result.results],
                         [(first, "collected", PLAYER), (second, "taken_by_other", 12)])
        self.assertEqual((result.requested_count, result.collected_count), (3, 1))
        self.assertEqual(bot.inventory_count(OAK_LOG_ITEM), 2)
        self.assertEqual(bot.observed_block(third).runtime_id, OAK_LOG_BLOCK)
        self.assert_no_more_collection_inputs()

    async def test_counted_collection_stops_when_confirmed_pickup_has_a_different_item_id(self):
        bot = await self.log_bot(SECOND_TARGET, TARGET)
        task = self.collect_logs(bot, count=1)
        await self.expect_batch_start(SECOND_TARGET)
        await self.finish_batch_break(SECOND_TARGET, runtime_id=101, total=1)
        result = await asyncio.wait_for(task, 1)
        attempt, = result.results
        self.assertEqual((attempt.status, attempt.drops[0].item_network_id), ("collected", DIRT))
        self.assertEqual(result.collected_count, 0)
        self.assertEqual(bot.inventory_count(OAK_LOG_ITEM), 0)
        self.assertEqual(bot.observed_block(TARGET).runtime_id, OAK_LOG_BLOCK)
        self.assert_no_more_collection_inputs()

    async def test_counted_collection_rejects_a_new_world_generation_with_the_same_id(self):
        bot = await self.log_bot(SECOND_TARGET, TARGET)
        async with bot._block_collector._lock:
            task = self.collect_logs(bot, count=2)
            await asyncio.sleep(0)
            await self.emit(self.b.ChangeDimensionPacket(dimension_id=0), self.block(OAK_LOG_BLOCK))
        with self.assertRaisesRegex(RuntimeError, "world changed") as failure:
            await asyncio.wait_for(task, 1)
        progress = failure.exception.collection_progress
        self.assertEqual((progress.positions, progress.collected_count, progress.results, progress.active),
                         ((SECOND_TARGET, TARGET), 0, (), None))
        self.assert_no_more_collection_inputs()

    async def test_counted_collection_preserves_pickup_confirmed_before_dimension_change(self):
        bot = await self.log_bot(SECOND_TARGET, TARGET)
        task = self.collect_logs(bot, count=2)
        await self.expect_batch_start(SECOND_TARGET)
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID, position=SECOND_TARGET),
            self.drop(position=(-2.5, 100.5, 4.5), item_id=OAK_LOG_ITEM),
            self.take(), self.slot(2, OAK_LOG_ITEM, 1),
            self.b.ChangeDimensionPacket(dimension_id=1), self.slot(2, OAK_LOG_ITEM, 3),
        )

        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.positions, (SECOND_TARGET, TARGET))
        attempt, = result.results
        self.assertEqual((attempt.position, attempt.status, attempt.collector_id, attempt.inventory_delta),
                         (SECOND_TARGET, "dimension_changed", PLAYER, 1))
        self.assertTrue(attempt.pickup_confirmed)
        self.assertEqual(result.collected_count, 1)
        self.assertEqual(bot.inventory_count(OAK_LOG_ITEM), 3)
        self.assert_no_more_collection_inputs()

    async def test_counted_collection_does_not_credit_ambiguous_pickup_before_dimension_change(self):
        bot = await self.log_bot(SECOND_TARGET, TARGET)
        task = self.collect_logs(bot, count=2)
        await self.expect_batch_start(SECOND_TARGET)
        self.wire.emit(
            self.drop(102, position=(1.5, 100.5, 4.5), item_id=OAK_LOG_ITEM), self.take(102),
            self.block(HASHED_AIR_RUNTIME_ID, position=SECOND_TARGET),
            self.drop(position=(-2.5, 100.5, 4.5), item_id=OAK_LOG_ITEM),
            self.take(), self.slot(2, OAK_LOG_ITEM, 1), self.b.ChangeDimensionPacket(dimension_id=1),
        )
        result = await asyncio.wait_for(task, 1)
        attempt, = result.results
        self.assertEqual((attempt.status, attempt.collector_id, attempt.inventory_delta), ("dimension_changed", PLAYER, 1))
        self.assertFalse(attempt.pickup_confirmed)
        self.assertEqual(result.collected_count, 0)
        self.assertEqual(bot.inventory_count(OAK_LOG_ITEM), 1)
        self.assert_no_more_collection_inputs()

    async def test_counted_collection_stops_after_subchunk_break_wait_times_out(self):
        bot = await self.log_bot(SECOND_TARGET, TARGET)
        task = self.collect_logs(bot, count=2, timeout=0.2)
        await self.expect_batch_start(SECOND_TARGET)
        b = self.b
        x, y, z = SECOND_TARGET
        pos = b.BlockPos(x=x, y=y, z=z)
        self.wire.emit(
            b.UpdateSubChunkBlocksPacket(
                sub_chunk_block_position=pos,
                blocks_changed=b.UpdateSubChunkBlocksChangedInfo(standards=[
                    b.UpdateSubChunkNetworkBlockInfo(pos=pos, runtime_id=HASHED_AIR_RUNTIME_ID),
                ]),
            ),
            self.drop(position=(-2.5, 100.5, 4.5), item_id=OAK_LOG_ITEM),
            self.take(), self.slot(2, OAK_LOG_ITEM, 1),
        )
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])

        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.positions, (SECOND_TARGET, TARGET))
        attempt, = result.results
        self.assertEqual((attempt.position, attempt.status, attempt.timed_out, attempt.inventory_delta),
                         (SECOND_TARGET, "collected", True, 1))
        self.assertTrue(attempt.block_after.is_air)
        self.assertEqual(result.collected_count, 1)
        self.assertEqual(bot.observed_block(TARGET).runtime_id, OAK_LOG_BLOCK)
        self.assert_no_more_collection_inputs()

    async def test_counted_collection_requires_a_positive_integer_count(self):
        bot = await self.log_bot(TARGET)
        for count in (0, -1, 1.5, True):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "positive integer"):
                await self.collect_logs(bot, count=count)
        self.assert_no_more_collection_inputs()

    async def mixed_drop(self, bot):
        await self.emit(self.drop(191, entity_id=5191, position=(1.5, 100.125, 3.5), item_id=77))
        return next(actor for actor in bot.observed_entities if actor.runtime_id == 191)

    async def next_mixed_move(self):
        async with asyncio.timeout(1):
            while True:
                packet = await self.wire.outgoing.get()
                if getattr(packet, "player_block_actions", None):
                    self.fail("unexpected mining input before the selected drop")
                if (isinstance(packet, self.b.PlayerAuthInputPacket)
                        and self.b.PlayerAuthInputPacket.InputData.UP in packet.input_data):
                    return packet

    def assert_no_more_collection_inputs(self):
        while not self.wire.outgoing.empty():
            packet = self.wire.outgoing.get_nowait()
            if isinstance(packet, self.b.PlayerAuthInputPacket):
                self.assertFalse(packet.player_block_actions)
                self.assertNotIn(self.b.PlayerAuthInputPacket.InputData.UP, packet.input_data)

    async def test_mixed_counts_distinct_inputs_and_accepts_a_snapshot_from_before_motion(self):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        b = self.b
        await self.emit(b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=191, pos=b.Vec3(x=1.75, y=100.125, z=3.5),
        )))
        task = asyncio.create_task(bot.collect_mixed_targets(
            [drop, drop, TARGET, list(TARGET), SECOND_TARGET], count=2, timeout=1,
        ))
        await self.next_mixed_move()
        self.wire.emit(self.drop(193, entity_id=5193, item_id=77), self.take(191))
        await self.expect_batch_start(TARGET)
        await self.finish_batch_break(TARGET, runtime_id=101, total=1)
        result = await asyncio.wait_for(task, 1)
        self.assertIsInstance(result, MixedCollectionBatch)
        self.assertEqual((result.dimension_id, result.targets, result.active), (0, (drop, TARGET), None))
        picked, block = result.results
        self.assertEqual((picked.target, picked.status, picked.pickup.item.position[0]), (drop, "picked_up", 1.75))
        self.assertEqual((block.position, block.status), (TARGET, "collected"))
        self.assertEqual(bot.observed_block(SECOND_TARGET).runtime_id, ORE)
        self.assertTrue(any(actor.runtime_id == 193 for actor in bot.observed_entities))

    async def test_mixed_running_follow_rejects_an_opposite_replacement_with_the_same_ids(self):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        task = asyncio.create_task(bot.collect_mixed_targets([drop, TARGET], timeout=1))
        frame = await self.next_mixed_move()
        self.assertGreater(frame.pos.x, -2.5)
        self.wire.emit(
            self.b.RemoveActorPacket(entity_id=5191),
            self.drop(191, entity_id=5191, position=(-9.5, 100.125, 3.5), item_id=77), self.take(191),
        )
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.results, (DropCollection(drop, "target_changed"),))
        self.assertEqual(bot.observed_block(TARGET).runtime_id, ORE)
        self.assert_no_more_collection_inputs()

    async def check_mixed_replacement_during_follow(self, runtime_id, entity_id):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        task = asyncio.create_task(bot.collect_mixed_targets([drop, TARGET], timeout=2))
        await self.next_mixed_move()
        self.wire.emit(self.drop(runtime_id, entity_id=entity_id, position=(-9.5, 100.125, 3.5), item_id=77))
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.results, (DropCollection(drop, "target_changed"),))
        self.assertTrue(any((actor.runtime_id, actor.entity_id) == (runtime_id, entity_id)
                            for actor in bot.observed_entities))
        self.assertEqual(bot.observed_block(TARGET).runtime_id, ORE)
        self.assert_no_more_collection_inputs()

    async def test_mixed_same_unique_id_at_a_new_runtime_stops_the_old_follow(self):
        await self.check_mixed_replacement_during_follow(192, 5191)

    async def test_mixed_same_runtime_with_a_new_unique_id_stops_the_old_follow(self):
        await self.check_mixed_replacement_during_follow(191, 5192)

    async def test_mixed_rekey_and_pickup_wake_after_the_follow_budget_ends(self):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        follow = bot._movement.follow_entity
        stopped = asyncio.Event()

        async def observe_follow(*args, **kwargs):
            result = await follow(*args, **kwargs)
            stopped.set()
            return result

        with patch.object(bot._movement, "follow_entity", new=observe_follow):
            task = asyncio.create_task(bot.collect_mixed_targets([drop, TARGET], ticks=0, timeout=2))
            await asyncio.wait_for(stopped.wait(), 1)
            # Allow the collector to consume the follow-completion notification first.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.wire.emit(self.drop(192, entity_id=5191, item_id=77), self.take(192))
            result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.results, (DropCollection(drop, "target_changed"),))
        self.assertFalse(any(actor.entity_id == 5191 for actor in bot.observed_entities))
        self.assertEqual(bot.observed_block(TARGET).runtime_id, ORE)
        self.assert_no_more_collection_inputs()

    async def test_mixed_rechecks_the_unique_id_of_a_queued_target(self):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        async with bot._block_collector._lock:
            task = asyncio.create_task(bot.collect_mixed_targets([drop, TARGET], timeout=1))
            await asyncio.sleep(0)
            await self.emit(self.drop(192, entity_id=5191, item_id=77))
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.results, (DropCollection(drop, "target_changed"),))
        self.assertTrue(self.wire.outgoing.empty())

    async def test_mixed_inventory_gain_without_a_pickup_receipt_is_unconfirmed(self):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        await self.emit(self.slot(3))
        task = asyncio.create_task(bot.collect_mixed_targets([drop, TARGET], timeout=1))
        await self.next_mixed_move()
        self.wire.emit(self.slot(3, 77, 1), self.b.RemoveActorPacket(entity_id=5191))
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.results, (DropCollection(drop, "pickup_unconfirmed"),))
        self.assertEqual(bot.inventory_count(77), 1)
        self.assertEqual(bot.observed_block(TARGET).runtime_id, ORE)
        self.assert_no_more_collection_inputs()

    async def check_mixed_early_pickup(self, *, collector):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        await self.emit(self.slot(3))
        limit = 2 if collector == PLAYER else None
        result_task = asyncio.create_task(bot.collect_mixed_targets(
            [TARGET, drop, SECOND_TARGET], count=limit, timeout=1,
        ))
        await self.expect_batch_start(TARGET)
        replacement_runtime = 192 if collector == PLAYER else 191
        self.wire.emit(
            self.take(191, player=collector), self.slot(3, 77, 1),
            self.drop(replacement_runtime, entity_id=5191, position=(-9.5, 100.125, 3.5), item_id=77),
        )
        await self.finish_batch_break(TARGET, runtime_id=101, total=1)
        result = await asyncio.wait_for(result_task, 2)
        block, item = result.results
        self.assertEqual((block.status, item.target, item.pickup.collector_runtime_id), ("collected", drop, collector))
        self.assertEqual(item.status, "picked_up" if collector == PLAYER else "picked_up_by_other")
        self.assertEqual(item.pickup.item.position, drop.position)
        replacement = next(actor for actor in bot.observed_entities if actor.runtime_id == replacement_runtime)
        self.assertEqual(replacement.position[0], -9.5)
        self.assertEqual(bot.observed_block(SECOND_TARGET).runtime_id, ORE)
        self.assert_no_more_collection_inputs()

    async def test_mixed_records_own_early_pickup_before_same_batch_replacement(self):
        await self.check_mixed_early_pickup(collector=PLAYER)

    async def test_mixed_early_other_pickup_beats_own_same_item_inventory_increase(self):
        await self.check_mixed_early_pickup(collector=99)

    async def test_mixed_binds_at_admission_and_keeps_no_active_attempt_on_queued_cancellation(self):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        coordinate = list(TARGET)
        async with bot._block_collector._lock:
            task = asyncio.create_task(bot.collect_mixed_targets([drop, coordinate], timeout=1))
            await asyncio.sleep(0)
            coordinate[:] = SECOND_TARGET
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 1)
            self.assertEqual(caught.exception.collection_progress, MixedCollectionBatch(0, (drop, TARGET), ()))

            task = asyncio.create_task(bot.collect_mixed_targets([drop, TARGET], timeout=1))
            await asyncio.sleep(0)
            await self.emit(
                self.b.RemoveActorPacket(entity_id=5191),
                self.drop(191, entity_id=5191, position=(-9.5, 100.125, 3.5), item_id=77),
            )
        self.assertEqual((await asyncio.wait_for(task, 1)).results, (DropCollection(drop, "target_changed"),))
        self.assertTrue(self.wire.outgoing.empty())

    async def test_mixed_keeps_world_generation_even_when_dimension_and_ids_repeat(self):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        async with bot._block_collector._lock:
            task = asyncio.create_task(bot.collect_mixed_targets([drop, TARGET], timeout=1))
            await asyncio.sleep(0)
            await self.emit(self.b.ChangeDimensionPacket(dimension_id=0),
                            self.drop(191, entity_id=5191, item_id=77), self.take(191))
        with self.assertRaisesRegex(RuntimeError, "world changed") as caught:
            await asyncio.wait_for(task, 1)
        self.assertEqual(caught.exception.collection_progress, MixedCollectionBatch(0, (drop, TARGET), ()))
        self.assert_no_more_collection_inputs()

    async def test_collect_drop_checks_its_instance_at_submission_and_on_the_pickup_packet(self):
        bot = await self.batch_bot()
        await self.mixed_drop(bot)
        send = bot._movement._send_input
        attempted = asyncio.Event()

        async def change_before_send(forward, **kwargs):
            if forward:
                attempted.set()
                await self.emit(
                    self.b.RemoveActorPacket(entity_id=5191),
                    self.drop(191, entity_id=5191, position=(-9.5, 100.125, 3.5), item_id=77),
                    self.take(191),
                )
            return await send(forward, **kwargs)

        with patch.object(bot._movement, "_send_input", new=change_before_send):
            with self.assertRaises(TimeoutError):
                await bot.collect_drop(191, ticks=4, timeout=0.75)
        self.assertTrue(attempted.is_set())
        self.assert_no_more_collection_inputs()

    async def check_mixed_drop_interruption(self, *, disconnect):
        bot = await self.batch_bot()
        drop = await self.mixed_drop(bot)
        subscribers = set(self.packets._subscribers)
        task = asyncio.create_task(bot.collect_mixed_targets([TARGET, drop, SECOND_TARGET], timeout=2))
        await self.expect_batch_start(TARGET)
        await self.finish_batch_break(TARGET, runtime_id=101, total=1)
        await self.next_mixed_move()
        if disconnect:
            self.wire.emit(self.b.DisconnectPacket())
        else:
            task.cancel()
        with self.assertRaises(ConnectionError if disconnect else asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 2)
        progress = caught.exception.collection_progress
        self.assertEqual(progress.targets, (TARGET, drop, SECOND_TARGET))
        block, = progress.results
        self.assertEqual((block.position, block.status), (TARGET, "collected"))
        self.assertEqual(progress.active, DropCollection(drop, "pickup_unconfirmed"))
        self.assertFalse(bot._block_collector._lock.locked())
        self.assertLessEqual(set(self.packets._subscribers), subscribers)
        self.assert_no_more_collection_inputs()

    async def test_mixed_active_cancellation_keeps_the_prefix_and_unconfirmed_drop(self):
        await self.check_mixed_drop_interruption(disconnect=False)

    async def test_mixed_active_disconnect_keeps_the_prefix_and_unconfirmed_drop(self):
        await self.check_mixed_drop_interruption(disconnect=True)

    async def test_empty_batch_and_first_business_failure_send_no_mining_input(self):
        bot = await self.batch_bot()
        self.assertEqual(await bot.collect_targets([TARGET], count=0), BlockCollectionBatch(0, (), ()))
        with self.assertRaises(ValueError):
            await bot.collect_targets([TARGET], count=-1)
        with self.assertRaises(ValueError):
            await bot.collect_targets([(0, 100), TARGET])

        missing = (2, 100, 3)
        result = await bot.collect_targets([missing, TARGET])
        first, = result.results
        self.assertEqual((first.position, first.status), (missing, "target_unobserved"))
        self.assertEqual(result.positions, (missing, TARGET))
        self.assertIsNone(result.active)
        self.assertEqual(bot.observed_block(TARGET).runtime_id, ORE)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_batch_checks_queued_targets_against_admission_observations(self):
        bot = await self.batch_bot()
        collector = bot._block_collector
        async with collector._lock:
            task = asyncio.create_task(bot.collect_targets([TARGET, SECOND_TARGET]))
            await asyncio.sleep(0)
            await self.emit(self.block(0x80310E21))
        result = await asyncio.wait_for(task, 1)
        first, = result.results
        self.assertEqual((first.status, first.block_before.runtime_id), ("target_changed", ORE))
        self.assertEqual(bot.observed_block(TARGET).runtime_id, 0x80310E21)
        self.assertTrue(self.wire.outgoing.empty())

    async def test_batch_world_epoch_expires_even_when_the_dimension_id_is_reused(self):
        bot = await self.batch_bot()
        collector = bot._block_collector
        async with collector._lock:
            task = asyncio.create_task(bot.collect_targets([TARGET, SECOND_TARGET]))
            await asyncio.sleep(0)
            await self.emit(self.b.ChangeDimensionPacket(dimension_id=0), self.block(ORE))
        with self.assertRaisesRegex(RuntimeError, "world changed") as failure:
            await asyncio.wait_for(task, 1)
        self.assertEqual(failure.exception.collection_progress, BlockCollectionBatch(0, (TARGET, SECOND_TARGET), ()))
        self.assertTrue(self.wire.outgoing.empty())

    async def test_batch_owns_collection_lock_across_targets(self):
        bot = await self.batch_bot()
        async with asyncio.TaskGroup() as tasks:
            batch = tasks.create_task(bot.collect_targets([TARGET, SECOND_TARGET], timeout=1))
            await self.expect_batch_start(TARGET)
            single = tasks.create_task(bot.collect_block(THIRD_TARGET, timeout=1))
            await asyncio.sleep(0)
            await self.finish_batch_break(TARGET, runtime_id=101, total=1)
            await self.expect_batch_start(SECOND_TARGET)
            self.assertFalse(single.done())
            await self.emit(self.block(HASHED_AIR_RUNTIME_ID, position=THIRD_TARGET))
            await self.finish_batch_break(SECOND_TARGET, runtime_id=102, total=2)
        self.assertEqual([item.status for item in batch.result().results], ["collected", "collected"])
        self.assertEqual(single.result().status, "target_air")
        self.assertTrue(self.wire.outgoing.empty())

    async def check_batch_interruption(self, *, disconnect):
        bot = await self.batch_bot()
        subscribers = set(self.packets._subscribers)
        positions = TARGET, SECOND_TARGET, THIRD_TARGET
        task = asyncio.create_task(bot.collect_targets(positions, timeout=2))
        await self.expect_batch_start(TARGET)
        await self.finish_batch_break(TARGET, runtime_id=101, total=1)
        await self.expect_batch_start(SECOND_TARGET)
        await self.finish_batch_break(SECOND_TARGET, runtime_id=102)
        self.assertEqual(bot.observed_block(THIRD_TARGET).runtime_id, ORE)

        if disconnect:
            self.wire.emit(self.b.DisconnectPacket())
            expected = ConnectionError
        else:
            task.cancel()
            expected = asyncio.CancelledError
        with self.assertRaises(expected) as interruption:
            await asyncio.wait_for(task, 1)
        progress = interruption.exception.collection_progress
        self.assertEqual((progress.dimension_id, progress.positions), (0, positions))
        first, = progress.results
        self.assertEqual((first.position, first.status, first.inventory_delta), (TARGET, "collected", 1))
        active = progress.active
        self.assertEqual((active.position, active.status), (SECOND_TARGET, "pickup_unconfirmed"))
        self.assertTrue(active.block_after.is_air)
        self.assertEqual((active.approach.status, active.tool.item_name), ("reached", "minecraft:iron_pickaxe"))
        drop, = active.drops
        self.assertEqual((drop.runtime_id, active.collector_id, active.inventory_delta), (102, None, 0))
        self.assertFalse(bot._block_collector._lock.locked())
        self.assertLessEqual(set(self.packets._subscribers), subscribers)
        while not self.wire.outgoing.empty():
            packet = self.wire.outgoing.get_nowait()
            self.assertFalse(getattr(packet, "player_block_actions", None))

    async def test_batch_cancellation_keeps_completed_and_active_evidence(self):
        await self.check_batch_interruption(disconnect=False)

    async def test_batch_disconnect_keeps_completed_and_active_evidence(self):
        await self.check_batch_interruption(disconnect=True)

    async def test_batch_cancelled_while_queued_has_no_active_attempt(self):
        bot = await self.batch_bot()
        async with bot._block_collector._lock:
            task = asyncio.create_task(bot.collect_targets([TARGET]))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as interruption:
                await asyncio.wait_for(task, 1)
        self.assertEqual(interruption.exception.collection_progress, BlockCollectionBatch(0, (TARGET,), ()))
        self.assertTrue(self.wire.outgoing.empty())

    async def test_batch_cancelled_during_start_preserves_unconfirmed_break(self):
        bot = await self.batch_bot()
        self.wire.pause_start = True
        task = asyncio.create_task(bot.collect_targets([TARGET, SECOND_TARGET], timeout=1))
        await self.expect_batch_start(TARGET)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as interruption:
            await asyncio.wait_for(task, 1)
        progress = interruption.exception.collection_progress
        self.assertEqual(progress.results, ())
        self.assertEqual((progress.active.position, progress.active.status, progress.active.block_after),
                         (TARGET, "break_unconfirmed", None))
        self.assertEqual(await self.next_actions(), ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
        self.assertTrue(self.wire.outgoing.empty())

    def ready_to_spawn(self, runtime_id):
        b = self.b
        return b.RespawnPacket(runtime_id=runtime_id, state=b.PlayerRespawnState.READY_TO_SPAWN,
                               pos=b.Vec3(x=-2.5, y=101.62, z=3.5))

    async def collect_across_respawn(self, task, *, runtime_id):
        await self.expect_batch_start(TARGET)
        await self.finish_batch_break(TARGET, runtime_id=101)
        # Dispatch the new-generation inventory evidence in the same native batch.
        self.wire.emit(self.ready_to_spawn(runtime_id), self.take(), self.slot(2, DIRT, 1))
        return await asyncio.wait_for(task, 1)

    def assert_pre_respawn_evidence(self, result):
        self.assertEqual((result.status, result.timed_out), ("pickup_unconfirmed", False))
        self.assertTrue(result.block_after.is_air)
        drop, = result.drops
        self.assertEqual((drop.runtime_id, result.collector_id, result.inventory_delta), (101, None, 0))
        self.assertEqual((result.approach.status, result.tool.item_name), ("reached", "minecraft:iron_pickaxe"))

    async def test_batch_own_respawn_seals_evidence_before_adjacent_pickup_packets(self):
        bot = await self.batch_bot()
        previous_epoch = bot._blocks._mining_epoch
        task = asyncio.create_task(bot.collect_targets([TARGET, SECOND_TARGET], timeout=2))
        result = await self.collect_across_respawn(task, runtime_id=PLAYER)
        self.assertEqual(bot._blocks._mining_epoch, previous_epoch + 1)
        self.assertEqual(result.positions, (TARGET, SECOND_TARGET))
        self.assertIsNone(result.active)
        attempt, = result.results
        self.assert_pre_respawn_evidence(attempt)
        self.assertEqual(bot.inventory_count(DIRT), 1)
        self.assertEqual(bot.observed_block(SECOND_TARGET).runtime_id, ORE)
        while not self.wire.outgoing.empty():
            packet = self.wire.outgoing.get_nowait()
            self.assertFalse(getattr(packet, "player_block_actions", None))

    async def test_single_own_respawn_zero_id_seals_pickup_evidence(self):
        bot = await self.public_bot()
        previous_epoch = bot._blocks._mining_epoch
        task = asyncio.create_task(bot.collect_block(TARGET, timeout=2))
        await self.expect_batch_start(TARGET)
        self.wire.emit(self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.ready_to_spawn(0), self.take(),
                       self.slot(2, DIRT, 1))
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(bot._blocks._mining_epoch, previous_epoch + 1)
        self.assert_pre_respawn_evidence(result)
        self.assertEqual(bot.inventory_count(DIRT), 1)

    async def test_batch_other_player_respawn_keeps_pickup_observation(self):
        bot = await self.public_bot()
        previous_epoch = bot._blocks._mining_epoch
        task = asyncio.create_task(bot.collect_targets([TARGET], timeout=2))
        result = await self.collect_across_respawn(task, runtime_id=PLAYER + 1)
        self.assertEqual(bot._blocks._mining_epoch, previous_epoch)
        attempt, = result.results
        self.assertEqual((attempt.status, attempt.collector_id, attempt.inventory_delta), ("collected", PLAYER, 1))

    async def test_single_own_respawn_before_break_keeps_world_change_error(self):
        bot = await self.public_bot()
        task = asyncio.create_task(bot.collect_block(TARGET, timeout=2))
        await self.expect_batch_start(TARGET)
        self.wire.emit(self.ready_to_spawn(PLAYER), self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(),
                       self.slot(2, DIRT, 1))
        with self.assertRaisesRegex(RuntimeError, "world changed"):
            await asyncio.wait_for(task, 1)

    async def check_equipment_disconnect(self, *, inventory_opened):
        bot = await self.public_bot(tool_slot=12)
        subscribers = set(self.packets._subscribers) - {
            bot._containers._on_packet, bot._movement._on_packet,
        }
        active = asyncio.create_task(bot.collect_block(TARGET, timeout=5))
        queued = None
        try:
            opening = await asyncio.wait_for(self.wire.outgoing.get(), 1)
            self.assertEqual(opening.action, self.b.InteractPacket.Action.OPEN_INVENTORY)
            open_packet = self.b.ContainerOpenPacket(
                container_id=2, type=self.b.ContainerType.INVENTORY,
                pos=self.b.BlockPos(x=0, y=0, z=0), entity_unique_id=-1,
            )
            if inventory_opened:
                self.wire.emit(open_packet)
                request = await asyncio.wait_for(self.wire.outgoing.get(), 1)
                self.assertIsInstance(request, self.b.ItemStackRequestPacket)
                self.assertIsNotNone(bot.open_window)
            queued = asyncio.create_task(bot.collect_block(TARGET, timeout=5))
            await asyncio.sleep(0)
            self.wire.emit(self.b.DisconnectPacket(), open_packet)
            done, _ = await asyncio.wait((active, queued), timeout=0.75)
            self.assertEqual(done, {active, queued})
            for task in (active, queued):
                with self.assertRaisesRegex(ConnectionError, "server disconnected"):
                    await task
            with self.assertRaisesRegex(ConnectionError, "server disconnected"):
                await bot.collect_block(TARGET)
            self.assertFalse(self.packets._reader_task.done())
            self.assertIsNone(bot.open_window)
            self.assertIsNone(bot._containers._inventory_open)
            self.assertFalse(bot._block_collector._lock.locked())
            # Other session features may also remove their own observers on a kick.
            self.assertLessEqual(set(self.packets._subscribers), subscribers)
            self.assertTrue(self.wire.outgoing.empty())
        finally:
            # Retire the fake session even on failure, without leaving tasks behind.
            await bot.close()
            await asyncio.gather(*(task for task in (active, queued) if task is not None),
                                 return_exceptions=True)

    async def test_disconnect_interrupts_inventory_open_and_queued_collection(self):
        await self.check_equipment_disconnect(inventory_opened=False)

    async def test_disconnect_releases_owned_inventory_during_transfer(self):
        await self.check_equipment_disconnect(inventory_opened=True)

    async def test_other_player_pickup_does_not_match_our_inventory_gain(self):
        await self.emit(self.slot(0))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(player=12),
            self.slot(0, DIRT, 1),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual((result.status, result.collector_id), ("taken_by_other", 12))
        self.assert_released()

    async def test_same_type_from_another_drop_can_explain_main_inventory_gain(self):
        await self.emit(self.slot(0), self.drop(102, position=(-2.1, 100.5, 3.5)))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(102), self.take(),
            self.slot(0, DIRT, 1),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(result.status, "ambiguous_drop")
        self.assertEqual((result.collector_id, result.inventory_delta), (PLAYER, 1))
        self.assert_released()

    async def test_pickup_and_newly_reported_slot_leave_the_delta_unknown(self):
        await self.emit(self.slot(0))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(),
            self.slot(1, DIRT, 1),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(result.status, "inventory_unconfirmed")
        self.assertEqual(result.collector_id, PLAYER)
        self.assertIsNone(result.inventory_delta)
        self.assertTrue(result.timed_out)
        self.assert_released()

    async def test_later_inventory_content_cannot_erase_a_missing_prior_slot_value(self):
        await self.emit(self.slot(0))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(),
            self.slot(1), self.content(self.item(DIRT, 1)),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual(result.status, "inventory_unconfirmed")
        self.assertIsNone(result.inventory_delta)
        self.assert_released()

    async def test_main_inventory_net_change_excludes_rearrangement_and_offhand(self):
        await self.emit(self.slot(0, DIRT, 1), self.slot(1))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(),
            self.inventory_transaction((0, 1, 0), (1, 0, 1)),
            self.slot(0, DIRT, 1, container=self.b.ContainerID.OFFHAND),
            self.slot(36, DIRT, 1),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual((result.status, result.inventory_delta), ("inventory_unconfirmed", 0))
        self.assert_released()

    async def test_transaction_with_a_stale_cache_does_not_invent_pickup_quantity(self):
        await self.emit(self.slot(0, DIRT, 4))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(count=2), self.take(),
            self.inventory_transaction((0, 5, 6)),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual((result.status, result.collector_id), ("inventory_unconfirmed", PLAYER))
        self.assertIsNone(result.inventory_delta)
        self.assert_released()

    async def test_transaction_checks_net_before_and_after_across_main_slots(self):
        await self.emit(self.slot(0, DIRT, 4), self.slot(1, DIRT, 1))
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(),
            self.inventory_transaction((0, 5, 6), (1, 0, 0)),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual((result.status, result.inventory_delta), ("collected", 1))
        self.assert_released()

    async def test_timeout_ignores_air_for_other_positions_and_layers_and_aborts_once(self):
        task = await self.start()
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID, position=(-1, 100, 3)),
            self.block(HASHED_AIR_RUNTIME_ID, layer=1),
            self.block(901),
        )
        result = await task
        self.assertEqual(result.status, "break_unconfirmed")
        self.assertTrue(result.timed_out)
        self.assertEqual(await self.next_actions(), ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
        self.assert_released()

    async def test_cancellation_during_mining_aborts_and_unsubscribes(self):
        task = await self.start()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self.next_actions(), ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
        self.assert_released()

    async def test_external_break_while_waiting_for_movement_does_not_start_or_collect(self):
        await self.emit(self.slot(0))
        async with self.blocks.movement._action_lock:
            task = self.collect(timeout=1)
            await self.wait_for_collection_subscribers()
            await self.emit(
                self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(),
                self.slot(0, DIRT, 1),
            )
            self.assertTrue(self.wire.outgoing.empty())
        result = await task
        self.assertEqual(result.status, "target_changed")
        self.assertIsNone(result.block_after)
        self.assertEqual(result.drops, ())
        self.assert_released()

    async def test_drops_and_inventory_changes_before_start_do_not_enter_the_attempt(self):
        await self.emit(self.slot(0))
        async with self.blocks.movement._action_lock:
            task = self.collect(timeout=1)
            await self.wait_for_collection_subscribers()
            await self.emit(self.drop(102), self.take(102), self.slot(0, DIRT, 1))
        self.assertEqual(await self.next_actions(), ["START_DESTROY_BLOCK"])
        self.wire.emit(
            self.block(HASHED_AIR_RUNTIME_ID), self.drop(), self.take(), self.slot(0, DIRT, 2),
        )
        result = await task
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.assertEqual((result.status, result.inventory_delta), ("collected", 1))
        self.assertEqual([drop.runtime_id for drop in result.drops], [101])
        self.assert_released()

    async def test_cancelling_queued_mining_sends_no_abort(self):
        async with self.blocks.movement._action_lock:
            task = self.collect(timeout=1)
            await self.wait_for_collection_subscribers()
            task.cancel()
            done, _ = await asyncio.wait((task,), timeout=0.5)
            self.assertIn(task, done)
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assert_released()

    async def test_cancelling_after_start_reaches_the_wire_aborts_while_send_is_pending(self):
        self.wire.pause_start = True
        task = await self.start(timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self.next_actions(), ["ABORT_DESTROY_BLOCK", "STOP_DESTROY_BLOCK"])
        self.assert_released()

    async def test_local_block_action_construction_error_sends_no_actions(self):
        with self.assertRaises(TypeError):
            await self.collect(face=2**64)
        self.assert_released()

    async def test_server_disconnect_packet_interrupts_instead_of_timing_out(self):
        task = await self.start()
        self.wire.emit(self.b.DisconnectPacket())
        with self.assertRaisesRegex(ConnectionError, "server disconnected"):
            await task
        self.assert_released()

    async def test_dimension_change_preserves_only_evidence_from_the_original_dimension(self):
        task = await self.start(timeout=3)
        self.wire.emit(self.block(HASHED_AIR_RUNTIME_ID), self.drop())
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.wire.emit(self.b.ChangeDimensionPacket(dimension_id=1), self.take())
        result = await task
        self.assertEqual(result.status, "dimension_changed")
        self.assertFalse(result.timed_out)
        self.assertTrue(result.block_after.is_air)
        self.assertEqual(result.drops[0].runtime_id, 101)
        self.assertIsNone(result.collector_id)
        self.assert_released()

    async def test_transport_loss_after_break_does_not_become_a_partial_timeout(self):
        task = await self.start()
        self.wire.emit(self.block(HASHED_AIR_RUNTIME_ID))
        self.assertEqual(await self.next_actions(), ["STOP_DESTROY_BLOCK"])
        self.wire.incoming.put_nowait(OSError("lost wire"))
        with self.assertRaisesRegex(ConnectionError, "packet stream lost") as failure:
            await task
        self.assertIsInstance(failure.exception.__cause__, OSError)
        self.assert_released()

    async def test_unobserved_and_air_targets_do_not_submit_mining_actions(self):
        result = await _observe_mined_block(
            self.blocks, self.entities, (0, 64, 0), face=4, timeout=0.1,
        )
        self.assertEqual(result.status, "target_unobserved")
        await self.emit(self.block(HASHED_AIR_RUNTIME_ID))
        result = await _observe_mined_block(
            self.blocks, self.entities, TARGET, face=4, timeout=0.1,
        )
        self.assertEqual(result.status, "target_air")
        self.assert_released()
