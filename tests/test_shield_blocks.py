"""Observe shield impact pulses through the native packet reader."""

import asyncio
from uuid import UUID

from endpilot import ShieldBlockEvent
from endpilot.protocol import Protocol

import test_entity_crouch as crouch_fixture
import test_entity_glide as glide_fixture


OWN_ID = -4294967295
REMOTE_ID = 108
# Extended flag words from the stock shield raise and one-tick impact.
RAISING = 35_184_405_644_032
RAISED = 35_184_405_643_520
IMPACT = 35_184_405_646_592
DAMAGED_SHIELD_BIT = 1 << (75 - 64)


class ShieldBlockTests(glide_fixture._GlideWireTestCase):
    def flags(self, word, *, field=92, typed=True):
        b = self.b
        payload = b.DataItemInt64Payload if typed else b.DataItemIntPayload
        data = () if word is None else (b.DataItemEntry(id=field, payload=payload(value=word)),)
        return b.SynchedActorData.CopyableDataList(data=data)

    def spawn(self, word, *, entity_id=REMOTE_ID):
        b = self.b
        return b.AddPlayerPacket(
            uuid=UUID(int=entity_id), runtime_id=8, name="Shield peer", unpack=self.flags(word),
            abilities_data=b.SerializedAbilitiesData(target_player=entity_id),
        )

    def update(self, runtime_id, tick, word, **options):
        return self.b.SetActorDataPacket(id=runtime_id, tick=tick, packed_items=self.flags(word, **options))

    async def arm(self, runtime_id):
        waiting = asyncio.create_task(self.bot.wait_for_shield_block(runtime_id=runtime_id, timeout=1))
        self.addAsyncCleanup(crouch_fixture.EntityCrouchTests.cancel_wait, waiting)
        await asyncio.sleep(0)
        return waiting

    async def test_short_impacts_reach_only_the_matching_runtime_filters(self):
        await self.join(runtime_id=7, entity_id=OWN_ID)
        await self.deliver(self.spawn(RAISED))
        all_events, own_events, remote_events = [], [], []
        self.bot.subscribe_shield_blocks(all_events.append)
        self.bot.subscribe_shield_blocks(own_events.append, runtime_id=7)
        self.bot.subscribe_shield_blocks(remote_events.append, runtime_id=8)
        own = await self.arm(7)
        remote = await self.arm(8)

        await self.deliver(
            self.update(9, 78, IMPACT),
            self.b.AddActorPacket(runtime_id=10, entity_id=110, actor_type="minecraft:zombie"),
            self.update(10, 79, IMPACT),
            self.update(7, 80, RAISING), self.update(7, 81, RAISED),
            self.update(8, 82, 1 << 10, typed=False), self.update(8, 83, IMPACT, field=0),
            self.update(8, 84, RAISED | DAMAGED_SHIELD_BIT),
        )
        self.assertEqual(all_events, [])
        self.assertFalse(own.done())
        self.assertFalse(remote.done())
        self.assertIsNone(self.entity(9))
        self.assertIsNotNone(self.entity(10))
        for runtime in (9, 10):
            with self.subTest(runtime_id=runtime), self.assertRaises(LookupError):
                self.bot.subscribe_shield_blocks(all_events.append, runtime_id=runtime)

        await self.deliver(self.update(7, 85, IMPACT), self.update(7, 86, RAISED))
        own_block = ShieldBlockEvent(runtime_id=7, entity_id=OWN_ID, tick=85)
        self.assertEqual(await own, own_block)
        self.assertFalse(remote.done())

        await self.deliver(
            self.update(8, 87, IMPACT), self.update(8, 88, IMPACT),
            self.update(8, 89, 0, typed=False), self.update(8, 90, None), self.update(8, 91, IMPACT),
            self.update(8, 92, RAISED), self.update(8, 93, IMPACT & ~DAMAGED_SHIELD_BIT), self.update(8, 94, RAISED),
        )
        first = ShieldBlockEvent(runtime_id=8, entity_id=REMOTE_ID, tick=87)
        second = ShieldBlockEvent(runtime_id=8, entity_id=REMOTE_ID, tick=93)
        self.assertEqual(await remote, first)
        self.assertEqual(all_events, [own_block, first, second])
        self.assertEqual(own_events, [own_block])
        self.assertEqual(remote_events, [first, second])

    async def test_own_startup_impact_seeds_a_silent_baseline(self):
        protocol = Protocol(2225)
        self.b = b = protocol.binding
        initial = self.update(7, 85, IMPACT)
        await self.join(runtime_id=7, entity_id=OWN_ID, startup=(
            protocol.encode(initial), protocol.encode(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN)),
        ))
        seen = []
        self.bot.subscribe_shield_blocks(seen.append, runtime_id=7)
        waiting = await self.arm(7)

        await self.deliver(initial)
        self.assertEqual(seen, [])
        self.assertFalse(waiting.done())

        await self.deliver(self.update(7, 86, RAISED), self.update(7, 87, IMPACT))
        event = ShieldBlockEvent(runtime_id=7, entity_id=OWN_ID, tick=87)
        self.assertEqual(await waiting, event)
        self.assertEqual(seen, [event])

    async def test_remote_spawn_seeds_silently_and_removal_ends_the_filter(self):
        await self.join()
        seen, old_instance = [], []
        self.bot.subscribe_shield_blocks(seen.append)
        await self.deliver(self.spawn(IMPACT))
        self.bot.subscribe_shield_blocks(old_instance.append, runtime_id=8)
        waiting = await self.arm(8)
        await self.deliver(self.update(8, 85, IMPACT))
        self.assertEqual(seen, [])
        self.assertFalse(waiting.done())

        await self.deliver(self.b.RemoveActorPacket(entity_id=REMOTE_ID))
        with self.assertRaises(LookupError):
            await waiting
        await self.deliver(self.spawn(None, entity_id=109), self.update(8, 86, IMPACT))
        self.assertEqual(seen, [ShieldBlockEvent(runtime_id=8, entity_id=109, tick=86)])
        self.assertEqual(old_instance, [])
