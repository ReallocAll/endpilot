"""Periodic Bedrock player input; this increment submits frames, not an effect receipt."""

import asyncio


class Movement:
    def __init__(self, packets, start_game):
        self.packets = packets
        self.position = (float(start_game.pos.x), float(start_game.pos.y), float(start_game.pos.z))
        self.tick = 0

    def _frame(self, forward: float):
        b = self.packets.protocol.binding
        x, y, z = self.position
        frame = b.PlayerAuthInputPacket()
        frame.rot = b.Vec2(x=0.0, y=0.0)
        frame.pos = b.Vec3(x=x, y=y, z=z + forward)
        frame.move = b.Vec2(x=0.0, y=1.0 if forward else 0.0)
        frame.y_head_rot = 0.0
        frame.input_data = (
            [b.PlayerAuthInputPacket.InputData.UP,
             b.PlayerAuthInputPacket.InputData.SPRINTING,
             b.PlayerAuthInputPacket.InputData.START_SPRINTING]
            if forward and self.tick == 0 else
            [b.PlayerAuthInputPacket.InputData.UP, b.PlayerAuthInputPacket.InputData.SPRINTING]
            if forward else []
        )
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

    async def walk_forward(self, frames: int = 34, *, interval: float = 0.05) -> int:
        """Submit forward input at 20 TPS; return frames sent, not server displacement."""
        for _ in range(frames):
            frame = self._frame(0.09)
            await self.packets.send(frame)
            self.position = (frame.pos.x, frame.pos.y, frame.pos.z)
            await asyncio.sleep(interval)
        # A neutral frame stops input when the caller is done walking.
        await self.packets.send(self._frame(0.0))
        return frames
