"""Observe Elytra firework attachments through the native packet reader."""

import asyncio

import test_entity_crouch as crouch_fixture
import test_entity_glide as glide_fixture


LOCAL_ID = -4294967294
FOREIGN_ID = -4294967295


class FireworkAttachmentTests(glide_fixture._GlideWireTestCase):
    def spawn(self, runtime_id, *metadata, actor_type="minecraft:fireworks_rocket"):
        b = self.b
        return b.AddActorPacket(
            runtime_id=runtime_id, entity_id=100 + runtime_id, actor_type=actor_type,
            data=b.SynchedActorData.CopyableDataList(data=metadata),
        )

    def attachment(self, value):
        b = self.b
        return b.DataItemEntry(id=int(b.ActorDataIDs.CUSTOM_DISPLAY), payload=b.DataItemInt64Payload(value=value))

    async def test_only_typed_rocket_attachment_populates_the_snapshot(self):
        await self.join(runtime_id=7, entity_id=LOCAL_ID)
        b = self.b
        owner = b.DataItemEntry(id=int(b.ActorDataIDs.OWNER), payload=b.DataItemInt64Payload(value=LOCAL_ID))
        wrong_type = b.DataItemEntry(id=int(b.ActorDataIDs.CUSTOM_DISPLAY), payload=b.DataItemIntPayload(value=7))
        seen = []
        self.bot.subscribe_attached_fireworks(seen.append)
        await self.deliver(
            self.spawn(8, self.attachment(FOREIGN_ID), owner),
            self.spawn(9, self.attachment(-1), owner),
            self.spawn(10, owner),
            self.spawn(11, wrong_type, owner),
            self.spawn(12, self.attachment(LOCAL_ID), actor_type="minecraft:sheep"),
            self.spawn(13, self.attachment(7)),
        )
        self.assertEqual([self.entity(runtime).firework_attached_entity_id for runtime in range(8, 14)],
                         [FOREIGN_ID, None, None, None, None, 7])
        self.assertEqual(seen, [])

    async def test_local_attachment_waits_for_a_new_visible_rocket(self):
        await self.join(runtime_id=7, entity_id=LOCAL_ID)
        rocket = self.spawn(8, self.attachment(LOCAL_ID))
        seen = []

        def observe(entity):
            seen.append((entity, self.entity(entity.runtime_id)))

        self.bot.subscribe_attached_fireworks(observe)
        await self.deliver(rocket)
        first = self.entity()
        self.assertEqual(seen, [(first, first)])
        self.assertIs(seen[0][0], seen[0][1])
        self.assertEqual(first.firework_attached_entity_id, LOCAL_ID)

        waiting = asyncio.create_task(self.bot.wait_for_attached_firework(timeout=1))
        self.addAsyncCleanup(crouch_fixture.EntityCrouchTests.cancel_wait, waiting)
        await asyncio.sleep(0)
        await self.deliver(rocket)
        self.assertFalse(waiting.done())
        self.assertEqual(seen, [(first, first)])

        await self.deliver(self.b.RemoveActorPacket(entity_id=rocket.entity_id))
        self.assertIsNone(self.entity())
        await self.deliver(rocket)
        second = await waiting
        self.assertIs(second, self.entity())
        self.assertIsNot(second, first)
        self.assertEqual(second.firework_attached_entity_id, LOCAL_ID)
        self.assertEqual(seen, [(first, first), (second, second)])
        self.assertIs(seen[1][0], seen[1][1])
