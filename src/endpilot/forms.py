"""Track server-issued forms and send native Bedrock responses."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from bedrock_protocol import ClientboundCloseFormPacket, ModalFormRequestPacket

from .compression import PacketConnection
from .protocol import RawPacket


FORM_PACKET_IDS = (ModalFormRequestPacket.ID, ClientboundCloseFormPacket.ID)


@dataclass(slots=True)
class _PendingForm:
    form_id: int
    body: Any
    request: RawPacket | None


def _custom_value(control: Any, value: Any) -> Any:
    kind = control.get("type") if isinstance(control, dict) else None
    if kind in ("label", "header", "divider"):
        return None
    if kind == "toggle":
        valid = isinstance(value, bool)
    elif kind == "input":
        valid = isinstance(value, str)
    elif kind in ("dropdown", "step_slider"):
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "slider":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        raise ValueError(f"unknown custom form control {kind!r}")
    if not valid:
        raise ValueError(f"invalid response for a {kind} control")
    return value


def _encode_response(form: Any, response: Any) -> str:
    kind = form.get("type") if isinstance(form, dict) else None
    if kind == "modal":
        if not isinstance(response, bool):
            raise ValueError("a confirmation form expects a boolean")
    elif kind == "form":
        if "elements" in form:
            elements = form["elements"]
            if not isinstance(elements, list):
                raise ValueError("invalid action form elements")
            count = sum(isinstance(element, dict) and element.get("type") == "button" for element in elements)
        else:
            buttons = form.get("buttons")
            count = len(buttons) if isinstance(buttons, list) else 0
        if isinstance(response, bool) or not isinstance(response, int) or not 0 <= response < count:
            raise ValueError("button index is outside the server's action form")
    elif kind == "custom_form":
        controls = form.get("content", [])
        if not isinstance(controls, list) or not isinstance(response, (tuple, list)) or len(response) != len(controls):
            raise ValueError("a custom form expects one value per control")
        response = [_custom_value(control, value) for control, value in zip(controls, response)]
    else:
        raise ValueError(f"unknown form type {kind!r}")
    return json.dumps(response, separators=(",", ":"), allow_nan=False)


class Forms:
    def __init__(self, packets: PacketConnection, initial_packets):
        self._packets = packets
        self._binding = packets.protocol.binding
        self._pending: dict[int, _PendingForm] = {}
        self._submitting = asyncio.Lock()
        for packet in initial_packets:
            self._apply(packet)
        self._unsubscribe = packets.subscribe_raw(self._on_packet)

    def _on_packet(self, raw: RawPacket) -> None:
        if raw.packet_id == self._binding.DisconnectPacket.ID:
            self.close()
        elif raw.packet_id in FORM_PACKET_IDS:
            self._apply(self._packets.protocol.decode(raw), raw)

    def _apply(self, packet, request: RawPacket | None = None) -> None:
        if packet.ID == ClientboundCloseFormPacket.ID:
            self._pending.clear()
            return
        try:
            form = json.loads(packet.form_json)
        except ValueError:
            form = None
        form_id = int(packet.form_id)
        # An ID reused by the server is the newest request, even if still pending.
        self._pending.pop(form_id, None)
        self._pending[form_id] = _PendingForm(form_id, form, request)

    def _find(self, form_id: int | RawPacket | None) -> _PendingForm | None:
        if form_id is None:
            return next(reversed(self._pending.values()), None)
        if isinstance(form_id, RawPacket):
            # The same ID and bytes can arrive as a new request in this session.
            return next((form for form in self._pending.values() if form.request is form_id), None)
        return self._pending.get(form_id)

    def get_pending(self, form_id: int | RawPacket | None) -> dict | None:
        form = self._find(form_id)
        return None if form is None else {"form_id": form.form_id, "form": deepcopy(form.body)}

    def _require_pending(self, form_id: int | RawPacket | None) -> _PendingForm:
        form = self._find(form_id)
        if form is None:
            raise RuntimeError("no form is waiting for an answer")
        return form

    async def answer(self, response: Any, form_id: int | RawPacket | None) -> None:
        form = self._require_pending(form_id)
        await self._submit(form, json_response=_encode_response(form.body, response))

    async def dismiss(self, reason: str, form_id: int | RawPacket | None) -> None:
        if reason not in ("USER_CLOSED", "USER_BUSY"):
            raise ValueError(f"unknown form cancel reason {reason!r}")
        form = self._require_pending(form_id)
        await self._submit(form, cancel_reason=self._binding.ModalFormCancelReason[reason])

    async def _submit(self, form: _PendingForm, *, json_response=None, cancel_reason=None) -> None:
        async with self._submitting:
            if self._pending.get(form.form_id) is not form:
                raise RuntimeError("form is no longer pending")
            await self._packets.send(self._binding.ModalFormResponsePacket(
                form_id=form.form_id, json_response=json_response, form_cancel_reason=cancel_reason,
            ))
            if self._pending.get(form.form_id) is form:
                del self._pending[form.form_id]

    def close(self) -> None:
        self._unsubscribe()
        self._pending.clear()
