"""Session-scoped PlayerAuthInput state and protocol encoding."""

import math
from dataclasses import fields, replace

from .types import Input, Vec2, Vec3


def finite(*values):
    for value in values:
        value_type = type(value)
        if value_type is Vec3:
            if not (math.isfinite(value.x) and math.isfinite(value.y) and math.isfinite(value.z)):
                return False
        elif value_type is Vec2:
            if not (math.isfinite(value.x) and math.isfinite(value.y)):
                return False
        elif isinstance(value, (Vec3, Vec2)):
            if not all(math.isfinite(getattr(value, f.name)) for f in fields(value)):
                return False
        elif not math.isfinite(value):
            return False
    return True


class _PlayerControl:
    """Owns input, look, teleport acknowledgement and stale-command generation."""

    def __init__(self, p, protocol):
        self.p = p
        self.protocol = protocol
        self.input = None
        self.look = None
        self.epoch = 0
        self.handled_teleport = False

    def invalidate(self, *, clear_look):
        self.input = None
        if clear_look:
            self.look = None
        self.epoch += 1

    def acknowledge_teleport(self):
        self.handled_teleport = True

    def set_input(self, frame):
        if not finite(frame.position, frame.delta, frame.move, frame.pitch, frame.yaw):
            raise ValueError("input values must be finite")
        try:
            flags = tuple(frame.flags)
        except TypeError:
            raise ValueError("input flags must be a sequence of native flag names") from None
        enum = self.p.PlayerAuthInputPacket.InputData
        reserved = {
            "INPUT_NUM", "HANDLED_TELEPORT", "VERTICAL_COLLISION",
            "PERFORM_ITEM_INTERACTION", "PERFORM_BLOCK_ACTIONS",
            "PERFORM_ITEM_STACK_REQUEST", "IS_IN_CLIENT_PREDICTED_VEHICLE",
            "INTERNAL_UPDATE",
        }
        if any(
            type(flag) is not str or not flag.isupper() or flag in reserved or not hasattr(enum, flag)
            for flag in flags
        ):
            raise ValueError("invalid or state-owned PlayerAuthInput flag for this protocol")
        if type(frame) is Input:
            self.input = Input(
                frame.position, frame.delta, frame.move, frame.pitch, frame.yaw,
                frame.vertical_collision, frame.jumping, frame.sneaking, frame.sprinting, flags,
            )
        else:
            self.input = replace(frame, flags=flags)

    def set_look(self, pitch, yaw):
        if not finite(pitch, yaw):
            raise ValueError("look values must be finite")
        self.look = Vec2(pitch, yaw)

    def tick_packet(self, status):
        frame = self.input or Input(position=status.position)
        self.input = None
        if self.look is not None:
            frame = replace(frame, pitch=self.look.x, yaw=self.look.y)
        p = self.p
        flags = []
        for enabled, flag in (
            (frame.move.y > 0, "UP"),
            (frame.move.y < 0, "DOWN"),
            (frame.move.x > 0, "RIGHT"),
            (frame.move.x < 0, "LEFT"),
            (frame.vertical_collision, "VERTICAL_COLLISION"),
            (frame.jumping, "JUMPING"),
            (frame.sneaking, "SNEAKING"),
            (frame.sprinting, "SPRINTING"),
            (self.handled_teleport, "HANDLED_TELEPORT"),
        ):
            if enabled:
                flags.append(getattr(p.PlayerAuthInputPacket.InputData, flag))
        for flag in frame.flags:
            value = getattr(p.PlayerAuthInputPacket.InputData, flag)
            if value not in flags:
                flags.append(value)
        if self.protocol == 1001:
            flags = sum(1 << int(flag) for flag in flags)
        tick = status.tick + 1
        rot = p.Vec2(x=frame.pitch, y=frame.yaw)
        move = p.Vec2(x=frame.move.x, y=frame.move.y)
        packet = p.PlayerAuthInputPacket(
            rot=rot,
            pos=self.wire_vec(frame.position),
            move=move,
            y_head_rot=frame.yaw,
            input_data=flags,
            input_mode=p.InputMode.MOUSE,
            play_mode=p.ClientPlayMode.SCREEN,
            new_interaction_model=p.NewInteractionModel.CROSSHAIR,
            interact_rotation=rot,
            client_tick=tick,
            pos_delta=self.wire_vec(frame.delta),
            item_use_transaction=None,
            item_stack_request=None,
            player_block_actions=None,
            vehicle_rot=None,
            client_predicted_vehicle=None,
            analog_move_vector=move,
            camera_orientation=p.Vec3(x=0, y=0, z=0),
            raw_move_vector=move,
        )
        return packet, frame.position, self.epoch

    def tick_enqueued(self):
        self.handled_teleport = False

    def wire_vec(self, value):
        return self.p.Vec3(x=value.x, y=value.y, z=value.z)
