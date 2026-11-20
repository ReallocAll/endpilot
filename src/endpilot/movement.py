"""Player position observations and a minimal server-authoritative walking input."""

import asyncio
import math


class Movement:
    def __init__(self, packets, player_id):
        self.packets = packets
        self.player_id = int(player_id)
        self.position: tuple[float, float, float] | None = None
        self._input_position: tuple[float, float, float] | None = None
        self._rotation = (0.0, 0.0)  # pitch, yaw in degrees
        self._tick = 1
        self._teleport_pending = False
        self._updated = asyncio.Event()
        self._unsubscribe = None
        self._action_lock = asyncio.Lock()
        self._stop_requested = False
        self._active_done: asyncio.Event | None = None
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

    async def look_at(self, target: tuple[float, float, float]) -> tuple[float, float]:
        """Set the facing direction for subsequent input; return (pitch, yaw)."""
        if self._input_position is None:
            await self.wait_for_position_sync()
        x, y, z = self._input_position
        dx, dy, dz = (target[0] - x, target[1] - y, target[2] - z)
        yaw = math.degrees(math.atan2(-dx, dz))
        pitch = math.degrees(-math.atan2(dy, math.hypot(dx, dz)))
        self._rotation = (pitch, yaw)
        return self._rotation

    async def _send_input(self, forward: bool, item_use_transaction=None):
        """Submit one predicted frame; only a later server observation proves acceptance."""
        b = self.packets.protocol.binding
        x, y, z = self._input_position
        pitch, yaw = self._rotation
        y_rad = math.radians(yaw)
        p_rad = math.radians(pitch)
        step = 0.09 if forward else 0.0
        dx, dz = -math.sin(y_rad) * step, math.cos(y_rad) * step
        next_pos = (x + dx, y, z + dz)
        movement = b.Vec2(x=0.0, y=1.0 if forward else 0.0)
        flags = [b.PlayerAuthInputPacket.InputData.UP] if forward else []
        if item_use_transaction is not None:
            flags.append(b.PlayerAuthInputPacket.InputData.PERFORM_ITEM_INTERACTION)
        if self._teleport_pending:
            flags.append(b.PlayerAuthInputPacket.InputData.HANDLED_TELEPORT)
        packet = b.PlayerAuthInputPacket(
            rot=b.Vec2(x=pitch, y=yaw),
            pos=b.Vec3(x=next_pos[0], y=next_pos[1], z=next_pos[2]),
            move=movement,
            y_head_rot=yaw,
            input_data=flags,
            input_mode=b.InputMode.MOUSE,
            play_mode=b.ClientPlayMode.NORMAL,
            new_interaction_model=b.NewInteractionModel.CROSSHAIR,
            interact_rotation=b.Vec2(x=pitch, y=yaw),
            client_tick=self._tick,
            pos_delta=b.Vec3(x=dx, y=0.0, z=dz),
            item_use_transaction=item_use_transaction,
            item_stack_request=None,
            player_block_actions=None,
            vehicle_rot=None,
            client_predicted_vehicle=None,
            analog_move_vector=movement,
            camera_orientation=b.Vec3(
                x=-math.sin(y_rad) * math.cos(p_rad),
                y=-math.sin(p_rad),
                z=math.cos(y_rad) * math.cos(p_rad),
            ),
            raw_move_vector=movement,
        )
        # A server correction can arrive while send() is suspended.
        # Commit this frame first so it cannot overwrite that correction.
        self._teleport_pending = False
        self._input_position = next_pos
        self._tick += 1
        await self.packets.send(packet)
        return self._tick - 1

    async def _idle_inputs(self):
        await self._updated.wait()
        while True:
            async with self._action_lock:
                await self._send_input(False)
            await asyncio.sleep(0.05)

    async def stop_moving(self) -> None:
        """Stop the currently active walk/follow and wait for its release frames."""
        done = self._active_done
        if done is not None:
            self._stop_requested = True
            await done.wait()

    async def walk_forward(self, ticks: int = 24) -> int:
        """Submit forward input at 20 Hz, then release it. Return submitted moving frames."""
        async with self._action_lock:
            self._stop_requested = False
            done = self._active_done = asyncio.Event()
            submitted = 0
            try:
                if self._input_position is None:
                    await self.wait_for_position_sync()
                for _ in range(ticks):
                    if self._stop_requested:
                        break
                    await self._send_input(True)
                    submitted += 1
                    await asyncio.sleep(0.05)
                return submitted
            finally:
                try:
                    # Release input after completion, stop or cancellation.
                    if self._input_position is not None:
                        await self._send_input(False)
                        await asyncio.sleep(0.05)
                        await self._send_input(False)
                finally:
                    self._active_done = None
                    done.set()

    async def follow_entity(self, entities, runtime_id: int, *, ticks: int = 80,
                            stop_distance: float = 1.5) -> int:
        """Track an observed actor for up to ticks frames, releasing input on exit.

        Return submitted moving frames, not a server-confirmed arrival.
        The route is not obstacle-aware; a missing actor ends tracking.
        """
        if not math.isfinite(stop_distance) or stop_distance < 0:
            raise ValueError("stop_distance must be finite and nonnegative")
        async with self._action_lock:
            self._stop_requested = False
            done = self._active_done = asyncio.Event()
            submitted = 0
            try:
                if self._input_position is None:
                    await self.wait_for_position_sync()
                for _ in range(ticks):
                    if self._stop_requested:
                        break
                    actor = entities.get(runtime_id)
                    if actor is None:
                        break
                    x, y, z = self._input_position
                    dx, dz = actor.position[0] - x, actor.position[2] - z
                    forward = math.hypot(dx, dz) > stop_distance
                    if forward:
                        await self.look_at((actor.position[0], y, actor.position[2]))
                        submitted += 1
                    await self._send_input(forward)
                    await asyncio.sleep(0.05)
                return submitted
            finally:
                try:
                    if self._input_position is not None:
                        await self._send_input(False)
                        await asyncio.sleep(0.05)
                        await self._send_input(False)
                finally:
                    self._active_done = None
                    done.set()

    async def send_item_use(self, transaction) -> int:
        """Serialize an item action in the same client-tick stream as movement."""
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            return await self._send_input(False, item_use_transaction=transaction)

    def close(self):
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
