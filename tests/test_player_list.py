import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from uuid import UUID

from bedrock_protocol import DecodeError

from endpilot import Bot, PlayerListChange, PlayerListEntry, PlayerSkinSnapshot
from endpilot.protocol import Protocol, RawPacket, write_batch
from endpilot.skin import _player_skin_snapshot


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
        return binding.SerializedSkinRef(
            id=snapshot.skin_id, resource_patch=snapshot.resource_patch,
            skin_image=binding.SkinImage(width=snapshot.width, height=snapshot.height, image_bytes=snapshot.rgba),
            arm_size_type=binding.ArmSizeType[model],
        )

    def skin_packet(self, uuid, snapshot):
        return self.binding.PlayerSkinPacket(uuid=uuid, skin=self.native_skin(snapshot))

    def deliver(self, *packets):
        self.transport.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))

    async def join(self, *before_spawn, queued=()):
        self.transport = BatchTransport()
        binding = self.binding
        self.deliver(binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE))
        self.deliver(binding.StartGamePacket(entity_id=17, runtime_id=72), *before_spawn,
                     binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN), *queued)
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19142")

    async def test_login_roster_survives_actor_removal_and_dimension_change(self):
        uuid = UUID(int=110)
        binding = self.binding
        skin = PlayerSkinSnapshot("initial", 2, 1, b"\x07\x0d\x17\xff\x03\x02\x01\x00", "wide", "patch")
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

        delivered = asyncio.create_task(self.bot.packets.recv_raw())
        await asyncio.sleep(0)
        self.deliver(binding.AddPlayerPacket(uuid=uuid, name="Peer actor", runtime_id=904),
                     binding.ChangeDimensionPacket(dimension_id=1),
                     self.roster(entries=[]))
        await delivered
        self.assertEqual(self.bot.observed_entities, ())
        self.assertEqual(self.bot.observed_players, saved)

    async def test_startup_skin_updates_precede_queued_live_updates(self):
        peer, late = UUID(int=111), UUID(int=112)
        initial = PlayerSkinSnapshot("initial", 1, 1, b"\x01\x02\x03\xff", "wide", "")
        startup = PlayerSkinSnapshot("startup", 1, 1, b"\x04\x05\x06\x80", "slim", "startup patch")
        queued = PlayerSkinSnapshot("queued", 1, 1, b"\x07\x08\x09\x00", None, "queued patch")
        await self.join(
            self.roster(entries=[self.add(peer, "Peer", skin=self.native_skin(initial))]),
            self.skin_packet(late, startup),
            self.skin_packet(peer, startup),
            self.roster(entries=[self.add(late, "Late")]),
            queued=(self.skin_packet(peer, queued),),
        )
        saved = self.bot.observed_players
        self.assertEqual(tuple(player.skin for player in saved), (startup, None))

        # The reader starts after join publishes the replayed roster. Waiting
        # immediately lets the already queued packet complete this live wait.
        self.assertEqual(await self.bot.wait_for_player_skin_update(peer, timeout=1), queued)
        self.assertEqual(self.bot.observed_players[0].skin, queued)
        self.assertEqual(saved[0].skin, startup)

    async def test_live_wait_captures_first_skin_in_batch_and_accepts_repeats_and_empty_reports(self):
        peer, other = UUID(int=120), UUID(int=121)
        first = PlayerSkinSnapshot("first", 1, 1, b"\x01\x13\x17\xff", "slim", "patch one")
        latest = PlayerSkinSnapshot("", 1, 2, b"\x04\x05\x06\xff\x03\x02\x01\x00", None, "patch two")
        latest_skin = self.native_skin(latest)
        latest_skin.arm_size_type = 250  # The decoder can return an unrecognized integer.
        latest_packet = self.binding.PlayerSkinPacket(uuid=peer, skin=latest_skin)
        await self.join(self.roster(entries=[
            self.add(peer, "Peer", id=131, xuid="4321", platform_online_id="platform", is_host=True, color=-33),
            self.add(other, "Other"),
        ]))
        old = self.bot.observed_players
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

    async def test_live_wait_skips_a_skin_packet_that_cannot_be_applied(self):
        peer = UUID(int=125)
        skin = PlayerSkinSnapshot("valid", 1, 1, b"\x01\x02\x03\xff", "wide", "")
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
        initial = PlayerSkinSnapshot("initial", 1, 1, b"\x90\x80\x70\xff", "wide", "")
        offered = PlayerSkinSnapshot("offered", 1, 1, b"\x60\x50\x40\xff", "wide", "")
        await self.join(self.roster(entries=[self.add(peer, "Peer", skin=self.native_skin(initial))]))
        saved, = self.bot.observed_players
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

        self.deliver(self.skin_packet(peer, offered))
        self.assertEqual(await waiting, offered)
        self.assertEqual(self.bot.observed_players[0].skin, offered)
        self.assertEqual(saved.skin, initial)

    async def test_skin_wait_expires_and_server_disconnect_ends_it(self):
        peer = UUID(int=140)
        await self.join(self.roster(entries=[self.add(peer, "Peer")]))
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_player_skin_update(peer, timeout=0)

        waiting = asyncio.create_task(self.bot.wait_for_player_skin_update(peer, timeout=None))
        await asyncio.sleep(0)
        self.deliver(self.binding.DisconnectPacket())
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, timeout=1)

    async def test_online_updates_are_ordered_and_snapshot_is_independent(self):
        first, second, unknown = UUID(int=1), UUID(int=2), UUID(int=3)
        await self.join(self.roster(entries=[self.add(first, "Before", id=3)]))
        old = self.bot.observed_players
        all_changes = asyncio.create_task(self.bot.wait_for_player_list_update(timeout=1))
        removals = asyncio.create_task(self.bot.wait_for_player_list_update(
            action="remove", player_uuid=first, timeout=1,
        ))
        await asyncio.sleep(0)
        self.deliver(
            self.roster(entries=[self.add(first, "Updated", id=3), self.add(second, "Other", id=4),
                                 self.remove(unknown), self.remove(first)]),
        )
        updated = PlayerListEntry(first, "Updated", 3, None, None, None, 0, False, False, False, 0)
        other = PlayerListEntry(second, "Other", 4, None, None, None, 0, False, False, False, 0)
        self.assertEqual(await all_changes, (
            PlayerListChange("add", first, updated), PlayerListChange("add", second, other),
            PlayerListChange("remove", unknown), PlayerListChange("remove", first),
        ))
        self.assertEqual(await removals, (PlayerListChange("remove", first),))
        self.assertEqual(self.bot.observed_players, (other,))
        self.assertEqual(old[0].name, "Before")

    async def test_wait_covers_future_updates_and_close_ends_session(self):
        uuid = UUID(int=5)
        await self.join(self.roster(entries=[self.add(uuid, "First session")]))
        saved = self.bot.observed_players
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_player_list_update(timeout=0.05)

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

        await self.join()
        self.assertEqual(self.bot.observed_players, ())
        self.assertEqual(saved[0].name, "First session")


class HistoricalSkinTests(unittest.TestCase):
    def test_native_codec_exposes_flat_skin_fields(self):
        protocol = Protocol(1001)
        binding = protocol.binding
        rgba = b"\x01\x02\x03\x04"
        skin = binding.SerializedSkinRef(
            id="historical", image_width=1, image_height=1, image_bytes=rgba,
            arm_size=binding.ArmSizeType.WIDE, resource_patch="historical patch",
        )
        raw = protocol.encode(binding.PlayerSkinPacket(uuid=UUID(int=150), skin=skin))
        observed = _player_skin_snapshot(protocol.decode(raw).skin)
        self.assertEqual(observed, PlayerSkinSnapshot("historical", 1, 1, rgba, "wide", "historical patch"))
