"""Read the pinned EndstoneMC/bedrock-data export for protocol 2225."""

from hashlib import sha256
import json
from pathlib import Path


SOURCE_REVISION = "40570f03ef64e37c71a81388b52fd55da5d73521"
_FILE_DIGESTS = {
    "biomes.json": "99a7cccdfc78462b9efe6dc9f18510469bf0849d8005d01920e45f39ef57c654",
    "block_palette.json": "87943cb0348844af61e661b6474bbefe95d5f8b9425a3cb5fe754bc6dad84745",
    "block_types.json": "79a38358f8bf372436cd2ab8f27f275d041d447f1068564172d549450b3f29d5",
    "items.json": "1f8e3bfd732edcd306588a411c240c51c520354b63293e352aa146bac3faeb3e",
    "voxel_shapes.json": "cff7a0bc8715f4669747f3f41e7401f11e2c7c7949773edb2c7af5579fd1085b",
}


def load_json(root: Path | str, filename: str):
    """Return a complete JSON document after verifying its original file bytes."""
    expected = _FILE_DIGESTS.get(filename)
    if expected is None:
        raise ValueError(f"unpinned bedrock-data file {filename!r}")
    path = Path(root) / filename
    raw = path.read_bytes()
    actual = sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(f"{path} does not match bedrock-data {SOURCE_REVISION}: SHA-256 {actual}, expected {expected}")
    return json.loads(raw)
