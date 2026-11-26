import asyncio
import unittest
from unittest.mock import patch
from uuid import UUID

from endpilot import Bot, PlayerListChange, PlayerListEntry
from endpilot.protocol import write_batch


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

    def deliver(self, *packets):
        self.transport.incoming.put_nowait(write_batch(self.bot.protocol.encode(packet) for packet in packets))

    async def join(self, *before_spawn):
        self.transport = BatchTransport()
        binding = self.binding
        self.deliver(binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE))
        self.deliver(binding.StartGamePacket(entity_id=17, runtime_id=72), *before_spawn,
                     binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19142")

    async def test_login_roster_survives_actor_removal_and_dimension_change(self):
        uuid = UUID(int=110)
        binding = self.binding
        await self.join(
            self.roster(entries=[self.add(uuid, "Peer", id=301, xuid="1234", play_fab_id="abcdef",
                                          platform_online_id="platform", build_platform=binding.BuildPlatform.WIN32,
                                          is_host=True, is_teacher=True, is_sub_client=True, color=-123)]),
        )
        expected = PlayerListEntry(uuid, "Peer", 301, "1234", "abcdef", "platform", 8, True, True, True, -123)
        saved = self.bot.observed_players
        self.assertEqual(saved, (expected,))
        self.assertEqual(self.bot.observed_entities, ())

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
        await asyncio.sleep(0)
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiting, timeout=1)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.observed_players

        await self.join()
        self.assertEqual(self.bot.observed_players, ())
        self.assertEqual(saved[0].name, "First session")
