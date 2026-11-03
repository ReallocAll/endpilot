"""Session-scoped self-player transitions and pre-StartGame replay."""

from collections import deque
from dataclasses import dataclass, replace

from .types import Vec3


@dataclass(frozen=True, slots=True)
class _InitialPosition:
    runtime_id: int
    position: Vec3
    teleport: bool
    acknowledge: bool


@dataclass(frozen=True, slots=True)
class _InitialHealth:
    runtime_id: int | None
    value: float


class _PlayerLifecycle:
    """Own the ordered early-state buffer and respawn negotiation phase."""

    def __init__(self, p):
        self.p = p
        self.pending = deque()
        self.respawn_phase = 0

    def defer(self, effect):
        if len(self.pending) >= 4096:
            return False
        self.pending.append(effect)
        return True

    def position_effect(self, packet):
        mode = packet.reset_position
        position_mode = self.p.PlayerPositionModeComponent.PositionMode
        return _InitialPosition(
            packet.player_id,
            Vec3(packet.pos.x, packet.pos.y, packet.pos.z),
            mode == position_mode.TELEPORT,
            mode in (position_mode.TELEPORT, position_mode.RESPAWN),
        )

    def take(self, runtime_id):
        while self.pending:
            effect = self.pending.popleft()
            if effect.runtime_id is None or effect.runtime_id == runtime_id:
                return effect
        return None

    def clear_pending(self):
        self.pending.clear()

    def health_transition(self, status, value):
        died = value <= 0 and status.alive
        respawned = value > 0 and not status.alive and self.respawn_phase == 2
        if died:
            status = replace(status, health=value, alive=False, selected_hotbar=None)
        elif respawned:
            status = replace(status, health=value, alive=True)
        else:
            status = replace(status, health=value)
        return status, died, respawned

    def respawn_intent(self, alive, retry):
        if alive:
            raise RuntimeError("bot is alive")
        if self.respawn_phase and not retry:
            return 0
        return 2 if self.respawn_phase == 2 else 1
