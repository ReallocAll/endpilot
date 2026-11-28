import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

from endpilot import CreativeArmorClearResult, CreativeCraftingGridClearResult
from endpilot.protocol import RawPacket

import test_creative_inventory as creative


GRID = "CRAFTING_INPUT_CONTAINER"
PLAYER = "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER"


class CreativeGridClearTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = creative.CreativeInventoryTests.asyncSetUp
    join = creative.CreativeInventoryTests.join
    deliver = creative.CreativeInventoryTests.deliver
    open = creative.CreativeInventoryTests.open
    close = creative.CreativeInventoryTests.close
    observed_slot = creative.CreativeInventoryTests.observed_slot
    item_slot = creative.CreativeInventoryTests.item_slot
    request = creative.CreativeInventoryTests.request
    transfer_reply = creative.CreativeInventoryTests.transfer_reply
    check_action = creative.CreativeInventoryTests.check_armor_action

    def grid_slot(self, slot, item_id=0, *, net_id=22):
        item = self.observed_slot(slot, item_id).item
        if item_id:
            item.net_id_variant = net_id
        return self.b.InventorySlotPacket(inventory_id=self.b.ContainerID.PLAYER_ONLY_UI, slot=slot, item=item)

    def response(self, request, *corrections, result="SUCCESS"):
        b = self.b
        return b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult[result],
            containers=[b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[domain]),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=slot, slot=slot, amount=amount,
                    item_stack_net_id=b.ItemStackNetId(raw_id=identity) if identity else None,
                )],
            ) for domain, slot, amount, identity in corrections] if result == "SUCCESS" else None,
        )])

    async def ready(self, *packets):
        await self.join(self.mode.CREATIVE)
        b = self.b
        await self.deliver(
            self.open(), self.grid_slot(28, 7), self.grid_slot(29), self.grid_slot(31),
            self.observed_slot(0), self.observed_slot(1, 7), self.observed_slot(2), self.observed_slot(3),
            b.ItemRegistryPacket(items=[b.ItemData(id=i, name=f"minecraft:test_{i}") for i in (7, 8, 9)]),
            b.CraftingDataPacket(shapeless_recipes=[b.ShapelessRecipePayload(
                recipe_id=f"test:outputs_{count}", net_id=10 + count, tag="crafting_table",
                ingredients=[b.SerializedRecipeIngredient(descriptor={"name": "minecraft:test_7"}, stack_size=1)],
                results=[b.SerializedNetworkItemInstanceDescriptor(id=i, stack_size=1)
                         for i in range(8, 8 + count)],
            ) for count in (1, 2)]),
            *packets,
        )

    def clear(self, **kwargs):
        return self.bot.clear_creative_inventory([], include_crafting_grid=True, **kwargs)

    def edit(self, operation, slot=28):
        match operation:
            case "stage":
                return self.bot.stage_crafting_ingredient(1, slot)
            case "return":
                return self.bot.return_crafting_ingredient(slot, 0)
            case "single craft":
                return self.bot.craft_recipe_grid(11, 2)
            case "multi craft":
                return self.bot.craft_recipe_grid(12, 2, additional_destination_slots=(3,))

    @asynccontextmanager
    async def hold_next_write(self):
        send = self.bot.packets.send
        held, release = asyncio.Event(), asyncio.Event()

        async def delay(packet, **kwargs):
            if isinstance(packet, self.b.ItemStackRequestPacket) and not held.is_set():
                held.set()
                await release.wait()
            await send(packet, **kwargs)

        with patch.object(self.bot.packets, "send", delay):
            try:
                yield held, release
            finally:
                release.set()

    async def request_for(self, task):
        wire = asyncio.create_task(self.request())
        try:
            done, _ = await asyncio.wait((task, wire), return_when=asyncio.FIRST_COMPLETED)
            if wire not in done:
                await task
                self.fail("operation completed before writing its request")
            return await wire
        finally:
            if not wire.done():
                wire.cancel()
                await asyncio.gather(wire, return_exceptions=True)

    async def test_main_armor_grid_order_and_slot_domains(self):
        await self.ready(self.grid_slot(30, 9), self.observed_slot(28, 7),
                         self.item_slot("ARMOR", 0, 25), *(self.item_slot("ARMOR", i, None) for i in (1, 2, 3)))
        changes = []
        self.bot.subscribe_inventory_changes(changes.append)
        task = asyncio.create_task(self.bot.clear_creative_inventory(
            [28], include_armor=True, include_crafting_grid=True,
        ))
        main = await self.request()
        self.check_action(main, "DestroyActionData", PLAYER, 28, 22, 3)
        await self.deliver(self.response(main, (PLAYER, 28, 0, None)))
        take = await self.request()
        self.check_action(take, "TakeActionData", "ARMOR_CONTAINER", 0, 25)
        await self.deliver(self.transfer_reply(take))
        armor = await self.request()
        self.check_action(armor, "DestroyActionData", PLAYER, 0, 51)
        await self.deliver(self.response(armor, (PLAYER, 0, 0, None)))
        changes.clear()
        grid = await self.request()
        self.check_action(grid, "DestroyActionData", GRID, 28, 22, 3)
        await self.deliver(self.response(grid, (GRID, 28, 0, None)))
        grid2 = await self.request()
        self.check_action(grid2, "DestroyActionData", GRID, 30, 22, 3)
        await self.deliver(self.response(grid2, result="ERROR"))
        result = await task
        self.assertIs(type(result), CreativeCraftingGridClearResult)
        self.assertIsInstance(result, CreativeArmorClearResult)
        self.assertEqual([(row.slot, row.status) for row in result.slots], [(28, "cleared")])
        self.assertEqual([row.status for row in result.armor], ["cleared"] + ["already_empty"] * 3)
        self.assertEqual([(row.slot, row.status) for row in result.crafting_grid],
                         [(28, "cleared"), (29, "already_empty"), (30, "rejected"), (31, "already_empty")])
        self.assertFalse(result.complete)
        self.assertEqual(changes, [])
        self.assertTrue(self.transport.sent.empty())

    async def test_success_requires_matching_grid_evidence_and_preserves_trusted_replacement(self):
        for evidence in ("missing", "wrong domain", "replacement", "replaced and restored", "trusted empty"):
            with self.subTest(evidence=evidence):
                await self.ready()
                task = asyncio.create_task(self.clear())
                request = await self.request()
                packets = []
                correction = ((GRID, 28, 0, None),)
                match evidence:
                    case "missing":
                        correction = ()
                    case "wrong domain":
                        correction = ((PLAYER, 28, 0, None),)
                    case "replacement" | "replaced and restored":
                        packets.append(self.grid_slot(28, 8))
                        if evidence == "replaced and restored":
                            packets.append(self.grid_slot(28, 7))
                    case "trusted empty":
                        packets.append(self.grid_slot(28))
                        correction = ()
                await self.deliver(*packets, self.response(request, *correction))
                result = await task
                self.assertEqual(result.armor, ())
                self.assertEqual([row.status for row in result.crafting_grid], [
                    "cleared" if evidence == "trusted empty" else "unconfirmed",
                    "already_empty", "unknown", "already_empty",
                ])
                item = self.bot._recipes._grid_slots.get(28)
                self.assertEqual(None if item is None else item.id, {
                    "missing": None, "wrong domain": None, "replacement": 8,
                    "replaced and restored": 7, "trusted empty": 0,
                }[evidence])
                await self.bot.close()

    async def test_write_rechecks_creative_current_window_and_grid_version(self):
        for change, error, message in (
            ("mode", PermissionError, "granted creative mode"),
            ("window", RuntimeError, "window changed"),
            ("observed", RuntimeError, "inputs changed"),
        ):
            with self.subTest(change=change):
                await self.ready(self.grid_slot(30, 9))
                async with self.hold_next_write() as (held, release):
                    task = asyncio.create_task(self.clear())
                    await asyncio.wait_for(held.wait(), 1)
                    if change == "mode":
                        await self.deliver(self.b.SetPlayerGameTypePacket(player_game_type=self.mode.SURVIVAL))
                    elif change == "window":
                        await self.deliver(self.close(), self.open(), self.grid_slot(28, 7))
                    else:
                        await self.deliver(self.grid_slot(28, 8), self.grid_slot(28, 7))
                    release.set()
                    with self.assertRaisesRegex(error, message) as caught:
                        await task
                progress = caught.exception.creative_progress
                step, = progress.requests
                self.assertEqual((step.slot, step.action, step.submitted, step.verdict), (28, "destroy_grid", False, None))
                self.assertEqual([row.status for row in progress.clear_result.crafting_grid],
                                 ["unconfirmed", "pending", "pending", "pending"])
                self.assertTrue(self.transport.sent.empty())
                await self.bot.close()

    async def test_same_slot_conflict_at_write_in_both_directions(self):
        for operation in ("stage", "return", "single craft", "multi craft"):
            for first in ("clear", "edit"):
                with self.subTest(operation=operation, first=first):
                    await self.ready()
                    async with self.hold_next_write() as (held, release):
                        # Plan the loser first. A plan alone does not reserve the wire slot.
                        loser = asyncio.create_task(self.edit(operation) if first == "clear" else self.clear())
                        await asyncio.wait_for(held.wait(), 1)
                        winner = asyncio.create_task(self.clear() if first == "clear" else self.edit(operation))
                        request = await self.request()
                        release.set()
                        with self.assertRaisesRegex(RuntimeError, "pending .*request") as caught:
                            await loser
                        if first == "edit":
                            step, = caught.exception.creative_progress.requests
                            self.assertEqual((step.action, step.submitted), ("destroy_grid", False))
                        await self.deliver(self.response(request, result="ERROR"))
                        await winner
                    self.assertTrue(self.transport.sent.empty())
                    await self.bot.close()

    async def test_stale_return_plan_cannot_write_after_destroy_verdict(self):
        await self.ready()
        async with self.hold_next_write() as (held, release):
            returning = asyncio.create_task(self.edit("return"))
            await asyncio.wait_for(held.wait(), 1)
            clear = asyncio.create_task(self.clear())
            request = await self.request()
            await self.deliver(self.response(request, (GRID, 28, 0, None)))
            await clear
            release.set()
            with self.assertRaisesRegex(RuntimeError, "crafting inputs changed"):
                await returning
        self.assertTrue(self.transport.sent.empty())

    async def test_cancelled_legacy_wait_still_owns_its_actual_grid_slot(self):
        for operation in ("stage", "return", "single craft", "multi craft"):
            with self.subTest(operation=operation):
                await self.ready()
                task = asyncio.create_task(self.edit(operation))
                old_request = await self.request()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaisesRegex(RuntimeError, "pending item-stack request"):
                    await self.clear()
                self.assertTrue(self.transport.sent.empty())
                await self.deliver(self.response(old_request, result="ERROR"))
                task = asyncio.create_task(self.clear())
                await self.deliver(self.response(await self.request(), (GRID, 28, 0, None)))
                self.assertEqual((await task).crafting_grid[0].status, "cleared")
                await self.bot.close()

    async def test_undecodable_reply_reports_error_and_keeps_submitted_ownership(self):
        await self.ready()
        clear = asyncio.create_task(self.clear())
        request = await self.request()
        await self.deliver(RawPacket(self.b.ItemStackResponsePacket.ID, b""))
        with self.assertRaisesRegex(ValueError, "could not decode item-stack response") as caught:
            await clear
        step, = caught.exception.creative_progress.requests
        self.assertIs(step.submitted, True)
        self.assertIsNone(step.verdict)
        with self.assertRaisesRegex(RuntimeError, "pending creative destroy"):
            await self.edit("return")
        self.assertTrue(self.transport.sent.empty())
        await self.deliver(self.response(request, (GRID, 28, 0, None)))
        self.assertEqual(self.bot._recipes._grid_slots[28].id, 7)
        self.assertIsNone(step.verdict)
        next_clear = asyncio.create_task(self.clear())
        await self.deliver(self.response(await self.request(), result="ERROR"))
        self.assertEqual((await next_clear).crafting_grid[0].status, "rejected")

    async def test_late_recipe_corrections_keep_newer_grid_versions(self):
        for operation, player_correction, player_stack in (
            ("stage", (1, 2, 22), (7, 2, 22)),
            ("return", (0, 1, 95), (7, 1, 95)),
            ("single craft", (2, 1, 96), (8, 1, 96)),
        ):
            for restored in (False, True):
                with self.subTest(operation=operation, restored=restored):
                    await self.bot.close()
                    await self.ready(self.grid_slot(30))
                    task = asyncio.create_task(self.edit(operation))
                    request = await self.request()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    packets = [self.grid_slot(28, 8, net_id=88)]
                    expected = (8, 88)
                    if restored:
                        packets.append(self.grid_slot(28, 7))
                        expected = (7, 22)
                    await self.deliver(
                        *packets, self.response(request, (GRID, 28, 0, None), (PLAYER, *player_correction)),
                    )
                    player_item = self.bot.inventory_slots[player_correction[0]]
                    self.assertEqual((player_item.id, player_item.stack_size, player_item.net_id_variant), player_stack)
                    observed = self.bot._recipes._grid_slots[28]
                    self.assertEqual((observed.id, observed.net_id_variant), expected)
                    clear = asyncio.create_task(self.clear())
                    destroy = await self.request()
                    self.check_action(destroy, "DestroyActionData", GRID, 28, expected[1], 3)
                    await self.deliver(self.response(destroy, (GRID, 28, 0, None)))
                    self.assertTrue((await clear).complete)
                    await self.bot.close()

    async def test_single_craft_grid_freshness_is_independent_per_input(self):
        await self.ready(self.grid_slot(30, 7))
        craft = asyncio.create_task(self.edit("single craft"))
        request = await self.request()
        await self.deliver(
            self.grid_slot(28, 8, net_id=88),
            self.response(request, (GRID, 28, 0, None), (GRID, 30, 0, None), (PLAYER, 2, 1, 73)),
        )
        self.assertEqual((await craft).result.name, "SUCCESS")
        slots = self.bot._recipes._grid_slots
        self.assertEqual((slots[28].net_id_variant, slots[30].id, self.bot.inventory_slots[2].net_id_variant),
                         (88, 0, 73))

    async def test_equivalent_grid_observation_does_not_hide_valid_stage_correction(self):
        await self.ready()
        stage = asyncio.create_task(self.edit("stage", 29))
        request = await self.request()
        await self.deliver(self.grid_slot(29), self.response(request, (GRID, 29, 1, 63), (PLAYER, 1, 2, 22)))
        self.assertEqual((await stage).result.name, "SUCCESS")
        item = self.bot._recipes._grid_slots[29]
        self.assertEqual((item.id, item.stack_size, item.net_id_variant), (7, 1, 63))

    async def test_failure_before_write_releases_player_and_grid_endpoints(self):
        for workbench in (False, True):
            with self.subTest(workbench=workbench):
                await self.ready(self.grid_slot(29, 7))
                first, second = (32, 33) if workbench else (28, 29)
                if workbench:
                    b = self.b
                    await self.deliver(
                        b.ContainerOpenPacket(container_id=7, type=b.ContainerType.WORKBENCH, entity_unique_id=-1),
                        self.grid_slot(first, 7), self.grid_slot(second, 7),
                    )
                send = self.bot.packets.send

                async def fail_before_write(packet, **kwargs):
                    if isinstance(packet, self.b.ItemStackRequestPacket):
                        raise RuntimeError("controlled failure before write")
                    await send(packet, **kwargs)

                with patch.object(self.bot.packets, "send", fail_before_write):
                    with self.assertRaisesRegex(RuntimeError, "controlled failure"):
                        await self.edit("stage", first)
                self.assertTrue(self.transport.sent.empty())
                for grid_slot in (second, first):
                    task = asyncio.create_task(self.bot.return_crafting_ingredient(grid_slot, 1, timeout=0.25))
                    request = await self.request_for(task)
                    await self.deliver(self.response(request, result="ERROR"))
                    self.assertEqual((await task).result.name, "ERROR")
                await self.bot.close()

    async def test_uncertain_recipe_submission_reserves_only_personal_grid_input(self):
        for failure in ("transport", "decode"):
            with self.subTest(failure=failure):
                await self.bot.close()
                await self.ready(self.grid_slot(29, 7))
                if failure == "transport":
                    send = self.transport.send

                    async def fail_after_write(data):
                        await send(data)
                        raise OSError("controlled error after possible submission")

                    with patch.object(self.transport, "send", fail_after_write):
                        with self.assertRaisesRegex(OSError, "possible submission"):
                            await self.edit("stage")
                    old = await self.request()
                else:
                    stage = asyncio.create_task(self.edit("stage"))
                    old = await self.request()
                    await self.deliver(RawPacket(self.b.ItemStackResponsePacket.ID, b""))
                    with self.assertRaisesRegex(ValueError, "could not decode item-stack response"):
                        await stage

                with self.assertRaisesRegex(RuntimeError, "pending item-stack request"):
                    await self.clear()
                self.assertTrue(self.transport.sent.empty())
                returning = asyncio.create_task(self.bot.return_crafting_ingredient(29, 1, timeout=0.25))
                await self.deliver(self.response(await self.request_for(returning), result="ERROR"))
                self.assertEqual((await returning).result.name, "ERROR")

                # A failed request still owns its personal grid endpoint until this verdict arrives.
                await self.deliver(self.response(old, (GRID, 28, 0, None), (PLAYER, 1, 0, None)))
                self.assertEqual(self.bot.inventory_slots[1].stack_size, 3)
                clear = asyncio.create_task(self.clear())
                request = await self.request()
                self.check_action(request, "DestroyActionData", GRID, 28, 22, 3)
                await self.deliver(self.response(request, result="ERROR"))
                await self.deliver(self.response(await self.request(), result="ERROR"))
                self.assertEqual((await clear).crafting_grid[0].status, "rejected")
                await self.bot.close()

    async def test_undecodable_multi_craft_verdict_releases_player_slots_and_window_sentinel(self):
        await self.ready()
        first = asyncio.create_task(self.edit("multi craft"))
        await self.request()
        await self.deliver(RawPacket(self.b.ItemStackResponsePacket.ID, b""))
        with self.assertRaisesRegex(ValueError, "could not decode item-stack response"):
            await first
        await self.deliver(self.grid_slot(28), self.grid_slot(29, 7))
        second = asyncio.create_task(self.bot.craft_recipe_grid(12, 2, additional_destination_slots=(3,), timeout=0.25))
        request = await self.request_for(second)
        await self.deliver(self.response(request, result="ERROR"))
        self.assertEqual((await second).result.name, "ERROR")

    async def test_undecodable_workbench_verdict_releases_legacy_request(self):
        await self.ready()
        b = self.b
        await self.deliver(
            b.ContainerOpenPacket(container_id=7, type=b.ContainerType.WORKBENCH, entity_unique_id=-1),
            self.grid_slot(32, 7), self.grid_slot(33, 7),
        )
        first = asyncio.create_task(self.bot.return_crafting_ingredient(32, 1))
        await self.request()
        await self.deliver(RawPacket(b.ItemStackResponsePacket.ID, b""))
        with self.assertRaisesRegex(ValueError, "could not decode item-stack response"):
            await first
        second = asyncio.create_task(self.bot.return_crafting_ingredient(33, 1, timeout=0.25))
        request = await self.request_for(second)
        await self.deliver(self.response(request, result="ERROR"))
        self.assertEqual((await second).result.name, "ERROR")

    async def test_disjoint_slots_and_legacy_multi_craft_window_guard(self):
        await self.ready(self.grid_slot(30), self.observed_slot(28))
        craft = asyncio.create_task(self.edit("multi craft"))
        crafting = await self.request()
        staging = asyncio.create_task(self.edit("stage", 29))
        stage = await self.request()
        with self.assertRaisesRegex(RuntimeError, "crafting window or a result destination"):
            await self.bot.craft_recipe_grid(12, 0, additional_destination_slots=(28,))
        await self.deliver(self.response(stage, result="ERROR"), self.response(crafting, result="ERROR"))
        await asyncio.gather(craft, staging)

        clear = asyncio.create_task(self.clear())
        destroy = await self.request()
        staging = asyncio.create_task(self.edit("stage", 29))
        stage = await self.request()
        await self.deliver(self.response(stage, result="ERROR"), self.response(destroy, (GRID, 28, 0, None)))
        result = await clear
        await staging
        self.assertTrue(result.complete)
        self.assertTrue(self.transport.sent.empty())

    async def test_legacy_stage_on_disjoint_grid_slots_can_share_a_player_stack(self):
        await self.ready(self.grid_slot(30))
        async with self.hold_next_write() as (held, release):
            delayed = asyncio.create_task(self.edit("stage", 29))
            await asyncio.wait_for(held.wait(), 1)
            first = asyncio.create_task(self.edit("stage", 30))
            request = await self.request()
            await self.deliver(self.response(request, (PLAYER, 1, 2, 22), (GRID, 30, 1, 52)))
            self.assertEqual((await first).result.name, "SUCCESS")
            release.set()
            request = await self.request()
            await self.deliver(self.response(request, (PLAYER, 1, 1, 22), (GRID, 29, 1, 53)))
            self.assertEqual((await delayed).result.name, "SUCCESS")
        self.assertEqual(self.bot.inventory_slots[1].stack_size, 1)
        self.assertTrue(self.transport.sent.empty())

    async def test_cancellation_and_timeout_retain_ownership_until_verdict(self):
        for interrupt in ("cancel before write", "cancel after write", "timeout after write", "cancel with verdict"):
            with self.subTest(interrupt=interrupt):
                await self.ready(self.grid_slot(30, 9))
                task = asyncio.create_task(self.clear(timeout=0.15 if "timeout" in interrupt else 1))
                first = await self.request()
                if interrupt == "cancel before write":
                    async with self.hold_next_write() as (held, _):
                        await self.deliver(self.response(first, (GRID, 28, 0, None)))
                        await asyncio.wait_for(held.wait(), 1)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError) as caught:
                            await task
                    request = None
                else:
                    await self.deliver(self.response(first, (GRID, 28, 0, None)))
                    request = await self.request()
                    if interrupt == "cancel after write":
                        task.cancel()
                    if interrupt == "cancel with verdict":
                        unsubscribe = self.bot.packets.subscribe_raw(
                            lambda raw: task.cancel() if raw.packet_id == self.b.ItemStackResponsePacket.ID else None,
                        )
                        try:
                            await self.deliver(self.response(request, (GRID, 30, 0, None)))
                        finally:
                            unsubscribe()
                    with self.assertRaises(TimeoutError if "timeout" in interrupt else asyncio.CancelledError) as caught:
                        await task
                progress = caught.exception.creative_progress
                self.assertEqual([(row.slot, row.action, row.submitted) for row in progress.requests], [
                    (28, "destroy_grid", True), (30, "destroy_grid", request is not None),
                ])
                status = "cleared" if interrupt == "cancel with verdict" else "unconfirmed"
                self.assertEqual([row.status for row in progress.clear_result.crafting_grid],
                                 ["cleared", "already_empty", status, "pending"])
                if interrupt in ("cancel after write", "timeout after write"):
                    with self.assertRaisesRegex(RuntimeError, "pending creative destroy"):
                        await self.edit("return", 30)
                    original = self.bot.open_window
                    await self.deliver(self.close(), self.open(), self.grid_slot(30, 8))
                    self.assertEqual(self.bot.open_window, original)
                    self.assertIsNot(self.bot.open_window, original)
                    replacement = asyncio.create_task(self.clear())
                    second = await self.request()
                    await self.deliver(self.response(request, (GRID, 30, 0, None)))
                    self.assertEqual(self.bot._recipes._grid_slots[30].id, 8)
                    await self.deliver(self.response(second, result="ERROR"))
                    self.assertEqual((await replacement).crafting_grid[2].status, "rejected")
                self.assertTrue(self.transport.sent.empty())
                await self.bot.close()
