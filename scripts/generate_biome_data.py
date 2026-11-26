"""Generate the protocol 2225 vanilla biome catalog from EndstoneMC/bedrock-data."""

import argparse
import json
from math import isfinite
from pathlib import Path

from bedrock_source import SOURCE_REVISION, load_json


OUTPUT = Path(__file__).resolve().parents[1] / "src/endpilot/data/biomes-2225.json"


def generate(checkout: Path) -> bytes:
    rows = []
    ids = set()
    for row in load_json(checkout, "biomes.json")["biomes"]:
        biome_id, name = row["id"], row["name"]
        temperature, downfall = row["temperature"], row["downfall"]
        rain, tags = row["rain"], row["tags"]
        if type(biome_id) is not int or biome_id < 0 or biome_id in ids:
            raise ValueError(f"invalid or repeated biome ID {biome_id!r}")
        if (not isinstance(name, str) or not name.startswith("minecraft:") or name == "minecraft:"
                or not all(type(value) in (int, float) and isfinite(value) for value in (temperature, downfall))
                or type(rain) is not bool or not isinstance(tags, list)
                or not all(isinstance(tag, str) for tag in tags)):
            raise ValueError(f"unexpected biome fields for ID {biome_id}")
        ids.add(biome_id)
        rows.append((biome_id, name, temperature, downfall, rain, tags))
    rows.sort(key=lambda row: row[0])
    rows_json = ",\n".join("    " + json.dumps(row, separators=(",", ":")) for row in rows)
    return (
        '{\n'
        f'  "source": "EndstoneMC/bedrock-data@{SOURCE_REVISION}:biomes.json",\n'
        '  "protocol": 2225,\n'
        f'  "biomes": [\n{rows_json}\n  ]\n'
        '}\n'
    ).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bedrock_data_checkout", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = generate(args.bedrock_data_checkout)
    if args.check:
        if OUTPUT.read_bytes() != expected:
            raise SystemExit(f"{OUTPUT.name} differs from generated data")
    else:
        OUTPUT.write_bytes(expected)
    print(f"{OUTPUT.name}: {len(expected)} bytes")


if __name__ == "__main__":
    main()
