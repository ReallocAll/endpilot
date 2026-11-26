"""Open a nearby chest, inspect its contents and optionally move items by slot."""

import argparse
import asyncio
import json

from endpilot import Bot


def report_move(action: str, response) -> None:
    print(json.dumps({
        "action": action, "request_id": int(response.client_request_id.raw_id),
        "result": response.result.name,
    }), flush=True)
    if response.result.name != "SUCCESS":
        raise RuntimeError(f"The server rejected {action}: {response.result.name}.")


async def run(
    url: str, name: str, target: tuple[int, int, int], *, face: int = 4,
    take_count: int = 0, destination_slot: int = 0, put_back_count: int = 0,
    split_slot: int | None = None, swap_slot: int | None = None, drop_count: int = 0,
    equip_armor_slot: int | None = None, unequip_after_equip: bool = False,
) -> None:
    bot = Bot(name)
    try:
        if url.startswith("raknet://"):
            host, sep, port = url.removeprefix("raknet://").rpartition(":")
            if not sep:
                host, port = port, "19132"
            await bot.join_raknet(host, int(port))
        else:
            await bot.join_nethernet(url)
        print(f"Joined as {name}.", flush=True)
        await asyncio.to_thread(
            input,
            f"Put a single chest at {target}, move the bot within reach of face {face}, "
            "and prepare chest slot 0 and any inventory slots used by your options. "
            "Press Enter to open the chest. ",
        )
        await bot.wait_for_position_sync()
        x, y, z = target
        await bot.request_sub_chunk(target)
        section = y // 16
        await bot.wait_for_column_coverage((x // 16, z // 16), range(section, section + 1))

        window = await bot.open_container(target, face=face)
        try:
            slots = await bot.wait_for_container_slots()
            print(json.dumps({
                "window": window.container_id, "type": window.container_type,
                "position": window.position, "slot_count": len(slots),
                "nonempty": [
                    {"slot": index, "item_id": int(item.id), "stack_size": int(item.stack_size)}
                    for index, item in sorted(slots.items()) if int(item.id) != 0
                ],
            }), flush=True)
            if take_count:
                taken = await bot.take_from_container(0, destination_slot, take_count)
                report_move("take from chest", taken)
            if put_back_count:
                stored = await bot.put_into_container(destination_slot, 0, put_back_count)
                report_move("put into chest", stored)
        finally:
            await bot.close_container()

        if split_slot is None and swap_slot is None and not drop_count and equip_armor_slot is None:
            return

        await bot.open_inventory_screen()
        try:
            if split_slot is not None:
                split = await bot.move_inventory_item(destination_slot, split_slot, 1)
                report_move("split inventory stack", split)
            if swap_slot is not None:
                swapped = await bot.swap_inventory_items(destination_slot, swap_slot)
                report_move("swap inventory stacks", swapped)
            if drop_count:
                drop_watch = asyncio.create_task(bot.wait_for_item_drop())
                try:
                    await asyncio.sleep(0)
                    dropped = await bot.drop_inventory_item(destination_slot, drop_count)
                    report_move("drop inventory stack", dropped)
                    drop = await drop_watch
                finally:
                    drop_watch.cancel()
                    await asyncio.gather(drop_watch, return_exceptions=True)
                print(json.dumps({
                    "observed_drop": drop.runtime_id, "item_id": drop.item_id,
                    "count": drop.item_count,
                }), flush=True)
            if equip_armor_slot is not None:
                equipped = await bot.equip_armor(destination_slot, equip_armor_slot)
                report_move("equip armor", equipped)
                if unequip_after_equip:
                    removed = await bot.unequip_armor(equip_armor_slot, destination_slot)
                    report_move("unequip armor", removed)
        finally:
            await bot.close_container()
    finally:
        await bot.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="NetherNet HTTP URL or raknet://host:port")
    parser.add_argument("--name", default="Knight")
    parser.add_argument("--chest", nargs=3, type=int, metavar=("X", "Y", "Z"), default=(1, 100, 0),
                        help="chest coordinates (default: 1 100 0)")
    parser.add_argument("--face", type=int, choices=range(6), default=4, help="chest face (default: west)")
    parser.add_argument("--take-count", type=int, default=0, help="take this many items from chest slot 0")
    parser.add_argument("--destination-slot", type=int, choices=range(36), default=0, metavar="SLOT",
                        help="main inventory slot 0-35 used for the transfer and other operations (default: 0)")
    parser.add_argument("--put-back-count", type=int, default=0, help="return this many items to chest slot 0")
    parser.add_argument("--split-slot", type=int, choices=range(36), metavar="SLOT",
                        help="after closing the chest, move one item into this empty inventory slot")
    parser.add_argument("--swap-slot", type=int, choices=range(36), metavar="SLOT",
                        help="swap the destination stack with another prepared inventory stack")
    parser.add_argument("--drop-count", type=int, default=0, help="drop this many items from the destination slot")
    parser.add_argument("--equip-armor-slot", type=int, choices=range(4),
                        help="equip a compatible piece from the destination slot into armor slot 0-3")
    parser.add_argument("--unequip-after-equip", action="store_true",
                        help="return the equipped piece to the destination slot")
    args = parser.parse_args()
    if min(args.take_count, args.put_back_count, args.drop_count) < 0:
        parser.error("item counts must be nonnegative")
    if args.unequip_after_equip and args.equip_armor_slot is None:
        parser.error("--unequip-after-equip requires --equip-armor-slot")
    asyncio.run(run(
        args.url, args.name, tuple(args.chest), face=args.face,
        take_count=args.take_count, destination_slot=args.destination_slot,
        put_back_count=args.put_back_count, split_slot=args.split_slot, swap_slot=args.swap_slot,
        drop_count=args.drop_count, equip_armor_slot=args.equip_armor_slot,
        unequip_after_equip=args.unequip_after_equip,
    ))
