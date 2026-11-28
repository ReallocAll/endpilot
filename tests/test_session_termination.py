import asyncio
import gc
import unittest
import zlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from bedrock_protocol import TextPacketType
from endpilot import BlockObservation, BoatPlacementResult, Bot, LoginRefused, PlacementResult, SessionEnd
from endpilot.blocks import HASHED_AIR_RUNTIME_ID
from endpilot.bot import observe_entity_placement
from endpilot.movement import Movement
from endpilot.protocol import RawPacket, write_batch
from test_join import _online_identity
from test_scheduled_input import Wire


SUPPORT, TARGET = (4, 90, 7), (4, 91, 7)
STONE = 0x80310E21


class _ZlibWire(Wire):
    async def send(self, batch):
        # Only the network settings request precedes compression negotiation.
        if self.sent:
            if batch[:1] != b"\xff":
                raise AssertionError("expected an uncompressed batch after network settings")
            batch = batch[1:]
        await super().send(batch)

    def push(self, packet):
        self.incoming.put_nowait(b"\xff" + write_batch([self.protocol.encode(packet)]))


class SessionTerminationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("SessionEndTest", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def wire(self, *after_spawn, player_id=41, negotiate_zlib=False, **start_game_options):
        b = self.b
        wire = _ZlibWire(self.bot.protocol) if negotiate_zlib else Wire(self.bot.protocol)
        algorithm = b.PacketCompressionAlgorithm.ZLIB if negotiate_zlib else b.PacketCompressionAlgorithm.NONE
        settings = b.NetworkSettingsPacket(compression_algorithm=algorithm, compression_threshold=0)
        wire.incoming.put_nowait(write_batch([wire.protocol.encode(settings)]))
        batch = write_batch(wire.protocol.encode(packet) for packet in (
            b.StartGamePacket(runtime_id=player_id, entity_id=player_id, **start_game_options),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
            *after_spawn,
        ))
        wire.incoming.put_nowait((b"\xff" if negotiate_zlib else b"") + batch)
        return wire

    def disconnect(self, message=None, *, filtered=""):
        b = self.b
        return b.DisconnectPacket(
            reason=b.DisconnectFailReason.KICKED,
            messages=None if message is None else b.DisconnectPacketMessages(message=message, filtered_message=filtered),
        )

    async def connect(self, wire, **options):
        with patch("endpilot.transport.nethernet.connect", return_value=wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1, **options)

    def command_text(self, kind, message):
        text = self.b.TextPacket
        return text(localize=False, body=text.MessageOnly(type=kind, message=message),
                    xuid="", platform_id="", filtered_message=None)

    def command_requests(self, wire):
        return [packet.command for packet in self.sent_command_packets(wire)]

    def sent_command_packets(self, wire):
        return [packet for packet in wire.sent if isinstance(packet, self.b.CommandRequestPacket)]

    def command_output(self, request_uuid, *, origin=None, request_id="", syntax_error=False):
        b = self.b
        return b.CommandOutputPacket(
            origin_data=b.CommandOriginData(type=b.CommandOriginType.PLAYER if origin is None else origin,
                                            uuid=request_uuid, request_id=request_id, player_id=0),
            output=b.CommandOutput(
                type=b.CommandOutputType.ALL_OUTPUT, success_count=0 if syntax_error else 1,
                messages=[b.CommandOutputMessage(
                    message_id="commands.generic.syntax" if syntax_error else "Minecraft Texture Pack (0.0.1)\n",
                    successful=not syntax_error, params=["packstack ", "bogus", " verbose"] if syntax_error else [],
                )],
            ),
        )

    def command_task(self, command, *, typed=False, timeout=10):
        execute = self.bot.execute_command_output if typed else self.bot.execute_command
        executing = asyncio.create_task(execute(command, timeout=timeout))

        async def drain():
            executing.cancel()
            await asyncio.gather(executing, return_exceptions=True)

        self.addAsyncCleanup(drain)
        return executing

    async def start_command(self, command, *, typed=False):
        sent = asyncio.Event()

        def observe_send(packet_id):
            if packet_id == self.b.CommandRequestPacket.ID:
                sent.set()

        unsubscribe = self.bot.packets.subscribe_sent(observe_send)
        try:
            executing = self.command_task(command, typed=typed)
            await asyncio.wait_for(sent.wait(), 1)
        finally:
            unsubscribe()
        self.assertFalse(executing.done())
        return executing

    async def start_blocked_command_output(self, wire, *, timeout=10):
        sending = asyncio.Event()

        async def hold_send():
            sending.set()
            await asyncio.Future()

        wire.before_send = hold_send
        executing = self.command_task("/packstack client", typed=True, timeout=timeout)
        await asyncio.wait_for(sending.wait(), 1)
        return executing

    async def test_command_feedback_ends_on_kick_with_live_reader_and_same_bot_can_rejoin(self):
        wire = self.wire()
        await self.connect(wire)
        packets = self.bot.packets
        baseline = set(packets._subscribers)
        executing = await self.start_command("/say first")
        queued = self.command_task("/say queued")
        await asyncio.sleep(0)
        self.assertFalse(queued.done())
        self.assertEqual(self.command_requests(wire), ["/say first"])
        callbacks = set(packets._subscribers) - baseline
        self.assertEqual(len(callbacks), 1)

        wire.push(self.disconnect("Kicked during feedback", filtered="Filtered reason"))
        ended = await self.bot.wait_for_session_end(timeout=1)
        self.assertEqual(ended, SessionEnd("server_disconnect", 55, "KICKED",
                                           "Kicked during feedback", "Filtered reason"))
        error = "^server disconnected while waiting for command feedback$"
        for task in (executing, queued, self.command_task("/say late")):
            with self.assertRaisesRegex(ConnectionError, error):
                await asyncio.wait_for(task, 1)
        self.assertEqual(self.command_requests(wire), ["/say first"])
        self.assertFalse(set(packets._subscribers) - baseline)
        self.assertFalse(packets._reader_task.done())
        self.assertFalse(wire.closed)
        self.assertIs(self.bot.session_end, ended)

        await self.bot.close()
        self.assertTrue(wire.closed)
        self.assertIs(self.bot.session_end, ended)
        replacement = self.wire(player_id=42)
        await self.connect(replacement)
        self.assertIsNone(self.bot.session_end)
        self.assertIsNot(self.bot.packets, packets)
        executing = await self.start_command("/say reopened")
        reply = '{"rawtext":[{"text":"reopened"}]}'
        replacement.push(self.command_text(TextPacketType.TEXT_OBJECT, reply))
        feedback = await asyncio.wait_for(executing, 1)
        self.assertEqual((feedback.body.type, feedback.body.message), (TextPacketType.TEXT_OBJECT, reply))
        self.assertEqual(self.command_requests(replacement), ["/say reopened"])

    async def test_first_command_feedback_or_kick_wins_in_same_batch(self):
        reply = '{"rawtext":[{"text":"command feedback"}]}'
        text = self.command_text(TextPacketType.TEXT_OBJECT, reply)
        system = self.command_text(TextPacketType.SYSTEM_MESSAGE, "unrelated system text")
        kick = self.disconnect("Kicked in the feedback batch")
        for feedback_first in (True, False):
            with self.subTest(feedback_first=feedback_first):
                wire = self.wire()
                await self.connect(wire)
                executing = await self.start_command("/say first")
                ordered = (text, kick) if feedback_first else (kick, text)
                wire.incoming.put_nowait(write_batch(wire.protocol.encode(packet) for packet in (system, *ordered)))
                ended = await self.bot.wait_for_session_end(timeout=1)
                self.assertEqual(ended.reason_name, "KICKED")
                if feedback_first:
                    feedback = await asyncio.wait_for(executing, 1)
                    self.assertEqual((feedback.body.type, feedback.body.message), (TextPacketType.TEXT_OBJECT, reply))
                else:
                    message = "^server disconnected while waiting for command feedback$"
                    with self.assertRaisesRegex(ConnectionError, message):
                        await asyncio.wait_for(executing, 1)
                await self.bot.close()

    async def test_kick_during_command_wait_prearming_prevents_send(self):
        wire = self.wire()
        await self.connect(wire)
        packets = self.bot.packets
        # Let the reader reach its live wire wait before scheduling the command and the kick.
        marker = self.b.SetTimePacket(time=42)
        reading = asyncio.create_task(packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        wire.push(marker)
        await reading

        baseline = set(packets._subscribers)
        executing = self.command_task("/say during kick")
        wire.push(self.disconnect("Kicked before submission"))
        with self.assertRaisesRegex(ConnectionError, "^server disconnected while waiting for command feedback$"):
            await asyncio.wait_for(executing, 1)
        self.assertEqual(self.command_requests(wire), [])
        self.assertFalse(set(packets._subscribers) - baseline)

    async def test_command_cancellation_during_wait_prearming_releases_subscription(self):
        wire = self.wire()
        await self.connect(wire)
        packets = self.bot.packets
        baseline = tuple(packets._subscribers)
        executing = self.command_task("/say cancelled")
        await asyncio.sleep(0)
        executing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await executing
        self.assertEqual(tuple(packets._subscribers), baseline)
        self.assertEqual(self.command_requests(wire), [])

    async def test_command_feedback_distinguishes_reader_failure_and_local_close(self):
        for eof in (None, EOFError("peer ended the receive stream")):
            with self.subTest(eof=eof):
                wire = self.wire()
                await self.connect(wire)
                executing = await self.start_command("/say waiting")
                if eof is None:
                    await self.bot.close()
                    message = "^packet stream closed while waiting$"
                    ended = SessionEnd("local_close")
                else:
                    wire.incoming.put_nowait(eof)
                    message = "^packet stream lost while waiting$"
                    ended = SessionEnd("reader_end", error_type="EOFError")
                with self.assertRaisesRegex(ConnectionError, message) as raised:
                    await asyncio.wait_for(executing, 1)
                self.assertIs(raised.exception.__cause__, eof)
                self.assertEqual(self.bot.session_end, ended)
                if eof is not None:
                    with self.assertRaises(EOFError) as closing:
                        await self.bot.close()
                    self.assertIs(closing.exception, eof)

    async def test_command_output_ignores_late_and_uncorrelated_packets_and_returns_syntax_error(self):
        wire = self.wire()
        await self.connect(wire)
        cancelled = await self.start_command("/packstack client", typed=True)
        previous, = self.sent_command_packets(wire)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled

        send = wire.send

        async def reply_during_send(batch):
            await send(batch)
            request = wire.sent[-1]
            if not isinstance(request, self.b.CommandRequestPacket):
                return
            request_uuid = request.origin.uuid
            for reply in (
                self.command_text(TextPacketType.TEXT_OBJECT, '{"rawtext":[{"text":"unrelated feedback"}]}'),
                self.command_output(previous.origin.uuid),
                self.command_output(self.bot.identity.uuid, request_id=request.origin.request_id),
                self.command_output(request_uuid, origin=self.b.CommandOriginType.DEDICATED_SERVER),
                self.command_output(request_uuid, syntax_error=True),
                self.b.SetTimePacket(time=42),
            ):
                wire.push(reply)
            # Ensure the reader observes the reply before the transport send returns.
            await self.bot.packets.wait_for_packet(self.b.SetTimePacket.ID, timeout=1)

        with patch.object(wire, "send", side_effect=reply_during_send):
            feedback = await self.bot.execute_command_output("packstack bogus verbose", timeout=1)

        first, request = self.sent_command_packets(wire)
        self.assertEqual((first.command, request.command), ("/packstack client", "/packstack bogus verbose"))
        self.assertEqual(len({self.bot.identity.uuid, first.origin.uuid, request.origin.uuid}), 3)
        for sent in (first, request):
            self.assertEqual((sent.origin.type, sent.origin.player_id), (self.b.CommandOriginType.PLAYER, 41))
            self.assertEqual(sent.version, self.b.CurrentCmdVersion.LATEST)
            self.assertFalse(sent.internal_source)
            self.assertTrue(sent.origin.request_id)
        self.assertNotEqual(first.origin.request_id, request.origin.request_id)
        self.assertIsInstance(feedback, self.b.CommandOutputPacket)
        self.assertEqual(self.bot.protocol.encode(feedback),
                         self.bot.protocol.encode(self.command_output(request.origin.uuid, syntax_error=True)))
        self.assertIsNone(self.bot.session_end)
        self.assertFalse(wire.closed)

    async def test_command_output_cancellation_during_malformed_reply_reports_no_loop_error(self):
        wire = self.wire()
        await self.connect(wire)
        executing = await self.start_command("/packstack client", typed=True)
        packets = self.bot.packets
        malformed = RawPacket(self.b.CommandOutputPacket.ID, b"")
        loop = asyncio.get_running_loop()
        reports = []
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: reports.append(context["message"]))

        def cancel_after_decode(raw):
            if raw.packet_id == malformed.packet_id:
                executing.cancel()

        # Subscribe after the command waiter so its matcher sees the malformed packet first.
        self.addCleanup(packets.subscribe_raw(cancel_after_decode))
        wire.incoming.put_nowait(write_batch([malformed]))
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(executing, 1)

        marker = self.b.SetTimePacket(time=42)
        wire.push(marker)
        self.assertEqual(await packets.wait_for_packet(marker.ID, timeout=1), wire.protocol.encode(marker))
        # The decode traceback can retain the failed future until cyclic collection.
        gc.collect()
        self.assertEqual(reports, [])

    async def test_first_matching_command_output_or_kick_wins_in_same_batch(self):
        for output_first in (True, False):
            with self.subTest(output_first=output_first):
                wire = self.wire()
                await self.connect(wire)
                executing = await self.start_command("/packstack client", typed=True)
                request, = self.sent_command_packets(wire)
                output = self.command_output(request.origin.uuid)
                kick = self.disconnect("Kicked in the output batch")
                ordered = (output, kick) if output_first else (kick, output)
                wire.incoming.put_nowait(write_batch(wire.protocol.encode(packet) for packet in ordered))

                ended = await self.bot.wait_for_session_end(timeout=1)
                self.assertEqual(ended.reason_name, "KICKED")
                if output_first:
                    feedback = await asyncio.wait_for(executing, 1)
                    self.assertIsInstance(feedback, self.b.CommandOutputPacket)
                    self.assertEqual(feedback.output.success_count, 1)
                else:
                    with self.assertRaises(ConnectionError):
                        await asyncio.wait_for(executing, 1)
                self.assertFalse(self.bot.packets._reader_task.done())
                self.assertFalse(wire.closed)
                self.assertEqual(self.command_requests(wire), ["/packstack client"])
                await self.bot.close()

    async def test_command_output_ends_on_peer_eof_during_transport_send(self):
        wire = self.wire()
        await self.connect(wire)
        executing = await self.start_blocked_command_output(wire)
        eof = EOFError("peer ended the receive stream")
        wire.incoming.put_nowait(eof)

        with self.assertRaises(ConnectionError) as raised:
            await asyncio.wait_for(executing, 1)
        self.assertIs(raised.exception.__cause__, eof)
        self.assertEqual(await self.bot.wait_for_session_end(timeout=1),
                         SessionEnd("reader_end", error_type="EOFError"))
        self.assertEqual(self.command_requests(wire), [])
        self.assertFalse(wire.closed)
        with self.assertRaises(EOFError) as closing:
            await self.bot.close()
        self.assertIs(closing.exception, eof)

    async def test_command_output_nonpositive_timeout_does_not_send(self):
        wire = self.wire()
        await self.connect(wire)

        for timeout in (0, -1):
            with self.subTest(timeout=timeout), self.assertRaises(TimeoutError):
                await self.bot.execute_command_output("/packstack client", timeout=timeout)
        self.assertEqual(self.command_requests(wire), [])

    async def test_command_output_timeout_includes_waiting_for_text_command(self):
        wire = self.wire()
        await self.connect(wire)
        executing = await self.start_command("/say first")
        queued = self.command_task("/packstack client", typed=True, timeout=0.05)

        _, pending = await asyncio.wait((queued,), timeout=1)
        self.assertFalse(pending)
        with self.assertRaises(TimeoutError):
            await queued
        self.assertFalse(executing.done())
        wire.push(self.command_text(TextPacketType.TEXT_OBJECT, '{"rawtext":[{"text":"first"}]}'))
        await asyncio.wait_for(executing, 1)
        self.assertEqual(self.command_requests(wire), ["/say first"])

    async def test_command_output_timeout_includes_transport_send(self):
        wire = self.wire()
        await self.connect(wire)
        executing = await self.start_blocked_command_output(wire, timeout=0.05)
        _, pending = await asyncio.wait((executing,), timeout=1)
        self.assertFalse(pending)
        with self.assertRaises(TimeoutError):
            await executing
        self.assertEqual(self.command_requests(wire), [])
        self.assertFalse(self.bot.packets._reader_task.done())
        self.assertFalse(wire.closed)

    def placement_block(self, pos, runtime_id):
        b = self.b
        x, y, z = pos
        return b.UpdateBlockPacket(pos=b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=0)

    def placement_slot(self, count, *, net_id=21):
        b = self.b
        return b.InventorySlotPacket(
            inventory_id=b.ContainerID.INVENTORY, slot=0,
            item=b.SerializedNetworkItemStackDescriptor(
                id=1, stack_size=count, net_id_variant=net_id, block_runtime_id=STONE,
            ),
        )

    async def join_for_placement(self, *, player_id=41, count=4, negotiate_zlib=False):
        b = self.b
        wire = self.wire(
            self.placement_block((3, 90, 7), STONE),
            self.placement_block(SUPPORT, STONE),
            self.placement_block(TARGET, HASHED_AIR_RUNTIME_ID),
            self.placement_slot(count),
            b.MovePlayerPacket(player_id=player_id, pos=b.Vec3(x=3.5, y=92.62, z=7.5),
                               reset_position=2, on_ground=True),
            player_id=player_id, negotiate_zlib=negotiate_zlib, entity_game_type=b.GameType.SURVIVAL,
            block_network_ids_are_hashes=True,
        )
        await self.connect(wire)
        await self.bot.wait_for_position_sync(timeout=1)
        return wire

    def placement_transactions(self, wire):
        return [
            packet.variant_transaction for packet in wire.sent
            if isinstance(packet, self.b.InventoryTransactionPacket)
        ]

    async def start_placement(self, wire, *, timeout, count=4):
        sent = asyncio.Event()

        def observe_send(packet_id):
            if packet_id == self.b.InventoryTransactionPacket.ID:
                sent.set()

        unsubscribe = self.bot.packets.subscribe_sent(observe_send)
        placing = asyncio.create_task(self.bot.place_block(SUPPORT, face=1, expected_runtime_id=STONE, timeout=timeout))

        async def drain():
            placing.cancel()
            await asyncio.gather(placing, return_exceptions=True)

        self.addAsyncCleanup(drain)
        try:
            await asyncio.wait_for(sent.wait(), 1)
        finally:
            unsubscribe()
        transaction, = self.placement_transactions(wire)
        self.assertEqual(transaction.action_type, self.b.ItemUseInventoryTransaction.ActionType.PLACE)
        self.assertEqual((transaction.pos.x, transaction.pos.y, transaction.pos.z), SUPPORT)
        self.assertEqual((transaction.face, transaction.target_block_id, transaction.slot), (1, STONE, 0))
        self.assertEqual((transaction.item.id, transaction.item.stack_size), (1, count))
        self.assertFalse(placing.done())
        return placing

    async def test_placement_completion_is_shared_once_and_isolates_callback_errors(self):
        wire = await self.join_for_placement()
        delivered, removed, reports = [], [], []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: reports.append(context))

        def failing(result):
            unsubscribe_removed()
            raise ValueError("observer failed")

        def cancelled(result):
            raise asyncio.CancelledError("observer cancelled")

        self.bot.subscribe_placement_completions(failing)
        unsubscribe_removed = self.bot.subscribe_placement_completions(removed.append)
        self.bot.subscribe_placement_completions(cancelled)
        self.bot.subscribe_placement_completions(delivered.append)
        unsubscribe = self.bot.subscribe_placement_completions(removed.append)
        unsubscribe()
        unsubscribe()
        self.assertEqual(self.placement_transactions(wire), [])

        placing = await self.start_placement(wire, timeout=1)
        target_update = asyncio.create_task(self.bot.wait_for_block_update(TARGET, timeout=1))
        await asyncio.sleep(0)
        wire.push(self.placement_block(TARGET, STONE))
        self.assertEqual((await target_update).runtime_id, STONE)
        self.assertEqual(delivered, [])
        self.assertFalse(placing.done())
        wire.push(self.placement_slot(3))
        result = await asyncio.wait_for(placing, 1)
        self.assertEqual(result.reason, "observations_complete")
        self.assertEqual(len(delivered), 1)
        self.assertIs(delivered[0], result)
        self.assertEqual(removed, [])
        self.assertEqual([type(report["exception"]) for report in reports], [ValueError, asyncio.CancelledError])
        self.assertEqual([report["callback"] for report in reports], [failing, cancelled])

    async def join_for_entity_placement(self, player_id=41):
        b = self.b
        wire = self.wire(
            b.ItemRegistryPacket(items=[b.ItemData(id=496, name="minecraft:oak_boat")]),
            self.placement_block(SUPPORT, 0x483B8B2A),
            self.boat_slot(2),
            b.MovePlayerPacket(player_id=player_id, pos=b.Vec3(x=3.5, y=92.62, z=7.5),
                               reset_position=2, on_ground=True),
            player_id=player_id, entity_game_type=b.GameType.SURVIVAL, block_network_ids_are_hashes=True,
        )
        await self.connect(wire)
        await self.bot.wait_for_position_sync(timeout=1)
        return wire

    def boat_slot(self, count):
        b = self.b
        return b.InventorySlotPacket(
            inventory_id=b.ContainerID.INVENTORY, slot=0,
            item=b.SerializedNetworkItemStackDescriptor(id=496, stack_size=count, net_id_variant=21),
        )

    def boat_spawn(self, entity_id):
        b = self.b
        x, y, z = SUPPORT
        return b.AddActorPacket(
            entity_id=entity_id, runtime_id=-entity_id, actor_type="minecraft:boat",
            pos=b.Vec3(x=x + .5, y=y + .875, z=z + .5),
        )

    async def start_boat_placement(self):
        sent = asyncio.Event()
        unsubscribe = self.bot.packets.subscribe_sent(
            lambda packet_id: sent.set() if packet_id == self.b.InventoryTransactionPacket.ID else None,
        )
        placing = asyncio.create_task(self.bot.place_oak_boat(SUPPORT, timeout=1))

        async def drain():
            placing.cancel()
            await asyncio.gather(placing, return_exceptions=True)

        self.addAsyncCleanup(drain)
        try:
            await asyncio.wait_for(sent.wait(), 1)
        finally:
            unsubscribe()
        return placing

    async def test_entity_placement_completion_keeps_callback_order_and_isolates_errors(self):
        wire = await self.join_for_entity_placement()
        delivered, late, removed, reports, blocks = [], [], [], [], []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: reports.append(context))

        def failing(result):
            delivered.append(("first", result))
            remove()
            self.bot.subscribe_entity_placement_completions(late.append)
            raise ValueError("observer failed")

        def cancelled(result):
            delivered.append(("cancelled", result))
            raise asyncio.CancelledError("observer cancelled")

        self.bot.subscribe_entity_placement_completions(failing)
        remove = self.bot.subscribe_entity_placement_completions(removed.append)
        self.bot.subscribe_entity_placement_completions(cancelled)
        self.bot.subscribe_entity_placement_completions(lambda result: delivered.append(("last", result)))
        self.bot.subscribe_placement_completions(blocks.append)
        placing = await self.start_boat_placement()
        wire.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, (
            self.boat_slot(1), self.boat_spawn(-50), self.boat_slot(1), self.boat_spawn(-50),
        ))))
        result = await placing
        self.assertIsInstance(result, BoatPlacementResult)
        self.assertEqual(result.reason, "observations_complete")
        self.assertEqual([label for label, _ in delivered], ["first", "cancelled", "last"])
        self.assertTrue(all(value is result for _, value in delivered))
        self.assertEqual((removed, late, blocks), ([], [], []))
        self.assertEqual([type(report["exception"]) for report in reports], [ValueError, asyncio.CancelledError])
        self.assertEqual([report["callback"] for report in reports], [failing, cancelled])

    async def test_entity_placement_completion_suppressed_if_reader_ends_before_return(self):
        wire = await self.join_for_entity_placement()
        delivered = []
        self.bot.subscribe_entity_placement_completions(delivered.append)
        placing = await self.start_boat_placement()
        wire.incoming.put_nowait(write_batch(map(self.bot.protocol.encode, (
            self.boat_spawn(-50), self.boat_slot(1), self.disconnect(),
        ))))
        result = await placing
        self.assertEqual(result.reason, "observations_complete")
        self.assertEqual(delivered, [])
        self.assertEqual((await self.bot.wait_for_session_end(timeout=1)).kind, "server_disconnect")
        with self.assertRaisesRegex(ConnectionError, "placement completion subscription stream is closed"):
            self.bot.subscribe_entity_placement_completions(delivered.append)

    async def test_old_entity_placement_result_cannot_notify_a_rejoined_session(self):
        previous, current = [], []
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.subscribe_entity_placement_completions(previous.append)
        wire = await self.join_for_entity_placement()
        remove = self.bot.subscribe_entity_placement_completions(previous.append)
        observed, resume = asyncio.Event(), asyncio.Event()

        async def hold_after_observation(*args, **kwargs):
            result = await observe_entity_placement(*args, **kwargs)
            observed.set()
            await resume.wait()
            return result

        with patch("endpilot.bot.observe_entity_placement", side_effect=hold_after_observation):
            previous_action = await self.start_boat_placement()
            wire.push(self.boat_spawn(-50))
            wire.push(self.boat_slot(1))
            await asyncio.wait_for(observed.wait(), 1)
        await self.bot.close()
        wire = await self.join_for_entity_placement(player_id=42)
        self.bot.subscribe_entity_placement_completions(current.append)
        remove()
        remove()
        resume.set()
        self.assertEqual((await previous_action).reason, "observations_complete")
        self.assertEqual((previous, current), ([], []))

        placing = await self.start_boat_placement()
        wire.push(self.boat_slot(1))
        wire.push(self.boat_spawn(-51))
        result = await placing
        self.assertEqual(result.reason, "observations_complete")
        self.assertEqual(previous, [])
        self.assertEqual(len(current), 1)
        self.assertIs(current[0], result)

    async def test_placement_completion_ignores_sparse_world_updates_and_partial_timeout(self):
        wire = await self.join_for_placement()
        delivered = []
        self.bot.subscribe_placement_completions(delivered.append)

        async def update_target(runtime_id):
            observed = asyncio.create_task(self.bot.wait_for_block_update(TARGET, timeout=1))
            await asyncio.sleep(0)
            wire.push(self.placement_block(TARGET, runtime_id))
            self.assertEqual((await observed).runtime_id, runtime_id)

        await update_target(STONE)
        self.assertEqual(delivered, [])
        rejected = await self.bot.place_block(SUPPORT, face=1, expected_runtime_id=STONE)
        self.assertEqual(rejected.reason, "precondition_failed")
        self.assertEqual(self.placement_transactions(wire), [])
        await update_target(HASHED_AIR_RUNTIME_ID)
        placing = await self.start_placement(wire, timeout=0.3)
        await update_target(STONE)
        result = await asyncio.wait_for(placing, 1)
        self.assertEqual((result.submitted, result.reason), (True, "timed_out"))
        self.assertEqual(result.world_change.after.runtime_id, STONE)
        self.assertIsNone(result.inventory_change)
        self.assertEqual(delivered, [])

    async def test_placement_completion_ignores_cancelled_action_and_ends_on_local_close(self):
        wire = await self.join_for_placement()
        delivered = []
        unsubscribe = self.bot.subscribe_placement_completions(delivered.append)
        placing = await self.start_placement(wire, timeout=1)
        placing.cancel("caller stopped")
        with self.assertRaisesRegex(asyncio.CancelledError, "caller stopped") as raised:
            await placing
        self.assertEqual(raised.exception.placement_progress.reason, "cancelled")
        await self.bot.close()
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.subscribe_placement_completions(delivered.append)

        replacement = await self.join_for_placement(player_id=42)
        current = []
        self.bot.subscribe_placement_completions(current.append)
        placing = await self.start_placement(replacement, timeout=1)
        replacement.push(self.placement_slot(3))
        replacement.push(self.placement_block(TARGET, STONE))
        result = await asyncio.wait_for(placing, 1)
        self.assertEqual(result.reason, "observations_complete")
        self.assertEqual(current, [result])
        self.assertEqual(delivered, [])
        unsubscribe()
        unsubscribe()

    async def test_placement_completion_after_reader_stops_returns_without_notifying(self):
        wire = await self.join_for_placement()
        packets = self.bot.packets
        reader = packets.start_receiving()
        delivered, session_ends, reader_ends = [], [], []
        self.bot.subscribe_placement_completions(delivered.append)
        self.bot.subscribe_session_end(session_ends.append)
        packets.subscribe_reader_end(reader_ends.append)
        placing = await self.start_placement(wire, timeout=1)

        wire.push(self.placement_block(TARGET, STONE))
        wire.push(self.placement_slot(3, net_id=22))
        # The next receive unwinds the reader before the queued placement task resumes.
        wire.incoming.put_nowait(asyncio.CancelledError())

        result = await asyncio.wait_for(placing, 1)
        self.assertEqual((result.submitted, result.reason), (True, "observations_complete"))
        self.assertEqual(result.world_change.after.runtime_id, STONE)
        self.assertEqual((result.inventory_change.before.count, result.inventory_change.after.count), (4, 3))
        self.assertEqual(delivered, [])
        await self.bot.close()
        self.assertTrue(reader.cancelled())
        self.assertEqual(reader_ends, [None])
        self.assertEqual(session_ends, [SessionEnd("reader_end")])

    async def test_placement_ends_on_peer_eof_and_same_bot_can_rejoin_after_close(self):
        wire = await self.join_for_placement()
        delivered = []
        self.bot.subscribe_placement_completions(delivered.append)
        packets = self.bot.packets
        reader = packets._reader_task
        ticker = self.bot._movement._ticker
        clock = self.bot._clock._task
        baseline = set(packets._subscribers)
        placing = await self.start_placement(wire, timeout=10)
        placement_callbacks = set(packets._subscribers) - baseline
        self.assertEqual(len(placement_callbacks), 1)
        self.assertFalse(reader.done())
        self.assertFalse(ticker.done())
        self.assertFalse(clock.done())

        eof = EOFError("peer ended the receive stream")
        wire.incoming.put_nowait(eof)
        self.assertEqual(await asyncio.wait_for(placing, 2),
                         PlacementResult(0, TARGET, 0, True, "disconnected", None, None))
        ended = await self.bot.wait_for_session_end(timeout=1)
        self.assertEqual(ended, SessionEnd("reader_end", error_type="EOFError"))
        with self.assertRaisesRegex(ConnectionError, "placement completion subscription stream is closed"):
            self.bot.subscribe_placement_completions(delivered.append)
        _, pending = await asyncio.wait((reader, ticker, clock), timeout=1)
        self.assertFalse(pending)
        self.assertIs(reader.exception(), eof)
        self.assertFalse(placement_callbacks.intersection(packets._subscribers))
        self.assertFalse(wire.closed)

        with self.assertRaises(EOFError) as raised:
            await self.bot.close()
        self.assertIs(raised.exception, eof)
        self.assertTrue(wire.closed)
        self.assertIs(self.bot.session_end, ended)

        replacement = await self.join_for_placement(player_id=42, count=3)
        self.assertIsNone(self.bot.session_end)
        self.assertEqual(self.bot.player_id, 42)
        self.assertIsNot(self.bot.packets, packets)
        self.assertIsNot(self.bot.packets._reader_task, reader)
        self.assertFalse(self.bot.packets._reader_task.done())
        tick = self.bot.get_tick()
        self.assertGreater(await asyncio.wait_for(self.bot.wait_ticks(1), 1), tick)

        placing = await self.start_placement(replacement, timeout=2, count=3)
        replacement.push(self.placement_block(TARGET, STONE))
        replacement.push(self.placement_slot(2, net_id=22))
        result = await asyncio.wait_for(placing, 1)
        self.assertEqual((result.submitted, result.reason, result.target, result.slot),
                         (True, "observations_complete", TARGET, 0))
        self.assertTrue(result.world_change.before.is_air)
        self.assertEqual(result.world_change.after.runtime_id, STONE)
        self.assertEqual((result.inventory_change.before.count, result.inventory_change.after.count), (3, 2))
        self.assertEqual(result.inventory_change.after.net_id, 22)
        self.assertEqual(delivered, [])
        self.assertEqual(len(self.placement_transactions(wire)), 1)
        self.assertEqual(len(self.placement_transactions(replacement)), 1)

    async def test_receive_budget_ends_placement_and_same_bot_can_replay_after_close(self):
        fixture = Path(__file__).with_name("fixtures") / "stock-subchunk-2225.hex"
        raw = RawPacket(self.b.SubChunkPacket.ID, bytes.fromhex(fixture.read_text(encoding="ascii")))
        batch = write_batch([raw])
        frame = b"\x00" + zlib.compress(batch, wbits=-15)
        chest = (5, 220, 10)

        wire = await self.join_for_placement(negotiate_zlib=True)
        packets = self.bot.packets
        blocks = self.bot._blocks
        reader, ticker, clock = packets._reader_task, self.bot._movement._ticker, self.bot._clock._task
        baseline = set(packets._subscribers)
        placing = await self.start_placement(wire, timeout=10)
        placement_callbacks = set(packets._subscribers) - baseline
        self.assertEqual(len(placement_callbacks), 1)

        with patch("endpilot.compression.MAX_DECOMPRESSED_BATCH_SIZE", len(batch) - 1):
            wire.incoming.put_nowait(frame)
            ended = await self.bot.wait_for_session_end(timeout=1)

        self.assertEqual(ended, SessionEnd("reader_end", error_type="ValueError"))
        self.assertEqual(await asyncio.wait_for(placing, 2),
                         PlacementResult(0, TARGET, 0, True, "disconnected", None, None))
        _, pending = await asyncio.wait((reader, ticker, clock), timeout=1)
        self.assertFalse(pending)
        failure = reader.exception()
        self.assertIsInstance(failure, ValueError)
        self.assertEqual(str(failure), "invalid compressed batch")
        with self.assertRaisesRegex(ConnectionError, "packet stream lost while waiting") as waiting:
            await self.bot.wait_for_server_disconnect(timeout=1)
        self.assertIs(waiting.exception.__cause__, failure)
        self.assertFalse(placement_callbacks.intersection(packets._subscribers))
        self.assertFalse(packets._reader_end_subscribers)
        self.assertFalse(blocks._block_observers)
        self.assertFalse(wire.closed)

        with self.assertRaises(ValueError) as closing:
            await self.bot.close()
        self.assertIs(closing.exception, failure)
        self.assertTrue(wire.closed)
        self.assertIs(self.bot.session_end, ended)

        replacement = await self.join_for_placement(player_id=42, count=3, negotiate_zlib=True)
        self.assertIsNone(self.bot.session_end)
        self.assertEqual(self.bot.player_id, 42)
        self.assertIsNot(self.bot.packets, packets)
        self.assertIsNone(self.bot.observed_block(chest))
        replacement.incoming.put_nowait(frame)
        self.assertEqual(await self.bot.packets.wait_for_packet(raw.packet_id, timeout=1), raw)
        self.assertEqual(self.bot.observed_block(chest), BlockObservation(chest, 741882976, 0, False))

        placing = await self.start_placement(replacement, timeout=2, count=3)
        replacement.push(self.placement_block(TARGET, STONE))
        replacement.push(self.placement_slot(2, net_id=22))
        result = await asyncio.wait_for(placing, 1)
        self.assertEqual((result.submitted, result.reason, result.target, result.slot),
                         (True, "observations_complete", TARGET, 0))
        self.assertEqual(result.world_change.after.runtime_id, STONE)
        self.assertEqual((result.inventory_change.before.count, result.inventory_change.after.count), (3, 2))

    async def test_placement_timeout_releases_its_subscriptions_on_a_live_connection(self):
        wire = await self.join_for_placement()
        packets = self.bot.packets
        blocks = self.bot._blocks
        marker = self.b.SetTimePacket(time=42)
        waiting = asyncio.create_task(packets.wait_for_packet(marker.ID, timeout=5))
        try:
            await asyncio.sleep(0)
            raw_subscribers = tuple(packets._subscribers)
            end_subscribers = tuple(packets._reader_end_subscribers)
            block_observers = tuple(blocks._block_observers)
            placing = await self.start_placement(wire, timeout=1)
            self.assertEqual(await asyncio.wait_for(placing, 2),
                             PlacementResult(0, TARGET, 0, True, "timed_out", None, None))
            self.assertEqual(len(self.placement_transactions(wire)), 1)
            self.assertEqual(tuple(packets._subscribers), raw_subscribers)
            self.assertEqual(tuple(packets._reader_end_subscribers), end_subscribers)
            self.assertEqual(tuple(blocks._block_observers), block_observers)
            self.assertFalse(waiting.done())
            self.assertFalse(packets._reader_task.done())
            self.assertFalse(wire.closed)
            self.assertIsNone(self.bot.session_end)

            wire.push(marker)
            self.assertEqual(packets.protocol.decode(await asyncio.wait_for(waiting, 1)).time, marker.time)
            tick = self.bot.get_tick()
            self.assertGreater(await asyncio.wait_for(self.bot.wait_ticks(1), 1), tick)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def test_disconnect_after_spawn_in_the_same_batch_survives_later_reader_failure(self):
        observed = []
        self.bot.subscribe_spawns(observed.append)
        self.bot.subscribe_session_end(lambda result: observed.append((result, self.bot.session_end)))
        wire = self.wire(self.disconnect("First reason", filtered=""), self.disconnect("Later reason"))
        await self.connect(wire)
        result = await self.bot.wait_for_session_end(timeout=1)
        self.assertEqual(result, SessionEnd("server_disconnect", 55, "KICKED", "First reason", ""))
        self.assertEqual(observed, ["initial", (result, result)])
        self.assertIs(observed[1][0], result)
        self.assertIs(observed[1][1], result)
        self.assertIs(self.bot.session_end, result)
        self.assertFalse(wire.closed)
        with self.assertRaises(FrozenInstanceError):
            result.message = "Changed"

        # The older API still waits for a new packet even after the snapshot is available.
        waiting = asyncio.create_task(self.bot.wait_for_server_disconnect(timeout=1))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        wire.push(self.disconnect("Next packet", filtered="Filtered"))
        packet = await waiting
        self.assertEqual((packet.messages.message, packet.messages.filtered_message), ("Next packet", "Filtered"))

        reader = self.bot.packets._reader_task
        failure = OSError("reader closed after the kick")
        wire.incoming.put_nowait(failure)
        with self.assertRaises(OSError) as raised:
            await reader
        self.assertIs(raised.exception, failure)
        self.assertIs(self.bot.session_end, result)
        with self.assertRaises(OSError) as raised:
            await self.bot.close(reason="Do not replace the first kick")
        self.assertIs(raised.exception, failure)
        self.assertTrue(wire.closed)
        self.assertIs(await self.bot.wait_for_session_end(timeout=0), result)
        self.assertEqual(len(observed), 2)

    async def test_independent_waiter_timeout_and_cancellation_keep_another_waiter_live(self):
        wire = self.wire()
        await self.connect(wire)
        remaining = asyncio.create_task(self.bot.wait_for_session_end(timeout=1))
        cancelled = asyncio.create_task(self.bot.wait_for_session_end())
        try:
            with self.assertRaises(TimeoutError):
                await self.bot.wait_for_session_end(timeout=0)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            self.assertFalse(remaining.done())
            self.assertIsNone(self.bot.session_end)
            wire.push(self.disconnect())
            result = await remaining
            self.assertEqual(result, SessionEnd("server_disconnect", 55, "KICKED"))
            self.assertIs(await self.bot.wait_for_session_end(timeout=0), result)
        finally:
            for task in (remaining, cancelled):
                task.cancel()
            await asyncio.gather(remaining, cancelled, return_exceptions=True)

    async def test_unknown_disconnect_enum_keeps_the_received_number_and_messages(self):
        b = self.b
        wire = self.wire(b.DisconnectPacket(
            reason=999,
            messages=b.DisconnectPacketMessages(message="Unknown reason", filtered_message="Filtered reason"),
        ))
        await self.connect(wire)
        result = await self.bot.wait_for_session_end(timeout=1)
        self.assertEqual(result, SessionEnd("server_disconnect", 999, None, "Unknown reason", "Filtered reason"))

    async def test_reader_failure_reports_only_the_error_type_and_preserves_close_error(self):
        wire = self.wire()
        await self.connect(wire)
        observed = []
        self.bot.subscribe_session_end(observed.append)
        failure = EOFError("details from the transport")
        wire.incoming.put_nowait(failure)
        result = await self.bot.wait_for_session_end(timeout=1)
        self.assertEqual(result, SessionEnd("reader_end", error_type="EOFError"))
        self.assertNotIn(str(failure), repr(result))
        self.assertFalse(wire.closed)
        with self.assertRaises(EOFError) as raised:
            await self.bot.close(reason="Do not replace reader EOF")
        self.assertIs(raised.exception, failure)
        self.assertTrue(wire.closed)
        self.assertIs(self.bot.session_end, result)
        self.assertEqual(len(observed), 1)
        self.assertIs(observed[0], result)

    async def test_zero_timeout_wait_and_close_see_reader_completion_before_its_callback(self):
        for wait_first in (False, True):
            with self.subTest(wait_first=wait_first):
                wire = self.wire()
                original_recv = wire.recv
                reader_cancellation = asyncio.CancelledError()
                notified = []
                close_started = asyncio.Event()
                close_task = None

                async def early_close():
                    self.assertEqual(notified, [])
                    close_started.set()
                    if wait_first:
                        result = await self.bot.wait_for_session_end(timeout=0)
                        self.assertEqual(result, SessionEnd("reader_end"))
                    await self.bot.close(reason="Reader already completed")

                async def recv():
                    nonlocal close_task
                    try:
                        return await original_recv()
                    except asyncio.CancelledError as error:
                        if error is reader_cancellation:
                            # This task is queued ahead of the reader's done callback.
                            close_task = asyncio.create_task(early_close())
                        raise

                with patch.object(wire, "recv", new=recv):
                    await self.connect(wire)
                    self.bot.packets.subscribe_reader_end(notified.append)
                    wire.incoming.put_nowait(reader_cancellation)
                    await asyncio.wait_for(close_started.wait(), 1)
                    await close_task
                self.assertEqual(notified, [None])
                self.assertTrue(wire.closed)
                self.assertEqual(self.bot.session_end, SessionEnd("reader_end"))

    async def test_session_end_observers_can_unsubscribe_and_schedule_close(self):
        events, late, duplicates = [], [], []
        scheduled_close = None
        duplicate = duplicates.append

        def first(result):
            nonlocal scheduled_close
            events.append((result, self.bot.session_end, self.bot._session._end_subscriptions))
            remove_first()
            remove_first()
            self.bot.subscribe_session_end(late.append)
            scheduled_close = asyncio.create_task(self.bot.close(reason="Second close"))

        def fails(result):
            if result.message == "First close":
                raise ValueError("broken session end observer")
            raise asyncio.CancelledError("cancelled session end observer")

        remove_first = self.bot.subscribe_session_end(first)
        self.bot.subscribe_session_end(fails)
        remove_duplicate = self.bot.subscribe_session_end(duplicate)
        self.bot.subscribe_session_end(duplicate)
        await self.bot.close()
        self.assertEqual(events, [])
        self.assertEqual(duplicates, [])

        wire = self.wire()
        await self.connect(wire)
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            await self.bot.close(reason="First close")
            self.assertIsNotNone(scheduled_close)
            await scheduled_close
            first_end = self.bot.session_end
            self.assertEqual(first_end, SessionEnd("local_close", message="First close"))
            self.assertEqual(events, [(first_end, first_end, ())])
            self.assertIs(events[0][0], first_end)
            self.assertIs(events[0][1], first_end)
            self.assertEqual(duplicates, [first_end, first_end])
            self.assertEqual(late, [])
            self.assertTrue(wire.closed)
            self.assertFalse(any(isinstance(packet, self.b.DisconnectPacket) for packet in wire.sent))

            remove_duplicate()
            remove_duplicate()
            await self.connect(self.wire(player_id=42))
            await self.bot.close()
            second_end = self.bot.session_end
            self.assertEqual(second_end, SessionEnd("local_close"))

        self.assertEqual(len(events), 1)
        self.assertEqual(duplicates, [first_end, first_end, second_end])
        self.assertEqual(late, [second_end])
        self.assertIs(late[0], second_end)
        self.assertEqual(
            [type(call.args[0]["exception"]) for call in report.call_args_list],
            [ValueError, asyncio.CancelledError],
        )

    async def test_local_close_binds_waiters_and_old_reader_packets_cannot_replace_a_rejoin(self):
        observed, late = [], []
        self.bot.subscribe_session_end(observed.append)
        first = self.wire()
        await self.connect(first)
        old_packets = self.bot.packets
        old_wait = asyncio.create_task(self.bot.wait_for_session_end())
        late_wire = asyncio.create_task(old_packets.wait_for_packet(self.b.DisconnectPacket.ID, timeout=1))
        state = self.bot._player_state
        drain = state.aclose
        detached, release = asyncio.Event(), asyncio.Event()

        async def drain_old_state():
            detached.set()
            await release.wait()
            await drain()

        with patch.object(state, "aclose", new=drain_old_state):
            closing = asyncio.create_task(self.bot.close(reason="Closing the first session"))
            try:
                await asyncio.wait_for(detached.wait(), 1)
                old_result = await old_wait
                self.assertEqual(old_result, SessionEnd("local_close", message="Closing the first session"))
                self.assertIs(self.bot.session_end, old_result)
                self.assertIs(observed[0], old_result)
                self.assertFalse(first.closed)
                self.bot.subscribe_session_end(late.append)
                self.assertEqual(late, [])

                replacement = self.wire(player_id=42)
                await self.connect(replacement)
                self.assertIsNone(self.bot.session_end)
                first.push(self.disconnect("Old session ended"))
                first.incoming.put_nowait(EOFError("Late EOF after local close"))
                self.assertEqual(old_packets.protocol.decode(await late_wire).messages.message, "Old session ended")
                self.assertIsNone(self.bot.session_end)
                replacement.push(self.disconnect("New session ended", filtered="New filtered"))
                new_result = await self.bot.wait_for_session_end(timeout=1)
                self.assertEqual(new_result, SessionEnd("server_disconnect", 55, "KICKED",
                                                        "New session ended", "New filtered"))
            finally:
                release.set()
                with self.assertRaisesRegex(EOFError, "Late EOF after local close"):
                    await closing
                for task in (old_wait, late_wire):
                    task.cancel()
                await asyncio.gather(old_wait, late_wire, return_exceptions=True)
        self.assertTrue(first.closed)
        self.assertFalse(replacement.closed)
        self.assertIs(self.bot.session_end, new_result)
        self.assertEqual(old_result, SessionEnd("local_close", message="Closing the first session"))
        self.assertEqual(observed, [old_result, new_result])
        self.assertEqual(late, [new_result])
        self.assertIs(late[0], new_result)

    async def test_pre_spawn_refusal_does_not_publish_a_result_and_invalid_join_retains_the_last_one(self):
        observed = []
        self.bot.subscribe_session_end(observed.append)
        self.assertIsNone(self.bot.session_end)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            await self.bot.wait_for_session_end(timeout=0)
        await self.connect(self.wire())
        await self.bot.close()
        saved = self.bot.session_end
        with self.assertRaises(ValueError):
            await self.connect(self.wire(), chunk_radius=0)
        self.assertIs(self.bot.session_end, saved)

        refused = Wire(self.bot.protocol)
        refused.push(self.b.NetworkSettingsPacket(compression_algorithm=self.b.PacketCompressionAlgorithm.NONE))
        refused.push(self.b.StartGamePacket(runtime_id=41, entity_id=41, server_version="Refused"))
        refused.push(self.disconnect("refused before spawn"))
        with self.assertRaises(LoginRefused):
            await self.connect(refused)
        self.assertTrue(refused.closed)
        self.assertIsNone(self.bot.session_end)
        self.assertIsNone(self.bot.reported_server_version)
        self.assertEqual(observed, [saved])
        self.assertIs(observed[0], saved)

    async def test_reported_server_version_tracks_only_the_active_joined_session(self):
        self.assertIsNone(self.bot.reported_server_version)
        b = self.b
        cases = (
            ("1.26.66", "server_disconnect"),
            (" Proxy Δ / 1.26.66 ", "reader_end"),
            ("", "local_close"),
            (" ", "local_close"),
        )
        for version, end in cases:
            with self.subTest(version=version, end=end):
                wire = self.wire(server_version=version)
                await self.connect(wire)
                sent_count = len(wire.sent)
                self.assertEqual(self.bot.reported_server_version, version or None)
                self.assertEqual(len(wire.sent), sent_count)
                with self.assertRaises(AttributeError):
                    self.bot.reported_server_version = "overwritten"

                if end == "server_disconnect":
                    wire.push(self.disconnect())
                elif end == "reader_end":
                    wire.incoming.put_nowait(EOFError("peer closed"))
                else:
                    await self.bot.close()
                self.assertEqual((await self.bot.wait_for_session_end(timeout=1)).kind, end)
                self.assertIsNone(self.bot.reported_server_version)
                if end == "reader_end":
                    with self.assertRaises(EOFError):
                        await self.bot.close()
                else:
                    await self.bot.close()

    async def test_cancelled_authorization_and_failed_assembly_never_publish_a_session(self):
        observed = []
        self.bot.subscribe_session_end(observed.append)
        entered = asyncio.Event()

        async def authorize(_public_key):
            entered.set()
            await asyncio.Event().wait()

        with patch("endpilot.transport.nethernet.connect") as connect:
            joining = asyncio.create_task(self.bot.join_nethernet(
                "http://127.0.0.1:19132", credential_provider=authorize,
            ))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertIsNone(self.bot.session_end)
                joining.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await joining
                connect.assert_not_awaited()
            finally:
                if not joining.done():
                    joining.cancel()
                    await asyncio.gather(joining, return_exceptions=True)
        self.assertIsNone(self.bot.session_end)

        wire = self.wire(self.disconnect("queued after spawn"))
        start = Movement.start

        def fail_after_start(movement, **options):
            start(movement, **options)
            raise RuntimeError("assembly failed")

        with patch.object(Movement, "start", new=fail_after_start):
            with self.assertRaisesRegex(RuntimeError, "assembly failed"):
                await self.connect(wire)
        self.assertTrue(wire.closed)
        self.assertIsNone(self.bot.session_end)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            await self.bot.wait_for_session_end(timeout=0)
        self.assertEqual(observed, [])

    async def test_cancelled_online_join_after_assembly_keeps_the_end_private(self):
        observed = []
        self.bot.subscribe_session_end(observed.append)
        wire = self.wire(self.disconnect("queued just after spawn"), server_version="Cancelled")
        start = self.bot._start_session

        def cancel_after_assembly(session, **options):
            start(session, **options)
            self.assertIsNone(self.bot.reported_server_version)
            joining.cancel()

        with (
            patch("endpilot.transport.nethernet.connect", return_value=wire),
            patch("endpilot.bot.acquire_online_identity", return_value=_online_identity()),
            patch.object(self.bot, "_start_session", new=cancel_after_assembly),
        ):
            joining = asyncio.create_task(self.bot.join_nethernet(
                "http://127.0.0.1:19132", credential_provider=object(),
            ))
            with self.assertRaises(asyncio.CancelledError):
                await joining
        self.assertTrue(wire.closed)
        self.assertIsNone(self.bot.session_end)
        self.assertIsNone(self.bot.reported_server_version)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            await self.bot.wait_for_session_end(timeout=0)
        self.assertEqual(observed, [])
