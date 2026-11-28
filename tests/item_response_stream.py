"""Deliver existing mock replies through the authoritative inventory subscriber."""

from unittest.mock import AsyncMock

from endpilot.inventory import Inventory


def feed_item_replies(inventory, *, timeout=15):
    packets = inventory.packets
    send, fixture_reply = packets.send, packets.wait_for_packet
    response_id = packets.protocol.binding.ItemStackResponsePacket.ID

    async def send_and_deliver(packet, *, _before_send=None):
        if _before_send is not None:
            _before_send()
        await send(packet)
        request_id = int(packet.requests[0].client_request_id.raw_id)
        delivered = False

        def deliver(raw):
            nonlocal delivered
            matched = any(int(reply.client_request_id.raw_id) == request_id
                          for reply in packets.protocol.decode(raw).responses)
            inventory._on_packet(raw)
            delivered |= matched
            return matched

        raw = await fixture_reply(response_id, timeout=timeout, match=deliver)
        if not delivered:
            deliver(raw)

    packets.send = AsyncMock(side_effect=send_and_deliver)


def item_fixture(packets, slots=None, *, request_id=-1, timeout=15):
    if not hasattr(packets, "subscribe_raw"):
        packets.subscribe_raw = lambda callback: lambda: None
    inventory = Inventory(packets, 1)
    if slots is not None:
        inventory._slots = slots
    inventory._next_request_id = request_id
    feed_item_replies(inventory, timeout=timeout)
    return inventory
