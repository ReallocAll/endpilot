"""Player position observations and a minimal server-authoritative walking input."""

import asyncio


class Movement:
    def __init__(self, packets, player_id):
        self.packets = packets
        self.player_id = int(player_id)
        self.position: tuple[float, float, float] | None = None
        self._input_position: tuple[float, float, float] | None = None
        self._tick = 1
        self._teleport_pending = False
        self._updated = asyncio.Event()
        self._unsubscribe = None
        self._action_lock = asyncio.Lock()
        self._ticker: asyncio.Task | None = None

    def start(self):
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)
        self.packets.start_receiving()
        self._ticker = asyncio.create_task(self._idle_inputs())

    def _on_packet(self, raw):
        b = self.packets.protocol.binding
        if raw.packet_id == b.MovePlayerPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.player_id) != self.player_id:
                return
            if int(packet.reset_position) == 2:
                self._teleport_pending = True
        elif raw.packet_id == b.CorrectPlayerMovePredictionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.prediction_type) != 0:
                return
        elif raw.packet_id == b.ServerPlayerPostMovePositionPacket.ID:
            packet = self.packets.protocol.decode(raw)
        else:
            return
        self.position = (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z))
        self._input_position = self.position
        self._updated.set()

    async def wait_for_position_sync(self, timeout: float = 15) -> tuple[float, float, float]:
        """Wait for the first server position; StartGame may contain a placeholder."""
        async with asyncio.timeout(timeout):
            await self._updated.wait()
        return self.position

    async def _send_input(self, forward: bool):
        """Submit one predicted frame; only a later server observation proves acceptance."""
        b = self.packets.protocol.binding
        x, y, z = self._input_position
        step = 0.09 if forward else 0.0
        next_pos = (x, y, z + step)
        movement = b.Vec2(x=0.0, y=1.0 if forward else 0.0)
        flags = [b.PlayerAuthInputPacket.InputData.UP] if forward else []
        if self._teleport_pending:
            flags.append(b.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT)
        packet = b.PlayerAuthInputPacket(
            rot=b.Vec2(x=0.0, y=0.0),
            pos=b.Vec3(x=next_pos[0], y=next_pos[1], z=next_pos[2]),
            move=movement,
            y_head_rot=0.0,
            input_data=flags,
            input_mode=b.InputMode.MOUSE,
            play_mode=b.ClientPlayMode.NORMAL,
            new_interaction_model=b.NewInteractionModel.CROSSHAIR,
            interact_rotation=b.Vec2(x=0.0, y=0.0),
            client_tick=self._tick,
            pos_delta=b.Vec3(x=0.0, y=0.0, z=step),
            item_use_transaction=None,
            item_stack_request=None,
            player_block_actions=None,
            vehicle_rot=None,
            client_predicted_vehicle=None,
            analog_move_vector=movement,
            camera_orientation=b.Vec3(x=0.0, y=0.0, z=1.0),
            raw_move_vector=movement,
        )
        # A server correction can arrive while send() is suspended.
        # Commit this frame first so it cannot overwrite that correction.
        self._teleport_pending = False
        self._input_position = next_pos
        self._tick += 1
        await self.packets.send(packet)

    async def _idle_inputs(self):
        await self._updated.wait()
        while True:
            async with self._action_lock:
                await self._send_input(False)
            await asyncio.sleep(0.05)

    async def walk_forward(self, ticks: int = 24) -> int:
        """Submit forward input at 20 Hz, then release it. Return submitted moving frames."""
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            try:
                for _ in range(ticks):
                    await self._send_input(True)
                    await asyncio.sleep(0.05)
            finally:
                # Release input after either completion or interruption.
                if self._input_position is not None:
                    await self._send_input(False)
                    await asyncio.sleep(0.05)
                    await self._send_input(False)
            return ticks

    def close(self):
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
