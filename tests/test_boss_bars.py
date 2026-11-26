import asyncio
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from bedrock_protocol import DecodeError

from endpilot import BossBar, BossBarEvent, Bot
from endpilot.protocol import RawPacket, write_batch


class BatchTransport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.incoming = asyncio.Queue()
        self.closed = False

    def deliver(self, *packets, compressed_connection=True):
        batch = write_batch(
            packet if isinstance(packet, RawPacket) else self.protocol.encode(packet) for packet in packets
        )
        self.incoming.put_nowait(b"\xff" + batch if compressed_connection else batch)

    async def recv(self):
        return await self.incoming.get()

    async def send(self, data):
        pass

    async def close(self):
        self.closed = True


class BossBarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("EndPilotBossBar", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.binding = self.bot.protocol.binding
        self.events = self.binding.BossEventUpdateType

    def boss_packet(self, boss_id, action, **fields):
        return self.binding.BossEventPacket(boss_id=boss_id, event_type=action, **fields)

    async def join(self, *early, post_spawn=()):
        binding = self.binding
        self.transport = BatchTransport(self.bot.protocol)
        self.transport.deliver(
            binding.NetworkSettingsPacket(
                compression_algorithm=binding.PacketCompressionAlgorithm.ZLIB,
                compression_threshold=1,
            ),
            compressed_connection=False,
        )
        self.transport.deliver(
            binding.StartGamePacket(runtime_id=42, entity_id=7),
            *early,
            binding.PlayStatusPacket(status=3),
            *post_spawn,
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    async def observe(self, *packets, kind=None, boss_id=None):
        wait = asyncio.create_task(self.bot.wait_for_boss_bar(kind=kind, boss_id=boss_id, timeout=1))
        await asyncio.sleep(0)
        self.transport.deliver(*packets)
        return await wait

    async def test_early_packets_and_reconnect(self):
        boss_id = -203
        await self.join(
            self.boss_packet(boss_id, self.events.ADD, name="Initial", health_percent=0.75, color=3, overlay=4),
            self.boss_packet(boss_id, self.events.UPDATE_NAME, name="Before spawn", filtered_name="Filtered"),
            post_spawn=(self.boss_packet(boss_id, self.events.UPDATE_PERCENT, health_percent=0.25),),
        )

        snapshot = self.bot.boss_bars
        self.assertEqual(snapshot, (BossBar(boss_id, "Before spawn", 0.75, 3, 4, "Filtered"),))
        with self.assertRaises(FrozenInstanceError):
            snapshot[0].title = "Local edit"
        event = await self.bot.wait_for_boss_bar(kind="updated", boss_id=boss_id, timeout=1)
        self.assertEqual(event, BossBarEvent("updated", BossBar(boss_id, "Before spawn", 0.25, 3, 4, "Filtered")))

        pending = asyncio.create_task(self.bot.wait_for_boss_bar())
        await asyncio.sleep(0)
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await pending
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.boss_bars

        await self.join()
        self.assertEqual(self.bot.boss_bars, ())
        self.assertEqual(snapshot[0].progress, 0.75)
        self.assertEqual(event.bar.progress, 0.25)

    async def test_two_ids_follow_independent_lifecycles(self):
        await self.join()
        first_id, second_id = 1 << 40, -304
        initial = BossBar(first_id, "§cFirst", 0.5, 2, 1, "First")
        second = BossBar(second_id, "Second", 1.0, 3, 2)
        first_event = await self.observe(
            self.boss_packet(second_id, self.events.ADD, name="Second", health_percent=1.0, color=3, overlay=2),
            self.boss_packet(
                first_id, self.events.ADD, name="§cFirst", filtered_name="First", health_percent=0.5, color=2, overlay=1,
            ),
            kind="created", boss_id=first_id,
        )
        self.assertEqual(first_event, BossBarEvent("created", initial))
        self.assertEqual(self.bot.boss_bars, (second, initial))

        renamed = await self.observe(
            self.boss_packet(first_id, self.events.UPDATE_PROPERTIES, color=0, overlay=0),
            self.boss_packet(first_id, self.events.UPDATE_NAME, name="Renamed"),
            self.boss_packet(first_id, self.events.UPDATE_PERCENT, health_percent=0.0),
            self.boss_packet(first_id, self.events.UPDATE_STYLE, color=7, overlay=4),
            kind="updated", boss_id=first_id,
        )
        self.assertEqual(renamed, BossBarEvent("updated", BossBar(first_id, "Renamed", 0.5, 2, 1)))
        updated = BossBar(first_id, "Renamed", 0.0, 7, 4)
        self.assertEqual(self.bot.boss_bars, (second, updated))
        self.assertEqual(first_event.bar, initial)

        removed = await self.observe(
            self.boss_packet(second_id, self.events.REMOVE), kind="removed", boss_id=second_id,
        )
        self.assertEqual(removed, BossBarEvent("removed", second))
        self.assertEqual(self.bot.boss_bars, (updated,))

        refreshed = await self.observe(
            self.boss_packet(second_id, self.events.UPDATE_PERCENT, health_percent=0.75),
            self.boss_packet(second_id, self.events.REMOVE),
            self.boss_packet(first_id, self.events.ADD, name="Refresh", health_percent=0.25, color=255, overlay=5),
        )
        self.assertEqual(refreshed, BossBarEvent("updated", BossBar(first_id, "Refresh", 0.25, 255, 5)))

        last = await self.observe(self.boss_packet(first_id, self.events.REMOVE), kind="removed")
        self.assertEqual(last, BossBarEvent("removed", refreshed.bar))
        self.assertEqual(self.bot.boss_bars, ())

    async def test_invalid_early_packet_closes_transport(self):
        raw = RawPacket(self.binding.BossEventPacket.ID, b"")
        with self.assertRaises(DecodeError):
            await self.join(raw)
        self.assertTrue(self.transport.closed)
        with self.assertRaisesRegex(RuntimeError, "player has not joined"):
            self.bot.boss_bars
