"""Observe protocol 2225 packets through the public explosion estimate."""

import asyncio
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot
from endpilot.protocol import RawPacket, read_batch, write_batch

from test_harvest import enchantment_data
from test_join import FakeTransport


_CENTER = (9.5, 220.5, 8.5)
_RADIUS = 2.25
# Windows vanilla 1.26.66.1, archived range/equipment and range-equality runs.
_FAR_MOVE = "0100004641b99e5d43000014410000000000000000000000000201000103000000010000000a"
_EQUAL_MOVE = "0100005441b99e5d4300000841000000000000000000000000020100010300000001000000ac01"
_COVER_MOVE = "0100003841b99e5d430000084100000000000000000000000002010001030000000100000046"


class GameHealthEstimateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("GameHealthEstimate", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def attributes(self, actor=1, **values):
        return self.b.UpdateAttributesPacket(runtime_id=actor, attribute_data=[
            self.b.AttributeData(name=f"minecraft:{name}", current_value=value)
            for name, value in values.items()
        ])

    def metadata(self, *, low=0, high=0, size=True, remote=False):
        b = self.b
        fields = [(0, b.DataItemInt64Payload, low), (92, b.DataItemInt64Payload, high)]
        if size:
            if remote:
                fields.extend([(53, b.DataItemFloatPayload, 0.6), (54, b.DataItemFloatPayload, 1.8)])
            else:
                fields.append((b.ActorDataIDs.COLLISION_BOX, b.DataItemVec3Payload, b.Vec3(x=0.6, y=1.8)))
        return b.SynchedActorData.CopyableDataList(data=[
            b.DataItemEntry(id=index, payload=payload(value=value))
            for index, payload, value in fields if value is not None
        ])

    def armor(self, *slots):
        empty = self.b.SerializedNetworkItemStackDescriptor()
        return self.b.InventoryContentPacket(inventory_id=self.b.ContainerID.ARMOR, slots=slots or [empty] * 4)

    def stack(self, item_id, user_data=bytes(10)):
        return self.b.SerializedNetworkItemStackDescriptor(
            id=item_id, stack_size=1, net_id_variant=item_id, user_data_buffer=user_data,
        )

    def terrain(self, dimension=0):
        b = self.b
        return b.SubChunkPacket(
            dimension_type=dimension, center_pos=b.SubChunkPos(x=0, y=13, z=0),
            sub_chunk_data=[b.SubChunkPacket.SubChunkPacketData(
                result=b.SubChunkPacket.SubChunkRequestResult.SUCCESS_ALL_AIR,
            )],
        )

    def move(self, x=12.375, z=9.25):
        return self.b.MovePlayerPacket(
            player_id=1, pos=self.b.Vec3(x=x, y=221.62001037597656, z=z), on_ground=True,
        )

    def batch(self, *packets):
        return write_batch(
            packet if isinstance(packet, RawPacket) else self.bot.protocol.encode(packet) for packet in packets
        )

    async def join(self, *initial, difficulty=2, size=True, feet=True):
        b = self.b
        self.transport = FakeTransport()
        self.transport.received.put_nowait(self.batch(
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
        ))
        self.transport.received.put_nowait(self.batch(
            b.StartGamePacket(runtime_id=1, entity_id=1, entity_game_type=b.GameType.SURVIVAL,
                              block_network_ids_are_hashes=True,
                              settings=b.LevelSettings(game_difficulty=b.Difficulty(difficulty))),
            b.SetActorDataPacket(id=1, packed_items=self.metadata(size=size)),
            *initial,
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ))
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", load_announced_terrain=False)
        if feet:
            await self.apply(self.move())

    async def apply(self, *packets):
        marker = self.b.SetTimePacket(time=31)
        observed = asyncio.create_task(self.bot.packets.wait_for_packet(type(marker).ID, timeout=1))
        await asyncio.sleep(0)
        self.transport.received.put_nowait(self.batch(*packets, marker))
        await observed

    def estimate(self, center=_CENTER, radius=_RADIUS, **options):
        count = len(self.transport.sent)
        result = self.bot.estimate_sourceless_explosion_damage(center, radius, **options)
        self.assertEqual(len(self.transport.sent), count)
        return result

    def equipment(self):
        b = self.b
        empty = b.SerializedNetworkItemStackDescriptor()
        return (
            b.ItemRegistryPacket(items=[b.ItemData(id=50, name="minecraft:iron_chestplate"),
                                        b.ItemData(id=51, name="minecraft:shield")]),
            self.armor(),
            b.InventoryContentPacket(inventory_id=b.ContainerID.OFFHAND, slots=[empty]),
            b.InventoryContentPacket(inventory_id=b.ContainerID.INVENTORY, slots=[empty] * 36),
        )

    async def submit_chestplate(self):
        b = self.b
        sent = asyncio.Event()
        first = len(self.transport.sent)
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == b.ItemStackRequestPacket.ID else None
        )
        try:
            equip = asyncio.create_task(self.bot.equip_armor(9, 1, timeout=2))
            await asyncio.wait_for(sent.wait(), timeout=1)
        finally:
            unsubscribe()
        raw_request = next(raw for batch in self.transport.sent[first:] for raw in read_batch(batch)
                           if raw.packet_id == b.ItemStackRequestPacket.ID)
        request = self.bot.protocol.decode(raw_request).requests[0]
        return equip, request.client_request_id

    def correction(self, container, slot, amount, net_id):
        b = self.b
        return b.ItemStackResponseContainerInfo(
            full_container_name=b.FullContainerName(name=b.ContainerEnumName[container]),
            slots=[b.ItemStackResponseSlotInfo(
                requested_slot=slot, slot=slot, amount=amount,
                item_stack_net_id=None if net_id is None else b.ItemStackNetId(raw_id=net_id),
                custom_name=b.RedactableString(unredacted_string=""),
            )],
        )

    async def test_archived_packet_values_and_complete_equipment(self):
        await self.join(*self.equipment(), self.attributes(absorption=0), self.attributes(health=20))
        await self.apply(self.terrain(), RawPacket(self.b.MovePlayerPacket.ID, bytes.fromhex(_FAR_MOVE)))
        self.assertAlmostEqual(self.estimate(raw=True), 7.92434024810791, delta=0.00002)
        self.assertAlmostEqual(self.estimate(), 7.92434024810791, delta=0.00002)
        self.assertEqual(self.estimate(target_runtime_id=1), self.estimate())
        b = self.b
        await self.apply(b.InventorySlotPacket(inventory_id=b.ContainerID.ARMOR, slot=1, item=self.stack(50)))
        self.assertAlmostEqual(self.estimate(), 7.278402328491211, delta=0.00002)
        self.assertAlmostEqual(self.estimate(raw=True), 7.92434024810791, delta=0.00002)

    async def test_archived_equality_and_outside_do_not_require_equipment(self):
        await self.join()
        await self.apply(RawPacket(self.b.MovePlayerPacket.ID, bytes.fromhex(_EQUAL_MOVE)))
        self.assertEqual(self.estimate((9.5, 220, 8.5), 1.875, raw=True), 1.0)
        self.assertIsNone(self.estimate((9.5, 220, 8.5), 1.875))
        await self.apply(self.move(x=13.28125, z=8.5))
        self.assertEqual(self.estimate((9.5, 220, 8.5), 1.875), 0.0)
        self.assertIsNone(self.estimate(raw=True))

    async def test_optional_arguments_reject_wrong_types(self):
        with self.assertRaisesRegex(TypeError, "target_runtime_id"):
            self.bot.estimate_sourceless_explosion_damage(_CENTER, _RADIUS, target_runtime_id=True)
        with self.assertRaisesRegex(TypeError, "raw"):
            self.bot.estimate_sourceless_explosion_damage(_CENTER, _RADIUS, raw="false")

    async def test_archived_stone_row_uses_collision_exposure(self):
        await self.join(*self.equipment(), self.attributes(health=20, absorption=0), difficulty=2)
        b = self.b
        floor = [(x, 219, z) for x in range(5, 16) for z in range(4, 14)]
        row = [(10, 221, z) for z in range(6, 11)]

        def stone(positions):
            return [b.UpdateBlockPacket(
                pos=b.BlockPos(x=x, y=y, z=z), runtime_id=0x80310E21, layer=0,
            ) for x, y, z in positions]

        center = (9.5, 220.1875, 8.5)
        await self.apply(self.terrain(), *stone(floor), RawPacket(b.MovePlayerPacket.ID, bytes.fromhex(_COVER_MOVE)))
        self.assertAlmostEqual(self.estimate(center, raw=True), 14.546372413635254, delta=0.00002)
        await self.apply(*stone(row))
        self.assertAlmostEqual(self.estimate(center, raw=True), 9.990047454833984, delta=0.00002)
        self.assertAlmostEqual(self.estimate(center), 9.990047454833984, delta=0.00002)

    async def test_absorption_effects_and_incomplete_or_shield_equipment_are_unknown(self):
        b = self.b
        await self.join(*self.equipment(), self.attributes(health=20))
        await self.apply(self.terrain())
        self.assertIsNone(self.estimate())
        await self.apply(self.attributes(actor=2, absorption=0))
        self.assertIsNone(self.estimate())
        await self.apply(self.attributes(absorption=2))
        self.assertIsNone(self.estimate())
        await self.apply(self.attributes(absorption=0), b.MobEffectPacket(
            runtime_id=1, event_id=b.MobEffectPacket.Event.ADD, effect_id=11, effect_duration_ticks=60,
        ))
        self.assertIsNone(self.estimate())
        await self.apply(b.MobEffectPacket(runtime_id=1, event_id=b.MobEffectPacket.Event.REMOVE, effect_id=11))
        self.assertAlmostEqual(self.estimate(), 7.92434024810791, delta=0.00002)
        await self.apply(b.SetDifficultyPacket(difficulty=b.Difficulty(3)))
        self.assertAlmostEqual(self.estimate(), 7.92434024810791, delta=0.00002)
        await self.apply(b.SetDifficultyPacket(difficulty=b.Difficulty(1)))
        self.assertIsNone(self.estimate())
        await self.apply(b.SetDifficultyPacket(difficulty=b.Difficulty(2)))

        # BDS item-stack verdicts can transfer a known type and net ID with no item data.
        await self.apply(b.InventorySlotPacket(inventory_id=b.ContainerID.ARMOR, slot=1, item=self.stack(50, b"")))
        self.assertIsNone(self.estimate())
        enchanted = self.stack(50, enchantment_data(b, 4, enchantment=b.Enchant.Type.PROTECTION))
        await self.apply(b.InventorySlotPacket(inventory_id=b.ContainerID.ARMOR, slot=1, item=enchanted))
        self.assertIsNone(self.estimate())
        await self.apply(b.InventorySlotPacket(inventory_id=b.ContainerID.ARMOR, slot=1, item=self.stack(50)),
                         b.InventorySlotPacket(inventory_id=b.ContainerID.OFFHAND, slot=0, item=self.stack(51)))
        self.assertIsNone(self.estimate())
        self.assertAlmostEqual(self.estimate(raw=True), 7.92434024810791, delta=0.00002)

    async def test_armor_verdict_cannot_supply_destination_item_data(self):
        b = self.b
        await self.join(*self.equipment(), self.attributes(health=20, absorption=0))
        await self.apply(self.terrain(), b.InventorySlotPacket(
            inventory_id=b.ContainerID.INVENTORY, slot=9, item=self.stack(50),
        ))
        original = self.estimate()
        self.assertAlmostEqual(original, 7.92434024810791, delta=0.00002)

        equip, request_id = await self.submit_chestplate()
        await self.apply(b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request_id, result=b.ItemStackNetResult.SUCCESS,
            containers=[self.correction("ARMOR_CONTAINER", 1, 1, 90),
                        self.correction("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 9, 0, None)],
        )]))
        self.assertEqual((await equip).result, b.ItemStackNetResult.SUCCESS)
        self.assertEqual(self.bot.armor_slots[1].user_data_buffer, bytes(10))
        self.assertIsNone(self.estimate())
        self.assertEqual(self.estimate(raw=True), original)
        chest = self.stack(50)
        chest.net_id_variant = 90
        await self.apply(b.InventorySlotPacket(inventory_id=b.ContainerID.ARMOR, slot=1, item=chest))
        self.assertAlmostEqual(self.estimate(), 7.278402328491211, delta=0.00002)

    async def test_success_missing_armor_slot_requires_observation_after_submission(self):
        b = self.b
        await self.join(*self.equipment(), self.attributes(health=20, absorption=0))
        await self.apply(self.terrain())
        raw_damage = self.estimate(raw=True)
        self.assertAlmostEqual(raw_damage, 7.92434024810791, delta=0.00002)
        chest = b.InventorySlotPacket(inventory_id=b.ContainerID.ARMOR, slot=1, item=self.stack(50))
        source = b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=9, item=self.stack(50))
        source_only = [self.correction("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 9, 0, None)]
        unrelated = b.InventorySlotPacket(inventory_id=b.ContainerID.ARMOR, slot=0)

        for name, result, corrections, newer, expected in (
            ("success without corrections", b.ItemStackNetResult.SUCCESS, [], (), None),
            ("success with source correction", b.ItemStackNetResult.SUCCESS, source_only, (unrelated,), None),
            ("success after destination slot packet", b.ItemStackNetResult.SUCCESS, [], (chest,), 7.278402328491211),
            ("success after armor content packet", b.ItemStackNetResult.SUCCESS, source_only,
             (self.armor(),), raw_damage),
            ("failure", b.ItemStackNetResult.ERROR, None, (), raw_damage),
        ):
            with self.subTest(name=name):
                await self.apply(self.armor(), source)
                self.assertEqual(self.estimate(), raw_damage)
                equip, request_id = await self.submit_chestplate()
                await self.apply(*newer, b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
                    client_request_id=request_id, result=result, containers=corrections,
                )]))
                self.assertEqual((await equip).result, result)
                self.assertEqual(self.estimate(raw=True), raw_damage)
                if expected is None:
                    self.assertEqual(self.bot.armor_slots[1].id, 0)
                    self.assertIsNone(self.estimate())
                    await self.apply(chest)
                    expected = 7.278402328491211
                self.assertAlmostEqual(self.estimate(), expected, delta=0.00002)

    async def test_main_hand_equipment_packet_overrides_older_empty_slot(self):
        b = self.b
        await self.join(*self.equipment(), self.attributes(health=20, absorption=0))
        await self.apply(self.terrain())
        original = self.estimate()
        self.assertAlmostEqual(original, 7.92434024810791, delta=0.00002)
        shield = b.MobEquipmentPacket(
            runtime_id=2, container_id=b.ContainerID.INVENTORY, slot=0, selected_slot=0, item=self.stack(51),
        )
        await self.apply(shield)
        self.assertEqual(self.estimate(), original)
        shield.runtime_id = 1
        await self.apply(shield)
        self.assertEqual(self.bot.inventory_slots[0].id, 0)
        self.assertIsNone(self.estimate())
        self.assertEqual(self.estimate(raw=True), original)
        await self.apply(b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=0))
        self.assertEqual(self.estimate(), original)

    async def test_remote_requires_standing_body_and_current_instance(self):
        b = self.b
        await self.join(feet=False)
        target = UUID(int=2)
        listed = b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=target, id=102, name="Target",
        )])

        def player(*, size):
            return b.AddPlayerPacket(runtime_id=2, uuid=target, name="Target",
                                     pos=b.Vec3(x=12.375, y=220, z=9.25),
                                     unpack=self.metadata(size=size, remote=True),
                                     abilities_data=b.SerializedAbilitiesData(target_player=102))

        await self.apply(listed, player(size=False))
        self.assertIsNone(self.estimate(target_runtime_id=2, raw=True))
        await self.apply(b.SetActorDataPacket(id=2, packed_items=self.metadata(remote=True)))
        self.assertEqual(self.estimate(radius=1, target_runtime_id=2), 0.0)
        await self.apply(self.terrain())
        self.assertAlmostEqual(self.estimate(target_runtime_id=2, raw=True), 7.92434024810791, delta=0.00002)
        self.assertIsNone(self.estimate(target_runtime_id=2))
        self.assertIsNone(self.estimate(raw=True))
        flag = 1 << int(b.ActorFlags.SNEAKING)
        await self.apply(b.SetActorDataPacket(id=2, packed_items=self.metadata(low=flag, high=None, size=False)))
        self.assertIsNone(self.estimate(target_runtime_id=2, raw=True))
        sleep_bit = 1 << (int(b.ActorFlags.SLEEPING) - 64)
        remote_move = self.move()
        remote_move.player_id = 2
        await self.apply(b.SetActorDataPacket(id=2, packed_items=self.metadata(high=sleep_bit, size=False)),
                         remote_move)
        self.assertIsNone(self.estimate(target_runtime_id=2, raw=True))
        await self.apply(b.SetActorDataPacket(id=2, packed_items=self.metadata(size=False)))
        self.assertIsNone(self.estimate(target_runtime_id=2, raw=True))
        await self.apply(remote_move)
        self.assertAlmostEqual(self.estimate(target_runtime_id=2, raw=True), 7.92434024810791, delta=0.00002)
        await self.apply(b.RemoveActorPacket(entity_id=102), player(size=False))
        self.assertIsNone(self.estimate(target_runtime_id=2, raw=True))

    async def test_same_dimension_respawn_accepts_sparse_width_and_height(self):
        b = self.b
        await self.join(*self.equipment(), self.attributes(health=20, absorption=0))
        await self.apply(self.terrain())
        original = self.estimate(raw=True)
        self.assertAlmostEqual(original, 7.92434024810791, delta=0.00002)
        await self.apply(b.RespawnPacket(runtime_id=1, state=b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
        self.assertIsNone(self.estimate(raw=True))
        await self.apply(b.RespawnPacket(runtime_id=1, state=b.PlayerRespawnState.READY_TO_SPAWN),
                         self.move(), b.SetActorDataPacket(id=1, packed_items=self.metadata(size=False)))
        self.assertIsNone(self.estimate(raw=True))

        def dimension(index, value):
            metadata = b.SynchedActorData.CopyableDataList(data=[
                b.DataItemEntry(id=index, payload=b.DataItemFloatPayload(value=value)),
            ])
            return b.SetActorDataPacket(id=1, packed_items=metadata)

        await self.apply(dimension(53, 0.6))
        self.assertIsNone(self.estimate(raw=True))
        await self.apply(dimension(54, 1.8))
        self.assertEqual(self.estimate(raw=True), original)

    async def test_dimension_disconnect_close_and_rejoin_require_fresh_state(self):
        b = self.b
        self.assertIsNone(self.bot.estimate_sourceless_explosion_damage(_CENTER, _RADIUS))
        await self.join(*self.equipment(), self.attributes(health=20, absorption=0))
        await self.apply(self.terrain())
        self.assertIsNotNone(self.estimate())
        await self.apply(b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(self.estimate(raw=True))
        await self.apply(b.PlayerActionPacket(runtime_id=1, action=b.PlayerActionType.CHANGE_DIMENSION_ACK))
        self.assertIsNone(self.estimate(raw=True))
        await self.apply(self.move(), b.SetActorDataPacket(id=1, packed_items=self.metadata()), self.terrain(1))
        self.assertIsNotNone(self.estimate(raw=True))
        await self.apply(b.DisconnectPacket())
        self.assertIsNone(self.estimate(raw=True))
        await self.bot.close()
        self.assertIsNone(self.estimate())
        await self.join()
        await self.apply(self.terrain())
        self.assertIsNone(self.estimate())
        self.assertIsNotNone(self.estimate(raw=True))
