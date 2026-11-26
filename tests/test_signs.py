import asyncio
from dataclasses import FrozenInstanceError, replace
from sys import exception
from time import sleep
import unittest
from unittest.mock import patch

from bedrock_protocol import nbt

from endpilot import Bot, SignEditor, SignObservation
from endpilot.compression import PacketConnection
from endpilot.login import JoinSession
from endpilot.protocol import read_batch, write_batch


POSITION = (8, 20, 9)


class Transport:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()

    async def recv(self):
        return await self.incoming.get()

    async def send(self, frame):
        self.sent.put_nowait(frame)

    async def close(self):
        pass


class SignTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("PilotSign", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding
        self.pos = self.b.BlockPos(x=8, y=20, z=9)
        await self.join()

    async def join(self):
        self.transport = Transport()
        settings = self.b.NetworkSettingsPacket(
            compression_algorithm=self.b.PacketCompressionAlgorithm.ZLIB, compression_threshold=0,
        )
        packets = PacketConnection(self.transport, self.bot.protocol, settings)
        session = JoinSession(packets, self.bot.identity, "http://127.0.0.1:19132")
        session.player_id, session.player_unique_id = 42, 7
        with patch.object(JoinSession, "connect_nethernet", return_value=session):
            await self.bot.join_nethernet("http://127.0.0.1:19132")

    def deliver(self, *packets):
        self.transport.incoming.put_nowait(b"\xff" + write_batch(map(self.bot.protocol.encode, packets)))

    async def sent(self, kind):
        async with asyncio.timeout(1):
            while True:
                frame = await self.transport.sent.get()
                raw = next(read_batch(frame[1:]))
                if raw.packet_id == kind.ID:
                    return self.bot.protocol.decode(raw)

    def queued_packets(self):
        while not self.transport.sent.empty():
            frame = self.transport.sent.get_nowait()
            self.assertEqual(frame[:1], b"\xff")
            for raw in read_batch(frame[1:]):
                yield self.bot.protocol.decode(raw)

    def assert_only_idle_input_sent(self):
        # The movement ticker can send a heartbeat while the sign is idle.
        for packet in self.queued_packets():
            self.assertIsInstance(packet, self.b.PlayerAuthInputPacket)
            self.assertFalse(packet.input_data)
            for vector in (packet.move, packet.analog_move_vector, packet.raw_move_vector):
                self.assertEqual((vector.x, vector.y), (0, 0))

    def sign_tag(self, *, x=8, waxed=False, kind="Sign", front="front"):
        return nbt.CompoundTag({
            "id": nbt.StringTag(kind), "x": nbt.IntTag(x), "y": nbt.IntTag(20), "z": nbt.IntTag(9),
            "IsWaxed": nbt.ByteTag(waxed),
            "Extra": nbt.Int64Tag(-831),
            "FrontText": nbt.CompoundTag({"Text": nbt.StringTag(front), "Color": nbt.IntTag(41)}),
            "BackText": nbt.CompoundTag({"Text": nbt.StringTag("back"), "Color": nbt.IntTag(52)}),
        })

    async def observe_sign(self, *, waxed=False):
        self.deliver(
            self.b.ServerPlayerPostMovePositionPacket(pos=self.b.Vec3(x=8.5, y=21.62, z=7.5)),
            self.b.UpdateBlockPacket(pos=self.pos, runtime_id=1234, update_flags=0, layer=0),
            self.b.BlockActorDataPacket(pos=self.pos, data=self.sign_tag(waxed=waxed)),
        )
        await asyncio.sleep(0)
        self.assertEqual(self.bot.observed_sign(POSITION), SignObservation(POSITION, 0, "front", "back", waxed))

    async def wait_open(self, *, position=None):
        task = asyncio.create_task(self.bot.wait_for_sign_open(position=position, timeout=1))
        self.addAsyncCleanup(self.cancel_wait, task)
        await asyncio.sleep(0)
        return task

    async def cancel_wait(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def pause_sign_write(self):
        packets = self.bot.packets
        send = packets.send
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(packet, **options):
            if isinstance(packet, self.b.BlockActorDataPacket):
                entered.set()
                await release.wait()
            return await send(packet, **options)

        return patch.object(packets, "send", side_effect=hold), entered, release

    def respawn(self, runtime_id, state):
        return self.b.RespawnPacket(runtime_id=runtime_id, state=state,
                                    pos=self.b.Vec3(x=8.5, y=21.62, z=7.5))

    async def test_public_wait_observes_without_root_and_retains_exactly_one_open_instance(self):
        b = self.b
        other = b.BlockPos(x=10, y=20, z=9)
        self.deliver(b.UpdateBlockPacket(pos=self.pos, runtime_id=1234, layer=0))
        self.stop_actions_without_write()
        want = await self.wait_open(position=POSITION)
        first = await self.wait_open()
        self.deliver(b.OpenSignPacket(pos=other, is_front_side=True))
        self.assertEqual((await first).position, (10, 20, 9))
        self.assertFalse(want.done())
        opening = b.OpenSignPacket(pos=self.pos, is_front_side=False)
        self.deliver(opening, opening)
        earlier = await want
        current = self.bot.sign_editor
        self.assertIsInstance(current, SignEditor)
        self.assertEqual((earlier.position, earlier.dimension_id, earlier.side), (POSITION, 0, "back"))
        self.assertEqual((current.position, current.dimension_id, current.side), (POSITION, 0, "back"))
        self.assertIsNot(earlier, current)
        with self.assertRaises(FrozenInstanceError):
            current.side = "front"
        for handle in (earlier, replace(current)):
            with self.assertRaisesRegex(RuntimeError, "sign editor changed"):
                await self.bot.submit_sign_text(handle, "stale")
        with self.assertRaises(LookupError):
            await self.bot.submit_sign_text(current, "missing root")
        self.assertIs(self.bot.sign_editor, current)
        self.assert_only_idle_input_sent()

        self.deliver(b.BlockActorDataPacket(pos=self.pos, data=self.sign_tag(front="new opposite")))
        await asyncio.sleep(0)
        await self.bot.submit_sign_text(current, "edited back")
        saved = await self.sent(b.BlockActorDataPacket)
        self.assertEqual(saved.data["BackText"]["Text"].value, "edited back")
        self.assertEqual(saved.data["FrontText"]["Text"].value, "new opposite")
        self.assertEqual(saved.data["FrontText"]["Color"].value, 41)
        self.assertEqual(saved.data["Extra"].value, -831)
        self.assertIsNone(self.bot.sign_editor)
        with self.assertRaises(RuntimeError):
            await self.bot.submit_sign_text(current, "second write")
        self.assert_only_idle_input_sent()

    async def test_wait_timeout_and_cancel_do_not_consume_current_open_or_stop_reader(self):
        opening = self.b.OpenSignPacket(pos=self.pos, is_front_side=True)
        self.deliver(opening)
        await asyncio.sleep(0)
        previous = self.bot.sign_editor
        reader = self.bot.packets.start_receiving()
        with self.assertRaises(TimeoutError):
            await self.bot.wait_for_sign_open(position=POSITION, timeout=0)
        self.assertIs(self.bot.sign_editor, previous)
        cancelled = await self.wait_open()
        survivor = await self.wait_open(position=POSITION)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.deliver(opening)
        new = await survivor
        self.assertIsNot(new, previous)
        self.assertIs(self.bot.sign_editor, new)
        self.assertIs(self.bot.packets.start_receiving(), reader)
        self.assertFalse(reader.done())
        self.assert_only_idle_input_sent()

    async def test_submission_rechecks_latest_root_at_the_write_gate(self):
        b = self.b
        await self.observe_sign()
        opening = await self.wait_open()
        self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=False))
        editor = await opening
        pause, entered, release = self.pause_sign_write()
        with pause:
            submitting = asyncio.create_task(self.bot.submit_sign_text(editor, "edited back"))
            self.addAsyncCleanup(self.cancel_wait, submitting)
            await asyncio.wait_for(entered.wait(), 1)
            self.deliver(b.BlockActorDataPacket(pos=self.pos, data=self.sign_tag(front="late opposite")))
            await asyncio.sleep(0)
            release.set()
            with self.assertRaisesRegex(RuntimeError, "sign data changed"):
                await submitting
        self.assertIs(self.bot.sign_editor, editor)
        self.assert_only_idle_input_sent()
        await self.bot.submit_sign_text(editor, "edited back")
        saved = await self.sent(b.BlockActorDataPacket)
        self.assertEqual(saved.data["FrontText"]["Text"].value, "late opposite")

    async def test_same_side_reopen_at_the_write_gate_preserves_the_new_editor(self):
        b = self.b
        await self.observe_sign()
        opening = b.OpenSignPacket(pos=self.pos, is_front_side=False)
        pending = await self.wait_open()
        self.deliver(opening)
        old = await pending
        pause, entered, release = self.pause_sign_write()
        with pause:
            submitting = asyncio.create_task(self.bot.submit_sign_text(old, "old back"))
            self.addAsyncCleanup(self.cancel_wait, submitting)
            await asyncio.wait_for(entered.wait(), 1)
            pending = await self.wait_open()
            self.deliver(opening)
            fresh = await pending
            release.set()
            with self.assertRaisesRegex(RuntimeError, "sign editor changed"):
                await submitting
        self.assertIsNot(fresh, old)
        self.assertIs(self.bot.sign_editor, fresh)
        self.assert_only_idle_input_sent()
        await self.bot.submit_sign_text(fresh, "new back")
        self.assertEqual((await self.sent(b.BlockActorDataPacket)).data["BackText"]["Text"].value, "new back")

    async def test_public_submission_rejects_waxed_or_replaced_signs_and_unloaded_columns(self):
        b = self.b
        await self.observe_sign(waxed=True)
        opening = await self.wait_open()
        self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=True))
        waxed = await opening
        with self.assertRaises(PermissionError):
            await self.bot.submit_sign_text(waxed, "waxed")
        self.assert_only_idle_input_sent()

        for packets in (
            (b.UpdateBlockPacket(pos=self.pos, runtime_id=0, layer=0),
             b.UpdateBlockPacket(pos=self.pos, runtime_id=1234, layer=0)),
            (b.NetworkChunkPublisherUpdatePacket(position=b.BlockPos(x=800, z=800), radius=64),
             b.NetworkChunkPublisherUpdatePacket(position=self.pos, radius=64),
             b.UpdateBlockPacket(pos=self.pos, runtime_id=1234, layer=0)),
        ):
            await self.observe_sign()
            opening = await self.wait_open()
            self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=True), *packets,
                         b.BlockActorDataPacket(pos=self.pos, data=self.sign_tag(front="replacement")))
            stale = await opening
            self.assertIsNone(self.bot.sign_editor)
            with self.assertRaises(RuntimeError):
                await self.bot.submit_sign_text(stale, "invalid")
            self.assertEqual(self.bot.observed_sign(POSITION).front_text, "replacement")
            self.assert_only_idle_input_sent()

    async def test_editor_without_root_does_not_survive_sub_chunk_replacement_and_return(self):
        b = self.b
        section = b.SubChunkPacket

        def refresh(result, blocks=None):
            return section(cache_enabled=False, dimension_type=0, center_pos=b.SubChunkPos(x=0, y=1, z=0),
                           sub_chunk_data=[section.SubChunkPacketData(
                               result=result, serialized_sub_chunk=blocks,
                           )])

        pending = await self.wait_open()
        self.deliver(b.UpdateBlockPacket(pos=self.pos, runtime_id=1, layer=0),
                     b.OpenSignPacket(pos=self.pos, is_front_side=False))
        editor = await pending
        self.assertIs(self.bot.sign_editor, editor)
        self.assertIsNone(self.bot.observed_sign(POSITION))
        self.deliver(
            refresh(section.SubChunkRequestResult.SUCCESS_ALL_AIR),
            refresh(section.SubChunkRequestResult.SUCCESS, bytes((9, 1, 1, 1, 2))),
            b.BlockActorDataPacket(pos=self.pos, data=self.sign_tag(front="replaced after air")),
        )
        await asyncio.sleep(0)
        self.assertEqual(self.bot.observed_sign(POSITION).front_text, "replaced after air")
        self.assertIsNone(self.bot.sign_editor)
        with self.assertRaises(RuntimeError):
            await self.bot.submit_sign_text(editor, "stale after air")
        self.assert_only_idle_input_sent()

    async def test_dimension_and_local_rejoin_never_reuse_an_earlier_handle(self):
        b = self.b
        await self.observe_sign()
        opened = await self.wait_open()
        self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=False))
        first = await opened
        next_dimension = await self.wait_open()
        self.deliver(b.ChangeDimensionPacket(dimension_id=1), b.OpenSignPacket(pos=self.pos, is_front_side=False))
        second = await next_dimension
        self.assertEqual(second.dimension_id, 1)
        self.assertIs(self.bot.sign_editor, second)
        self.stop_actions_without_write()
        with self.assertRaisesRegex(RuntimeError, "sign editor changed"):
            await self.bot.submit_sign_text(first, "old dimension")
        pending = await self.wait_open()
        await self.bot.close()
        with self.assertRaises(ConnectionError):
            await pending
        self.assertIsNone(self.bot.sign_editor)

        await self.join()
        await self.observe_sign()
        opened = await self.wait_open()
        self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=False))
        current = await opened
        for stale in (first, second):
            with self.assertRaisesRegex(RuntimeError, "sign editor changed"):
                await self.bot.submit_sign_text(stale, "old session")
        self.assertIs(self.bot.sign_editor, current)
        self.assert_only_idle_input_sent()

    async def test_disconnect_drops_current_editor_and_errors_future_waits(self):
        b = self.b
        opened = await self.wait_open()
        opening = b.OpenSignPacket(pos=self.pos, is_front_side=False)
        self.deliver(opening)
        editor = await opened
        pending = await self.wait_open()
        self.deliver(b.DisconnectPacket(), opening)
        with self.assertRaises(ConnectionError):
            await pending
        with self.assertRaises(ConnectionError):
            await self.bot.wait_for_sign_open(timeout=0)
        self.assertIsNone(self.bot.sign_editor)
        with self.assertRaises(ConnectionError):
            await self.bot.submit_sign_text(editor, "disconnected")
        self.assert_only_idle_input_sent()

    async def test_reader_end_drops_current_editor_without_a_disconnect_packet(self):
        opened = await self.wait_open()
        self.deliver(self.b.OpenSignPacket(pos=self.pos, is_front_side=False))
        editor = await opened
        pending = await self.wait_open()
        reader = self.bot.packets.start_receiving()
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reader
        self.assertIsNone(self.bot.sign_editor)
        with self.assertRaises(ConnectionError):
            await pending
        with self.assertRaises(ConnectionError):
            await self.bot.submit_sign_text(editor, "reader ended")
        self.assert_only_idle_input_sent()

    async def test_local_respawn_invalidates_the_current_editor_and_a_queued_write(self):
        b = self.b
        states = b.PlayerRespawnState
        opening = b.OpenSignPacket(pos=self.pos, is_front_side=False)
        for runtime_id, state in ((0, states.SEARCHING_FOR_SPAWN), (42, states.READY_TO_SPAWN)):
            with self.subTest(runtime_id=runtime_id, state=state):
                await self.observe_sign()
                pending = await self.wait_open()
                self.deliver(opening)
                old = await pending
                pause, entered, release = self.pause_sign_write()
                with pause:
                    submitting = asyncio.create_task(self.bot.submit_sign_text(old, "stale after respawn"))
                    self.addAsyncCleanup(self.cancel_wait, submitting)
                    await asyncio.wait_for(entered.wait(), 1)
                    self.deliver(self.respawn(runtime_id, state))
                    await asyncio.sleep(0)
                    release.set()
                    with self.assertRaisesRegex(RuntimeError, "sign editor changed"):
                        await submitting
                self.assertIsNone(self.bot.sign_editor)
                self.assertEqual(self.bot.observed_sign(POSITION).back_text, "back")
                self.assert_only_idle_input_sent()

        pending = await self.wait_open()
        self.deliver(opening)
        fresh = await pending
        self.assertEqual((fresh.position, fresh.dimension_id, fresh.side), (POSITION, 0, "back"))
        with self.assertRaisesRegex(RuntimeError, "sign editor changed"):
            await self.bot.submit_sign_text(old, "earlier instance")
        await self.bot.submit_sign_text(fresh, "new back")
        saved = await self.sent(b.BlockActorDataPacket)
        self.assertEqual(saved.data["BackText"]["Text"].value, "new back")
        self.assertEqual(saved.data["FrontText"]["Text"].value, "front")

    async def test_other_player_respawn_and_non_transition_do_not_invalidate_editor(self):
        b = self.b
        state = b.PlayerRespawnState
        await self.observe_sign()
        pending = await self.wait_open()
        self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=True))
        handle = await pending
        self.deliver(self.respawn(7, state.SEARCHING_FOR_SPAWN), self.respawn(7, state.READY_TO_SPAWN),
                     self.respawn(42, state.CLIENT_READY_TO_SPAWN))
        await asyncio.sleep(0)
        # 7 is the local player's unique ID, but not their runtime ID.
        self.assertIs(self.bot.sign_editor, handle)
        self.assert_only_idle_input_sent()
        await self.bot.submit_sign_text(handle, "front after remote respawn")
        saved = await self.sent(b.BlockActorDataPacket)
        self.assertEqual(saved.data["FrontText"]["Text"].value, "front after remote respawn")
        self.assertEqual(saved.data["BackText"]["Text"].value, "back")

    async def test_combined_edit_ignores_remote_respawn_and_aborts_on_own_respawn_before_open(self):
        b = self.b
        await self.observe_sign()
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "new back", face=2, timeout=1))
        self.addAsyncCleanup(self.cancel_wait, editing)
        await self.sent(b.InventoryTransactionPacket)
        self.deliver(self.respawn(7, b.PlayerRespawnState.READY_TO_SPAWN),
                     b.OpenSignPacket(pos=self.pos, is_front_side=False))
        self.assertEqual(await editing, "back")
        saved = await self.sent(b.BlockActorDataPacket)
        self.assertEqual(saved.data["BackText"]["Text"].value, "new back")
        self.assertEqual(self.stop_actions_without_write(), [b.PlayerActionType.STOP_ITEM_USE_ON])

        await self.observe_sign()
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "stale edit", face=2, timeout=1))
        self.addAsyncCleanup(self.cancel_wait, editing)
        await self.sent(b.InventoryTransactionPacket)
        self.deliver(self.respawn(0, b.PlayerRespawnState.SEARCHING_FOR_SPAWN))
        with self.assertRaisesRegex(RuntimeError, "sign target changed"):
            await editing
        self.assertIsNone(self.bot.sign_editor)
        self.assertEqual(self.stop_actions_without_write(), [b.PlayerActionType.STOP_ITEM_USE_ON])

    async def test_opened_side_is_edited_without_losing_opposite_or_other_tags(self):
        await self.observe_sign()
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "new back", face=2, timeout=1))
        request = await self.sent(self.b.InventoryTransactionPacket)
        self.assertEqual(request.variant_transaction.target_block_id, 1234)
        self.deliver(
            self.b.OpenSignPacket(pos=self.b.BlockPos(x=10, y=20, z=9), is_front_side=True),
            self.b.OpenSignPacket(pos=self.pos, is_front_side=False),
            self.b.BlockActorDataPacket(pos=self.pos, data=self.sign_tag(front="refreshed front")),
        )
        saved = await self.sent(self.b.BlockActorDataPacket)
        self.assertEqual(await editing, "back")
        self.assertEqual(saved.data["BackText"]["Text"].value, "new back")
        self.assertEqual(saved.data["BackText"]["Color"].value, 52)
        self.assertEqual(saved.data["FrontText"]["Text"].value, "refreshed front")
        self.assertEqual(saved.data["FrontText"]["Color"].value, 41)
        self.assertEqual(saved.data["Extra"].value, -831)
        self.assertIsNone(self.bot.observed_sign(POSITION))

    async def test_new_sign_editor_in_same_batch_displaces_waiting_edit(self):
        await self.observe_sign()
        b = self.b
        other = b.BlockPos(x=10, y=20, z=9)
        self.deliver(
            b.UpdateBlockPacket(pos=other, runtime_id=5678, update_flags=0, layer=0),
            b.BlockActorDataPacket(pos=other, data=self.sign_tag(x=10)),
        )
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "old front", face=2, timeout=1))
        await self.sent(b.InventoryTransactionPacket)
        self.deliver(
            b.OpenSignPacket(pos=self.pos, is_front_side=True),
            b.OpenSignPacket(pos=other, is_front_side=False),
        )
        with self.assertRaisesRegex(RuntimeError, "sign editor changed"):
            await editing
        self.assertEqual(self.stop_actions_without_write(), [b.PlayerActionType.STOP_ITEM_USE_ON])

        await self.bot._signs.close_editor("new back")
        saved = await self.sent(b.BlockActorDataPacket)
        self.assertEqual(saved.pos.x, 10)
        self.assertEqual(saved.data["FrontText"]["Text"].value, "front")
        self.assertEqual(saved.data["BackText"]["Text"].value, "new back")

    async def test_reopened_side_in_same_batch_displaces_waiting_edit(self):
        await self.observe_sign()
        b = self.b
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "old front", face=2, timeout=1))
        await self.sent(b.InventoryTransactionPacket)
        self.deliver(
            b.OpenSignPacket(pos=self.pos, is_front_side=True),
            b.OpenSignPacket(pos=self.pos, is_front_side=False),
        )
        with self.assertRaisesRegex(RuntimeError, "sign editor changed"):
            await editing
        self.assertEqual(self.stop_actions_without_write(), [b.PlayerActionType.STOP_ITEM_USE_ON])

        await self.bot._signs.close_editor("new back")
        saved = await self.sent(b.BlockActorDataPacket)
        self.assertEqual(saved.pos.x, self.pos.x)
        self.assertEqual(saved.data["FrontText"]["Text"].value, "front")
        self.assertEqual(saved.data["BackText"]["Text"].value, "new back")

    async def test_sign_submission_requires_current_editor_and_preserves_tag_data(self):
        await self.observe_sign()
        with self.assertRaisesRegex(RuntimeError, "no sign editor is open"):
            await self.bot._signs.close_editor("no editor")
        self.deliver(self.b.OpenSignPacket(pos=self.pos, is_front_side=False))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "requested position"):
            await self.bot._signs.set_editor_text([9, 20, 9], "wrong target")
        await self.bot._signs.close_editor("new back")
        saved = await self.sent(self.b.BlockActorDataPacket)
        self.assertEqual(saved.data["FrontText"]["Text"].value, "front")
        self.assertEqual(saved.data["BackText"]["Text"].value, "new back")
        self.assertEqual(saved.data["BackText"]["Color"].value, 52)
        self.assertEqual(saved.data["Extra"].value, -831)
        with self.assertRaisesRegex(RuntimeError, "no sign editor is open"):
            await self.bot._signs.close_editor("already submitted")

        await self.observe_sign()
        self.deliver(self.b.OpenSignPacket(pos=self.pos, is_front_side=True))
        await asyncio.sleep(0)
        await self.bot._signs.set_editor_text(list(POSITION), "written front", "written back")
        saved = await self.sent(self.b.BlockActorDataPacket)
        self.assertEqual(saved.data["FrontText"]["Text"].value, "written front")
        self.assertEqual(saved.data["FrontText"]["Color"].value, 41)
        self.assertEqual(saved.data["BackText"]["Text"].value, "written back")
        self.assertEqual(saved.data["BackText"]["Color"].value, 52)

        await self.observe_sign()
        self.deliver(
            self.b.OpenSignPacket(pos=self.pos, is_front_side=True),
            self.b.UpdateBlockPacket(pos=self.pos, runtime_id=1234, update_flags=0, layer=0),
            self.b.BlockActorDataPacket(pos=self.pos, data=self.sign_tag(front="replacement")),
        )
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "no sign editor is open"):
            await self.bot._signs.set_editor_text(list(POSITION), "stale")
        self.assertEqual(self.bot.observed_sign(POSITION).front_text, "replacement")
        self.stop_actions_without_write()

    async def test_open_edit_does_not_follow_same_coordinate_replacement(self):
        b = self.b
        for scenario, changes, new_kind in (
            ("actor kind", (), "HangingSign"),
            ("same runtime and kind", (
                b.UpdateBlockPacket(pos=self.pos, runtime_id=0, update_flags=0, layer=0),
                b.UpdateBlockPacket(pos=self.pos, runtime_id=1234, update_flags=0, layer=0),
            ), "Sign"),
            ("unpublish then republish", (
                b.NetworkChunkPublisherUpdatePacket(
                    position=b.BlockPos(x=1000, y=20, z=1000), radius=128, server_built_chunks=[],
                ),
                b.NetworkChunkPublisherUpdatePacket(position=self.pos, radius=128, server_built_chunks=[]),
            ), "Sign"),
        ):
            with self.subTest(scenario=scenario):
                await self.observe_sign()
                editing = asyncio.create_task(self.bot.edit_sign(POSITION, "wrong target", face=2, timeout=1))
                await self.sent(b.InventoryTransactionPacket)
                self.deliver(
                    b.OpenSignPacket(pos=self.pos, is_front_side=False), *changes,
                    b.BlockActorDataPacket(pos=self.pos, data=self.sign_tag(kind=new_kind, front="replacement")),
                )
                with self.assertRaisesRegex(RuntimeError, "sign target changed"):
                    await editing
                self.assertEqual(self.bot.observed_sign(POSITION).front_text, "replacement")
                self.assertEqual(self.stop_actions_without_write(), [b.PlayerActionType.STOP_ITEM_USE_ON])

    async def test_unknown_waxed_and_dimension_change_decline_write(self):
        with self.assertRaises(LookupError):
            await self.bot.edit_sign(POSITION, "unknown", face=2)
        await self.observe_sign(waxed=True)
        with self.assertRaises(PermissionError):
            await self.bot.edit_sign(POSITION, "waxed", face=2)
        await self.observe_sign()
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "other world", face=2, timeout=1))
        await self.sent(self.b.InventoryTransactionPacket)
        self.deliver(self.b.ChangeDimensionPacket(dimension_id=1, pos=self.b.Vec3(x=8.5, y=22, z=7.5)))
        with self.assertRaisesRegex(RuntimeError, "dimension changed"):
            await editing
        self.assertIsNone(self.bot.observed_sign(POSITION))

    async def test_moving_out_of_published_chunks_forgets_sign_observation(self):
        await self.observe_sign()
        self.deliver(self.b.NetworkChunkPublisherUpdatePacket(
            position=self.b.BlockPos(x=1000, y=20, z=1000), radius=128, server_built_chunks=[],
        ))
        await asyncio.sleep(0)
        self.assertIsNone(self.bot.observed_sign(POSITION))

    async def test_failed_section_request_preserves_the_back_editor_and_observed_root(self):
        await self.observe_sign()
        b = self.b
        section = b.SubChunkPacket
        failed = section(cache_enabled=False, dimension_type=0, center_pos=b.SubChunkPos(x=0, y=1, z=0),
                         sub_chunk_data=[section.SubChunkPacketData(
                             result=section.SubChunkRequestResult.PLAYER_DOESNT_EXIST,
                         )])
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "new back", face=2, timeout=1))
        try:
            await self.sent(b.InventoryTransactionPacket)
            self.deliver(failed)
            await asyncio.sleep(0)
            root = self.bot.observed_block_entity(POSITION)
            self.assertIsNotNone(root)
            self.assertEqual(root["BackText"]["Text"].value, "back")
            self.assertEqual(self.bot.observed_sign(POSITION).front_text, "front")

            self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=False), failed)
            self.assertEqual(await editing, "back")
        finally:
            editing.cancel()
            await asyncio.gather(editing, return_exceptions=True)
        saved = await self.sent(b.BlockActorDataPacket)
        self.assertEqual(saved.data["FrontText"]["Text"].value, "front")
        self.assertEqual(saved.data["BackText"]["Text"].value, "new back")
        self.assertEqual(saved.data["Extra"].value, -831)

    async def test_silent_denial_submits_no_text_and_releases_interaction(self):
        await self.observe_sign()
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "denied", face=2, timeout=0.05))
        start = await self.sent(self.b.PlayerActionPacket)
        self.assertEqual(start.action, self.b.PlayerActionType.START_ITEM_USE_ON)
        with self.assertRaises(TimeoutError):
            await editing
        self.assertEqual(self.stop_actions_without_write(), [self.b.PlayerActionType.STOP_ITEM_USE_ON])
        self.assertEqual(self.bot.observed_sign(POSITION).back_text, "back")

    def stop_actions_without_write(self):
        actions = []
        for packet in self.queued_packets():
            self.assertNotIsInstance(packet, self.b.BlockActorDataPacket)
            if isinstance(packet, self.b.PlayerActionPacket):
                actions.append(packet.action)
        return actions

    async def test_expired_edit_budget_does_not_click(self):
        await self.observe_sign()
        with self.assertRaises(TimeoutError):
            await self.bot.edit_sign(POSITION, "too late", face=2, timeout=0)
        self.assert_only_idle_input_sent()

    async def test_edit_deadline_or_cancellation_bounds_a_blocked_click_and_stop(self):
        b = self.b
        send = self.transport.send
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                blocked, entered = asyncio.Event(), asyncio.Event()
                attempted = []

                async def hold_actions(frame):
                    raw = next(read_batch(frame[1:]))
                    if raw.packet_id == b.PlayerActionPacket.ID:
                        attempted.append(self.bot.protocol.decode(raw).action)
                        entered.set()
                        await blocked.wait()
                    await send(frame)

                await self.observe_sign()
                with patch.object(self.transport, "send", side_effect=hold_actions):
                    edit = asyncio.create_task(self.bot.edit_sign(
                        POSITION, "transport blocked", face=2, timeout=2 if cancel else 0.05,
                    ))
                    self.addAsyncCleanup(self.cancel_wait, edit)
                    try:
                        await asyncio.wait_for(entered.wait(), 0.5)
                        if cancel:
                            edit.cancel("caller cancelled")
                        done, _ = await asyncio.wait((edit,), timeout=0.5)
                        self.assertIn(edit, done)
                        with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError) as caught:
                            await edit
                        self.assertTrue(any("cleanup failed" in note for note in caught.exception.__notes__))
                        self.assertEqual(
                            attempted, [b.PlayerActionType.START_ITEM_USE_ON, b.PlayerActionType.STOP_ITEM_USE_ON],
                        )
                        self.assert_only_idle_input_sent()
                    finally:
                        blocked.set()

    async def test_edit_checks_deadline_after_click_or_text_encoding(self):
        b = self.b
        encode = self.bot.protocol.encode
        for delayed in (b.InventoryTransactionPacket, b.BlockActorDataPacket):
            with self.subTest(packet=delayed.__name__):
                await self.observe_sign()

                def delay(packet):
                    if isinstance(packet, delayed):
                        # The event loop cannot interrupt synchronous encoding.
                        sleep(0.18)
                    return encode(packet)

                with patch.object(self.bot.protocol, "encode", side_effect=delay):
                    edit = asyncio.create_task(self.bot.edit_sign(POSITION, "late", face=2, timeout=0.15))
                    self.addAsyncCleanup(self.cancel_wait, edit)
                    if delayed is b.BlockActorDataPacket:
                        await self.sent(b.InventoryTransactionPacket)
                        self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=False))
                    with self.assertRaises(TimeoutError):
                        await edit
                    self.assertFalse(any(isinstance(p, delayed) for p in self.queued_packets()))
                self.assertEqual(self.bot.observed_sign(POSITION).back_text, "back")

    async def test_stop_deadline_does_not_mask_the_submitted_side(self):
        b = self.b
        send = self.transport.send
        stopping = asyncio.Event()

        async def hold_stop(frame):
            raw = next(read_batch(frame[1:]))
            if (raw.packet_id == b.PlayerActionPacket.ID
                    and self.bot.protocol.decode(raw).action == b.PlayerActionType.STOP_ITEM_USE_ON):
                stopping.set()
                await asyncio.Event().wait()
            await send(frame)

        await self.observe_sign()
        with patch.object(self.transport, "send", side_effect=hold_stop):
            edit = asyncio.create_task(self.bot.edit_sign(POSITION, "saved", face=2, timeout=0.15))
            self.addAsyncCleanup(self.cancel_wait, edit)
            await self.sent(b.InventoryTransactionPacket)
            self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=False))
            saved = await self.sent(b.BlockActorDataPacket)
            self.assertEqual(saved.data["BackText"]["Text"].value, "saved")
            self.assertEqual(await asyncio.wait_for(edit, 0.8), "back")
            self.assertTrue(stopping.is_set())

    async def test_waiter_cleanup_cancellation_keeps_the_first_edit_failure_and_stops(self):
        b = self.b
        send = self.transport.send
        gather = asyncio.gather
        for first in ("send", "deadline", "caller"):
            with self.subTest(first=first):
                await self.observe_sign()
                original_error = OSError("click write failed")
                before_cleanup, actions = [], []

                async def observe_send(frame):
                    raw = next(read_batch(frame[1:]))
                    if raw.packet_id == b.PlayerActionPacket.ID:
                        actions.append(self.bot.protocol.decode(raw).action)
                    if first == "send" and raw.packet_id == b.InventoryTransactionPacket.ID:
                        raise original_error
                    await send(frame)

                async def cancel_in_cleanup(*pending, **options):
                    waiting = gather(*pending, **options)
                    if asyncio.current_task() is edit and not before_cleanup:
                        before_cleanup.append(exception())
                        asyncio.get_running_loop().call_soon(edit.cancel, "cleanup cancellation")
                        # Keep the cleanup await cancellable even when its packet waiter is already done.
                        await asyncio.sleep(0)
                    return await waiting

                with patch.object(self.transport, "send", side_effect=observe_send), \
                        patch.object(asyncio, "gather", side_effect=cancel_in_cleanup):
                    edit = asyncio.create_task(self.bot.edit_sign(
                        POSITION, "not submitted", face=2, timeout=0.15 if first == "deadline" else 2,
                    ))
                    self.addAsyncCleanup(self.cancel_wait, edit)
                    if first == "caller":
                        await self.sent(b.InventoryTransactionPacket)
                        edit.cancel("first caller cancellation")
                    try:
                        await edit
                    except (Exception, asyncio.CancelledError) as error:
                        received = error
                    else:
                        self.fail("sign edit unexpectedly completed")

                self.stop_actions_without_write()
                self.assertEqual(len(before_cleanup), 1)
                self.assertIs(received, before_cleanup[0])
                if first == "send":
                    self.assertIs(received, original_error)
                elif first == "deadline":
                    self.assertIsInstance(received, TimeoutError)
                else:
                    self.assertIsInstance(received, asyncio.CancelledError)
                    self.assertEqual(received.args, ("first caller cancellation",))
                self.assertEqual(actions, [b.PlayerActionType.START_ITEM_USE_ON, b.PlayerActionType.STOP_ITEM_USE_ON])

    async def test_cleanup_stop_does_not_cross_a_dimension_change_at_send(self):
        b = self.b
        packets = self.bot.packets
        send = packets.send
        entered, resume = asyncio.Event(), asyncio.Event()

        async def pause_stop(packet, **options):
            if isinstance(packet, b.PlayerActionPacket) and packet.action == b.PlayerActionType.STOP_ITEM_USE_ON:
                entered.set()
                await resume.wait()
            await send(packet, **options)

        await self.observe_sign()
        with patch.object(packets, "send", side_effect=pause_stop):
            edit = asyncio.create_task(self.bot.edit_sign(POSITION, "submitted", face=2, timeout=1))
            self.addAsyncCleanup(self.cancel_wait, edit)
            await self.sent(b.InventoryTransactionPacket)
            self.deliver(b.OpenSignPacket(pos=self.pos, is_front_side=False))
            saved = await self.sent(b.BlockActorDataPacket)
            self.assertEqual(saved.data["BackText"]["Text"].value, "submitted")
            await asyncio.wait_for(entered.wait(), 0.5)
            changed = asyncio.create_task(packets.wait_for_packet(b.ChangeDimensionPacket.ID, timeout=0.5))
            await asyncio.sleep(0)
            self.deliver(b.ChangeDimensionPacket(dimension_id=1))
            await changed
            resume.set()
            self.assertEqual(await edit, "back")
        self.assertEqual(self.stop_actions_without_write(), [])

    async def test_stop_failure_preserves_timeout_and_cancellation(self):
        b = self.b
        send = self.transport.send

        async def fail_stop(frame):
            raw = next(read_batch(frame[1:]))
            if (raw.packet_id == b.PlayerActionPacket.ID
                    and self.bot.protocol.decode(raw).action == b.PlayerActionType.STOP_ITEM_USE_ON):
                raise OSError("stop refused")
            await send(frame)

        await self.observe_sign()
        with patch.object(self.transport, "send", side_effect=fail_stop):
            for cancel in (False, True):
                with self.subTest(cancel=cancel):
                    editing = asyncio.create_task(self.bot.edit_sign(
                        POSITION, "no acknowledgement", face=2, timeout=1 if cancel else 0.05,
                    ))
                    await self.sent(b.InventoryTransactionPacket)
                    if cancel:
                        editing.cancel("caller cancelled")
                    with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError) as caught:
                        await editing
                    if cancel:
                        self.assertEqual(caught.exception.args, ("caller cancelled",))
                    self.assertTrue(any("stop refused" in note for note in caught.exception.__notes__))

    @unittest.skipUnless(hasattr(nbt, "decode_network_compound"), "native chunk NBT cursor unavailable")
    async def test_sub_chunk_observes_two_consecutive_sign_roots_then_replaces_them(self):
        chunk = self.b.SubChunkPacket
        prefix = bytes((9, 1, 1, 1, 2))

        def encoded_roots(front="front"):
            return b"".join(self.b.encode(self.b.BlockActorDataPacket(
                pos=self.b.BlockPos(x=0, y=0, z=0), data=self.sign_tag(x=x, waxed=x == 9, front=front),
            ))[3:] for x in (8, 9))

        def section(result, data=None):
            return chunk(cache_enabled=False, dimension_type=0,
                         center_pos=self.b.SubChunkPos(x=0, y=1, z=0), sub_chunk_data=[
                             chunk.SubChunkPacketData(
                                 sub_chunk_pos_offset=chunk.SubChunkPosOffset(x=0, y=0, z=0),
                                 result=result, serialized_sub_chunk=data,
                             ),
                         ])

        full = section(chunk.SubChunkRequestResult.SUCCESS, prefix + encoded_roots())
        air = section(chunk.SubChunkRequestResult.SUCCESS_ALL_AIR)
        self.deliver(
            self.b.ServerPlayerPostMovePositionPacket(pos=self.b.Vec3(x=8.5, y=21.62, z=7.5)), full,
        )
        await asyncio.sleep(0)
        self.assertEqual(self.bot.observed_sign(POSITION), SignObservation(POSITION, 0, "front", "back", False))
        other = (9, 20, 9)
        self.assertEqual(self.bot.observed_sign(other), SignObservation(other, 0, "front", "back", True))

        opening = self.b.OpenSignPacket(pos=self.pos, is_front_side=False)
        editing = asyncio.create_task(self.bot.edit_sign(POSITION, "new back", face=2, timeout=1))
        await self.sent(self.b.InventoryTransactionPacket)
        self.deliver(opening, section(chunk.SubChunkRequestResult.SUCCESS, prefix + encoded_roots("fresh front")))
        saved = await self.sent(self.b.BlockActorDataPacket)
        self.assertEqual(await editing, "back")
        self.assertEqual(saved.data["FrontText"]["Text"].value, "fresh front")
        self.assertEqual(saved.data["BackText"]["Text"].value, "new back")

        replaced = section(chunk.SubChunkRequestResult.SUCCESS, prefix[:4] + bytes((4,)) + encoded_roots())
        for scenario, refresh in (("all air", air), ("different runtime ID", replaced)):
            with self.subTest(scenario=scenario):
                self.deliver(full)
                await asyncio.sleep(0)
                editing = asyncio.create_task(self.bot.edit_sign(POSITION, "wrong target", face=2, timeout=1))
                await self.sent(self.b.InventoryTransactionPacket)
                self.deliver(opening, refresh, full)
                with self.assertRaisesRegex(RuntimeError, "sign target changed"):
                    await editing
                self.assertIsNotNone(self.bot.observed_sign(POSITION))
                self.assertEqual(self.stop_actions_without_write(), [self.b.PlayerActionType.STOP_ITEM_USE_ON])

        self.deliver(air)
        await asyncio.sleep(0)
        self.assertIsNone(self.bot.observed_sign(POSITION))
        self.assertIsNone(self.bot.observed_sign(other))
