"""Generate protocol 2225 block data from the pinned EndstoneMC export."""

import argparse
from itertools import groupby, product
import json
from pathlib import Path
import re

from bedrock_source import SOURCE_REVISION, load_json


STATE_VERSION = 18168865
STATE_COUNT = 23960
OUTPUT = Path(__file__).resolve().parents[1] / "src" / "endpilot" / "data" / "harvest-2225.json"

# The pinned palette stores block properties as flat SNBT compounds.
_PROPERTY = re.compile(r"(?P<name>'[^'\\]+'|[A-Za-z0-9_]+):(?P<value>'[^'\\]*'|-?[0-9]+b?)(?P<end>[,}])")


def parse_properties(text):
    if text == "{}":
        return {}
    if not text.startswith("{"):
        raise ValueError(f"expected a block state compound: {text!r}")
    properties = {}
    offset = 1
    while True:
        field = _PROPERTY.match(text, offset)
        if field is None:
            raise ValueError(f"invalid property at offset {offset} in {text!r}")
        key = field["name"].strip("'")
        if key in properties:
            raise ValueError(f"repeated property {key!r} in {text!r}")
        token = field["value"]
        if token.startswith("'"):
            value = "string", token[1:-1]
        elif token.endswith("b"):
            number = int(token[:-1])
            if not -128 <= number <= 127:
                raise ValueError(f"invalid byte property {key!r} in {text!r}")
            value = "byte", number
        else:
            number = int(token)
            if not -(2**31) <= number < 2**31:
                raise ValueError(f"invalid int property {key!r} in {text!r}")
            value = "int", number
        properties[key] = value
        offset = field.end()
        if field["end"] == "}":
            if offset != len(text):
                raise ValueError(f"trailing data in {text!r}")
            return properties


def terrain_indices(blocks):
    unit_cubes = []
    unsafe_support = []
    for index, block in enumerate(blocks):
        if block["collision_shapes"] != [[0, 0, 0, 1, 1, 1]]:
            continue
        unit_cubes.append(index)
        name = block["name"]
        # Magma damages players, and piston and shulker collision can change
        # independently of the observed state ID.
        if name in {"minecraft:magma", "minecraft:piston", "minecraft:sticky_piston"} or name.endswith("_shulker_box"):
            unsafe_support.append(index)
    return {
        "air": next(i for i, block in enumerate(blocks) if block["name"] == "minecraft:air"),
        "unknown": next(i for i, block in enumerate(blocks) if block["name"] == "minecraft:unknown"),
        "unit_cubes": unit_cubes,
        "unsafe_support": unsafe_support,
    }


def bed_states(blocks, states):
    directions = ("south", "west", "north", "east")
    beds = []
    for index, (block, properties) in enumerate(zip(blocks, states, strict=True)):
        name = block["name"]
        if name == "minecraft:bed":
            facing = directions[properties["direction"][1]]
        elif name == "minecraft:straw_bed":
            facing = properties["minecraft:cardinal_direction"][1]
        else:
            continue
        beds.append((index, name, facing, bool(properties["head_piece_bit"][1]),
                     bool(properties["occupied_bit"][1])))
    return beds


def state_lookup(states, names):
    layouts = {}
    layout_ids = []
    first = 0
    for last, name in names:
        rows = states[first:last + 1]
        fields = rows[0].keys()
        if any(row.keys() != fields for row in rows):
            raise ValueError(f"inconsistent block state keys for {name}")

        columns = []
        for key in fields:
            kind = rows[0][key][0]
            column = [row[key] for row in rows]
            if any(prop[0] != kind for prop in column):
                raise ValueError(f"inconsistent block state type for {name}[{key}]")
            values = [prop[1] for prop in column]
            first_change = next((i for i, value in enumerate(values) if value != values[0]), len(rows))
            columns.append((first_change, key, kind, tuple(dict.fromkeys(values))))

        # The first property change identifies its stride in the original palette.
        layout = tuple((key, kind, values) for _, key, kind, values in sorted(columns, key=lambda row: (-row[0], row[1])))
        expanded = [
            {key: (kind, value) for (key, kind, _), value in zip(layout, values, strict=True)}
            for values in product(*(values for _, _, values in layout))
        ]
        if expanded != rows:
            raise ValueError(f"block state layout does not reproduce the palette for {name}")
        layout_ids.append(layouts.setdefault(layout, len(layouts)))
        first = last + 1
    return {"version": STATE_VERSION, "layouts": list(layouts), "layout_ids": layout_ids}


