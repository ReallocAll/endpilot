import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import AsyncMock, patch

from endpilot import Bot
from endpilot.protocol import write_batch
from test_scheduled_input import ManualClock, Wire


class FlightTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def packet(binding, target, *layers):
        data = binding.SerializedAbilitiesData
        return binding.UpdateAbilitiesPacket(data=data(
            target_player=target,
            layers=[data.SerializedLayer(serialized_layer=data.SerializedAbilitiesLayer[name],
                                         abilities_set=defined, ability_values=values)
                    for name, defined, values in layers],
        ))

    async def join(self, bot, wire, *before_start, start=None, after_start=()):
        b = bot.protocol.binding
        for packet in (
            b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE),
            *before_start,
            start if start is not None else b.StartGamePacket(runtime_id=13, entity_id=-27),
            *after_start,
            b.PlayStatusPacket(status=3),
        ):
            wire.push(packet)
        with patch("endpilot.transport.nethernet.connect", new=AsyncMock(return_value=wire)):
            await bot.join_nethernet("http://127.0.0.1:20592")

    async def deliver(self, bot, wire, *packets):
        marker = bot.protocol.binding.SetTimePacket()
        seen = asyncio.create_task(bot.packets.wait_for_packet(marker.ID, timeout=1))
        await asyncio.sleep(0)
        wire.incoming.put_nowait(write_batch(bot.protocol.encode(packet) for packet in (*packets, marker)))
        await seen

    async def test_spawn_replay_preserves_authorization_and_mode_order(self):
        for order in ("grant_first", "mode_first"):
            with self.subTest(order=order):
                bot = Bot("Flight")
                self.addAsyncCleanup(bot.close)
                b = bot.protocol.binding
                wire = Wire(bot.protocol)
                may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
                flying = 1 << int(b.AbilitiesIndex.FLYING)
                grant = self.packet(b, -27, ("BASE", may_fly | flying, may_fly))
                switch = b.SetPlayerGameTypePacket(player_game_type=b.GameType.SURVIVAL)
                start = b.StartGamePacket(runtime_id=13, entity_id=-27, entity_game_type=b.GameType.CREATIVE)
                after = (grant, switch) if order == "grant_first" else (switch, grant)
                await self.join(bot, wire, start=start, after_start=after)
                self.assertEqual(bot.game_mode, b.GameType.SURVIVAL)
                self.assertIs(bot.abilities.may_fly, True)
                before = list(wire.sent)
                if order == "grant_first":
                    with self.assertRaises(PermissionError):
                        await bot.set_flying(False)
                else:
                    await bot.set_flying(False)
                self.assertEqual(wire.sent, before)

    async def test_only_an_effective_local_mode_change_disarms_public_flight(self):
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
        flying = 1 << int(b.AbilitiesIndex.FLYING)
        mode = b.GameType
        grant = self.packet(b, -27, ("BASE", may_fly | flying, may_fly))
        start = b.StartGamePacket(runtime_id=13, entity_id=-27, entity_game_type=mode.DEFAULT,
                                  settings=b.LevelSettings(game_type=mode.CREATIVE))
        await self.join(bot, wire, grant, start=start)
        self.assertEqual(bot.game_mode, mode.CREATIVE)
        observed = bot.abilities
        before = list(wire.sent)

        for packet in (
            b.UpdatePlayerGameTypePacket(target_player=13, player_game_type=mode.SURVIVAL),
            b.SetDefaultGameTypePacket(default_game_type=mode.CREATIVE),
            b.SetDefaultGameTypePacket(default_game_type=mode.UNDEFINED),
            b.SetDefaultGameTypePacket(default_game_type=mode.CREATIVE),
        ):
            await self.deliver(bot, wire, packet)
            await bot.set_flying(False)
        self.assertEqual(wire.sent, before)

        await self.deliver(bot, wire, b.SetDefaultGameTypePacket(default_game_type=mode.UNDEFINED),
                           b.SetDefaultGameTypePacket(default_game_type=mode.SURVIVAL))
        self.assertEqual(bot.game_mode, mode.SURVIVAL)
        self.assertIs(bot.abilities, observed)
        await self.deliver(bot, wire, self.packet(b, 13, ("BASE", may_fly | flying, may_fly)))
        with self.assertRaises(PermissionError):
            await bot.set_flying(True)
        self.assertEqual(wire.sent, before)

        await self.deliver(bot, wire, grant, b.SetPlayerGameTypePacket(player_game_type=mode.SURVIVAL),
                           b.SetDefaultGameTypePacket(default_game_type=mode.CREATIVE))
        self.assertEqual(bot.game_mode, mode.SURVIVAL)
        await bot.set_flying(False)
        self.assertEqual(wire.sent, before)

    async def test_pre_spawn_layers_owner_and_next_wire_update(self):
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
        flying = 1 << int(b.AbilitiesIndex.FLYING)
        original = self.packet(b, -27, ("COMMANDS", flying, 0), ("BASE", may_fly | flying, may_fly | flying))
        other = self.packet(b, 13, ("BASE", may_fly | flying, 0))
        await self.join(bot, wire, original, other)
        snapshot = bot.abilities
        self.assertIs(snapshot.may_fly, True)
        self.assertIs(snapshot.flying, False)
        with self.assertRaises(FrozenInstanceError):
            snapshot.flying = True

        next_update = asyncio.create_task(bot.wait_for_abilities())
        await asyncio.sleep(0)
        wire.push(other)
        await asyncio.sleep(0)
        self.assertFalse(next_update.done())
        self.assertEqual(bot.abilities, snapshot)
        wire.push(self.packet(b, -27, ("BASE", flying, may_fly | flying)))
        wire.push(self.packet(b, -27, ("BASE", may_fly | flying, 0)))
        omitted = await next_update
        self.assertIsNone(omitted.may_fly)
        self.assertIs(omitted.flying, True)
        self.assertEqual(omitted.ability_values, flying)
        self.assertIs(bot.abilities.may_fly, False)
        self.assertIs(bot.abilities.flying, False)

        unchanged = asyncio.create_task(bot.wait_for_abilities())
        await asyncio.sleep(0)
        self.assertFalse(unchanged.done())
        wire.push(self.packet(b, -27, ("BASE", may_fly | flying, 0)))
        self.assertEqual(await unchanged, bot.abilities)

    async def test_switches_use_confirmed_and_submitted_targets_without_duplicate_inputs(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        flags = b.PlayerAuthInputPacket.InputData
        may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
        flying = 1 << int(b.AbilitiesIndex.FLYING)

        with clock.patch_scheduler():
            await self.join(bot, wire, self.packet(b, -27, ("BASE", may_fly | flying, may_fly)))
            before = len(wire.sent)
            await asyncio.wait_for(bot.set_flying(False), 1)
            self.assertEqual(len(wire.sent), before)

            wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
            await bot.wait_for_position_sync()
            start = asyncio.create_task(bot.set_flying(True))
            duplicate = asyncio.create_task(bot.set_flying(True))
            await asyncio.sleep(0)
            await self.deliver(
                bot, wire,
                self.packet(b, -27, ("BASE", may_fly | flying, may_fly | flying)),
                self.packet(b, -27, ("BASE", may_fly | flying, may_fly)),
                self.packet(b, 13, ("BASE", may_fly | flying, 0)),
            )
            await clock.wake_at(50)
            await clock.wake_at(100)
            await asyncio.wait_for(asyncio.gather(start, duplicate), 1)
            self.assertEqual(len(wire.sent), before + 3)
            self.assertIn(flags.START_FLYING, wire.sent[-1].input_data)
            self.assertIs(bot.abilities.flying, False)

            stop = asyncio.create_task(bot.set_flying(False))
            await asyncio.sleep(0)
            await clock.wake_at(150)
            await clock.wake_at(200)
            await asyncio.wait_for(stop, 1)
            self.assertEqual(len(wire.sent), before + 6)
            self.assertIn(flags.STOP_FLYING, wire.sent[-1].input_data)

            omitted = asyncio.create_task(bot.wait_for_abilities())
            await asyncio.sleep(0)
            wire.push(self.packet(b, -27, ("BASE", may_fly, may_fly)))
            self.assertIsNone((await omitted).flying)
            await asyncio.wait_for(bot.set_flying(False), 1)
            self.assertEqual(len(wire.sent), before + 6)

            confirmed = asyncio.create_task(bot.wait_for_abilities())
            await asyncio.sleep(0)
            wire.push(self.packet(b, -27, ("BASE", may_fly | flying, may_fly | flying)))
            self.assertIs((await confirmed).flying, True)
            await asyncio.wait_for(bot.set_flying(True), 1)
            self.assertEqual(len(wire.sent), before + 6)

    async def test_own_update_during_final_send_takes_precedence_over_submission(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
        flying = 1 << int(b.AbilitiesIndex.FLYING)
        grounded = self.packet(b, -27, ("BASE", may_fly | flying, may_fly))

        with clock.patch_scheduler():
            await self.join(bot, wire, grounded)
            wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
            await bot.wait_for_position_sync()
            before = len(wire.sent)
            final_send = asyncio.Event()
            release = asyncio.Event()

            async def hold_final_send():
                if len(wire.sent) == before + 2:
                    final_send.set()
                    await release.wait()

            wire.before_send = hold_final_send
            start = asyncio.create_task(bot.set_flying(True))
            await asyncio.sleep(0)
            await clock.wake_at(50)
            await clock.wake_at(100)
            await asyncio.wait_for(final_send.wait(), 1)
            confirmed = asyncio.create_task(bot.wait_for_abilities())
            await asyncio.sleep(0)
            wire.push(grounded)
            self.assertIs((await confirmed).flying, False)
            release.set()
            await asyncio.wait_for(start, 1)
            await asyncio.wait_for(bot.set_flying(False), 1)
            self.assertEqual(len(wire.sent), before + 3)

    async def test_public_creative_attempt_preserves_observations_and_can_retry(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        flags = b.PlayerAuthInputPacket.InputData
        mode = b.GameType
        may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
        flying = 1 << int(b.AbilitiesIndex.FLYING)
        grounded = self.packet(b, -27, ("BASE", may_fly | flying, 0))
        creative = b.UpdatePlayerGameTypePacket(target_player=-27, player_game_type=mode.CREATIVE)
        other_creative = b.UpdatePlayerGameTypePacket(target_player=13, player_game_type=mode.CREATIVE)
        other_survival = b.UpdatePlayerGameTypePacket(target_player=13, player_game_type=mode.SURVIVAL)

        with clock.patch_scheduler():
            start = b.StartGamePacket(runtime_id=13, entity_id=-27, entity_game_type=mode.SURVIVAL)
            await self.join(bot, wire, grounded, start=start)
            wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
            await bot.wait_for_position_sync()
            before = len(wire.sent)
            observed = bot.abilities
            await self.deliver(bot, wire, other_creative)
            with self.assertRaises(PermissionError):
                await bot.set_flying(True)
            await self.deliver(bot, wire, b.SetPlayerGameTypePacket(player_game_type=mode.SPECTATOR))
            with self.assertRaises(PermissionError):
                await bot.set_flying(True)
            self.assertEqual(len(wire.sent), before)

            await self.deliver(bot, wire, creative)
            attempt = asyncio.create_task(bot.set_flying(True))
            await asyncio.sleep(0)
            await self.deliver(bot, wire, creative, other_survival)
            await clock.wake_at(50)
            await clock.wake_at(100)
            self.assertIsNone(await asyncio.wait_for(attempt, 1))
            frames = wire.sent[before:]
            self.assertEqual(len(frames), 3)
            self.assertEqual(
                [tuple(flag for flag in (flags.JUMP_PRESSED_RAW, flags.JUMP_RELEASED_RAW, flags.START_FLYING)
                       if flag in frame.input_data) for frame in frames],
                [(flags.JUMP_PRESSED_RAW,), (flags.JUMP_RELEASED_RAW,), (flags.JUMP_PRESSED_RAW, flags.START_FLYING)],
            )
            self.assertIs(bot.abilities, observed)
            self.assertIs(bot.abilities.may_fly, False)
            self.assertIs(bot.abilities.flying, False)

            await self.deliver(bot, wire, self.packet(b, -27, ("BASE", 0, 0)))
            self.assertIsNone(bot.abilities.may_fly)
            await asyncio.wait_for(bot.set_flying(True), 1)
            self.assertEqual(len(wire.sent), before + 3)

            # The server can repeat the original flying value when it cancels the attempt.
            await self.deliver(bot, wire, self.packet(b, -27, ("BASE", flying, 0)))
            retry = asyncio.create_task(bot.set_flying(True))
            await asyncio.sleep(0)
            await clock.wake_at(150)
            await clock.wake_at(200)
            self.assertIsNone(await asyncio.wait_for(retry, 1))
            self.assertEqual(len(wire.sent), before + 6)
            self.assertIn(flags.START_FLYING, wire.sent[-1].input_data)
            self.assertIs(bot.abilities.flying, False)

    async def test_unresolved_mode_interrupts_creative_only_attempts(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        mode = b.GameType
        flags = b.PlayerAuthInputPacket.InputData

        with clock.patch_scheduler():
            start = b.StartGamePacket(runtime_id=13, entity_id=-27, entity_game_type=mode.DEFAULT,
                                      settings=b.LevelSettings(game_type=mode.CREATIVE))
            await self.join(bot, wire, start=start)
            wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
            await bot.wait_for_position_sync()
            before = len(wire.sent)
            switching = asyncio.create_task(bot.set_flying(True))
            queued = asyncio.create_task(bot.set_flying(True))
            try:
                await asyncio.sleep(0)
                self.assertEqual(len(wire.sent), before + 1)
                self.assertFalse(queued.done())
                await self.deliver(bot, wire, b.SetDefaultGameTypePacket(default_game_type=mode.UNDEFINED))
                self.assertIsNone(bot.game_mode)
                with self.assertRaises(PermissionError):
                    await asyncio.wait_for(bot.set_flying(True), 1)
                self.assertEqual(len(wire.sent), before + 1)

                await self.deliver(bot, wire, b.SetDefaultGameTypePacket(default_game_type=mode.CREATIVE))
                await clock.wake_at(50)
                for action in (switching, queued):
                    with self.assertRaisesRegex(PermissionError, "changed"):
                        await asyncio.wait_for(action, 1)
                switch_flags = (flags.JUMP_PRESSED_RAW, flags.START_FLYING, flags.STOP_FLYING)
                edges = [flag for frame in wire.sent[before:] for flag in switch_flags if flag in frame.input_data]
                self.assertEqual(edges, [flags.JUMP_PRESSED_RAW])
            finally:
                for action in (switching, queued):
                    if not action.done():
                        action.cancel()
                await asyncio.gather(switching, queued, return_exceptions=True)

            before_retry = len(wire.sent)
            fresh = asyncio.create_task(bot.set_flying(True))
            await asyncio.sleep(0)
            await clock.wake_at(100)
            await clock.wake_at(150)
            self.assertIsNone(await asyncio.wait_for(fresh, 1))
            self.assertEqual(len(wire.sent), before_retry + 3)
            self.assertIn(flags.START_FLYING, wire.sent[-1].input_data)

    async def test_mode_change_during_final_send_does_not_restore_the_old_submitted_target(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        flags = b.PlayerAuthInputPacket.InputData
        may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
        flying = 1 << int(b.AbilitiesIndex.FLYING)
        grounded = self.packet(b, -27, ("BASE", may_fly | flying, may_fly))

        with clock.patch_scheduler():
            start = b.StartGamePacket(runtime_id=13, entity_id=-27, entity_game_type=b.GameType.CREATIVE)
            await self.join(bot, wire, grounded, start=start)
            wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
            await bot.wait_for_position_sync()
            before = len(wire.sent)
            final_send = asyncio.Event()
            release = asyncio.Event()

            async def hold_final_send():
                if len(wire.sent) == before + 2:
                    final_send.set()
                    await release.wait()

            wire.before_send = hold_final_send
            first = asyncio.create_task(bot.set_flying(True))
            try:
                await asyncio.sleep(0)
                await clock.wake_at(50)
                await clock.wake_at(100)
                await asyncio.wait_for(final_send.wait(), 1)
                observed = bot.abilities
                await self.deliver(bot, wire, b.UpdatePlayerGameTypePacket(
                    target_player=-27, player_game_type=b.GameType.SURVIVAL,
                ))
                self.assertIs(bot.abilities, observed)
                with self.assertRaises(PermissionError):
                    await bot.set_flying(True)
                await self.deliver(bot, wire, self.packet(b, -27, ("BASE", may_fly, may_fly)))
            finally:
                release.set()
            with self.assertRaisesRegex(PermissionError, "changed"):
                await asyncio.wait_for(first, 1)

            second = asyncio.create_task(bot.set_flying(True))
            await asyncio.sleep(0)
            await clock.wake_at(150)
            await clock.wake_at(200)
            await asyncio.wait_for(second, 1)
            self.assertEqual(len(wire.sent), before + 6)
            self.assertIn(flags.START_FLYING, wire.sent[-1].input_data)

    async def test_new_grant_does_not_rescue_incomplete_or_queued_switches(self):
        for interruption in ("mode", "revoked", "omitted"):
            with self.subTest(interruption=interruption):
                clock = ManualClock()
                bot = Bot("Flight")
                self.addAsyncCleanup(bot.close)
                b = bot.protocol.binding
                wire = Wire(bot.protocol)
                flags = b.PlayerAuthInputPacket.InputData
                may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
                flying = 1 << int(b.AbilitiesIndex.FLYING)
                grounded = self.packet(b, -27, ("BASE", may_fly | flying, may_fly))
                loss = {
                    "mode": b.SetPlayerGameTypePacket(player_game_type=b.GameType.SURVIVAL),
                    "revoked": self.packet(b, -27, ("BASE", may_fly | flying, 0)),
                    "omitted": self.packet(b, -27, ("BASE", flying, 0)),
                }[interruption]

                with clock.patch_scheduler():
                    start = b.StartGamePacket(runtime_id=13, entity_id=-27, entity_game_type=b.GameType.CREATIVE)
                    await self.join(bot, wire, grounded, start=start)
                    wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
                    await bot.wait_for_position_sync()
                    before = len(wire.sent)
                    switching = asyncio.create_task(bot.set_flying(True))
                    queued = None
                    try:
                        await asyncio.sleep(0)
                        self.assertEqual(len(wire.sent), before + 1)
                        queued = asyncio.create_task(bot.set_flying(True))
                        await asyncio.sleep(0)
                        self.assertFalse(queued.done())
                        await self.deliver(bot, wire, loss, grounded)
                        await clock.wake_at(50)
                        for action in (switching, queued):
                            with self.assertRaisesRegex(PermissionError, "changed"):
                                await asyncio.wait_for(action, 1)
                    finally:
                        for action in (switching, queued):
                            if action is not None and not action.done():
                                action.cancel()
                                await asyncio.gather(action, return_exceptions=True)
                    switch_flags = (flags.JUMP_PRESSED_RAW, flags.START_FLYING, flags.STOP_FLYING)
                    edges = [flag for frame in wire.sent[before:] for flag in switch_flags if flag in frame.input_data]
                    self.assertEqual(edges, [flags.JUMP_PRESSED_RAW])
                    await bot.set_flying(False)
                    fresh = asyncio.create_task(bot.set_flying(True))
                    await asyncio.sleep(0)
                    await clock.wake_at(100)
                    await clock.wake_at(150)
                    await asyncio.wait_for(fresh, 1)
                    self.assertIn(flags.START_FLYING, wire.sent[-1].input_data)

    async def test_normal_flight_inputs_and_revocation_during_a_switch(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        flags = b.PlayerAuthInputPacket.InputData
        may_fly = 1 << int(b.AbilitiesIndex.MAY_FLY)
        flying = 1 << int(b.AbilitiesIndex.FLYING)

        with clock.patch_scheduler():
            await self.join(bot, wire)
            before = len(wire.sent)
            with self.assertRaises(PermissionError):
                await bot.set_flying(True)
            self.assertEqual(len(wire.sent), before)

            observed = asyncio.create_task(bot.wait_for_abilities())
            await asyncio.sleep(0)
            wire.push(self.packet(b, -27, ("BASE", may_fly | flying, may_fly)))
            self.assertIs((await observed).may_fly, True)
            wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
            await bot.wait_for_position_sync()

            switch = asyncio.create_task(bot.set_flying(True))
            await asyncio.sleep(0)
            await clock.wake_at(50)
            await clock.wake_at(100)
            self.assertIsNone(await switch)
            first, release, second = wire.sent[before:]
            self.assertIn(flags.JUMP_PRESSED_RAW, first.input_data)
            self.assertIn(flags.JUMP_RELEASED_RAW, release.input_data)
            self.assertIn(flags.JUMP_PRESSED_RAW, second.input_data)
            self.assertIn(flags.START_FLYING, second.input_data)
            self.assertIs(bot.abilities.flying, False)

            before_stop = len(wire.sent)
            stop = asyncio.create_task(bot.set_flying(False))
            await asyncio.sleep(0)
            revoked = asyncio.create_task(bot.wait_for_abilities())
            await asyncio.sleep(0)
            wire.push(self.packet(b, -27, ("BASE", may_fly | flying, 0)))
            self.assertIs((await revoked).may_fly, False)
            await clock.wake_at(150)
            with self.assertRaises(PermissionError):
                await stop
            self.assertFalse(any(flags.STOP_FLYING in packet.input_data for packet in wire.sent[before_stop:]))

            before_denial = len(wire.sent)
            with self.assertRaises(PermissionError):
                await bot.set_flying(True)
            self.assertEqual(len(wire.sent), before_denial)

    async def test_unpermitted_flight_input_uses_wire_denial_for_retry(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        flags = b.PlayerAuthInputPacket.InputData
        flight_bits = (1 << int(b.AbilitiesIndex.MAY_FLY)) | (1 << int(b.AbilitiesIndex.FLYING))
        denied = self.packet(b, -27, ("BASE", flight_bits, 0))

        with clock.patch_scheduler():
            await self.join(bot, wire)
            movement, state = bot._movement, bot._player_state
            before = len(wire.sent)
            self.assertIsNone(await asyncio.wait_for(movement.set_flying(False, state, require_permission=False), 1))
            self.assertEqual(len(wire.sent), before)
            self.assertIsNone(bot.abilities.flying)

            next_abilities = asyncio.create_task(bot.wait_for_abilities())
            await asyncio.sleep(0)
            wire.push(denied)
            await next_abilities
            ungranted = bot.abilities
            self.assertIs(ungranted.may_fly, False)
            with self.assertRaises(PermissionError):
                await bot.set_flying(True)
            self.assertEqual(len(wire.sent), before)

            wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
            await bot.wait_for_position_sync()
            start = asyncio.create_task(movement.set_flying(True, state, require_permission=False))
            duplicate = asyncio.create_task(movement.set_flying(True, state, require_permission=False))
            await asyncio.sleep(0)
            await clock.wake_at(50)
            await clock.wake_at(100)
            self.assertEqual(await asyncio.wait_for(asyncio.gather(start, duplicate), 1), [None, None])
            first, release, second = wire.sent[before:]
            self.assertIn(flags.JUMP_PRESSED_RAW, first.input_data)
            self.assertIn(flags.JUMP_RELEASED_RAW, release.input_data)
            self.assertIn(flags.JUMP_PRESSED_RAW, second.input_data)
            self.assertNotIn(flags.START_FLYING, first.input_data)
            self.assertNotIn(flags.START_FLYING, release.input_data)
            self.assertIn(flags.START_FLYING, second.input_data)
            self.assertIs(bot.abilities, ungranted)

            corrected = asyncio.create_task(bot.wait_for_abilities())
            await asyncio.sleep(0)
            wire.push(denied)
            self.assertEqual(await corrected, ungranted)
            retry = asyncio.create_task(movement.set_flying(True, state, require_permission=False))
            await asyncio.sleep(0)
            await clock.wake_at(150)
            await clock.wake_at(200)
            self.assertIsNone(await asyncio.wait_for(retry, 1))
            self.assertEqual(len(wire.sent), before + 6)
            self.assertIn(flags.START_FLYING, wire.sent[-1].input_data)

    async def test_flight_send_failure_and_task_cancellation_leave_target_unsubmitted(self):
        clock = ManualClock()
        bot = Bot("Flight")
        self.addAsyncCleanup(bot.close)
        b = bot.protocol.binding
        wire = Wire(bot.protocol)
        flags = b.PlayerAuthInputPacket.InputData

        with clock.patch_scheduler():
            await self.join(bot, wire)
            movement, state = bot._movement, bot._player_state
            wire.push(b.MovePlayerPacket(player_id=13, pos=b.Vec3(x=0.5, y=101.62, z=0.5), on_ground=True))
            await bot.wait_for_position_sync()
            before = len(wire.sent)

            async def fail_final_send():
                if len(wire.sent) == before + 2:
                    raise OSError("flight send failed")

            wire.before_send = fail_final_send
            failed = asyncio.create_task(movement.set_flying(True, state, require_permission=False))
            await asyncio.sleep(0)
            await clock.wake_at(50)
            await clock.wake_at(100)
            with self.assertRaisesRegex(OSError, "flight send failed"):
                await asyncio.wait_for(failed, 1)
            self.assertEqual(len(wire.sent), before + 2)

            wire.before_send = None
            cancelled = asyncio.create_task(movement.set_flying(True, state, require_permission=False))
            await asyncio.sleep(0)
            self.assertEqual(len(wire.sent), before + 3)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            self.assertFalse(any(flags.START_FLYING in packet.input_data for packet in wire.sent[before:]))

            retry = asyncio.create_task(movement.set_flying(True, state, require_permission=False))
            await asyncio.sleep(0)
            await clock.wake_at(150)
            await clock.wake_at(200)
            self.assertIsNone(await asyncio.wait_for(retry, 1))
            self.assertEqual(len(wire.sent), before + 6)
            self.assertIn(flags.START_FLYING, wire.sent[-1].input_data)
