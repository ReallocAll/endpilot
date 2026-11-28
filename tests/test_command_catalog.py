import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot
from endpilot.commands import CommandCatalog, CommandCatalogEntry, CommandCompletion
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

    def overload(self, *symbols, optional_from=None, chaining=False):
        packet = self.b.AvailableCommandsPacket
        return packet.OverloadData(
            is_chaining=chaining,
            params=[
                packet.ParamData(parse_symbol=symbol, optional=optional_from is not None and index >= optional_from)
                for index, symbol in enumerate(symbols)
            ],
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

    async def test_completes_only_matching_positional_enum_overloads(self):
        packet = self.b.AvailableCommandsPacket
        start, stop = 0x00300001, 0x00300002
        hidden = 0x00300006
        await self.join(packet(
            enum_values=("cfg", "Start", "Stop", "fast", "shared", "slow", "safe", "hidden", "unused", "confirm"),
            enums=[
                packet.EnumData(name=name, values=indexes)
                for name, indexes in (
                    ("Alias", (0,)), ("Start", (1,)), ("Stop", (2,)),
                    ("FirstMode", (3, 4, 3)), ("SecondMode", (4, 5)), ("StopMode", (6,)),
                    ("Hidden", (7,)), ("BadValue", (255,)), ("Confirm", (9,)),
                )
            ],
            postfixes=("postfix",),
            commands=[packet.CommandData(name="configure", alias_enum=0, overloads=[
                self.overload(start, 0x00300003, 0x00300008, optional_from=1),
                self.overload(start, 0x00300004),
                self.overload(stop, 0x00300005),
                self.overload(start, hidden, chaining=True),
                self.overload(0x00100001, hidden),
                self.overload(0x01000000, hidden),
                self.overload(0x00300009, hidden),
                self.overload(0x04100001, hidden),
                self.overload(start, 0x00300007),
            ])],
        ))
        sent = list(self.wire.sent)
        for text, expected in (
            ("/c", ("configure", "cfg")),
            ("configure ", ("Start", "Stop")),
            ("/cfg Start\t", ("fast", "shared", "slow")),
            ("cfg Start s", ("shared", "slow")),
            ("cfg Stop ", ("safe",)),
            ("cfg Start fast c", ("confirm",)),
            ("cfg Start c", ()),
            ("cfg Start F", ()),
            ("cfg start ", ()),
            ("cfg Star ", ()),
            ("cfg anything ", ()),
            ("cfg postfix ", ()),
            ("cfg confirm ", ()),
            ("cfg Start fast confirm ", ()),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.bot.complete_command(text), expected)
        self.assertEqual(self.bot.complete_command_name("/c"), ("configure", "cfg"))
        self.assertEqual(self.bot._commands.complete_command("cfg Start s"), ("shared", "slow"))
        with self.assertRaisesRegex(ValueError, "whitespace"):
            self.bot.complete_command_name("cfg Start ")
        self.assertEqual(self.wire.sent, sent)

    async def test_hard_enum_reference_can_exceed_16_bits(self):
        packet = self.b.AvailableCommandsPacket
        low = packet.EnumData(name="LowIndex", values=(0,))
        high = packet.EnumData(name="HighIndex", values=(1,))
        await self.join(packet(
            enum_values=("low-value", "high-value"),
            enums=[low] * 65_536 + [high],
            commands=[packet.CommandData(
                name="configure", alias_enum=-1, overloads=[self.overload(0x00310000)],
            )],
        ))
        self.assertEqual(self.bot.complete_command("configure "), ("high-value",))

    async def test_completes_live_literal_tell_targets_and_replacement_spans(self):
        b = self.b
        packet = b.AvailableCommandsPacket
        target, message = 0x00100008, 0x00100044

        def advertisement(*aliases):
            return packet(
                enum_values=aliases,
                enums=[packet.EnumData(name="TellAliases", values=tuple(range(len(aliases))))],
                commands=[
                    packet.CommandData(name="tell", alias_enum=0, overloads=[self.overload(target, message)]),
                    packet.CommandData(name="other", alias_enum=-1, overloads=[self.overload(target, message)]),
                ],
            )

        def roster(*names):
            return b.PlayerListPacket(entries=[
                b.PlayerListPacket.AddEntry(action=b.PlayerListPacketType.ADD, uuid=UUID(int=index), name=name)
                for index, name in enumerate(names, start=1)
            ])

        await self.join()
        self.assertIsNone(self.bot.complete_command("/tell Dm"))
        self.assertIsNone(self.bot.complete_command_with_span("/tell Dm"))
        await self.deliver(advertisement("tell", "w", "msg"))
        self.assertEqual(self.bot.complete_command_with_span("/tell Dm"), CommandCompletion(6, 8, ()))
        await self.deliver(roster("Dm Peer", "DmSender_1", "Dm Peer", 'Bad"Name', "Bad\\Name", "Bad\tName",
                                  "Bad\x00Name", "Bad\nName", "@a", "Bad[Name]", "Bad'Name", " Bad Name"))

        sent = list(self.wire.sent)
        for query, start, expected in (
            ("/tell Dm", 6, ('"Dm Peer"', "DmSender_1")),
            ('/tell "Dm P', 6, ('"Dm Peer"',)),
            ('/tell "Dm ', 6, ('"Dm Peer"',)),
            ('/tell "DmS', 6, ("DmSender_1",)),
            ("/w DmS", 3, ("DmSender_1",)),
            ("msg\tDm", 4, ('"Dm Peer"', "DmSender_1")),
            (" \t/tell  ", 9, ('"Dm Peer"', "DmSender_1")),
            ("/tel", 1, ("tell",)),
        ):
            with self.subTest(query=query):
                result = self.bot.complete_command_with_span(query)
                self.assertEqual(result, CommandCompletion(start, len(query), expected))
                self.assertEqual(self.bot.complete_command(query), expected)

        result = self.bot.complete_command_with_span('/tell "Dm P')
        self.assertEqual('/tell "Dm P'[:result.start] + result.tokens[0] + '/tell "Dm P'[result.end:], '/tell "Dm Peer"')
        with self.assertRaises(FrozenInstanceError):
            result.start = 0

        for query in (
            "/tell dm", "/tell @", '/tell "@', "/tell Bad", '/tell "Bad', "/other Dm",
            "/unadvertised Dm", '/tell "Dm Peer" ', '/tell "Dm Peer" Dm', "/tell DmSender_1 Dm",
            "/tell Dm P", "/tell\nDm",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.bot.complete_command(query), ())
        self.assertEqual(self.wire.sent, sent)

        await self.deliver(b.PlayerListPacket(entries=[
            b.PlayerListPacket.RemoveEntry(action=b.PlayerListPacketType.REMOVE, uuid=UUID(int=index))
            for index in (1, 3)
        ]))
        self.assertEqual(self.bot.complete_command('/tell "Dm P'), ())
        self.assertEqual(self.bot.complete_command("/tell Dm"), ("DmSender_1",))

        await self.deliver(advertisement("msg"))
        self.assertEqual(self.bot.complete_command("/w Dm"), ())
        self.assertEqual(self.bot.complete_command("/msg Dm"), ("DmSender_1",))
        await self.deliver(self.catalog())
        self.assertEqual(self.bot.complete_command("/msg Dm"), ())
        await self.deliver(b.DisconnectPacket())
        self.assertIsNone(self.bot.complete_command_with_span("/msg Dm"))

    async def test_tell_requires_an_advertised_required_first_target(self):
        b = self.b
        packet = b.AvailableCommandsPacket
        await self.join(packet(commands=[packet.CommandData(name="tell", alias_enum=-1, overloads=[
            self.overload(0x0010000A),
            self.overload(0x00100008, optional_from=0),
            self.overload(0x00100008, chaining=True),
            self.overload(0x00100001, 0x00100008),
        ])]), b.PlayerListPacket(entries=[b.PlayerListPacket.AddEntry(
            action=b.PlayerListPacketType.ADD, uuid=UUID(int=1), name="Dm Peer",
        )]))
        self.assertEqual(self.bot.observed_players[0].name, "Dm Peer")
        self.assertEqual(self.bot.complete_command("/tell Dm"), ())

    async def test_scoreboard_completes_live_objectives_after_a_literal_player(self):
        packet = self.b.AvailableCommandsPacket
        players, set_action, reset_action = 0x00300000, 0x00300001, 0x00300002
        target, objectives, count = 0x0010000A, 0x04100000, 0x00100001
        await self.join(packet(
            enum_values=("players", "set", "add", "remove", "reset", "list"),
            enums=[
                packet.EnumData(name="ScoreboardPlayersCategory", values=(0,)),
                packet.EnumData(name="ScoreboardPlayersNumAction", values=(1, 2, 3)),
                packet.EnumData(name="ScoreboardResetAction", values=(4,)),
                packet.EnumData(name="ScoreboardListAction", values=(5,)),
            ],
            soft_enums=[packet.SoftEnumData(name="ScoreboardObjectives", values=("ep_alpha",))],
            commands=[packet.CommandData(name="scoreboard", alias_enum=-1, overloads=[
                self.overload(players, set_action, target, objectives, count),
                self.overload(players, reset_action, target, objectives, optional_from=3),
                self.overload(players, 0x00300003, target, optional_from=2),
            ])],
        ))
        sent = list(self.wire.sent)
        set_query = "/scoreboard players set Pilot_1 ep_"
        reset_query = "/scoreboard players reset Pilot_1 ep_"
        for query in (set_query, reset_query):
            self.assertEqual(self.bot.complete_command(query), ("ep_alpha",))
            self.assertEqual(self.bot.complete_command_with_span(query),
                             CommandCompletion(len(query) - 3, len(query), ("ep_alpha",)))

        update = self.b.SoftEnumUpdateType
        for operation, value, expected in (
            (update.ADD, "ep_beta", ("ep_alpha", "ep_beta")),
            (update.REMOVE, "ep_alpha", ("ep_beta",)),
        ):
            await self.deliver(self.b.UpdateSoftEnumPacket(
                enum_name="ScoreboardObjectives", type=operation, values=(value,),
            ))
            for query in (set_query, reset_query):
                self.assertEqual(self.bot.complete_command(query), expected)

        self.assertEqual(self.bot.complete_command("/scoreboard players set Pilot_1 "), ("ep_beta",))
        self.assertEqual(self.bot.complete_command("/scoreboard players reset Pilot_1 ep_a"), ())
        self.assertEqual(self.bot.complete_command("/scoreboard players reset Pilot_1 EP_"), ())
        self.assertEqual(self.bot.complete_command("/scoreboard players set "), ())
        self.assertEqual(self.bot.complete_command("/scoreboard players set ep_"), ())
        self.assertEqual(self.bot.complete_command("/scoreboard players set Pilot_1 ep_beta "), ())
        self.assertEqual(self.bot.complete_command("/scoreboard players Set Pilot_1 ep_"), ())
        self.assertEqual(self.bot.complete_command("/scoreboard players list Pilot_1 ep_"), ())
        for target_token in ("@s", "@a[tag=probe]", '*', '"Pilot_1"', '"Pilot 1"', "Pilot[one]"):
            with self.subTest(target=target_token):
                self.assertEqual(self.bot.complete_command(f"/scoreboard players set {target_token} ep_"), ())
        self.assertEqual(self.wire.sent, sent)

    async def test_scoreboard_completes_quoted_roster_targets_and_advertised_objectives(self):
        b = self.b
        packet = b.AvailableCommandsPacket
        players, set_action, reset_action, fixed_objectives, aliases = (0x00300000 + index for index in range(5))
        target, objectives, count = 0x0010000A, 0x04100000, 0x00100001

        def advertisement(*overloads):
            return packet(
                enum_values=("players", "set", "add", "reset", "ep_fixed", "scores"),
                enums=[
                    packet.EnumData(name=name, values=indexes)
                    for name, indexes in (
                        ("Category", (0,)), ("Actions", (1, 2)), ("Reset", (3,)),
                        ("Fixed", (4,)), ("Aliases", (5,)),
                    )
                ],
                soft_enums=[packet.SoftEnumData(name="ScoreboardObjectives", values=("ep_alpha",))],
                commands=[
                    packet.CommandData(name="scoreboard", alias_enum=aliases & 0xFFFFF, overloads=overloads),
                    packet.CommandData(name="other", alias_enum=-1, overloads=[
                        self.overload(players, set_action, target, objectives, count),
                    ]),
                ],
            )

        await self.join(advertisement(
            self.overload(players, set_action, target, objectives, count),
            self.overload(players, reset_action, target, fixed_objectives, optional_from=3),
            self.overload(players, reset_action, target, objectives, optional_from=3),
        ))

        def query(text, starts_at, tokens):
            with self.subTest(text=text):
                before = list(self.wire.sent)
                self.assertEqual(self.bot.complete_command_with_span(text),
                                 CommandCompletion(text.index(starts_at), len(text), tokens))
                self.assertEqual(self.bot.complete_command(text), tokens)
                self.assertEqual(self.wire.sent, before)

        query('/scoreboard players set "Dm P', '"', ())
        await self.deliver(b.PlayerListPacket(entries=[
            b.PlayerListPacket.AddEntry(action=b.PlayerListPacketType.ADD, uuid=UUID(int=1), name="Dm Peer"),
            b.PlayerListPacket.AddEntry(action=b.PlayerListPacketType.ADD, uuid=UUID(int=2), name="Dm Pal"),
        ]))

        partial = ' \t/scoreboard  players set\t"Dm '
        query(partial, '"', ('"Dm Peer"', '"Dm Pal"'))
        completion = self.bot.complete_command_with_span(partial)
        self.assertEqual(partial[:completion.start] + completion.tokens[0] + partial[completion.end:],
                         ' \t/scoreboard  players set\t"Dm Peer"')
        query('/scores players reset "Dm Pe', '"', ('"Dm Peer"',))
        query('/scores players set "Dm Peer" ep_', 'ep_', ("ep_alpha",))
        reset_query = '/scoreboard players reset "Dm Peer" ep_'
        set_query = '/scoreboard players set "Dm Peer" ep_'
        query(reset_query, 'ep_', ("ep_fixed", "ep_alpha"))
        for operation, value, expected in (
            (b.SoftEnumUpdateType.ADD, "ep_beta", ("ep_alpha", "ep_beta")),
            (b.SoftEnumUpdateType.REMOVE, "ep_alpha", ("ep_beta",)),
        ):
            await self.deliver(b.UpdateSoftEnumPacket(
                enum_name="ScoreboardObjectives", type=operation, values=(value,),
            ))
            query(set_query, 'ep_', expected)
            query(reset_query, 'ep_', ("ep_fixed", *expected))

        for text in (
            '/scoreboard players set "Dm Peer"', '/scoreboard players set "Dm Peer"ep_',
            '/scoreboard players set "Dm Peer" ep_beta ', '/scoreboard players set "Dm Peer" ep_ extra',
            '/scoreboard players set "Dm Peer " ep_', '/scoreboard players set "dm peer" ep_',
            '/scoreboard players set "Unknown Peer" ep_', '/scoreboard players set "@a" ep_',
            '/scoreboard players add "Dm Pe', '/scoreboard players add "Dm Peer" ep_',
            '/scoreboard players unknown "Dm Pe', '/other players set "Dm Pe',
            '/scoreboard players set Dm',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.bot.complete_command(text), ())

        await self.deliver(b.PlayerListPacket(entries=[
            b.PlayerListPacket.RemoveEntry(action=b.PlayerListPacketType.REMOVE, uuid=UUID(int=1)),
        ]))
        query(partial, '"', ('"Dm Pal"',))
        query(set_query, 'ep_', ())
        await self.deliver(advertisement(
            self.overload(players, set_action, target, objectives, count, chaining=True),
            self.overload(players, reset_action, target, objectives, optional_from=2),
        ))
        self.assertEqual(self.bot.complete_command(partial), ())
        self.assertEqual(self.bot.complete_command('/scores players reset "Dm Pa'), ())

    async def test_literal_target_keeps_overlapping_enum_overloads_independent(self):
        packet = self.b.AvailableCommandsPacket
        choose, named_target, fixed_choice, hidden_choice = (0x00300000 + i for i in range(4))
        target, objectives = 0x0010000A, 0x04100000
        await self.join(packet(
            enum_values=("choose", "Named", "shared", "fixed", "hidden"),
            enums=[
                packet.EnumData(name="Action", values=(0,)),
                packet.EnumData(name="Named", values=(1,)),
                packet.EnumData(name="Fixed", values=(2, 3)),
                packet.EnumData(name="Hidden", values=(4,)),
            ],
            soft_enums=[packet.SoftEnumData(name="Live", values=("live", "shared"))],
            commands=[packet.CommandData(name="configure", alias_enum=-1, overloads=[
                self.overload(choose, named_target, fixed_choice),
                self.overload(choose, target, objectives),
                self.overload(choose, 0x00100001, hidden_choice),
                self.overload(choose, target, hidden_choice, chaining=True),
            ])],
        ))
        self.assertEqual(self.bot.complete_command("/configure choose "), ("Named",))
        self.assertEqual(self.bot.complete_command("/configure choose Named "), ("shared", "fixed", "live"))
        self.assertEqual(self.bot.complete_command("/configure choose Another "), ("live", "shared"))
        self.assertEqual(self.bot.complete_command("/configure choose @s "), ())

    async def test_soft_enum_updates_follow_catalog_and_spawn_in_wire_order(self):
        packet = self.b.AvailableCommandsPacket
        enum_name = "ScoreboardObjectives"
        update_type = self.b.SoftEnumUpdateType
        query = "/scoreboard objectives remove "

        def catalog(*values):
            return packet(
                enum_values=("objectives", "remove", "inspect", "hard-only", "confirm"),
                enums=[
                    packet.EnumData(name=name, values=(index,))
                    for index, name in enumerate(("Category", "Remove", "Inspect", enum_name, "Confirm"))
                ],
                soft_enums=[packet.SoftEnumData(name=enum_name, values=values)],
                commands=[packet.CommandData(name="scoreboard", alias_enum=-1, overloads=[
                    self.overload(0x00300000, 0x00300001, 0x04100000, 0x00300004),
                    self.overload(0x00300000, 0x00300002, 0x00300003),
                ])],
            )

        def update(operation, *values, name=enum_name):
            return self.b.UpdateSoftEnumPacket(enum_name=name, values=values, type=operation)

        for transport in ("nethernet", "raknet"):
            with self.subTest(transport=transport):
                try:
                    self.assertIsNone(self.bot.complete_command(query))
                    await self.join(
                        update(update_type.ADD, "before"),
                        catalog("first"),
                        update(update_type.ADD, "old"),
                        catalog("base"),
                        update(update_type.ADD, "kept", "base", "kept"),
                        after_spawn=(update(update_type.REMOVE, "base"),),
                        transport=transport,
                    )
                    sent = list(self.wire.sent)
                    self.assertEqual(self.bot.complete_command(query), ("base", "kept"))

                    await self.deliver()
                    self.assertEqual(self.bot.complete_command(query), ("kept",))
                    await self.deliver(
                        update(update_type.REPLACE, "wrong-case", name=enum_name.lower()),
                        update(update_type.ADD, "later", "kept"),
                    )
                    self.assertEqual(self.bot.complete_command(query), ("kept", "later"))
                    self.assertEqual(self.bot.complete_command(query + "l"), ("later",))
                    self.assertEqual(self.bot.complete_command(query + "kept c"), ("confirm",))
                    self.assertEqual(self.bot.complete_command(query + "Kept c"), ())

                    await self.deliver(update(update_type.REPLACE, "fresh", "first", "fresh"))
                    self.assertEqual(self.bot.complete_command(query), ("fresh", "first"))
                    self.assertEqual(self.bot.complete_command("/scoreboard objectives inspect "), ("hard-only",))
                    await self.deliver(catalog())
                    self.assertEqual(self.bot.complete_command(query), ())
                    self.assertEqual(self.bot.complete_command("/scoreb"), ("scoreboard",))

                    await self.deliver(self.b.DisconnectPacket(), update(update_type.ADD, "late"))
                    self.assertIsNone(self.bot.complete_command(query))
                    self.assertEqual(self.wire.sent, sent)
                finally:
                    await self.bot.close()

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
