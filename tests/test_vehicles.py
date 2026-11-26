import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID
import weakref

from endpilot import Bot, VehicleLink, VehicleLinkChange
from endpilot.protocol import read_batch, write_batch

from test_scheduled_input import ManualClock


class VehicleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("Rider", protocol=2225)
        self.binding = self.bot.protocol.binding
        self.incoming = asyncio.Queue()
        self.outgoing = []

        async def recv():
            data = await self.incoming.get()
            if isinstance(data, Exception):
                raise data
            return data

        self.transport = SimpleNamespace(
            recv=recv, send=AsyncMock(side_effect=self.outgoing.append), close=AsyncMock(),
        )
        self.addAsyncCleanup(self.bot.close)

    def push(self, *packets):
        self.incoming.put_nowait(write_batch([self.bot.protocol.encode(packet) for packet in packets]))

    async def join(self, *initial):
        b = self.binding
        self.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.push(b.StartGamePacket(runtime_id=9, entity_id=-2001), *initial,
                  b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    async def deliver(self, *packets):
        seen = asyncio.create_task(self.bot.packets.wait_for_packet(type(packets[-1]).ID, timeout=1))
        await asyncio.sleep(0)
        self.push(*packets)
        await seen

    def link(self, vehicle, rider, role):
        return self.binding.ActorLink(a=vehicle, b=rider, type=self.binding.ActorLinkType[role])

    def sent(self, kind):
        return [self.bot.protocol.decode(raw) for batch in self.outgoing for raw in read_batch(batch)
                if raw.packet_id == kind.ID]

    async def next_input(self):
        ready = asyncio.Event()
        kind = self.binding.PlayerAuthInputPacket

        def observe(packet_id):
            if packet_id == kind.ID:
                ready.set()

        unsubscribe = self.bot.packets.subscribe_sent(observe)
        try:
            await asyncio.wait_for(ready.wait(), 1)
        finally:
            unsubscribe()
        return self.sent(kind)[-1]

    async def join_on_boat(self, role="RIDING"):
        b = self.binding
        await self.join(b.AddActorPacket(
            entity_id=-4001, runtime_id=50, actor_type="minecraft:boat",
            pos=b.Vec3(x=4, y=80, z=7), rot=b.Vec2(x=0, y=28),
            links=[self.link(-4001, -2001, role)],
        ))
        await self.deliver(b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=4, y=81, z=7)))

    async def test_boat_control_uses_scheduled_frames_and_server_vehicle_correction(self):
        clock = ManualClock()
        self.enterContext(clock.patch_scheduler())
        b = self.binding
        await self.join_on_boat()
        await clock.wake_at(50)
        initial, = self.sent(b.PlayerAuthInputPacket)
        await self.bot.look_at((12, 81, 7))
        sent_before = len(self.outgoing)
        await self.bot.set_boat_input(forward=True, turn="left")
        self.assertEqual(len(self.outgoing), sent_before)
        await clock.wake_at(100)
        first = self.sent(b.PlayerAuthInputPacket)[-1]
        self.assertEqual(first.client_tick, initial.client_tick + 1)
        self.assertEqual(first.client_predicted_vehicle, -4001)
        self.assertIn(b.PlayerAuthInputPacket.InputData.IS_IN_CLIENT_PREDICTED_VEHICLE, first.input_data)
        self.assertEqual((first.move.x, first.move.y), (1, 1))
        self.assertEqual((first.pos.x, first.pos.y, first.pos.z), (4, 80, 7))
        self.assertEqual((first.rot.y, first.vehicle_rot.y), (-90, 28))

        await clock.wake_at(150)
        held = self.sent(b.PlayerAuthInputPacket)[-1]
        self.assertEqual(held.client_tick, first.client_tick + 1)
        self.assertEqual((held.move.x, held.move.y, held.client_predicted_vehicle), (1, 1, -4001))

        correction = b.CorrectPlayerMovePredictionPacket(
            prediction_type=b.RewindType.VEHICLE, tick=held.client_tick,
            pos=b.Vec3(x=6, y=80, z=8), pos_delta=b.Vec3(x=0.25, y=0, z=0.125),
            vehicle_rotation=b.Vec2(x=0, y=47), vehicle_angular_velocity=2,
        )
        await self.deliver(correction)
        sent_before = len(self.outgoing)
        await self.bot.set_boat_input(turn="right")
        self.assertEqual(len(self.outgoing), sent_before)
        await clock.wake_at(200)
        corrected = self.sent(b.PlayerAuthInputPacket)[-1]
        self.assertEqual((corrected.move.x, corrected.move.y), (-1, 0))
        self.assertEqual((corrected.pos.x, corrected.pos.y, corrected.pos.z), (6, 80, 8))
        self.assertEqual((corrected.pos_delta.x, corrected.pos_delta.y, corrected.pos_delta.z), (0.25, 0, 0.125))
        self.assertEqual((corrected.rot.y, corrected.vehicle_rot.y), (-90, 47))
        self.assertEqual(self.bot.position, (4, 81, 7))

        sent_before = len(self.outgoing)
        await self.bot.stop_moving()
        self.assertEqual(len(self.outgoing), sent_before)
        await clock.wake_at(250)
        released = self.sent(b.PlayerAuthInputPacket)[-1]
        self.assertEqual((released.move.x, released.move.y, released.client_predicted_vehicle), (0, 0, -4001))
        self.assertEqual((released.pos.x, released.pos.y, released.pos.z), (6, 80, 8))
        self.assertEqual([frame.client_tick for frame in self.sent(b.PlayerAuthInputPacket)],
                         list(range(initial.client_tick, initial.client_tick + 5)))

    async def test_boat_held_input_expires_on_reseat_and_dimension_change(self):
        b = self.binding
        await self.join_on_boat()
        await self.bot.set_boat_input(forward=True)
        before = await self.next_input()
        self.assertEqual((before.move.y, before.client_predicted_vehicle), (1, -4001))

        await self.deliver(
            b.SetActorLinkPacket(link=self.link(-4001, -2001, "NONE")),
            b.SetActorLinkPacket(link=self.link(-4001, -2001, "RIDING")),
        )
        amount = len(self.sent(b.PlayerAuthInputPacket))
        await self.bot.wait_ticks(2)
        self.assertEqual(len(self.sent(b.PlayerAuthInputPacket)), amount)

        await self.bot.set_boat_input(forward=True)
        await self.deliver(b.CorrectPlayerMovePredictionPacket(
            prediction_type=b.RewindType.VEHICLE, tick=before.client_tick,
            pos=b.Vec3(x=20, y=80, z=20), vehicle_rotation=b.Vec2(y=99),
        ))
        fresh = await self.next_input()
        self.assertEqual((fresh.pos.x, fresh.vehicle_rot.y), (4, 28))
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(self.bot.position)
        with self.assertRaisesRegex(RuntimeError, "observed driver seat"):
            await self.bot.set_boat_input(forward=True)
        amount = len(self.sent(b.PlayerAuthInputPacket))
        await self.deliver(b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=14, y=81, z=7)))
        await self.bot.wait_ticks(2)
        self.assertEqual(len(self.sent(b.PlayerAuthInputPacket)), amount)
        await self.deliver(b.PlayerActionPacket(runtime_id=9, action=b.PlayerActionType.CHANGE_DIMENSION_ACK))
        await self.bot.set_move_vector(0, 0.5)
        on_foot = await self.next_input()
        self.assertEqual((on_foot.move.y, on_foot.client_predicted_vehicle), (0.5, None))
        self.assertEqual(on_foot.pos.x, 14)

    async def test_nearby_follow_and_walk_cannot_take_the_boat_input(self):
        await self.join_on_boat()
        await self.bot.set_boat_input(forward=True)
        driven = await self.next_input()
        following = asyncio.create_task(self.bot.follow_entity(50, ticks=2))
        await asyncio.sleep(0)
        await self.bot.stop_moving()
        with self.assertRaisesRegex(RuntimeError, "ground movement requires leaving"):
            await following
        with self.assertRaisesRegex(RuntimeError, "ground movement requires leaving"):
            await self.bot.walk_forward(2)
        released = await self.next_input()
        self.assertGreater(released.client_tick, driven.client_tick)
        self.assertEqual((released.move.y, released.client_predicted_vehicle), (0, -4001))

    async def test_dismount_without_self_pose_waits_after_the_boat_has_moved(self):
        b = self.binding
        await self.join_on_boat()
        await self.bot.set_boat_input(forward=True)
        first = await self.next_input()
        await self.deliver(b.CorrectPlayerMovePredictionPacket(
            prediction_type=b.RewindType.PLAYER, tick=first.client_tick,
            pos=b.Vec3(x=4, y=81, z=7),
        ))
        advanced = await self.next_input()
        await self.deliver(b.CorrectPlayerMovePredictionPacket(
            prediction_type=b.RewindType.VEHICLE, tick=advanced.client_tick,
            pos=b.Vec3(x=24, y=80, z=7), vehicle_rotation=b.Vec2(y=28),
        ))
        moved = await self.next_input()
        self.assertEqual((moved.pos.x, self.bot.position), (24, (4, 81, 7)))
        await self.deliver(
            b.SetActorLinkPacket(link=self.link(-4001, -2001, "NONE")),
            b.MovePlayerPacket(player_id=9, riding_id=50, pos=b.Vec3(x=4, y=81, z=7)),
            b.CorrectPlayerMovePredictionPacket(
                prediction_type=b.RewindType.PLAYER, tick=first.client_tick, pos=b.Vec3(x=4, y=81, z=7),
            ),
        )
        first_foot = len(self.sent(b.PlayerAuthInputPacket))
        self.assertIsNone(self.bot.position)
        self.assertIsNone(self.bot._movement.raycast_pose[0])
        for action, args in (
            (self.bot.set_move_vector, (0, 1)),
            (self.bot.move_to, ((25, 80, 7),)),
            (self.bot.walk_forward, (2,)),
            (self.bot.follow_entity, (50,)),
        ):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(RuntimeError, "not synchronized"):
                await asyncio.wait_for(action(*args), timeout=0.25)
            self.assertFalse(self.bot.movement_state.moving)
        waiting = asyncio.create_task(self.bot.wait_for_position_sync(timeout=1))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await asyncio.wait_for(self.bot.stop_moving(), timeout=0.25)
        await self.bot.wait_ticks(2)
        self.assertEqual(len(self.sent(b.PlayerAuthInputPacket)), first_foot)
        await self.deliver(b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=24.5, y=81.25, z=7.5)))
        foot = await self.next_input()
        self.assertEqual((foot.pos.x, foot.pos.y, foot.pos.z, foot.client_predicted_vehicle),
                         (24.5, 81.25, 7.5, None))
        self.assertEqual(self.bot._movement.raycast_pose[0], (24.5, 81.25, 7.5))
        await self.bot.set_move_vector(0, 0.5)
        moving = await self.next_input()
        self.assertEqual((moving.move.y, moving.client_predicted_vehicle), (0.5, None))

    async def test_active_dismount_keeps_the_own_correction_before_the_link(self):
        b = self.binding
        await self.join_on_boat()
        await self.bot.set_boat_input(forward=True)
        driven = await self.next_input()
        await self.deliver(b.CorrectPlayerMovePredictionPacket(
            prediction_type=b.RewindType.VEHICLE, tick=driven.client_tick,
            pos=b.Vec3(x=24, y=80, z=7), vehicle_rotation=b.Vec2(y=28),
        ))
        adjusted = await self.next_input()
        await self.bot.dismount()
        request, = self.sent(b.InteractPacket)
        self.assertEqual(request.action, b.InteractPacket.Action.STOP_RIDING)
        await self.deliver(
            b.CorrectPlayerMovePredictionPacket(
                prediction_type=b.RewindType.PLAYER, tick=adjusted.client_tick,
                pos=b.Vec3(x=24.75, y=81.5, z=8),
            ),
            b.SetActorLinkPacket(link=self.link(-4001, -2001, "NONE")),
        )
        self.assertEqual(await self.bot.wait_for_position_sync(timeout=0.1), (24.75, 81.5, 8))
        foot = await self.next_input()
        self.assertEqual((foot.pos.x, foot.pos.y, foot.pos.z, foot.client_predicted_vehicle),
                         (24.75, 81.5, 8, None))
        self.assertEqual(self.bot._movement.raycast_pose[0], (24.75, 81.5, 8))

    async def test_boat_passenger_cannot_send_driver_controls(self):
        b = self.binding
        await self.join_on_boat("PASSENGER")

        with self.assertRaisesRegex(RuntimeError, "observed driver seat"):
            await self.bot.set_boat_input(forward=True)
        packet = await self.next_input()
        self.assertIsNone(packet.client_predicted_vehicle)
        self.assertNotIn(b.PlayerAuthInputPacket.InputData.IS_IN_CLIENT_PREDICTED_VEHICLE, packet.input_data)

    async def prepare_nearest_player_boat(self):
        b = self.binding
        nearby, other = UUID(int=101), UUID(int=102)
        await self.join()
        await self.deliver(
            b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=0, y=101.62, z=0)),
            b.PlayerListPacket(entries=[
                b.PlayerListPacket.AddEntry(action=b.PlayerListPacketType.ADD, uuid=nearby, id=101, name="Nearby"),
                b.PlayerListPacket.AddEntry(action=b.PlayerListPacketType.ADD, uuid=other, id=102, name="Other"),
            ]),
            b.AddPlayerPacket(uuid=nearby, runtime_id=11, name="Nearby", pos=b.Vec3(x=2, y=100, z=0)),
            b.AddPlayerPacket(uuid=other, runtime_id=12, name="Other", pos=b.Vec3(x=14, y=100, z=0)),
            b.AddActorPacket(entity_id=-4001, runtime_id=50, actor_type="minecraft:boat",
                             pos=b.Vec3(x=2, y=100, z=0)),
        )
        self.assertEqual(self.bot.nearest_player().entity_id, 101)

    def moved_nearest_player_boat(self):
        b = self.binding
        return b.MoveActorAbsolutePacket(move_data=b.MoveActorAbsoluteData(
            runtime_id=50, pos=b.Vec3(x=13, y=100, z=0),
        ))

    async def test_nearest_player_waits_for_the_local_passengers_own_dismount_pose(self):
        b = self.binding
        await self.prepare_nearest_player_boat()
        await self.deliver(
            b.SetActorLinkPacket(link=self.link(-4001, -2001, "PASSENGER")),
            self.moved_nearest_player_boat(),
        )
        self.assertEqual(self.bot.vehicle.role, "PASSENGER")
        self.assertIsNone(self.bot.nearest_player())
        await self.deliver(b.SetActorLinkPacket(link=self.link(-4001, -2001, "NONE")))
        self.assertIsNone(self.bot.nearest_player())
        await self.deliver(b.MovePlayerPacket(player_id=9, riding_id=50, pos=b.Vec3(x=13, y=101, z=0)))
        self.assertIsNone(self.bot.nearest_player())
        await self.deliver(b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=13, y=101.62, z=0)))
        self.assertEqual(self.bot.nearest_player().entity_id, 102)

    async def test_nearest_player_uses_other_players_during_remote_passenger_movement(self):
        b = self.binding
        await self.prepare_nearest_player_boat()
        await self.deliver(
            b.SetActorLinkPacket(link=self.link(-4001, 101, "PASSENGER")),
            self.moved_nearest_player_boat(),
        )
        self.assertEqual(self.bot.nearest_player().entity_id, 102)
        self.assertIsNone(self.bot.nearest_player(lambda player: player.entity_id == 101))
        await self.deliver(b.SetActorLinkPacket(link=self.link(-4001, 101, "NONE")))
        self.assertEqual(self.bot.nearest_player().entity_id, 102)
        await self.deliver(b.MovePlayerPacket(player_id=11, riding_id=50, pos=b.Vec3(x=13, y=101, z=0)))
        self.assertIsNone(self.bot.nearest_player(lambda player: player.entity_id == 101))
        await self.deliver(b.MovePlayerPacket(player_id=11, pos=b.Vec3(x=1, y=101.62, z=0)))
        self.assertEqual(self.bot.nearest_player().entity_id, 101)

    async def test_startup_and_embedded_links_resolve_without_losing_the_rider(self):
        b = self.binding
        await self.join(
            b.SetActorLinkPacket(link=self.link(-4001, -2001, "RIDING")),
            b.AddActorPacket(entity_id=-4001, runtime_id=50, actor_type="minecraft:boat",
                             links=[self.link(-4001, -2002, "PASSENGER")]),
        )

        self.assertEqual(self.bot.vehicle, VehicleLink(-4001, -2001, "RIDING", 50, 9))
        self.assertIn(VehicleLink(-4001, -2002, "PASSENGER", 50, None), self.bot.vehicle_links)
        boat, = (actor for actor in self.bot.observed_entities if actor.actor_type == "minecraft:boat")
        self.assertEqual(boat.entity_id, -4001)

        observer_uuid = UUID(int=12)
        await self.deliver(b.AddPlayerPacket(uuid=observer_uuid, runtime_id=73, name="Passenger"))
        await self.deliver(b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=observer_uuid, id=-2002, name="Passenger",
        )]))
        self.assertIn(VehicleLink(-4001, -2002, "PASSENGER", 50, 73), self.bot.vehicle_links)

        await self.deliver(b.SetActorLinkPacket(link=self.link(-4999, -2001, "NONE")))
        self.assertIsNotNone(self.bot.vehicle)
        await self.deliver(b.SetActorLinkPacket(link=self.link(-4001, -2001, "NONE")))
        self.assertIsNone(self.bot.vehicle)
        self.assertEqual(self.bot.vehicle_links, (VehicleLink(-4001, -2002, "PASSENGER", 50, 73),))

        await self.deliver(b.AddPlayerPacket(uuid=observer_uuid, runtime_id=73, name="Passenger",
                                             links=[self.link(-4001, -2002, "RIDING")]))
        self.assertEqual(self.bot.vehicle_links, (VehicleLink(-4001, -2002, "RIDING", 50, 73),))

        await self.deliver(b.RemoveActorPacket(entity_id=-4001))
        self.assertEqual(self.bot.vehicle_links, ())

    async def test_mount_and_dismount_submit_without_predicting_a_link(self):
        b = self.binding
        await self.join(b.AddActorPacket(entity_id=-4001, runtime_id=50, actor_type="minecraft:boat"))
        await self.deliver(b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=0.5, y=81.62, z=0.5)))

        await self.bot.mount(50)
        transaction, = self.sent(b.InventoryTransactionPacket)
        self.assertEqual(transaction.variant_transaction.runtime_id, 50)
        self.assertEqual(transaction.variant_transaction.action_type, b.ItemUseOnActorInventoryTransaction.ActionType.INTERACT)
        self.assertIsNone(self.bot.vehicle)

        await self.deliver(b.SetActorLinkPacket(link=self.link(-4002, -2001, "PASSENGER")))
        unresolved = self.bot.vehicle
        self.assertEqual(unresolved, VehicleLink(-4002, -2001, "PASSENGER", None, 9))
        with self.assertRaisesRegex(RuntimeError, "not riding a vehicle the client knows"):
            await self.bot.dismount()

        await self.deliver(b.AddActorPacket(entity_id=-4002, runtime_id=51, actor_type="minecraft:minecart"))
        self.assertEqual(self.bot.vehicle, VehicleLink(-4002, -2001, "PASSENGER", 51, 9))
        self.assertIsNone(unresolved.vehicle_runtime_id)
        await self.bot.dismount()
        request, = self.sent(b.InteractPacket)
        self.assertEqual((request.action, request.target_id, request.pos), (b.InteractPacket.Action.STOP_RIDING, 51, None))
        self.assertEqual(self.bot.vehicle.vehicle_runtime_id, 51)

        await self.deliver(b.ChangeDimensionPacket(dimension_id=1))
        self.assertIsNone(self.bot.vehicle)
        self.assertEqual(self.bot.vehicle_links, ())

    async def check_mount_invalidated(self, *changes):
        b = self.binding
        await self.join(b.AddActorPacket(entity_id=-4001, runtime_id=50, actor_type="minecraft:boat"))
        self.assertIsNone(self.bot.position)
        mounting = asyncio.create_task(self.bot.mount(50))

        async def stop_mount():
            mounting.cancel()
            await asyncio.gather(mounting, return_exceptions=True)

        self.addAsyncCleanup(stop_mount)
        await asyncio.sleep(0)
        self.assertFalse(mounting.done())
        await self.deliver(*changes, b.MovePlayerPacket(player_id=9, pos=b.Vec3(x=0.5, y=81.62, z=0.5)))
        with self.assertRaisesRegex(RuntimeError, "vehicle target changed"):
            await asyncio.wait_for(mounting, 1)
        self.assertEqual(self.sent(b.InventoryTransactionPacket), [])

    async def test_mount_rejects_removed_and_readded_target_before_first_position(self):
        b = self.binding
        await self.check_mount_invalidated(
            b.RemoveActorPacket(entity_id=-4001),
            b.AddActorPacket(entity_id=-4001, runtime_id=50, actor_type="minecraft:boat"),
        )

    async def test_mount_rejects_rebound_runtime_before_first_position(self):
        b = self.binding
        await self.check_mount_invalidated(b.AddActorPacket(entity_id=-4002, runtime_id=50, actor_type="minecraft:boat"))

    async def test_mount_rejects_previous_dimension_before_first_position(self):
        b = self.binding
        await self.check_mount_invalidated(
            b.ChangeDimensionPacket(dimension_id=1),
            b.AddActorPacket(entity_id=-4001, runtime_id=50, actor_type="minecraft:boat"),
        )

    async def test_disconnect_ends_rides_and_sends_no_entity_actions_while_reader_is_live(self):
        b = self.binding
        ride = b.SetActorLinkPacket(link=self.link(-4001, -2001, "RIDING"))
        await self.join(b.AddActorPacket(entity_id=-4001, runtime_id=50, actor_type="minecraft:boat"), ride)
        before = self.bot.vehicle
        observed = []
        unsubscribe = self.bot.subscribe_vehicle_links(observed.append)

        await self.deliver(b.DisconnectPacket(), ride, b.SetTimePacket(time=1200))

        self.assertEqual(observed, [VehicleLinkChange(-2001, before, None, "unobserved")])
        unsubscribe()
        self.assertIsNone(self.bot.vehicle)
        self.assertEqual(self.bot.vehicle_links, ())
        for action, args in ((self.bot.dismount, ()), (self.bot.mount, (50,)), (self.bot.swing, ()),
                             (self.bot.interact_entity, (50,)), (self.bot.attack_entity, (50,))):
            with self.subTest(action=action.__name__), self.assertRaises(ConnectionError):
                await action(*args)
        for kind in (b.InteractPacket, b.AnimatePacket, b.InventoryTransactionPacket):
            self.assertEqual(self.sent(kind), [])
        await self.deliver(b.SetTimePacket(time=1300))
        self.transport.close.assert_not_awaited()

    async def observe_end_with_two_riders(self):
        b = self.binding
        await self.join(b.AddActorPacket(
            entity_id=-4001, runtime_id=50, actor_type="minecraft:boat",
            links=[self.link(-4001, -2001, "RIDING"), self.link(-4001, -2002, "PASSENGER")],
        ))
        entities = self.bot._entities
        observed = []

        def on_change(change):
            observed.append((change, entities.vehicle_links()))

        listener = weakref.ref(on_change)
        self.bot.subscribe_vehicle_links(on_change)
        expected = [(VehicleLinkChange(link.rider_entity_id, link, None, "unobserved"), ())
                    for link in self.bot.vehicle_links]
        self.assertEqual(len(expected), 2)
        return observed, expected, listener

    async def check_transport_loss(self, *, disconnect_first=False, pending_read=False):
        observed, expected, listener = await self.observe_end_with_two_riders()
        b = self.binding
        packets = self.bot.packets
        reader = packets._reader_task
        if disconnect_first:
            await self.deliver(b.DisconnectPacket(), b.SetTimePacket(time=1200))
            self.assertEqual(observed, expected)
            self.assertIsNone(listener())

        failure = ConnectionError("transport receive failed")
        self.incoming.put_nowait(failure)
        if pending_read:
            with self.assertRaises(ConnectionError) as error:
                await packets.recv_raw()
            self.assertIs(error.exception, failure)
            with self.assertRaisesRegex(ConnectionError, "entity session is closed"):
                await self.bot.dismount()
            self.assertEqual(self.bot.vehicle_links, ())

        with self.assertRaises(ConnectionError) as error:
            await reader
        self.assertIs(error.exception, failure)
        # In the reader-only case these events arrived before any state query.
        self.assertEqual(observed, expected)
        self.assertIsNone(listener())
        self.assertIsNone(self.bot.vehicle)
        self.assertEqual(self.bot.vehicle_links, ())
        for action, args in ((self.bot.mount, (50,)), (self.bot.dismount, ()), (self.bot.swing, ())):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(ConnectionError, "entity session is closed"):
                await action(*args)
        for kind in (b.InteractPacket, b.AnimatePacket, b.InventoryTransactionPacket):
            self.assertEqual(self.sent(kind), [])

        with self.assertRaises(ConnectionError) as error:
            await self.bot.close()
        self.assertIs(error.exception, failure)
        self.assertEqual(observed, expected)

    async def test_transport_loss_notifies_once_without_a_disconnect_packet(self):
        await self.check_transport_loss()

    async def test_disconnect_then_transport_loss_does_not_repeat_events(self):
        await self.check_transport_loss(disconnect_first=True)

    async def test_raw_receive_waiter_cannot_act_as_soon_as_reader_fails(self):
        await self.check_transport_loss(pending_read=True)

    async def test_local_close_notifies_once_and_releases_link_observer(self):
        observed, expected, listener = await self.observe_end_with_two_riders()

        await self.bot.close()
        await self.bot.close()

        self.assertEqual(observed, expected)
        self.assertIsNone(listener())
        self.transport.close.assert_awaited_once()

    async def test_link_subscribers_observe_filtered_transitions_even_when_final_state_is_empty(self):
        b = self.binding
        await self.join(
            b.AddActorPacket(entity_id=-4001, runtime_id=50, actor_type="minecraft:boat"),
            b.AddActorPacket(entity_id=-4002, runtime_id=51, actor_type="minecraft:minecart"),
        )
        local, first_vehicle, unrelated = [], [], []
        unsubscribe = self.bot.subscribe_vehicle_links(local.append, rider_entity_id=-2001)
        self.bot.subscribe_vehicle_links(first_vehicle.append, vehicle_entity_id=-4001)
        self.bot.subscribe_vehicle_links(unrelated.append, rider_entity_id=-2003, vehicle_entity_id=-4001)

        mounted = b.SetActorLinkPacket(link=self.link(-4001, -2001, "RIDING"))
        await self.deliver(
            mounted, mounted,
            b.SetActorLinkPacket(link=self.link(-4002, -2003, "PASSENGER")),
            b.SetActorLinkPacket(link=self.link(-4002, -2003, "NONE")),
            b.SetActorLinkPacket(link=self.link(-4002, -2001, "PASSENGER")),
            b.SetActorLinkPacket(link=self.link(-4002, -2001, "NONE")),
            b.SetTimePacket(time=1200),
        )
        boat = VehicleLink(-4001, -2001, "RIDING", 50, 9)
        cart = VehicleLink(-4002, -2001, "PASSENGER", 51, 9)
        transitions = [VehicleLinkChange(-2001, before, after, "link")
                       for before, after in ((None, boat), (boat, cart), (cart, None))]
        self.assertEqual(local, transitions)
        self.assertEqual(first_vehicle, transitions[:2])
        self.assertEqual(unrelated, [])
        self.assertIsNone(self.bot.vehicle)
        self.assertEqual(self.bot.vehicle_links, ())

        unsubscribe()
        await self.deliver(mounted)
        self.assertEqual(local, transitions)
        self.assertEqual(self.bot.vehicle, boat)

        await self.deliver(b.AddActorPacket(entity_id=-4003, runtime_id=50, actor_type="minecraft:boat"))
        self.assertEqual(first_vehicle[-1], VehicleLinkChange(-2001, boat, None, "unobserved"))
        self.assertIsNone(self.bot.vehicle)
