import asyncio
from importlib.util import find_spec
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot, LoginRefused
from endpilot.protocol import Protocol, read_batch, write_batch
from endpilot.qa import PacketTimeline
from test_scheduled_input import Wire


class SessionObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.protocol = Protocol(2225)
        self.b = self.protocol.binding
        self.bot = Bot("SessionObserver", protocol=self.protocol.number)
        self.addAsyncCleanup(self.bot.close)

    def wire(self, *login):
        tunnel = Wire(self.protocol)
        b = self.b
        tunnel.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        for packet in login:
            tunnel.push(packet)
        return tunnel

    async def connect(self, wire, *, timeline=None):
        with patch("endpilot.transport.nethernet.connect", return_value=wire):
            await self.bot.join_address("http://127.0.0.1:20512", timeline=timeline)

    async def loading(self, *, timeline=None):
        b = self.b
        tunnel = self.wire(b.StartGamePacket(runtime_id=42, entity_id=7))
        radius = asyncio.Event()
        send = tunnel.send

        async def observe(batch):
            await send(batch)
            if any(raw.packet_id == b.RequestChunkRadiusPacket.ID for raw in read_batch(batch)):
                radius.set()

        tunnel.send = observe
        task = asyncio.create_task(self.connect(tunnel, timeline=timeline))
        await asyncio.wait_for(radius.wait(), 1)
        return tunnel, task

    async def receive(self, tunnel, *packets):
        last = self.protocol.encode(packets[-1])
        pending = asyncio.create_task(self.bot.packets.wait_for_packet(
            last.packet_id, timeout=1, match=lambda raw: raw == last,
        ))
        await asyncio.sleep(0)
        for packet in packets:
            tunnel.push(packet)
        await pending

    def text(self, message):
        b = self.b
        return b.TextPacket(body=b.TextPacket.MessageOnly(type=b.TextPacketType.RAW, message=message))

    async def test_entities_resolve_drop_names_from_the_observed_item_registry(self):
        b = self.b
        wire = self.wire(
            b.StartGamePacket(runtime_id=42, entity_id=7),
            b.ItemRegistryPacket(items=[b.ItemData(id=306, name="minecraft:diamond")]),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        )
        await self.connect(wire)

        def item(entity_id, runtime_id, numeric_id, x):
            return b.AddItemActorPacket(
                id=entity_id, runtime_id=runtime_id, pos=b.Vec3(x=x, y=80.25, z=-1),
                item=b.SerializedNetworkItemStackDescriptor(id=numeric_id, stack_size=1),
            )

        await self.receive(
            wire, item(999, 23, 306, 5.5), item(1000, 24, 735, 6.5),
            b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
                action=b.PlayerListPacketType.ADD, uuid=UUID(int=3), id=103, name="Other",
            )]),
            b.AddPlayerPacket(uuid=UUID(int=3), name="Other", runtime_id=21,
                              pos=b.Vec3(x=3, y=81.5, z=4)),
            b.AddActorPacket(entity_id=998, runtime_id=22, actor_type="minecraft:boat",
                             pos=b.Vec3(x=-4, y=79.75, z=5)),
        )
        known, unknown, player, boat = self.bot.observed_entities
        self.assertEqual((known.runtime_id, known.entity_id, known.actor_type, known.position, known.item_id),
                         (23, 999, "minecraft:item", (5.5, 80.25, -1.0), "minecraft:diamond"))
        self.assertEqual((unknown.runtime_id, unknown.entity_id, unknown.actor_type, unknown.position, unknown.item_id),
                         (24, 1000, "minecraft:item", (6.5, 80.25, -1.0), "#735"))
        self.assertEqual((player.runtime_id, player.entity_id, player.actor_type, player.position, player.name),
                         (21, 103, "minecraft:player", (3.0, 81.5, 4.0), "Other"))
        self.assertEqual((boat.runtime_id, boat.entity_id, boat.actor_type, boat.position, boat.name),
                         (22, 998, "minecraft:boat", (-4.0, 79.75, 5.0), None))

        await self.receive(wire, b.TakeItemActorPacket(item_id=23, actor_id=21), b.RemoveActorPacket(entity_id=1000))
        self.assertEqual(self.bot.observed_entities, (player, boat))

    async def test_entity_positions_and_history_follow_the_current_attempt(self):
        b = self.b
        timeline = PacketTimeline(max_kept=2)
        self.assertIsNone(timeline.disconnect_reason())
        self.assertEqual(timeline.received_count("TextPacket"), 0)
        with self.assertRaisesRegex(RuntimeError, "not joined"):
            _ = self.bot.observed_entities

        wire, joining = await self.loading(timeline=timeline)
        self.assertFalse(joining.done())
        self.assertEqual(timeline.received_count("PlayStatusPacket"), 0)
        with self.assertRaisesRegex(RuntimeError, "not joined"):
            _ = self.bot.observed_entities
        wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        await joining
        self.assertEqual(timeline.received_count("PlayStatusPacket"), 1)
        self.assertEqual(self.bot.observed_entities, ())
        self.assertIsNone(self.bot.session_end)
        self.assertIsNone(timeline.disconnect_reason())

        await self.receive(
            wire,
            b.MovePlayerPacket(player_id=42, pos=b.Vec3(x=2, y=81.5, z=3)),
            b.AddPlayerPacket(uuid=UUID(int=3), name="Other", runtime_id=21, pos=b.Vec3(x=3, y=81.5, z=4)),
            b.AddActorPacket(entity_id=998, runtime_id=22, actor_type="minecraft:boat",
                             pos=b.Vec3(x=-4, y=79.75, z=5)),
            b.AddItemActorPacket(id=999, runtime_id=23, pos=b.Vec3(x=5, y=80.25, z=-1),
                                 item=b.SerializedNetworkItemStackDescriptor(id=1, stack_size=1)),
            b.MoveActorDeltaPacket(move_data=b.MoveActorDeltaData(runtime_id=22, new_position_x=-4.5)),
            self.text("one"), self.text("two"), self.text("three"),
        )
        observed = self.bot.observed_entities
        self.assertEqual({actor.runtime_id: actor.position for actor in observed}, {
            21: (3.0, 81.5, 4.0), 22: (-4.5, 79.75, 5.0), 23: (5.0, 80.25, -1.0),
        })
        self.assertEqual(timeline.received_count("TextPacket"), 3)
        self.assertEqual(len(timeline.received("TextPacket")), 2)
        self.assertEqual(timeline.received_count("NetworkSettingsPacket"), 1)
        self.assertEqual(timeline.received("NetworkSettingsPacket"), ())

        await self.bot.close()
        self.assertTrue(wire.closed)
        self.assertEqual(self.bot.session_end.kind, "local_close")
        self.assertIsNone(timeline.disconnect_reason())
        self.assertEqual(observed[-1].position, (5.0, 80.25, -1.0))
        self.assertEqual(timeline.received_count("TextPacket"), 3)
        with self.assertRaisesRegex(RuntimeError, "not joined"):
            _ = self.bot.observed_entities

        fresh_timeline = PacketTimeline()
        fresh, joining = await self.loading(timeline=fresh_timeline)
        self.assertIsNone(self.bot.session_end)
        self.assertEqual(fresh_timeline.received_count("TextPacket"), 0)
        fresh.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        await joining
        self.assertEqual(self.bot.observed_entities, ())
        self.assertEqual(timeline.received_count("TextPacket"), 3)

    async def test_disconnect_reason_persists_after_eviction_and_leave(self):
        b = self.b
        tunnel = self.wire(b.StartGamePacket(runtime_id=42, entity_id=7),
                           b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        timeline = PacketTimeline(max_kept=1)
        await self.connect(tunnel, timeline=timeline)
        self.bot.packets.subscribe_reader_end(timeline._record_transport_failure)
        await self.receive(tunnel, b.DisconnectPacket(
            reason=b.DisconnectFailReason.KICKED,
            messages=b.DisconnectPacketMessages(message="Session ended", filtered_message="Other form"),
        ), self.text("already queued"))
        end = await self.bot.wait_for_session_end(timeout=1)
        self.assertEqual((end.kind, end.reason_name, end.message), ("server_disconnect", "KICKED", "Session ended"))
        self.assertEqual(timeline.received("DisconnectPacket"), ())
        self.assertEqual(timeline.received_count("DisconnectPacket"), 1)
        self.assertEqual(timeline.disconnect_reason(), "KICKED: Session ended")
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _: ended.set())
        tunnel.incoming.put_nowait(ConnectionError("transport closed after disconnect"))
        await asyncio.wait_for(ended.wait(), 1)
        self.assertEqual(timeline.disconnect_reason(), "KICKED: Session ended")
        with self.assertRaisesRegex(ConnectionError, "transport closed after disconnect"):
            await self.bot.close()
        self.assertEqual(timeline.disconnect_reason(), "KICKED: Session ended")

    async def test_normal_login_denial_reason_and_new_attempt_reset(self):
        b = self.b
        for packet, reason in (
            (b.PlayStatusPacket(status=b.PlayStatus.LOGIN_FAILED_SERVER_FULL_SUB_CLIENT),
             "PlayStatus LOGIN_FAILED_SERVER_FULL_SUB_CLIENT"),
            (b.DisconnectPacket(reason=b.DisconnectFailReason.KICKED), "KICKED"),
        ):
            with self.subTest(reason=reason):
                timeline = PacketTimeline()
                tunnel = self.wire(packet)
                with self.assertRaises(LoginRefused) as refusal:
                    await self.connect(tunnel, timeline=timeline)
                self.assertEqual(refusal.exception.reason_name, reason.removeprefix("PlayStatus "))
                self.assertIsNone(self.bot.session_end)
                self.assertEqual(timeline.disconnect_reason(), reason)
                self.assertTrue(tunnel.closed)
                await self.bot.close()
                self.assertEqual(timeline.disconnect_reason(), reason)
        timeline = PacketTimeline()
        fresh, joining = await self.loading(timeline=timeline)
        self.assertIsNone(timeline.disconnect_reason())
        fresh.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        await joining

    async def test_loading_transport_errors_leave_reasons_and_local_cancel_does_not(self):
        b = self.b
        timeline = PacketTimeline()
        tunnel, joining = await self.loading(timeline=timeline)
        read_error = ConnectionError("loading receive closed")
        tunnel.incoming.put_nowait(read_error)
        with self.assertRaises(ConnectionError) as caught:
            await joining
        self.assertIs(caught.exception, read_error)
        self.assertTrue(tunnel.closed)
        self.assertIsNone(self.bot.session_end)
        self.assertEqual(timeline.disconnect_reason(), str(read_error))
        await self.bot.close()
        self.assertEqual(timeline.disconnect_reason(), str(read_error))

        tunnel = self.wire(b.StartGamePacket(runtime_id=42, entity_id=7),
                           b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        timeline = PacketTimeline()
        write_error = ConnectionError("loading initialization send closed")
        send = tunnel.send

        async def send_while_loading(batch):
            if any(raw.packet_id == b.SetLocalPlayerAsInitializedPacket.ID for raw in read_batch(batch)):
                self.assertIsNone(timeline.disconnect_reason())
                raise write_error
            await send(batch)

        tunnel.send = send_while_loading
        with self.assertRaises(ConnectionError) as caught:
            await self.connect(tunnel, timeline=timeline)
        self.assertIs(caught.exception, write_error)
        self.assertTrue(tunnel.closed)
        self.assertIsNone(self.bot.session_end)
        self.assertEqual(timeline.received_count("PlayStatusPacket"), 1)
        self.assertEqual(timeline.disconnect_reason(), str(write_error))
        await self.bot.close()
        self.assertEqual(timeline.disconnect_reason(), str(write_error))

        timeline = PacketTimeline()
        fresh, joining = await self.loading(timeline=timeline)
        self.assertIsNone(timeline.disconnect_reason())
        self.assertEqual(timeline.received_count("PlayStatusPacket"), 0)
        await self.bot.close()
        with self.assertRaises(asyncio.CancelledError):
            await joining
        self.assertTrue(fresh.closed)
        self.assertIsNone(timeline.disconnect_reason())

    @unittest.skipUnless(find_spec("nethernet"), "install the nethernet extra")
    async def test_native_nethernet_close_reasons_in_loading_send_and_receive(self):
        from nethernet import Connection, ConnectionClosed, SessionError, SessionState

        b = self.b
        for operation in ("recv", "send"):
            with self.subTest(operation=operation):
                radius_requested = asyncio.Event()
                session = SimpleNamespace(state=SessionState.CONNECTED)
                tunnel = Connection(session, None)
                tunnel._mark_connected()

                def send(batch, _reliability):
                    if any(raw.packet_id == b.RequestChunkRadiusPacket.ID for raw in read_batch(batch)):
                        radius_requested.set()

                async def close(error):
                    if session.state is not SessionState.CLOSED:
                        session.state = SessionState.CLOSED
                        tunnel._mark_closed(error)

                session.send = send
                session.aclose = close
                errors = []
                original_io = getattr(tunnel, operation)

                async def capture(*args, **kwargs):
                    try:
                        return await original_io(*args, **kwargs)
                    except ConnectionClosed as error:
                        errors.append(error)
                        raise

                setattr(tunnel, operation, capture)
                for packet in (b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
                               b.StartGamePacket(runtime_id=42, entity_id=7)):
                    tunnel._enqueue(write_batch([self.protocol.encode(packet)]))
                timeline = PacketTimeline()
                timeline = PacketTimeline()
                joining = asyncio.create_task(self.connect(tunnel, timeline=timeline))
                await asyncio.wait_for(radius_requested.wait(), 1)
                if operation == "send":
                    # An ordinary spawn packet may already be buffered when the transport closes.
                    spawn = b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN)
                    tunnel._enqueue(write_batch([self.protocol.encode(spawn)]))
                await tunnel.close(SessionError.DATA_CHANNEL_CLOSED)
                with self.assertRaises(ConnectionClosed) as failure:
                    await asyncio.wait_for(joining, 1)
                error, = errors
                self.assertIs(failure.exception, error)
                self.assertEqual(error.error, SessionError.DATA_CHANNEL_CLOSED)
                self.assertIsNone(self.bot.session_end)
                try:
                    self.assertEqual(timeline.disconnect_reason(), str(error))
                finally:
                    await self.bot.close()
                self.assertEqual(timeline.disconnect_reason(), str(error))
