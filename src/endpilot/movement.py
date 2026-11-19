"""Periodic Bedrock player input with server-origin position synchronization."""

import asyncio


class Movement:
    def __init__(self, packets, start_game):
        self.packets = packets
        self.player_id = int(start_game.runtime_id)
        self.position = (float(start_game.pos.x), float(start_game.pos.y), float(start_game.pos.z))
        self.tick = 0
        self._moving = False
        self._remaining = 0
        self._waiter = None
        self._task = None
        self._unsubscribe = None

    def start(self):
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)
        self.packets.start_receiving()
        self._task = asyncio.create_task(self._run())

    def _on_packet(self, raw):
        b = self.packets.protocol.binding
        if raw.packet_id == b.MovePlayerPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.player_id) != self.player_id:
                return
        elif raw.packet_id == b.CorrectPlayerMovePredictionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.prediction_type) != 0:  # RewindType.PLAYER
                return
        elif raw.packet_id == b.ServerPlayerPostMovePositionPacket.ID:
            packet = self.packets.protocol.decode(raw)
        else:
            return
        self.position = (float(packet.pos.x), float(packet.pos.y), float(packet.pos.z))

    def _frame(self, forward: float):
        b = self.packets.protocol.binding
        x, y, z = self.position
        frame = b.PlayerAuthInputPacket()
        frame.rot = b.Vec2(x=0.0, y=0.0)
        frame.pos = b.Vec3(x=x, y=y, z=z + forward)
        frame.move = b.Vec2(x=0.0, y=1.0 if forward else 0.0)
        frame.y_head_rot = 0.0
        flags = []
        if forward:
            flags = [b.PlayerAuthInputPacket.InputData.UP, b.PlayerAuthInputPacket.InputData.SPRINTING]
            if not self._moving:
                flags.append(b.PlayerAuthInputPacket.InputData.START_SPRINTING)
        elif self._moving:
            flags = [b.PlayerAuthInputPacket.InputData.STOP_SPRINTING]
        frame.input_data = flags
        self._moving = bool(forward)
        frame.input_mode = b.InputMode.MOUSE
        frame.play_mode = b.ClientPlayMode.NORMAL
        frame.new_interaction_model = b.NewInteractionModel.CLASSIC
        frame.interact_rotation = b.Vec2(x=0.0, y=0.0)
        frame.client_tick = self.tick + 1
        frame.pos_delta = b.Vec3(x=0.0, y=0.0, z=forward)
        frame.item_use_transaction = None
        frame.item_stack_request = None
        frame.player_block_actions = None
        frame.vehicle_rot = None
        frame.client_predicted_vehicle = None
        frame.analog_move_vector = frame.move
        frame.camera_orientation = b.Vec3(x=0.0, y=0.0, z=1.0)
        frame.raw_move_vector = frame.move
        self.tick += 1
        return frame

    async def _run(self):
        try:
            while True:
                moving = self._remaining > 0
                frame = self._frame(0.09 if moving else 0.0)
                await self.packets.send(frame)
                self.position = (frame.pos.x, frame.pos.y, frame.pos.z)
                if moving:
                    self._remaining -= 1
                    if self._remaining == 0 and self._waiter is not None:
                        self._waiter.set_result(None)
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            if self._waiter is not None and not self._waiter.done():
                self._waiter.cancel()
            raise
        except Exception as error:
            if self._waiter is not None and not self._waiter.done():
                self._waiter.set_exception(error)
            raise

    async def walk_forward(self, frames: int = 34) -> int:
        """Submit forward frames; return submission count, not BDS-observed position."""
        if self._task is None:
            raise RuntimeError("movement ticks are not running")
        if self._task.done():
            await self._task
        if self._waiter is not None:
            raise RuntimeError("a walk is already in progress")
        if frames <= 0:
            return 0
        self._waiter = asyncio.get_running_loop().create_future()
        self._remaining = frames
        try:
            await self._waiter
            return frames
        finally:
            self._waiter = None

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
