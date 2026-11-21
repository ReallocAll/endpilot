"""Open a server-created chest; the server's ContainerOpenPacket confirms the window."""
import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str, take_count: int = 0, destination_slot: int = 0,
              put_back_count: int = 0, equip_armor_slot: int | None = None,
              expect_server_close: bool = False, drop_count: int = 0,
              split_slot: int | None = None, chat_during_take: bool = False,
              server_grant_during_take: bool = False,
              swap_slot: int | None = None, swap_late_grant: bool = False,
              split_late_grant: bool = False,
              unequip_after_equip: bool = False) -> None:
    if server_grant_during_take and not chat_during_take:
        raise ValueError("server grant scenario requires --chat-during-take")
    bot = Bot("EndPilotTest")
    target = (1, 100, 0)
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print("spawned EndPilotTest", flush=True)
        # Testbed places the chest after the watch is armed.
        await asyncio.to_thread(sys.stdin.readline)
        pending = asyncio.create_task(bot.wait_for_block_update(target))
        await asyncio.sleep(0)
        print("chest_watch_ready", flush=True)
        observed = await pending
        print(f"chest_runtime_id={observed.runtime_id}", flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        window = await bot.open_container(target, face=4)
        print("container_opened=" + json.dumps({
            "id": window.container_id, "type": window.container_type,
            "position": window.position, "entity_unique_id": window.entity_unique_id,
        }), flush=True)
        slots = await bot.wait_for_container_slots()
        print("container_slots=" + json.dumps({
            "count": len(slots),
            "nonempty": [
                {"slot": slot, "item_id": int(item.id), "stack_size": int(item.stack_size)}
                for slot, item in sorted(slots.items()) if int(item.id) != 0
            ],
        }), flush=True)
        if take_count:
            if chat_during_take:
                packets = bot.packets
                send = packets.send
                binding = bot.protocol.binding
                request_sent = asyncio.Event()
                response_received = asyncio.Event()
                request_id = None

                def observe_response(raw):
                    if request_id is None or raw.packet_id != binding.ItemStackResponsePacket.ID:
                        return
                    packet = bot.protocol.decode(raw)
                    if any(
                        int(reply.client_request_id.raw_id) == request_id
                        for reply in packet.responses
                    ):
                        response_received.set()

                unsubscribe = packets.subscribe_raw(observe_response)

                async def send_and_signal(packet):
                    nonlocal request_id
                    is_request = isinstance(packet, binding.ItemStackRequestPacket)
                    if is_request:
                        request_id = int(packet.requests[0].client_request_id.raw_id)
                    await send(packet)
                    if is_request:
                        request_sent.set()

                packets.send = send_and_signal
                taking = asyncio.create_task(
                    bot.take_from_container(0, destination_slot, take_count)
                )
                taking.add_done_callback(lambda _: request_sent.set())
                try:
                    # Confirm the request reached the transport before sending chat.
                    await request_sent.wait()
                    packets.send = send
                    if taking.done() or response_received.is_set():
                        await taking
                        raise AssertionError(
                            "the matching item-stack response arrived before chat submission"
                        )
                    await bot.send_chat("endpilot-container-interleave")
                    pending_at_chat = not taking.done() and not response_received.is_set()
                    if not pending_at_chat:
                        await taking
                        raise AssertionError(
                            "the matching item-stack response arrived during chat submission"
                        )
                    if server_grant_during_take:
                        print("server_grant_after_request_sent", flush=True)
                        await asyncio.to_thread(sys.stdin.readline)
                    response = await taking
                    print("chat_during_take=" + json.dumps({
                        "message": "endpilot-container-interleave",
                        "item_request_pending_at_chat": pending_at_chat,
                    }), flush=True)
                finally:
                    packets.send = send
                    unsubscribe()
                    if not taking.done():
                        taking.cancel()
                        await asyncio.gather(taking, return_exceptions=True)
            else:
                response = await bot.take_from_container(0, destination_slot, take_count)
            if server_grant_during_take:
                count = await bot.wait_for_inventory_item_count(
                    int(slots[0].id), take_count + 1, timeout=15
                )
                granted = bot.inventory_slots.get(4)
                if (granted is None or int(granted.id) != int(slots[0].id)
                        or int(granted.stack_size) != 1):
                    raise AssertionError(f"unexpected server-granted slot 4: {granted}")
                print("server_grant_observed=" + json.dumps({
                    "slot": 4, "stack_size": int(granted.stack_size),
                    "item_id": int(granted.id), "inventory_count": count,
                }), flush=True)
            print("container_take_response=" + json.dumps({
                "request_id": int(response.client_request_id.raw_id),
                "result": response.result.name,
                "chest_slot_count": int(bot.container_slots[0].stack_size),
                "inventory_count": bot.inventory_count(int(item.id)) if (
                    item := slots[0]
                ) else None,
            }), flush=True)
        if put_back_count:
            response = await bot.put_into_container(destination_slot, 0, put_back_count)
            print("container_put_response=" + json.dumps({
                "request_id": int(response.client_request_id.raw_id),
                "result": response.result.name,
                "chest_slot_count": int(bot.container_slots[0].stack_size),
                "inventory_count": bot.inventory_count(int(slots[0].id)),
            }), flush=True)
        if expect_server_close:
            pending = asyncio.create_task(bot.wait_for_container_close(timeout=15))
            await asyncio.sleep(0)
            print("server_close_watch_ready", flush=True)
            closed_packet = await pending
            print("server_container_closed=" + json.dumps({
                "id": int(closed_packet.container_id),
                "server_initiated": bool(closed_packet.server_initiated_close),
                "open_window": bot.open_window is not None,
            }), flush=True)
        else:
            await asyncio.to_thread(sys.stdin.readline)
            closed = await bot.close_container()
            print("container_closed=" + json.dumps({
                "id": None if closed is None else closed.container_id,
                "open_window": bot.open_window is not None,
            }), flush=True)
        if split_slot is not None:
            screen = await bot.open_inventory_screen()
            print("player_inventory_opened=" + json.dumps({
                "id": screen.container_id, "type": screen.container_type,
            }), flush=True)
            source = bot.inventory_slots[destination_slot]
            if split_late_grant:
                packets = bot.packets
                wait_for_packet = packets.wait_for_packet
                reply_received = asyncio.Event()
                release_reply = asyncio.Event()

                async def delayed_wait(packet_id, *, timeout, match=None):
                    raw = await wait_for_packet(packet_id, timeout=timeout, match=match)
                    if packet_id == bot.protocol.binding.ItemStackResponsePacket.ID:
                        reply_received.set()
                        await release_reply.wait()
                    return raw

                packets.wait_for_packet = delayed_wait
                pending_split = asyncio.create_task(
                    bot.move_inventory_item(destination_slot, split_slot, 1)
                )
                try:
                    async with asyncio.timeout(15):
                        await reply_received.wait()
                    print("split_reply_received_before_grant", flush=True)
                    await asyncio.to_thread(sys.stdin.readline)
                    async with asyncio.timeout(15):
                        while True:
                            current = bot.inventory_slots.get(split_slot)
                            if (current is not None
                                    and int(current.id) != int(source.id)
                                    and int(current.id) != 0):
                                break
                            await asyncio.sleep(0.05)
                    release_reply.set()
                    response = await pending_split
                finally:
                    release_reply.set()
                    packets.wait_for_packet = wait_for_packet
                    if not pending_split.done():
                        pending_split.cancel()
                        await asyncio.gather(pending_split, return_exceptions=True)
            else:
                response = await bot.move_inventory_item(destination_slot, split_slot, 1)
            source_after = bot.inventory_slots[destination_slot]
            target_after = bot.inventory_slots[split_slot]
            print("inventory_split_response=" + json.dumps({
                "request_id": int(response.client_request_id.raw_id),
                "result": response.result.name,
                "source_before": int(source.stack_size),
                "source_after": int(source_after.stack_size),
                "target_count": int(target_after.stack_size),
                "source_item_id": int(source_after.id),
                "target_item_id": int(target_after.id),
            }), flush=True)
            screen_closed = await bot.close_container()
            print("player_inventory_closed=" + json.dumps({
                "id": None if screen_closed is None else screen_closed.container_id,
                "open_window": bot.open_window is not None,
            }), flush=True)
        if swap_slot is not None:
            # The Testbed grants a second kind of item in a different slot.
            print("swap_ready", flush=True)
            await asyncio.to_thread(sys.stdin.readline)
            async with asyncio.timeout(15):
                while True:
                    other = bot.inventory_slots.get(swap_slot)
                    if other is not None and int(other.id) != 0:
                        break
                    await asyncio.sleep(0.05)
            opened = await bot.open_inventory_screen()
            before_source = bot.inventory_slots[destination_slot]
            before_target = bot.inventory_slots[swap_slot]
            if swap_late_grant:
                # Testbed gates only the local waiter; BDS receives the actual
                # request and sends its real reply before granting a newer item.
                packets = bot.packets
                wait_for_packet = packets.wait_for_packet
                reply_received = asyncio.Event()
                release_reply = asyncio.Event()

                async def delayed_wait(packet_id, *, timeout, match=None):
                    raw = await wait_for_packet(packet_id, timeout=timeout, match=match)
                    if packet_id == bot.protocol.binding.ItemStackResponsePacket.ID:
                        reply_received.set()
                        await release_reply.wait()
                    return raw

                packets.wait_for_packet = delayed_wait
                pending_swap = asyncio.create_task(
                    bot.swap_inventory_items(destination_slot, swap_slot)
                )
                try:
                    async with asyncio.timeout(15):
                        await reply_received.wait()
                    print("swap_reply_received_before_grant", flush=True)
                    await asyncio.to_thread(sys.stdin.readline)
                    async with asyncio.timeout(15):
                        while True:
                            current = bot.inventory_slots.get(destination_slot)
                            if (current is not None
                                    and int(current.id) not in (
                                        int(before_source.id), int(before_target.id)
                                    )):
                                break
                            await asyncio.sleep(0.05)
                    release_reply.set()
                    response = await pending_swap
                finally:
                    release_reply.set()
                    packets.wait_for_packet = wait_for_packet
                    if not pending_swap.done():
                        pending_swap.cancel()
                        await asyncio.gather(pending_swap, return_exceptions=True)
            else:
                response = await bot.swap_inventory_items(destination_slot, swap_slot)
            current_source = bot.inventory_slots.get(destination_slot)
            current_target = bot.inventory_slots.get(swap_slot)
            print("inventory_swap_response=" + json.dumps({
                "request_id": int(response.client_request_id.raw_id),
                "result": response.result.name,
                "source_before": [int(before_source.id), int(before_source.stack_size)],
                "target_before": [int(before_target.id), int(before_target.stack_size)],
                "source_after": None if current_source is None else
                [int(current_source.id), int(current_source.stack_size)],
                "target_after": None if current_target is None else
                [int(current_target.id), int(current_target.stack_size)],
                "window_id": opened.container_id,
            }), flush=True)
            await bot.close_container()
        if drop_count:
            screen = await bot.open_inventory_screen()
            print("player_inventory_opened=" + json.dumps({
                "id": screen.container_id, "type": screen.container_type,
            }), flush=True)
            drop_waiter = asyncio.create_task(bot.wait_for_item_drop(timeout=15))
            try:
                await asyncio.sleep(0)
                response = await bot.drop_inventory_item(destination_slot, drop_count)
                if response.result.name != "SUCCESS":
                    raise AssertionError(f"BDS rejected inventory drop: {response.result.name}")
                drop = await drop_waiter
            finally:
                if not drop_waiter.done():
                    drop_waiter.cancel()
                    await asyncio.gather(drop_waiter, return_exceptions=True)
            source = bot.inventory_slots.get(destination_slot)
            print("inventory_drop_response=" + json.dumps({
                "request_id": int(response.client_request_id.raw_id),
                "result": response.result.name,
                "inventory_count": None if source is None else int(source.stack_size),
                "item_id": None if source is None else int(source.id),
                "drop_item_id": drop.item_id,
                "drop_count": drop.item_count,
                "drop_runtime_id": drop.runtime_id,
            }), flush=True)
            screen_closed = await bot.close_container()
            print("player_inventory_closed=" + json.dumps({
                "id": None if screen_closed is None else screen_closed.container_id,
                "open_window": bot.open_window is not None,
            }), flush=True)
        if equip_armor_slot is not None:
            inventory_window = await bot.open_inventory_screen()
            print("player_inventory_opened=" + json.dumps({
                "id": inventory_window.container_id,
                "type": inventory_window.container_type,
            }), flush=True)
            source = bot.inventory_slots.get(destination_slot)
            previous = bot.armor_slots.get(equip_armor_slot)
            print("armor_equip_before=" + json.dumps({
                "inventory_item_id": None if source is None else int(source.id),
                "inventory_net_id": None if source is None else source.net_id_variant,
                "armor_item_id": None if previous is None else int(previous.id),
                "armor_net_id": None if previous is None else previous.net_id_variant,
            }), flush=True)
            response = await bot.equip_armor(destination_slot, equip_armor_slot)
            armor = bot.armor_slots.get(equip_armor_slot)
            print("armor_equip_response=" + json.dumps({
                "request_id": int(response.client_request_id.raw_id),
                "result": response.result.name,
                "armor_item_id": None if armor is None else int(armor.id),
                "armor_count": None if armor is None else int(armor.stack_size),
                "inventory_count": bot.inventory_count(int(slots[0].id)),
            }), flush=True)
            if unequip_after_equip:
                print("armor_unequip_ready", flush=True)
                await asyncio.to_thread(sys.stdin.readline)  # Testbed checks equipped BDS slot.
                retrieved = await bot.unequip_armor(equip_armor_slot, destination_slot)
                returned = bot.inventory_slots.get(destination_slot)
                remaining = bot.armor_slots.get(equip_armor_slot)
                print("armor_unequip_response=" + json.dumps({
                    "request_id": int(retrieved.client_request_id.raw_id),
                    "result": retrieved.result.name,
                    "inventory_item_id": None if returned is None else int(returned.id),
                    "inventory_count": None if returned is None else int(returned.stack_size),
                    "armor_item_id": None if remaining is None else int(remaining.id),
                    "armor_count": None if remaining is None else int(remaining.stack_size),
                }), flush=True)
            inventory_closed = await bot.close_container()
            print("player_inventory_closed=" + json.dumps({
                "id": None if inventory_closed is None else inventory_closed.container_id,
                "open_window": bot.open_window is not None,
            }), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        await bot.close()
    print("disconnected EndPilotTest", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--take-count", type=int, default=0,
                        help="after opening, take this many items from chest slot 0")
    parser.add_argument("--destination-slot", type=int, default=0)
    parser.add_argument("--put-back-count", type=int, default=0,
                        help="return this many items from destination slot to chest slot 0")
    parser.add_argument("--equip-armor-slot", type=int, default=None,
                        help="after closing the chest, equip the taken item into armor slot 0-3")
    parser.add_argument("--unequip-after-equip", action="store_true",
                        help="take the helmet back into inventory after an equip")
    parser.add_argument("--drop-count", type=int, default=0,
                        help="after closing chest, drop from the destination inventory slot")
    parser.add_argument("--expect-server-close", action="store_true",
                        help="observe server-initiated window close instead of requesting it")
    parser.add_argument("--split-slot", type=int, default=None,
                        help="after closing the chest, split one item into this inventory slot")
    parser.add_argument("--split-late-grant", action="store_true",
                        help="Testbed: hold the split reply until a newer server slot update")
    parser.add_argument("--chat-during-take", action="store_true",
                        help="send player chat without blocking on the chest item response")
    parser.add_argument("--server-grant-during-take", action="store_true",
                        help="Testbed independently grants inventory after the stack request is sent")
    parser.add_argument("--swap-slot", type=int, default=None,
                        help="swap the taken chest stack with a Testbed-granted inventory item")
    parser.add_argument("--swap-late-grant", action="store_true",
                        help="Testbed: hold the swap reply until a newer server slot update")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.take_count, args.destination_slot,
                    args.put_back_count, args.equip_armor_slot, args.expect_server_close,
                    args.drop_count, args.split_slot, args.chat_during_take,
                    args.server_grant_during_take, args.swap_slot,
                    args.swap_late_grant, args.split_late_grant,
                    args.unequip_after_equip))
