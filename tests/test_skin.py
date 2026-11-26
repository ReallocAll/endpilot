import base64
import json
import struct
import sys
import unittest
import zlib
from dataclasses import FrozenInstanceError
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from endpilot import Bot, Skin
from test_scheduled_input import Wire


WIDE = "geometry.humanoid.custom"
SLIM = "geometry.humanoid.customSlim"


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(width, height, rows, *, color_type=6, bit_depth=8, extra_chunks=b""):
    header = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0))
    rows = b"".join(b"\0" + row for row in rows)
    return b"\x89PNG\r\n\x1a\n" + header + extra_chunks + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")


def _texture(seed, width=64, height=64):
    return [
        bytes(value for x in range(width) for value in (
            (3 * x + seed) % 256, (7 * y + seed) % 256, (x + 2 * y + seed) % 256, (x * y * seed) % 256,
        ))
        for y in range(height)
    ]


def _jwt_claims(token):
    body = token.split(b".")[1]
    return json.loads(base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4)))


def _login_claims(wire):
    login, = [packet for packet in wire.sent if isinstance(packet, wire.protocol.binding.LoginPacket)]
    size, = struct.unpack_from("<i", login.connection_request)
    token = json.loads(login.connection_request[4:4 + size])["Token"].encode()
    return UUID(_jwt_claims(token)["leguuid"]), _jwt_claims(login.connection_request[8 + size:])


async def _join(bot, transport):
    wire = Wire(bot.protocol)
    binding = wire.protocol.binding
    wire.push(binding.NetworkSettingsPacket(compression_algorithm=binding.PacketCompressionAlgorithm.NONE))
    wire.push(binding.StartGamePacket(runtime_id=42, entity_id=25))
    wire.push(binding.PlayStatusPacket(status=binding.PlayStatus.PLAYER_SPAWN))
    endpoint = ("http://127.0.0.1:19132",) if transport == "nethernet" else ("127.0.0.1", 19132)
    with patch(f"endpilot.transport.{transport}.connect", new=AsyncMock(return_value=wire)):
        await getattr(bot, f"join_{transport}")(*endpoint)
    return wire


class SkinTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_login_and_legacy_solid_skin(self):
        bot = Bot("Default")
        self.addAsyncCleanup(bot.close)
        wire = await _join(bot, "nethernet")
        player_uuid, login = _login_claims(wire)
        self.assertEqual(login["SkinId"], f"endpilot.{player_uuid}")
        self.assertEqual(login["ArmSize"], "wide")
        self.assertEqual((login["SkinImageWidth"], login["SkinImageHeight"]), (64, 32))
        self.assertEqual(base64.b64decode(login["SkinData"]), b"\x00\x00\x00\xff" * (64 * 32))
        self.assertEqual(json.loads(base64.b64decode(login["SkinResourcePatch"]))["geometry"]["default"], WIDE)
        await bot.change_skin(skin_id="solid", rgba=[7, 13, 23, 127])
        update = wire.sent[-1]
        self.assertEqual(update.uuid, player_uuid)
        self.assertEqual((update.skin.id, update.skin.full_id), ("solid", "solid"))
        self.assertEqual(update.skin.arm_size_type, wire.protocol.binding.ArmSizeType.WIDE)
        self.assertEqual((update.skin.skin_image.width, update.skin.skin_image.height), (64, 32))
        self.assertEqual(update.skin.skin_image.image_bytes, b"\x07\x0d\x17\x7f" * (64 * 32))

    def test_skin_requires_immutable_complete_rgba_and_a_classic_model(self):
        with self.assertRaisesRegex(TypeError, "immutable bytes"):
            Skin(bytearray(64 * 64 * 4))
        with self.assertRaisesRegex(ValueError, "64×64"):
            Skin(b"")
        with self.assertRaisesRegex(ValueError, "dimensions"):
            Skin(bytes(32 * 64 * 4), width=32)
        with self.assertRaisesRegex(ValueError, "model"):
            Skin(bytes(64 * 64 * 4), model="other")
        with self.assertRaises(FrozenInstanceError):
            Skin(bytes(64 * 64 * 4)).model = "slim"
        with self.assertRaisesRegex(TypeError, "skin must be a Skin"):
            Bot("Invalid", skin="skin.png")

    def test_missing_pillow_reports_the_optional_extra(self):
        with patch.dict(sys.modules, {"PIL": None}):
            with self.assertRaisesRegex(ImportError, r"endpilot\[skin\]"):
                Skin.from_png("skin.png")


