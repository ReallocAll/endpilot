"""Exercise visible entity lifecycles through the native packet reader."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot, EntityLifecycleEvent, PaintingSnapshot
from endpilot.entities import EntityState
from endpilot.protocol import RawPacket, write_batch
from test_scheduled_input import ManualClock, Wire


class EntityLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("LifecycleWatch", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.protocol = self.bot.protocol
        self.b = self.protocol.binding

    async def join(self, *before_spawn):
        b = self.b
        self.wire = Wire(self.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.incoming.put_nowait(write_batch(map(self.protocol.encode, (
            b.StartGamePacket(runtime_id=7, entity_id=17),
            *before_spawn,
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ))))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket(time=23)
        processed = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(
            packet if isinstance(packet, RawPacket) else self.protocol.encode(packet)
            for packet in (*packets, marker)
        ))
        await processed

    def actor(self, entity_id=108, runtime_id=8, *, actor_type="minecraft:zombie"):
        b = self.b
        return b.AddActorPacket(entity_id=entity_id, runtime_id=runtime_id, actor_type=actor_type,
                                pos=b.Vec3(x=1.5, y=100, z=2.5))

    def player(self, uuid, runtime_id, entity_id=0):
        b = self.b
        return b.AddPlayerPacket(uuid=uuid, runtime_id=runtime_id, name="Peer",
                                 abilities_data=b.SerializedAbilitiesData(target_player=entity_id))

    def roster_add(self, uuid, entity_id):
        b = self.b
        return b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=uuid, id=entity_id, name="Peer",
        )])

    def roster_remove(self, *uuids):
        b = self.b
        return b.PlayerListPacket(entries=[b.PlayerListPacket.RemoveEntry(
            action=b.PlayerListPacketType.REMOVE, uuid=uuid,
        ) for uuid in uuids])

    def item(self):
        b = self.b
        return b.AddItemActorPacket(id=109, runtime_id=9, pos=b.Vec3(x=2, y=3, z=4),
                                    item=b.SerializedNetworkItemStackDescriptor(id=5, stack_size=2))

    def owner_metadata(self, owner, *, tamed=True, owner_type=None, sitting=False):
        b = self.b
        flags = (1 << int(b.ActorFlags.TAMED)) if tamed else 0
        if sitting:
            flags |= 1 << int(b.ActorFlags.SITTING)
        fields = [b.DataItemEntry(id=int(b.ActorDataIDs.RESERVED_0),
                                  payload=b.DataItemInt64Payload(value=flags))]
        if owner is not None:
            payload = owner_type or b.DataItemInt64Payload
            fields.append(b.DataItemEntry(id=int(b.ActorDataIDs.OWNER), payload=payload(value=owner)))
        return b.SynchedActorData.CopyableDataList(data=fields)

    async def test_wolf_owner_updates_only_from_typed_owner_metadata(self):
        b = self.b
        owner_id = -4294967295
        wolf = self.actor(actor_type="minecraft:wolf")
        wolf.data = self.owner_metadata(-1, tamed=False)
        await self.join()
        await self.deliver(wolf)
        untamed, = self.bot.observed_entities
        self.assertIsNone(untamed.owner_entity_id)

        async def update(owner, *, owner_type=None):
            await self.deliver(b.SetActorDataPacket(id=wolf.runtime_id,
                                                   packed_items=self.owner_metadata(owner, owner_type=owner_type)))
            current, = self.bot.observed_entities
            return current.owner_entity_id

        self.assertIsNone(await update(None))
        self.assertIsNone(await update(17, owner_type=b.DataItemIntPayload))
        self.assertEqual(await update(owner_id), owner_id)
        self.assertEqual(await update(None), owner_id)
        self.assertEqual(await update(17, owner_type=b.DataItemIntPayload), owner_id)
        self.assertIsNone(await update(-1))
        self.assertIsNone(untamed.owner_entity_id)

    async def test_wolf_owner_is_available_on_spawn_and_cleared_by_actor_replacement(self):
        b = self.b
        owner_id = -4294967295
        wolf = self.actor(actor_type="minecraft:wolf")
        wolf.data = self.owner_metadata(owner_id)
        await self.join(wolf)
        initial, = self.bot.observed_entities
        self.assertEqual(initial.owner_entity_id, owner_id)
        self.assertEqual(self.bot.observed_players, ())

        replacement = self.actor(entity_id=109, actor_type="minecraft:wolf")
        await self.deliver(b.RemoveActorPacket(entity_id=wolf.entity_id), replacement)
        current, = self.bot.observed_entities
        self.assertEqual((current.entity_id, current.owner_entity_id), (replacement.entity_id, None))
        self.assertEqual(initial.owner_entity_id, owner_id)

    async def test_wolf_sitting_tracks_valid_flags_across_sparse_updates(self):
        b = self.b
        owner_id = -4294967295
        wolf = self.actor(actor_type="minecraft:wolf")
        wolf.data = self.owner_metadata(owner_id)
        control = self.actor(entity_id=109, runtime_id=9, actor_type="minecraft:wolf")
        control.data = self.owner_metadata(-1, tamed=False)
        await self.join()
        await self.deliver(wolf, control)

        def observed(runtime_id):
            return next(actor for actor in self.bot.observed_entities if actor.runtime_id == runtime_id)

        initial = observed(wolf.runtime_id)
        self.assertIs(initial.sitting, False)
        await self.deliver(b.SetActorDataPacket(id=wolf.runtime_id, packed_items=self.owner_metadata(None, sitting=True)))
        sitting = observed(wolf.runtime_id)
        self.assertIs(sitting.sitting, True)
        self.assertIs(initial.sitting, False)
        self.assertEqual(sitting.owner_entity_id, owner_id)

        owner_only = b.DataItemEntry(id=int(b.ActorDataIDs.OWNER), payload=b.DataItemInt64Payload(value=owner_id))
        wrong_flags = b.DataItemEntry(id=int(b.ActorDataIDs.RESERVED_0), payload=b.DataItemIntPayload(value=0))
        for entry in (owner_only, wrong_flags):
            metadata = b.SynchedActorData.CopyableDataList(data=[entry])
            await self.deliver(b.SetActorDataPacket(id=wolf.runtime_id, packed_items=metadata))
            self.assertIs(observed(wolf.runtime_id).sitting, True)

        await self.deliver(b.SetActorDataPacket(id=wolf.runtime_id, packed_items=self.owner_metadata(None)))
        self.assertIs(observed(wolf.runtime_id).sitting, False)
        self.assertIs(sitting.sitting, True)
        self.assertIs(observed(control.runtime_id).sitting, False)

    async def test_wolf_sitting_is_scoped_to_the_visible_actor_instance(self):
        b = self.b
        wolf = self.actor(actor_type="minecraft:wolf")
        wolf.data = self.owner_metadata(-4294967295, sitting=True)
        other = self.actor(entity_id=109, runtime_id=9, actor_type="minecraft:zombie")
        other.data = wolf.data
        await self.join(wolf, other)
        initial, observed_other = self.bot.observed_entities
        self.assertIs(initial.sitting, True)
        self.assertIsNone(observed_other.sitting)

        replacement = self.actor(entity_id=110, actor_type="minecraft:wolf")
        wrong_flags = b.DataItemEntry(id=int(b.ActorDataIDs.RESERVED_0), payload=b.DataItemIntPayload(value=0))
        await self.deliver(b.RemoveActorPacket(entity_id=wolf.entity_id), replacement,
                           b.SetActorDataPacket(id=replacement.runtime_id,
                                                packed_items=b.SynchedActorData.CopyableDataList(data=[wrong_flags])))
        current = next(actor for actor in self.bot.observed_entities if actor.runtime_id == replacement.runtime_id)
        self.assertEqual(current.entity_id, replacement.entity_id)
        self.assertIsNone(current.sitting)
        self.assertIs(initial.sitting, True)

    async def test_all_add_families_publish_immutable_physical_snapshots(self):
        await self.join()
        b = self.b
        player_fixture = Path(__file__).with_name("fixtures") / "remote-player-add-2225.hex"
        raw_player = RawPacket(b.AddPlayerPacket.ID, bytes.fromhex(player_fixture.read_text(encoding="ascii")))
        player = UUID("00000000-0000-4000-8000-000000000001")
        painting = PaintingSnapshot(-4294967287, 10, (8.0, 223.0, 13.96875), 2, "unpacked")
        seen = []
        self.bot.subscribe_entity_lifecycle(
            lambda event: seen.append((event, self.bot.observed_entities)),
        )
        await self.deliver(
            self.roster_add(player, 0x1122334455667788),
            b.ItemRegistryPacket(items=[b.ItemData(id=5, name="minecraft:diamond")]),
            self.actor(), raw_player, self.item(),
            b.AddPaintingPacket(entity_id=painting.entity_id, runtime_id=painting.runtime_id,
                                pos=b.Vec3(x=8, y=223, z=13.96875), dir=painting.dir, motif=painting.motif),
            self.player(UUID(int=7), 7, 17),
        )
        self.assertEqual([(event.kind, event.reason, event.entity.actor_type) for event, _ in seen], [
            ("entered", "spawn_packet", "minecraft:zombie"),
            ("entered", "spawn_packet", "minecraft:player"),
            ("entered", "spawn_packet", "minecraft:item"),
            ("entered", "spawn_packet", "minecraft:painting"),
        ])
        actor, remote, drop, artwork = [event.entity for event, _ in seen]
        self.assertEqual(actor, EntityState(108, 8, "minecraft:zombie", (1.5, 100, 2.5)))
        self.assertEqual((remote.entity_id, remote.runtime_id, remote.name, remote.position),
                         (0x1122334455667788, 1, "FixtureActor", (8.5, 220, 8.5)))
        self.assertEqual(drop, EntityState(109, 9, "minecraft:item", (2, 3, 4), "minecraft:diamond", 2, 5))
        self.assertEqual(artwork, EntityState(
            painting.entity_id, 10, "minecraft:painting", painting.position, painting=painting,
        ))
        self.assertEqual([states for _, states in seen], [
            (actor,), (actor, remote), (actor, remote, drop), (actor, remote, drop, artwork),
        ])
        with self.assertRaises(FrozenInstanceError):
            seen[0][0].kind = "left"
        with self.assertRaises(FrozenInstanceError):
            actor.position = (0, 0, 0)

    async def test_same_ids_reenter_and_replacement_publishes_old_before_new(self):
        await self.join()
        b = self.b
        uuid = UUID(int=1)
        player = self.player(uuid, 8, 108)
        seen = []
        self.bot.subscribe_entity_lifecycle(
            lambda event: seen.append((event, self.bot.observed_entities)),
        )
        await self.deliver(
            self.roster_add(uuid, 108),
            player,
            b.MovePlayerPacket(player_id=8, pos=b.Vec3(x=2, y=72, z=5)),
            b.RemoveActorPacket(entity_id=108),
            b.RemoveActorPacket(entity_id=108),
            b.RemoveActorPacket(entity_id=8),
            player,
            self.actor(entity_id=208),
        )
        entered = EntityState(108, 8, "minecraft:player", (0, 0, 0), name="Peer")
        moved = replace(entered, position=(2, 72, 5))
        replacement = EntityState(208, 8, "minecraft:zombie", (1.5, 100, 2.5))
        self.assertEqual(seen, [
            (EntityLifecycleEvent(kind="entered", entity=entered, reason="spawn_packet"), (entered,)),
            (EntityLifecycleEvent(kind="left", entity=moved, reason="remove_actor"), ()),
            (EntityLifecycleEvent(kind="entered", entity=entered, reason="spawn_packet"), (entered,)),
            (EntityLifecycleEvent(kind="left", entity=entered, reason="replaced"), (replacement,)),
            (EntityLifecycleEvent(kind="entered", entity=replacement, reason="spawn_packet"), (replacement,)),
        ])

    async def test_pickup_then_unique_id_removal_leaves_a_tracked_drop_once(self):
        await self.join()
        b = self.b
        seen = []
        self.bot.subscribe_entity_lifecycle(seen.append)
        await self.deliver(
            self.item(),
            b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
                runtime_id=9, pos=b.Vec3(x=5, y=6, z=7),
            )),
            b.TakeItemActorPacket(item_id=9, actor_id=7),
            b.RemoveActorPacket(entity_id=109),
            b.TakeItemActorPacket(item_id=9, actor_id=7),
            b.TakeItemActorPacket(item_id=999, actor_id=7),
        )
        drop = EntityState(109, 9, "minecraft:item", (2, 3, 4), "#5", 2, 5)
        self.assertEqual(seen, [
            EntityLifecycleEvent(kind="entered", entity=drop, reason="spawn_packet"),
            EntityLifecycleEvent(kind="left", entity=replace(drop, position=(5, 6, 7)), reason="pickup"),
        ])
        self.assertEqual(self.bot.observed_entities, ())

    async def test_roster_corrections_and_actual_evictions_use_final_cache(self):
        await self.join()
        b = self.b
        first, second, late, unknown = (UUID(int=value) for value in range(1, 5))
        await self.deliver(self.player(first, 8, 108), self.roster_add(second, 109), self.player(second, 9, 109))
        seen = []
        self.bot.subscribe_entity_lifecycle(
            lambda event: seen.append((event, self.bot.observed_entities, self.bot.observed_players)),
        )
        await self.deliver(self.roster_add(first, 108), self.roster_add(first, 208), self.roster_remove(unknown))
        self.assertEqual(seen, [])
        first_state, second_state = self.bot.observed_entities
        self.assertEqual((first_state.runtime_id, first_state.entity_id), (8, 208))

        await self.deliver(self.roster_remove(first, second))
        self.assertEqual(seen, [
            (EntityLifecycleEvent(kind="left", entity=state, reason="roster_removed"), (), ())
            for state in (first_state, second_state)
        ])
        await self.deliver(self.player(late, 10), b.RemoveActorPacket(entity_id=110))
        late_state, = self.bot.observed_entities
        self.assertEqual((late_state.runtime_id, late_state.entity_id), (10, 0))
        self.assertEqual(seen[-1][0], EntityLifecycleEvent(kind="entered", entity=late_state, reason="spawn_packet"))
        self.assertEqual(len(seen), 3)

        await self.deliver(self.roster_add(late, 110))
        entry, = self.bot.observed_players
        self.assertEqual((entry.uuid, entry.entity_id), (late, 110))
        self.assertEqual(seen[3:], [
            (EntityLifecycleEvent(kind="left", entity=late_state, reason="identity_invalidated"), (), (entry,)),
        ])

    async def test_remote_runtime_leaves_after_roster_projects_own_unique_id(self):
        await self.join()
        b = self.b
        uuid = UUID(int=1)
        seen = []
        self.bot.subscribe_entity_lifecycle(
            lambda event: seen.append((event, self.bot.observed_entities)),
        )
        await self.deliver(self.roster_add(uuid, 108), self.player(uuid, 8, 108), self.actor(17, 7))
        remote = EntityState(108, 8, "minecraft:player", (0, 0, 0), name="Peer")
        entered = EntityLifecycleEvent(kind="entered", entity=remote, reason="spawn_packet")
        self.assertEqual(seen, [(entered, (remote,))])

        await self.deliver(self.roster_add(uuid, 17))
        projected = replace(remote, entity_id=17)
        self.assertEqual([state for state in self.bot.observed_entities if state.runtime_id == 8], [projected])
        self.assertEqual(seen, [(entered, (remote,))])

        await self.deliver(b.ChangeDimensionPacket(dimension_id=1))
        left = EntityLifecycleEvent(kind="left", entity=projected, reason="dimension_changed")
        self.assertEqual(seen, [(entered, (remote,)), (left, ())])
        self.assertEqual(self.bot.observed_entities, ())

    async def test_dimension_batch_and_observer_registration_during_reduction(self):
        await self.join()
        b = self.b
        await self.deliver(self.item(), self.actor(), b.SetActorLinkPacket(
            link=b.ActorLink(a=108, b=17, type=b.ActorLinkType.RIDING),
        ))
        before = self.bot.observed_entities
        self.assertEqual(tuple(state.runtime_id for state in before), (9, 8))
        seen, added = [], []
        self.bot.subscribe_entity_lifecycle(
            lambda event: seen.append((event, self.bot.observed_entities)),
        )

        def register_on_link_cleanup(change):
            if change.reason == "unobserved":
                self.bot.subscribe_entity_lifecycle(added.append)

        self.bot.subscribe_vehicle_links(register_on_link_cleanup)
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1), self.actor(208))
        reentered = EntityState(208, 8, "minecraft:zombie", (1.5, 100, 2.5))
        next_event = EntityLifecycleEvent(kind="entered", entity=reentered, reason="spawn_packet")
        self.assertEqual(seen[:-1], [
            (EntityLifecycleEvent(kind="left", entity=state, reason="dimension_changed"), ())
            for state in before
        ])
        self.assertEqual(seen[-1], (next_event, (reentered,)))
        self.assertEqual(added, [next_event])

    async def test_callback_exceptions_do_not_block_later_observers_or_raw_packets(self):
        await self.join()
        seen, raw_packets, errors = [], [], []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: errors.append(context["exception"]))

        def fail(_event):
            raise ValueError("observer failed")

        def cancel(_event):
            raise asyncio.CancelledError("observer cancelled")

        stop_fail = self.bot.subscribe_entity_lifecycle(fail)
        stop_cancel = self.bot.subscribe_entity_lifecycle(cancel)
        self.bot.subscribe_entity_lifecycle(seen.append)
        self.bot.packets.subscribe_raw(raw_packets.append)
        spawned = self.actor()
        await self.deliver(spawned)
        for stop in (stop_fail, stop_cancel):
            stop()
            stop()
        removed = self.b.RemoveActorPacket(entity_id=108)
        await self.deliver(removed)
        self.assertEqual([type(error) for error in errors], [ValueError, asyncio.CancelledError])
        self.assertEqual([(event.kind, event.reason) for event in seen],
                         [("entered", "spawn_packet"), ("left", "remove_actor")])
        marker = self.b.SetTimePacket(time=23)
        self.assertEqual(raw_packets, list(map(self.protocol.encode, (spawned, marker, removed, marker))))

    async def test_subscription_has_no_startup_replay_and_ends_with_its_session(self):
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.subscribe_entity_lifecycle(lambda _: None)
        await self.join(self.actor())
        self.assertEqual([entity.runtime_id for entity in self.bot.observed_entities], [8])
        old_events, new_events = [], []
        stop_old = self.bot.subscribe_entity_lifecycle(old_events.append)
        self.assertEqual(old_events, [])
        await self.deliver(self.b.DisconnectPacket(), self.actor(109, 9))
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_entity_lifecycle(old_events.append)
        await self.bot.close()

        await self.join()
        self.bot.subscribe_entity_lifecycle(new_events.append)
        stop_old()
        stop_old()
        await self.deliver(self.actor())
        await self.bot.close()
        self.assertEqual(old_events, [])
        self.assertEqual([(event.kind, event.reason, event.entity.runtime_id) for event in new_events],
                         [("entered", "spawn_packet", 8)])
