"""Build the protocol 2225 mining rules from independently observed BDS data."""

import argparse
from collections.abc import Sequence
from hashlib import file_digest
import json
from math import isfinite
from pathlib import Path


_OBSERVATIONS = Path(__file__).with_name("mining-observations-2225.json")
_BEDROCK_DATA_REVISION = "40570f03ef64e37c71a81388b52fd55da5d73521"
_BDS_VERSION = "1.26.66.1"
_NATIVE_BUILD = 53696239
_QUALIFICATION_BUILD = 53696234
_NON_HARVESTABLE = frozenset({
    "minecraft:air", "minecraft:unknown",
    "minecraft:water", "minecraft:flowing_water", "minecraft:lava", "minecraft:flowing_lava",
})

type MiningRule = tuple[frozenset[str] | None, dict[str, float]]


def mining_rules(block_types, items, blocks) -> dict[str, MiningRule | None]:
    """Read the checked-in BDS observations beside the data generator."""
    observations = json.loads(_OBSERVATIONS.read_text(encoding="utf-8"))
    if (
        observations["bedrock_data_revision"] != _BEDROCK_DATA_REVISION
        or observations["bds_version"] != _BDS_VERSION
        or observations["native_build"] != _NATIVE_BUILD
        or observations["qualification_build"] != _QUALIFICATION_BUILD
        or observations["protocol"] != 2225
    ):
        raise ValueError("mining observations are for a different BDS build or palette")
    profiles = observations["profiles"]
    speed_profiles = observations["speed_profiles"]
    qualification = observations["qualification"]
    by_block = {}
    speeds = {}
    for name, profile_id in observations["blocks"].items():
        row, speed_id = profiles[profile_id]
        by_block[name] = row
        speeds[name] = speed_profiles[speed_id]
    qualification["by_block"] = by_block
    return build_rules(block_types, items, blocks, qualification=qualification, speeds=speeds)


def build_rules(block_types, items, blocks, *, qualification, speeds) -> dict[str, MiningRule | None]:
    """Combine BDS tool eligibility and unmodified speed observations."""
    requires_tool = {entry["name"]: entry["requires_correct_tool_for_drops"] for entry in block_types}
    item_names = {entry["name"] for entry in items}
    destructible = {}
    for state in blocks:
        name = state["name"]
        can_break = state["destroy_speed"] >= 0 and name not in _NON_HARVESTABLE
        if destructible.setdefault(name, can_break) != can_break:
            raise ValueError(f"mining rules need per-state observations for {name}")
    if destructible.keys() != requires_tool.keys():
        raise ValueError("block states and block types disagree on vanilla names")

    if (
        qualification["schema"] != 1
        or qualification["scope"] != "default_permutations_unenchanted"
        or qualification["reference_commit"] != _BEDROCK_DATA_REVISION
    ):
        raise ValueError("mining qualification uses a different sampling scope or block registry")
    tools = qualification["tools"]
    if not tools or tools[0] is not None or len(set(tools)) != len(tools) or set(tools[1:]) - item_names:
        raise ValueError("mining qualification includes invalid tools")
    by_block = qualification["by_block"]
    if by_block.keys() != requires_tool.keys():
        raise ValueError("mining qualification does not cover the vanilla block registry")
    if unknown_blocks := speeds.keys() - requires_tool.keys():
        raise ValueError(f"mining speeds include unknown blocks: {sorted(unknown_blocks)}")

    rules = {}
    for name, can_break in destructible.items():
        row = by_block[name]
        results = set(row)
        if len(row) != len(tools) or results - {"A", "D", "E", "I", "?"}:
            raise ValueError(f"invalid mining qualification for {name}")
        if "?" in results:
            raise ValueError(f"unresolved mining qualification for {name}")
        needs_tool = requires_tool[name]
        if "A" in results:
            # Undefined array entries carry no tool eligibility information.
            if results != {"A"} or needs_tool:
                raise ValueError(f"unresolved mining qualification for {name}")
        elif needs_tool != (row[0] == "I"):
            raise ValueError(f"BDS bare-hand eligibility disagrees with requires_correct_tool_for_drops for {name}")
        if not can_break or results == {"I"}:
            rules[name] = None
            continue
        if name not in speeds:
            raise ValueError(f"missing mining speed observations for {name}")
        block_speeds = speeds[name]
        if not isinstance(block_speeds, dict):
            raise ValueError(f"invalid mining speeds for {name}")
        if unknown_items := block_speeds.keys() - item_names:
            raise ValueError(f"mining observation for {name} names unknown items: {sorted(unknown_items)}")
        for item, speed in block_speeds.items():
            if type(speed) not in (int, float) or not isfinite(speed) or speed <= 0:
                raise ValueError(f"invalid base speed for {name} with {item}: {speed!r}")
        if needs_tool:
            required = frozenset(tool for tool, result in zip(tools[1:], row[1:], strict=True) if result in {"D", "E"})
        else:
            if "I" in row:
                raise ValueError(f"mining rule cannot represent tool restrictions with bare hands for {name}")
            required = None
        rules[name] = (required, dict(block_speeds))
    return rules


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _check_build(log: Path, build: int) -> None:
    evidence = log.read_text(encoding="utf-8")
    if f"Version: {_BDS_VERSION}\n" not in evidence or f"Build ID: {build}\n" not in evidence:
        raise ValueError(f"unexpected BDS build in {log}")


