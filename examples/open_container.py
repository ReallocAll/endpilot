"""Open a server-created chest; the server's ContainerOpenPacket confirms the window."""
import argparse
import asyncio
import json
import sys

from endpilot import Bot


async def run(url: str, take_count: int = 0, destination_slot: int = 0,
              put_back_count: int = 0, equip_armor_slot: int | None = None) -> None:
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
            response = await bot.take_from_container(0, destination_slot, take_count)
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
        await asyncio.to_thread(sys.stdin.readline)
        closed = await bot.close_container()
        print("container_closed=" + json.dumps({
            "id": None if closed is None else closed.container_id,
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
    args = parser.parse_args()
    asyncio.run(run(args.url, args.take_count, args.destination_slot,
                    args.put_back_count, args.equip_armor_slot))
