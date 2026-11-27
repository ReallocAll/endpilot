"""Observe remote sleep changes through the public Bot's native packet reader."""

import asyncio
import unittest
from uuid import UUID

import test_entity_crouch as crouch_fixture


class EntitySleepTests(unittest.IsolatedAsyncioTestCase):
    join = crouch_fixture.EntityCrouchTests.join
    deliver = crouch_fixture.EntityCrouchTests.deliver
    entity = crouch_fixture.EntityCrouchTests.entity

    def flags(self, value):
        b = self.b
        return b.DataItemEntry(id=int(b.ActorDataIDs.PLAYER_FLAGS), payload=b.DataItemBytePayload(value=value))

    def spawn(self, *entries, runtime_id=8, entity_id=108):
        b = self.b
        return b.AddPlayerPacket(
            uuid=UUID(int=entity_id), runtime_id=runtime_id, name=f"Player {entity_id}",
            unpack=b.SynchedActorData.CopyableDataList(data=entries),
            abilities_data=b.SerializedAbilitiesData(target_player=entity_id),
        )

    def update(self, *entries, runtime_id=8):
        b = self.b
        return b.SetActorDataPacket(id=runtime_id, packed_items=b.SynchedActorData.CopyableDataList(data=entries))

    async def arm(self, *, runtime_id=None):
        waiting = asyncio.create_task(self.bot.wait_for_entity_sleep_change(runtime_id=runtime_id, timeout=1))
        self.addAsyncCleanup(crouch_fixture.EntityCrouchTests.cancel_wait, waiting)
        await asyncio.sleep(0)
        return waiting

    async def test_typed_sleep_flags_seed_silently_and_publish_only_live_changes(self):
        await self.join()
        b = self.b
        events, scoped = [], []
        sent = len(self.wire.sent)

        def observe(entity):
            events.append((entity, self.entity(entity.runtime_id)))

        self.bot.subscribe_entity_sleep_changes(observe)
        await self.deliver(self.spawn(self.flags(0x06)), self.spawn(runtime_id=9, entity_id=109))
        initial = self.entity()
        self.assertIs(initial.sleeping, True)
        self.assertIsNone(self.entity(9).sleeping)
        self.assertEqual(events, [])

        self.bot.subscribe_entity_sleep_changes(scoped.append, runtime_id=8)
        waking = await self.arm(runtime_id=8)
        await self.deliver(
            self.update(b.DataItemEntry(
                id=int(b.ActorDataIDs.BED_POSITION), payload=b.DataItemPosPayload(value=b.BlockPos(x=4, y=80, z=5)),
            ), runtime_id=9),
            self.update(b.DataItemEntry(id=0, payload=b.DataItemInt64Payload(value=0))),
            self.update(b.DataItemEntry(id=int(b.ActorDataIDs.PLAYER_FLAGS), payload=b.DataItemIntPayload(value=0))),
            self.update(self.flags(0x02)),
        )
        self.assertEqual(events, [])
        self.assertIs(self.entity().sleeping, True)
        self.assertIsNone(self.entity(9).sleeping)
        await self.deliver(self.update(self.flags(0x04), runtime_id=9))
        self.assertIs(self.entity(9).sleeping, False)
        self.assertEqual(events, [])
        await self.deliver(self.update(self.flags(0x02), runtime_id=9))
        other = self.entity(9)
        self.assertEqual(events, [(other, other)])
        self.assertEqual(scoped, [])
        self.assertFalse(waking.done())

        await self.deliver(self.update(self.flags(0x04)), self.update(self.flags(0x00)))
        awake = self.entity()
        self.assertEqual(await waking, awake)
        self.assertIs(awake.sleeping, False)
        self.assertEqual(scoped, [awake])

        falling_asleep = await self.arm()
        await self.deliver(self.update(self.flags(0x02)), self.update(self.flags(0x02)))
        asleep = self.entity()
        self.assertEqual(await falling_asleep, asleep)
        self.assertIs(asleep.sleeping, True)
        self.assertIs(awake.sleeping, False)
        self.assertEqual(events, [(other, other), (awake, awake), (asleep, asleep)])
        self.assertEqual(scoped, [awake, asleep])
        self.assertEqual(len(self.wire.sent), sent)

    async def test_scoped_sleep_observers_end_on_removal_or_runtime_reuse(self):
        await self.join()
        seen, scoped = [], []
        self.bot.subscribe_entity_sleep_changes(seen.append)
        await self.deliver(self.spawn(self.flags(0)))

        for removal, replacement in ((self.b.RemoveActorPacket(entity_id=108), 109), (None, 110)):
            with self.subTest(explicit_removal=removal is not None):
                self.bot.subscribe_entity_sleep_changes(scoped.append, runtime_id=8)
                waiting = await self.arm(runtime_id=8)
                if removal is not None:
                    await self.deliver(removal)
                    with self.assertRaises(LookupError):
                        await waiting
                await self.deliver(self.spawn(self.flags(0), entity_id=replacement), self.update(self.flags(0x02)))
                if removal is None:
                    with self.assertRaises(LookupError):
                        await waiting

        self.assertEqual(scoped, [])
        self.assertEqual([(entity.runtime_id, entity.name, entity.sleeping) for entity in seen],
                         [(8, "Player 109", True), (8, "Player 110", True)])
