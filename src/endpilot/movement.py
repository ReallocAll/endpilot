"""Player position observations and a minimal server-authoritative walking input."""

import asyncio
import math
from collections import deque


class Movement:
    def __init__(self, packets, player_id):
        self.packets = packets
        self.player_id = int(player_id)
        self.position: tuple[float, float, float] | None = None
        self._input_position: tuple[float, float, float] | None = None
        self._rotation = (0.0, 0.0)  # pitch, yaw in degrees
        self._tick = 1
        self._teleport_pending = False
        self._sneaking = False
        self._sprint_key = False
        self._sprinting = False
        self._jump_floor_y: float | None = None
        self._jump_velocity = 0.0
        self._jump_was_pressed = False
        self._updated = asyncio.Event()
        self._unsubscribe = None
        self._action_lock = asyncio.Lock()
        self._stop_requested = False
        self._active_done: asyncio.Event | None = None
        self._follow_target_id: int | None = None
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
                self._jump_floor_y = None
                self._jump_velocity = 0.0
        elif raw.packet_id == b.CorrectPlayerMovePredictionPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if int(packet.prediction_type) != 0:
                return
        elif raw.packet_id == b.ServerPlayerPostMovePositionPacket.ID:
            packet = self.packets.protocol.decode(raw)
        elif raw.packet_id == b.RespawnPacket.ID:
            packet = self.packets.protocol.decode(raw)
            if packet.state != b.PlayerRespawnState.READY_TO_SPAWN:
                return
            self._jump_floor_y = None
            self._jump_velocity = 0.0
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

    async def _send_input(self, forward: bool, item_use_transaction=None, block_actions=(),
                          sneak_changed: bool = False, jump_pressed: bool = False,
                          backward: bool = False):
        """Submit one predicted frame; only a later server observation proves acceptance."""
        b = self.packets.protocol.binding
        x, y, z = self._input_position
        pitch, yaw = self._rotation
        y_rad = math.radians(yaw)
        p_rad = math.radians(pitch)
        # Sprint key is held; the sprint state starts only during forward movement.
        sprinting = self._sprint_key and forward and not self._sneaking
        sprint_changed = sprinting != self._sprinting
        self._sprinting = sprinting
        scale = 0.3 if self._sneaking else 1.0
        direction = 1 if forward else -1 if backward else 0
        step = 0.09 * scale * (1.3 if sprinting else 1.0) * direction
        dx, dz = -math.sin(y_rad) * step, math.cos(y_rad) * step
        # Grounded jump follows the upstream client's vertical travel:
        # 0.42 launch velocity, then gravity 0.08 and vertical drag 0.98.
        if jump_pressed and self._jump_floor_y is None:
            self._jump_floor_y = y
            self._jump_velocity = 0.42
        next_y = y
        if self._jump_floor_y is not None:
            next_y = max(self._jump_floor_y, y + self._jump_velocity)
            if next_y <= self._jump_floor_y:
                self._jump_floor_y = None
                self._jump_velocity = 0.0
            else:
                self._jump_velocity = (self._jump_velocity - 0.08) * 0.98
        dy = next_y - y
        next_pos = (x + dx, next_y, z + dz)
        movement = b.Vec2(x=0.0, y=scale * direction)
        flags = ([b.PlayerAuthInputPacket.InputData.UP] if forward else
                 [b.PlayerAuthInputPacket.InputData.DOWN] if backward else [])
        if self._sneaking:
            flags.extend((
                b.PlayerAuthInputPacket.InputData.SNEAKING,
                b.PlayerAuthInputPacket.InputData.SNEAK_DOWN,
                b.PlayerAuthInputPacket.InputData.SNEAK_CURRENT_RAW,
                b.PlayerAuthInputPacket.InputData.WANT_DOWN,
            ))
        if self._sprinting:
            flags.append(b.PlayerAuthInputPacket.InputData.SPRINTING)
        if self._sprint_key:
            flags.append(b.PlayerAuthInputPacket.InputData.SPRINT_DOWN)
        if sprint_changed:
            flags.append(
                b.PlayerAuthInputPacket.InputData.START_SPRINTING
                if sprinting else b.PlayerAuthInputPacket.InputData.STOP_SPRINTING
            )
        if jump_pressed:
            flags.extend((
                b.PlayerAuthInputPacket.InputData.JUMPING,
                b.PlayerAuthInputPacket.InputData.JUMP_DOWN,
                b.PlayerAuthInputPacket.InputData.JUMP_CURRENT_RAW,
                b.PlayerAuthInputPacket.InputData.WANT_UP,
                b.PlayerAuthInputPacket.InputData.JUMP_PRESSED_RAW,
            ))
            if dy > 0 and self._jump_floor_y is not None and y == self._jump_floor_y:
                flags.append(b.PlayerAuthInputPacket.InputData.START_JUMPING)
        elif self._jump_was_pressed:
            flags.append(b.PlayerAuthInputPacket.InputData.JUMP_RELEASED_RAW)
        self._jump_was_pressed = jump_pressed
        if sneak_changed:
            flags.extend((
                (b.PlayerAuthInputPacket.InputData.START_SNEAKING,
                 b.PlayerAuthInputPacket.InputData.SNEAK_PRESSED_RAW)
                if self._sneaking else
                (b.PlayerAuthInputPacket.InputData.STOP_SNEAKING,
                 b.PlayerAuthInputPacket.InputData.SNEAK_RELEASED_RAW)
            ))
        if item_use_transaction is not None:
            flags.append(b.PlayerAuthInputPacket.InputData.PERFORM_ITEM_INTERACTION)
        if block_actions:
            flags.append(b.PlayerAuthInputPacket.InputData.PERFORM_BLOCK_ACTIONS)
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
            pos_delta=b.Vec3(x=dx, y=dy, z=dz),
            item_use_transaction=item_use_transaction,
            item_stack_request=None,
            player_block_actions=[
                b.PlayerBlockActionData(
                    player_action_type=getattr(b.PlayerActionType, action),
                    pos=b.BlockPos(x=pos[0], y=pos[1], z=pos[2]),
                    facing=face,
                )
                for action, pos, face in block_actions
            ] or None,
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

    async def set_sneaking(self, sneaking: bool) -> int:
        """Submit a held sneak/stand state; return the submitted client tick.

        This does not imply that BDS accepted the state. Subsequent input
        frames keep the same held state until another call changes it.
        """
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            changed = self._sneaking != sneaking
            self._sneaking = sneaking
            return await self._send_input(False, sneak_changed=changed)

    async def set_sprinting(self, sprinting: bool) -> int:
        """Hold or release sprint input for subsequent forward-moving frames.

        Sprinting begins only with forward movement and ends when movement stops
        or sneaking starts. Return the submitted tick, not BDS acceptance.
        """
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            self._sprint_key = sprinting
            return await self._send_input(False)

    async def jump(self) -> int:
        """Submit a one-tick jump press and predicted vertical motion.

        Return the submitted tick, not proof of BDS displacement. The idle
        input stream continues the trajectory and releases the press.
        """
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            return await self._send_input(False, jump_pressed=True)

    async def stop_moving(self) -> None:
        """Stop the currently active walk/follow and wait for its release frames."""
        done = self._active_done
        if done is not None:
            self._stop_requested = True
            await done.wait()

    async def walk_forward(self, ticks: int = 24) -> int:
        """Submit forward input at 20 Hz, then release it. Return submitted moving frames."""
        return await self._walk(ticks, backward=False)

    async def walk_backward(self, ticks: int = 24) -> int:
        """Submit backward input at 20 Hz without turning; return submitted frames."""
        return await self._walk(ticks, backward=True)

    async def _walk(self, ticks: int, *, backward: bool) -> int:
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
                    await self._send_input(not backward, backward=backward)
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

    def switch_follow_target(self, runtime_id: int) -> None:
        """Use a new runtime ID on the next active follow frame."""
        if self._follow_target_id is None or self._stop_requested:
            raise RuntimeError("no active entity follow")
        self._follow_target_id = runtime_id

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
            self._follow_target_id = runtime_id
            done = self._active_done = asyncio.Event()
            submitted = 0
            try:
                if self._input_position is None:
                    await self.wait_for_position_sync()
                for _ in range(ticks):
                    if self._stop_requested:
                        break
                    actor = entities.get(self._follow_target_id)
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
                self._follow_target_id = None
                try:
                    if self._input_position is not None:
                        await self._send_input(False)
                        await asyncio.sleep(0.05)
                        await self._send_input(False)
                finally:
                    self._active_done = None
                    done.set()


    @staticmethod
    def _flat_route(blocks, start, goal, feet_y, radius):
        """Find a route through observed, two-block-high air above known floor."""
        def clear(cell):
            x, z = cell
            body = blocks.observed_block((x, feet_y, z))
            head = blocks.observed_block((x, feet_y + 1, z))
            floor = blocks.observed_block((x, feet_y - 1, z))
            return (body is not None and body.is_air is True
                    and head is not None and head.is_air is True
                    and floor is not None and floor.is_air is False)

        if not clear(start):
            raise RuntimeError("starting terrain is not known walkable")
        queue = deque([start])
        previous = {start: None}
        while queue:
            current = queue.popleft()
            if current == goal:
                route = []
                while previous[current] is not None:
                    route.append(current)
                    current = previous[current]
                return list(reversed(route))
            for dx, dz in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                neighbor = (current[0] + dx, current[1] + dz)
                if (neighbor in previous
                        or abs(neighbor[0] - start[0]) > radius
                        or abs(neighbor[1] - start[1]) > radius
                        or not clear(neighbor)):
                    continue
                previous[neighbor] = current
                queue.append(neighbor)
        raise RuntimeError("no route through observed flat terrain")

    async def navigate_to(self, blocks, target: tuple[float, float, float], *,
                          ticks: int = 160, search_radius: int = 8) -> int:
        """Walk to a block's center on observed flat ground.

        Unknown cells are impassable. Return submitted moving input frames,
        not a server-confirmed arrival. Stop/cancellation releases input.
        """
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            x, y, z = self._input_position
            # PlayerAuthInput/MovePlayer positions are eye-high (EndstoneMC/client).
            feet_y = math.floor(y - 1.62 + 0.001)
            if math.floor(target[1]) != feet_y:
                raise ValueError("flat navigation cannot change elevation")
            start = (math.floor(x), math.floor(z))
            goal = (math.floor(target[0]), math.floor(target[2]))
            route = self._flat_route(blocks, start, goal, feet_y, search_radius)
            self._stop_requested = False
            done = self._active_done = asyncio.Event()
            submitted = 0
            try:
                for cell_x, cell_z in route:
                    while submitted < ticks:
                        if self._stop_requested:
                            return submitted
                        px, py, pz = self._input_position
                        waypoint = (cell_x + 0.5, py, cell_z + 0.5)
                        if math.hypot(waypoint[0] - px, waypoint[2] - pz) <= 0.12:
                            break
                        # Do not enter a cell that changed or became unknown.
                        body = blocks.observed_block((cell_x, feet_y, cell_z))
                        head = blocks.observed_block((cell_x, feet_y + 1, cell_z))
                        floor = blocks.observed_block((cell_x, feet_y - 1, cell_z))
                        if (body is None or body.is_air is not True
                                or head is None or head.is_air is not True
                                or floor is None or floor.is_air is not False):
                            return submitted
                        await self.look_at(waypoint)
                        await self._send_input(True)
                        submitted += 1
                        await asyncio.sleep(0.05)
                    else:
                        return submitted
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

    async def send_block_actions(self, actions) -> int:
        """Submit one or more block actions in a single player input tick."""
        async with self._action_lock:
            if self._input_position is None:
                await self.wait_for_position_sync()
            return await self._send_input(False, block_actions=actions)

    def close(self):
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
