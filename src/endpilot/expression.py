"""Packet construction for the connected player's expressive actions."""

from collections.abc import Sequence
from uuid import UUID

from .skin import Skin, _BLACK_RGBA, _SKIN_WIDTH, _SOLID_SKIN_HEIGHT, _SkinModel, _resource_patch


def animation(binding, runtime_id: int, action_name: str, data: float = 0.0, *, source: str | None = None):
    packet = binding.AnimatePacket
    return packet(
        action=packet.Action[action_name], runtime_id=runtime_id, data=float(data),
        swing_source=None if source is None else binding.ActorSwingSource[source],
    )


def _serialized_skin(binding, skin_id: str, model: _SkinModel, width: int, height: int, rgba: bytes):
    return binding.SerializedSkinRef(
        id=skin_id,
        resource_patch=_resource_patch(model),
        skin_image=binding.SkinImage(width=width, height=height, image_bytes=rgba),
        cape_image=binding.SkinImage(width=0, height=0, image_bytes=b""),
        geometry_data="null",
        geometry_data_min_engine_version="0.0.0",
        full_id=skin_id,
        arm_size_type=binding.ArmSizeType[model.upper()],
        is_primary_user=True,
        overrides_player_appearance=True,
        is_trusted_skin=binding.TrustedSkinFlag.UNSET,
    )


def skin_packet(binding, player_uuid: UUID, skin_id: str | Skin, rgba: Sequence[int] | None):
    if isinstance(skin_id, Skin):
        if rgba is not None:
            raise TypeError("RGBA color is only used with a string skin ID")
        skin = _serialized_skin(
            binding, skin_id._id_for(player_uuid), skin_id.model, skin_id.width, skin_id.height, skin_id.rgba,
        )
    else:
        if not isinstance(skin_id, str):
            raise TypeError("skin must be a Skin or a string skin ID")
        color = _BLACK_RGBA if rgba is None else bytes(tuple(rgba))
        if len(color) != 4:
            raise ValueError("RGBA must contain four bytes")
        skin = _serialized_skin(
            binding, skin_id, "wide", _SKIN_WIDTH, _SOLID_SKIN_HEIGHT, color * (_SKIN_WIDTH * _SOLID_SKIN_HEIGHT),
        )
    return binding.PlayerSkinPacket(
        uuid=player_uuid, skin=skin, localized_new_skin_name="", localized_old_skin_name="",
    )
