import asyncio
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID
import weakref

from bedrock_protocol import DecodeError

from endpilot import Bot, PlayerCapeSnapshot, PlayerListChange, PlayerListEntry, PlayerMembershipEvent, PlayerSkinSnapshot
from endpilot.protocol import Protocol, RawPacket, read_batch, write_batch
from endpilot.skin import _player_skin_snapshot


EMPTY_CAPE = PlayerCapeSnapshot("", 0, 0, b"")

# bedrock-protocol 1aa8ae119 tests/goldens/v2225/063_player_list.json, add_and_remove_entries.
GOLDEN_ROSTER = RawPacket(63, bytes.fromhex(
    "020100000000000000000001000000000000000e055374657665103235333534313030303030303030303104706669640470636964"
    "0800000003736964257b2267656f6d65747279223a7b2264656661756c74223a2267656f6d657472792e61227d7d010000000100"
    "000004010203040001000000010000000405060708037b7d0a05312e302e30025b5d036369640366696401040302010000010101"
    "01010474727565046861736801010108070605000100000000000000000200000000000000"
))
# tests/goldens/v2225/093_player_skin.json, player_skin_update.
GOLDEN_UPDATE = RawPacket(93, bytes.fromhex(
    "8e3b48a5588c689ebfd05a00e1c969af03736964257b2267656f6d65747279223a7b2264656661756c74223a2267656f6d657472"
    "792e61227d7d0100000001000000040102030401010000000100000004090a0b0c010000803f010100000001000000040506070803"
    "7b7d0a05312e302e30025b5d04636170650466756c6c0101020304010270310100000089351bad0840a55541dfc129260d21aa0104"
    "70726f640108736b656c65746f6e0100000002000000030000000400000001010101010566616c73650468617368036e6577036f6c64"
))
# tests/goldens/v1001/093_player_skin.json, player_skin_update.
GOLDEN_HISTORICAL_UPDATE = RawPacket(93, bytes.fromhex(
    "8e3b48a5588c689ebfd05a00e1c969af037369640470666964267b2267656f6d65747279223a7b2264656661756c74223a226765"
    "6f6d657472792e61227d7d0a0100000001000000040102030401000000010000000100000004090a0b0c01000000000000400100"
    "000001000000010000000405060708037b7d0a05312e302e30035b5d0a0363696403666964047769646509236666303130323033"
    "01000000037069640c706572736f6e615f686169722430303030303030302d303030302d303030302d303030302d303030303030"
    "303030303033010470726f64010000000c706572736f6e615f686169720400000009236666303130323033092366663035303630"
    "3709236666303130323034092366663035303630380101010101036e6577036f6c6401"
))


class BatchTransport:
    def __init__(self):
        self.incoming = asyncio.Queue()

    async def send(self, data):
        pass

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        pass


class PlayerListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("RosterObserver", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.binding = self.bot.protocol.binding
        self.roster = self.binding.PlayerListPacket

    def add(self, uuid, name, **metadata):
        return self.roster.AddEntry(
            action=self.binding.PlayerListPacketType.ADD, uuid=uuid, name=name, **metadata,
        )

    def remove(self, uuid):
        return self.roster.RemoveEntry(action=self.binding.PlayerListPacketType.REMOVE, uuid=uuid)

    def native_skin(self, snapshot):
        binding = self.binding
        model = "UNKNOWN" if snapshot.model is None else snapshot.model.upper()
        cape = snapshot.cape or EMPTY_CAPE
        return binding.SerializedSkinRef(
            id=snapshot.skin_id, resource_patch=snapshot.resource_patch,
            skin_image=binding.SkinImage(width=snapshot.width, height=snapshot.height, image_bytes=snapshot.rgba),
            cape_id=cape.id,
            cape_image=binding.SkinImage(width=cape.width, height=cape.height, image_bytes=cape.rgba),
            arm_size_type=binding.ArmSizeType[model],
        )

    def skin_packet(self, uuid, snapshot):
        return self.binding.PlayerSkinPacket(uuid=uuid, skin=self.native_skin(snapshot))

    def deliver(self, *packets):
        self.transport.incoming.put_nowait(write_batch(
            packet if isinstance(packet, RawPacket) else self.bot.protocol.encode(packet) for packet in packets
        ))

    async def deliver_through(self, *packets):
        last = self.bot.protocol.encode(packets[-1])
        waiting = asyncio.create_task(self.bot.packets.wait_for_packet(
            last.packet_id, match=lambda packet: packet == last, timeout=1,
        ))
        await asyncio.sleep(0)
        self.deliver(*packets)
        await waiting

    async def join(self, *before_spawn, queued=()):
        self.transport = BatchTransport()
        binding = self.binding
        self.deliver(binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE))
        self.deliver(binding.StartGamePacket(entity_id=17, runtime_id=72), *before_spawn,
                     binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN), *queued)
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19142")

    def player_spawn(self, uuid, runtime_id, entity_id, mode):
        binding = self.binding
        return binding.AddPlayerPacket(
            uuid=uuid, name="Peer", runtime_id=runtime_id, player_game_type=mode,
            abilities_data=binding.SerializedAbilitiesData(target_player=entity_id),
        )

    def mode_update(self, entity_id, mode, *, tick=10):
        return self.binding.UpdatePlayerGameTypePacket(target_player=entity_id, player_game_type=mode, tick=tick)

    def player_modes(self):
        return {player.uuid: player.game_mode for player in self.bot.observed_players}

    async def test_startup_game_modes_follow_roster_and_spawn_wire_order(self):
        mode = self.binding.GameType
        before, after, updated = (UUID(int=index) for index in (110, 111, 112))
        await self.join(
            self.player_spawn(before, 201, 301, mode.CREATIVE),
            self.mode_update(301, mode.SURVIVAL),
            self.roster(entries=[self.add(before, "Before", id=301), self.add(after, "After", id=302),
                                 self.add(updated, "Updated", id=303)]),
            self.mode_update(302, mode.CREATIVE),
            self.player_spawn(after, 202, 302, mode.SURVIVAL),
            self.player_spawn(updated, 203, 303, mode.CREATIVE),
            self.mode_update(303, mode.ADVENTURE),
        )
        self.assertEqual(self.player_modes(), {before: mode.CREATIVE, after: mode.SURVIVAL, updated: mode.ADVENTURE})

    async def test_spawn_mode_requires_matching_nonzero_identity_and_an_explicit_mode(self):
        mode = self.binding.GameType
        peer = UUID(int=110)
        await self.join(self.roster(entries=[self.add(peer, "Peer", id=301)]))
        self.assertEqual(self.player_modes(), {peer: None})
        await self.deliver_through(self.player_spawn(peer, 201, 301, mode.CREATIVE))
        self.assertEqual(self.player_modes(), {peer: mode.CREATIVE})
        for entity_id, offered in ((302, mode.SURVIVAL), (0, mode.SURVIVAL), (301, mode.DEFAULT),
                                   (301, mode.UNDEFINED), (301, 101)):
            with self.subTest(entity_id=entity_id, offered=offered):
                await self.deliver_through(self.player_spawn(peer, 201, entity_id, offered))
                self.assertEqual(self.player_modes(), {peer: mode.CREATIVE})
        await self.deliver_through(self.mode_update(301, mode.DEFAULT), self.mode_update(301, 101))
        self.assertEqual(self.player_modes(), {peer: mode.CREATIVE})
        await self.deliver_through(self.player_spawn(peer, 201, 301, mode.SURVIVAL))
        self.assertEqual(self.player_modes(), {peer: mode.SURVIVAL})

    async def test_unlisted_spawn_mode_expires_with_instance_or_first_roster_add(self):
        binding = self.binding
        await self.join()
        for index, cause in enumerate(("remove", "dimension", "runtime_reuse", "appearance_replaced", "id_reuse",
                                       "unmatched_add", "zero_add", "roster_remove"), 1):
            with self.subTest(cause=cause):
                peer, other = UUID(int=index), UUID(int=index + 100)
                runtime_id, entity_id = index + 200, index + 300
                await self.deliver_through(self.player_spawn(peer, runtime_id, entity_id, binding.GameType.CREATIVE))
                match cause:
                    case "remove":
                        invalidation = binding.RemoveActorPacket(entity_id=entity_id)
                    case "dimension":
                        invalidation = binding.ChangeDimensionPacket(dimension_id=1)
                    case "runtime_reuse":
                        invalidation = self.player_spawn(other, runtime_id, 400, binding.GameType.DEFAULT)
                    case "appearance_replaced":
                        invalidation = self.player_spawn(peer, 500, entity_id, binding.GameType.DEFAULT)
                    case "id_reuse":
                        invalidation = self.player_spawn(other, 500, entity_id, binding.GameType.DEFAULT)
                    case "unmatched_add":
                        invalidation = self.roster(entries=[self.add(peer, "Peer", id=400)])
                    case "zero_add":
                        invalidation = self.roster(entries=[self.add(peer, "Peer", id=0)])
                    case "roster_remove":
                        invalidation = self.roster(entries=[self.remove(peer)])
                await self.deliver_through(invalidation, self.roster(entries=[self.add(peer, "Peer", id=entity_id)]))
                self.assertIsNone(self.player_modes()[peer])

    async def test_listed_game_mode_survives_visibility_changes_and_expires_with_membership(self):
        binding = self.binding
        mode = binding.GameType
        peer = UUID(int=110)
        initial = self.roster(entries=[self.add(peer, "Peer", id=301)])
        await self.join(initial, self.player_spawn(peer, 201, 301, mode.CREATIVE))
        saved, = self.bot.observed_players
        self.assertEqual(saved.game_mode, mode.CREATIVE)
        membership = []
        self.bot.subscribe_player_membership(membership.append, player_uuid=peer)
        self.assertEqual(membership, [])
        await self.deliver_through(binding.RemoveActorPacket(entity_id=301), self.mode_update(301, mode.ADVENTURE, tick=100))
        self.assertEqual(self.player_modes(), {peer: mode.ADVENTURE})
        self.assertEqual(self.bot.observed_entities, ())
        await self.deliver_through(binding.ChangeDimensionPacket(dimension_id=1))
        self.assertEqual(self.player_modes(), {peer: mode.ADVENTURE})
        await self.deliver_through(self.mode_update(301, mode.SURVIVAL, tick=1),
                                   self.roster(entries=[self.add(peer, "Renamed", id=301)]))
        skin = PlayerSkinSnapshot("last", 1, 1, b"\x00\x00\x00\xff", "slim", "", cape=EMPTY_CAPE)
        await self.deliver_through(self.skin_packet(peer, skin))
        current, = self.bot.observed_players
        self.assertEqual((current.name, current.game_mode, current.skin), ("Renamed", mode.SURVIVAL, skin))
        self.assertEqual((saved.name, saved.game_mode), ("Peer", mode.CREATIVE))
        self.assertEqual(membership, [])
        self.assertEqual(self.bot.observed_entities, ())
        await self.deliver_through(self.roster(entries=[self.remove(peer), self.remove(peer)]))
        self.assertEqual(self.bot.observed_players, ())
        self.assertEqual(membership, [PlayerMembershipEvent("left", current)])
        self.assertIs(membership[0].player, current)
        await self.deliver_through(self.mode_update(301, mode.CREATIVE), initial)
        self.assertEqual(self.player_modes(), {peer: None})
        self.assertEqual(membership[-1], PlayerMembershipEvent("joined", self.bot.observed_players[0]))
        self.assertIsNone(membership[-1].player.skin)
        await self.deliver_through(self.mode_update(301, mode.CREATIVE))
        self.assertEqual(self.player_modes(), {peer: mode.CREATIVE})
        self.assertEqual(len(membership), 2)
        self.assertIsNone(membership[-1].player.game_mode)
        await self.bot.close()
        await self.join(initial)
        self.assertEqual(self.player_modes(), {peer: None})

    async def test_player_changes_follow_mode_updates_and_uuid_membership(self):
        b = self.binding
        mode = b.GameType
        local, peer, other = self.bot.identity.uuid, UUID(int=110), UUID(int=111)
        initial = self.add(peer, "Peer", id=301)
        await self.join(self.roster(entries=[self.add(local, "Self", id=17), initial]))
        saved_local, saved_peer = self.bot.observed_players
        changes, matching = [], []

        def observe(entry):
            current = next(player for player in self.bot.observed_players if player.uuid == entry.uuid)
            changes.append((entry, current))

        self.bot.subscribe_player_changes(observe)
        self.bot.subscribe_player_changes(matching.append, uuid=peer)
        await self.deliver_through(
            self.mode_update(17, mode.CREATIVE),
            self.mode_update(301, mode.SURVIVAL),
            self.mode_update(301, mode.SURVIVAL),
            self.roster(entries=[self.add(other, "Other", id=401)]),
            self.mode_update(401, mode.CREATIVE),
            b.SetTimePacket(),
        )
        self.assertEqual(self.bot.observed_entities, ())
        await self.deliver_through(
            self.player_spawn(peer, 201, 301, mode.CREATIVE),
            self.player_spawn(peer, 202, 301, mode.CREATIVE),
            self.roster(entries=[self.remove(peer)]),
            self.mode_update(301, mode.ADVENTURE),
            self.roster(entries=[initial]),
            self.roster(entries=[initial]),
            b.SetTimePacket(),
        )
        self.assertIsNone(self.player_modes()[peer])
        await self.deliver_through(
            b.ChangeDimensionPacket(dimension_id=1),
            self.mode_update(301, mode.ADVENTURE),
            self.roster(entries=[self.add(local, "Renamed self", id=17)]),
        )
        final_players = {entry.uuid: entry for entry in self.bot.observed_players}
        self.assertEqual(final_players[other].game_mode, mode.CREATIVE)
        first = replace(saved_peer, game_mode=mode.SURVIVAL)
        visible = replace(saved_peer, game_mode=mode.CREATIVE)
        rejoined = replace(saved_peer, game_mode=mode.ADVENTURE)
        expected = [first, final_players[other], visible, rejoined, replace(saved_local, name="Renamed self")]
        self.assertEqual(changes, [(entry, entry) for entry in expected])
        self.assertTrue(all(entry is current for entry, current in changes))
        self.assertEqual(matching, [first, visible, rejoined])
        self.assertIsNone(saved_peer.game_mode)
        self.assertIsNone(final_players[local].game_mode)
        with self.assertRaises(FrozenInstanceError):
            matching[0].name = "Changed"

    async def test_player_changes_apply_each_roster_row_and_keep_raw_packet_observations(self):
        peer, other = UUID(int=112), UUID(int=113)
        await self.join(self.roster(entries=[self.add(peer, "Original", id=301)]),
                        self.mode_update(301, self.binding.GameType.SURVIVAL))
        original, = self.bot.observed_players
        changes, raw = [], []

        def observe(entry):
            changes.append((entry, self.bot.observed_players[0]))

        self.bot.subscribe_player_changes(observe)
        self.bot.subscribe_player_list_updates(lambda rows: raw.append((rows, self.bot.observed_players)))
        first = self.add(peer, "First", id=301, color=4)
        second = self.add(peer, "Second", id=301, color=4)
        new = self.add(other, "New", id=402)
        await self.deliver_through(self.roster(entries=[first, first, new, second]))
        expected = [replace(original, name="First", color=4), replace(original, name="Second", color=4)]
        self.assertEqual(changes, [(entry, entry) for entry in expected])
        self.assertTrue(all(entry is current for entry, current in changes))
        (raw_rows, raw_roster), = raw
        self.assertEqual([row.player.name for row in raw_rows], ["First", "First", "New", "Second"])
        self.assertTrue(all(row.player.game_mode is None for row in raw_rows))
        self.assertEqual(raw_roster, self.bot.observed_players)
        self.assertEqual(raw_roster[0], expected[-1])

    async def test_mode_update_requires_a_single_current_remote_target(self):
        mode = self.binding.GameType
        local, peer, other, without_id = self.bot.identity.uuid, UUID(int=110), UUID(int=111), UUID(int=112)
        await self.join(self.roster(entries=[self.add(local, "Self", id=17), self.add(peer, "Peer", id=301),
                                             self.add(other, "Other", id=301), self.add(without_id, "Zero", id=0)]),
                        self.player_spawn(peer, 201, 301, mode.CREATIVE))
        own_changes = []
        roster_changes = []
        self.bot.subscribe_game_mode(own_changes.append)
        self.bot.subscribe_player_list_updates(roster_changes.append)
        await self.deliver_through(self.mode_update(301, mode.SURVIVAL), self.mode_update(0, mode.SURVIVAL))
        self.assertEqual(self.player_modes(), {local: None, peer: mode.CREATIVE, other: None, without_id: None})
        self.assertEqual(roster_changes, [])
        self.assertEqual(own_changes, [])

        await self.deliver_through(self.roster(entries=[self.add(peer, "Peer", id=302)]),
                                   self.mode_update(301, mode.SURVIVAL))
        self.assertEqual(self.player_modes(), {local: None, peer: None, other: mode.SURVIVAL, without_id: None})
        await self.deliver_through(self.player_spawn(local, 205, 17, mode.CREATIVE), self.mode_update(17, mode.CREATIVE))
        self.assertEqual(own_changes, [mode.CREATIVE])
        self.assertEqual(self.bot.game_mode, mode.CREATIVE)
        self.assertIsNone(self.player_modes()[local])
        await self.deliver_through(self.roster(entries=[self.add(local, "Self", id=303)]),
                                   self.player_spawn(local, 206, 303, mode.CREATIVE), self.mode_update(303, mode.SURVIVAL))
        self.assertEqual(own_changes, [mode.CREATIVE])
        self.assertIsNone(self.player_modes()[local])

    async def test_roster_modes_use_login_uuid_after_configured_identity_changes(self):
        binding = self.binding
        mode = binding.GameType
        identity = self.bot.identity
        login_uuid = identity.uuid
        next_uuid = UUID(int=110)
        login_sent = asyncio.Event()

        async def observe_send(data):
            if any(raw.packet_id == binding.LoginPacket.ID for raw in read_batch(data)):
                login_sent.set()

        self.transport = BatchTransport()
        self.deliver(binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE))
        with (patch("endpilot.transport.nethernet.connect", return_value=self.transport),
              patch.object(self.transport, "send", side_effect=observe_send)):
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.bot.join_nethernet("http://127.0.0.1:19142"))
                await asyncio.wait_for(login_sent.wait(), timeout=1)
                identity.uuid = next_uuid
                self.deliver(
                    binding.StartGamePacket(entity_id=17, runtime_id=72),
                    self.roster(entries=[self.add(login_uuid, "Self", id=301), self.add(next_uuid, "Peer", id=302)]),
                    self.player_spawn(login_uuid, 201, 301, mode.CREATIVE),
                    self.player_spawn(next_uuid, 202, 302, mode.CREATIVE),
                    binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN),
                )

        self.assertEqual(self.player_modes(), {login_uuid: None, next_uuid: mode.CREATIVE})
        await self.deliver_through(self.mode_update(301, mode.ADVENTURE), self.mode_update(302, mode.SURVIVAL))
        self.assertEqual(self.player_modes(), {login_uuid: None, next_uuid: mode.SURVIVAL})

    async def test_login_roster_survives_actor_removal_and_dimension_change(self):
        uuid = UUID(int=110)
        binding = self.binding
        skin = PlayerSkinSnapshot("initial", 2, 1, b"\x07\x0d\x17\xff\x03\x02\x01\x00", "wide", "patch",
                                  cape=EMPTY_CAPE)
        await self.join(
            self.roster(entries=[self.add(uuid, "Peer", id=301, xuid="1234", play_fab_id="abcdef",
                                          platform_online_id="platform", build_platform=binding.BuildPlatform.WIN32,
                                          is_host=True, is_teacher=True, is_sub_client=True, color=-123,
                                          skin=self.native_skin(skin))]),
        )
        expected = PlayerListEntry(uuid, "Peer", 301, "1234", "abcdef", "platform", 8, True, True, True, -123, skin)
        saved = self.bot.observed_players
        self.assertEqual(saved, (expected,))
        self.assertEqual(self.bot.observed_entities, ())
        with self.assertRaises(FrozenInstanceError):
            saved[0].skin.model = "slim"
        self.assertNotIn("rgba=", repr(saved[0].skin))
        changes = []
        self.bot.subscribe_player_list_updates(changes.append)

        delivered = asyncio.create_task(self.bot.packets.recv_raw())
        await asyncio.sleep(0)
        self.deliver(binding.AddPlayerPacket(uuid=uuid, name="Peer actor", runtime_id=903))
        await delivered
        actor, = self.bot.observed_entities
        self.assertEqual((actor.entity_id, actor.runtime_id), (301, 903))

        delivered = asyncio.create_task(self.bot.packets.recv_raw())
        await asyncio.sleep(0)
        self.deliver(binding.RemoveActorPacket(entity_id=301))
        await delivered
        self.assertEqual(self.bot.observed_entities, ())
        self.assertEqual(self.bot.observed_players, saved)

        await self.deliver_through(binding.AddPlayerPacket(uuid=uuid, name="Peer actor", runtime_id=904),
                                   binding.ChangeDimensionPacket(dimension_id=1), self.roster(entries=[]))
        self.assertEqual(self.bot.observed_entities, ())
        self.assertEqual(self.bot.observed_players, saved)
        self.assertEqual(changes, [])
        await self.deliver_through(self.roster(entries=[self.remove(uuid)]))
        self.assertEqual(changes, [(PlayerListChange("remove", uuid),)])

    async def test_golden_capes_through_roster_and_live_update(self):
        peer = UUID("9e688c58-a548-3b8e-af69-c9e1005ad0bf")
        old_cape = PlayerCapeSnapshot("previous", 1, 2, b"\x10\x20\x30\xff\x40\x50\x60\x00")
        initial = PlayerSkinSnapshot("previous skin", 1, 1, b"\x11\x22\x33\xff", "wide", "", cape=old_cape)
        await self.join(GOLDEN_ROSTER, self.roster(entries=[self.add(peer, "Peer", skin=self.native_skin(initial))]))
        listed, before = self.bot.observed_players
        self.assertEqual(listed.uuid, UUID(int=1))
        self.assertEqual(listed.skin.cape, PlayerCapeSnapshot("cid", 1, 1, b"\x05\x06\x07\x08"))
        self.assertEqual(before.skin.cape, old_cape)

        updates = []
        self.bot.subscribe_player_skin_updates(updates.append, player_uuid=peer)
        waiting = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=1))
        await asyncio.sleep(0)
        self.deliver(GOLDEN_UPDATE)
        current = await waiting
        self.assertEqual(current.cape, PlayerCapeSnapshot("cape", 1, 1, b"\x05\x06\x07\x08"))
        after = replace(before, skin=current)
        self.assertEqual(self.bot.observed_players, (listed, after))
        self.assertEqual(updates, [after])
        self.assertEqual(before.skin, initial)
        with self.assertRaises(FrozenInstanceError):
            current.cape = None
        with self.assertRaises(FrozenInstanceError):
            current.cape.rgba = b""
        self.assertNotIn("rgba=", repr(current.cape))

    async def test_cape_only_reports_survive_roster_and_live_update(self):
        peer = UUID(int=109)
        initial = PlayerSkinSnapshot("", 0, 0, b"", None, "",
                                     cape=PlayerCapeSnapshot("first", 1, 1, b"\x01\x02\x03\xff"))
        latest = replace(initial, cape=PlayerCapeSnapshot("latest", 1, 1, b"\x04\x05\x06\xff"))
        empty = replace(initial, cape=EMPTY_CAPE)
        await self.join(self.roster(entries=[self.add(peer, "Peer", skin=self.native_skin(initial))]))
        saved, = self.bot.observed_players
        self.assertEqual(saved.skin, initial)

        updates = []
        self.bot.subscribe_player_skin_updates(updates.append, player_uuid=peer)
        waiting = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.skin_packet(peer, latest))
        self.assertEqual(await waiting, latest)
        self.assertEqual(self.bot.observed_players, (replace(saved, skin=latest),))

        await self.deliver_through(self.skin_packet(peer, empty))
        self.assertEqual(self.bot.observed_players, (replace(saved, skin=None),))
        self.assertEqual(updates, [replace(saved, skin=latest), replace(saved, skin=None)])
        self.assertEqual(saved.skin, initial)

    async def test_startup_skin_updates_precede_queued_live_updates(self):
        peer, late = UUID(int=111), UUID(int=112)
        initial = PlayerSkinSnapshot("initial", 1, 1, b"\x01\x02\x03\xff", "wide", "", cape=EMPTY_CAPE)
        startup = PlayerSkinSnapshot("startup", 1, 1, b"\x04\x05\x06\x80", "slim", "startup patch", cape=EMPTY_CAPE)
        queued = PlayerSkinSnapshot("queued", 1, 1, b"\x07\x08\x09\x00", None, "queued patch", cape=EMPTY_CAPE)
        await self.join(
            self.roster(entries=[self.add(peer, "Peer", skin=self.native_skin(initial))]),
            self.skin_packet(late, startup),
            self.skin_packet(peer, startup),
            self.roster(entries=[self.add(late, "Late")]),
            queued=(self.roster(entries=[self.add(late, "Live")]), self.skin_packet(peer, queued)),
        )
        saved = self.bot.observed_players
        self.assertEqual(tuple(player.skin for player in saved), (startup, None))
        lists, skins, changes = [], [], []
        self.bot.subscribe_player_list_updates(lists.append)
        self.bot.subscribe_player_skin_updates(skins.append)
        self.bot.subscribe_player_changes(changes.append)
        self.assertEqual((lists, skins, changes), ([], [], []))

        # The reader starts after join publishes the replayed roster. Waiting
        # immediately lets the already queued packet complete this live wait.
        self.assertEqual(await self.bot.wait_for_player_skin_update(peer, timeout=1), queued)
        self.assertEqual(self.bot.observed_players[0].skin, queued)
        self.assertEqual(saved[0].skin, startup)
        self.assertEqual(lists, [(PlayerListChange("add", late, replace(saved[1], name="Live")),)])
        self.assertEqual(skins, [replace(saved[0], skin=queued)])
        self.assertEqual(changes, [lists[0][0].player, skins[0]])

    async def test_live_wait_captures_first_skin_in_batch_and_accepts_repeats_and_empty_reports(self):
        peer, other = UUID(int=120), UUID(int=121)
        first = PlayerSkinSnapshot("first", 1, 1, b"\x01\x13\x17\xff", "slim", "patch one", cape=EMPTY_CAPE)
        latest = PlayerSkinSnapshot("", 1, 2, b"\x04\x05\x06\xff\x03\x02\x01\x00", None, "patch two", cape=EMPTY_CAPE)
        latest_skin = self.native_skin(latest)
        latest_skin.arm_size_type = 250  # The decoder can return an unrecognized integer.
        latest_packet = self.binding.PlayerSkinPacket(uuid=peer, skin=latest_skin)
        await self.join(self.roster(entries=[
            self.add(peer, "Peer", id=131, xuid="4321", platform_online_id="platform", is_host=True, color=-33),
            self.add(other, "Other"),
        ]))
        old = self.bot.observed_players
        all_skins, peer_skins, changes = [], [], []

        def observe_skin(player):
            listed = next(entry for entry in self.bot.observed_players if entry.uuid == player.uuid)
            all_skins.append((player, listed))

        self.bot.subscribe_player_skin_updates(observe_skin)
        self.bot.subscribe_player_skin_updates(peer_skins.append, player_uuid=peer)
        self.bot.subscribe_player_changes(changes.append)
        waiting = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=1))
        waiting_list = asyncio.create_task(self.bot.wait_for_player_list_update(timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.skin_packet(other, latest), self.skin_packet(peer, first), latest_packet)

        observed = await waiting
        self.assertEqual(observed, first)
        self.assertEqual(self.bot.observed_players, (
            PlayerListEntry(peer, "Peer", 131, "4321", None, "platform", 0, False, True, False, -33, latest),
            PlayerListEntry(other, "Other", 0, None, None, None, 0, False, False, False, 0, latest),
        ))
        self.assertIsNone(old[0].skin)
        self.assertFalse(waiting_list.done())

        repeat = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=1))
        await asyncio.sleep(0)
        self.deliver(latest_packet)
        self.assertEqual(await repeat, latest)

        empty = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.binding.PlayerSkinPacket(uuid=peer), self.roster(entries=[self.remove(other)]))
        self.assertIsNone(await empty)
        self.assertEqual(await waiting_list, (PlayerListChange("remove", other),))
        self.assertIsNone(self.bot.observed_players[0].skin)
        self.assertEqual(observed, first)
        other_latest, peer_first, peer_latest, peer_empty = (
            replace(old[1], skin=latest), replace(old[0], skin=first), replace(old[0], skin=latest), old[0],
        )
        expected_skins = (other_latest, peer_first, peer_latest, peer_latest, peer_empty)
        self.assertEqual(all_skins, [(entry, entry) for entry in expected_skins])
        self.assertEqual(peer_skins, [peer_first, peer_latest, peer_latest, peer_empty])
        self.assertEqual(changes, [other_latest, peer_first, peer_latest, peer_empty])
        with self.assertRaises(FrozenInstanceError):
            peer_skins[0].skin = None

    async def test_live_wait_skips_a_skin_packet_that_cannot_be_applied(self):
        peer = UUID(int=125)
        skin = PlayerSkinSnapshot("valid", 1, 1, b"\x01\x02\x03\xff", "wide", "", cape=EMPTY_CAPE)
        await self.join(self.roster(entries=[self.add(peer, "Peer")]))
        loop = asyncio.get_running_loop()
        errors = []
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: errors.append(context))

        waiting = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=1))
        await asyncio.sleep(0)
        valid = self.bot.protocol.encode(self.skin_packet(peer, skin))
        truncated = RawPacket(valid.header, valid.body[:4])
        self.transport.incoming.put_nowait(write_batch((truncated, valid)))

        self.assertEqual(await waiting, skin)
        self.assertEqual(self.bot.observed_players[0].skin, skin)
        error, = errors
        self.assertEqual(error["message"], "packet observer failed")
        self.assertIsInstance(error["exception"], DecodeError)

    async def test_removed_player_skin_updates_are_ignored_until_readd(self):
        peer = UUID(int=130)
        initial = PlayerSkinSnapshot("initial", 1, 1, b"\x90\x80\x70\xff", "wide", "", cape=EMPTY_CAPE)
        offered = PlayerSkinSnapshot("offered", 1, 1, b"\x60\x50\x40\xff", "wide", "", cape=EMPTY_CAPE)
        await self.join(self.roster(entries=[self.add(peer, "Peer", skin=self.native_skin(initial))]))
        saved, = self.bot.observed_players
        skins = []
        self.bot.subscribe_player_skin_updates(skins.append, player_uuid=peer)
        waiting = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=1))
        removed = asyncio.create_task(self.bot.wait_for_player_list_update(action="remove", timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.roster(entries=[self.remove(peer)]), self.skin_packet(peer, offered))
        self.assertEqual(await removed, (PlayerListChange("remove", peer),))
        self.assertFalse(waiting.done())
        self.assertEqual(self.bot.observed_players, ())

        readded = asyncio.create_task(self.bot.wait_for_player_list_update(action="add", timeout=1))
        await asyncio.sleep(0)
        self.deliver(self.roster(entries=[self.add(peer, "Peer again")]))
        self.assertEqual(await readded, (PlayerListChange("add", peer, self.bot.observed_players[0]),))
        self.assertIsNone(self.bot.observed_players[0].skin)
        self.assertFalse(waiting.done())
        self.assertEqual(skins, [])

        self.deliver(self.skin_packet(peer, offered))
        self.assertEqual(await waiting, offered)
        self.assertEqual(self.bot.observed_players[0].skin, offered)
        self.assertEqual(skins, [replace(saved, name="Peer again", skin=offered)])
        self.assertEqual(saved.skin, initial)

    async def test_skin_wait_expires_and_server_disconnect_ends_it(self):
        peer = UUID(int=140)
        await self.join(self.roster(entries=[self.add(peer, "Peer")]))
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_player_skin_update(peer, timeout=0)

        lists, skins = [], []
        self.bot.subscribe_player_list_updates(lists.append)
        self.bot.subscribe_player_skin_updates(skins.append)
        waiting = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=None))
        await self.deliver_through(self.binding.DisconnectPacket(), self.roster(entries=[self.add(peer, "Late")]),
                                   self.binding.PlayerSkinPacket(uuid=peer))
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, timeout=1)
        self.assertEqual((lists, skins), ([], []))
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_player_list_updates(lists.append)
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_player_skin_updates(skins.append)

    async def test_online_updates_are_ordered_and_snapshot_is_independent(self):
        first, second, unknown = UUID(int=1), UUID(int=2), UUID(int=3)
        await self.join(self.roster(entries=[self.add(first, "Before", id=3)]))
        old = self.bot.observed_players
        binding = self.binding
        await self.deliver_through(
            binding.AddPlayerPacket(uuid=first, name="Before", runtime_id=31),
            binding.AddPlayerPacket(uuid=second, name="Second actor", runtime_id=41),
            binding.SetActorLinkPacket(link=binding.ActorLink(a=50, b=3, type=binding.ActorLinkType.PASSENGER)),
        )
        self.assertEqual(tuple(actor.entity_id for actor in self.bot.observed_entities), (3, 0))
        packets, first_changes, removals, first_removals = [], [], [], []
        raws, added_during_removal = [], []
        membership, first_membership = [], []

        def observe_roster(changes):
            actors = tuple((actor.runtime_id, actor.entity_id) for actor in self.bot.observed_entities)
            packets.append((changes, self.bot.observed_players, actors))

        def observe_link(_change):
            self.bot.subscribe_player_list_updates(added_during_removal.append)

        self.bot.subscribe_player_list_updates(observe_roster)
        self.bot.subscribe_player_list_updates(first_changes.append, player_uuid=first)
        self.bot.subscribe_player_list_updates(removals.append, action="remove")
        self.bot.subscribe_player_list_updates(first_removals.append, action="remove", player_uuid=first)
        self.bot.subscribe_player_membership(
            lambda event: membership.append((event, self.bot.observed_players)),
        )
        stop_first = self.bot.subscribe_player_membership(first_membership.append, player_uuid=first)
        self.assertEqual(membership, [])
        self.bot.subscribe_vehicle_links(observe_link, rider_entity_id=3)
        self.bot.packets.subscribe_raw(raws.append)
        all_changes = asyncio.create_task(self.bot.wait_for_player_list_update(timeout=1))
        wait_for_removal = asyncio.create_task(self.bot.wait_for_player_list_update(
            action="remove", player_uuid=first, timeout=1,
        ))
        await asyncio.sleep(0)
        update = self.roster(entries=[
            self.add(first, "Updated", id=3), self.add(second, "Other", id=4),
            self.add(first, "Updated", id=3), self.add(first, "Updated again", id=3),
            self.remove(unknown), self.remove(first),
        ])
        self.deliver(update)
        updated = PlayerListEntry(first, "Updated", 3, None, None, None, 0, False, False, False, 0)
        other = PlayerListEntry(second, "Other", 4, None, None, None, 0, False, False, False, 0)
        expected = (
            PlayerListChange("add", first, updated), PlayerListChange("add", second, other),
            PlayerListChange("add", first, updated),
            PlayerListChange("add", first, replace(updated, name="Updated again")),
            PlayerListChange("remove", unknown), PlayerListChange("remove", first),
        )
        self.assertEqual(await all_changes, expected)
        self.assertEqual(await wait_for_removal, (expected[-1],))
        self.assertEqual(packets, [(expected, (other,), ((41, 4),))])
        self.assertEqual(first_changes, [(expected[0], expected[2], expected[3], expected[5])])
        self.assertEqual(removals, [expected[-2:]])
        self.assertEqual(first_removals, [(expected[-1],)])
        added = PlayerMembershipEvent("joined", other)
        left = PlayerMembershipEvent("left", expected[3].player)
        self.assertEqual(membership, [(added, (updated, other)), (left, (other,))])
        self.assertIs(membership[0][0].player, membership[0][1][1])
        self.assertEqual(first_membership, [left])
        self.assertIsNone(packets[0][0][-1].player)
        self.assertEqual(raws, [self.bot.protocol.encode(update)])
        self.assertEqual(self.bot.observed_players, (other,))
        self.assertEqual(old[0].name, "Before")
        with self.assertRaises(FrozenInstanceError):
            packets[0][0][0].action = "remove"
        with self.assertRaises(FrozenInstanceError):
            membership[0][0].kind = "left"
        with self.assertRaises(FrozenInstanceError):
            membership[1][0].player.name = "after"

        await self.deliver_through(self.roster(entries=[self.add(first, "Returned", id=3)]))
        returned = PlayerListChange("add", first, replace(updated, name="Returned"))
        self.assertEqual(packets[-1], ((returned,), (other, returned.player), ((41, 4),)))
        self.assertEqual(first_changes[-1], (returned,))
        self.assertEqual(removals, [expected[-2:]])
        self.assertEqual(first_removals, [(expected[-1],)])
        self.assertEqual(added_during_removal, [(returned,)])
        self.assertEqual(membership[-1], (PlayerMembershipEvent("joined", returned.player), (other, returned.player)))
        self.assertEqual(first_membership, [left, membership[-1][0]])
        stop_first()
        stop_first()
        await self.deliver_through(self.roster(entries=[self.remove(first)]))
        self.assertEqual(membership[-1], (PlayerMembershipEvent("left", returned.player), (other,)))
        self.assertEqual(len(first_membership), 2)

    async def test_wait_covers_future_updates_and_close_ends_session(self):
        uuid = UUID(int=5)
        await self.join(self.roster(entries=[self.add(uuid, "First session")]))
        saved = self.bot.observed_players
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_player_list_update(timeout=0.05)

        old_lists, old_skins, old_changes, old_membership = [], [], [], []
        stop_list = self.bot.subscribe_player_list_updates(old_lists.append)
        stop_skin = self.bot.subscribe_player_skin_updates(old_skins.append)
        stop_changes = self.bot.subscribe_player_changes(old_changes.append, uuid=uuid)
        stop_membership = self.bot.subscribe_player_membership(old_membership.append, player_uuid=uuid)
        waiting = asyncio.create_task(self.bot.wait_for_player_list_update(timeout=15))
        waiting_skin = asyncio.create_task(self.bot.wait_for_player_skin_update(uuid, timeout=15))
        await asyncio.sleep(0)
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, timeout=1)
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting_skin, timeout=1)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.observed_players
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.subscribe_player_membership(old_membership.append)

        await self.join()
        self.assertEqual(self.bot.observed_players, ())
        self.assertEqual(saved[0].name, "First session")
        new_lists, new_skins, new_changes, new_membership = [], [], [], []
        self.bot.subscribe_player_list_updates(new_lists.append)
        self.bot.subscribe_player_skin_updates(new_skins.append)
        self.bot.subscribe_player_changes(new_changes.append, uuid=uuid)
        self.bot.subscribe_player_membership(new_membership.append, player_uuid=uuid)
        rejoined = self.roster(entries=[self.add(uuid, "Second session")])
        skin = self.binding.PlayerSkinPacket(uuid=uuid)
        await self.deliver_through(rejoined, skin)
        for stop in (stop_list, stop_skin, stop_changes, stop_membership):
            stop()
            stop()
        await self.deliver_through(rejoined, skin)
        new_player = replace(saved[0], name="Second session")
        self.assertEqual(new_lists, [(PlayerListChange("add", uuid, new_player),)] * 2)
        self.assertEqual(new_skins, [new_player] * 2)
        self.assertEqual(new_membership, [PlayerMembershipEvent("joined", new_player)])
        self.assertEqual(old_membership, [])
        self.assertEqual((old_lists, old_skins), ([], []))
        self.assertEqual(new_changes, [])
        await self.deliver_through(self.roster(entries=[self.add(uuid, "Changed")]))
        self.assertEqual(new_changes, [replace(new_player, name="Changed")])
        self.assertEqual(old_changes, [])

    async def test_subscription_changes_take_effect_on_next_packet_and_failures_are_isolated(self):
        peer = UUID(int=145)
        await self.join(self.roster(entries=[self.add(peer, "Peer")]))
        saved, = self.bot.observed_players
        skin = PlayerSkinSnapshot("changed", 1, 1, b"\x00\x00\x00\xff", "wide", "", cape=EMPTY_CAPE)

        def register(subscribe):
            duplicates, added, kept = [], [], []

            def change_registrations(_event):
                stop_changing()
                stop_duplicate()
                stop_duplicate()
                subscribe(added.append)
                raise ValueError("observer failed")

            def cancel(_event):
                raise asyncio.CancelledError("observer cancelled")

            stop_changing = subscribe(change_registrations)
            duplicate = duplicates.append
            stop_duplicate = subscribe(duplicate)
            subscribe(duplicate)
            subscribe(cancel)
            subscribe(kept.append)
            return duplicates, added, kept

        lists = register(self.bot.subscribe_player_list_updates)
        skins = register(self.bot.subscribe_player_skin_updates)
        changes = register(self.bot.subscribe_player_changes)
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            await self.deliver_through(
                self.roster(entries=[self.add(peer, "First")]), self.roster(entries=[self.add(peer, "Second")]),
                self.skin_packet(peer, skin), self.binding.PlayerSkinPacket(uuid=peer),
            )

        first_list = (PlayerListChange("add", peer, replace(saved, name="First")),)
        second_player = replace(saved, name="Second")
        second_list = (PlayerListChange("add", peer, second_player),)
        first_skin = replace(second_player, skin=skin)
        self.assertEqual(lists, ([first_list, first_list, second_list], [second_list], [first_list, second_list]))
        self.assertEqual(skins, ([first_skin, first_skin, second_player], [second_player], [first_skin, second_player]))
        first_player = first_list[0].player
        self.assertEqual(changes, ([first_player, first_player, second_player, first_skin, second_player],
                                  [second_player, first_skin, second_player],
                                  [first_player, second_player, first_skin, second_player]))
        self.assertEqual([type(call.args[0]["exception"]) for call in report.call_args_list],
                         [ValueError, asyncio.CancelledError, ValueError, asyncio.CancelledError,
                          asyncio.CancelledError, asyncio.CancelledError,
                          ValueError, asyncio.CancelledError, asyncio.CancelledError,
                          asyncio.CancelledError, asyncio.CancelledError])

    async def test_membership_reentrant_subscription_changes_apply_on_the_next_packet(self):
        first, second = UUID(int=110), UUID(int=111)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.subscribe_player_membership(self.fail)
        await self.join()
        earlier, later, retained = [], [], []

        def replace_registration(event):
            earlier.append(event)
            if len(earlier) == 1:
                unsubscribe()
                self.bot.subscribe_player_membership(later.append)
                raise ValueError("membership callback failed")

        unsubscribe = self.bot.subscribe_player_membership(replace_registration)
        self.bot.subscribe_player_membership(retained.append)
        with patch.object(asyncio.get_running_loop(), "call_exception_handler") as report:
            await self.deliver_through(self.roster(entries=[self.add(first, "First"), self.add(second, "Second")]),
                                       self.roster(entries=[self.remove(first)]))
        self.assertEqual([(event.kind, event.player.uuid) for event in retained],
                         [("joined", first), ("joined", second), ("left", first)])
        self.assertEqual(earlier, retained[:2])
        self.assertEqual(later, retained[2:])
        self.assertEqual(len(report.call_args_list), 1)
        self.assertIsInstance(report.call_args.args[0]["exception"], ValueError)

    async def test_membership_join_applies_visible_identity_before_each_callback(self):
        b = self.binding
        peer, zero, removed, unseen = (UUID(int=index) for index in (110, 111, 112, 113))
        runtimes = {peer: 201, zero: 202, removed: 203, unseen: 204}
        await self.join(*(self.player_spawn(uuid, runtimes[uuid], 0, b.GameType.DEFAULT)
                          for uuid in (peer, zero, removed)))
        await self.deliver_through(
            b.MobArmorEquipmentPacket(
                runtime_id=201, head=b.SerializedNetworkItemStackDescriptor(id=6, stack_size=1),
            ),
            b.RemoveActorPacket(entity_id=303),
        )
        self.assertEqual([(actor.runtime_id, actor.entity_id) for actor in self.bot.observed_entities],
                         [(201, 0), (202, 0), (203, 0)])
        self.assertIsNone(self.bot.observed_player_equipment(201).entity_id)
        self.assertEqual(self.bot.observed_players, ())
        memberships, packet_views = [], []

        def observe(event):
            runtime_id = runtimes[event.player.uuid]
            actor = next((actor for actor in self.bot.observed_entities if actor.runtime_id == runtime_id), None)
            equipment = self.bot.observed_player_equipment(runtime_id)
            memberships.append((event, tuple(player.uuid for player in self.bot.observed_players), actor, equipment))

        self.bot.subscribe_player_membership(observe)
        self.bot.subscribe_player_list_updates(lambda _: packet_views.append(self.bot.observed_entities))
        await self.deliver_through(self.roster(entries=[
            self.add(peer, "Visible", id=301), self.add(zero, "Without identity"),
            self.add(removed, "Previously removed", id=303), self.add(unseen, "Unseen", id=304),
        ]))

        peers = tuple(runtimes)
        self.assertEqual([(event.kind, event.player.uuid, event.player.entity_id, roster)
                          for event, roster, _, _ in memberships],
                         [("joined", uuid, entity_id, peers[:index])
                          for index, (uuid, entity_id) in enumerate(zip(peers, (301, 0, 303, 304)), start=1)])
        _, _, actor, equipment = memberships[0]
        self.assertEqual((actor.entity_id, equipment.entity_id, equipment.head.item_id), (301, 301, 6))
        self.assertEqual(memberships[1][2].entity_id, 0)
        self.assertIsNone(memberships[1][3].entity_id)
        self.assertEqual([item[2:] for item in memberships[2:]], [(None, None), (None, None)])
        self.assertEqual([[(actor.runtime_id, actor.entity_id) for actor in actors] for actors in packet_views],
                         [[(201, 301), (202, 0)]])

    async def test_reader_failure_releases_subscription_callbacks(self):
        await self.join()

        def observe(_event):
            self.fail("unexpected subscription callback")

        callback = weakref.ref(observe)
        self.bot.subscribe_player_list_updates(observe)
        self.bot.subscribe_player_skin_updates(observe)
        self.bot.subscribe_player_changes(observe)
        self.bot.subscribe_player_membership(observe)
        del observe
        self.transport.incoming.put_nowait(b"\x02\x00")
        self.assertEqual((await self.bot.wait_for_session_end(timeout=1)).kind, "reader_end")
        self.assertIsNone(callback())
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_player_membership(self.fail)
        with self.assertRaisesRegex(ValueError, "truncated packet in batch"):
            await self.bot.close()


