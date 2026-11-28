import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import Bot, ItemRegistrySnapshot
from endpilot.block_data import _catalog
from endpilot.blocks import ColumnCoverageInvalidated, HASHED_UNKNOWN_RUNTIME_ID
from endpilot.compression import PacketConnection
from endpilot.harvest import (
    HarvestTool, HarvestToolChest, HarvestToolSupply, equip_harvest_tool_from_chest, equip_harvest_tool_from_chests,
)
from endpilot.inventory import _InventoryMoveNotSubmitted
from endpilot.login import JoinSession

from test_container_take import item
from test_harvest import DIAMOND_ORE, DIRT, enchantment_data
from test_range_transfer import RangePacketStream
from test_subchunks import solid_sub_chunk


TARGET = (3, 100, 0)
CHEST = (0, 100, 2)
SECOND_CHEST = (2, 100, 2)
IRON, GOLD, PAPER = 1201, 1202, 1203
IRON_NAME = "minecraft:iron_pickaxe"
STORAGE = "LEVEL_ENTITY_CONTAINER"
PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"


class HarvestChestRefillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("RefillTest", protocol=2225)
        self.b = self.bot.protocol.binding
        self.stream = RangePacketStream(self.bot.protocol)
        self.stream.subscribe_raw = lambda callback: PacketConnection._subscribe(self.stream.observers, callback)
        self.stream.close = AsyncMock()
        session = JoinSession(self.stream, self.bot.identity, "127.0.0.1")
        session.player_id = session.player_unique_id = 1
        session.block_network_ids_are_hashes = True
        session.item_names = {IRON: IRON_NAME, GOLD: "minecraft:golden_pickaxe", PAPER: "minecraft:paper"}
        session.item_registry = ItemRegistrySnapshot(session.item_names)
        with patch.object(JoinSession, "connect_raknet", return_value=session):
            await self.bot.join_raknet("127.0.0.1")
        self.addAsyncCleanup(self.bot.close)
        movement = self.bot._movement
        movement._ticker.cancel()
        await asyncio.gather(movement._ticker, return_exceptions=True)
        movement.position = movement._input_position = (0.5, 101.62, 0.5)
        movement._updated.set()
        palette, hashes = _catalog()
        chest_id = next(runtime for runtime, index in hashes.items()
                        if palette[index] is not None and palette[index].name == "minecraft:chest")
        self.block(TARGET, DIAMOND_ORE)
        self.block(CHEST, chest_id)
        self.block(SECOND_CHEST, chest_id)
        self.contents(0, 36, {0: self.stack(GOLD, 10)})
        self.before_write = None
        self.after_write = None
        send_packet = self.stream.send

        async def send(packet, **kwargs):
            if self.before_write is not None:
                self.before_write(packet)
            await send_packet(packet, **kwargs)
            if self.after_write is not None:
                self.after_write(packet)

        self.stream.send = send

    def emit(self, packet):
        self.stream.emit(self.bot.protocol.encode(packet))

    def block(self, position, runtime_id):
        x, y, z = position
        self.emit(self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=0))

    def stack(self, item_id=0, net_id=None, *, efficiency=None):
        result = item(self.b, item_id, int(bool(item_id)), net_id)
        if efficiency is not None:
            result.user_data_buffer = enchantment_data(self.b, efficiency)
        return result

    def contents(self, window_id, width, stacks):
        container_name = self.b.ContainerEnumName[PLAYER if window_id == 0 else STORAGE]
        self.emit(self.b.InventoryContentPacket(
            inventory_id=window_id, slots=[stacks.get(index, self.stack()) for index in range(width)],
            full_container_name=self.b.FullContainerName(name=container_name),
            storage_item=self.stack(),
        ))

    def slot(self, window_id, index, stack):
        self.emit(self.b.InventorySlotPacket(
            inventory_id=window_id, slot=index, item=stack, full_container_name=None, storage_item=None,
        ))

    def open_storage(self, *, position=CHEST, stacks=None, width=27):
        x, y, z = position
        self.emit(self.b.ContainerOpenPacket(
            container_id=2, type=self.b.ContainerType.CONTAINER,
            pos=self.b.BlockPos(x=x, y=y, z=z), entity_unique_id=-1,
        ))
        if stacks is not None:
            self.contents(2, width, stacks)
        return self.bot.open_window

    def close_storage(self):
        self.emit(self.b.ContainerClosePacket(
            container_id=2, container_type=self.b.ContainerType.CONTAINER, server_initiated_close=False,
        ))

    def reply(self, request, *corrections, verdict="SUCCESS"):
        b = self.b
        self.emit(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult[verdict],
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[group]),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=slot, slot=slot, amount=count,
                    item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                    custom_name=b.RedactableString(unredacted_string="", redacted_string=None),
                    durability_correction=0,
                )],
            ) for group, slot, count, net_id in corrections] if corrections else None,
        )]))

    def actions(self):
        return [action for packet in self.stream.sent if isinstance(packet, self.b.ItemStackRequestPacket)
                for request in packet.requests for action in request.actions]

    def equipment(self):
        return [packet for packet in self.stream.sent if isinstance(packet, self.b.MobEquipmentPacket)]

    async def acquire(self, *, allowed=range(4, 7), destinations=range(14, 16), timeout=1, max_tools=1):
        return await equip_harvest_tool_from_chest(
            self.bot, TARGET, chest=CHEST, chest_slots=allowed, inventory_slots=destinations,
            face=2, timeout=timeout, max_tools=max_tools,
        )

    async def supply(self, *, chests=None, timeout=1, max_tools=1):
        if chests is None:
            chests = (HarvestToolChest(CHEST, range(4, 5), range(3, 4)),
                      HarvestToolChest(SECOND_CHEST, range(6, 7), range(3, 4)))
        return await equip_harvest_tool_from_chests(
            self.bot, TARGET, chests=chests, face=2, timeout=timeout, max_tools=max_tools,
        )

    def supply_server(self, packet):
        if isinstance(packet, self.b.InventoryTransactionPacket):
            pos = packet.variant_transaction.pos
            position = (pos.x, pos.y, pos.z)
            stacks = ({4: self.stack(GOLD, 14)} if position == CHEST else
                      {6: self.stack(IRON, 16), 8: self.stack(IRON, 18, efficiency=5)})
            self.open_storage(position=position, stacks=stacks)
        elif isinstance(packet, self.b.ContainerClosePacket):
            self.emit(packet)

    def chest_clicks(self):
        return [packet.variant_transaction.pos for packet in self.stream.sent
                if isinstance(packet, self.b.InventoryTransactionPacket)]

    def backups(self, destinations=range(3, 5)):
        return (HarvestToolChest(CHEST, range(4, 6), destinations),
                HarvestToolChest(SECOND_CHEST, range(6, 7), destinations))

    def backups_server(self, packet, *, outcome="success", number=2):
        b = self.b
        if isinstance(packet, b.InventoryTransactionPacket):
            pos = packet.variant_transaction.pos
            stocks = {4: self.stack(IRON, 14), 8: self.stack(IRON, 18, efficiency=5)}
            if number == 2:
                stocks[5] = self.stack(IRON, 15, efficiency=2)
            self.open_storage(position=(pos.x, pos.y, pos.z), stacks=stocks)
        elif isinstance(packet, b.ItemStackRequestPacket):
            request, = packet.requests
            action, = request.actions
            source, destination = int(action.source.slot), int(action.destination.slot)
            if isinstance(action, b.ItemStackRequestCereal.TakeActionData):
                if len(self.actions()) == 2:
                    if outcome == "reject":
                        self.reply(request, verdict="ERROR")
                        return
                    if outcome == "reopen":
                        self.open_storage(stacks={4: self.stack(PAPER, 42)})
                    if outcome == "pending":
                        return
                self.reply(request, (STORAGE, source, 0, None), (PLAYER, destination, 1, 90 + source))
            else:
                self.reply(request, (PLAYER, source, 0, None), (PLAYER, destination, 1, 250))
        elif isinstance(packet, b.ContainerClosePacket):
            self.emit(packet)

    async def test_multiple_supply_ranks_both_receipts_and_later_calls_use_inventory(self):
        self.after_write = self.backups_server
        result = await self.supply(chests=self.backups(), max_tools=2)
        attempt, = result.attempts
        self.assertEqual((result.status, result.tool.slot, attempt.transfer.stop_reason), ("equipped", 3, "complete"))
        self.assertEqual(attempt.transfer.requested, attempt.transfer.confirmed_count)
        self.assertEqual([(row.source_slot, row.destination_slot, row.confirmed) for row in attempt.transfer.steps],
                         [(5, 3, 1), (4, 4, 1)])
        self.assertEqual((self.bot.inventory_slots[3].net_id_variant, self.bot.inventory_slots[4].net_id_variant), (95, 94))
        equipped, = self.equipment()
        self.assertEqual(equipped.item.net_id_variant, 95)
        self.assertEqual(len(self.chest_clicks()), 1)

        self.stream.sent.clear()
        repeated = await self.supply(chests=self.backups(), max_tools=200)
        self.assertEqual(repeated, HarvestToolSupply("equipped", tool=result.tool))
        self.assertEqual(self.stream.sent, self.equipment())

    async def test_multiple_supply_equips_confirmed_tool_after_known_source_or_destination_exhaustion(self):
        for stock, destination, reason in ((1, range(3, 5), "source_exhausted"),
                                           (2, range(3, 4), "destination_full")):
            with self.subTest(reason=reason):
                self.contents(0, 36, {0: self.stack(GOLD, 10)})
                self.stream.sent.clear()
                self.after_write = lambda packet: self.backups_server(packet, number=stock)
                result = await self.supply(chests=self.backups(destination), max_tools=10**100)
                attempt, = result.attempts
                self.assertEqual((result.status, attempt.transfer.stop_reason, attempt.transfer.confirmed_count),
                                 ("equipped", reason, 1))
                self.assertEqual(len(self.actions()), 1)
                self.assertEqual(len(self.chest_clicks()), 1)
                self.assertEqual(len(self.equipment()), 1)

    async def test_multiple_supply_second_rejection_or_replacement_keeps_first_without_equipping(self):
        for outcome, status in (("reject", "rejected"), ("reopen", "window_changed")):
            with self.subTest(outcome=outcome):
                self.contents(0, 36, {0: self.stack(GOLD, 10)})
                self.stream.sent.clear()
                self.after_write = lambda packet: self.backups_server(packet, outcome=outcome)
                result = await self.supply(chests=self.backups(), max_tools=2)
                attempt, = result.attempts
                first, second = attempt.transfer.steps
                self.assertEqual((result.status, first.source_slot, first.confirmed), (status, 5, 1))
                self.assertEqual(second.source_slot, 4)
                self.assertEqual((second.confirmed, second.verdict), (0, "ERROR") if outcome == "reject" else (1, "SUCCESS"))
                self.assertEqual(attempt.transfer.confirmed_count, 1 if outcome == "reject" else 2)
                self.assertIsNone(result.tool)
                self.assertEqual(self.equipment(), [])
                self.assertEqual(len(self.chest_clicks()), 1)
                if outcome == "reopen":
                    self.assertEqual(self.bot.container_slots[4].net_id_variant, 42)
                    self.assertIsNot(self.bot.open_window, attempt.window)

    async def test_multiple_supply_stops_if_inventory_gains_tool_or_replaces_confirmed_stack_before_second_write(self):
        for slot, stack, status in ((6, self.stack(IRON, 26), "inventory_available"),
                                     (3, self.stack(PAPER, 23), "equip_unavailable")):
            with self.subTest(status=status):
                self.contents(0, 36, {0: self.stack(GOLD, 10)})
                self.stream.sent.clear()
                self.after_write = self.backups_server

                def replace_slot(packet):
                    if isinstance(packet, self.b.ItemStackRequestPacket) and len(self.actions()) == 1:
                        self.slot(0, slot, stack)

                self.before_write = replace_slot
                result = await self.supply(chests=self.backups(), max_tools=2)
                attempt, = result.attempts
                self.assertEqual((result.status, attempt.transfer.stop_reason, attempt.transfer.confirmed_count),
                                 (status, status, 1))
                self.assertEqual(len(self.actions()), 1)
                self.assertEqual(len(self.chest_clicks()), 1)
                self.assertEqual(self.equipment(), [])

    async def test_multiple_supply_cancellation_keeps_pending_second_and_matching_first(self):
        pending = asyncio.Event()

        def server(packet):
            self.backups_server(packet, outcome="pending")
            if isinstance(packet, self.b.ItemStackRequestPacket) and len(self.actions()) == 2:
                pending.set()

        self.after_write = server
        task = asyncio.create_task(self.supply(chests=self.backups(), max_tools=2))
        await asyncio.wait_for(pending.wait(), 1)
        task.cancel("stop after second take")
        with self.assertRaisesRegex(asyncio.CancelledError, "stop after second take") as caught:
            await task
        error = caught.exception
        attempt, = error.harvest_supply_progress.attempts
        self.assertIs(attempt, error.harvest_progress)
        self.assertIs(attempt.transfer, error.transfer_progress)
        first, second = attempt.transfer.steps
        self.assertEqual((first.confirmed, first.verdict, second.confirmed, second.request_id), (1, "SUCCESS", None, -3))
        self.assertEqual(len(self.chest_clicks()), 1)
        self.assertEqual(self.equipment(), [])
        self.assertIsNone(self.bot.open_window)

    async def test_multiple_single_chest_timeout_keeps_pending_second(self):
        self.after_write = lambda packet: self.backups_server(packet, outcome="pending")
        with self.assertRaises(TimeoutError) as caught:
            await self.acquire(max_tools=2, timeout=0.08)
        error = caught.exception
        self.assertIs(error.harvest_progress.transfer, error.transfer_progress)
        first, second = error.transfer_progress.steps
        self.assertEqual((first.confirmed, second.confirmed, second.request_id), (1, None, -3))
        self.assertEqual(self.equipment(), [])
        self.assertIsNone(self.bot.open_window)

    async def test_multiple_single_chest_uses_total_budget_and_reuses_hotbar_equipment_path(self):
        async with self.bot._harvest_tools._equip_lock:
            with self.assertRaises(TimeoutError) as caught:
                await self.acquire(max_tools=2, timeout=0.01)
        self.assertIsNone(caught.exception.harvest_progress.transfer)
        self.assertEqual(self.stream.sent, [])

        self.after_write = self.backups_server
        result = await self.acquire(allowed=range(4, 6), destinations=range(14, 16), max_tools=2)
        self.assertEqual((result.status, result.transfer.confirmed_count, result.tool.slot), ("equipped", 2, 1))
        self.assertEqual([(row.source_slot, row.destination_slot) for row in result.transfer.steps], [(5, 14), (4, 15)])
        self.assertEqual(self.bot.inventory_slots[1].net_id_variant, 250)
        self.assertEqual(self.bot.inventory_slots[15].net_id_variant, 94)

    async def test_multiple_supply_keeps_acquired_spare_when_hotbar_swap_moves_it(self):
        b = self.b
        self.contents(0, 36, {slot: self.stack(GOLD, 20 + slot) for slot in range(9)})

        def server(packet):
            if isinstance(packet, b.ItemStackRequestPacket):
                request, = packet.requests
                action, = request.actions
                if isinstance(action, b.ItemStackRequestCereal.SwapActionData):
                    self.assertEqual((action.source.slot, action.destination.slot), (9, 0))
                    self.reply(request, (PLAYER, 9, 1, 251), (PLAYER, 0, 1, 250))
                    return
            self.backups_server(packet)
            if isinstance(packet, b.ItemStackRequestPacket) and len(self.actions()) == 1:
                self.slot(0, 0, self.stack())

        self.after_write = server
        result = await self.acquire(allowed=range(4, 6), destinations=range(0, 16), max_tools=2)
        self.assertEqual((result.status, result.transfer.confirmed_count, result.tool.slot), ("equipped", 2, 0))
        self.assertEqual([(row.source_slot, row.destination_slot) for row in result.transfer.steps], [(5, 9), (4, 0)])
        self.assertEqual(self.bot.inventory_slots[0].net_id_variant, 250)
        self.assertEqual(self.bot.inventory_slots[9].net_id_variant, 251)

    async def test_multiple_supply_validates_strictly_positive_integer_before_network(self):
        for size in (False, True, 0, -1, 1.5, "2"):
            for operation in (self.acquire, self.supply):
                with self.subTest(size=size, operation=operation), self.assertRaisesRegex(ValueError, "positive integer"):
                    await operation(max_tools=size)
        self.assertEqual(self.stream.sent, [])

    async def test_ordered_supply_waits_for_close_and_takes_only_authorized_tool(self):
        first_close = asyncio.Event()
        b = self.b
        chests = [HarvestToolChest(CHEST, range(4, 5)), HarvestToolChest(SECOND_CHEST, range(6, 7), range(3, 4))]

        def server(packet):
            if isinstance(packet, b.ContainerClosePacket) and self.bot.open_window.position == CHEST:
                first_close.set()
            elif isinstance(packet, b.ItemStackRequestPacket):
                request, = packet.requests
                action, = request.actions
                self.assertEqual((action.source.slot, action.source.net_id_variant, action.destination.slot, action.amount),
                                 (6, 16, 3, 1))
                self.reply(request, (STORAGE, 6, 0, None), (PLAYER, 3, 1, 90))
            else:
                self.supply_server(packet)

        self.after_write = server
        task = asyncio.create_task(self.supply(chests=chests))
        try:
            await asyncio.wait_for(first_close.wait(), 1)
            self.assertEqual(len(self.chest_clicks()), 1)
            chests.clear()
            self.close_storage()
            result = await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result, HarvestToolSupply)
        self.assertEqual((result.status, result.tool), ("equipped", HarvestTool(3, IRON_NAME, 6)))
        first, second = result.attempts
        self.assertEqual((first.status, first.window.position, first.transfer), ("tool_unavailable", CHEST, None))
        self.assertEqual((second.status, second.window.position, second.transfer.confirmed_count), ("equipped", SECOND_CHEST, 1))
        self.assertIsNone(self.bot.open_window)
        self.assertEqual([type(packet) for packet in self.stream.sent], [
            b.InventoryTransactionPacket, b.ContainerClosePacket, b.InventoryTransactionPacket,
            b.ItemStackRequestPacket, b.MobEquipmentPacket, b.ContainerClosePacket,
        ])
        with self.assertRaises(FrozenInstanceError):
            result.status = "unobserved"

    async def test_ordered_supply_borrows_the_active_first_chest_without_trying_second(self):
        borrowed = self.open_storage(stacks={4: self.stack(GOLD, 14)})
        result = await self.supply()
        attempted, = result.attempts
        self.assertEqual((result.status, attempted.status), ("tool_unavailable", "tool_unavailable"))
        self.assertIs(attempted.window, borrowed)
        self.assertIs(self.bot.open_window, borrowed)
        self.assertEqual(self.stream.sent, [])

    async def test_ordered_supply_does_not_open_second_when_close_is_unconfirmed(self):
        self.after_write = lambda packet: (self.supply_server(packet)
                                           if not isinstance(packet, self.b.ContainerClosePacket) else None)
        with self.assertRaises(TimeoutError) as caught:
            await self.supply(timeout=0.05)
        error = caught.exception
        attempt, = error.harvest_supply_progress.attempts
        self.assertIs(attempt, error.harvest_progress)
        self.assertEqual((error.harvest_supply_progress.status, attempt.status), ("interrupted", "tool_unavailable"))
        self.assertIs(attempt.window, self.bot.open_window)
        self.assertEqual([type(packet) for packet in self.stream.sent],
                         [self.b.InventoryTransactionPacket, self.b.ContainerClosePacket])

    async def test_ordered_supply_timeout_includes_lock_wait(self):
        async with self.bot._harvest_tools._equip_lock:
            with self.assertRaises(TimeoutError) as caught:
                await self.supply(timeout=0.01)
        self.assertEqual(caught.exception.harvest_supply_progress, HarvestToolSupply("interrupted"))
        self.assertEqual(self.stream.sent, [])

    async def test_ordered_supply_cancelled_while_closing_keeps_ownership_until_ack(self):
        closing = asyncio.Event()
        b = self.b

        def server(packet):
            if isinstance(packet, b.ContainerClosePacket):
                closing.set()
            else:
                self.supply_server(packet)

        self.after_write = server
        task = asyncio.create_task(self.supply())
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel("cancel after close submission")
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.bot._harvest_tools._equip_lock.locked())
        self.close_storage()
        with self.assertRaisesRegex(asyncio.CancelledError, "cancel after close submission") as caught:
            await task
        self.assertIsNone(self.bot.open_window)
        self.assertEqual(caught.exception.harvest_supply_progress.attempts, (caught.exception.harvest_progress,))
        self.assertEqual(len(self.chest_clicks()), 1)

    async def test_ordered_supply_repeated_cancellation_does_not_abandon_submitted_open(self):
        clicked = asyncio.Event()
        b = self.b

        def server(packet):
            if isinstance(packet, b.InventoryTransactionPacket):
                clicked.set()
            elif isinstance(packet, b.ContainerClosePacket):
                self.emit(packet)

        self.after_write = server
        task = asyncio.create_task(self.supply())
        await asyncio.wait_for(clicked.wait(), 1)
        task.cancel("first cancellation")
        await asyncio.sleep(0)
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.bot._harvest_tools._equip_lock.locked())
        owned = self.open_storage(stacks={4: self.stack(GOLD, 14)})
        with self.assertRaisesRegex(asyncio.CancelledError, "first cancellation") as caught:
            await task
        error = caught.exception
        self.assertIs(error.harvest_progress.window, owned)
        self.assertEqual(error.harvest_supply_progress.attempts, (error.harvest_progress,))
        self.assertIsNone(self.bot.open_window)
        self.assertEqual([type(packet) for packet in self.stream.sent], [b.InventoryTransactionPacket, b.ContainerClosePacket])

    async def test_ordered_supply_cancelled_take_preserves_pending_step_and_previous_miss(self):
        submitted = asyncio.Event()
        b = self.b

        def server(packet):
            if isinstance(packet, b.ItemStackRequestPacket):
                submitted.set()
            else:
                self.supply_server(packet)

        self.after_write = server
        task = asyncio.create_task(self.supply())
        await asyncio.wait_for(submitted.wait(), 1)
        task.cancel("cancel after take submission")
        with self.assertRaisesRegex(asyncio.CancelledError, "cancel after take submission") as caught:
            await task
        error = caught.exception
        first, second = error.harvest_supply_progress.attempts
        self.assertEqual(first.status, "tool_unavailable")
        self.assertIs(second, error.harvest_progress)
        self.assertIs(second.transfer, error.transfer_progress)
        step, = second.transfer.steps
        self.assertEqual((step.source_slot, step.destination_slot, step.request_id, step.confirmed), (6, 3, -1, None))
        self.assertIsNone(self.bot.open_window)
        self.assertEqual(self.equipment(), [])

    async def test_ordered_supply_stops_after_target_or_epoch_changes_at_close(self):
        for invalidate in (lambda: self.block(TARGET, DIRT),
                           lambda: self.bot._blocks._invalidate_mining("dimension_changed")):
            with self.subTest(invalidate=invalidate):
                self.block(TARGET, DIAMOND_ORE)
                self.stream.sent.clear()

                def server(packet):
                    self.supply_server(packet)
                    if isinstance(packet, self.b.ContainerClosePacket):
                        invalidate()

                self.after_write = server
                result = await self.supply()
                self.assertEqual(result.status, "unobserved")
                self.assertEqual([attempt.status for attempt in result.attempts], ["tool_unavailable"])
                self.assertEqual(len(self.chest_clicks()), 1)
                self.assertEqual(self.actions(), [])

    async def test_ordered_supply_inventory_only_and_existing_tool_need_no_supply_chest(self):
        self.assertEqual(await self.supply(chests=[]), HarvestToolSupply("tool_unavailable"))
        self.assertEqual(self.stream.sent, [])
        self.contents(0, 36, {7: self.stack(IRON, 11)})
        for chests in ([], None):
            with self.subTest(chests=chests):
                result = await self.supply(chests=chests)
                self.assertEqual(result, HarvestToolSupply("equipped", tool=HarvestTool(7, IRON_NAME, 6)))
        self.assertEqual(self.stream.sent, self.equipment())

    async def test_ordered_supply_owned_inventory_screen_is_closed_after_equipment_move(self):
        self.contents(0, 36, {0: self.stack(GOLD, 10), 14: self.stack(IRON, 11)})
        b = self.b

        def server(packet):
            if isinstance(packet, b.InteractPacket):
                self.emit(b.ContainerOpenPacket(container_id=2, type=b.ContainerType.INVENTORY,
                                                pos=b.BlockPos(x=0, y=0, z=0), entity_unique_id=-1))
            elif isinstance(packet, b.ItemStackRequestPacket):
                self.reply(packet.requests[0], (PLAYER, 14, 0, None), (PLAYER, 1, 1, 90))
            elif isinstance(packet, b.ContainerClosePacket):
                self.emit(packet)

        self.after_write = server
        self.assertEqual(await self.supply(), HarvestToolSupply("equipped", tool=HarvestTool(1, IRON_NAME, 6)))
        self.assertIsNone(self.bot.open_window)
        self.assertEqual([type(packet) for packet in self.stream.sent], [
            b.InteractPacket, b.ItemStackRequestPacket, b.ContainerClosePacket, b.MobEquipmentPacket,
        ])

    async def test_ordered_supply_stops_after_rejected_or_partial_take(self):
        b = self.b
        for verdict, status, stop_reason, corrections in (
            ("ERROR", "rejected", "rejected", ()),
            ("SUCCESS", "unobserved", "unconfirmed", ((STORAGE, 4, 0, None),)),
        ):
            with self.subTest(verdict=verdict):
                self.stream.sent.clear()

                def server(packet):
                    if isinstance(packet, b.InventoryTransactionPacket):
                        self.open_storage(stacks={4: self.stack(IRON, 14)})
                    elif isinstance(packet, b.ItemStackRequestPacket):
                        self.reply(packet.requests[0], *corrections, verdict=verdict)
                    else:
                        self.supply_server(packet)

                self.after_write = server
                result = await self.supply()
                attempt, = result.attempts
                self.assertEqual((result.status, attempt.transfer.stop_reason), (status, stop_reason))
                self.assertEqual(attempt.transfer.steps[0].verdict, verdict)
                self.assertEqual(len(self.chest_clicks()), 1)
                self.assertEqual(self.equipment(), [])
                self.assertIsNone(self.bot.open_window)

    async def test_ordered_supply_validates_all_chests_before_sending(self):
        for chests in ("chests", iter(()), [HarvestToolChest(CHEST, range(27)), object()]):
            with self.subTest(chests=chests), self.assertRaises(TypeError):
                await self.supply(chests=chests)
        self.assertEqual(self.stream.sent, [])

    async def test_takes_one_best_eligible_stack_and_equips_its_receipt(self):
        b = self.b
        source_stacks = {
            1: self.stack(IRON, 11, efficiency=5),
            36: self.stack(GOLD, 36, efficiency=5),
            38: self.stack(IRON, 38),
            40: self.stack(IRON, 40, efficiency=2),
            41: self.stack(IRON, 41, efficiency=2),
        }

        def server(packet):
            if isinstance(packet, b.InventoryTransactionPacket):
                interaction = packet.variant_transaction
                self.assertEqual((interaction.pos.x, interaction.pos.y, interaction.pos.z), CHEST)
                self.assertEqual(interaction.face, 2)
                self.open_storage(stacks=source_stacks, width=54)
            elif isinstance(packet, b.ItemStackRequestPacket):
                request, = packet.requests
                action, = request.actions
                if isinstance(action, b.ItemStackRequestCereal.TakeActionData):
                    self.assertEqual((action.source.slot, action.destination.slot, action.amount), (40, 14, 1))
                    self.assertEqual(action.source.net_id_variant, 40)
                    self.reply(request, (STORAGE, 40, 0, None), (PLAYER, 14, 1, 70))
                else:
                    self.assertIsInstance(action, b.ItemStackRequestCereal.PlaceActionData)
                    self.assertEqual((action.source.slot, action.destination.slot, action.amount), (14, 1, 1))
                    self.assertEqual(action.source.net_id_variant, 70)
                    self.reply(request, (PLAYER, 14, 0, None), (PLAYER, 1, 1, 91))
            elif isinstance(packet, b.ContainerClosePacket):
                self.assertEqual(self.bot.container_slots[40].id, 0)
                for slot in (1, 36, 38, 41):
                    self.assertEqual(self.bot.container_slots[slot].net_id_variant,
                                     source_stacks[slot].net_id_variant)
                self.emit(packet)

        self.after_write = server
        result = await self.acquire(allowed=range(35, 42))
        self.assertEqual(result.status, "equipped")
        self.assertEqual(result.tool, HarvestTool(1, IRON_NAME, 6))
        self.assertEqual(result.transfer.confirmed_count, 1)
        self.assertEqual(len(self.actions()), 2)
        self.assertEqual(result.window.position, CHEST)
        self.assertIsNone(self.bot.open_window)
        equipment, = self.equipment()
        self.assertEqual(equipment.item.net_id_variant, 91)
        self.assertEqual(equipment.item.user_data_buffer, enchantment_data(b, 2))
        self.assertEqual(self.bot.inventory_slots[0].id, GOLD)
        with self.assertRaises(FrozenInstanceError):
            result.status = "unobserved"

    async def test_uses_owned_tool_outside_destination_range_without_chest(self):
        self.contents(0, 2, {0: self.stack(GOLD, 10), 1: self.stack(IRON, 11)})
        result = await self.acquire()
        self.assertEqual((result.status, result.tool), ("equipped", HarvestTool(1, IRON_NAME, 6)))
        self.assertIsNone(result.window)
        self.assertIsNone(result.transfer)
        self.assertEqual(self.stream.sent, self.equipment())

    async def test_uses_bare_hands_without_chest(self):
        self.block(TARGET, DIRT)
        result = await self.acquire()
        self.assertEqual((result.status, result.tool), ("equipped", HarvestTool(1, None, 1)))
        self.assertIsNone(result.transfer)
        equipment, = self.stream.sent
        self.assertEqual(equipment.item.id, 0)

    async def test_owned_tool_with_no_observed_hotbar_never_opens_chest(self):
        self.contents(0, 0, {})
        self.slot(0, 20, self.stack(IRON, 11))
        result = await self.acquire()
        self.assertEqual(result.status, "equip_unavailable")
        self.assertEqual(self.stream.sent, [])

    async def test_gold_is_ineligible_and_a_borrowed_chest_remains_open(self):
        borrowed = self.open_storage(stacks={4: self.stack(GOLD, 21, efficiency=5), 5: self.stack(IRON, 22)})
        result = await self.acquire(allowed=range(4, 5))
        self.assertEqual(result.status, "tool_unavailable")
        self.assertIs(result.window, borrowed)
        self.assertIs(self.bot.open_window, borrowed)
        self.assertIsNone(result.transfer)
        self.assertEqual(self.stream.sent, [])

    async def test_another_open_window_is_busy(self):
        other = self.open_storage(position=(0, 100, 1), stacks={4: self.stack(IRON, 14)})
        self.assertEqual((await self.acquire()).status, "busy")
        self.assertIs(self.bot.open_window, other)
        self.assertEqual(self.stream.sent, [])

    async def test_pending_public_open_keeps_ownership_and_is_not_clicked_twice(self):
        b = self.b
        clicked = asyncio.Event()

        def server(packet):
            if isinstance(packet, b.InventoryTransactionPacket):
                clicked.set()

        self.after_write = server
        public_open = asyncio.create_task(self.bot.open_container(CHEST, face=2, timeout=1))
        try:
            await asyncio.wait_for(clicked.wait(), 1)
            pending = await self.acquire(allowed=range(4, 5))
            self.assertEqual(pending.status, "window_busy")
            self.assertIsNone(pending.window)
            active = self.open_storage(stacks={4: self.stack(GOLD, 14)})
            self.assertEqual(await public_open, active)
            borrowed = await self.acquire(allowed=range(4, 5))
            self.assertEqual(borrowed.status, "tool_unavailable")
            self.assertIs(borrowed.window, active)
            self.assertIs(self.bot.open_window, active)
            self.assertEqual([type(packet) for packet in self.stream.sent], [b.InventoryTransactionPacket])
        finally:
            public_open.cancel()
            await asyncio.gather(public_open, return_exceptions=True)

    def announce_cold_chest(self):
        b = self.b
        runtime_id = self.bot.observed_block(CHEST).runtime_id
        self.bot._blocks._updates.pop((*CHEST, 0))
        self.emit(b.LevelChunkPacket(
            pos=b.ChunkPos(x=0, z=0), dimension_id=0, cache_enabled=False,
            sub_chunks_count=0, client_request_sub_chunk_limit=-1,
        ))
        return runtime_id

    def supply_section(self, raw):
        b = self.b
        sub = b.SubChunkPacket
        self.emit(sub(center_pos=b.SubChunkPos(x=0, y=0, z=0), sub_chunk_data=[
            sub.SubChunkPacketData(sub_chunk_pos_offset=sub.SubChunkPosOffset(x=0, y=6, z=0),
                                   result=sub.SubChunkRequestResult.SUCCESS,
                                   serialized_sub_chunk=raw),
        ]))

    async def test_public_open_requests_cold_section_before_its_only_click(self):
        b = self.b
        runtime_id = self.announce_cold_chest()
        requested = asyncio.Event()

        def server(packet):
            if isinstance(packet, b.SubChunkRequestPacket):
                requested.set()
            elif isinstance(packet, b.InventoryTransactionPacket):
                self.assertEqual(packet.variant_transaction.target_block_id, runtime_id)
                self.open_storage()
        self.after_write = server
        opened = asyncio.create_task(self.bot.open_container(CHEST, face=2, timeout=1))
        try:
            await asyncio.wait_for(requested.wait(), 0.3)
            with self.assertRaisesRegex(_InventoryMoveNotSubmitted, "window_busy"):
                await self.bot._containers.open(CHEST, self.bot._blocks, _on_observed=lambda _: None)
            packet, = self.stream.sent
            offset, = packet.sub_chunk_pos_offsets
            self.assertEqual((offset.x, offset.y, offset.z), (0, 6, 0))
            self.supply_section(solid_sub_chunk(runtime_id))
            self.assertEqual((await opened).position, CHEST)
            self.assertEqual([type(packet) for packet in self.stream.sent], [
                b.SubChunkRequestPacket, b.InventoryTransactionPacket,
            ])
        finally:
            opened.cancel()
            await asyncio.gather(opened, return_exceptions=True)

    async def test_public_open_times_out_before_click_if_terrain_remains_unknown(self):
        self.announce_cold_chest()
        for _ in range(2):
            with self.assertRaisesRegex(TimeoutError, "terrain"):
                await self.bot.open_container(CHEST, timeout=0.15)
        with self.assertRaisesRegex(TimeoutError, "terrain"):
            await self.bot.open_container(CHEST, timeout=0)
        self.assertEqual([type(packet) for packet in self.stream.sent], [self.b.SubChunkRequestPacket] * 2)

    async def test_public_open_does_not_click_if_section_cannot_describe_the_target(self):
        self.announce_cold_chest()
        self.after_write = lambda _: self.supply_section(bytes((9, 0, 6)))
        with self.assertRaisesRegex(TimeoutError, "terrain"):
            await self.bot.open_container(CHEST, timeout=0.05)
        self.assertEqual([type(packet) for packet in self.stream.sent], [self.b.SubChunkRequestPacket])

    async def test_public_open_does_not_click_after_its_column_unloads(self):
        b = self.b
        runtime_id = self.announce_cold_chest()
        self.after_write = lambda _: self.supply_section(solid_sub_chunk(runtime_id))
        wait = self.stream.wait_for_packet

        async def unload_while_registering(packet_id, **kwargs):
            if packet_id == b.ContainerOpenPacket.ID:
                self.emit(b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=48, y=0, z=0), radius=16))
            return await wait(packet_id, **kwargs)
        self.stream.wait_for_packet = unload_while_registering
        with self.assertRaisesRegex(RuntimeError, "unloaded"):
            await self.bot.open_container(CHEST, timeout=1)
        self.assertEqual([type(packet) for packet in self.stream.sent], [b.SubChunkRequestPacket])

    async def test_public_open_never_clicks_a_new_dimension_after_position_sync(self):
        b = self.b
        movement = self.bot._movement
        movement._updated.clear()
        waiting = asyncio.Event()
        wait = self.stream.wait_for_packet

        async def observe_wait(packet_ids, **kwargs):
            waiting.set()
            return await wait(packet_ids, **kwargs)
        self.stream.wait_for_packet = observe_wait
        self.after_write = lambda _: self.open_storage()
        opening = asyncio.create_task(self.bot.open_container(CHEST, timeout=1))
        await asyncio.wait_for(waiting.wait(), 0.5)
        self.assertEqual(self.stream.sent, [])
        self.emit(b.ChangeDimensionPacket(dimension_id=1, pos=b.Vec3(x=1, y=101.62, z=0.5),
                                          loading_screen_id=1))
        self.block(CHEST, 3463497305)
        with self.assertRaises(ColumnCoverageInvalidated) as error:
            await opening
        self.assertEqual(error.exception.reason, "dimension_changed")
        self.assertEqual(self.stream.sent, [])

    async def test_public_open_refreshes_unknown_hash_without_sending_it(self):
        b = self.b
        runtime_id = self.announce_cold_chest()
        self.block(CHEST, HASHED_UNKNOWN_RUNTIME_ID)

        def server(packet):
            if isinstance(packet, b.SubChunkRequestPacket):
                self.supply_section(solid_sub_chunk(runtime_id))
            else:
                self.assertEqual(packet.variant_transaction.target_block_id, runtime_id)
                self.open_storage()
        self.after_write = server
        self.assertEqual((await self.bot.open_container(CHEST, timeout=1)).position, CHEST)
        self.assertEqual([type(packet) for packet in self.stream.sent], [
            b.SubChunkRequestPacket, b.InventoryTransactionPacket,
        ])

    async def test_public_open_does_not_complete_on_old_empty_coverage(self):
        b = self.b
        runtime_id = self.announce_cold_chest()
        self.supply_section(bytes((9, 0, 6)))
        self.assertEqual(self.bot.column_coverage((0, 0)).received_section_ys, (6,))
        requested = asyncio.Event()

        def server(packet):
            if isinstance(packet, b.SubChunkRequestPacket):
                requested.set()
            else:
                self.assertEqual(packet.variant_transaction.target_block_id, runtime_id)
                self.open_storage()
        self.after_write = server
        opening = asyncio.create_task(self.bot.open_container(CHEST, timeout=1))
        try:
            await asyncio.wait_for(requested.wait(), 0.5)
            # Give the old coverage-based path time to register its open waiter.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(opening.done())
            packet, = self.stream.sent
            self.assertIsInstance(packet, b.SubChunkRequestPacket)
            self.supply_section(solid_sub_chunk(runtime_id))
            self.assertEqual((await opening).position, CHEST)
            self.assertEqual([type(packet) for packet in self.stream.sent], [
                b.SubChunkRequestPacket, b.InventoryTransactionPacket,
            ])
        finally:
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)

    async def test_public_open_cancellation_does_not_click_or_reserve_the_next_open(self):
        b = self.b
        runtime_id = self.announce_cold_chest()
        requested = asyncio.Event()
        self.after_write = lambda _: requested.set()
        opening = asyncio.create_task(self.bot.open_container(CHEST, timeout=1))
        await asyncio.wait_for(requested.wait(), 0.5)
        opening.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await opening
        self.supply_section(solid_sub_chunk(runtime_id))
        self.assertEqual([type(packet) for packet in self.stream.sent], [b.SubChunkRequestPacket])
        self.after_write = lambda _: self.open_storage()
        self.assertEqual((await self.bot.open_container(CHEST)).position, CHEST)
        self.assertEqual([type(packet) for packet in self.stream.sent], [
            b.SubChunkRequestPacket, b.InventoryTransactionPacket,
        ])

    async def test_unknown_rule_sparse_inventory_and_unknown_item_leave_shortage_unobserved(self):
        self.block(TARGET, 0x12345678)
        self.assertEqual((await self.acquire()).status, "unobserved")
        self.block(TARGET, DIAMOND_ORE)
        self.contents(0, 35, {0: self.stack(GOLD, 10)})
        self.assertEqual((await self.acquire()).status, "unobserved")
        self.slot(0, 35, self.stack(3333, 20))
        self.assertEqual((await self.acquire()).status, "unobserved")
        self.assertEqual(self.stream.sent, [])

    async def test_pending_request_into_any_main_inventory_slot_obscures_shortage(self):
        self.open_storage(position=(0, 100, 1), stacks={4: self.stack(IRON, 14)})
        started = asyncio.Event()
        self.after_write = lambda packet: started.set() if isinstance(packet, self.b.ItemStackRequestPacket) else None
        taking = asyncio.create_task(self.bot.take_from_container(4, 31))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.close_storage()
            result = await self.acquire()
            self.assertEqual(result.status, "unobserved")
            self.assertEqual(len(self.stream.sent), 1)
        finally:
            taking.cancel()
            await asyncio.gather(taking, return_exceptions=True)

    async def test_only_pending_requests_touching_the_selected_sparse_tool_block_equipping(self):
        b = self.b
        original = {0: self.stack(GOLD, 10), 1: self.stack(IRON, 11), 2: self.stack(PAPER, 12)}
        submitted = asyncio.Queue()

        def server(packet):
            if isinstance(packet, b.ItemStackRequestPacket):
                submitted.put_nowait(packet.requests[0])

        self.after_write = server
        for pending_slot, expected_status in ((2, "equipped"), (1, "unobserved")):
            with self.subTest(pending_slot=pending_slot):
                self.contents(0, 3, original)
                caller_window = self.open_storage(stacks={})
                putting = asyncio.create_task(self.bot.put_into_container(pending_slot, 4, timeout=1))
                try:
                    request = await asyncio.wait_for(submitted.get(), 1)
                    before = len(self.equipment())
                    result = await self.acquire()
                    self.assertEqual(result.status, expected_status)
                    if pending_slot == 2:
                        self.assertEqual(result.tool, HarvestTool(1, IRON_NAME, 6))
                        self.assertEqual(len(self.equipment()), before + 1)
                    else:
                        self.assertIsNone(result.tool)
                        self.assertEqual(len(self.equipment()), before)
                    self.assertIsNone(result.window)
                    self.assertIsNone(result.transfer)
                    self.assertIs(self.bot.open_window, caller_window)
                    self.reply(request, (PLAYER, pending_slot, 0, None), (STORAGE, 4, 1, 20))
                    self.assertEqual((await putting).result.name, "SUCCESS")
                finally:
                    putting.cancel()
                    await asyncio.gather(putting, return_exceptions=True)
        self.assertEqual([type(packet) for packet in self.stream.sent],
                         [b.ItemStackRequestPacket, b.MobEquipmentPacket, b.ItemStackRequestPacket])

    async def test_inventory_tool_arrives_after_planning_and_preempts_the_take(self):
        window = self.open_storage(stacks={4: self.stack(IRON, 14, efficiency=2)})

        def before_send(packet):
            if isinstance(packet, self.b.ItemStackRequestPacket):
                self.slot(0, 6, self.stack(IRON, 26))

        self.before_write = before_send
        result = await self.acquire()
        self.assertEqual((result.status, result.tool), ("equipped", HarvestTool(6, IRON_NAME, 6)))
        self.assertIs(result.window, window)
        self.assertIsNone(result.transfer)
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.bot.container_slots[4].net_id_variant, 14)

    async def test_replaced_selected_stack_is_not_redirected_to_another_tool(self):
        for change in ("new_packet", "in_place"):
            with self.subTest(change=change):
                self.open_storage(stacks={4: self.stack(IRON, 14, efficiency=2), 5: self.stack(IRON, 15)})

                def before_send(packet):
                    if isinstance(packet, self.b.ItemStackRequestPacket):
                        if change == "new_packet":
                            self.slot(2, 4, self.stack(IRON, 14, efficiency=1))
                        else:
                            self.bot.container_slots[4].user_data_buffer = enchantment_data(self.b, 1)

                self.before_write = before_send
                result = await self.acquire()
                self.assertEqual(result.status, "unobserved")
                self.assertEqual(result.transfer.steps, ())
                self.assertEqual(self.stream.sent, [])

    async def test_source_identity_after_write_remains_the_snapshot_at_submission(self):
        self.open_storage(stacks={4: self.stack(IRON, 14, efficiency=2)})

        def server(packet):
            self.bot.container_slots[4].user_data_buffer = enchantment_data(self.b, 1)
            self.reply(packet.requests[0], (STORAGE, 4, 0, None), (PLAYER, 1, 1, 90))

        self.after_write = server
        result = await self.acquire(destinations=range(1, 2))
        self.assertEqual(result.status, "equip_unavailable")
        self.assertEqual(result.transfer.confirmed_count, 1)
        self.assertEqual(self.equipment(), [])

    async def test_target_changed_while_waiting_for_contents_does_not_take_or_equip(self):
        b = self.b

        def server(packet):
            if isinstance(packet, b.InventoryTransactionPacket):
                self.open_storage(stacks={4: self.stack(IRON, 14)})
                self.block(TARGET, DIRT)
            elif isinstance(packet, b.ContainerClosePacket):
                self.emit(packet)

        self.after_write = server
        result = await self.acquire()
        self.assertEqual(result.status, "unobserved")
        self.assertEqual([type(packet) for packet in self.stream.sent],
                         [b.InventoryTransactionPacket, b.ContainerClosePacket])

    async def test_overlapping_acquisitions_share_the_equip_lock(self):
        b = self.b
        submitted = asyncio.Queue()

        def server(packet):
            if isinstance(packet, b.InventoryTransactionPacket):
                self.open_storage(stacks={4: self.stack(IRON, 14)})
            elif isinstance(packet, b.ItemStackRequestPacket):
                submitted.put_nowait(packet.requests[0])
            elif isinstance(packet, b.ContainerClosePacket):
                self.emit(packet)

        self.after_write = server
        first = asyncio.create_task(self.acquire(destinations=range(1, 2)))
        second = None
        try:
            request = await asyncio.wait_for(submitted.get(), 1)
            second = asyncio.create_task(self.acquire(destinations=range(1, 2)))
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            self.reply(request, (STORAGE, 4, 0, None), (PLAYER, 1, 1, 90))
            filled, owned = await asyncio.wait_for(asyncio.gather(first, second), 1)
            self.assertEqual((filled.status, owned.status), ("equipped", "equipped"))
            self.assertEqual(filled.transfer.confirmed_count, 1)
            self.assertIsNone(owned.transfer)
            self.assertIsNone(owned.window)
            self.assertEqual(len(self.actions()), 1)
            self.assertEqual(len(self.equipment()), 2)
        finally:
            tasks = (first,) if second is None else (first, second)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_partial_server_receipt_keeps_the_attempt_and_skips_equipment(self):
        self.open_storage(stacks={4: self.stack(IRON, 14)})

        def server(packet):
            self.reply(packet.requests[0], (STORAGE, 4, 0, None))

        self.after_write = server
        result = await self.acquire()
        self.assertEqual(result.status, "unobserved")
        self.assertEqual(result.transfer.stop_reason, "unconfirmed")
        attempted, = result.transfer.steps
        self.assertEqual((attempted.request_id, attempted.verdict, attempted.confirmed), (-1, "SUCCESS", None))
        self.assertEqual(self.equipment(), [])
        self.assertIsNone(result.tool)
        self.assertEqual((await self.acquire()).status, "unobserved")

    async def test_confirmed_transfer_is_kept_when_its_destination_is_replaced(self):
        self.open_storage(stacks={4: self.stack(IRON, 14, efficiency=2)})

        def server(packet):
            self.reply(packet.requests[0], (STORAGE, 4, 0, None), (PLAYER, 1, 1, 90))
            self.slot(0, 1, self.stack(IRON, 91, efficiency=2))
            self.slot(0, 2, self.stack(IRON, 92, efficiency=5))

        self.after_write = server
        result = await self.acquire(destinations=range(1, 2))
        self.assertEqual(result.status, "equip_unavailable")
        self.assertEqual(result.transfer.confirmed_count, 1)
        self.assertEqual(self.equipment(), [])
        self.assertIsNone(result.tool)

    async def test_successful_hotbar_move_without_its_receipt_cannot_equip_another_stack(self):
        self.open_storage(stacks={4: self.stack(IRON, 14, efficiency=2)})

        def server(packet):
            request, = packet.requests
            action, = request.actions
            if isinstance(action, self.b.ItemStackRequestCereal.TakeActionData):
                self.reply(request, (STORAGE, 4, 0, None), (PLAYER, 14, 1, 90))
            else:
                self.slot(0, 1, self.stack(IRON, 91, efficiency=2))
                self.reply(request, (PLAYER, 14, 0, None))

        self.after_write = server
        result = await self.acquire()
        self.assertEqual(result.status, "equip_unavailable")
        self.assertEqual(result.transfer.confirmed_count, 1)
        self.assertEqual(len(self.actions()), 2)
        self.assertEqual(self.equipment(), [])

    async def test_reopened_same_id_and_position_is_neither_taken_from_nor_closed(self):
        initial = replacement = None

        def server(packet):
            nonlocal initial, replacement
            if isinstance(packet, self.b.InventoryTransactionPacket):
                initial = self.open_storage(stacks={4: self.stack(IRON, 14)})
                self.close_storage()
                replacement = self.open_storage(stacks={4: self.stack(IRON, 15)})

        self.after_write = server
        result = await self.acquire()
        self.assertEqual(result.status, "window_changed")
        self.assertIs(result.window, initial)
        self.assertEqual(initial, replacement)
        self.assertIsNot(initial, replacement)
        self.assertIs(self.bot.open_window, replacement)
        self.assertEqual([type(packet) for packet in self.stream.sent], [self.b.InventoryTransactionPacket])

    async def test_window_change_during_take_retains_the_confirmed_receipt(self):
        original = self.open_storage(stacks={4: self.stack(IRON, 14)})

        def server(packet):
            self.close_storage()
            self.open_storage(stacks={4: self.stack(IRON, 15)})
            self.reply(packet.requests[0], (STORAGE, 4, 0, None), (PLAYER, 1, 1, 90))

        self.after_write = server
        result = await self.acquire(destinations=range(1, 2))
        self.assertEqual(result.status, "window_changed")
        self.assertIs(result.window, original)
        self.assertEqual(result.transfer.confirmed_count, 1)
        self.assertIsNone(result.tool)
        self.assertEqual(self.bot.container_slots[4].net_id_variant, 15)
        self.assertEqual(self.equipment(), [])

    async def test_same_id_window_replacement_before_hotbar_receipt_prevents_equipment(self):
        b = self.b
        initial = replacement = None

        def server(packet):
            nonlocal initial, replacement
            if isinstance(packet, b.InventoryTransactionPacket):
                initial = self.open_storage(stacks={4: self.stack(IRON, 14)})
            elif isinstance(packet, b.ItemStackRequestPacket):
                request, = packet.requests
                action, = request.actions
                if isinstance(action, b.ItemStackRequestCereal.TakeActionData):
                    self.reply(request, (STORAGE, 4, 0, None), (PLAYER, 14, 1, 90))
                else:
                    self.close_storage()
                    replacement = self.open_storage(stacks={4: self.stack(IRON, 15)})
                    self.reply(request, (PLAYER, 14, 0, None), (PLAYER, 1, 1, 91))

        self.after_write = server
        result = await self.acquire()
        self.assertEqual(result.status, "window_changed")
        self.assertEqual(result.transfer.confirmed_count, 1)
        self.assertIsNone(result.tool)
        self.assertEqual(initial, replacement)
        self.assertIsNot(initial, replacement)
        self.assertIs(result.window, initial)
        self.assertIs(self.bot.open_window, replacement)
        self.assertEqual([type(packet) for packet in self.stream.sent],
                         [b.InventoryTransactionPacket, b.ItemStackRequestPacket, b.ItemStackRequestPacket])

    async def test_close_error_preserves_the_original_exception_and_completed_tool(self):
        b = self.b
        failure = ConnectionError("close failed")

        def server(packet):
            if isinstance(packet, b.InventoryTransactionPacket):
                self.open_storage(stacks={4: self.stack(IRON, 14)})
            elif isinstance(packet, b.ItemStackRequestPacket):
                self.reply(packet.requests[0], (STORAGE, 4, 0, None), (PLAYER, 1, 1, 90))
            elif isinstance(packet, b.ContainerClosePacket):
                raise failure

        self.after_write = server
        with self.assertRaises(ConnectionError) as caught:
            await self.acquire(destinations=range(1, 2))
        self.assertIs(caught.exception, failure)
        result = failure.harvest_progress
        self.assertEqual((result.status, result.tool), ("equipped", HarvestTool(1, IRON_NAME, 6)))
        self.assertEqual(result.transfer.confirmed_count, 1)
        self.assertIs(result.window, self.bot.open_window)

    async def test_replacement_during_close_registration_preserves_completed_tool(self):
        b = self.b
        original_wait = self.stream.wait_for_packet
        replacement = None

        async def wait_for_packet(packet_ids, **kwargs):
            nonlocal replacement
            if b.ContainerClosePacket.ID in (packet_ids if isinstance(packet_ids, tuple) else (packet_ids,)):
                replacement = self.open_storage(stacks={4: self.stack(IRON, 15)})
            return await original_wait(packet_ids, **kwargs)

        def server(packet):
            if isinstance(packet, b.InventoryTransactionPacket):
                self.open_storage(stacks={4: self.stack(IRON, 14)})
            elif isinstance(packet, b.ItemStackRequestPacket):
                self.reply(packet.requests[0], (STORAGE, 4, 0, None), (PLAYER, 1, 1, 90))

        self.stream.wait_for_packet = wait_for_packet
        self.after_write = server
        result = await self.acquire(destinations=range(1, 2))
        self.assertEqual((result.status, result.tool), ("window_changed", HarvestTool(1, IRON_NAME, 6)))
        self.assertEqual(result.transfer.confirmed_count, 1)
        self.assertEqual(result.window, replacement)
        self.assertIsNot(result.window, replacement)
        self.assertIs(self.bot.open_window, replacement)
        self.assertEqual([type(packet) for packet in self.stream.sent],
                         [b.InventoryTransactionPacket, b.ItemStackRequestPacket, b.MobEquipmentPacket])

    async def test_cancelled_owned_open_settles_and_closes_only_its_window(self):
        b = self.b
        clicked = asyncio.Event()

        def server(packet):
            if isinstance(packet, b.InventoryTransactionPacket):
                clicked.set()
            elif isinstance(packet, b.ContainerClosePacket):
                self.emit(packet)

        self.after_write = server
        acquisition = asyncio.create_task(self.acquire())
        await asyncio.wait_for(clicked.wait(), 1)
        acquisition.cancel("caller cancelled refill")
        await asyncio.sleep(0)
        owned = self.open_storage(stacks={4: self.stack(IRON, 14)})
        with self.assertRaisesRegex(asyncio.CancelledError, "caller cancelled refill") as caught:
            await acquisition
        self.assertIs(caught.exception.harvest_progress.window, owned)
        self.assertIsNone(caught.exception.harvest_progress.transfer)
        self.assertIsNone(self.bot.open_window)
        self.assertEqual([type(packet) for packet in self.stream.sent],
                         [b.InventoryTransactionPacket, b.ContainerClosePacket])

    async def test_disconnect_after_the_open_packet_keeps_the_window_as_progress(self):
        b = self.b
        opened = None

        def server(packet):
            nonlocal opened
            if isinstance(packet, b.InventoryTransactionPacket):
                opened = self.open_storage()
                self.emit(b.DisconnectPacket())

        self.after_write = server
        with self.assertRaises(ConnectionError) as caught:
            await self.acquire()
        self.assertIs(caught.exception.harvest_progress.window, opened)
        self.assertIsNone(caught.exception.harvest_progress.transfer)
        self.assertEqual([type(packet) for packet in self.stream.sent], [b.InventoryTransactionPacket])

    async def test_timed_out_take_keeps_window_and_unknown_transfer_progress(self):
        window = self.open_storage(stacks={4: self.stack(IRON, 14)})
        with self.assertRaises(TimeoutError) as caught:
            await self.acquire(timeout=0.05)
        result = caught.exception.harvest_progress
        self.assertIs(result.window, window)
        self.assertIs(result.transfer, caught.exception.transfer_progress)
        attempted, = result.transfer.steps
        self.assertEqual((attempted.source_slot, attempted.destination_slot, attempted.confirmed), (4, 14, None))
        self.assertIs(self.bot.open_window, window)
        self.assertEqual(self.equipment(), [])
