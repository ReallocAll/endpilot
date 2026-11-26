"""Print the compression settings advertised by a NetherNet HTTP endpoint."""

import argparse
import asyncio

from endpilot.login import request_network_settings
from endpilot.protocol import Protocol
from endpilot.transport.nethernet import connect


async def main(url: str) -> None:
    connection = await connect(url)
    try:
        async with asyncio.timeout(10):
            settings = await request_network_settings(connection, Protocol())
        print(settings.compression_algorithm, settings.compression_threshold)
    finally:
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP signaling URL")
    asyncio.run(main(parser.parse_args().url))
