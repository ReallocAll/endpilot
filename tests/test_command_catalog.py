import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.commands import CommandCatalog, CommandCatalogEntry
from endpilot.protocol import write_batch
from endpilot.qa import PacketTimeline

from test_scheduled_input import Wire


class CommandCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot("CommandCatalog", protocol=2225)
        self.b = self.bot.protocol.binding
        self.addAsyncCleanup(self.bot.close)

    def catalog(self, names=(), *, values=(), enums=()):
        packet = self.b.AvailableCommandsPacket
        return packet(
            commands=[packet.CommandData(name=name, alias_enum=index) for name, index in names],
            enum_values=values,
            enums=[packet.EnumData(name=name, values=indexes) for name, indexes in enums],
        )

    def push_batch(self, *packets):
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))

    async def join(self, *startup, enabled=True, after_spawn=(), transport="nethernet"):
        self.wire = Wire(self.bot.protocol)
        self.timeline = PacketTimeline()
        b = self.b
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.push_batch(
            b.StartGamePacket(runtime_id=13, entity_id=27, settings=b.LevelSettings(commands_enabled=enabled)),
            *startup, b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN), *after_spawn,
        )
        with patch(f"endpilot.transport.{transport}.connect", return_value=self.wire):
            if transport == "nethernet":
                await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1, timeline=self.timeline)
            else:
                await self.bot.join_raknet("127.0.0.1", timeout=1, timeline=self.timeline)

    async def deliver(self, *packets):
        barrier = self.b.SetTimePacket(time=91)
        pending = asyncio.create_task(self.bot.packets.wait_for_packet(barrier.ID, timeout=1))
        await asyncio.sleep(0)
        self.push_batch(*packets, barrier)
        await pending

    async def test_startup_keeps_latest_catalog_and_completes_only_referenced_names(self):
        for transport in ("nethernet", "raknet"):
            with self.subTest(transport=transport):
                try:
                    self.assertIsNone(self.bot.command_catalog)
                    self.assertIsNone(self.bot.commands_enabled)
                    self.assertIsNone(self.bot.complete_command_name("/Tel"))
                    await self.join(
                        self.catalog((("old", -1),)),
                        self.catalog((("Tell", 1), ("t", -1)),
                                     values=("unreferenced", "t", "example:Tell", "Tell", "msg"),
                                     enums=(("otherValues", (0,)), ("TellAliases", (1, 2, 3, 2, 4)))),
                        self.b.SetCommandsEnabledPacket(commands_enabled=False),
                        transport=transport,
                    )
                    snapshot = self.bot.command_catalog
                    tell = CommandCatalogEntry("Tell", ("t", "example:Tell", "Tell", "msg"))
                    short = CommandCatalogEntry("t", ())
                    self.assertEqual(snapshot, CommandCatalog((tell, short)))
                    self.assertIs(self.bot.commands_enabled, False)
                    sent = list(self.wire.sent)
                    self.assertEqual(self.bot.complete_command_name("/Tel"), ("Tell",))
                    self.assertEqual(self.bot.complete_command_name("example:T"), ("example:Tell",))
                    self.assertEqual(self.bot.complete_command_name("m"), ("msg",))
                    self.assertEqual(self.bot.complete_command_name("/t"), ("t",))
                    for token in ("", "/"):
                        self.assertEqual(self.bot.complete_command_name(token), ("Tell", "t", "example:Tell", "msg"))
                    for token in ("//Tell", "/tell", "old", "unreferenced"):
                        self.assertEqual(self.bot.complete_command_name(token), ())
                    for token in ("Tell x", " /Tell", "Tell\t", "/Tell\nprobe"):
                        with self.assertRaisesRegex(ValueError, "whitespace"):
                            self.bot.complete_command_name(token)
                    self.assertEqual(self.wire.sent, sent)
                    with self.assertRaises(FrozenInstanceError):
                        snapshot.commands = ()
                    with self.assertRaises(FrozenInstanceError):
                        snapshot.commands[0].aliases = ()
                    self.assertEqual(self.timeline.received_count("AvailableCommandsPacket"), 2)
                finally:
                    await self.bot.close()

    async def test_live_replacement_and_empty_catalog_keep_enablement_independent(self):
        first = self.catalog((("first", 0),), values=("one",), enums=(("Aliases", (0,)),))
        second = self.catalog((("next", -1),), values=("one", "first"), enums=(("Unused", (0, 1)),))
        await self.join(first, enabled=False, after_spawn=(second,))
        before_live = self.bot.command_catalog
        self.assertEqual(before_live, CommandCatalog((CommandCatalogEntry("first", ("one",)),)))
        self.assertIs(self.bot.commands_enabled, False)

        await self.deliver(second, self.b.SetCommandsEnabledPacket(commands_enabled=True))
        replacement = self.bot.command_catalog
        self.assertEqual(replacement, CommandCatalog((CommandCatalogEntry("next", ()),)))
        self.assertEqual(self.bot.complete_command_name("/ne"), ("next",))
        self.assertEqual(self.bot.complete_command_name("one"), ())
        self.assertEqual(self.bot.complete_command_name("first"), ())
        self.assertIs(self.bot.commands_enabled, True)
        self.assertEqual(before_live.commands[0].name, "first")
        self.assertEqual(self.timeline.received_count("AvailableCommandsPacket"), 3)

        await self.deliver(self.catalog(), self.b.SetCommandsEnabledPacket(commands_enabled=False))
        self.assertEqual(self.bot.command_catalog, CommandCatalog(()))
        self.assertIs(self.bot.commands_enabled, False)
        self.assertEqual(self.bot.complete_command_name("next"), ())
        self.assertEqual(self.bot.complete_command_name(""), ())

    async def test_disconnect_clears_lookup_and_rejoin_rebuilds_it(self):
        await self.join(self.catalog((("visible", -1),)))
        snapshot = self.bot.command_catalog
        await self.deliver(self.b.DisconnectPacket(), self.catalog((("late", -1),)),
                           self.b.SetCommandsEnabledPacket(commands_enabled=True))
        self.assertIsNone(self.bot.command_catalog)
        self.assertIsNone(self.bot.commands_enabled)
        self.assertIsNone(self.bot.complete_command_name("visible"))
        await self.bot.close()
        self.assertEqual(snapshot.commands[0].name, "visible")

        await self.join(enabled=False)
        self.assertIsNone(self.bot.command_catalog)
        self.assertIs(self.bot.commands_enabled, False)
        await self.deliver(self.catalog((("new", -1),)))
        self.assertEqual(self.bot.complete_command_name("/n"), ("new",))
        await self.bot.close()
        self.assertIsNone(self.bot.command_catalog)
        self.assertIsNone(self.bot.commands_enabled)
        self.assertIsNone(self.bot.complete_command_name(""))

    async def test_reader_failure_clears_its_observations(self):
        await self.join(self.catalog((("visible", -1),)))
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _cause: ended.set())
        self.wire.incoming.put_nowait(ConnectionError("test transport lost"))
        await asyncio.wait_for(ended.wait(), timeout=1)
        self.assertIsNone(self.bot.command_catalog)
        self.assertIsNone(self.bot.commands_enabled)
        with self.assertRaisesRegex(ConnectionError, "test transport lost"):
            await self.bot.close()