def palette_metadata(blocks, block_types):
    """Build the state table sections independently of mining observations."""
    if len(blocks) != STATE_COUNT:
        raise ValueError(f"expected {STATE_COUNT} states in the protocol 2225 palette")
    if any(type(row["version"]) is not int or row["version"] != STATE_VERSION for row in blocks):
        raise ValueError(f"expected block state version {STATE_VERSION}")

    hashes = [block["id"] & 0xffffffff for block in blocks]
    if len(set(hashes)) != len(hashes):
        raise ValueError("duplicate network block hashes")

    names = []
    last = -1
    for name, rows in groupby(blocks, key=lambda row: row["name"]):
        last += sum(1 for _ in rows)
        names.append((last, name))
    declared = {entry["name"] for entry in block_types}
    palette_names = {name for _, name in names}
    if len(palette_names) != len(names) or len(declared) != len(block_types) or declared != palette_names:
        raise ValueError("block types and contiguous state name ranges disagree")

    states = [parse_properties(block["states"]) for block in blocks]
    return hashes, {
        "terrain": terrain_indices(blocks),
        "bed_states": bed_states(blocks, states),
        "block_names": names,
        "state_lookup": state_lookup(states, names),
    }


def encode_rules(names, mining):
    if mining.keys() != {name for _, name in names}:
        raise ValueError("mining rules and the pinned palette disagree on block names")
    rules = [None]
    rule_ids = {}
    block_ids = {}
    for _, name in names:
        rule = mining[name]
        if rule is None:
            block_ids[name] = 0
            continue
        required, speeds = rule
        required_names = None if required is None else tuple(sorted(required))
        speed_items = tuple(sorted(speeds.items()))
        key = required_names, speed_items
        if key not in rule_ids:
            rule_ids[key] = len(rules)
            rules.append((required_names, dict(speed_items)))
        block_ids[name] = rule_ids[key]
    return rules, block_ids


def generate(source: Path) -> str:
    from bedrock_mining import mining_rules

    document = load_json(source, "block_palette.json")
    if document["type"] != "hashed":
        raise ValueError("expected a palette with vanilla network hashes")
    blocks = document["blocks"]
    block_types = load_json(source, "block_types.json")["block_types"]
    items = load_json(source, "items.json")["items"]

    hashes, metadata = palette_metadata(blocks, block_types)
    names = metadata["block_names"]
    rules, block_ids = encode_rules(names, mining_rules(block_types, items, blocks))
    palette = [(network_hash, block_ids[block["name"]]) for block, network_hash in zip(blocks, hashes, strict=True)]

    def compact(value):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    rule_rows = ",\n".join("    " + compact(rule) for rule in rules)
    name_rows = ",\n".join("    " + compact(names[i:i + 8])[1:-1] for i in range(0, len(names), 8))
    state_rows = ",\n".join("    " + compact(palette[i:i + 32])[1:-1] for i in range(0, len(palette), 32))
    terrain = compact(metadata["terrain"])
    beds = compact(metadata["bed_states"])
    lookup = compact(metadata["state_lookup"])
    return (
        f'{{\n  "terrain": {terrain},\n  "bed_states": {beds},\n  "rules": [\n{rule_rows}\n  ],\n'
        f'  "block_names": [\n{name_rows}\n  ],\n  "palette": [\n{state_rows}\n  ],\n  "state_lookup": {lookup}\n}}\n'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bedrock_data", type=Path, help="directory containing the pinned bedrock-data JSON files")
    parser.add_argument("--check", action="store_true", help="check existing tables without writing")
    args = parser.parse_args()
    payload = generate(args.bedrock_data)
    if args.check:
        if OUTPUT.read_text(encoding="utf-8") != payload:
            raise SystemExit(f"{OUTPUT.name} differs from generated data")
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_bytes(payload.encode("utf-8"))
    print(f"{OUTPUT.name}: {len(payload.encode('utf-8'))} bytes from {SOURCE_REVISION}")


if __name__ == "__main__":
    main()