class SkinProjectionTests(unittest.TestCase):
    def test_historical_golden_exposes_flat_cape_fields(self):
        observed = _player_skin_snapshot(Protocol(1001).decode(GOLDEN_HISTORICAL_UPDATE).skin)
        self.assertEqual(observed, PlayerSkinSnapshot(
            "sid", 1, 1, b"\x01\x02\x03\x04", "wide", '{"geometry":{"default":"geometry.a"}}\n',
            cape=PlayerCapeSnapshot("cid", 1, 1, b"\x05\x06\x07\x08"),
        ))

    def test_missing_fields_and_explicit_empty_cape_are_distinct(self):
        rgba = bytes((1, 2, 3, 4))
        skin = SimpleNamespace(id="base", skin_image=SimpleNamespace(width=1, height=1, image_bytes=rgba),
                               arm_size_type=None, resource_patch="")
        self.assertEqual(_player_skin_snapshot(skin), PlayerSkinSnapshot("base", 1, 1, rgba, None, ""))
        self.assertIsNone(_player_skin_snapshot(skin).cape)
        skin.cape_id = ""
        self.assertIsNone(_player_skin_snapshot(skin).cape)
        skin.cape_image = SimpleNamespace(width=0, height=0)
        self.assertIsNone(_player_skin_snapshot(skin).cape)
        skin.cape_image.image_bytes = b""
        self.assertEqual(_player_skin_snapshot(skin).cape, EMPTY_CAPE)

        cape_rgba = bytes((5, 6, 7, 8))
        skin.cape_id = "cape"
        skin.cape_image = SimpleNamespace(width=1, height=1, image_bytes=cape_rgba)
        self.assertIs(_player_skin_snapshot(skin).cape.rgba, cape_rgba)
        with self.assertRaisesRegex(TypeError, "immutable bytes"):
            PlayerCapeSnapshot("cape", 1, 1, bytearray(cape_rgba))
