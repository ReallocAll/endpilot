import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from endpilot.entities import Entities, EntityMovement, EntitySpawn, EntityState
from endpilot.protocol import Protocol, RawPacket


class EntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_attack_uses_observed_actor_and_selected_item_in_typed_packet(self):
        protocol = Protocol(2193)
        b = protocol.binding
        item = b.SerializedNetworkItemStackDescriptor(
            id=1, stack_size=1, aux_value=0, net_id_variant=9,
            block_runtime_id=123, user_data_buffer=b"\x00" * 10,
        )
        packets = SimpleNamespace(protocol=protocol, send=AsyncMock())
        entities = Entities(packets)
        entities._record(EntityState(42, 77, "minecraft:cow", (2.5, 100.0, 0.5)))
        movement = SimpleNamespace(wait_for_position_sync=AsyncMock(return_value=(0.5, 101.62, 0.5)))
        inventory = SimpleNamespace(item_or_empty=lambda slot: item)

        await entities.attack(movement, inventory, 77, slot=2, hit_height=0.75)
        packets.send.assert_awaited_once()
        packet = protocol.decode(protocol.encode(packets.send.await_args.args[0]))
        self.assertIsInstance(packet, b.InventoryTransactionPacket)
        tx = packet.variant_transaction
        self.assertIsInstance(tx, b.ItemUseOnActorInventoryTransaction)
        self.assertEqual(int(tx.runtime_id), 77)
        self.assertEqual(tx.action_type, b.ItemUseOnActorInventoryTransaction.ActionType.ATTACK)
        self.assertEqual(tx.slot, 2)
        self.assertEqual(int(tx.item.id), 1)
        self.assertEqual(int(tx.item.net_id_variant), 9)
        self.assertAlmostEqual(tx.from_pos.y, 101.62, places=2)
        self.assertAlmostEqual(tx.hit_pos.x, 2.5)
        self.assertAlmostEqual(tx.hit_pos.y, 100.75)
        self.assertEqual(tx.transaction.actions, [])

    async def test_attack_requires_observed_actor_not_arbitrary_runtime_id(self):
        packets = SimpleNamespace(protocol=Protocol(2193), send=AsyncMock())
        entities = Entities(packets)
        with self.assertRaisesRegex(ValueError, "not currently observed"):
            await entities.attack(None, None, 999)
        packets.send.assert_not_awaited()

    async def test_spawn_filters_type_and_returns_server_ids_and_position(self):
        binding = Protocol(2193).binding
        other = RawPacket(binding.AddActorPacket.ID, b"other")
        cow = RawPacket(binding.AddActorPacket.ID, b"cow")
        decoded = {
            b"other": SimpleNamespace(actor_type="minecraft:pig"),
            b"cow": SimpleNamespace(
                actor_type="minecraft:cow", entity_id=42, runtime_id=77,
                pos=SimpleNamespace(x=2.5, y=101.0, z=0.5),
            ),
        }
        protocol = SimpleNamespace(binding=binding, decode=lambda raw: decoded[raw.body])
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, binding.AddActorPacket.ID)
            self.assertEqual(timeout, 5)
            self.assertFalse(match(other))
            self.assertTrue(match(cow))
            return cow

        packets.wait_for_packet.side_effect = wait
        entity = await Entities(packets).wait_for_spawn(actor_type="minecraft:cow", timeout=5)
        self.assertEqual(entity, EntitySpawn(42, 77, "minecraft:cow", (2.5, 101.0, 0.5)))

    async def test_removal_matches_unique_id_not_runtime_id(self):
        binding = Protocol(2193).binding
        packet = binding.RemoveActorPacket(entity_id=42)
        protocol = Protocol(2193)
        raw = protocol.encode(packet)
        self.assertEqual(protocol.decode(raw).entity_id, 42)
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, binding.RemoveActorPacket.ID)
            self.assertFalse(match(protocol.encode(binding.RemoveActorPacket(entity_id=77))))
            self.assertTrue(match(raw))
            return raw

        packets.wait_for_packet.side_effect = wait
        self.assertIsNone(await Entities(packets).wait_for_removal(42))

    async def test_movement_matches_runtime_id_and_preserves_omitted_axes(self):
        binding = Protocol(2193).binding
        other = RawPacket(binding.MoveActorDeltaPacket.ID, b"other")
        moved = RawPacket(binding.MoveActorDeltaPacket.ID, b"moved")
        decoded = {
            b"other": SimpleNamespace(move_data=SimpleNamespace(runtime_id=999)),
            b"moved": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=77, new_position_x=6.0,
                new_position_y=None, new_position_z=None,
            )),
        }
        protocol = SimpleNamespace(binding=binding, decode=lambda raw: decoded[raw.body])
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_ids, *, match, timeout):
            self.assertEqual(set(packet_ids), {
                binding.MoveActorAbsolutePacket.ID, binding.MoveActorDeltaPacket.ID,
            })
            self.assertFalse(match(other))
            self.assertTrue(match(moved))
            self.assertEqual(timeout, 8)
            return moved

        packets.wait_for_packet.side_effect = wait
        result = await Entities(packets).wait_for_movement(77, timeout=8)
        self.assertEqual(result, EntityMovement(77, 6.0, None, None))

    async def test_absolute_actor_movement_has_all_coordinates(self):
        binding = Protocol(2193).binding
        packet = RawPacket(binding.MoveActorAbsolutePacket.ID, b"absolute")
        move_data = SimpleNamespace(
            runtime_id=77, pos=SimpleNamespace(x=6.0, y=100.0, z=0.5),
        )
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=binding, decode=lambda raw: SimpleNamespace(move_data=move_data)),
            wait_for_packet=AsyncMock(return_value=packet),
        )
        result = await Entities(packets).wait_for_movement(77)
        self.assertEqual(result, EntityMovement(77, 6.0, 100.0, 0.5))

    async def test_snapshot_accumulates_partial_moves_and_removal(self):
        binding = Protocol(2193).binding
        decoded = {
            b"spawn": SimpleNamespace(
                entity_id=42, runtime_id=77, actor_type="minecraft:cow",
                pos=SimpleNamespace(x=2.5, y=100.0, z=0.5),
            ),
            b"delta": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=77, new_position_x=6.0,
                new_position_y=None, new_position_z=None,
            )),
            b"absolute": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=77, pos=SimpleNamespace(x=7.0, y=101.0, z=1.0),
            )),
            b"remove": SimpleNamespace(entity_id=42),
        }
        callbacks = []

        def subscribe(callback):
            callbacks.append(callback)
            return lambda: callbacks.remove(callback)

        packets = SimpleNamespace(
            protocol=SimpleNamespace(
                binding=binding, decode=lambda raw: decoded[raw.body],
            ),
            subscribe_raw=subscribe,
        )
        entities = Entities(packets)
        entities.start()

        def emit(packet_type, body):
            callbacks[0](RawPacket(packet_type, body))

        emit(binding.MoveActorDeltaPacket.ID, b"delta")
        self.assertEqual(entities.snapshot(), ())
        emit(binding.AddActorPacket.ID, b"spawn")
        initial = entities.snapshot()
        self.assertEqual(initial, (
            EntityState(42, 77, "minecraft:cow", (2.5, 100.0, 0.5)),
        ))
        emit(binding.MoveActorDeltaPacket.ID, b"delta")
        self.assertEqual(entities.snapshot()[0].position, (6.0, 100.0, 0.5))
        self.assertEqual(initial[0].position, (2.5, 100.0, 0.5))
        emit(binding.MoveActorAbsolutePacket.ID, b"absolute")
        self.assertEqual(entities.snapshot()[0].position, (7.0, 101.0, 1.0))
        emit(binding.RemoveActorPacket.ID, b"remove")
        self.assertEqual(entities.snapshot(), ())
        emit(binding.AddActorPacket.ID, b"spawn")
        self.assertEqual(len(entities.snapshot()), 1)
        emit(binding.ChangeDimensionPacket.ID, b"dimension")
        self.assertEqual(entities.snapshot(), ())
        entities.close()
        self.assertEqual(callbacks, [])


    async def test_item_drop_from_registry_tracks_movement_and_take(self):
        b = Protocol(2193).binding
        callbacks = []
        decoded = {
            b"registry": SimpleNamespace(items=[
                SimpleNamespace(id=42, name="minecraft:beef"),
            ]),
            b"drop": SimpleNamespace(
                id=101, runtime_id=202,
                item=SimpleNamespace(id=42, stack_size=2),
                pos=SimpleNamespace(x=3.5, y=100.5, z=0.5),
            ),
            b"move": SimpleNamespace(move_data=SimpleNamespace(
                runtime_id=202, new_position_x=4.5,
                new_position_y=None, new_position_z=None,
            )),
            b"take": SimpleNamespace(item_id=202),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda callback: callbacks.append(callback) or (lambda: callbacks.remove(callback)),
        )
        entities = Entities(packets)
        entities.start()
        callbacks[0](RawPacket(b.ItemRegistryPacket.ID, b"registry"))
        callbacks[0](RawPacket(b.AddItemActorPacket.ID, b"drop"))
        initial = entities.drops()[0]
        self.assertEqual(initial, EntityState(
            101, 202, "minecraft:item", (3.5, 100.5, 0.5), "minecraft:beef", 2, 42
        ))
        callbacks[0](RawPacket(b.MoveActorDeltaPacket.ID, b"move"))
        self.assertEqual(entities.drops()[0].position, (4.5, 100.5, 0.5))
        self.assertEqual(initial.position, (3.5, 100.5, 0.5))
        callbacks[0](RawPacket(b.TakeItemActorPacket.ID, b"take"))
        self.assertEqual(entities.drops(), ())
        self.assertEqual(entities.snapshot(), ())
        entities.close()
        self.assertEqual(callbacks, [])

    async def test_item_drop_fallback_id_and_unique_id_removal(self):
        b = Protocol(2193).binding
        callbacks = []
        decoded = {
            b"drop": SimpleNamespace(
                id=101, runtime_id=202,
                item=SimpleNamespace(id=52, stack_size=1),
                pos=SimpleNamespace(x=0.5, y=101, z=0.5),
            ),
            b"remove": SimpleNamespace(entity_id=101),
        }
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda raw: decoded[raw.body]),
            subscribe_raw=lambda callback: callbacks.append(callback) or (lambda: callbacks.remove(callback)),
        )
        entities = Entities(packets)
        entities.start()
        callbacks[0](RawPacket(b.AddItemActorPacket.ID, b"drop"))
        self.assertEqual(entities.drops()[0].item_id, "#52")
        callbacks[0](RawPacket(b.RemoveActorPacket.ID, b"remove"))
        self.assertEqual(entities.drops(), ())
        entities.close()

    async def test_wait_for_item_drop_returns_server_snapshot(self):
        b = Protocol(2193).binding
        raw = RawPacket(b.AddItemActorPacket.ID, b"drop")
        packet = SimpleNamespace(
            id=30, runtime_id=31,
            item=SimpleNamespace(id=4, stack_size=3),
            pos=SimpleNamespace(x=1, y=2, z=3),
        )
        packets = SimpleNamespace(
            protocol=SimpleNamespace(binding=b, decode=lambda _: packet),
            wait_for_packet=AsyncMock(return_value=raw),
        )
        result = await Entities(packets, item_names={4: "minecraft:stone"}).wait_for_item_drop(timeout=7)
        packets.wait_for_packet.assert_awaited_once_with(b.AddItemActorPacket.ID, timeout=7)
        self.assertEqual(result, EntityState(
            30, 31, "minecraft:item", (1.0, 2.0, 3.0), "minecraft:stone", 3, 4
        ))

    async def test_pickup_matches_runtime_item_and_collector_ids(self):
        protocol = Protocol(2193)
        b = protocol.binding
        wrong_item = protocol.encode(b.TakeItemActorPacket(item_id=203, actor_id=7))
        wrong_actor = protocol.encode(b.TakeItemActorPacket(item_id=202, actor_id=8))
        correct = protocol.encode(b.TakeItemActorPacket(item_id=202, actor_id=7))
        packets = SimpleNamespace(protocol=protocol, wait_for_packet=AsyncMock())

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, b.TakeItemActorPacket.ID)
            self.assertEqual(timeout, 6)
            self.assertFalse(match(wrong_item))
            self.assertFalse(match(wrong_actor))
            self.assertTrue(match(correct))
            return correct

        packets.wait_for_packet.side_effect = wait
        self.assertIsNone(await Entities(packets).wait_for_item_pickup(202, 7, timeout=6))

    async def test_collect_drop_releases_movement_after_confirmed_take(self):
        protocol = Protocol(2193)
        b = protocol.binding
        packet_ready = asyncio.Event()
        move_started = asyncio.Event()
        incoming = asyncio.get_running_loop().create_future()

        async def wait_for_packet(packet_id, *, match, timeout):
            self.assertEqual(packet_id, b.TakeItemActorPacket.ID)
            self.assertEqual(timeout, 2)
            packet_ready.set()
            raw = await incoming
            self.assertTrue(match(raw))
            return raw

        async def follow(*_args, **_kwargs):
            move_started.set()
            await asyncio.Event().wait()

        packets = SimpleNamespace(
            protocol=protocol, wait_for_packet=wait_for_packet,
        )
        entities = Entities(packets)
        item = EntityState(13, 23, "minecraft:item", (3.5, 100.5, 0.5), "minecraft:dirt", 1, 3)
        entities._record(item)
        movement = SimpleNamespace(follow_entity=AsyncMock(side_effect=follow))
        task = asyncio.create_task(entities.collect_drop(movement, 23, 7, timeout=2))
        try:
            await asyncio.wait_for(packet_ready.wait(), 1)
            await asyncio.wait_for(move_started.wait(), 1)
            incoming.set_result(protocol.encode(
                b.TakeItemActorPacket(item_id=23, actor_id=7)
            ))
            self.assertEqual(await asyncio.wait_for(task, 1), item)
            movement.follow_entity.assert_awaited_once_with(
                entities, 23, ticks=120, stop_distance=0.0
            )
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_collect_drop_rejects_unknown_actor_without_walking(self):
        movement = SimpleNamespace(follow_entity=AsyncMock())
        entities = Entities(SimpleNamespace())
        with self.assertRaisesRegex(ValueError, "item actor"):
            await entities.collect_drop(movement, 13, 7)
        movement.follow_entity.assert_not_awaited()
