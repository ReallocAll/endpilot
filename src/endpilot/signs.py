"""Server-observed sign data and player-initiated editing."""

import asyncio
from dataclasses import dataclass
from typing import Literal

from bedrock_protocol import nbt

from .blocks import FACE_OFFSETS, HASHED_UNKNOWN_RUNTIME_ID


SignSide = Literal["front", "back"]


def _position(packet) -> tuple[int, int, int]:
    return int(packet.pos.x), int(packet.pos.y), int(packet.pos.z)


@dataclass(frozen=True, slots=True)
class SignObservation:
    position: tuple[int, int, int]
    dimension_id: int
    front_text: str
    back_text: str
    waxed: bool


@dataclass(frozen=True, slots=True, eq=False)
class SignEditor:
    """One sign editor opened by the server for this player."""

    position: tuple[int, int, int]
    dimension_id: int
    side: SignSide


@dataclass(slots=True)
class _EditTarget:
    position: tuple[int, int, int]
    runtime_id: int | None
    invalidated: bool = False


@dataclass(slots=True)
class _SignEditor:
    view: SignEditor
    runtime_id: int | None

    @property
    def position(self) -> tuple[int, int, int]:
        return self.view.position

    @property
    def is_front(self) -> bool:
        return self.view.side == "front"


def _observation(position, dimension_id, tag) -> SignObservation:
    front, back = (tag[key] for key in ("FrontText", "BackText"))
    front_text, back_text, waxed = front["Text"], back["Text"], tag["IsWaxed"]
    if not isinstance(front_text, nbt.StringTag) or not isinstance(back_text, nbt.StringTag):
        raise TypeError("invalid sign text tag")
    if not isinstance(waxed, nbt.ByteTag):
        raise TypeError("invalid sign waxed tag")
    return SignObservation(position, dimension_id, front_text.value, back_text.value, bool(waxed.value))


def _sign_kind(root) -> str | None:
    if root is not None and "id" in root:
        tag = root["id"]
        if isinstance(tag, nbt.StringTag) and tag.value in ("Sign", "HangingSign"):
            return tag.value
    return None


