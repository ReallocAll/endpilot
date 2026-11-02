"""A third-party extension sending a native typed packet through Bot's public API.

Call send_latency_packet(bot) after bot.wait_ready(). The packet is queued;
this example deliberately does not track its response.
"""

import bedrock_protocol as bp

from endpilot import Bot


def send_latency_packet(bot: Bot, stamp: int) -> None:
    packets = getattr(bp, f"v{bot.capture_options().protocol}")
    bot.send_packet(packets.NetworkStackLatencyPacket(create_time=stamp, from_server=False))
