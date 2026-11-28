"""Generate the 2225 vanilla outline and collision tables from EndstoneMC/bedrock-data."""

import argparse
from collections import Counter, defaultdict
import json
from math import isfinite
from pathlib import Path

from bedrock_source import SOURCE_REVISION, load_json


DATA = Path(__file__).resolve().parents[1] / "src/endpilot/data"
_LIQUIDS = {"minecraft:water", "minecraft:flowing_water", "minecraft:lava", "minecraft:flowing_lava"}
_DYNAMIC = {
    "minecraft:piston", "minecraft:sticky_piston", "minecraft:piston_arm_collision",
    "minecraft:sticky_piston_arm_collision", "minecraft:moving_block",  # Moving/extended geometry.
}
_AIR, _UNRESOLVED, _UNRESOLVED_OVERHANG = range(3)
_UNIT_COLLISION = [[0, 0, 0, 1, 1, 1]]
_SOLID_BLOCKS = frozenset("""
    amethyst_block andesite barrier bedrock blackstone brick_block calcite chiseled_deepslate chiseled_nether_bricks
    chiseled_polished_blackstone chiseled_quartz_block chiseled_red_sandstone chiseled_sandstone
    chiseled_stone_bricks clay coal_block coarse_dirt cobbled_deepslate cobblestone copper_block cracked_deepslate_bricks
    cracked_deepslate_tiles cracked_nether_bricks cracked_polished_blackstone_bricks cracked_stone_bricks
    crimson_stem crying_obsidian deepslate deepslate_bricks deepslate_tiles diamond_block diorite dirt dirt_with_roots
    dripstone_block emerald_block end_bricks end_stone glass gold_block granite grass_block gravel hardened_clay
    ice invisible_bedrock iron_block lapis_block magma moss_block mossy_cobblestone mossy_stone_bricks
    mycelium nether_brick netherite_block netherrack obsidian
    packed_ice pale_moss_block podzol polished_andesite polished_basalt polished_blackstone
    polished_blackstone_bricks polished_deepslate polished_diorite polished_granite prismarine purpur_block
    quartz_block quartz_bricks raw_copper_block raw_gold_block raw_iron_block red_nether_brick red_sand red_sandstone
    redstone_block reinforced_deepslate sand sandstone smooth_stone snow stone stone_bricks stripped_crimson_stem
    stripped_warped_stem tuff warped_stem
""".split())
_SOLID_FAMILIES = (
    "_planks", "_log", "_wood", "_hyphae", "_wool", "_concrete", "_concrete_powder",
    "_terracotta", "_stained_glass", "_ore",
)
_COLLISION_FAMILIES = ("_button", "_slab", "_fence", "_fence_gate", "_wall", "_stairs")


def collision_shape(row):
    # An empty or full box at the export's sample position alone cannot identify
    # a static shape. Only the named materials and state-encoded families qualify.
    name = row["name"].removeprefix("minecraft:")
    if name in _SOLID_BLOCKS or name.endswith(_SOLID_FAMILIES):
        if row["collision_shapes"] != _UNIT_COLLISION:
            raise ValueError(f"expected full collision geometry for {row['name']}")
    elif name not in ("air", "fence_gate") and not name.endswith(_COLLISION_FAMILIES):
        return None
    shape = tuple(tuple(box) for box in row["collision_shapes"])
    if any(len(box) != 6 or not all(isfinite(value) for value in box)
           or not all(box[axis] < box[axis + 3] for axis in range(3)) for box in shape):
        raise ValueError(f"invalid collision box for {row['name']}")
    return shape


def shape_id_rows(shape_ids):
    return ",\n".join(
        "    " + ", ".join(map(str, shape_ids[index:index + 32]))
        for index in range(0, len(shape_ids), 32)
    )