@unittest.skipUnless(find_spec("PIL"), "install the skin extra")
class PngSkinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "skin.png"

    def test_rejects_wrong_size_non_png_corruption_and_animation(self):
        from PIL import Image

        rgba = _png(64, 64, _texture(23))
        header_end = 33
        animated = (
            rgba[:header_end] + _chunk(b"acTL", struct.pack(">II", 1, 0))
            + _chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 64, 64, 0, 0, 1, 1, 0, 0))
            + rgba[header_end:]
        )
        Image.new("RGB", (64, 64)).save(self.path, format="BMP")
        cases = (
            ("wrong dimensions", _png(32, 64, _texture(4, width=32)), "64×32, 64×64 or 128×128"),
            ("different format", self.path.read_bytes(), "valid PNG"),
            ("no IDAT", rgba[:header_end] + rgba[-12:], "valid PNG"),
            ("bad IDAT CRC", rgba[:-13] + bytes([rgba[-13] ^ 1]) + rgba[-12:], "valid PNG"),
            ("truncated IEND", rgba[:-2], "IEND"),
            ("invalid IEND CRC", rgba[:-1] + bytes([rgba[-1] ^ 1]), "IEND"),
            ("animated PNG", animated, "static image"),
            ("16-bit PNG", _png(64, 64, [bytes(128)] * 64, color_type=0, bit_depth=16), "8 bits"),
        )
        for reason, content, error in cases:
            with self.subTest(reason=reason):
                self.path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, error):
                    Skin.from_png(self.path)
        with self.assertRaisesRegex(ValueError, "model"):
            Skin.from_png(self.path, model="alex")

    def test_preserves_indexed_png_pixels_and_transparency(self):
        palette = _chunk(b"PLTE", bytes((6, 53, 89, 105, 81, 7, 9, 22, 2)))
        transparency = _chunk(b"tRNS", bytes((0, 127)))
        indices = bytes((0, 1, 2, 0)) * 16
        png = _png(64, 64, [indices] * 64, color_type=3, extra_chunks=palette + transparency)
        self.path.write_bytes(png + b"export metadata")
        skin = Skin.from_png(self.path)
        self.assertEqual(skin.model, "wide")
        pixel_group = bytes((6, 53, 89, 0, 105, 81, 7, 127, 9, 22, 2, 255, 6, 53, 89, 0))
        self.assertEqual(skin.rgba, pixel_group * 16 * 64)

    def test_preserves_transparency_in_low_bit_depth_grayscale(self):
        for bit_depth, samples, transparent, expected in (
            (2, b"\xe4" * 16, 3, bytes((255, 255, 255, 0, 170, 170, 170, 255, 85, 85, 85, 255, 0, 0, 0, 255))),
            (4, b"\x15" * 32, 5, bytes((17, 17, 17, 255, 85, 85, 85, 0)) * 2),
        ):
            with self.subTest(bit_depth=bit_depth):
                part = _chunk(b"tRNS", struct.pack(">H", transparent))
                self.path.write_bytes(_png(64, 64, [samples] * 64, color_type=0, bit_depth=bit_depth,
                                           extra_chunks=part))
                self.assertEqual(Skin.from_png(self.path).rgba, expected * 16 * 64)

    async def test_both_joins_support_reloading_and_restoring_skin_snapshots(self):
        original_rows = _texture(11)
        replacement_rows = _texture(89, 128, 128)
        original_pixels = b"".join(original_rows)
        replacement_pixels = b"".join(replacement_rows)
        for transport in ("nethernet", "raknet"):
            with self.subTest(transport=transport):
                self.path.write_bytes(_png(64, 64, original_rows))
                original = Skin.from_png(self.path, model="slim")
                bot = Bot("SkinPlayer", skin=original)
                self.addAsyncCleanup(bot.close)
                wire = await _join(bot, transport)
                player_uuid, login = _login_claims(wire)
                self.assertEqual(player_uuid, bot.identity.uuid)
                self.assertEqual((login["SkinImageWidth"], login["SkinImageHeight"]), (64, 64))
                self.assertEqual(login["ArmSize"], "slim")
                self.assertEqual(json.loads(base64.b64decode(login["SkinResourcePatch"]))["geometry"]["default"], SLIM)
                self.assertEqual(base64.b64decode(login["SkinData"]), original_pixels)

                self.path.write_bytes(_png(128, 128, replacement_rows))
                replacement = Skin.from_png(self.path, model="slim")
                wide = Skin.from_png(self.path, model="wide")
                self.path.unlink()

                # Game updates target the UUID frozen at login, regardless of the configured identity.
                bot.identity.uuid = uuid4()
                await bot.change_skin(replacement)
                await bot.change_skin(original)
                await bot.change_skin(wide)
                changed, restored, same_pixels_wide = wire.sent[-3:]
                self.assertEqual(login["SkinId"], restored.skin.id)
                self.assertEqual(len({changed.skin.id, restored.skin.id, same_pixels_wide.skin.id}), 3)
                arm = wire.protocol.binding.ArmSizeType
                for packet, size, pixels, model, geometry in (
                    (changed, 128, replacement_pixels, arm.SLIM, SLIM),
                    (restored, 64, original_pixels, arm.SLIM, SLIM),
                    (same_pixels_wide, 128, replacement_pixels, arm.WIDE, WIDE),
                ):
                    self.assertEqual(packet.uuid, player_uuid)
                    self.assertEqual(packet.skin.arm_size_type, model)
                    self.assertEqual(json.loads(packet.skin.resource_patch)["geometry"]["default"], geometry)
                    image = packet.skin.skin_image
                    self.assertEqual((image.width, image.height), (size, size))
                    self.assertEqual(image.image_bytes, pixels)

                before = len(wire.sent)
                with self.assertRaisesRegex(TypeError, "only used with a string"):
                    await bot.change_skin(replacement, [0, 0, 0, 255])
                with self.assertRaisesRegex(TypeError, "Skin or a string"):
                    await bot.change_skin(self.path)
                self.assertEqual(len(wire.sent), before)
                await bot.close()

    async def test_legacy_png_keeps_its_dimensions_pixels_and_model(self):
        rows = _texture(23, height=32)
        self.path.write_bytes(_png(64, 32, rows))
        pixels = b"".join(rows)
        wide = Skin.from_png(self.path)
        slim = Skin.from_png(self.path, model="slim")
        self.assertEqual((wide.width, wide.height, wide.model, wide.rgba), (64, 32, "wide", pixels))
        bot = Bot("LegacySkin", skin=wide)
        self.addAsyncCleanup(bot.close)
        wire = await _join(bot, "nethernet")
        player_uuid, login = _login_claims(wire)
        self.assertEqual((login["SkinImageWidth"], login["SkinImageHeight"]), (64, 32))
        self.assertEqual(login["ArmSize"], "wide")
        self.assertEqual(json.loads(base64.b64decode(login["SkinResourcePatch"]))["geometry"]["default"], WIDE)
        self.assertEqual(base64.b64decode(login["SkinData"]), pixels)

        await bot.change_skin(slim)
        packet = wire.sent[-1]
        self.assertEqual(packet.uuid, player_uuid)
        self.assertNotEqual(packet.skin.id, login["SkinId"])
        self.assertEqual(packet.skin.arm_size_type, wire.protocol.binding.ArmSizeType.SLIM)
        self.assertEqual(json.loads(packet.skin.resource_patch)["geometry"]["default"], SLIM)
        image = packet.skin.skin_image
        self.assertEqual((image.width, image.height, image.image_bytes), (64, 32, pixels))
