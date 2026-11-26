import asyncio
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bedrock_protocol import TextPacketType, nbt
from endpilot import BlockEntityUpdate, Bot
from endpilot.commands import Commands
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import Protocol, read_batch, write_batch
from test_world_observation import Transport


# Conditional command block states in the protocol 2225 sequential palette.
_IMPULSE_BLOCK = 22542
_REPEATING_BLOCK = 22251
_CHAIN_BLOCK = 18747


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_sends_typed_request_and_observes_bds_text_feedback(self):
        protocol = Protocol(2193)
        b = protocol.binding
        player_uuid = uuid.UUID("01234567-89ab-cdef-fedc-ba9876543210")
        stream = SimpleNamespace(protocol=protocol, send=AsyncMock(), wait_for_packet=AsyncMock())
        commands = Commands(stream, player_uuid, 57)
        text = b.TextPacket

        def incoming(kind, message):
            return protocol.encode(text(
                localize=False,
                body=text.MessageOnly(type=kind, message=message),
                xuid="", platform_id="", filtered_message=None,
            ))

        async def wait(packet_id, *, match, timeout):
            self.assertEqual(packet_id, (text.ID, b.DisconnectPacket.ID))
            self.assertEqual(timeout, 5)
            self.assertFalse(match(incoming(TextPacketType.SYSTEM_MESSAGE, "unrelated system text")))
            self.assertFalse(match(incoming(TextPacketType.RAW, "unrelated raw text")))
            while stream.send.await_count == 0:
                await asyncio.sleep(0)
            sent = stream.send.await_args.args[0]
            self.assertEqual(sent.command, "/setblock 1 99 0 gold_block")
            self.assertEqual(sent.origin.uuid, player_uuid)
            self.assertEqual(sent.origin.player_id, 57)
            self.assertEqual(sent.origin.type, b.CommandOriginType.PLAYER)
            self.assertEqual(sent.version, b.CurrentCmdVersion.LATEST)
            self.assertEqual(protocol.decode(protocol.encode(sent)).origin.request_id, sent.origin.request_id)
            reply = incoming(TextPacketType.TEXT_OBJECT, '{"rawtext":[{"translate":"commands.setblock.success"}]}')
            self.assertTrue(match(reply))
            return reply

        stream.wait_for_packet.side_effect = wait
        received = await commands.execute("/setblock 1 99 0 gold_block", timeout=5)
        self.assertIsInstance(received, b.TextPacket)
        self.assertEqual(received.body.type, TextPacketType.TEXT_OBJECT)
        self.assertIn("commands.setblock.success", received.body.message)


