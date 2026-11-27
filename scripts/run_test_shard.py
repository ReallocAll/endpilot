"""Run one zero-based shard of the SDK's unittest suite in CI."""

import sys
import unittest
from itertools import groupby, islice


def test_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_cases(test)
        else:
            yield test


def sharded_suite(index, total):
    discovered = unittest.defaultTestLoader.discover("tests")
    modules = groupby(test_cases(discovered), key=lambda test: type(test).__module__)
    shard = unittest.TestSuite()
    for position, (module, cases) in enumerate(modules):
        # These three modules dominate CI time. Spread their cases across runners.
        if module in {"test_navigation", "test_collection", "test_movement"}:
            shard.addTests(islice(cases, index, None, total))
        elif position % total == index:
            shard.addTests(cases)
    return shard


if __name__ == "__main__":
    index, total = map(int, sys.argv[1:])
    result = unittest.TextTestRunner(verbosity=2).run(sharded_suite(index, total))
    sys.exit(not result.wasSuccessful())
