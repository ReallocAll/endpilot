import asyncio
from contextlib import ExitStack
from copy import copy
import unittest
from unittest.mock import AsyncMock, Mock, patch

from endpilot import (
    Bot, HeldItemStack, ItemRegistrySnapshot, RangeTransferResult, RangeTransferStep, transfer_item, transfer_range,
)
from endpilot.block_data import _catalog
from endpilot.login import JoinSession

from test_blocks import PacketStream
from test_container_take import item


CHEST = "LEVEL_ENTITY_CONTAINER"
BARREL = "BARREL_CONTAINER"
PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"
PAPER = 1230
SNOWBALL = 1231
CUSTOM = 1232
IRON_PICKAXE = 1233
DIAMOND = 1234
STICK = 1235
POSITION = (1, 100, 0)
POWERED_HOPPER = 1981605110
UNPOWERED_HOPPER = 3036911681
UTILITY_LAYOUTS = (
    ("DISPENSER", "minecraft:dispenser", None, 9),
    ("DROPPER", "minecraft:dropper", None, 9),
    ("HOPPER", "minecraft:hopper", POWERED_HOPPER, 5),
)


class RangePacketStream(PacketStream):
    async def send(self, packet, *, _before_send=None):
        if _before_send is not None:
            _before_send()
        await super().send(packet)


class RangeTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("RangeTransfer", protocol=2225)
        self.b = self.bot.protocol.binding
        self.stream = RangePacketStream(self.bot.protocol)
        self.stream.close = AsyncMock()
        self.stream.subscribe_reader_end = lambda callback: lambda: None
        session = JoinSession(self.stream, self.bot.identity, "127.0.0.1")
        session.player_id = session.player_unique_id = 1
        session.block_network_ids_are_hashes = True
        session.item_names = {
            PAPER: "minecraft:paper", SNOWBALL: "minecraft:snowball", CUSTOM: "range_test:custom_item",
            IRON_PICKAXE: "minecraft:iron_pickaxe", DIAMOND: "minecraft:diamond", STICK: "minecraft:stick",
        }
        session.item_registry = ItemRegistrySnapshot(session.item_names)
        with patch.object(JoinSession, "connect_raknet", return_value=session):
            await self.bot.join_raknet("127.0.0.1")
        self.addAsyncCleanup(self.bot.close)
        self.observe_block("minecraft:chest")

    def observe_block(self, name, position=POSITION, *, runtime_id=None, layer=0):
        palette, by_hash = _catalog()
        if runtime_id is None:
            runtime_id = next(key for key, index in by_hash.items()
                              if palette[index] is not None and palette[index].name == name)
        x, y, z = position
        self.emit(self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=layer))

    def emit(self, packet):
        self.stream.emit(self.bot.protocol.encode(packet))

    def stack(self, item_id=0, amount=0, net_id=None, *, user_data=b""):
        descriptor = item(self.b, item_id, amount, net_id)
        descriptor.user_data_buffer = user_data
        return descriptor

    def open_chest(self, *slots, position=POSITION, entity_unique_id=-1, width=27):
        self.open_window("CONTAINER", *slots, position=position, entity_unique_id=entity_unique_id, width=width)

    def open_window(self, window_type, *slots, width, position=POSITION, entity_unique_id=-1):
        b = self.b
        x, y, z = position
        self.emit(b.ContainerOpenPacket(container_id=2, type=b.ContainerType[window_type],
                                        pos=b.BlockPos(x=x, y=y, z=z), entity_unique_id=entity_unique_id))
        if width is not None:
            # Stock utility windows use the default full name (0) on content packets.
            name = CHEST if window_type == "CONTAINER" else "ANVIL_INPUT_CONTAINER"
            self.content(*slots, width=width, name=name)

    def content(self, *slots, width, inventory_id=2, name=CHEST):
        b = self.b
        self.assertLessEqual(len(slots), width)
        self.emit(b.InventoryContentPacket(inventory_id=inventory_id,
                                           slots=(*slots, *(self.stack() for _ in range(width - len(slots)))),
                                           full_container_name=b.FullContainerName(name=b.ContainerEnumName[name]),
                                           storage_item=self.stack()))

    def set_slot(self, inventory, index, stack):
        b = self.b
        self.emit(b.InventorySlotPacket(inventory_id=inventory, slot=index, item=stack,
                                        full_container_name=None, storage_item=None))

    def player_slot(self, index, stack):
        self.set_slot(self.b.ContainerID.INVENTORY, index, stack)

    def reply(self, request, corrections=(), *, verdict="SUCCESS"):
        b = self.b
        containers = [b.ItemStackResponseContainerInfo(
            full_container_name=b.FullContainerName(name=b.ContainerEnumName[side], dynamic_id=None),
            slots=[b.ItemStackResponseSlotInfo(
                requested_slot=slot, slot=slot, amount=amount,
                item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                custom_name=b.RedactableString(unredacted_string="", redacted_string=None), durability_correction=0,
            )],
        ) for side, slot, amount, net_id in corrections]
        self.emit(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult[verdict],
            containers=containers or None,
        )]))

    def outgoing(self):
        return [packet.requests[0] for packet in self.stream.sent if isinstance(packet, self.b.ItemStackRequestPacket)]

    @staticmethod
    def action(request):
        action, = request.actions
        return (action.source.slot, action.destination.slot, action.amount,
                action.source.net_id_variant, action.destination.net_id_variant)

    @staticmethod
    def action_addresses(request):
        action, = request.actions
        return action.source.full_container_name.name.name, action.destination.full_container_name.name.name

    def answer(self, *replies):
        remaining = iter(replies)
        original = self.stream.send

        async def send(packet, **kwargs):
            await original(packet, **kwargs)
            request, = packet.requests
            expected, corrections = next(remaining)
            self.assertEqual(self.action(request), expected)
            self.reply(request, corrections)

        self.stream.send = send

    def hold_requests(self):
        pending = asyncio.Queue()
        original = self.stream.send

        async def send(packet):
            await original(packet)
            if isinstance(packet, self.b.ItemStackRequestPacket):
                request, = packet.requests
                pending.put_nowait(request)

        self.stream.send = send
        return pending

    def start(self, operation):
        task = asyncio.create_task(operation)

        async def finish():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(finish)
        return task

    async def transfer(self, count, *, direction="take", item_name="minecraft:paper",
                       chest=range(0, 2), player=range(9, 11), timeout=1):
        source, destination = (chest, player) if direction == "take" else (player, chest)
        return await transfer_range(self.bot, direction=direction, item=item_name, count=count,
                                    source_slots=source, destination_slots=destination, timeout=timeout)

    async def test_transfers_across_multiple_sources_merge_before_empty_in_both_directions(self):
        original_send = self.stream.send
        for block, address in (("minecraft:chest", CHEST), ("minecraft:barrel", BARREL)):
            with self.subTest(block=block):
                self.stream.send = original_send
                self.stream.sent.clear()
                self.observe_block(block)
                await self.check_bidirectional_transfer(address)

    async def check_bidirectional_transfer(self, address):
        self.open_chest(self.stack(PAPER, 7, 17), self.stack(PAPER, 6, 18), self.stack(PAPER, 31, 19))
        self.player_slot(9, self.stack(PAPER, 62, 31))
        self.player_slot(10, self.stack())
        wrong_address = BARREL if address == CHEST else CHEST
        self.answer(
            ((0, 9, 2, 17, 31), [(wrong_address, 0, 8, 75), (address, 0, 5, 17), (PLAYER, 9, 64, 31)]),
            ((0, 10, 5, 17, 0), [(address, 0, 0, None), (PLAYER, 10, 5, 32)]),
            ((1, 10, 3, 18, 32), [(address, 1, 3, 18), (PLAYER, 10, 8, 32)]),
            ((9, 1, 61, 31, 18), [(PLAYER, 9, 3, 31), (wrong_address, 1, 8, 75), (address, 1, 64, 18)]),
            ((9, 0, 3, 31, 0), [(PLAYER, 9, 0, None), (address, 0, 3, 33)]),
            ((10, 0, 4, 32, 33), [(PLAYER, 10, 4, 32), (address, 0, 7, 33)]),
        )

        taken = await self.transfer(10)
        first_id, second_id, third_id = (request.client_request_id.raw_id for request in self.outgoing())
        self.assertEqual(taken, RangeTransferResult(10, (
            RangeTransferStep(0, 9, 2, 2, first_id, "SUCCESS"),
            RangeTransferStep(0, 10, 5, 5, second_id, "SUCCESS"),
            RangeTransferStep(1, 10, 3, 3, third_id, "SUCCESS"),
        ), "complete"))
        stored = await self.transfer(68, direction="put")
        self.assertEqual((stored.confirmed_count, stored.stop_reason), (68, "complete"))
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.container_slots[1].stack_size,
                          self.bot.container_slots[2].stack_size, self.bot.inventory_slots[10].stack_size), (7, 64, 31, 4))
        self.assertEqual([self.action_addresses(request) for request in self.outgoing()],
                         [(address, PLAYER)] * 3 + [(PLAYER, address)] * 3)

    async def test_keeps_variants_apart_and_skips_an_unknown_destination(self):
        self.open_chest(self.stack(PAPER, 4, 17, user_data=b"plain variant"))
        self.player_slot(9, self.stack(PAPER, 3, 31, user_data=b"renamed variant"))
        unknown = await self.transfer(4)
        self.assertEqual(unknown, RangeTransferResult(4, (), "unobserved"))
        self.assertEqual(self.outgoing(), [])
        self.player_slot(10, self.stack())
        self.answer(((0, 10, 4, 17, 0), [(CHEST, 0, 0, None), (PLAYER, 10, 4, 32)]))
        moved = await self.transfer(4)
        self.assertEqual(moved.confirmed_count, 4)
        self.assertEqual(self.bot.inventory_slots[9].user_data_buffer, b"renamed variant")
        self.assertEqual(self.bot.inventory_slots[10].user_data_buffer, b"plain variant")

    async def test_match_selects_only_the_requested_stack_variant_in_both_directions(self):
        named = b"\x01rangetest-renamed-paper\x00"

        def variant(amount, net_id, aux=3):
            stack = self.stack(PAPER, amount, net_id, user_data=named)
            stack.aux_value = aux
            return stack

        self.open_chest(self.stack(PAPER, 8, 17), variant(5, 18), variant(9, 19, 1), variant(7, 20))
        self.player_slot(9, self.stack(PAPER, 60, 30))
        self.player_slot(10, variant(60, 31))
        self.player_slot(11, self.stack())
        self.player_slot(12, variant(4, 33))
        self.answer(
            ((1, 10, 4, 18, 31), [(CHEST, 1, 2, 18), (PLAYER, 10, 63, 31)]),
            ((1, 10, 1, 18, 31), [(CHEST, 1, 1, 18), (PLAYER, 10, 64, 31)]),
            ((1, 11, 1, 18, 0), [(CHEST, 1, 0, None), (PLAYER, 11, 1, 32)]),
            ((10, 1, 3, 31, 0), [(PLAYER, 10, 61, 31), (CHEST, 1, 3, 34)]),
        )

        def matches(stack):
            self.assertIsInstance(stack, HeldItemStack)
            return stack.aux_value == 3 and stack.user_data_buffer == named

        taken = await transfer_range(
            self.bot, direction="take", item="minecraft:paper", count=5,
            source_slots=range(3), destination_slots=range(9, 12), match=matches,
        )
        stored = await transfer_range(
            self.bot, direction="put", item="minecraft:paper", count=3,
            source_slots=range(9, 11), destination_slots=range(3), match=matches,
        )
        self.assertEqual((taken.confirmed_count, taken.stop_reason), (5, "complete"))
        self.assertEqual([step.confirmed for step in taken.steps], [3, 1, 1])
        self.assertEqual((stored.confirmed_count, stored.stop_reason), (3, "complete"))
        self.assertEqual([self.action_addresses(request) for request in self.outgoing()],
                         [(CHEST, PLAYER)] * 3 + [(PLAYER, CHEST)])
        for slots, expected in (
            (self.bot.container_slots, [(0, 8, 17, 0, b""), (1, 3, 34, 3, named),
                                        (2, 9, 19, 1, named), (3, 7, 20, 3, named)]),
            (self.bot.inventory_slots, [(9, 60, 30, 0, b""), (10, 61, 31, 3, named),
                                        (11, 1, 32, 3, named), (12, 4, 33, 3, named)]),
        ):
            for index, count, net_id, aux, data in expected:
                stack = slots[index]
                self.assertEqual((stack.id, stack.stack_size, stack.net_id_variant, stack.aux_value,
                                  stack.user_data_buffer), (PAPER, count, net_id, aux, data))

    async def test_match_exhaustion_preserves_unknown_source_slots(self):
        self.open_chest()
        self.player_slot(9, self.stack(PAPER, 5, 31))
        self.player_slot(11, self.stack(STICK, 2, 33))
        seen = []

        def named_only(stack):
            seen.append(stack.item_id)
            return bool(stack.user_data_buffer)

        options = dict(direction="put", item="minecraft:paper", count=1, source_slots=range(9, 12),
                       destination_slots=range(1), match=named_only)
        self.assertEqual(await transfer_range(self.bot, **options), RangeTransferResult(1, (), "unobserved"))
        self.player_slot(10, self.stack())
        self.assertEqual(await transfer_range(self.bot, **options), RangeTransferResult(1, (), "source_exhausted"))
        self.assertEqual(seen, [PAPER, PAPER])
        self.assertEqual(self.outgoing(), [])

    async def test_match_cannot_redirect_put_to_a_replacement_empty_chest(self):
        self.open_chest()
        self.player_slot(9, self.stack(PAPER, 2, 31))
        other = (3, 100, 0)
        self.observe_block("minecraft:chest", other)
        self.answer(((9, 0, 1, 31, 0), [(PLAYER, 9, 1, 31), (CHEST, 0, 1, 42)]))

        def match(_):
            self.open_chest(position=other)
            return True

        result = await transfer_range(
            self.bot, direction="put", item="minecraft:paper", count=1,
            source_slots=range(9, 10), destination_slots=range(1), match=match,
        )
        self.assertEqual(result, RangeTransferResult(1, (), "window_changed"))
        self.assertEqual(self.outgoing(), [])
        self.assertEqual(self.bot.open_window.position, other)
        self.assertEqual(self.bot.container_slots[0].stack_size, 0)
        self.assertEqual(self.bot.inventory_slots[9].stack_size, 2)

    async def test_match_close_stops_before_planning_the_next_candidate(self):
        b = self.b
        self.open_chest()
        self.player_slot(9, self.stack(PAPER, 2, 31))
        self.player_slot(10, self.stack(PAPER, 4, 32))
        seen = []

        def match(stack):
            seen.append(stack.net_id)
            self.emit(b.ContainerClosePacket(container_id=2, container_type=b.ContainerType.CONTAINER))
            return False

        result = await transfer_range(
            self.bot, direction="put", item="minecraft:paper", count=1,
            source_slots=range(9, 11), destination_slots=range(1), match=match,
        )
        self.assertEqual(result, RangeTransferResult(1, (), "window_changed"))
        self.assertEqual(seen, [31])
        self.assertEqual(self.outgoing(), [])
        self.assertIsNone(self.bot.open_window)

    async def test_match_after_a_session_change_stops_or_preserves_its_original_exception(self):
        self.open_chest()
        self.player_slot(9, self.stack(PAPER, 2, 31))
        failure = ValueError("classifier failed after the session changed")

        for replacement, raises in ((None, False), (copy(self.bot._containers), False), (None, True)):
            with self.subTest(rejoined=replacement is not None, raises=raises), ExitStack() as patches:
                def match(_):
                    patches.enter_context(patch.object(self.bot, "_containers", replacement))
                    if raises:
                        raise failure
                    return True

                operation = transfer_range(
                    self.bot, direction="put", item="minecraft:paper", count=1,
                    source_slots=range(9, 10), destination_slots=range(1), match=match,
                )
                if raises:
                    with self.assertRaises(ValueError) as raised:
                        await operation
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(failure.transfer_progress, RangeTransferResult(1, (), "unconfirmed"))
                else:
                    self.assertEqual(await operation, RangeTransferResult(1, (), "window_changed"))
                self.assertEqual(self.outgoing(), [])

    async def test_match_cannot_use_changed_double_chest_endpoints_with_the_same_window(self):
        self.observe_block("minecraft:chest", runtime_id=3162850062)
        self.open_chest(width=54)
        self.player_slot(9, self.stack(PAPER, 2, 31))
        window = self.bot.open_window

        def match(_):
            self.observe_block("minecraft:chest", runtime_id=2001328343)
            return True

        result = await transfer_range(
            self.bot, direction="put", item="minecraft:paper", count=1,
            source_slots=range(9, 10), destination_slots=range(27, 28), match=match,
        )
        self.assertIs(self.bot.open_window, window)
        self.assertEqual(result, RangeTransferResult(1, (), "window_changed"))
        self.assertEqual(self.outgoing(), [])

    async def test_matched_put_rechecks_the_window_at_write_and_keeps_its_confirmed_prefix(self):
        self.open_chest(self.stack(PAPER, 63, 17))
        self.player_slot(9, self.stack(PAPER, 3, 31))
        other = (3, 100, 0)
        self.observe_block("minecraft:chest", other)
        first = ((9, 0, 1, 31, 17), [(PLAYER, 9, 2, 31), (CHEST, 0, 64, 17)])
        self.answer(first, ((9, 1, 2, 31, 0), [(PLAYER, 9, 0, None), (CHEST, 1, 2, 42)]))
        send = self.stream.send
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_second(packet, **kwargs):
            if self.outgoing():
                entered.set()
                await release.wait()
            await send(packet, **kwargs)

        with patch.object(self.stream, "send", hold_second):
            task = self.start(transfer_range(
                self.bot, direction="put", item="minecraft:paper", count=3,
                source_slots=range(9, 10), destination_slots=range(2), match=lambda _: True,
            ))
            await asyncio.wait_for(entered.wait(), 1)
            self.open_chest(position=other)
            release.set()
            result = await task
        request, = self.outgoing()
        self.assertEqual(result, RangeTransferResult(3, (
            RangeTransferStep(9, 0, 1, 1, int(request.client_request_id.raw_id), "SUCCESS"),
        ), "window_changed"))
        self.assertEqual(self.bot.open_window.position, other)
        self.assertEqual(self.bot.container_slots[1].stack_size, 0)
        self.assertEqual(self.bot.inventory_slots[9].stack_size, 2)

    async def test_matched_put_rechecks_the_physical_double_chest_endpoint_at_write(self):
        self.observe_block("minecraft:chest", runtime_id=3162850062)
        self.open_chest(width=54)
        self.player_slot(9, self.stack(PAPER, 2, 31))
        window = self.bot.open_window
        send = self.stream.send

        async def change_endpoint(packet, **kwargs):
            self.observe_block("minecraft:chest", runtime_id=2001328343)
            await send(packet, **kwargs)

        with patch.object(self.stream, "send", change_endpoint):
            result = await transfer_range(
                self.bot, direction="put", item="minecraft:paper", count=1,
                source_slots=range(9, 10), destination_slots=range(27, 28), match=lambda _: True,
            )
        self.assertIs(self.bot.open_window, window)
        self.assertEqual(result, RangeTransferResult(1, (), "window_changed"))
        self.assertEqual(self.outgoing(), [])

    async def test_match_exception_preserves_confirmed_prefix_without_sending_another_step(self):
        self.open_chest(self.stack(PAPER, 2, 17))
        self.player_slot(9, self.stack())
        self.answer(((0, 9, 2, 17, 0), [(CHEST, 0, 1, 17), (PLAYER, 9, 1, 31)]))
        failure = ValueError("cannot classify this observation")

        def match(stack):
            if stack.count == 1:
                raise failure
            return True

        with self.assertRaises(ValueError) as raised:
            await transfer_range(
                self.bot, direction="take", item="minecraft:paper", count=2,
                source_slots=range(1), destination_slots=range(9, 10), match=match,
            )
        self.assertIs(raised.exception, failure)
        request, = self.outgoing()
        self.assertEqual(failure.transfer_progress, RangeTransferResult(2, (
            RangeTransferStep(0, 9, 2, 1, int(request.client_request_id.raw_id), "SUCCESS"),
        ), "unconfirmed"))

    async def test_async_match_is_refused_before_any_request(self):
        self.open_chest(self.stack(PAPER, 2, 17))
        self.player_slot(9, self.stack())

        async def match(_):
            return False

        with self.assertRaisesRegex(TypeError, "synchronous predicate") as raised:
            await transfer_range(
                self.bot, direction="take", item="minecraft:paper", count=1,
                source_slots=range(1), destination_slots=range(9, 10), match=match,
            )
        self.assertEqual(raised.exception.transfer_progress, RangeTransferResult(1, (), "unconfirmed"))
        self.assertEqual(self.outgoing(), [])

    async def test_matched_source_variant_is_rechecked_before_sending(self):
        named = b"\x01rangetest-renamed-paper\x00"
        self.open_chest(self.stack(PAPER, 2, 17, user_data=named))
        self.player_slot(9, self.stack())
        asyncio.get_running_loop().call_soon(self.set_slot, 2, 0, self.stack(PAPER, 2, 17))
        with self.assertRaisesRegex(RuntimeError, "slots changed before submission"):
            await transfer_range(
                self.bot, direction="take", item="minecraft:paper", count=1,
                source_slots=range(1), destination_slots=range(9, 10),
                match=lambda stack: stack.user_data_buffer == named,
            )
        self.assertEqual(self.outgoing(), [])
        self.assertEqual(self.bot.container_slots[0].user_data_buffer, b"")

    async def test_unknown_capacity_allows_observed_empty_slots_only(self):
        self.open_chest(self.stack(CUSTOM, 6, 17))
        self.player_slot(9, self.stack(CUSTOM, 3, 31))
        self.assertEqual(await self.transfer(6, item_name="range_test:custom_item", player=range(9, 10)),
                         RangeTransferResult(6, (), "unobserved"))
        self.assertEqual(self.outgoing(), [])
        self.player_slot(10, self.stack())
        self.answer(((0, 10, 6, 17, 0), [(CHEST, 0, 0, None), (PLAYER, 10, 6, 32)]))
        self.assertEqual((await self.transfer(6, item_name="range_test:custom_item")).confirmed_count, 6)

    async def test_vanilla_resource_capacity_prioritizes_existing_stack_before_earlier_empty(self):
        self.open_chest(self.stack(DIAMOND, 5, 17), self.stack(DIAMOND, 6, 18))
        self.player_slot(9, self.stack())
        self.player_slot(10, self.stack(DIAMOND, 61, 31))
        self.answer(
            ((0, 10, 3, 17, 31), [(CHEST, 0, 2, 17), (PLAYER, 10, 64, 31)]),
            ((0, 9, 2, 17, 0), [(CHEST, 0, 0, None), (PLAYER, 9, 2, 32)]),
            ((1, 9, 4, 18, 32), [(CHEST, 1, 2, 18), (PLAYER, 9, 6, 32)]),
        )
        result = await self.transfer(9, item_name="minecraft:diamond")
        self.assertEqual((result.confirmed_count, result.stop_reason), (9, "complete"))
        self.assertEqual((self.bot.inventory_slots[9].stack_size, self.bot.inventory_slots[10].stack_size), (6, 64))
        self.assertEqual(len(self.outgoing()), 3)

    async def test_partial_success_uses_both_corrections_then_retains_rejection(self):
        self.open_chest(self.stack(SNOWBALL, 9, 17))
        self.player_slot(9, self.stack(SNOWBALL, 14, 31))
        original = self.stream.send

        async def send(packet):
            await original(packet)
            request, = packet.requests
            if len(self.outgoing()) == 1:
                self.assertEqual(self.action(request), (0, 9, 2, 17, 31))
                self.reply(request, [(CHEST, 0, 8, 17), (PLAYER, 9, 15, 31)])
            else:
                self.assertEqual(self.action(request), (0, 9, 1, 17, 31))
                self.reply(request, verdict="ERROR")

        self.stream.send = send
        result = await self.transfer(2, item_name="minecraft:snowball", player=range(9, 10))
        self.assertEqual(result, RangeTransferResult(2, (
            RangeTransferStep(0, 9, 2, 1, -1, "SUCCESS"), RangeTransferStep(0, 9, 1, 0, -3, "ERROR"),
        ), "rejected"))
        self.assertEqual(result.confirmed_count, 1)

    async def test_snowball_cap_and_single_item_tools(self):
        self.open_chest(self.stack(SNOWBALL, 9, 17), self.stack(IRON_PICKAXE, 1, 18))
        self.player_slot(9, self.stack(SNOWBALL, 15, 31))
        self.player_slot(10, self.stack(IRON_PICKAXE, 1, 32))
        self.player_slot(11, self.stack())
        self.answer(
            ((0, 9, 1, 17, 31), [(CHEST, 0, 8, 17), (PLAYER, 9, 16, 31)]),
            ((1, 11, 1, 18, 0), [(CHEST, 1, 0, None), (PLAYER, 11, 1, 18)]),
        )
        clamped = await self.transfer(2, item_name="minecraft:snowball", player=range(9, 10))
        self.assertEqual((clamped.confirmed_count, clamped.stop_reason), (1, "destination_full"))
        tool = await self.transfer(1, item_name="minecraft:iron_pickaxe", player=range(10, 12))
        self.assertEqual((tool.steps[0].destination_slot, tool.confirmed_count), (11, 1))

    async def test_transfer_item_merges_one_snowball_with_full_inventory(self):
        self.open_chest(self.stack(SNOWBALL, 9, 17))
        for index in range(35):
            self.player_slot(index, self.stack(STICK, 64, 50 + index))
        self.player_slot(35, self.stack(SNOWBALL, 15, 31))
        self.answer(((0, 35, 1, 17, 31), [(CHEST, 0, 8, 17), (PLAYER, 35, 16, 31)]))

        result = await transfer_item(self.bot, direction="take", item="minecraft:snowball", count=2)

        self.assertEqual(result, RangeTransferResult(2, (
            RangeTransferStep(0, 35, 1, 1, -1, "SUCCESS"),
        ), "destination_full"))
        self.assertEqual(result.confirmed_count, 1)
        self.assertEqual(self.bot.inventory_slots[35].stack_size, 16)
        self.assertEqual(self.bot.container_slots[0].stack_size, 8)
        self.assertEqual(len(self.outgoing()), 1)

    async def test_transfer_item_stops_at_nonconserving_corrections(self):
        self.open_chest(self.stack(PAPER, 1, 17), self.stack(PAPER, 4, 18))
        self.player_slot(35, self.stack())
        self.answer(
            ((0, 35, 1, 17, 0), [(CHEST, 0, 0, None), (PLAYER, 35, 1, 31)]),
            ((1, 35, 2, 18, 31), [(CHEST, 1, 2, 18), (PLAYER, 35, 2, 31)]),
        )

        result = await transfer_item(self.bot, direction="take", item="minecraft:paper", count=3)

        self.assertEqual(result, RangeTransferResult(3, (
            RangeTransferStep(0, 35, 1, 1, -1, "SUCCESS"),
            RangeTransferStep(1, 35, 2, None, -3, "SUCCESS"),
        ), "unconfirmed"))
        self.assertEqual(result.confirmed_count, 1)
        self.assertEqual(len(self.outgoing()), 2)

    async def test_transfer_item_waits_for_content_and_uses_the_full_double_chest(self):
        self.open_chest(width=None)
        self.player_slot(35, self.stack())
        self.answer(
            ((53, 35, 2, 18, 0), [(CHEST, 53, 2, 18), (PLAYER, 35, 2, 31)]),
            ((35, 53, 2, 31, 18), [(PLAYER, 35, 0, None), (CHEST, 53, 4, 18)]),
        )

        def match(stack):
            return stack.user_data_buffer == b"selected"

        task = self.start(transfer_item(
            self.bot, direction="take", item="minecraft:paper", count=2, match=match,
        ))
        self.set_slot(2, 53, self.stack(PAPER, 9, 27, user_data=b"selected"))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(self.outgoing(), [])
        self.content(self.stack(PAPER, 4, 17), *(self.stack() for _ in range(52)),
                     self.stack(PAPER, 4, 18, user_data=b"selected"), width=54)

        taken = await asyncio.wait_for(task, 1)
        stored = await transfer_item(self.bot, direction="put", item="minecraft:paper", count=2, match=match)
        self.assertEqual(taken, RangeTransferResult(2, (RangeTransferStep(53, 35, 2, 2, -1, "SUCCESS"),), "complete"))
        self.assertEqual(stored, RangeTransferResult(2, (RangeTransferStep(35, 53, 2, 2, -3, "SUCCESS"),), "complete"))
        self.assertEqual(self.bot.container_slots[53].stack_size, 4)
        self.assertEqual(len(self.outgoing()), 2)

    async def test_transfer_item_does_not_use_replacement_contents_while_waiting(self):
        self.open_chest(width=None)
        original_window = self.bot.open_window
        self.player_slot(35, self.stack())
        task = self.start(transfer_item(self.bot, direction="take", item="minecraft:paper", count=1))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.open_chest(self.stack(PAPER, 3, 17))
        self.assertIsNot(self.bot.open_window, original_window)
        self.assertEqual(self.bot.open_window, original_window)

        self.assertEqual(await asyncio.wait_for(task, 1), RangeTransferResult(1, (), "window_changed"))
        self.assertEqual(self.outgoing(), [])

    async def test_transfer_item_rechecks_the_window_at_write_without_a_predicate(self):
        self.open_chest()
        self.player_slot(35, self.stack(PAPER, 3, 31))
        send = self.stream.send

        async def replace_window(packet, **kwargs):
            self.open_chest()
            await send(packet, **kwargs)

        with patch.object(self.stream, "send", replace_window):
            result = await transfer_item(self.bot, direction="put", item="minecraft:paper", count=1)

        self.assertEqual(result, RangeTransferResult(1, (), "window_changed"))
        self.assertEqual(self.outgoing(), [])

    async def test_transfer_item_content_wait_and_requests_share_the_timeout(self):
        self.open_chest(width=None)
        self.player_slot(35, self.stack())
        self.answer(((0, 35, 1, 17, 0), [(CHEST, 0, 1, 17), (PLAYER, 35, 1, 31)]))
        loop = asyncio.get_running_loop()
        now = loop.time
        elapsed = 0
        with patch.object(loop, "time", lambda: now() + elapsed), patch.object(loop, "slow_callback_duration", 20):
            task = self.start(transfer_item(self.bot, direction="take", item="minecraft:paper", count=1, timeout=10))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.content(self.stack(PAPER, 2, 17), width=27)
            elapsed = 10
            with self.assertRaises(TimeoutError) as raised:
                await task

        self.assertEqual(raised.exception.transfer_progress, RangeTransferResult(1, (), "unconfirmed"))
        self.assertEqual(self.outgoing(), [])

    async def test_success_with_missing_duplicate_or_disagreeing_corrections_is_unconfirmed(self):
        original = self.stream.send
        for corrections in (
            [(CHEST, 0, 3, 17)],
            [(CHEST, 0, 3, 17), (PLAYER, 9, 1, 31), (PLAYER, 9, 1, 31)],
            [(CHEST, 0, 2, 17), (PLAYER, 9, 1, 31)],
        ):
            with self.subTest(corrections=corrections):
                self.open_chest(self.stack(PAPER, 4, 17))
                self.player_slot(9, self.stack())
                self.stream.sent.clear()
                self.stream.send = original
                self.answer(((0, 9, 2, 17, 0), corrections))
                result = await self.transfer(2, player=range(9, 10))
                self.assertEqual(result.stop_reason, "unconfirmed")
                step, = result.steps
                self.assertEqual((step.confirmed, step.verdict, result.confirmed_count), (None, "SUCCESS", 0))
                self.assertEqual(len(self.outgoing()), 1)

    async def test_confirmed_prefix_survives_reopened_window_and_new_slot_contents(self):
        self.open_chest(self.stack(PAPER, 1, 17), self.stack(PAPER, 4, 18))
        self.player_slot(9, self.stack())
        old = self.bot.open_window
        original = self.stream.send

        async def send(packet):
            await original(packet)
            request, = packet.requests
            self.assertEqual(self.action(request), (0, 9, 1, 17, 0))
            self.open_chest(self.stack(PAPER, 31, 42))
            self.reply(request, [(CHEST, 0, 0, None), (PLAYER, 9, 1, 31)])
            self.player_slot(9, self.stack(PAPER, 7, 43, user_data=b"later observation"))

        self.stream.send = send
        result = await self.transfer(2, player=range(9, 10))
        self.assertEqual((result.confirmed_count, result.stop_reason), (1, "window_changed"))
        self.assertIsNot(self.bot.open_window, old)
        self.assertEqual(self.bot.open_window, old)
        self.assertEqual(self.bot.container_slots[0].stack_size, 31)
        self.assertEqual(self.bot.inventory_slots[9].stack_size, 7)
        self.assertEqual(len(self.outgoing()), 1)

    async def test_same_network_stack_count_changes_before_send_do_not_confirm_a_stale_delta(self):
        self.open_chest(self.stack(SNOWBALL, 9, 17))
        self.player_slot(9, self.stack(SNOWBALL, 14, 31))
        self.answer(((0, 9, 2, 17, 31), [(CHEST, 0, 7, 17), (PLAYER, 9, 16, 31)]))

        def update_before_send():
            self.set_slot(2, 0, self.stack(SNOWBALL, 8, 17))
            self.player_slot(9, self.stack(SNOWBALL, 15, 31))

        asyncio.get_running_loop().call_soon(update_before_send)
        with self.assertRaises(RuntimeError) as raised:
            await self.transfer(2, item_name="minecraft:snowball", player=range(9, 10))
        self.assertEqual(raised.exception.transfer_progress, RangeTransferResult(2, (
            RangeTransferStep(0, 9, 2, None, None, None),
        ), "unconfirmed"))
        self.assertEqual(self.outgoing(), [])
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.inventory_slots[9].stack_size), (8, 15))

    async def test_clear_verdict_counts_transfer_without_overwriting_new_source_stack(self):
        original = self.stream.send
        for direction in ("take", "put"):
            with self.subTest(direction=direction):
                into_chest = direction == "put"
                self.stream.sent.clear()
                self.open_chest(self.stack() if into_chest else self.stack(PAPER, 1, 17))
                self.player_slot(9, self.stack(PAPER, 1, 31) if into_chest else self.stack())

                async def send(packet):
                    await original(packet)
                    request, = packet.requests
                    newer = self.stack(STICK, 8, 75)
                    if into_chest:
                        self.assertEqual(self.action(request), (9, 0, 1, 31, 0))
                        self.player_slot(9, newer)
                        self.reply(request, [(PLAYER, 9, 0, None), (CHEST, 0, 1, 65)])
                    else:
                        self.assertEqual(self.action(request), (0, 9, 1, 17, 0))
                        self.set_slot(2, 0, newer)
                        self.reply(request, [(CHEST, 0, 0, None), (PLAYER, 9, 1, 65)])

                self.stream.send = send
                result = await self.transfer(2, direction=direction, chest=range(1), player=range(9, 10))
                source = self.bot.inventory_slots[9] if into_chest else self.bot.container_slots[0]
                destination = self.bot.container_slots[0] if into_chest else self.bot.inventory_slots[9]
                self.assertEqual((result.confirmed_count, result.stop_reason), (1, "source_exhausted"))
                self.assertEqual((source.id, source.stack_size, source.net_id_variant), (STICK, 8, 75))
                self.assertEqual((destination.id, destination.stack_size), (PAPER, 1))
                self.assertEqual(len(self.outgoing()), 1)

    async def test_timeout_and_cancel_keep_progress_and_allow_late_writeback(self):
        original = self.stream.send
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.open_chest(self.stack(PAPER, 1, 17), self.stack(PAPER, 2, 18))
                self.player_slot(9, self.stack())
                self.stream.sent.clear()
                pending = asyncio.Event()

                async def send(packet):
                    await original(packet)
                    request, = packet.requests
                    if len(self.outgoing()) == 1:
                        self.reply(request, [(CHEST, 0, 0, None), (PLAYER, 9, 1, 31)])
                    else:
                        pending.set()

                self.stream.send = send
                transfer = asyncio.create_task(self.transfer(3, player=range(9, 10), timeout=1 if cancel else 0.1))
                await asyncio.wait_for(pending.wait(), 1)
                if cancel:
                    transfer.cancel("stop transferring")
                error_type = asyncio.CancelledError if cancel else TimeoutError
                with self.assertRaises(error_type) as observed:
                    await transfer
                self.assertIs(type(observed.exception), error_type)
                progress = observed.exception.transfer_progress
                self.assertEqual(progress.confirmed_count, 1)
                self.assertEqual(progress.steps[-1], RangeTransferStep(1, 9, 2, None, None, None))
                self.assertEqual(progress.stop_reason, "cancelled" if cancel else "unconfirmed")
                self.reply(self.outgoing()[-1], [(CHEST, 1, 0, None), (PLAYER, 9, 3, 31)])
                self.assertEqual(self.bot.inventory_slots[9].stack_size, 3)
                self.assertEqual(progress.confirmed_count, 1)
                self.assertEqual(len(self.outgoing()), 2)

    async def test_overlapping_ranges_do_not_confirm_two_moves_against_the_same_baseline(self):
        self.open_chest(self.stack(SNOWBALL, 9, 17))
        self.player_slot(9, self.stack(SNOWBALL, 14, 31))
        pending = self.hold_requests()
        first = self.start(self.transfer(1, item_name="minecraft:snowball", player=range(9, 10)))
        first_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(first_request), (0, 9, 1, 17, 31))
        second = self.start(self.transfer(2, item_name="minecraft:snowball", player=range(9, 10)))
        next_sent = self.start(pending.get())
        ready, _ = await asyncio.wait((second, next_sent), timeout=1, return_when=asyncio.FIRST_COMPLETED)
        self.assertTrue(ready)
        self.reply(first_request, [(CHEST, 0, 8, 17), (PLAYER, 9, 15, 31)])
        if next_sent in ready:
            # Without endpoint ownership, both requests share the 9/14 snapshot.
            second_request = await next_sent
            self.assertEqual(self.action(second_request), (0, 9, 2, 17, 31))
            self.reply(second_request, [(CHEST, 0, 7, 17), (PLAYER, 9, 16, 31)])
        self.assertEqual((await asyncio.wait_for(first, 1)).confirmed_count, 1)
        self.assertEqual(await asyncio.wait_for(second, 1), RangeTransferResult(2, (), "unconfirmed"))
        self.assertEqual(self.outgoing(), [first_request])
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.inventory_slots[9].stack_size), (8, 15))

    async def test_disjoint_ranges_share_a_window_without_waiting_for_each_other(self):
        self.open_chest(self.stack(PAPER, 4, 17), self.stack(PAPER, 7, 18))
        self.player_slot(9, self.stack())
        self.player_slot(10, self.stack())
        pending = self.hold_requests()
        first = self.start(self.transfer(1, chest=range(1), player=range(9, 10)))
        before = await asyncio.wait_for(pending.get(), 1)
        second = self.start(self.transfer(3, chest=range(1, 2), player=range(10, 11)))
        after = await asyncio.wait_for(pending.get(), 1)
        self.assertNotEqual(before.client_request_id.raw_id, after.client_request_id.raw_id)
        self.assertEqual(self.action(before), (0, 9, 1, 17, 0))
        self.assertEqual(self.action(after), (1, 10, 3, 18, 0))
        self.reply(after, [(CHEST, 1, 4, 18), (PLAYER, 10, 3, 42)])
        self.assertEqual((await asyncio.wait_for(second, 1)).confirmed_count, 3)
        self.assertFalse(first.done())
        self.reply(before, [(CHEST, 0, 3, 17), (PLAYER, 9, 1, 41)])
        self.assertEqual((await asyncio.wait_for(first, 1)).confirmed_count, 1)
        self.assertEqual(len(self.outgoing()), 2)

    async def test_conflicting_next_step_leaves_only_the_confirmed_prefix(self):
        self.open_chest(self.stack(PAPER, 1, 17), self.stack(PAPER, 5, 18))
        self.player_slot(9, self.stack())
        self.player_slot(10, self.stack())
        pending = self.hold_requests()
        incumbent = self.start(self.transfer(1, chest=range(1, 2), player=range(10, 11)))
        reserved = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(reserved), (1, 10, 1, 18, 0))
        contender = self.start(self.transfer(3, chest=range(2), player=range(9, 10)))
        sent = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(sent), (0, 9, 1, 17, 0))
        self.reply(sent, [(CHEST, 0, 0, None), (PLAYER, 9, 1, 41)])
        result = await asyncio.wait_for(contender, 1)
        self.assertEqual(result, RangeTransferResult(3, (
            RangeTransferStep(0, 9, 1, 1, sent.client_request_id.raw_id, "SUCCESS"),
        ), "unconfirmed"))
        self.assertEqual(self.outgoing(), [reserved, sent])
        self.reply(reserved, [(CHEST, 1, 4, 18), (PLAYER, 10, 1, 42)])
        self.assertEqual((await asyncio.wait_for(incumbent, 1)).confirmed_count, 1)

    async def test_plain_put_waits_for_overlapping_range_take_before_using_its_new_stack(self):
        self.open_chest(self.stack(PAPER, 6, 17))
        self.player_slot(9, self.stack())
        pending = self.hold_requests()
        first = self.start(self.transfer(2, chest=range(1), player=range(9, 10)))
        take_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(take_request), (0, 9, 2, 17, 0))
        with self.assertRaises(TimeoutError):
            await self.bot.put_into_container(9, 0, 1, timeout=0.05)
        self.assertFalse(first.done())
        self.assertTrue(pending.empty())
        put = self.start(self.bot.put_into_container(9, 0, 1, timeout=1))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(put.done())
        self.assertTrue(pending.empty())
        self.reply(take_request, [(CHEST, 0, 4, 17), (PLAYER, 9, 2, 41)])
        self.assertEqual((await asyncio.wait_for(first, 1)).confirmed_count, 2)
        put_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(put_request), (9, 0, 1, 41, 17))
        self.reply(put_request, [(PLAYER, 9, 1, 41), (CHEST, 0, 5, 17)])
        verdict = await asyncio.wait_for(put, 1)
        self.assertEqual((verdict.result.name, verdict.client_request_id.raw_id),
                         ("SUCCESS", put_request.client_request_id.raw_id))
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.inventory_slots[9].stack_size), (5, 1))

    async def test_range_put_conflicts_with_plain_take_on_just_the_chest_slot(self):
        self.open_chest(self.stack(PAPER, 6, 17))
        self.player_slot(9, self.stack())
        self.player_slot(10, self.stack(PAPER, 3, 42))
        pending = self.hold_requests()
        take = self.start(self.bot.take_from_container(0, 9, 2, timeout=1))
        take_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(take_request), (0, 9, 2, 17, 0))
        contents = dict(self.bot.container_slots)
        blocked = await asyncio.wait_for(self.transfer(1, direction="put", chest=range(1), player=range(10, 11)), 1)
        self.assertEqual(blocked, RangeTransferResult(1, (), "unconfirmed"))
        self.assertEqual(self.outgoing(), [take_request])
        self.assertEqual(self.bot.container_slots, contents)
        self.reply(take_request, [(CHEST, 0, 4, 17), (PLAYER, 9, 2, 41)])
        reply = await asyncio.wait_for(take, 1)
        self.assertEqual(reply.result.name, "SUCCESS")
        retried = self.start(self.transfer(1, direction="put", chest=range(1), player=range(10, 11)))
        put_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(put_request), (10, 0, 1, 42, 17))
        self.reply(put_request, [(PLAYER, 10, 2, 42), (CHEST, 0, 5, 17)])
        self.assertEqual((await asyncio.wait_for(retried, 1)).confirmed_count, 1)
        self.assertEqual((self.bot.inventory_slots[9].stack_size, self.bot.inventory_slots[10].stack_size), (2, 2))

    async def test_cancel_or_timeout_blocks_retry_until_late_verdict(self):
        original = self.stream.send
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                self.stream.send = original
                self.stream.sent.clear()
                self.open_chest(self.stack(SNOWBALL, 9, 17))
                self.player_slot(9, self.stack(SNOWBALL, 14, 31))
                pending = self.hold_requests()
                old = self.start(self.transfer(1, item_name="minecraft:snowball", chest=range(1), player=range(9, 10),
                                               timeout=1 if cancel else 0.1))
                request = await asyncio.wait_for(pending.get(), 1)
                self.assertEqual(self.action(request), (0, 9, 1, 17, 31))
                if cancel:
                    old.cancel("old request")
                error = asyncio.CancelledError if cancel else TimeoutError
                with self.assertRaises(error) as caught:
                    await asyncio.wait_for(old, 1)
                progress = caught.exception.transfer_progress
                self.assertEqual(progress.steps, (RangeTransferStep(0, 9, 1, None, None, None),))
                self.assertEqual(progress.stop_reason, "cancelled" if cancel else "unconfirmed")
                blocked = await asyncio.wait_for(self.transfer(2, item_name="minecraft:snowball", chest=range(1),
                                                               player=range(9, 10)), 1)
                self.assertEqual(blocked, RangeTransferResult(2, (), "unconfirmed"))
                self.assertEqual(self.outgoing(), [request])
                self.reply(request, [(CHEST, 0, 8, 17), (PLAYER, 9, 15, 31)])
                retry = self.start(self.transfer(2, item_name="minecraft:snowball", chest=range(1), player=range(9, 10)))
                updated = await asyncio.wait_for(pending.get(), 1)
                self.assertEqual(self.action(updated), (0, 9, 1, 17, 31))
                self.reply(updated, [(CHEST, 0, 7, 17), (PLAYER, 9, 16, 31)])
                result = await asyncio.wait_for(retry, 1)
                self.assertEqual((result.confirmed_count, result.stop_reason), (1, "destination_full"))
                self.assertEqual(progress.confirmed_count, 0)
                self.assertEqual(len(self.outgoing()), 2)

    async def test_unobserved_ranges_are_distinct_from_a_known_full_destination(self):
        self.open_chest(self.stack(SNOWBALL, 7, 17), self.stack(SNOWBALL, 1, None))
        self.player_slot(9, self.stack(SNOWBALL, 16, 31))
        for chest, player, reason in (
            (range(2), range(9, 10), "unobserved"),
            (range(1), range(9, 11), "unobserved"),
            (range(1), range(9, 10), "destination_full"),
        ):
            with self.subTest(chest=chest, player=player):
                result = await self.transfer(1, item_name="minecraft:snowball", chest=chest, player=player)
                self.assertEqual(result, RangeTransferResult(1, (), reason))
        self.assertEqual(self.outgoing(), [])

    async def test_incomplete_corrections_cannot_become_the_next_steps_baseline(self):
        self.open_chest(self.stack(SNOWBALL, 9, 17))
        self.player_slot(9, self.stack(SNOWBALL, 14, 31))
        pending = self.hold_requests()
        first = self.start(self.transfer(1, item_name="minecraft:snowball", player=range(9, 10)))
        request = await asyncio.wait_for(pending.get(), 1)
        self.reply(request, [(PLAYER, 9, 15, 31)])
        result = await asyncio.wait_for(first, 1)
        self.assertEqual((result.confirmed_count, result.stop_reason), (0, "unconfirmed"))
        self.assertEqual(result.steps, (RangeTransferStep(0, 9, 1, None, request.client_request_id.raw_id,
                                                          "SUCCESS"),))
        blocked = await asyncio.wait_for(self.transfer(1, item_name="minecraft:snowball", player=range(9, 10)), 1)
        self.assertIn(blocked.stop_reason, ("unobserved", "unconfirmed"))
        self.assertEqual((blocked.confirmed_count, blocked.steps), (0, ()))
        self.assertEqual(self.outgoing(), [request])
        self.set_slot(2, 0, self.stack(SNOWBALL, 8, 17))
        self.player_slot(9, self.stack(SNOWBALL, 15, 31))
        retry = self.start(self.transfer(1, item_name="minecraft:snowball", player=range(9, 10)))
        updated = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(updated), (0, 9, 1, 17, 31))
        self.reply(updated, [(CHEST, 0, 7, 17), (PLAYER, 9, 16, 31)])
        self.assertEqual((await asyncio.wait_for(retry, 1)).confirmed_count, 1)

    async def test_plain_success_without_slot_corrections_requires_both_fresh_observations(self):
        self.open_chest(self.stack(SNOWBALL, 9, 17))
        self.player_slot(9, self.stack(SNOWBALL, 14, 31))
        pending = self.hold_requests()
        ordinary = self.start(self.bot.take_from_container(0, 9, 1, timeout=1))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(request), (0, 9, 1, 17, 31))
        self.reply(request)
        verdict = await asyncio.wait_for(ordinary, 1)
        self.assertEqual((verdict.result.name, verdict.containers), ("SUCCESS", None))

        async def take():
            return await self.transfer(2, item_name="minecraft:snowball", chest=range(1), player=range(9, 10))

        speculative = self.start(take())
        next_sent = self.start(pending.get())
        ready, _ = await asyncio.wait((speculative, next_sent), timeout=1, return_when=asyncio.FIRST_COMPLETED)
        self.assertTrue(ready)
        if next_sent in ready:
            wrongly_sent = await next_sent
            self.assertEqual(self.action(wrongly_sent), (0, 9, 2, 17, 31))
            self.reply(wrongly_sent, [(CHEST, 0, 7, 17), (PLAYER, 9, 16, 31)])
        result = await asyncio.wait_for(speculative, 1)
        self.assertEqual(result, RangeTransferResult(2, (), "unconfirmed"))
        self.assertEqual(self.outgoing(), [request])
        next_sent.cancel()
        self.set_slot(2, 0, self.stack(SNOWBALL, 8, 17))
        self.assertEqual(await asyncio.wait_for(take(), 1), RangeTransferResult(2, (), "unconfirmed"))
        self.assertEqual(self.outgoing(), [request])
        self.player_slot(9, self.stack(SNOWBALL, 15, 31))
        retry = self.start(take())
        updated = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(updated), (0, 9, 1, 17, 31))
        self.reply(updated, [(CHEST, 0, 7, 17), (PLAYER, 9, 16, 31)])
        result = await asyncio.wait_for(retry, 1)
        self.assertEqual((result.confirmed_count, result.stop_reason), (1, "destination_full"))

    async def test_same_network_stack_observation_during_request_can_invalidate_its_quantity(self):
        original = self.stream.send
        for observed, confirmed, reason in (
            ((9, 14), 2, "complete"),
            ((7, None), 2, "complete"),
            ((8, 15), None, "unconfirmed"),
        ):
            with self.subTest(observed=observed):
                self.stream.send = original
                self.stream.sent.clear()
                self.open_chest(self.stack(SNOWBALL, 9, 17))
                self.player_slot(9, self.stack(SNOWBALL, 14, 31))
                pending = self.hold_requests()
                take = self.start(self.transfer(2, item_name="minecraft:snowball", chest=range(1), player=range(9, 10)))
                request = await asyncio.wait_for(pending.get(), 1)
                self.assertEqual(self.action(request), (0, 9, 2, 17, 31))
                chest, player = observed
                self.set_slot(2, 0, self.stack(SNOWBALL, chest, 17))
                if player is not None:
                    self.player_slot(9, self.stack(SNOWBALL, player, 31))
                self.reply(request, [(CHEST, 0, 7, 17), (PLAYER, 9, 16, 31)])
                result = await asyncio.wait_for(take, 1)
                self.assertEqual(result, RangeTransferResult(2, (
                    RangeTransferStep(0, 9, 2, confirmed, request.client_request_id.raw_id, "SUCCESS"),
                ), reason))
                self.assertEqual(self.outgoing(), [request])
                self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.inventory_slots[9].stack_size), (7, 16))

    async def test_independent_inventory_move_invalidates_overlapping_range_deltas(self):
        self.open_chest(self.stack(SNOWBALL, 9, 17))
        self.player_slot(9, self.stack(SNOWBALL, 14, 31))
        self.player_slot(10, self.stack(SNOWBALL, 3, 51))
        pending = self.hold_requests()
        take = self.start(self.transfer(2, item_name="minecraft:snowball", player=range(9, 10)))
        take_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(take_request), (0, 9, 2, 17, 31))
        move = self.start(self.bot.move_inventory_item(10, 9, 1, timeout=1))
        move_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(move_request), (10, 9, 1, 51, 31))
        self.reply(move_request, [(PLAYER, 10, 2, 51), (PLAYER, 9, 15, 31)])
        self.assertEqual((await asyncio.wait_for(move, 1)).result.name, "SUCCESS")
        self.set_slot(2, 0, self.stack(SNOWBALL, 8, 17))
        self.reply(take_request, [(CHEST, 0, 7, 17), (PLAYER, 9, 16, 31)])
        result = await asyncio.wait_for(take, 1)
        self.assertEqual(result, RangeTransferResult(2, (
            RangeTransferStep(0, 9, 2, None, take_request.client_request_id.raw_id, "SUCCESS"),
        ), "unconfirmed"))
        self.assertEqual(self.outgoing(), [take_request, move_request])

    async def test_reused_window_id_preserves_new_contents_but_shares_pending_physical_endpoints(self):
        self.open_chest(self.stack(PAPER, 1, 17), self.stack(PAPER, 27, 19))
        self.player_slot(9, self.stack())
        self.player_slot(10, self.stack())
        old_window = self.bot.open_window
        pending = self.hold_requests()
        old = self.start(self.transfer(1, chest=range(1), player=range(9, 10)))
        old_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(old_request), (0, 9, 1, 17, 0))
        self.assertEqual(self.action_addresses(old_request), (CHEST, PLAYER))
        self.observe_block("minecraft:barrel")
        self.open_chest(self.stack(PAPER, 20, 22), self.stack(STICK, 8, 75), self.stack(PAPER, 11, 23))
        new_window = self.bot.open_window
        self.assertEqual(new_window, old_window)
        self.assertIsNot(new_window, old_window)
        for chest, player in ((range(2, 3), range(9, 10)), (range(1), range(10, 11))):
            with self.subTest(chest=chest, player=player):
                blocked = await asyncio.wait_for(self.transfer(2, chest=chest, player=player), 1)
                self.assertEqual(blocked, RangeTransferResult(2, (), "unconfirmed"))
                self.assertEqual(self.outgoing(), [old_request])
        fresh = self.start(self.transfer(3, chest=range(2, 3), player=range(10, 11)))
        new_request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(new_request), (2, 10, 3, 23, 0))
        self.assertEqual(self.action_addresses(new_request), (BARREL, PLAYER))
        self.reply(new_request, [(BARREL, 2, 8, 23), (PLAYER, 10, 3, 42)])
        self.assertEqual((await asyncio.wait_for(fresh, 1)).confirmed_count, 3)
        self.reply(old_request, [(CHEST, 0, 0, None), (PLAYER, 9, 1, 41)])
        self.assertEqual((await asyncio.wait_for(old, 1)).confirmed_count, 1)
        self.assertIs(self.bot.open_window, new_window)
        self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.container_slots[0].net_id_variant), (20, 22))
        self.assertEqual((self.bot.container_slots[2].stack_size, self.bot.container_slots[2].net_id_variant), (8, 23))
        sentinel = self.bot.container_slots[1]
        self.assertEqual((sentinel.id, sentinel.stack_size, sentinel.net_id_variant), (STICK, 8, 75))
        self.assertEqual((self.bot.inventory_slots[9].net_id_variant, self.bot.inventory_slots[10].net_id_variant), (41, 42))

    async def test_range_validation_and_zero_quantity_send_no_requests(self):
        self.open_chest(width=27)
        for chest, player in ((range(0, 28), range(9, 10)), (range(0, 1), range(35, 37)),
                              (range(0, 2, 2), range(9, 10)), (range(-1, 0), range(9, 10))):
            with self.subTest(chest=chest, player=player):
                with self.assertRaises(ValueError):
                    await self.transfer(1, chest=chest, player=player)
        self.assertEqual(await self.transfer(0), RangeTransferResult(0, (), "complete"))
        self.assertEqual(self.outgoing(), [])

    async def test_zero_count_without_join_does_not_require_a_window(self):
        bot = Bot("UnjoinedRange", protocol=2225)
        result = await transfer_range(bot, direction="take", item="minecraft:paper", count=0,
                                      source_slots=range(1), destination_slots=range(9, 10))
        self.assertEqual(result, RangeTransferResult(0, (), "complete"))
        self.assertEqual(await transfer_item(bot, direction="take", item="minecraft:paper", count=0, timeout=0),
                         RangeTransferResult(0, (), "complete"))
        with self.assertRaisesRegex(ValueError, "source_slots"):
            await transfer_range(bot, direction="take", item="minecraft:paper", count=0,
                                 source_slots=range(0, 2, 2), destination_slots=range(9, 10))

    async def test_unjoined_positive_count_preserves_empty_transfer_progress(self):
        bot = Bot("UnjoinedRange", protocol=2225)
        with self.assertRaisesRegex(RuntimeError, "player has not joined") as raised:
            await transfer_range(bot, direction="take", item="minecraft:paper", count=1,
                                 source_slots=range(1), destination_slots=range(9, 10))
        self.assertEqual(raised.exception.transfer_progress, RangeTransferResult(1, (), "unconfirmed"))

    async def test_unknown_width_keeps_possible_double_chest_ranges_unobserved(self):
        self.open_chest(width=None)
        self.set_slot(2, 53, self.stack(PAPER, 3, 17))
        self.player_slot(9, self.stack(PAPER, 2, 18))
        for direction in ("take", "put"):
            with self.subTest(direction=direction):
                self.assertEqual(await self.transfer(1, direction=direction, chest=range(53, 54)),
                                 RangeTransferResult(1, (), "unobserved"))
                with self.assertRaisesRegex(ValueError, r"0\.\.54"):
                    await self.transfer(1, direction=direction, chest=range(54, 55))
        self.content(width=27)
        for direction in ("take", "put"):
            with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, r"0\.\.27"):
                await self.transfer(1, direction=direction, chest=range(53, 54))
        self.assertEqual(self.outgoing(), [])

    async def test_rejects_unsupported_blocks_widths_and_entity_windows_before_sending(self):
        for block, size, entity in (
            ("minecraft:trapped_chest", 54, -1), ("minecraft:chest", 28, -1),
            ("minecraft:barrel", 54, -1), ("minecraft:barrel", 27, -42),
        ):
            with self.subTest(block=block, size=size, entity=entity):
                self.observe_block(block)
                self.open_chest(self.stack(PAPER, 2, 17), width=size, entity_unique_id=entity)
                self.player_slot(9, self.stack())
                with self.assertRaisesRegex(RuntimeError, r"ordinary chest|unsupported slot count") as raised:
                    await self.transfer(1, chest=range(1), player=range(9, 10))
                self.assertEqual(raised.exception.transfer_progress, RangeTransferResult(1, (), "unconfirmed"))
                self.assertEqual(self.outgoing(), [])

    async def test_utility_block_ranges_and_whole_window_use_native_addresses(self):
        original_send = self.stream.send
        for window_type, block, runtime_id, width in UTILITY_LAYOUTS:
            with self.subTest(window_type=window_type):
                self.stream.send = original_send
                self.stream.sent.clear()
                self.observe_block(block, runtime_id=runtime_id)
                end = width - 1
                slots = [self.stack() for _ in range(width)]
                slots[0], slots[end] = self.stack(STICK, 4, 16), self.stack(PAPER, 63, 17)
                self.open_window(window_type, *slots, width=width)
                self.player_slot(35, self.stack(PAPER, 3, 31))
                self.answer(
                    ((end, 35, 3, 17, 31), [(CHEST, end, 60, 17), (PLAYER, 35, 6, 31)]),
                    ((35, end, 4, 31, 17), [(PLAYER, 35, 2, 31), (CHEST, end, 64, 17)]),
                    ((35, 1, 2, 31, 0), [(PLAYER, 35, 0, None), (CHEST, 1, 2, 32)]),
                )
                taken = await self.transfer(3, chest=range(end, width), player=range(35, 36))
                stored = await transfer_item(self.bot, direction="put", item="minecraft:paper", count=6)
                self.assertEqual((taken.confirmed_count, taken.stop_reason), (3, "complete"))
                self.assertEqual((stored.confirmed_count, stored.stop_reason), (6, "complete"))
                self.assertEqual((self.bot.container_slots[0].stack_size, self.bot.container_slots[1].stack_size,
                                  self.bot.container_slots[end].stack_size, self.bot.inventory_slots[35].stack_size),
                                 (4, 2, 64, 0))
                actions = []
                for packet in self.stream.sent:
                    encoded = self.bot.protocol.decode(self.bot.protocol.encode(packet))
                    request, = encoded.requests
                    action, = request.actions
                    actions.append((type(action).__name__, self.action(request), tuple(
                        (int(slot.full_container_name.name), slot.full_container_name.dynamic_id)
                        for slot in (action.source, action.destination)
                    )))
                self.assertEqual(actions, [
                    ("TakeActionData", (end, 35, 3, 17, 31), ((7, None), (12, None))),
                    ("PlaceActionData", (35, end, 4, 31, 17), ((12, None), (7, None))),
                    ("PlaceActionData", (35, 1, 2, 31, 0), ((12, None), (7, None))),
                ])

    async def test_utility_layouts_require_matching_full_content_and_enforce_bounds(self):
        self.player_slot(9, self.stack())
        for window_type, block, runtime_id, width in UTILITY_LAYOUTS:
            with self.subTest(window_type=window_type):
                self.observe_block(block, runtime_id=runtime_id)
                self.open_window(window_type, width=None)
                self.set_slot(2, width - 1, self.stack(PAPER, 3, 17))
                self.content(self.stack(PAPER, 3, 17), width=width, inventory_id=3)
                self.assertEqual(await self.transfer(1), RangeTransferResult(1, (), "unobserved"))

                waiting = self.start(transfer_item(self.bot, direction="take", item="minecraft:paper", count=1))
                await asyncio.sleep(0)
                self.assertFalse(waiting.done())
                self.content(width=width)
                self.set_slot(2, width, self.stack(PAPER, 3, 17))
                self.assertEqual(await asyncio.wait_for(waiting, 1), RangeTransferResult(1, (), "source_exhausted"))
                for direction in ("take", "put"):
                    with self.assertRaisesRegex(ValueError, rf"0\.\.{width}"):
                        await self.transfer(1, direction=direction, chest=range(width, width + 1))

                for wrong_width in (width - 1, width + 1):
                    self.content(self.stack(PAPER, 3, 17), width=wrong_width)
                    with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
                        await self.transfer(1)
                    with self.assertRaisesRegex(RuntimeError, "unsupported slot count"):
                        await transfer_item(self.bot, direction="take", item="minecraft:paper", count=1)
        self.assertEqual(self.outgoing(), [])

    async def test_utility_block_identity_and_hopper_power_are_checked_before_matching(self):
        match = Mock(return_value=True)
        self.player_slot(9, self.stack(PAPER, 2, 31))
        for window_type, block, runtime_id, width in UTILITY_LAYOUTS:
            with self.subTest(window_type=window_type):
                self.observe_block(block, runtime_id=runtime_id)
                for entity_unique_id in (0, -42):
                    self.open_window(window_type, width=width, entity_unique_id=entity_unique_id)
                    with self.assertRaisesRegex(RuntimeError, "block storage container|bound to a hopper minecart"):
                        await transfer_item(self.bot, direction="put", item="minecraft:paper", count=1, match=match)
                self.open_window(window_type, width=width)
                self.observe_block("minecraft:chest")
                with self.assertRaisesRegex(RuntimeError, block):
                    await transfer_item(self.bot, direction="put", item="minecraft:paper", count=1, match=match)

        position = (3, 100, 0)
        self.observe_block("minecraft:hopper", position, runtime_id=POWERED_HOPPER, layer=1)
        self.open_window("HOPPER", self.stack(PAPER, 2, 17), position=position, width=5)
        options = dict(direction="take", item="minecraft:paper", count=1, match=match)
        ranges = dict(source_slots=range(1), destination_slots=range(9, 10))
        self.assertEqual(await transfer_range(self.bot, **options, **ranges), RangeTransferResult(1, (), "unobserved"))
        self.assertEqual(await transfer_item(self.bot, **options), RangeTransferResult(1, (), "unobserved"))

        self.observe_block("minecraft:hopper", position, runtime_id=UNPOWERED_HOPPER)
        with self.assertRaisesRegex(RuntimeError, "powered"):
            await transfer_range(self.bot, **options, **ranges)
        with self.assertRaisesRegex(RuntimeError, "powered"):
            await transfer_item(self.bot, **options)
        match.assert_not_called()
        self.assertEqual(self.outgoing(), [])

        # Exact transfers retain their behavior when a block hopper is unpowered.
        self.answer(((0, 10, 1, 17, 0), [(CHEST, 0, 1, 17), (PLAYER, 10, 1, 41)]))
        exact = await self.bot.take_from_container(0, 10)
        self.assertEqual(exact.result.name, "SUCCESS")
        self.assertEqual(self.action_addresses(self.outgoing()[0]), (CHEST, PLAYER))

    async def test_utility_range_rechecks_binding_at_write_and_keeps_confirmed_prefix(self):
        original_send = self.stream.send
        for change, window_type, block, runtime_id, width in (
            ("power", *UTILITY_LAYOUTS[2]),
            ("window", *UTILITY_LAYOUTS[0]),
            ("block", *UTILITY_LAYOUTS[0]),
            ("width", *UTILITY_LAYOUTS[1]),
        ):
            with self.subTest(change=change):
                self.stream.send = original_send
                self.stream.sent.clear()
                self.observe_block(block, runtime_id=runtime_id)
                self.open_window(window_type, self.stack(PAPER, 1, 17), self.stack(PAPER, 3, 18), width=width)
                window = self.bot.open_window
                self.player_slot(9, self.stack())
                self.answer(
                    ((0, 9, 1, 17, 0), [(CHEST, 0, 0, None), (PLAYER, 9, 1, 31)]),
                    ((1, 9, 1, 18, 31), [(CHEST, 1, 2, 18), (PLAYER, 9, 2, 31)]),
                )
                send = self.stream.send
                entered, release = asyncio.Event(), asyncio.Event()

                async def hold_second(packet, **kwargs):
                    if self.outgoing():
                        entered.set()
                        await release.wait()
                    await send(packet, **kwargs)

                with patch.object(self.stream, "send", hold_second):
                    task = self.start(self.transfer(2))
                    await asyncio.wait_for(entered.wait(), 1)
                    slots = tuple(self.bot.container_slots[index] for index in range(width))
                    if change == "power":
                        self.observe_block(block, runtime_id=UNPOWERED_HOPPER)
                    elif change == "window":
                        self.open_window(window_type, *slots, width=width)
                        self.assertEqual(self.bot.open_window, window)
                        self.assertIsNot(self.bot.open_window, window)
                    elif change == "block":
                        self.observe_block("minecraft:dropper")
                    else:
                        self.content(*slots, width=width + 1)
                        self.content(*slots, width=width)
                    release.set()
                    result = await asyncio.wait_for(task, 1)

                request, = self.outgoing()
                self.assertEqual(result, RangeTransferResult(2, (
                    RangeTransferStep(0, 9, 1, 1, int(request.client_request_id.raw_id), "SUCCESS"),
                ), "window_changed"))
                self.assertEqual((self.bot.container_slots[1].stack_size, self.bot.inventory_slots[9].stack_size), (3, 1))

    async def test_sparse_slot_reports_do_not_establish_container_width(self):
        self.open_chest(width=None)
        self.set_slot(2, 0, self.stack(PAPER, 4, 17))
        self.player_slot(9, self.stack())
        self.assertEqual(await self.transfer(2), RangeTransferResult(2, (), "unobserved"))
        self.assertEqual(self.outgoing(), [])

    async def test_double_chest_ranges_use_server_indices_on_both_halves(self):
        slots = [self.stack() for _ in range(54)]
        slots[0], slots[53] = self.stack(STICK, 7, 71), self.stack(DIAMOND, 3, 72)
        slots[26], slots[27] = self.stack(PAPER, 1, 17), self.stack(PAPER, 64, 18)
        self.open_chest(*slots, width=54)
        self.player_slot(9, self.stack())
        self.answer(
            ((26, 9, 1, 17, 0), [(CHEST, 26, 0, None), (PLAYER, 9, 1, 41)]),
            ((27, 9, 2, 18, 41), [(CHEST, 27, 62, 18), (PLAYER, 9, 3, 41)]),
            ((9, 27, 2, 41, 18), [(PLAYER, 9, 1, 41), (CHEST, 27, 64, 18)]),
            ((9, 26, 1, 41, 0), [(PLAYER, 9, 0, None), (CHEST, 26, 1, 19)]),
        )
        for direction in ("take", "put"):
            result = await self.transfer(3, direction=direction, chest=range(26, 28), player=range(9, 10))
            self.assertEqual((result.stop_reason, result.confirmed_count), ("complete", 3))
        self.assertEqual([self.action_addresses(request) for request in self.outgoing()],
                         [(CHEST, PLAYER)] * 2 + [(PLAYER, CHEST)] * 2)
        self.assertEqual([self.bot.container_slots[slot].stack_size for slot in (0, 26, 27, 53)], [7, 1, 64, 3])
        with self.assertRaisesRegex(ValueError, r"0\.\.54"):
            await self.transfer(1, chest=range(54, 55))
        self.assertEqual(len(self.outgoing()), 4)

    async def test_width_change_preserves_confirmed_prefix_without_touching_replacement_slots(self):
        original_send = self.stream.send
        for returned_to_54 in (False, True):
            with self.subTest(returned_to_54=returned_to_54):
                self.stream.send = original_send
                self.stream.sent.clear()
                empties = [self.stack() for _ in range(26)]
                self.open_chest(*empties, self.stack(PAPER, 1, 17), self.stack(PAPER, 6, 18), width=54)
                self.player_slot(9, self.stack())
                window = self.bot.open_window
                pending = self.hold_requests()
                transfer = self.start(self.transfer(2, chest=range(26, 28), player=range(9, 10)))
                request = await asyncio.wait_for(pending.get(), 1)
                self.assertEqual(self.action(request), (26, 9, 1, 17, 0))
                self.content(*empties, self.stack(PAPER, 9, 51), width=27)
                if returned_to_54:
                    self.content(*empties, self.stack(PAPER, 9, 51), self.stack(PAPER, 11, 52), width=54)
                self.assertIs(self.bot.open_window, window)
                self.reply(request, [(CHEST, 26, 0, None), (PLAYER, 9, 1, 41)])
                result = await asyncio.wait_for(transfer, 1)
                self.assertEqual((result.stop_reason, result.confirmed_count), ("window_changed", 1))
                self.assertEqual((self.bot.container_slots[26].stack_size, self.bot.inventory_slots[9].stack_size), (9, 1))
                self.assertEqual(len(self.outgoing()), 1)

    async def test_unmapped_double_chest_arbitrates_possible_overlap_after_opposite_reopen(self):
        opposite = (POSITION[0] + 1, POSITION[1], POSITION[2])
        for position in (POSITION, opposite):
            self.observe_block("minecraft:chest", position, runtime_id=3162850062)
        slots = [self.stack() for _ in range(54)]
        slots[26], slots[27], slots[53] = self.stack(PAPER, 5, 17), self.stack(PAPER, 8, 18), self.stack(PAPER, 7, 19)
        self.open_chest(*slots, width=54)
        self.player_slot(9, self.stack())
        self.player_slot(10, self.stack())
        pending = self.hold_requests()
        old = self.start(self.transfer(1, chest=range(26, 27), player=range(9, 10)))
        old_request = await asyncio.wait_for(pending.get(), 1)
        self.open_chest(*slots, position=opposite, width=54)
        blocked = await self.transfer(1, chest=range(53, 54), player=range(10, 11))
        self.assertEqual(blocked, RangeTransferResult(1, (), "unconfirmed"))
        self.assertEqual(len(self.outgoing()), 1)
        independent = self.start(self.transfer(1, chest=range(27, 28), player=range(10, 11)))
        request = await asyncio.wait_for(pending.get(), 1)
        self.assertEqual(self.action(request), (27, 10, 1, 18, 0))
        self.reply(request, [(CHEST, 27, 7, 18), (PLAYER, 10, 1, 31)])
        self.reply(old_request, [(CHEST, 26, 4, 17), (PLAYER, 9, 1, 41)])
        self.assertEqual((await independent).confirmed_count, 1)
        self.assertEqual((await old).confirmed_count, 1)
        self.assertEqual([self.bot.container_slots[index].stack_size for index in (26, 27, 53)], [5, 7, 7])

    async def test_unknown_window_block_is_unobserved_and_sends_nothing(self):
        self.open_chest(self.stack(PAPER, 2, 17), position=(2, 100, 0))
        self.player_slot(9, self.stack())
        result = await self.transfer(1, chest=range(1), player=range(9, 10))
        self.assertEqual(result, RangeTransferResult(1, (), "unobserved"))
        self.assertEqual(self.outgoing(), [])
