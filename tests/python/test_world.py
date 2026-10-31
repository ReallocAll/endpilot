"""Conservative two-layer Bedrock standability regression."""
from endpilot.world import _AIR, _Slice, _Storage, _block_hash, WorldQuery


def test_secondary_storage_is_not_assumed_safe():
    world = WorldQuery(object())
    world._hashes = True
    world._dimension = 0
    world._set((0, 6, 0), _Slice((_Storage(0, b"", (_AIR,)),)))
    world._update(0, 100, 0, 0, _block_hash("minecraft:stone"))
    assert world.can_stand(0, 101, 0) is True
    assert world.block(0, 101, 0).air

    # An unverified secondary water layer cannot be treated as flat air.
    world._update(0, 101, 0, 1, _block_hash("minecraft:water"))
    assert world.block(0, 101, 0).air
    assert world.can_stand(0, 101, 0) is None

    world.clear()
    assert world.can_stand(0, 101, 0) is None