class Signs:
    def __init__(self, packets, blocks, movement, inventory, player_id):
        self.packets = packets
        self.blocks = blocks
        self.movement = movement
        self.inventory = inventory
        self.player_id = player_id
        self._dimension_epoch = 0
        self._editing: _EditTarget | None = None
        self._editor: _SignEditor | None = None
        self._open_waiters: dict[asyncio.Future[SignEditor], tuple[int, int, int] | None] = {}
        self._closed = False
        self._unsubscribe = None
        self._unsubscribe_reader_end = None

    def start(self) -> None:
        # Whole-section packets must reach the shared block cache first.
        self._unsubscribe = self.packets.subscribe_raw(self._on_packet)
        self._unsubscribe_reader_end = self.packets.subscribe_reader_end(self._end_session)

    def close(self) -> None:
        self._end_session()

    def _end_session(self, cause: BaseException | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._editor = None
        self._dimension_epoch += 1
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._unsubscribe_reader_end is not None:
            self._unsubscribe_reader_end()
            self._unsubscribe_reader_end = None
        for future in self._open_waiters:
            if not future.done():
                error = ConnectionError("sign editor session is closed")
                error.__cause__ = cause
                future.set_exception(error)
                future.exception()
        self._open_waiters.clear()

    def _sync_reader_end(self) -> None:
        if not self._closed:
            ended, cause = self.packets._reader_end_result()
            if ended:
                self._end_session(cause)

    def _require_live(self) -> None:
        self._sync_reader_end()
        if self._closed:
            raise ConnectionError("sign editor session is closed")

    @property
    def editor(self) -> SignEditor | None:
        self._sync_reader_end()
        return None if self._editor is None else self._editor.view

    async def wait_for_open(self, *, position: tuple[int, int, int] | None, timeout: float | None) -> SignEditor:
        self._require_live()
        if position is not None:
            position = tuple(position)
            if len(position) != 3 or any(type(axis) is not int for axis in position):
                raise ValueError("sign position must contain three integer coordinates")
        future = asyncio.get_running_loop().create_future()
        self._open_waiters[future] = position
        try:
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self._open_waiters.pop(future, None)

    def _invalidate_editor(self, position) -> None:
        if self._editor is not None and self._editor.position == position:
            self._editor = None
        if self._editing is not None and self._editing.position == position:
            self._editing.invalidated = True

    def _project(self, position, root) -> SignObservation | None:
        if _sign_kind(root) is None:
            return None
        try:
            return _observation(position, self.blocks.dimension_id, root)
        except (KeyError, TypeError):
            return None

    def _on_block_entity(self, position, previous, root) -> None:
        if self._closed:
            return
        active = tuple(target for target in (self._editing, self._editor)
                       if target is not None and target.position == position)
        if not active:
            return
        previous_kind = _sign_kind(previous)
        if (self._project(position, root) is None
                or (previous_kind is not None and _sign_kind(root) != previous_kind)):
            self._invalidate_editor(position)
            return
        block = self.blocks.observed_block(position)
        if any(target.runtime_id is not None and (block is None or block.runtime_id != target.runtime_id)
               for target in active):
            self._invalidate_editor(position)
        elif self._editor is not None and self._editor.position == position and block is not None:
            self._editor.runtime_id = block.runtime_id

    def _local_respawn_transition(self, raw) -> bool:
        protocol = self.packets.protocol
        packet = protocol.decode(raw)
        states = protocol.binding.PlayerRespawnState
        # Clientbound runtime ID 0 identifies the receiving player.
        return packet.runtime_id in (0, self.player_id) and packet.state in (
            states.SEARCHING_FOR_SPAWN, states.READY_TO_SPAWN,
        )

    def _on_packet(self, raw) -> None:
        if self._closed:
            return
        protocol = self.packets.protocol
        binding = protocol.binding
        if raw.packet_id == binding.OpenSignPacket.ID:
            packet = protocol.decode(raw)
            position = _position(packet)
            side = "front" if packet.is_front_side else "back"
            view = SignEditor(position, self.blocks.dimension_id, side)
            block = self.blocks.observed_block(position)
            self._editor = (
                _SignEditor(view, None if block is None else block.runtime_id)
                if self.blocks._column_is_published(position[0] >> 4, position[2] >> 4) else None
            )
            for future, requested in tuple(self._open_waiters.items()):
                if not future.done() and (requested is None or requested == position):
                    future.set_result(view)
                    self._open_waiters.pop(future)
        elif raw.packet_id == binding.ChangeDimensionPacket.ID:
            self._editor = None
            self._dimension_epoch += 1
        elif raw.packet_id == binding.RespawnPacket.ID and self._local_respawn_transition(raw):
            self._editor = None
            if self._editing is not None:
                self._editing.invalidated = True
        elif raw.packet_id == binding.DisconnectPacket.ID:
            self._end_session()
        elif raw.packet_id == binding.NetworkChunkPublisherUpdatePacket.ID and self._editor is not None:
            x, _, z = self._editor.position
            if not self.blocks._column_is_published(x >> 4, z >> 4):
                self._invalidate_editor(self._editor.position)
        elif raw.packet_id in (binding.SubChunkPacket.ID, binding.LevelChunkPacket.ID):
            editor = self._editor
            if editor is not None:
                # A section may replace the block when no actor root was
                # available to trigger the block-entity observer.
                block = self.blocks.observed_block(editor.position)
                if (block is not None and block.is_air
                        or editor.runtime_id is not None and (block is None or block.runtime_id != editor.runtime_id)):
                    self._invalidate_editor(editor.position)
                elif block is not None:
                    editor.runtime_id = block.runtime_id

    def observed(self, position: tuple[int, int, int]) -> SignObservation | None:
        return self._project(position, self.blocks._block_entity(position))

    def _editable_sign(self, position):
        data = self.blocks._block_entity(position)
        observation = self._project(position, data)
        if observation is None:
            raise LookupError(f"no current server sign data at {position}")
        if observation.waxed:
            raise PermissionError(f"sign at {position} is waxed")
        return data

    def _current_editor(self) -> _SignEditor:
        self._require_live()
        if self._editing is not None:
            raise RuntimeError("sign edit already in progress")
        editor = self._editor
        if editor is None:
            raise RuntimeError("no sign editor is open")
        return editor

    def _editor_data(self, editor: _SignEditor):
        self._require_live()
        if self._editor is not editor or editor.view.dimension_id != self.blocks.dimension_id:
            raise RuntimeError("sign editor changed during edit")
        if not self.blocks._column_is_published(editor.position[0] >> 4, editor.position[2] >> 4):
            self._invalidate_editor(editor.position)
            raise RuntimeError("sign target changed during edit")
        block = self.blocks.observed_block(editor.position)
        if (block is None or block.runtime_id is None
                or (self.blocks.hashed_runtime_ids and block.runtime_id == HASHED_UNKNOWN_RUNTIME_ID)):
            raise LookupError(f"no current server block at {editor.position}")
        if block.is_air or (editor.runtime_id is not None and block.runtime_id != editor.runtime_id):
            self._invalidate_editor(editor.position)
            raise RuntimeError("sign target changed during edit")
        return self._editable_sign(editor.position)

    async def submit_text(self, view: SignEditor, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("sign text must be a string")
        editor = self._current_editor()
        if editor.view is not view:
            raise RuntimeError("sign editor changed during edit")
        front, back = (text, None) if editor.is_front else (None, text)
        await self._submit_editor_text(editor, front, back)

    async def close_editor(self, text: str, other_side_text: str | None = None) -> None:
        editor = self._current_editor()
        front, back = (text, other_side_text) if editor.is_front else (other_side_text, text)
        await self._submit_editor_text(editor, front, back)

    async def set_editor_text(self, pos: list[int], front_text: str, back_text: str = "") -> None:
        editor = self._current_editor()
        if editor.position != tuple(pos):
            raise RuntimeError("no sign editor is open at the requested position")
        await self._submit_editor_text(editor, front_text, back_text)

    async def _submit_editor_text(self, editor: _SignEditor, front: str | None, back: str | None) -> None:
        editing = self._editing
        root = self._editor_data(editor)
        data = nbt.CompoundTag(root)
        for key, text in (("FrontText", front), ("BackText", back)):
            if text is not None:
                side = nbt.CompoundTag(data[key])
                side["Text"] = nbt.StringTag(text)
                data[key] = side

        def before_send():
            if self._editing is not editing or self._editor_data(editor) is not root:
                raise RuntimeError("sign data changed before submission")
            self._editor = None
            # BDS can broadcast the new text to nearby observers without echoing it here.
            self.blocks._set_block_entity(editor.position, None)

        b = self.packets.protocol.binding
        x, y, z = editor.position
        await self.packets.send(b.BlockActorDataPacket(pos=b.BlockPos(x=x, y=y, z=z), data=data),
                                _before_send=before_send)

    async def edit(self, position: tuple[int, int, int], text: str, *, face: int,
                   timeout: float) -> SignSide:
        self._require_live()
        if self._editing is not None:
            raise RuntimeError("sign edit already in progress")
        if face not in FACE_OFFSETS:
            raise ValueError("sign face must be in 0..5")
        self._editable_sign(position)

        block = self.blocks.observed_block(position)
        target = _EditTarget(position, None if block is None else block.runtime_id)
        self._editing = target
        epoch = self._dimension_epoch
        try:
            return await self._edit(target, text, face, timeout, epoch)
        finally:
            self._editing = None

    def _check_edit_target(self, target: _EditTarget, epoch: int) -> None:
        if self._dimension_epoch != epoch:
            raise RuntimeError("dimension changed during sign edit")
        if target.invalidated:
            raise RuntimeError("sign target changed during edit")

    async def _edit(self, target: _EditTarget, text, face, timeout, epoch) -> SignSide:
        protocol = self.packets.protocol
        b = protocol.binding
        position = target.position
        px, py, pz = position
        dx, dy, dz = FACE_OFFSETS[face]
        pos = b.BlockPos(x=px, y=py, z=pz)
        adjacent = b.BlockPos(x=px + dx, y=py + dy, z=pz + dz)
        x, y, z = await self.movement.wait_for_position_sync(timeout)
        await self.movement.look_at((px + 0.5, py + 0.75, pz + 0.5))

        opened: _SignEditor | None = None

        def matches(raw):
            nonlocal opened
            if raw.packet_id == b.ChangeDimensionPacket.ID:
                return True
            if raw.packet_id == b.RespawnPacket.ID:
                return self._local_respawn_transition(raw)
            if _position(protocol.decode(raw)) != position:
                return False
            # The sign observer ran for this packet. Capture its editor before
            # the reader delivers any other packets in the same batch.
            opened = self._editor
            return True

        pending = asyncio.create_task(self.packets.wait_for_packet(
            (b.OpenSignPacket.ID, b.ChangeDimensionPacket.ID, b.RespawnPacket.ID), timeout=timeout, match=matches,
        ))
        started = False
        failure = None
        try:
            await asyncio.sleep(0)
            self._check_edit_target(target, epoch)
            self._editable_sign(position)
            block = self.blocks.observed_block(position)
            if block is None or block.runtime_id is None or block.is_air:
                raise LookupError(f"no current server block at {position}")
            target.runtime_id = block.runtime_id
            started = True
            await self.packets.send(b.PlayerActionPacket(
                runtime_id=self.player_id, action=b.PlayerActionType.START_ITEM_USE_ON,
                pos=pos, result_pos=adjacent, face=face,
            ))
            await self.packets.send(b.AnimatePacket(
                runtime_id=self.player_id, action=b.AnimatePacket.Action.SWING,
                data=0.0, swing_source=b.ActorSwingSource.INTERACT,
            ))
            self._check_edit_target(target, epoch)
            slot = self.inventory.selected_slot
            transaction = b.ItemUseInventoryTransaction(
                transaction=b.InventoryTransaction(actions=[]),
                action_type=b.ItemUseInventoryTransaction.ActionType.PLACE,
                trigger_type=b.ItemUseInventoryTransaction.TriggerType.PLAYER_INPUT,
                pos=pos, face=face, slot=slot, hand=b.HandSlot.MAINHAND,
                item=self.inventory.item_or_empty(slot), from_pos=b.Vec3(x=x, y=y, z=z),
                click_pos=b.Vec3(x=0.5 + dx * 0.25, y=0.75 + dy * 0.20, z=0.5 + dz * 0.25),
                target_block_id=block.runtime_id,
                client_predicted_result=b.ItemUseInventoryTransaction.PredictedResult.SUCCESS,
                client_cooldown_state=b.ItemUseInventoryTransaction.ClientCooldownState.OFF,
            )
            await self.packets.send(b.InventoryTransactionPacket(
                legacy_request_id=b.ItemStackLegacyRequestId(raw_id=0),
                legacy_set_item_slots=None, variant_transaction=transaction,
            ))
            await pending
            self._check_edit_target(target, epoch)
            if opened is None:
                raise RuntimeError("sign editor changed during edit")
            front, back = (text, None) if opened.is_front else (None, text)
            await self._submit_editor_text(opened, front, back)
            return "front" if opened.is_front else "back"
        except BaseException as error:
            failure = error
            raise
        finally:
            if opened is not None and self._editor is opened:
                self._editor = None
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if started and self._dimension_epoch == epoch:
                try:
                    await self.packets.send(b.PlayerActionPacket(
                        runtime_id=self.player_id, action=b.PlayerActionType.STOP_ITEM_USE_ON,
                        pos=adjacent, result_pos=b.BlockPos(x=0, y=0, z=0), face=0,
                    ))
                except (Exception, asyncio.CancelledError) as error:
                    if failure is None:
                        raise
                    failure.add_note(f"sign edit cleanup failed: {error!r}")