def generate_collision(rows):
    shapes = {None: 0, (): 1}
    shape_ids = []
    for row in rows:
        shape = collision_shape(row)
        shape_ids.append(shapes.setdefault(shape, len(shapes)))
    shape_rows = ",\n".join("    " + json.dumps(shape) for shape in shapes)
    return (
        '{\n'
        f'  "source": "EndstoneMC/bedrock-data@{SOURCE_REVISION}",\n'
        '  "notice": "Generated from the MIT-licensed block_palette.json for EndPilot.",\n'
        f'  "shapes": [\n{shape_rows}\n  ],\n'
        f'  "shape_ids": [\n{shape_id_rows(shape_ids)}\n  ]\n'
        '}\n',
        sum(shape_id != 0 for shape_id in shape_ids), len(shapes) - 1,
    )


def has_position_offset(outline, visual):
    # The export samples shapes at one position. Matching extents with a shift
    # in both horizontal axes can indicate a position-dependent block offset.
    offset = [bound - reference for bound, reference in zip(outline, visual, strict=True)]
    tolerance = 1e-6
    return (
        abs(offset[0]) > tolerance and abs(offset[2]) > tolerance
        and all(abs(offset[axis] - offset[axis + 3]) <= tolerance for axis in range(3))
    )


def classify(row):
    name = row["name"]
    if name == "minecraft:air":
        return "air", _AIR
    if name == "minecraft:unknown":
        return "placeholder", _UNRESOLVED_OVERHANG
    if name.endswith("_door"):
        return "door", _UNRESOLVED
    if name in _DYNAMIC or name.endswith("_shulker_box"):
        return "dynamic", _UNRESOLVED_OVERHANG
    if name in _LIQUIDS:
        return "liquid", _UNRESOLVED
    box = row["outline"]
    if not isinstance(box, list) or len(box) != 6:
        raise ValueError(f"unexpected outline format for {name}")
    if not all(isfinite(value) and -1 <= value <= 2 for value in box):
        raise ValueError(f"outline outside the one-cell neighborhood for {name}")
    if not all(box[axis] < box[axis + 3] for axis in range(3)):
        return "degenerate", _UNRESOLVED
    if has_position_offset(box, row["visual_shape"]):
        return "position_offset", _UNRESOLVED_OVERHANG
    reason = "static_overhang" if any(value < 0 or value > 1 for value in box) else "static"
    return reason, tuple(box)


def generate(rows):
    # Equal boxes share a geometry entry. The export's array order is the runtime ID order.
    boxes = [[], None, None]
    by_box = {}
    shape_ids = []
    classes = defaultdict(Counter)
    for row in rows:
        reason, shape = classify(row)
        classes[reason][row["name"]] += 1
        if isinstance(shape, tuple):
            if shape not in by_box:
                by_box[shape] = len(boxes)
                boxes.append(shape)
            shape = by_box[shape]
        shape_ids.append(shape)
    box_rows = ",\n".join("    " + json.dumps(box) for box in boxes)
    return (
        '{\n'
        f'  "source": "EndstoneMC/bedrock-data@{SOURCE_REVISION}",\n'
        '  "notice": "Generated from the MIT-licensed block_palette.json for EndPilot.",\n'
        f'  "unresolved_overhang_id": {_UNRESOLVED_OVERHANG},\n'
        f'  "boxes": [\n{box_rows}\n  ],\n'
        f'  "shape_ids": [\n{shape_id_rows(shape_ids)}\n  ]\n'
        '}\n',
        {"states": len(shape_ids), "unique_boxes": len(by_box),
         "classes": {reason: {"states": names.total(), "names": dict(sorted(names.items()))}
                     for reason, names in sorted(classes.items())}},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bedrock_data_checkout", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    rows = load_json(args.bedrock_data_checkout, "block_palette.json")["blocks"]
    outline, report = generate(rows)
    collision, supported, shapes = generate_collision(rows)
    for filename, expected in (("outline-2225.json", outline), ("collision-2225.json", collision)):
        output = DATA / filename
        if args.check:
            if output.read_text(encoding="utf-8") != expected:
                raise SystemExit(f"{filename} differs from generated data")
        else:
            output.write_bytes(expected.encode("utf-8"))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    counts = {reason: group["states"] for reason, group in report["classes"].items()}
    print(f"outline-2225.json: {len(outline)} bytes, {report['unique_boxes']} distinct boxes, {counts}")
    print(f"collision-2225.json: {len(collision)} bytes, {shapes} distinct shapes, {supported} supported states")


if __name__ == "__main__":
    main()
