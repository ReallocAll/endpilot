import asyncio
import struct
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.harvest import HarvestTool
from endpilot.login import JoinSession

from test_blocks import PacketStream


POSITION = (4, 99, -9)
DIAMOND_ORE = 0x54A1D0FD
IRON_ORE = 0x79275ADE
DIRT = 0x824EEF86
OAK_LOG = 0xF253CAD8
NETHER_WART_BLOCK = 0x22A97687
HAY_BLOCK = 0x5A4A90AB
WHITE_WOOL = 0x9096F28E
WEB = 0x38FA6D0A


def enchantment_data(binding, level, *, enchantment=None):
    enchantment = binding.Enchant.Type.EFFICIENCY if enchantment is None else enchantment
    return (b"\xff\xff\x01\x0a\x00\x00\x09\x04\x00ench\x0a\x01\x00\x00\x00\x02\x02\x00id"
            + struct.pack("<h", int(enchantment)) + b"\x02\x03\x00lvl" + struct.pack("<h", level) + b"\x00\x00")


class HarvestTests(unittest.IsolatedAsyncioTestCase):
    async def connect(self, *, protocol=2225, hashed=True, custom=False):
        bot = Bot("HarvestTest", protocol=protocol)
        self.stream = PacketStream(bot.protocol)
        self.binding = bot.protocol.binding
        session = JoinSession(self.stream, bot.identity, "127.0.0.1")
        session.player_id = session.player_unique_id = 1
        session.block_network_ids_are_hashes = hashed
        session.has_custom_blocks = custom
        # Use different session IDs and give the catalog's iron ID to an add-on.
        session.item_names = {
            1200: "minecraft:golden_pickaxe",
            1201: "minecraft:iron_pickaxe",
            1202: "minecraft:golden_shovel",
            1203: "minecraft:shears",
            1204: "minecraft:iron_axe",
            1205: "minecraft:diamond_pickaxe",
            1206: "minecraft:iron_shovel",
            1207: "minecraft:diamond_axe",
            1208: "minecraft:diamond_shovel",
            1209: "minecraft:iron_hoe",
            1210: "minecraft:diamond_hoe",
            1211: "minecraft:iron_sword",
            1212: "minecraft:copper_pickaxe",
            299: "test:iron_pickaxe",
        }
        self.stream.close = self.close_stream
        with patch.object(JoinSession, "connect_raknet", return_value=session):
            await bot.join_raknet("127.0.0.1")
        self.addAsyncCleanup(bot.close)
        return bot

    async def close_stream(self):
        pass

    def emit(self, packet):
        self.stream.emit(self.stream.protocol.encode(packet))

    def block(self, runtime_id):
        x, y, z = POSITION
        self.emit(self.binding.UpdateBlockPacket(
            pos=self.binding.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id,
            update_flags=0, layer=0,
        ))

    def inventory_slot(self, slot, item_id, count=1, *, user_data=b""):
        b = self.binding
        item = b.SerializedNetworkItemStackDescriptor(
            id=item_id, stack_size=count if item_id else 0, aux_value=0,
            net_id_variant=slot + 100 if item_id else None, block_runtime_id=0,
            user_data_buffer=user_data,
        )
        self.emit(b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=slot,
                                        full_container_name=None, storage_item=None, item=item))

    def open_window(self, container_type, container_id):
        b = self.binding
        self.emit(b.ContainerOpenPacket(
            container_id=container_id, type=container_type,
            pos=b.BlockPos(x=0, y=0, z=0), entity_unique_id=-1,
        ))

    def reply_to_transfers(self, bot, result_name, *, corrections=True, answer_open=True):
        b = self.binding
        original_send = self.stream.send

        async def send(packet):
            await original_send(packet)
            if isinstance(packet, b.InteractPacket):
                self.assertEqual(packet.action, b.InteractPacket.Action.OPEN_INVENTORY)
                if answer_open:
                    self.open_window(b.ContainerType.INVENTORY, 2)
                return
            if isinstance(packet, b.ContainerClosePacket):
                self.assertEqual(int(packet.container_id), 2)
                self.emit(packet)
                return
            if not isinstance(packet, b.ItemStackRequestPacket):
                return
            request = packet.requests[0]
            action = request.actions[0]
            slots = bot.inventory_slots
            source, destination = int(action.source.slot), int(action.destination.slot)
            self.assertIsNotNone(bot.open_window)
            if slots[destination].id == 0:
                self.assertIsInstance(action, b.ItemStackRequestCereal.PlaceActionData)
                self.assertEqual(int(action.amount), int(slots[source].stack_size))
            else:
                self.assertIsInstance(action, b.ItemStackRequestCereal.SwapActionData)
            response_slots = []
            for index, previous in ((source, destination), (destination, source)):
                item = slots[previous]
                self.assertEqual(int(item.net_id_variant or 0),
                                 int((action.destination if index == source else action.source).net_id_variant))
                response_slots.append(b.ItemStackResponseSlotInfo(
                    requested_slot=index, slot=index, amount=int(item.stack_size),
                    item_stack_net_id=b.ItemStackNetId(raw_id=index + 500) if item.id else None,
                    custom_name=b.RedactableString(unredacted_string="", redacted_string=None),
                    durability_correction=0,
                ))
            containers = [b.ItemStackResponseContainerInfo(
                full_container_name=action.source.full_container_name, slots=response_slots,
            )] if result_name == "SUCCESS" and corrections else None
            self.emit(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
                result=getattr(b.ItemStackNetResult, result_name), client_request_id=request.client_request_id,
                containers=containers,
            )]))

        self.stream.send = send

    async def test_eligibility_precedes_speed_and_uses_session_item_names(self):
        bot = await self.connect()
        self.inventory_slot(0, 1200)
        self.inventory_slot(1, 299)
        self.block(DIAMOND_ORE)
        self.assertIsNone(bot.best_harvest_tool(POSITION))
        self.assertIsNone(await bot.equip_harvest_tool(POSITION))
        self.assertEqual(self.stream.sent, [])
        self.inventory_slot(12, 1201)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(12, "minecraft:iron_pickaxe", 6))
        self.block(0x80310E21)  # Stone admits the faster gold tool.
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(0, "minecraft:golden_pickaxe", 12))

    async def test_clear_tool_compares_progress_with_and_without_a_drop(self):
        bot = await self.connect()
        self.block(IRON_ORE)
        gold = HarvestTool(1, "minecraft:golden_pickaxe", 12)
        copper = HarvestTool(2, "minecraft:copper_pickaxe", 5)
        self.inventory_slot(1, 1200)
        self.assertIsNone(bot.best_harvest_tool(POSITION))
        self.assertIsNone(await bot.equip_harvest_tool(POSITION, require_harvest=True))
        self.assertEqual(bot.best_harvest_tool(POSITION, require_harvest=False), gold)

        self.inventory_slot(2, 1212)
        self.assertEqual(bot.best_harvest_tool(POSITION, require_harvest=False), copper)
        self.inventory_slot(1, 1200, user_data=enchantment_data(self.binding, 2))
        self.assertEqual(bot.best_harvest_tool(POSITION), copper)
        self.assertEqual(bot.best_harvest_tool(POSITION, require_harvest=True), copper)
        self.assertEqual(bot.best_harvest_tool(POSITION, require_harvest=False), gold)
        self.assertEqual(self.stream.sent, [])

    async def test_clear_tool_ties_favor_empty_hands_then_selected_slot_then_index(self):
        bot = await self.connect()
        self.block(IRON_ORE)
        self.inventory_slot(0, 1206, user_data=enchantment_data(self.binding, 5))
        self.inventory_slot(1, 0)
        self.assertIsNone(bot.best_harvest_tool(POSITION))
        self.assertEqual(bot.best_harvest_tool(POSITION, require_harvest=False), HarvestTool(None, None, 1))
        self.assertEqual(await bot.equip_harvest_tool(POSITION, require_harvest=False), HarvestTool(1, None, 1))
        self.assertEqual(bot.held_item.slot, 1)
        self.assertEqual(bot.inventory_slots[1].id, 0)

        self.inventory_slot(4, 1200)
        self.inventory_slot(2, 1200)
        self.assertEqual(bot.best_harvest_tool(POSITION, require_harvest=False),
                         HarvestTool(2, "minecraft:golden_pickaxe", 12))
        await bot.select_hotbar(4)
        self.assertEqual(bot.best_harvest_tool(POSITION, require_harvest=False),
                         HarvestTool(4, "minecraft:golden_pickaxe", 12))

    async def test_clear_tool_with_a_full_inventory_can_swap_but_cannot_select_bare_hands(self):
        bot = await self.connect()
        self.block(IRON_ORE)
        for slot in range(36):
            self.inventory_slot(slot, 299)
        self.assertEqual(bot.best_harvest_tool(POSITION, require_harvest=False), HarvestTool(None, None, 1))
        self.assertIsNone(await bot.equip_harvest_tool(POSITION, require_harvest=False))
        self.assertEqual(self.stream.sent, [])

        self.inventory_slot(0, 1212)
        enchanted = enchantment_data(self.binding, 2)
        self.inventory_slot(12, 1200, user_data=enchanted)
        self.reply_to_transfers(bot, "SUCCESS")
        self.assertEqual(await bot.equip_harvest_tool(POSITION, require_harvest=False),
                         HarvestTool(0, "minecraft:golden_pickaxe", 12))
        self.assertEqual([type(packet) for packet in self.stream.sent], [
            self.binding.InteractPacket, self.binding.ItemStackRequestPacket,
            self.binding.ContainerClosePacket, self.binding.MobEquipmentPacket,
        ])
        self.assertEqual(bot.inventory_slots[0].id, 1200)
        self.assertEqual(bot.inventory_slots[0].user_data_buffer, enchanted)
        self.assertEqual(bot.inventory_slots[12].id, 1212)
        self.assertTrue(all(bot.inventory_slots[slot].id == 299 for slot in range(36) if slot not in (0, 12)))
        self.assertIsNone(bot.open_window)

    async def test_efficiency_preserves_eligibility_ties_and_base_speed(self):
        bot = await self.connect()
        b = self.binding
        self.block(DIAMOND_ORE)
        self.inventory_slot(1, 1200, user_data=enchantment_data(b, 5))
        self.assertIsNone(bot.best_harvest_tool(POSITION))
        self.inventory_slot(0, 1205)
        self.inventory_slot(12, 1201, user_data=enchantment_data(b, 1))
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(0, "minecraft:diamond_pickaxe", 8))
        for level in range(2, 6):
            with self.subTest(level=level):
                self.inventory_slot(0, 1205, user_data=enchantment_data(b, level - 1))
                self.inventory_slot(12, 1201, user_data=enchantment_data(b, level))
                self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(12, "minecraft:iron_pickaxe", 6))

        self.block(0x80310E21)  # Compare a speed-12 gold tool on stone.
        self.inventory_slot(0, 1205)
        self.inventory_slot(1, 1200)
        self.inventory_slot(12, 1201, user_data=enchantment_data(b, 2))
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(1, "minecraft:golden_pickaxe", 12))
        self.inventory_slot(12, 1201, user_data=enchantment_data(b, 3))
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(12, "minecraft:iron_pickaxe", 6))
        self.assertEqual(self.stream.sent, [])

    async def test_efficiency_is_limited_to_matching_tool_categories(self):
        bot = await self.connect()
        b = self.binding
        self.inventory_slot(0, 1201, user_data=enchantment_data(b, 5))
        self.inventory_slot(1, 1206, user_data=enchantment_data(b, 2))
        self.block(OAK_LOG)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(None, None, 1))
        self.inventory_slot(2, 1207)
        self.inventory_slot(12, 1204, user_data=enchantment_data(b, 2))
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(12, "minecraft:iron_axe", 6))
        self.block(DIRT)
        self.inventory_slot(3, 1208)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(1, "minecraft:iron_shovel", 6))

        self.block(0x3EE83EB2)  # Oak leaves.
        self.inventory_slot(0, 1203)
        self.inventory_slot(4, 1203, user_data=enchantment_data(b, 5))
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(4, "minecraft:shears", 15))
        self.assertEqual(self.stream.sent, [])

    async def test_efficiency_shears_rank_only_where_they_have_a_base_speed_bonus(self):
        bot = await self.connect()
        self.inventory_slot(0, 1211)
        self.inventory_slot(4, 1203)
        self.inventory_slot(12, 1203)
        self.block(WHITE_WOOL)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(4, "minecraft:shears", 5))
        self.block(WEB)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(0, "minecraft:iron_sword", 15))

        self.inventory_slot(12, 1203, user_data=enchantment_data(self.binding, 2))
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(12, "minecraft:shears", 15))
        self.block(WHITE_WOOL)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(12, "minecraft:shears", 5))
        self.block(OAK_LOG)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(None, None, 1))
        self.assertEqual(self.stream.sent, [])

    async def test_efficiency_hoe_ranks_only_for_its_material(self):
        bot = await self.connect()
        self.inventory_slot(0, 1210)
        self.inventory_slot(12, 1209)
        for block in (NETHER_WART_BLOCK, HAY_BLOCK):
            self.block(block)
            self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(0, "minecraft:diamond_hoe", 8))

        self.inventory_slot(12, 1209, user_data=enchantment_data(self.binding, 2))
        for block in (NETHER_WART_BLOCK, HAY_BLOCK):
            with self.subTest(block=block):
                self.block(block)
                self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(12, "minecraft:iron_hoe", 6))

        self.block(OAK_LOG)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(None, None, 1))
        self.assertEqual(self.stream.sent, [])

    async def test_non_efficiency_and_out_of_range_or_invalid_metadata_keep_base_speed(self):
        bot = await self.connect()
        b = self.binding
        self.block(DIAMOND_ORE)
        self.inventory_slot(0, 1205)
        for data in (enchantment_data(b, 6), enchantment_data(b, 3, enchantment=b.Enchant.Type.UNBREAKING),
                     b"invalid NBT"):
            with self.subTest(data=data):
                self.inventory_slot(12, 1201, user_data=data)
                self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(0, "minecraft:diamond_pickaxe", 8))

    async def test_equips_enchanted_variant_when_swap_changes_the_network_stack_id(self):
        bot = await self.connect()
        b = self.binding
        self.block(DIAMOND_ORE)
        self.inventory_slot(0, 1201)
        enchanted = enchantment_data(b, 2)
        self.inventory_slot(12, 1201, user_data=enchanted)
        self.reply_to_transfers(bot, "SUCCESS")

        self.assertEqual(await bot.equip_harvest_tool(POSITION), HarvestTool(0, "minecraft:iron_pickaxe", 6))
        self.assertEqual(bot.inventory_slots[0].net_id_variant, 500)
        self.assertEqual(bot.inventory_slots[0].user_data_buffer, enchanted)
        self.assertEqual(self.stream.sent[-1].item.user_data_buffer, enchanted)
        self.assertEqual(bot.inventory_slots[12].user_data_buffer, b"")

    async def test_equip_rejects_predicted_empty_hand_before_sending_equipment(self):
        bot = await self.connect()
        b = self.binding
        self.block(DIAMOND_ORE)
        self.inventory_slot(0, 1201, user_data=enchantment_data(b, 2))
        await bot.drop_selected_item()
        drop, = self.stream.sent
        self.assertIsInstance(drop, b.InventoryTransactionPacket)
        self.assertEqual(drop.variant_transaction.transaction.actions[1].to_item_descriptor.id, 0)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(0, "minecraft:iron_pickaxe", 6))

        with self.assertRaisesRegex(RuntimeError, "changed before selecting"):
            await bot.equip_harvest_tool(POSITION)
        self.assertEqual(self.stream.sent, [drop])

    async def test_equip_rejects_a_replacement_network_id_after_transfer_confirmation(self):
        for packet_type_name, expected_error in (("ItemStackRequestPacket", "changed before selecting"),
                                                 ("ContainerClosePacket", "changed before selecting"),
                                                 ("MobEquipmentPacket", "changed while equipping")):
            with self.subTest(replaced_after=packet_type_name):
                bot = await self.connect()
                b = self.binding
                self.block(DIAMOND_ORE)
                enchanted = enchantment_data(b, 2)
                self.inventory_slot(0, 0)
                self.inventory_slot(12, 1201, user_data=enchanted)
                self.reply_to_transfers(bot, "SUCCESS")
                original_send = self.stream.send

                async def send(packet):
                    await original_send(packet)
                    if isinstance(packet, getattr(b, packet_type_name)):
                        self.assertEqual(bot.inventory_slots[0].net_id_variant, 500)
                        self.inventory_slot(0, 1201, user_data=enchanted)

                self.stream.send = send
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    await bot.equip_harvest_tool(POSITION)
                self.assertEqual(bot.inventory_slots[0].net_id_variant, 100)
                self.assertEqual(bot.inventory_slots[0].user_data_buffer, enchanted)
                self.assertIsNone(bot.open_window)
                expected_packets = [b.InteractPacket, b.ItemStackRequestPacket, b.ContainerClosePacket]
                if packet_type_name == "MobEquipmentPacket":
                    expected_packets.append(b.MobEquipmentPacket)
                    self.assertEqual(self.stream.sent[-1].item.net_id_variant, 500)
                self.assertEqual([type(packet) for packet in self.stream.sent], expected_packets)

    async def test_equipping_different_enchantment_with_same_item_id_is_rejected(self):
        for when, expected_error in (("opening", "changed before"), ("closing", "changed before selecting"),
                                     ("selecting", "changed while")):
            with self.subTest(when=when):
                bot = await self.connect()
                b = self.binding
                self.block(DIAMOND_ORE)
                source = 0 if when == "selecting" else 12
                self.inventory_slot(0, 0)
                self.inventory_slot(source, 1201, user_data=enchantment_data(b, 2))
                self.reply_to_transfers(bot, "SUCCESS")
                original_send = self.stream.send

                async def send(packet):
                    await original_send(packet)
                    if when == "opening" and isinstance(packet, b.InteractPacket):
                        self.inventory_slot(source, 1201, user_data=enchantment_data(b, 1))
                    if (when == "closing" and isinstance(packet, b.ContainerClosePacket)
                            or when == "selecting" and isinstance(packet, b.MobEquipmentPacket)):
                        self.inventory_slot(0, 1201, user_data=enchantment_data(b, 1))

                self.stream.send = send
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    await bot.equip_harvest_tool(POSITION)
                self.assertIsNone(bot.open_window)
                sent = [type(packet) for packet in self.stream.sent]
                if when == "opening":
                    self.assertNotIn(b.ItemStackRequestPacket, sent)
                if when != "selecting":
                    self.assertNotIn(b.MobEquipmentPacket, sent)

    async def test_equips_from_inventory_after_successful_swap_and_preserves_stacks(self):
        bot = await self.connect()
        self.inventory_slot(0, 1200)
        self.inventory_slot(2, 1203, 2)
        self.inventory_slot(12, 1201)
        self.block(DIAMOND_ORE)
        self.open_window(self.binding.ContainerType.CONTAINER, 5)
        existing_window = bot.open_window
        self.reply_to_transfers(bot, "SUCCESS")

        self.assertEqual(await bot.equip_harvest_tool(POSITION), HarvestTool(0, "minecraft:iron_pickaxe", 6))
        swap, select = self.stream.sent
        self.assertEqual(int(swap.requests[0].actions[0].source.slot), 12)
        self.assertIsInstance(select, self.binding.MobEquipmentPacket)
        self.assertEqual(int(select.selected_slot), 0)
        self.assertEqual((select.item.id, select.item.net_id_variant), (1201, 500))
        self.assertEqual([(item.id, item.stack_size, item.net_id_variant)
                          for item in (bot.inventory_slots[s] for s in (0, 2, 12))],
                         [(1201, 1, 500), (1203, 2, 102), (1200, 1, 512)])
        self.assertIs(bot.open_window, existing_window)

    async def test_equips_into_an_empty_hotbar_slot_and_closes_its_own_inventory(self):
        bot = await self.connect()
        self.inventory_slot(0, 1200)
        self.inventory_slot(2, 0)
        self.inventory_slot(12, 1201)
        self.block(DIAMOND_ORE)
        self.reply_to_transfers(bot, "SUCCESS")
        self.assertEqual(await bot.equip_harvest_tool(POSITION), HarvestTool(2, "minecraft:iron_pickaxe", 6))
        self.assertEqual([type(packet) for packet in self.stream.sent], [
            self.binding.InteractPacket, self.binding.ItemStackRequestPacket,
            self.binding.ContainerClosePacket, self.binding.MobEquipmentPacket,
        ])
        self.assertEqual((bot.inventory_slots[0].id, bot.inventory_slots[2].id, bot.inventory_slots[12].id),
                         (1200, 1201, 0))
        self.assertIsNone(bot.open_window)

    async def test_cancellation_after_open_packet_closes_only_its_own_window(self):
        for replace_at in (None, "cancellation", "close_waiter"):
            with self.subTest(replace_at=replace_at):
                bot = await self.connect()
                b = self.binding
                self.inventory_slot(2, 0)
                self.inventory_slot(12, 1201)
                self.block(DIAMOND_ORE)
                original_send = self.stream.send
                original_wait = self.stream.wait_for_packet
                replacement = None

                def replace_inventory_window():
                    nonlocal replacement
                    initial = bot.open_window
                    self.open_window(b.ContainerType.INVENTORY, 2)
                    replacement = bot.open_window
                    self.assertEqual(replacement, initial)
                    self.assertIsNot(replacement, initial)

                async def wait(packet_id, *, timeout, match=None):
                    if replace_at == "close_waiter" and packet_id == b.ContainerClosePacket.ID:
                        replace_inventory_window()
                    return await original_wait(packet_id, timeout=timeout, match=match)

                async def send(packet):
                    await original_send(packet)
                    if isinstance(packet, b.InteractPacket):
                        self.open_window(b.ContainerType.INVENTORY, 2)
                        self.assertIsNotNone(bot.open_window)
                        if replace_at == "cancellation":
                            replace_inventory_window()
                        task.cancel()
                    elif isinstance(packet, b.ContainerClosePacket):
                        self.assertEqual(int(packet.container_id), 2)
                        self.emit(packet)

                self.stream.send = send
                self.stream.wait_for_packet = wait
                task = asyncio.create_task(bot.equip_harvest_tool(POSITION))
                with self.assertRaises(asyncio.CancelledError):
                    await task
                sent = [type(packet) for packet in self.stream.sent]
                if replace_at is not None:
                    self.assertEqual(sent, [b.InteractPacket])
                    self.assertIsNotNone(replacement)
                    self.assertIs(bot.open_window, replacement)
                else:
                    self.assertEqual(sent, [b.InteractPacket, b.ContainerClosePacket])
                    self.assertIsNone(bot.open_window)

    async def test_cancellation_does_not_close_an_existing_window(self):
        bot = await self.connect()
        b = self.binding
        self.inventory_slot(2, 0)
        self.inventory_slot(12, 1201)
        self.block(DIAMOND_ORE)
        self.open_window(b.ContainerType.CONTAINER, 5)
        existing = bot.open_window
        original_send = self.stream.send

        async def send(packet):
            await original_send(packet)
            if isinstance(packet, b.ItemStackRequestPacket):
                task.cancel()

        self.stream.send = send
        task = asyncio.create_task(bot.equip_harvest_tool(POSITION))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([type(packet) for packet in self.stream.sent], [b.ItemStackRequestPacket])
        self.assertIs(bot.open_window, existing)

    async def test_equip_borrows_a_pending_public_inventory_open(self):
        for cancel_at in (None, "open_wait", "transfer"):
            with self.subTest(cancel_at=cancel_at):
                bot = await self.connect()
                b = self.binding
                self.inventory_slot(2, 0)
                self.inventory_slot(12, 1201)
                self.block(DIAMOND_ORE)
                self.reply_to_transfers(bot, "SUCCESS", answer_open=False)
                original_send = self.stream.send
                submitted = asyncio.Event()

                async def send(packet):
                    if cancel_at == "transfer" and isinstance(packet, b.ItemStackRequestPacket):
                        self.stream.sent.append(packet)
                        equip.cancel()
                        return
                    await original_send(packet)
                    if isinstance(packet, b.InteractPacket):
                        submitted.set()

                self.stream.send = send
                opening = asyncio.create_task(bot.open_inventory_screen(timeout=1))
                await asyncio.wait_for(submitted.wait(), 1)
                equip = asyncio.create_task(bot.equip_harvest_tool(POSITION, timeout=1))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                expected = [b.InteractPacket]
                if cancel_at == "open_wait":
                    equip.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await equip
                    self.assertFalse(opening.done())
                self.open_window(b.ContainerType.INVENTORY, 2)
                caller_window = bot.open_window
                self.assertEqual(await opening, caller_window)
                if cancel_at == "transfer":
                    with self.assertRaises(asyncio.CancelledError):
                        await equip
                    expected.append(b.ItemStackRequestPacket)
                elif cancel_at is None:
                    self.assertEqual(await equip, HarvestTool(2, "minecraft:iron_pickaxe", 6))
                    expected.extend((b.ItemStackRequestPacket, b.MobEquipmentPacket))
                self.assertEqual([type(packet) for packet in self.stream.sent], expected)
                self.assertIs(bot.open_window, caller_window)

    async def test_public_inventory_open_is_rejected_while_equip_owns_the_pending_open(self):
        bot = await self.connect()
        b = self.binding
        self.inventory_slot(2, 0)
        self.inventory_slot(12, 1201)
        self.block(DIAMOND_ORE)
        self.reply_to_transfers(bot, "SUCCESS", answer_open=False)
        original_send = self.stream.send
        submitted = asyncio.Event()

        async def send(packet):
            await original_send(packet)
            if isinstance(packet, b.InteractPacket):
                submitted.set()

        self.stream.send = send
        equip = asyncio.create_task(bot.equip_harvest_tool(POSITION, timeout=1))
        await asyncio.wait_for(submitted.wait(), 1)
        try:
            with self.assertRaisesRegex(RuntimeError, "open already in progress"):
                await bot.open_inventory_screen(timeout=0.05)
        finally:
            self.open_window(b.ContainerType.INVENTORY, 2)
            self.assertEqual(await equip, HarvestTool(2, "minecraft:iron_pickaxe", 6))
        self.assertEqual([type(packet) for packet in self.stream.sent], [
            b.InteractPacket, b.ItemStackRequestPacket, b.ContainerClosePacket, b.MobEquipmentPacket,
        ])
        self.assertIsNone(bot.open_window)

    async def test_cancellation_waits_for_an_inflight_inventory_open_within_deadline(self):
        for reply in (True, False):
            with self.subTest(reply=reply):
                bot = await self.connect()
                b = self.binding
                self.inventory_slot(2, 0)
                self.inventory_slot(12, 1201)
                self.block(DIAMOND_ORE)
                submitted = asyncio.Event()
                original_send = self.stream.send

                async def send(packet):
                    await original_send(packet)
                    if isinstance(packet, b.InteractPacket):
                        self.open_window(b.ContainerType.CONTAINER, 5)
                        submitted.set()
                    elif isinstance(packet, b.ContainerClosePacket):
                        self.assertEqual(int(packet.container_id), 2)
                        self.emit(packet)

                self.stream.send = send
                timeout = 1 if reply else 0.05
                task = asyncio.create_task(bot.equip_harvest_tool(POSITION, timeout=timeout))
                await asyncio.wait_for(submitted.wait(), 1)
                existing = bot.open_window
                task.cancel("stop equipping")
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                if reply:
                    self.open_window(b.ContainerType.INVENTORY, 2)
                with self.assertRaisesRegex(asyncio.CancelledError, "stop equipping") as cancelled:
                    await task
                sent = [type(packet) for packet in self.stream.sent]
                if reply:
                    self.assertEqual(sent, [b.InteractPacket, b.ContainerClosePacket])
                    self.assertIsNone(bot.open_window)
                else:
                    self.assertEqual(sent, [b.InteractPacket])
                    self.assertIs(bot.open_window, existing)
                    self.assertIn("player inventory open unconfirmed", " ".join(cancelled.exception.__notes__))

    async def test_transfer_rejection_or_missing_corrections_never_selects_hotbar(self):
        for result, corrections, error in (("ERROR", True, "rejected"), ("SUCCESS", False, "confirm")):
            with self.subTest(result=result):
                bot = await self.connect()
                self.inventory_slot(2, 0)
                self.inventory_slot(12, 1201)
                self.block(DIAMOND_ORE)
                self.reply_to_transfers(bot, result, corrections=corrections)
                with self.assertRaisesRegex(RuntimeError, error):
                    await bot.equip_harvest_tool(POSITION)
                self.assertEqual([type(packet) for packet in self.stream.sent], [
                    self.binding.InteractPacket, self.binding.ItemStackRequestPacket, self.binding.ContainerClosePacket,
                ])
                self.assertIsNone(bot.open_window)
                self.assertEqual((bot.inventory_slots[2].id, bot.inventory_slots[12].id), (0, 1201))

    async def test_hands_use_a_known_empty_slot(self):
        bot = await self.connect()
        self.inventory_slot(0, 1200)
        self.block(DIRT)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(None, None, 1))
        self.assertIsNone(await bot.equip_harvest_tool(POSITION))
        self.assertEqual(self.stream.sent, [])
        self.inventory_slot(3, 0)
        self.assertEqual(await bot.equip_harvest_tool(POSITION), HarvestTool(3, None, 1))
        self.assertEqual([int(packet.selected_slot) for packet in self.stream.sent], [3])
        self.assertEqual(bot.inventory_slots[0].id, 1200)

    async def test_hands_can_stow_the_selected_stack(self):
        bot = await self.connect()
        self.inventory_slot(0, 1200)
        self.inventory_slot(12, 0)
        self.block(DIRT)
        self.reply_to_transfers(bot, "SUCCESS")
        self.assertEqual(await bot.equip_harvest_tool(POSITION), HarvestTool(0, None, 1))
        self.assertEqual((bot.inventory_slots[0].id, bot.inventory_slots[12].id), (0, 1200))

    async def test_uses_speed_data_for_other_tool_types_and_state_kinds(self):
        bot = await self.connect()
        for slot, item_id in enumerate((1202, 1203, 1204)):
            self.inventory_slot(slot, item_id)
        for runtime_id, expected in (
            (DIRT, HarvestTool(0, "minecraft:golden_shovel", 12)),
            (0x3EE83EB2, HarvestTool(1, "minecraft:shears", 15)),  # Oak leaves with byte states.
            (0xF253CAD8, HarvestTool(2, "minecraft:iron_axe", 6)),  # Oak log with string state.
            (WEB, HarvestTool(1, "minecraft:shears", 15)),  # Web requires a tool.
        ):
            self.block(runtime_id)
            self.assertEqual(bot.best_harvest_tool(POSITION), expected)

    async def test_unknown_and_unbreakable_blocks_have_no_candidate(self):
        bot = await self.connect()
        self.inventory_slot(0, 1201)
        self.assertIsNone(bot.best_harvest_tool(POSITION))
        for runtime_id in (10, 0xFFFFFFFE, 0xF5AC7CFB, 0xDBF44120):
            with self.subTest(runtime_id=runtime_id):
                self.block(runtime_id)
                self.assertIsNone(bot.best_harvest_tool(POSITION))
                self.assertIsNone(await bot.equip_harvest_tool(POSITION))
                self.assertIsNone(bot.best_harvest_tool(POSITION, require_harvest=False))
                self.assertIsNone(await bot.equip_harvest_tool(POSITION, require_harvest=False))
        self.assertEqual(self.stream.sent, [])

    async def test_vanilla_palette_rejects_unknown_and_custom_registries(self):
        bot = await self.connect(hashed=False)
        self.inventory_slot(0, 1201)
        self.block(10489)
        self.assertEqual(bot.best_harvest_tool(POSITION), HarvestTool(0, "minecraft:iron_pickaxe", 6))
        self.block(11244)  # The vanilla unknown block placeholder.
        self.assertIsNone(bot.best_harvest_tool(POSITION))
        bot = await self.connect(hashed=False, custom=True)
        self.inventory_slot(0, 1201)
        self.block(10489)
        self.assertIsNone(bot.best_harvest_tool(POSITION))

    async def test_other_protocols_have_no_harvest_catalog(self):
        bot = await self.connect(protocol=2193)
        self.inventory_slot(0, 1201)
        self.block(DIAMOND_ORE)
        self.assertIsNone(bot.best_harvest_tool(POSITION))
        self.assertIsNone(await bot.equip_harvest_tool(POSITION))
        self.assertEqual(self.stream.sent, [])
