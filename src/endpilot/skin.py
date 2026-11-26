"""Bedrock skin snapshots and classic textures for submission."""

import hashlib
from dataclasses import dataclass, field
from os import SEEK_CUR, PathLike
from struct import error as StructError
from struct import unpack
from typing import BinaryIO, Literal, Self
from uuid import UUID


_SKIN_WIDTH = 64
_SOLID_SKIN_HEIGHT = 32
_CLASSIC_PNG_SIZES = ((64, 32), (64, 64), (128, 128))
_BLACK_RGBA = b"\x00\x00\x00\xff"
_PNG_IEND_CRC = b"\xaeB`\x82"
_SkinModel = Literal["wide", "slim"]


@dataclass(frozen=True, slots=True)
class PlayerSkinSnapshot:
    """The base skin image and metadata reported by the server."""

    skin_id: str
    width: int
    height: int
    rgba: bytes = field(repr=False)
    model: _SkinModel | None
    resource_patch: str


def _player_skin_snapshot(skin) -> PlayerSkinSnapshot | None:
    if skin is None:
        return None
    image = getattr(skin, "skin_image", None)
    if image is None:
        # Older bindings expose the image and model directly on SerializedSkinRef.
        width, height, rgba = int(skin.image_width), int(skin.image_height), bytes(skin.image_bytes)
        arm_size = skin.arm_size
    else:
        width, height, rgba = int(image.width), int(image.height), bytes(image.image_bytes)
        arm_size = skin.arm_size_type
    skin_id = str(skin.id)
    if not skin_id and width == height == 0 and not rgba:
        return None
    model = {"WIDE": "wide", "SLIM": "slim"}.get(getattr(arm_size, "name", None))
    return PlayerSkinSnapshot(skin_id, width, height, rgba, model, str(skin.resource_patch))


def _validate_model(model: _SkinModel) -> None:
    if not isinstance(model, str) or model not in ("wide", "slim"):
        raise ValueError("skin model must be 'wide' or 'slim'")


def _resource_patch(model: _SkinModel) -> str:
    geometry = "geometry.humanoid.customSlim" if model == "slim" else "geometry.humanoid.custom"
    return f'{{"geometry":{{"default":"{geometry}"}}}}'


def _verify_png_end(source: BinaryIO) -> None:
    # Pillow verifies chunk CRCs up to IEND. Walk the chunk headers to check its
    # CRC while allowing file metadata beyond the PNG datastream.
    source.seek(8)
    while True:
        length, name = unpack(">I4s", source.read(8))
        if name == b"IEND":
            break
        source.seek(length + 4, SEEK_CUR)
    if length != 0 or source.read(4) != _PNG_IEND_CRC:
        raise ValueError("skin PNG must contain a valid IEND chunk")


@dataclass(frozen=True, slots=True)
class Skin:
    """An immutable classic skin with RGBA bytes in PNG row order."""

    rgba: bytes = field(repr=False)
    model: _SkinModel = field(default="wide", kw_only=True)
    width: int = field(default=64, kw_only=True)
    height: int = field(default=64, kw_only=True)

    def __post_init__(self) -> None:
        _validate_model(self.model)
        dimensions = (self.width, self.height)
        if any(type(value) is not int for value in dimensions) or dimensions not in _CLASSIC_PNG_SIZES:
            raise ValueError("skin dimensions must be 64×32, 64×64 or 128×128 pixels")
        if not isinstance(self.rgba, bytes):
            raise TypeError("skin RGBA must be immutable bytes")
        if len(self.rgba) != self.width * self.height * 4:
            raise ValueError(f"skin RGBA must contain exactly {self.width}×{self.height} pixels")

    @classmethod
    def from_png(cls, path: str | PathLike[str], *, model: _SkinModel = "wide") -> Self:
        """Read a 64×32, 64×64 or 128×128 static PNG with up to 8 bits per color sample."""
        _validate_model(model)
        try:
            from PIL import Image
        except ModuleNotFoundError as error:
            if error.name != "PIL":
                raise
            raise ImportError("PNG skins require Pillow. Install endpilot[skin].") from error

        with open(path, "rb") as source:
            try:
                with Image.open(source, formats=("PNG",)) as image:
                    if image.size not in _CLASSIC_PNG_SIZES:
                        raise ValueError("skin PNG must be 64×32, 64×64 or 128×128 pixels")
                    if image.get_format_mimetype() == "image/apng":
                        raise ValueError("skin PNG must be a static image")
                    image.verify()

                _verify_png_end(source)
                # The PNG IHDR bit depth is lost when Pillow reduces 16-bit pixels.
                source.seek(24)
                bit_depth, = source.read(1)
                if bit_depth > 8:
                    raise ValueError("skin PNG must use at most 8 bits per color sample")
                source.seek(0)
                with Image.open(source, formats=("PNG",)) as image:
                    transparent = image.info.get("transparency")
                    if image.mode == "L" and bit_depth in (2, 4) and transparent is not None:
                        # Pillow expands grayscale pixels to a byte but may leave tRNS unscaled.
                        max_sample = (1 << bit_depth) - 1
                        if transparent <= max_sample:
                            image.info["transparency"] = transparent * (255 // max_sample)
                    return cls(image.convert("RGBA").tobytes(), model=model, width=image.width, height=image.height)
            except (Image.DecompressionBombError, IndexError, OSError, StructError, SyntaxError) as error:
                raise ValueError("skin must be a valid PNG") from error

    def _id_for(self, player_uuid: UUID) -> str:
        description = f"{self.model}:{self.width}x{self.height}:".encode("ascii")
        digest = hashlib.sha256(description + self.rgba).hexdigest()
        return f"endpilot.{player_uuid}.{digest}"
