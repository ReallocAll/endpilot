import asyncio
import unittest
from unittest.mock import patch

from endpilot import Bot, ForwardInput
from endpilot.blocks import HASHED_AIR_RUNTIME_ID
from endpilot.protocol import write_batch
from test_scheduled_input import ManualClock, Wire


WATER = 1211861802


class SwimmingTests(unittest.IsolatedAsyncioTestCase):
    async def join(self, *, initial=None, input_source=None, before_spawn=None):
        self.clock = ManualClock()
        self.addCleanup(self.clock.patch_scheduler().close)
        self.bot = Bot("Swim", protocol=2225, input_source=input_source)
        self.addAsyncCleanup(self.bot.close)
        self.b = b = self.bot.protocol.binding
        self.wire = Wire(self.bot.protocol)
        self.wire.push(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.wire.push(b.StartGamePacket(runtime_id=1, entity_id=7, block_network_ids_are_hashes=True))
        if initial is not None:
            self.wire.push(self.metadata(swimming=initial))
        if before_spawn is not None:
            for packet in before_spawn():
                self.wire.push(packet)
        self.wire.push(b.PlayStatusPacket(status=b.PlayStatus.PLAYER_SPAWN))
        with patch("endpilot.transport.nethernet.connect", return_value=self.wire):
            await self.bot.join_nethernet("http://127.0.0.1:20662")
        await self.deliver(self.position(), self.block(WATER, layer=1))

    async def deliver(self, *packets):
        marker = self.b.SetTimePacket()
        wait = asyncio.create_task(self.bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        self.wire.incoming.put_nowait(write_batch(self.bot.protocol.encode(p) for p in (*packets, marker)))
        await wait

    def position(self, x=0.5):
        b = self.b
        return b.MovePlayerPacket(player_id=1, pos=b.Vec3(x=x, y=221.62, z=0.5))

    def block(self, runtime_id, *, layer=0, x=0, y=221):
        b = self.b
        return b.UpdateBlockPacket(pos=b.BlockPos(x=x, y=y, z=0), runtime_id=runtime_id, layer=layer)

    def metadata(self, runtime_id=1, *, swimming=None, air=None, tick=0):
        b = self.b
        entries = []
        if swimming is not None:
            flags = (1 << int(b.ActorFlags.SWIMMING)) if swimming else 0
            entries.append(b.DataItemEntry(id=0, payload=b.DataItemInt64Payload(value=flags)))
        if air is not None:
            entries.append(b.DataItemEntry(id=int(b.ActorDataIDs.AIR_SUPPLY),
                                           payload=b.DataItemShortPayload(value=air)))
        return b.SetActorDataPacket(id=runtime_id, tick=tick,
                                    packed_items=b.SynchedActorData.CopyableDataList(data=entries))

    def respawn(self, runtime_id, state="READY_TO_SPAWN", *, x=0.5):
        b = self.b
        return b.RespawnPacket(runtime_id=runtime_id, pos=b.Vec3(x=x, y=221.62, z=0.5),
                               state=b.PlayerRespawnState[state])

    async def test_pre_spawn_dimension_change_discards_prior_swim_observation(self):
        def before_spawn():
            b = self.b
            ability = 1 << int(b.AbilitiesIndex.MAY_FLY)
            data = b.SerializedAbilitiesData
            return (
                b.UpdateAbilitiesPacket(data=data(target_player=7, layers=[data.SerializedLayer(
                    serialized_layer=data.SerializedAbilitiesLayer.BASE, abilities_set=ability,
                    ability_values=ability,
                )])),
                self.metadata(swimming=True, air=23),
                b.ChangeDimensionPacket(dimension_id=1),
            )

        await self.join(before_spawn=before_spawn)
        self.assertIsNone(self.bot.is_swimming)
        self.assertEqual(self.bot.vitals.air_supply, 23)
        self.assertIs(self.bot.abilities.may_fly, True)

    async def test_public_swim_submission_and_sparse_own_metadata_are_independent(self):
        await self.join(initial=False)
        bot, b = self.bot, self.b
        flags = b.PlayerAuthInputPacket.InputData
        self.assertIs(bot.is_swimming, False)
        await bot.set_move_vector(0, 1)
        async with bot._movement._action_lock:
            pending = asyncio.create_task(bot.set_swimming(True))
            await asyncio.sleep(0)
            await self.deliver(self.metadata(99, swimming=True), self.respawn(99), self.metadata(air=30))
        tick = await pending
        frame = self.wire.sent[-1]
        self.assertEqual(frame.client_tick, tick)
        self.assertTrue({flags.UP, flags.START_SWIMMING}.issubset(frame.input_data))
        self.assertIs(bot.is_swimming, False)
        self.assertEqual(bot.vitals.air_supply, 30)
        self.assertIsNone(await bot.set_swimming(True))

        await self.deliver(self.metadata(swimming=True), self.metadata(air=27))
        self.assertIs(bot.is_swimming, True)
        stop_tick = await bot.set_swimming(False)
        frame = self.wire.sent[-1]
        self.assertEqual(stop_tick, frame.client_tick)
        self.assertTrue({flags.UP, flags.STOP_SWIMMING}.issubset(frame.input_data))
        self.assertIs(bot.is_swimming, True)
        begin = len(self.wire.sent)
        self.assertIsNone(await bot.set_swimming(False))
        self.assertEqual(len(self.wire.sent), begin)
        await self.deliver(self.metadata(swimming=False))
        self.assertIs(bot.is_swimming, False)
        self.assertEqual(bot.vitals.air_supply, 27)

    async def test_unknown_and_observed_dry_terrain_submit_no_start(self):
        await self.join()
        begin = len(self.wire.sent)
        await self.deliver(self.block(HASHED_AIR_RUNTIME_ID, layer=1))
        self.assertIsNone(await self.bot.set_swimming(True))
        await self.deliver(self.block(HASHED_AIR_RUNTIME_ID), self.block(HASHED_AIR_RUNTIME_ID, y=220),
                           self.block(HASHED_AIR_RUNTIME_ID, y=220, layer=1))
        self.assertIsNone(await self.bot.set_swimming(True))
        self.assertEqual(len(self.wire.sent), begin)
        self.assertIsNone(self.bot.is_swimming)

    async def test_late_metadata_keeps_the_newer_submitted_swim_intent(self):
        await self.join(initial=False)
        bot = self.bot
        begin = len(self.wire.sent)
        first = await bot.set_swimming(True)
        stopped = await bot.set_swimming(False)
        await self.deliver(self.metadata(swimming=True, tick=first))
        self.assertIs(bot.is_swimming, True)

        restarted = await bot.set_swimming(True)
        self.assertEqual((first, stopped, restarted), (1, 2, 3))
        frames = self.wire.sent[begin:]
        self.assertEqual([[flag.name for flag in frame.input_data] for frame in frames], [
            ["START_SWIMMING"], ["STOP_SWIMMING"], ["START_SWIMMING"],
        ])
        await self.deliver(self.metadata(swimming=False, tick=restarted))
        self.assertEqual(await bot.set_swimming(True), 4)
        self.assertIs(bot.is_swimming, False)

    async def test_send_failure_does_not_become_the_latest_swim_intent(self):
        await self.join(initial=False)

        async def fail_send():
            raise OSError("send failed")

        begin = len(self.wire.sent)
        self.wire.before_send = fail_send
        with self.assertRaisesRegex(OSError, "send failed"):
            await self.bot.set_swimming(True)
        self.assertEqual(len(self.wire.sent), begin)
        self.wire.before_send = None
        self.assertIsInstance(await self.bot.set_swimming(True), int)

    async def test_own_search_blocks_new_swim_inputs_until_ready_at_new_position(self):
        await self.join(initial=False)
        bot = self.bot
        await self.deliver(self.respawn(99, "SEARCHING_FOR_SPAWN"))
        self.assertIsInstance(await bot.set_swimming(True), int)
        await bot.set_swimming(False)
        begin = len(self.wire.sent)

        await self.deliver(self.respawn(0, "SEARCHING_FOR_SPAWN"), self.position())
        self.assertIsNone(await bot.set_swimming(True))
        await self.deliver(self.respawn(99, x=2.5))
        self.assertIsNone(await bot.set_swimming(True))
        await self.deliver(self.respawn(1, x=2.5))
        self.assertIsNone(await bot.set_swimming(True))
        self.assertEqual(len(self.wire.sent), begin)

        await self.deliver(self.block(WATER, x=2))
        self.assertIsInstance(await bot.set_swimming(True), int)

    async def test_dimension_and_local_respawn_cancel_queued_swim_input(self):
        await self.join(initial=False)
        begin = len(self.wire.sent)
        for reset in (self.b.ChangeDimensionPacket(dimension_id=1), self.respawn(0), self.respawn(1)):
            with self.subTest(packet=type(reset).__name__, runtime=getattr(reset, "runtime_id", None)):
                await self.deliver(self.metadata(swimming=True))
                async with self.bot._movement._action_lock:
                    pending = asyncio.create_task(self.bot.set_swimming(True))
                    await asyncio.sleep(0)
                    await self.deliver(reset, self.position(), self.block(WATER))
                    self.assertIsNone(self.bot.is_swimming)
                with self.assertRaisesRegex(RuntimeError, "position changed"):
                    await pending
                self.assertEqual(len(self.wire.sent), begin)

    async def test_cancelled_request_does_not_steal_current_vehicle_control(self):
        await self.join()
        b = self.b
        begin = len(self.wire.sent)
        async with self.bot._movement._action_lock:
            pending = asyncio.create_task(self.bot.set_swimming(True))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await self.deliver(b.SetActorLinkPacket(link=b.ActorLink(a=9, b=7, type=b.ActorLinkType.RIDING)))
        with self.assertRaisesRegex(RuntimeError, "owns movement"):
            await self.bot.set_swimming(True)
        self.assertEqual(len(self.wire.sent), begin)
        await self.deliver(b.SetActorLinkPacket(link=b.ActorLink(a=9, b=7, type=b.ActorLinkType.NONE)))
        self.assertIsInstance(await self.bot.set_swimming(True), int)

    async def test_timed_input_source_owns_movement(self):
        await self.join(input_source=lambda: ForwardInput(0.0, 0.09))
        begin = len(self.wire.sent)
        with self.assertRaisesRegex(RuntimeError, "owns movement"):
            await self.bot.set_swimming(True)
        self.assertEqual(len(self.wire.sent), begin)
