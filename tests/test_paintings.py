import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.entities import EntitySpawn, PaintingSnapshot
from endpilot.protocol import write_batch
from test_join import FakeTransport


NORTH = PaintingSnapshot(-4294967287, 9, (8.0, 223.0, 13.96875), 2, "unpacked")
WEST = PaintingSnapshot(-4294967286, 10, (14.96875, 223.0, 9.0), 1, "Pigscene")
PAINTING = "minecraft:painting"


class PaintingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("Paintings", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding

    def packet(self, paint):
        x, y, z = paint.position
        return self.b.AddPaintingPacket(entity_id=paint.entity_id, runtime_id=paint.runtime_id,
                                        pos=self.b.Vec3(x=x, y=y, z=z), dir=paint.dir, motif=paint.motif)

    def feed(self, *packets):
        self.transport.received.put_nowait(write_batch(map(self.bot.protocol.encode, packets)))

    async def join(self, *startup):
        self.transport = FakeTransport()
        b = self.b
        self.feed(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.feed(
            b.StartGamePacket(runtime_id=4, entity_id=320), *startup,
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        marker = asyncio.create_task(self.bot.packets.wait_for_packet(self.b.SetTimePacket.ID, timeout=1))
        await asyncio.sleep(0)
        self.feed(*packets, self.b.SetTimePacket())
        await marker

    def paintings(self):
        sent = len(self.transport.sent)
        rows = tuple(actor.painting for actor in self.bot.observed_entities if actor.actor_type == PAINTING)
        self.assertEqual(len(self.transport.sent), sent)
        return rows

    async def test_pre_spawn_painting_and_removal_replay_in_order_on_public_join(self):
        await self.join(self.packet(NORTH), self.packet(WEST),
                        self.b.RemoveActorPacket(entity_id=NORTH.entity_id), self.packet(NORTH))
        self.assertEqual(self.paintings(), (WEST, NORTH))
        old = self.paintings()[1]
        with self.assertRaises(FrozenInstanceError):
            old.dir = 7
        await self.bot.close()
        await self.join(self.packet(WEST))
        self.assertEqual(self.paintings(), (WEST,))
        self.assertEqual(old, NORTH)
        state = self.bot._entities.get(WEST.runtime_id)
        self.assertEqual((state.entity_id, state.actor_type, state.position, state.painting),
                         (WEST.entity_id, PAINTING, WEST.position, WEST))

    async def test_public_filtered_spawn_and_unique_id_removal_keep_general_event_behavior(self):
        await self.join()
        filtered = asyncio.create_task(self.bot.wait_for_entity_spawn(actor_type=PAINTING, timeout=1))
        generic = asyncio.create_task(self.bot.wait_for_entity_spawn(timeout=1))
        self.addCleanup(filtered.cancel)
        self.addCleanup(generic.cancel)
        await asyncio.sleep(0)
        await self.deliver(self.packet(NORTH))
        self.assertEqual(await filtered, EntitySpawn(NORTH.entity_id, NORTH.runtime_id, PAINTING, NORTH.position))
        self.assertFalse(generic.done())
        await self.deliver(self.b.AddActorPacket(entity_id=-16, runtime_id=12, actor_type="minecraft:cow"))
        self.assertEqual((await generic).actor_type, "minecraft:cow")

        await self.deliver(self.packet(WEST))
        self.assertEqual(self.paintings(), (NORTH, WEST))
        removing = asyncio.create_task(self.bot.wait_for_entity_removal(NORTH.entity_id, timeout=1))
        self.addCleanup(removing.cancel)
        await asyncio.sleep(0)
        await self.deliver(self.b.RemoveActorPacket(entity_id=12))
        self.assertFalse(removing.done())
        self.assertEqual(self.paintings(), (NORTH, WEST))
        await self.deliver(self.b.RemoveActorPacket(entity_id=NORTH.entity_id))
        await removing
        self.assertEqual(self.paintings(), (WEST,))
        self.assertIsNone(self.bot._entities.get(NORTH.runtime_id))

    async def test_dimension_change_clears_paintings_and_isolates_reused_runtime(self):
        await self.join(self.packet(NORTH), self.packet(WEST))
        self.assertEqual(self.paintings(), (NORTH, WEST))
        await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1))
        self.assertEqual(self.paintings(), ())
        reused = PaintingSnapshot(-20, NORTH.runtime_id, (5.0, 65.0, 7.0), -1, "raw_variant")
        await self.deliver(self.packet(reused), self.b.RemoveActorPacket(entity_id=NORTH.entity_id))
        self.assertEqual(self.paintings(), (reused,))
        self.assertEqual(self.bot._entities.get(NORTH.runtime_id).entity_id, reused.entity_id)
