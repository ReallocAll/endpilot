import asyncio
from dataclasses import FrozenInstanceError
import gc
import unittest
from unittest.mock import patch
from weakref import ref

from endpilot import Bot, FurnaceProgress
from endpilot.containers import Containers, ContainerWindow
from endpilot.protocol import Protocol, write_batch

from test_blocks import PacketStream
from test_scheduled_input import ManualClock, Wire


class FurnaceUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(ManualClock().patch_scheduler())
        self.bot = Bot("FurnaceUpdates", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            b.StartGamePacket(runtime_id=7, entity_id=17),
            b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN),
        ):
            self.wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    def open_packet(self, kind="FURNACE"):
        b = self.b
        return b.ContainerOpenPacket(container_id=2, type=b.ContainerType[kind],
                                     pos=b.BlockPos(x=0, y=220, z=0), entity_unique_id=-1)

    def close_packet(self):
        b = self.b
        return b.ContainerClosePacket(container_id=2, container_type=b.ContainerType.FURNACE,
                                      server_initiated_close=False)

    def data(self, field, value, *, window_id=2):
        return self.b.ContainerSetDataPacket(container_id=window_id, id=field, value=value)

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        received = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        payload = write_batch(self.bot.protocol.encode(packet) for packet in (*packets, marker))
        self.wire.incoming.put_nowait(payload)
        await received

    async def test_each_supported_packet_including_repeats_matches_existing_wait(self):
        subscribe = self.bot.subscribe_furnace_progress
        with self.assertRaisesRegex(RuntimeError, "no furnace window"):
            subscribe(lambda _: None)
        sent = list(self.wire.sent)
        for kind in ("FURNACE", "SMOKER", "BLAST_FURNACE"):
            with self.subTest(kind=kind):
                await self.deliver(self.open_packet(kind), self.data(2, 800))
                window = self.bot.open_window
                seen = []
                unsubscribe = subscribe(lambda progress: seen.append((progress, self.bot.furnace_progress)))
                self.assertFalse(seen)
                waiting = asyncio.create_task(self.bot.wait_for_furnace_progress(timeout=1))
                await asyncio.sleep(0)
                await self.deliver(self.data(0, 7, window_id=3), self.data(3, 100))
                self.assertFalse(waiting.done())
                self.assertFalse(seen)
                await self.deliver(self.data(0, 0), self.data(0, 0), self.data(1, 731), self.data(0, 1))
                expected = [
                    FurnaceProgress(window, 0, None, 800), FurnaceProgress(window, 0, None, 800),
                    FurnaceProgress(window, 0, 731, 800), FurnaceProgress(window, 1, 731, 800),
                ]
                self.assertEqual(await waiting, expected[0])
                self.assertEqual(seen, [(snapshot, snapshot) for snapshot in expected])
                self.assertIsNot(seen[0][0], seen[1][0])
                with self.assertRaises(FrozenInstanceError):
                    seen[0][0].cook_ticks = 1
                unsubscribe()
                unsubscribe()
                await self.deliver(self.data(1, 730))
                self.assertEqual(len(seen), 4)
                self.assertEqual(self.bot.furnace_progress, FurnaceProgress(window, 1, 730, 800))
        self.assertEqual(self.wire.sent, sent)

    async def test_window_lifecycle_does_not_reuse_subscriptions_or_confuse_brewing_progress(self):
        b = self.b
        subscribe = self.bot.subscribe_furnace_progress
        await self.deliver(self.open_packet())
        seen = []
        callback = seen.append
        previous_unsubscribe = subscribe(callback)
        original = self.bot.open_window

        await self.deliver(self.close_packet(), self.data(0, 5), self.open_packet())
        self.assertEqual(self.bot.open_window, original)
        self.assertIsNot(self.bot.open_window, original)
        unsubscribed_later = subscribe(callback)
        previous_unsubscribe()
        previous_unsubscribe()
        await self.deliver(self.data(0, 12))
        self.assertEqual(seen, [FurnaceProgress(self.bot.open_window, 12)])

        await self.deliver(self.open_packet("BREWING_STAND"), self.data(0, 400))
        self.assertEqual(self.bot.brewing_progress.brew_ticks, 400)
        with self.assertRaisesRegex(RuntimeError, "no furnace window"):
            subscribe(callback)
        await self.deliver(self.open_packet("SMOKER"), self.data(0, 20))
        unsubscribed_later()
        self.assertEqual(len(seen), 1)

        subscribe(callback)
        await self.deliver(b.ChangeDimensionPacket(dimension_id=1), self.data(0, 21),
                           self.open_packet("BLAST_FURNACE"), self.data(0, 22))
        self.assertEqual(len(seen), 1)
        subscribe(callback)
        current_window = self.bot.open_window
        await self.deliver(self.data(0, 23), b.DisconnectPacket(), self.data(0, 24))
        self.assertEqual(seen[1:], [FurnaceProgress(current_window, 23)])
        self.assertIsNone(self.bot.furnace_progress)

    async def test_callback_mutations_and_failure_do_not_change_the_current_notification_group(self):
        await self.deliver(self.open_packet())
        subscribe = self.bot.subscribe_furnace_progress
        loop = asyncio.get_running_loop()
        errors, first_seen, removed_seen, added_seen, final_seen = [], [], [], [], []
        original = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: errors.append(context))
        self.addCleanup(loop.set_exception_handler, original)

        def first(progress):
            first_seen.append(progress)
            remove_itself()
            remove_next()
            subscribe(added_seen.append)
            raise asyncio.CancelledError("furnace callback cancelled")

        remove_itself = subscribe(first)
        remove_next = subscribe(removed_seen.append)
        subscribe(final_seen.append)
        window = self.bot.open_window
        await self.deliver(self.data(0, 1), self.data(0, 2))
        self.assertEqual(first_seen, [FurnaceProgress(window, 1)])
        self.assertFalse(removed_seen)
        self.assertEqual(added_seen, [FurnaceProgress(window, 2)])
        self.assertEqual(final_seen, [FurnaceProgress(window, 1), FurnaceProgress(window, 2)])
        self.assertEqual(len(errors), 1)
        self.assertIs(errors[0]["callback"], first)
        self.assertIsInstance(errors[0]["exception"], asyncio.CancelledError)

    async def test_reader_failure_ends_observation_and_retains_the_last_snapshot(self):
        await self.deliver(self.open_packet())
        seen = []
        unsubscribe = self.bot.subscribe_furnace_progress(seen.append)
        window = self.bot.open_window
        await self.deliver(self.data(1, 500))
        ended = asyncio.Event()
        self.bot.packets.subscribe_reader_end(lambda _: ended.set())
        self.wire.incoming.put_nowait(ConnectionError("test reader ended"))
        await asyncio.wait_for(ended.wait(), 1)
        self.assertIsNone(self.bot.furnace_progress)
        self.assertEqual(seen, [FurnaceProgress(window, burn_remaining_ticks=500)])
        unsubscribe()
        unsubscribe()
        with self.assertRaisesRegex(ConnectionError, "test reader ended"):
            await self.bot.close()

    async def test_saved_unsubscribe_handle_does_not_keep_closed_modules_alive(self):
        await self.deliver(self.open_packet())
        containers = ref(self.bot._containers)
        blocks = ref(self.bot._blocks)
        unsubscribe = self.bot.subscribe_furnace_progress(lambda _: None)

        await self.bot.close()
        # Let asyncio's reader wake-up release the cancellation traceback.
        await asyncio.sleep(0)
        gc.collect()
        self.assertIsNone(containers())
        self.assertIsNone(blocks())
        unsubscribe()
        unsubscribe()


class FurnaceHandleLifetimeTests(unittest.TestCase):
    def test_inactive_handles_do_not_retain_unrelated_observers(self):
        for boundary in ("unsubscribe", "reopen"):
            with self.subTest(boundary=boundary):
                containers = Containers(PacketStream(Protocol(2225)), None, None)
                containers._set_window(ContainerWindow(2, "FURNACE", (0, 220, 0), -1))
                unsubscribe = containers.subscribe_furnace_progress(lambda _: None)
                if boundary == "unsubscribe":
                    unsubscribe()
                else:
                    containers._set_window(ContainerWindow(2, "FURNACE", (0, 220, 0), -1))
                # A live observer can refer back to its container.
                containers.subscribe_furnace_progress(lambda _, owner=containers: None)
                owner_ref = ref(containers)
                del containers
                gc.collect()
                self.assertIsNone(owner_ref())
                unsubscribe()
