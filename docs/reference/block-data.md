# Bedrock data tables

## Look up vanilla block properties

`vanilla_blocks(protocol=2225)` returns a cached read-only mapping of canonical
block names to `VanillaBlock` records. It works before connecting to a server.

```python
from endpilot import vanilla_blocks

ore = vanilla_blocks()["minecraft:iron_ore"]
print(ore.hardness)  # 3.0
copper = ore.tool_properties("minecraft:copper_pickaxe")
print(copper.speed_multiplier, copper.qualifies_for_drops)  # 5.0 True
gold = ore.tool_properties("minecraft:golden_pickaxe")
print(gold.speed_multiplier, gold.qualifies_for_drops)  # 12.0 False
```

`hardness` is the pinned block palette's raw `destroy_speed` value.
`tool_properties()` covers the 48 measured, unenchanted items. Measured
tools with no speed bonus have multiplier `1.0`. A result of `None`
means the item is outside the measured set or the block has no mining rule.
A result with `qualifies_for_drops=None` means that the base speed is known
and drop eligibility is unknown. Eligibility describes whether the tool
satisfies the ordinary drop requirement. Actual drops depend on the block's
loot table and the conditions of the break.

## Regenerate the protocol 2225 block tables

EndstoneMC/bedrock-data at `r26_u6@40570f03ef64e37c71a81388b52fd55da5d73521`
is authoritative for the fields it publishes, including the block palette,
names and shapes. The shared reader checks the SHA-256 of each input file.
[Pinned source](https://github.com/EndstoneMC/bedrock-data/tree/40570f03ef64e37c71a81388b52fd55da5d73521)

`generate_outline_data.py` produces the outline and collision tables. The
collision table takes the ordered `collision_shapes` arrays for its supported
states from `block_palette.json`, including boxes that extend outside a block.
Both block network ID modes use the same palette entries.

The harvest table also uses the mining observations included in the source
distribution. The data records tool qualification and survival crack progress
from Windows vanilla Bedrock Dedicated Server `1.26.66.1`, build `53696234`,
and per-state mining speeds from Linux vanilla Bedrock Dedicated Server
`1.26.66.1`, build `53696239`. Both builds use protocol `2225`.
[Packaged mining observations](../../scripts/mining-observations-2225.json)

From the source tree or unpacked source distribution, pass a checkout at the
pinned revision to the generators. Python 3.12 or later and the packaged
observations are sufficient to regenerate the tables.

```sh
git clone --branch r26_u6 --single-branch https://github.com/EndstoneMC/bedrock-data.git ../bedrock-data
git -C ../bedrock-data checkout --detach 40570f03ef64e37c71a81388b52fd55da5d73521
python scripts/generate_harvest_data.py ../bedrock-data
python scripts/generate_outline_data.py ../bedrock-data
python scripts/generate_biome_data.py ../bedrock-data
```

Use `--check` on a generator command to compare the output to the
packaged table without rewriting it.