def _compact_observations(
    native: Path, qualification_file: Path, state_summary: Path, loot_sources: Sequence[Sequence[Path]],
) -> dict:
    _check_build(native / "server.log", _NATIVE_BUILD)
    matrix = json.loads((native / "compact.json").read_bytes())
    audit = json.loads((native / "audit.json").read_bytes())
    if _sha256(native / "raw.jsonl.gz") != audit["raw_sha256"]:
        raise ValueError("native mining data differs from its audit")
    if audit["state_dependent_names"] or matrix["unknown_speed"]:
        raise ValueError("native data has state-dependent profiles or missing tool speeds")
    qualification = json.loads(qualification_file.read_bytes())
    expected_digests = [source["sha256"] for source in qualification["raw_sources"]]
    loot_digests = []
    for console, raw_loot in loot_sources:
        _check_build(console, _QUALIFICATION_BUILD)
        loot_digests.append(_sha256(raw_loot))
    if sorted(loot_digests) != sorted(expected_digests):
        raise ValueError("loot data differs from the qualification matrix source digests")
    state_check = json.loads(state_summary.read_bytes())
    if state_check["differences_from_default"]:
        raise ValueError("loot data has state-dependent qualification")
    if matrix["blocks"].keys() != qualification["by_block"].keys():
        raise ValueError("native mining and loot observations disagree on block names")

    profile_ids = {}
    blocks = {}
    for name, row in sorted(qualification.pop("by_block").items()):
        profile = row, matrix["blocks"][name]
        blocks[name] = profile_ids.setdefault(profile, len(profile_ids))
    return {
        "bedrock_data_revision": _BEDROCK_DATA_REVISION,
        "bds_version": _BDS_VERSION,
        "protocol": 2225,
        "native_build": _NATIVE_BUILD,
        "qualification_build": _QUALIFICATION_BUILD,
        "native_raw_sha256": audit["raw_sha256"],
        "qualification_state_summary_sha256": _sha256(state_summary),
        "qualification": {
            **{key: qualification[key] for key in ("schema", "scope", "reference_commit", "tools")},
            "raw_sources": [{"sha256": digest} for digest in expected_digests],
        },
        "speed_profiles": [profile["speed"] for profile in matrix["profiles"]],
        "profiles": list(profile_ids),
        "blocks": blocks,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compact BDS mining observations for table generation.",
        epilog="Run generate_harvest_data.py with the pinned bedrock-data export to use the bundled observations.",
    )
    parser.add_argument("native", type=Path, help="directory containing compact.json, audit.json, raw.jsonl.gz and server.log")
    parser.add_argument(
        "qualification", type=Path,
        help="JSON matrix with tools, by_block results and SHA-256 digests in raw_sources",
    )
    parser.add_argument("state_summary", type=Path, help="JSON comparison of state and default-permutation qualifications")
    parser.add_argument(
        "--loot-source", action="append", nargs=2, type=Path, required=True, metavar=("CONSOLE", "RAW_LOOT"),
        help="BDS console log and raw JSONL used by the qualification matrix (repeat for each source)",
    )
    parser.add_argument("--check", action="store_true", help="compare against the bundled observations without writing")
    args = parser.parse_args(argv)
    data = _compact_observations(args.native, args.qualification, args.state_summary, args.loot_source)
    payload = (json.dumps(data, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if args.check:
        if payload != _OBSERVATIONS.read_bytes():
            raise SystemExit(f"{_OBSERVATIONS.name} differs from the supplied observations")
    else:
        _OBSERVATIONS.write_bytes(payload)
    print(f"{_OBSERVATIONS.name}: {len(data['speed_profiles'])} speed profiles, {len(data['blocks'])} block names")


if __name__ == "__main__":
    main()
