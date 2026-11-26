"""Block clearance and full-height support from the vanilla 2225 palette."""

import hashlib
import unittest
from unittest.mock import patch

from bedrock_protocol import nbt

from endpilot.block_data import matches_vanilla_palette
from endpilot.blocks import Blocks
from endpilot.protocol import Protocol

from test_blocks import PacketStream
from test_subchunks import solid_sub_chunk


POSITION = (4, 100, -9)
HEAD = (4, 101, -9)
FLOOR = (4, 99, -9)

# Corresponding hashed and sequential IDs from the 2225 Bedrock palette.
AIR = (0xDBF44120, 18894)
GLASS = (0x374B17D2, 17896)
WATER = (0x483B8B2A, 14122)
UNKNOWN = (0xFFFFFFFE, 11244)


class BlockSupportTests(unittest.TestCase):
    def prepare_stance(self, *, hashed, custom=False, explicit_extra=True):
        self.codec = Protocol(2225)
        self.blocks = Blocks(PacketStream(self.codec), None, None,
                             hashed_runtime_ids=hashed, custom_blocks=custom)
        self.id_index = 0 if hashed else 1
        self.install(POSITION, AIR)
        self.install(HEAD, AIR)
        self.install(FLOOR, GLASS)
        if explicit_extra:
            self.install(FLOOR, AIR, layer=1)
            self.install(POSITION, AIR, layer=1)
            self.install(HEAD, AIR, layer=1)

    def install(self, position, ids, *, layer=0):
        x, y, z = position
        binding = self.codec.binding
        packet = binding.UpdateBlockPacket(
            pos=binding.BlockPos(x=x, y=y, z=z), runtime_id=ids[self.id_index],
            update_flags=0, layer=layer,
        )
        self.blocks._on_packet(self.codec.encode(packet))

    def test_both_id_modes_distinguish_safe_support_hazards_and_unknowns(self):
        cases = (
            ("glass", GLASS, True),
            ("oak log facing horizontally", (0xF253CAD8, 1900), True),
            ("double slab", (0x0A53C99B, 6877), True),
            ("bottom slab", (0xA3BE7E64, 10165), False),
            ("top slab", (0x670AF759, 10166), True),
            ("stairs", (0xF8B309F3, 1858), False),
            ("fence", (0x195BFACD, 14806), False),
            ("water", WATER, False),
            ("powder snow", (0x0BB85623, 351), False),
            ("magma", (0x6680F5C9, 22826), False),
            ("piston", (0x713BF7A9, 4127), False),
            ("shulker box", (0x9DC62B9F, 7059), False),
            ("air", AIR, False),
            ("unknown placeholder", UNKNOWN, None),
            ("unrecognized ID", (0x12345678, 25000), None),
        )
        for hashed in (True, False):
            self.prepare_stance(hashed=hashed)
            self.assertIs(self.blocks.observed_block(POSITION).is_air, True)
            for name, ids, expected in cases:
                with self.subTest(hashed=hashed, block=name):
                    self.install(FLOOR, ids)
                    self.assertIs(self.blocks._flat_stance(POSITION), expected)

    def test_extra_layer_and_unknown_clearance_are_checked(self):
        for hashed in (True, False):
            with self.subTest(hashed=hashed):
                self.prepare_stance(hashed=hashed)
                self.install(HEAD, WATER, layer=1)
                self.assertIs(self.blocks._flat_stance(POSITION), False)
                self.install(HEAD, UNKNOWN, layer=1)
                self.assertIsNone(self.blocks._flat_stance(POSITION))
                self.install(HEAD, AIR, layer=1)
                self.assertIs(self.blocks._flat_stance(POSITION), True)
                self.install(FLOOR, (0x670AF759, 10166))  # Upper oak slab
                self.install(FLOOR, WATER, layer=1)
                self.assertIs(self.blocks._flat_stance(POSITION), False)
                self.install(FLOOR, UNKNOWN, layer=1)
                self.assertIsNone(self.blocks._flat_stance(POSITION))
                self.install(FLOOR, AIR, layer=1)
                self.install(POSITION, UNKNOWN)
                self.assertIsNone(self.blocks._flat_stance(POSITION))
                self.install(HEAD, GLASS)
                self.assertIs(self.blocks._flat_stance(POSITION), False)

    def test_only_a_complete_subchunk_can_confirm_an_omitted_extra_layer(self):
        for hashed in (True, False):
            for all_air in (False, True):
                with self.subTest(hashed=hashed, all_air=all_air):
                    self.prepare_stance(hashed=hashed, explicit_extra=False)
                    self.assertIsNone(self.blocks.observed_block(HEAD, layer=1))
                    self.assertIsNone(self.blocks._flat_stance(POSITION))

                    binding = self.codec.binding
                    packet = binding.SubChunkPacket
                    result = packet.SubChunkRequestResult
                    height_map = packet.HeightMapDataType.NO_DATA
                    self.blocks._on_packet(self.codec.encode(packet(
                        cache_enabled=False, dimension_type=0,
                        center_pos=binding.SubChunkPos(x=0, y=6, z=-1),
                        sub_chunk_data=[packet.SubChunkPacketData(
                            sub_chunk_pos_offset=packet.SubChunkPosOffset(x=0, y=0, z=0),
                            result=result.SUCCESS_ALL_AIR if all_air else result.SUCCESS,
                            serialized_sub_chunk=None if all_air else solid_sub_chunk(AIR[self.id_index]),
                            height_map_data=packet.HeightmapData(
                                height_map_type=height_map, subchunk_height_map=None,
                                render_height_map_type=height_map, subchunk_render_height_map=None,
                            ),
                            blob_id=None,
                        )],
                    )))
                    self.install(FLOOR, GLASS)
                    extra = self.blocks.observed_block(HEAD, layer=1)
                    self.assertIs(extra.is_air, True)
                    self.assertIsNone(extra.runtime_id)
                    self.assertIs(self.blocks._flat_stance(POSITION), True)
                    self.install(HEAD, WATER, layer=1)
                    self.assertIs(self.blocks._flat_stance(POSITION), False)

    def test_unrecognized_palette_is_not_used_for_air_or_support(self):
        self.prepare_stance(hashed=False, custom=True)
        self.assertIsNone(self.blocks.observed_block(POSITION).is_air)
        self.assertIsNone(self.blocks._flat_stance(POSITION))
        self.prepare_stance(hashed=True, custom=True)
        self.assertIs(self.blocks._flat_stance(POSITION), True)

    def test_sequential_registry_checks_declaration_contents_and_checksum(self):
        binding = Protocol(2225).binding
        checksum = 0x449B4D8291A8C9A2
        air = binding.ServerBlockProperty(name="minecraft:air", tag=nbt.CompoundTag())
        tag = nbt.CompoundTag()
        tag["z"] = nbt.IntTag(1)
        tag["a"] = nbt.StringTag("2")
        declared = binding.ServerBlockProperty(name="minecraft:test", tag=tag)
        expected = hashlib.sha256(
            b"\x0dminecraft:air\x0a\x00\x00"
            b"\x0eminecraft:test\x0a\x00\x08\x01a\x012\x03\x01z\x02\x00"
        ).hexdigest()
        with patch("endpilot.block_data._VANILLA_BLOCK_PROPERTIES_SHA256", expected):
            self.assertTrue(matches_vanilla_palette(binding, checksum, [declared, air]))
            self.assertFalse(matches_vanilla_palette(binding, 0, [declared, air]))
            self.assertFalse(matches_vanilla_palette(binding, checksum, []))
            tag["z"] = nbt.IntTag(2)
            overridden = binding.ServerBlockProperty(name="minecraft:test", tag=tag)
            self.assertFalse(matches_vanilla_palette(binding, checksum, [overridden, air]))
