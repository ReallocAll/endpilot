# Bedrock data tables

## Regenerate the protocol 2225 block tables

EndstoneMC/bedrock-data at `r26_u6@40570f03ef64e37c71a81388b52fd55da5d73521`
is authoritative for the fields it publishes, including the block palette,
names and shapes. The shared reader checks the SHA-256 of each input file.
[Pinned source](https://github.com/EndstoneMC/bedrock-data/tree/40570f03ef64e37c71a81388b52fd55da5d73521)

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