class CommandBlockTests(unittest.IsolatedAsyncioTestCase):
    position = (8, 20, 9)
    command = "say edited command"

    async def asyncSetUp(self):
        self.bot = Bot("CommandBlock", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.transport = Transport()
        packets = PacketConnection(self.transport, self.bot.protocol, b.NetworkSettingsPacket(
            compression_algorithm=b.PacketCompressionAlgorithm.ZLIB, compression_threshold=0,
        ))
        address = "http://127.0.0.1:19132"
        session = JoinSession(packets, self.bot.identity, address)
        session.player_id, session.player_unique_id = 42, 7
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await self.bot.join_nethernet(address)
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=b.GameType.CREATIVE))

    def block(self, runtime_id=_IMPULSE_BLOCK):
        x, y, z = self.position
        return self.b.UpdateBlockPacket(pos=self.b.BlockPos(x=x, y=y, z=z), runtime_id=runtime_id, layer=0)

    def actor(self, command="say previous command", *, position=None, output="", filtered_name=None):
        x, y, z = self.position if position is None else position
        data = nbt.CompoundTag({
            "id": nbt.StringTag("CommandBlock"), "x": nbt.IntTag(x), "y": nbt.IntTag(y), "z": nbt.IntTag(z),
            "Command": nbt.StringTag(command), "auto": nbt.ByteTag(1), "conditionalMode": nbt.ByteTag(1),
            "conditionMet": nbt.ByteTag(0), "powered": nbt.ByteTag(0), "CustomName": nbt.StringTag("Keep this name"),
            "TrackOutput": nbt.ByteTag(0), "TickDelay": nbt.IntTag(12), "ExecuteOnFirstTick": nbt.ByteTag(1),
            "LastOutput": nbt.StringTag(output),
        })
        if filtered_name is not None:
            data["FilteredCustomName"] = nbt.StringTag(filtered_name)
        return self.b.BlockActorDataPacket(pos=self.b.BlockPos(x=x, y=y, z=z), data=data)

    async def deliver(self, *packets):
        self.transport.incoming.put_nowait(b"\xff" + write_batch(map(self.bot.protocol.encode, packets)))
        await asyncio.sleep(0)

    def during_command_send(self, receive):
        send = self.transport.send

        async def send_and_receive(frame):
            await send(frame)
            if any(raw.packet_id == self.b.CommandBlockUpdatePacket.ID for raw in read_batch(frame[1:])):
                await receive()

        return patch.object(self.transport, "send", side_effect=send_and_receive)

    def sent_commands(self):
        return [self.bot.protocol.decode(raw) for frame in self.transport.sent for raw in read_batch(frame[1:])
                if raw.packet_id == self.b.CommandBlockUpdatePacket.ID]

    async def test_command_block_uses_fresh_receipt_during_send_and_preserves_settings(self):
        filtered_name = "Filtered name"
        mode = self.b.CommandBlockMode

        async def receive():
            await self.deliver(self.actor(self.command, output="fresh", filtered_name=filtered_name))
            self.assertEqual(self.bot.observed_block_entity(self.position)["LastOutput"].value, "fresh")

        for runtime_id, expected_mode in (
            (_IMPULSE_BLOCK, mode.NORMAL), (_REPEATING_BLOCK, mode.REPEATING), (_CHAIN_BLOCK, mode.CHAIN),
        ):
            with self.subTest(mode=expected_mode.name):
                self.transport.sent.clear()
                await self.deliver(
                    self.block(runtime_id), self.actor(self.command, output="cached", filtered_name=filtered_name),
                )

                with self.during_command_send(receive):
                    result = await self.bot.set_command_block(self.position, self.command, timeout=1)

                self.assertIsInstance(result, BlockEntityUpdate)
                self.assertEqual((result.dimension_id, result.position), (0, self.position))
                self.assertEqual(result.data["Command"].value, self.command)
                self.assertEqual(result.data["LastOutput"].value, "fresh")
                self.assertEqual(len(self.transport.sent), 1)
                packet, = self.sent_commands()
                self.assertIsInstance(packet.target, self.b.CommandBlockUpdatePacket.BlockCommandData)
                pos = packet.target.block_pos
                self.assertEqual((pos.x, pos.y, pos.z), self.position)
                self.assertEqual(packet.target.mode, expected_mode)
                self.assertFalse(packet.target.redstone_mode)
                self.assertTrue(packet.target.is_conditional)
                self.assertEqual(packet.command, self.command)
                self.assertEqual(packet.name, "Keep this name")
                self.assertEqual(packet.filtered_name, filtered_name)
                self.assertFalse(packet.track_output)
                self.assertEqual(packet.tick_delay, 12)
                self.assertTrue(packet.execute_on_first_tick)

    async def test_command_block_rejection_and_zero_timeout_do_not_send(self):
        with self.assertRaises(RuntimeError):
            await self.bot.set_command_block(self.position, self.command, timeout=1)
        await self.deliver(self.block(3317), self.actor())  # minecraft:stone in the 2225 palette.
        with self.assertRaises(RuntimeError):
            await self.bot.set_command_block(self.position, self.command, timeout=1)
        await self.deliver(self.block(), self.actor())
        with self.assertRaises(TimeoutError):
            await self.bot.set_command_block(self.position, self.command, timeout=0)
        self.assertEqual(self.transport.sent, [])

    async def test_command_block_timeout_rejects_pre_send_and_unrelated_receipts_without_retry(self):
        await self.deliver(self.block(), self.actor())
        send = self.bot.packets.send

        async def before_guard(packet, **kwargs):
            if isinstance(packet, self.b.CommandBlockUpdatePacket):
                await self.deliver(self.actor(self.command))
                self.assertEqual(self.bot.observed_block_entity(self.position)["Command"].value, self.command)
            await send(packet, **kwargs)

        async def receive():
            await self.deliver(self.actor(self.command, position=(9, 20, 9)), self.actor(self.command + "!"))
            far_away = self.b.BlockPos(x=1000, y=20, z=1000)
            await self.deliver(self.b.NetworkChunkPublisherUpdatePacket(position=far_away, radius=32),
                               self.actor(self.command))
            self.assertIsNone(self.bot.observed_block_entity(self.position))

        with patch.object(self.bot.packets, "send", side_effect=before_guard), self.during_command_send(receive):
            with self.assertRaises(TimeoutError):
                await self.bot.set_command_block(self.position, self.command, timeout=0.1)
        self.assertEqual(len(self.sent_commands()), 1)

    async def test_command_block_dimension_cycle_cannot_confirm_same_coordinates(self):
        await self.deliver(self.block(), self.actor())

        async def receive():
            await self.deliver(self.b.ChangeDimensionPacket(dimension_id=1),
                               self.b.ChangeDimensionPacket(dimension_id=0),
                               self.block(), self.actor(self.command))

        with self.during_command_send(receive), self.assertRaises(RuntimeError):
            await asyncio.wait_for(self.bot.set_command_block(self.position, self.command, timeout=2), 1)
        self.assertEqual(len(self.sent_commands()), 1)

    async def test_command_block_mode_replacement_cannot_confirm_edit(self):
        for initial_id, replacement_id, replacement_name in (
            (_IMPULSE_BLOCK, _REPEATING_BLOCK, "minecraft:repeating_command_block"),
            (_REPEATING_BLOCK, _CHAIN_BLOCK, "minecraft:chain_command_block"),
        ):
            with self.subTest(replacement=replacement_name):
                self.transport.sent.clear()
                await self.deliver(self.block(initial_id), self.actor())

                async def receive():
                    await self.deliver(self.block(replacement_id), self.actor(self.command))

                with self.during_command_send(receive), self.assertRaises((RuntimeError, TimeoutError)):
                    await self.bot.set_command_block(self.position, self.command, timeout=0.25)

                state = self.bot.observed_block_state(self.position)
                self.assertEqual(state.name, replacement_name)
                self.assertEqual(self.bot.observed_block_entity(self.position)["Command"].value, self.command)
                self.assertEqual(len(self.sent_commands()), 1)

    async def test_send_command_proceeds_while_command_block_waits_for_receipt(self):
        await self.deliver(self.block(), self.actor())
        packets = self.bot.packets
        raw_before = tuple(packets._subscribers)
        edit_sent = asyncio.Event()

        def observe_send(packet_id):
            if packet_id == self.b.CommandBlockUpdatePacket.ID:
                edit_sent.set()

        self.addCleanup(packets.subscribe_sent(observe_send))
        edit = asyncio.create_task(self.bot.set_command_block(self.position, self.command, timeout=5))
        try:
            await asyncio.wait_for(edit_sent.wait(), 1)
            await asyncio.wait_for(self.bot.send_command("say ordinary command"), 1)
            self.assertFalse(edit.done())
            await self.deliver(self.actor(self.command))
            result = await asyncio.wait_for(edit, 1)
        finally:
            edit.cancel()
            await asyncio.gather(edit, return_exceptions=True)

        self.assertEqual(result.data["Command"].value, self.command)
        self.assertEqual(tuple(packets._subscribers), raw_before)
        self.assertEqual(len(self.transport.sent), 2)
        edit_packet, = self.sent_commands()
        self.assertEqual(edit_packet.command, self.command)
        self.assertEqual(edit_packet.filtered_name, "")
        raw, = read_batch(self.transport.sent[1][1:])
        ordinary_packet = self.bot.protocol.decode(raw)
        self.assertIsInstance(ordinary_packet, self.b.CommandRequestPacket)
        self.assertEqual(ordinary_packet.command, "/say ordinary command")

    async def test_command_block_disconnect_cannot_confirm_later_packet(self):
        await self.deliver(self.block(), self.actor())

        async def receive():
            await self.deliver(self.b.DisconnectPacket(), self.actor(self.command))

        with self.during_command_send(receive), self.assertRaises(ConnectionError):
            await asyncio.wait_for(self.bot.set_command_block(self.position, self.command, timeout=2), 1)
        self.assertEqual(len(self.sent_commands()), 1)

    async def test_command_block_cancel_during_send_releases_receipt_observers(self):
        await self.deliver(self.block(), self.actor())
        packets = self.bot.packets
        raw_before = tuple(packets._subscribers)
        end_before = tuple(packets._reader_end_subscribers)
        sending = asyncio.Event()

        async def hold_send():
            sending.set()
            await asyncio.Future()

        with self.during_command_send(hold_send):
            edit = asyncio.create_task(self.bot.set_command_block(self.position, self.command, timeout=2))
            try:
                await asyncio.wait_for(sending.wait(), 1)
            finally:
                edit.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await edit

        self.assertEqual(tuple(packets._subscribers), raw_before)
        self.assertEqual(tuple(packets._reader_end_subscribers), end_before)
        self.assertEqual(len(self.sent_commands()), 1)
