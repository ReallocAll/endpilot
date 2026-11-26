"""Generate the 2225 vanilla outline table from EndstoneMC/bedrock-data."""

import argparse
from collections import Counter, defaultdict
import json
from math import isfinite
from pathlib import Path

from bedrock_source import SOURCE_REVISION, load_json


DATA = Path(__file__).resolve().parents[1] / "src/endpilot/data"
_LIQUIDS = {"minecraft:water", "minecraft:flowing_water", "minecraft:lava", "minecraft:flowing_lava"}
_DYNAMIC = {
    "minecraft:trapped_chest",
    "minecraft:piston", "minecraft:sticky_piston", "minecraft:piston_arm_collision",
    "minecraft:sticky_piston_arm_collision", "minecraft:moving_block",  # Moving/extended geometry.
}
_AIR, _UNRESOLVED, _UNRESOLVED_OVERHANG = range(3)


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


def generate(source):
    rows = load_json(source, "block_palette.json")["blocks"]
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
    state_rows = ",\n".join(
        "    " + ", ".join(map(str, shape_ids[index:index + 32]))
        for index in range(0, len(shape_ids), 32)
    )
    return (
        '{\n'
        f'  "source": "EndstoneMC/bedrock-data@{SOURCE_REVISION}",\n'
        '  "notice": "Generated from the MIT-licensed block_palette.json for EndPilot.",\n'
        f'  "unresolved_overhang_id": {_UNRESOLVED_OVERHANG},\n'
        f'  "boxes": [\n{box_rows}\n  ],\n'
        f'  "shape_ids": [\n{state_rows}\n  ]\n'
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
    expected, report = generate(args.bedrock_data_checkout)
    output = DATA / "outline-2225.json"
    if args.check:
        if output.read_text(encoding="utf-8") != expected:
            raise SystemExit(f"{output.name} differs from generated data")
    else:
        output.write_bytes(expected.encode("utf-8"))
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    counts = {reason: group["states"] for reason, group in report["classes"].items()}
    print(f"{output.name}: {len(expected)} bytes, {report['unique_boxes']} distinct boxes, {counts}")


if __name__ == "__main__":
    main()
