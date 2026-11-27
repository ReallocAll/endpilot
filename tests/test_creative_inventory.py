import asyncio
from dataclasses import replace
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.protocol import read_batch

from test_boss_bars import BatchTransport


_NBT = bytes.fromhex("ffff010a000003060044616d61676507000000000000000000000000")


class _CreativeTransport(BatchTransport):
    def __init__(self, protocol):
        super().__init__(protocol)
        self.sent = asyncio.Queue()
        self.block_send = None

    async def send(self, data):
        b = self.protocol.binding
        # Capture only after join, when every send is an uncompressed batch.
        if getattr(self, "capture", False):
            for raw in read_batch(data[1:]):
                if raw.packet_id == b.ItemStackRequestPacket.ID:
                    self.sent.put_nowait(self.protocol.decode(raw).requests[0])
                    if self.block_send is not None:
                        await self.block_send.wait()


class CreativeInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("Creative")
        self.b = self.bot.protocol.binding
        self.mode = self.b.GameType
        self.addAsyncCleanup(self.bot.close)

    def catalog(self, item_id=7):
        b = self.b
        item = b.SerializedNetworkItemInstanceDescriptor(
            id=item_id, stack_size=1, aux_value=7, block_runtime_id=-2147483647,
            user_data_buffer=_NBT,
        )
        return b.CreativeContentPacket(
            groups=[b.CreativeGroupInfoPayload(
                name="minecraft:creative.other", creative_item_category=b.CreativeItemCategory.ITEMS, icon=item,
            )],
            entries=[b.CreativeItemEntryPayload(creative_item_net_id=73, group_index=0, item_descriptor=item)],
        )

    def registry(self, item_id, name):
        return self.b.ItemRegistryPacket(items=[self.b.ItemData(id=item_id, name=name)])

    async def join(self, mode, *early):
        b = self.b
        self.transport = _CreativeTransport(self.bot.protocol)
        self.transport.deliver(
            b.NetworkSettingsPacket(
                compression_algorithm=b.PacketCompressionAlgorithm.ZLIB, compression_threshold=0,
            ), compressed_connection=False,
        )
        self.transport.deliver(
            b.StartGamePacket(runtime_id=42, entity_id=7, entity_game_type=mode,
                              settings=b.LevelSettings(game_type=self.mode.SURVIVAL)),
            *early,
            b.PlayStatusPacket(status=3),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)
        self.transport.capture = True

    async def deliver(self, *packets):
        barrier = self.b.SetTimePacket(time=37)
        pending = asyncio.create_task(self.bot.packets.wait_for_packet(barrier.ID, timeout=1))
        await asyncio.sleep(0)
        self.transport.deliver(*packets, barrier)
        await pending

    def open(self):
        b = self.b
        return b.ContainerOpenPacket(container_id=4, type=b.ContainerType.INVENTORY, entity_unique_id=7)

    def close(self):
        b = self.b
        return b.ContainerClosePacket(container_id=4, container_type=b.ContainerType.INVENTORY)

    def observed_slot(self, slot, item_id=0):
        b = self.b
        item = b.SerializedNetworkItemStackDescriptor(
            id=item_id, stack_size=3 if item_id else 0, net_id_variant=22 if item_id else None,
            user_data_buffer=_NBT if item_id else b"",
        )
        return b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=slot, item=item)

    async def request(self):
        async with asyncio.timeout(1):
            return await self.transport.sent.get()

    def reply(self, request, amount, *, slot=3, result="SUCCESS"):
        b = self.b
        return b.ItemStackResponsePacket(responses=[
            b.ItemStackResponseInfo(
                client_request_id=request.client_request_id, result=b.ItemStackNetResult[result],
                containers=None if result != "SUCCESS" else [b.ItemStackResponseContainerInfo(
                    full_container_name=b.FullContainerName(name=b.ContainerEnumName.HOTBAR_CONTAINER),
                    slots=[b.ItemStackResponseSlotInfo(
                        requested_slot=slot, slot=slot, amount=amount,
                        item_stack_net_id=b.ItemStackNetId(raw_id=29) if amount else None,
                    )],
                )],
            ),
        ])

    async def test_catalog_and_mode_preserved_across_spawn_and_reset_on_rejoin(self):
        b, mode = self.b, self.mode
        await self.join(
            mode.DEFAULT, self.catalog(), self.registry(7, "minecraft:fixture"),
            b.SetDefaultGameTypePacket(default_game_type=mode.CREATIVE),
        )
        catalog = self.bot.creative_catalog
        self.assertEqual(self.bot.game_mode, mode.CREATIVE)
        group, entry = catalog.groups[0], catalog.entries[0]
        self.assertEqual((group.name, group.category, entry.group_index, entry.net_id),
                         ("minecraft:creative.other", "ITEMS", 0, 73))
        self.assertEqual(entry.item, group.icon)
        self.assertEqual((entry.item.name, entry.item.aux_value, entry.item.block_runtime_id),
                         ("minecraft:fixture", 7, -2147483647))
        nbt = entry.item.nbt
        self.assertEqual(nbt, {"Damage": 7})
        nbt["Damage"] = 19
        self.assertEqual(entry.item.nbt, {"Damage": 7})

        await self.deliver(b.UpdatePlayerGameTypePacket(player_game_type=mode.SPECTATOR, target_player=42))
        self.assertEqual(self.bot.game_mode, mode.CREATIVE)
        await self.deliver(b.UpdatePlayerGameTypePacket(player_game_type=mode.ADVENTURE, target_player=7),
                           b.SetDefaultGameTypePacket(default_game_type=mode.SURVIVAL))
        self.assertEqual(self.bot.game_mode, mode.ADVENTURE)
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=mode.DEFAULT))
        self.assertEqual(self.bot.game_mode, mode.SURVIVAL)
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=mode.UNDEFINED))
        self.assertIsNone(self.bot.game_mode)

        await self.bot.close()
        self.assertIsNone(self.bot.creative_catalog)
        self.assertIsNone(self.bot.game_mode)
        await self.join(mode.CREATIVE, self.catalog(), self.registry(7, "minecraft:replacement"))
        await self.deliver(self.open())
        with self.assertRaisesRegex(ValueError, "current creative catalog"):
            await self.bot.take_creative_item(entry, 3)
        self.assertEqual(catalog.entries[0].item.name, "minecraft:fixture")
        self.assertEqual(self.bot.creative_catalog.entries[0].item.name, "minecraft:replacement")
        self.assertTrue(self.transport.sent.empty())

    async def test_game_mode_subscription_reports_only_effective_local_changes(self):
        b, mode = self.b, self.mode
        await self.join(mode.CREATIVE, b.SetPlayerGameTypePacket(player_game_type=mode.SURVIVAL))
        self.assertEqual(self.bot.game_mode, mode.SURVIVAL)
        modes = []
        sent = []
        self.addCleanup(self.bot.packets.subscribe_sent(sent.append))
        self.bot.subscribe_game_mode(lambda value: modes.append((value, self.bot.game_mode)))
        self.assertEqual(modes, [])

        await self.deliver(
            b.UpdatePlayerGameTypePacket(target_player=42, player_game_type=mode.CREATIVE),
            b.SetDefaultGameTypePacket(default_game_type=mode.CREATIVE),
            b.SetPlayerGameTypePacket(player_game_type=mode.SURVIVAL),
            b.SetPlayerGameTypePacket(player_game_type=mode.DEFAULT),
            b.UpdatePlayerGameTypePacket(target_player=7, player_game_type=mode.CREATIVE),
            b.SetPlayerGameTypePacket(player_game_type=mode.DEFAULT),
            b.SetDefaultGameTypePacket(default_game_type=mode.UNDEFINED),
            b.SetDefaultGameTypePacket(default_game_type=mode.DEFAULT),
            b.SetDefaultGameTypePacket(default_game_type=mode.ADVENTURE),
            b.UpdatePlayerGameTypePacket(target_player=7, player_game_type=mode.SURVIVAL),
        )
        expected = [mode.CREATIVE, None, mode.ADVENTURE, mode.SURVIVAL]
        self.assertEqual(modes, [(value, value) for value in expected])
        self.assertEqual(sent, [])

        await self.deliver(b.ChangeDimensionPacket(dimension_id=1),
                           b.SetPlayerGameTypePacket(player_game_type=mode.SURVIVAL))
        self.assertEqual(self.bot.game_mode, mode.SURVIVAL)
        self.assertEqual(modes, [(value, value) for value in expected])

        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=mode.UNDEFINED))
        self.assertIsNone(self.bot.game_mode)
        self.assertEqual(modes[-1], (None, None))
        self.assertEqual(len(modes), len(expected) + 1)

    async def test_game_mode_subscriptions_are_independent_and_isolate_callback_errors(self):
        b, mode = self.b, self.mode
        await self.join(mode.SURVIVAL)
        loop = asyncio.get_running_loop()
        errors = []
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: errors.append(context["exception"]))
        failure = RuntimeError("listener failed")

        def fail(_):
            raise failure

        remove_failure = self.bot.subscribe_game_mode(fail)
        modes = []
        callback = modes.append
        remove_first = self.bot.subscribe_game_mode(callback)
        remove_second = self.bot.subscribe_game_mode(callback)
        remove_first()
        remove_first()
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=mode.CREATIVE))
        self.assertEqual(errors, [failure])
        self.assertEqual(modes, [mode.CREATIVE])
        remove_failure()
        remove_second()
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=mode.SURVIVAL))
        self.assertEqual(errors, [failure])
        self.assertEqual(modes, [mode.CREATIVE])

    async def test_game_mode_subscription_ends_with_its_session(self):
        b, mode = self.b, self.mode
        old_modes = []
        with self.assertRaises(RuntimeError):
            self.bot.subscribe_game_mode(old_modes.append)
        await self.join(mode.SURVIVAL)
        remove_old = self.bot.subscribe_game_mode(old_modes.append)
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=mode.CREATIVE), b.DisconnectPacket())
        self.assertEqual((await self.bot.wait_for_session_end()).kind, "server_disconnect")
        self.assertEqual(old_modes, [mode.CREATIVE])
        self.assertIsNone(self.bot.game_mode)
        with self.assertRaises(ConnectionError):
            self.bot.subscribe_game_mode(old_modes.append)
        await self.bot.close()

        await self.join(mode.ADVENTURE)
        new_modes = []
        self.bot.subscribe_game_mode(new_modes.append)
        remove_old()
        remove_old()
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=mode.SURVIVAL))
        await self.bot.close()
        self.assertEqual(new_modes, [mode.SURVIVAL])
        self.assertEqual(old_modes, [mode.CREATIVE])

    async def test_take_and_partial_destroy_apply_only_native_responses(self):
        await self.join(self.mode.CREATIVE, self.catalog(), self.registry(7, "minecraft:fixture"))
        await self.deliver(self.open(), self.observed_slot(3))
        b = self.b
        entry = self.bot.creative_catalog.entries[0]
        pending = asyncio.create_task(self.bot.take_creative_item(entry, 3, 64, timeout=1))
        request = await self.request()
        craft, place = request.actions
        self.assertEqual((craft.creative_item_net_id, place.amount, place.source.net_id_variant),
                         (73, 64, request.client_request_id.raw_id))
        self.assertEqual(place.source.slot, 50)
        self.assertEqual(self.bot.inventory_slots[3].id, 0)
        await self.deliver(self.reply(request, 64))
        self.assertEqual((await pending).result.name, "SUCCESS")
        issued = self.bot.inventory_slots[3]
        self.assertEqual((issued.id, issued.stack_size, issued.aux_value, issued.net_id_variant,
                          issued.block_runtime_id, issued.user_data_buffer),
                         (7, 64, 7, 29, 0x80000001, _NBT))

        destroying = asyncio.create_task(self.bot.destroy_creative_item(3, 63, timeout=1))
        request = await self.request()
        self.assertFalse(destroying.done())
        self.assertEqual(request.client_request_id.raw_id, -3)
        action = request.actions[0]
        self.assertIsInstance(action, b.ItemStackRequestCereal.DestroyActionData)
        self.assertEqual((action.source.slot, action.source.net_id_variant, action.amount), (3, 29, 63))
        self.assertEqual(self.bot.inventory_slots[3].stack_size, 64)
        await self.deliver(self.reply(request, 1))
        self.assertEqual((await destroying).result.name, "SUCCESS")
        remaining = self.bot.inventory_slots[3]
        self.assertEqual((remaining.id, remaining.stack_size, remaining.user_data_buffer), (7, 1, _NBT))

        pending = asyncio.create_task(self.bot.clear_creative_slot(3, timeout=1))
        request = await self.request()
        await self.deliver(self.reply(request, 0))
        self.assertEqual((await pending).result.name, "SUCCESS")
        self.assertEqual(self.bot.inventory_slots[3].id, 0)
        self.assertIsNone(await self.bot.clear_creative_slot(3))

    async def test_same_session_catalog_refresh_closed_window_and_survival_are_local_rejections(self):
        b = self.b
        await self.join(self.mode.CREATIVE, self.catalog())
        await self.deliver(self.open())
        old = self.bot.creative_catalog.entries[0]
        await self.deliver(self.catalog(8), self.close())
        entry = self.bot.creative_catalog.entries[0]
        with self.assertRaisesRegex(RuntimeError, "open the player inventory screen"):
            await self.bot.fill_creative_slot(3, entry, 8)
        await self.deliver(self.open())
        with self.assertRaisesRegex(ValueError, "current creative catalog"):
            await self.bot.take_creative_item(old, 3)
        for amount in (0, 256, True):
            with self.assertRaisesRegex(ValueError, "amount"):
                await self.bot.fill_creative_slot(3, entry, amount)
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=self.mode.SURVIVAL))
        for action in (self.bot.take_creative_item(entry, 3), self.bot.destroy_creative_item(3, 1),
                       self.bot.fill_creative_slot(3, entry), self.bot.clear_creative_slot(3)):
            with self.assertRaises(PermissionError):
                await action
        self.assertTrue(self.transport.sent.empty())

    async def test_queue_rechecks_mode_before_sending(self):
        b = self.b
        await self.join(self.mode.CREATIVE, self.catalog())
        await self.deliver(self.open(), self.observed_slot(3))
        # Wake the receive task first, then queue the action in the current task.
        self.transport.deliver(b.SetPlayerGameTypePacket(player_game_type=self.mode.SURVIVAL))
        entry, = self.bot.creative_catalog.entries
        request = await self.bot._creative.take(entry, 3, 3, timeout=1, queued=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with self.assertRaises(PermissionError):
            self.bot._inventory._qa_response(request)
        self.assertTrue(self.transport.sent.empty())

    async def test_fill_keeps_observed_stack_when_server_rejects_destroy(self):
        await self.join(self.mode.CREATIVE, self.catalog(8))
        b = self.b
        observed = b.SerializedNetworkItemStackDescriptor(
            id=7, stack_size=3, aux_value=0, net_id_variant=22, block_runtime_id=0,
            user_data_buffer=_NBT,
        )
        await self.deliver(
            self.open(), b.InventorySlotPacket(inventory_id=b.ContainerID.INVENTORY, slot=3, item=observed),
        )
        entry = self.bot.creative_catalog.entries[0]
        pending = asyncio.create_task(self.bot.fill_creative_slot(3, entry, 2, timeout=1))
        request = await self.request()
        self.assertIsInstance(request.actions[0], b.ItemStackRequestCereal.DestroyActionData)
        await self.deliver(self.reply(request, 0, result="ERROR"))
        self.assertEqual([reply.result.name for reply in await pending], ["ERROR"])
        self.assertTrue(self.transport.sent.empty())
        item = self.bot.inventory_slots[3]
        self.assertEqual((item.id, item.stack_size, item.net_id_variant, item.user_data_buffer),
                         (7, 3, 22, _NBT))

    async def test_unknown_slot_is_visible_and_creative_actions_do_not_infer_air(self):
        await self.join(self.mode.CREATIVE, self.catalog())
        await self.deliver(self.open(), self.observed_slot(4), self.observed_slot(5, 1))
        entry = self.bot.creative_catalog.entries[0]
        for action in (self.bot.take_creative_item(entry, 3), self.bot.fill_creative_slot(3, entry),
                       self.bot.clear_creative_slot(3)):
            with self.assertRaisesRegex(RuntimeError, "slot 3 has not been observed"):
                await action
        with self.assertRaisesRegex(RuntimeError, "slot 3 has not been observed"):
            await self.bot._creative.take(entry, 3, 1, timeout=1, queued=True)
        self.assertIsNone(await self.bot.clear_creative_slot(4))
        self.assertTrue(self.transport.sent.empty())

        pending = asyncio.create_task(self.bot.clear_creative_inventory([4, 5, 3, 4], timeout=1))
        request = await self.request()
        await self.deliver(self.reply(request, 0, slot=5))
        result = await pending
        self.assertFalse(result.complete)
        self.assertEqual([(item.slot, item.status) for item in result.slots],
                         [(4, "already_empty"), (5, "cleared"), (3, "unknown")])
        self.assertEqual(result.slots[1].verdict.result.name, "SUCCESS")
        self.assertNotIn(3, self.bot.inventory_slots)
        self.assertTrue(self.transport.sent.empty())

    async def test_fill_interruption_carries_destroy_verdict_and_take_submission(self):
        b = self.b
        for interruption, expected_submission in (("timeout", True), ("blocked_send", None),
                                                  ("disconnect", True), ("before_take", False)):
            with self.subTest(interruption=interruption):
                await self.join(self.mode.CREATIVE, self.catalog(8))
                await self.deliver(self.open(), self.observed_slot(3, 7))
                entry = self.bot.creative_catalog.entries[0]
                timeout = 0.2 if interruption in ("timeout", "blocked_send") else 1
                pending = asyncio.create_task(self.bot.fill_creative_slot(3, entry, 2, timeout=timeout))
                destroy = await self.request()
                if interruption == "before_take":
                    await self.deliver(self.reply(destroy, 0), b.DisconnectPacket())
                else:
                    if interruption == "blocked_send":
                        self.transport.block_send = asyncio.Event()
                    await self.deliver(self.reply(destroy, 0))
                    take = await self.request()
                    self.assertIsInstance(take.actions[0], b.ItemStackRequestCereal.CraftCreativeActionData)
                    if interruption == "disconnect":
                        await self.deliver(b.DisconnectPacket())

                error_type = TimeoutError if interruption in ("timeout", "blocked_send") else ConnectionError
                with self.assertRaises(error_type) as caught:
                    await pending
                steps = caught.exception.creative_progress.requests
                self.assertEqual((steps[0].action, steps[0].request_id, steps[0].submitted,
                                  steps[0].verdict.result.name), ("destroy", -1, True, "SUCCESS"))
                if interruption == "before_take":
                    self.assertEqual(len(steps), 1)
                    self.assertTrue(self.transport.sent.empty())
                else:
                    self.assertEqual([(step.action, step.request_id, step.submitted, step.verdict)
                                      for step in steps[1:]], [("take", -3, expected_submission, None)])
                self.assertEqual(self.bot.inventory_slots[3].id, 0)
                await self.bot.close()

    async def test_bulk_clear_preserves_per_slot_progress_on_disconnect(self):
        b = self.b
        await self.join(self.mode.CREATIVE)
        await self.deliver(
            self.open(), self.observed_slot(3, 7), self.observed_slot(4), self.observed_slot(5, 8),
        )
        pending = asyncio.create_task(self.bot.clear_creative_inventory([4, 3, 5, 1], timeout=1))
        destroy = await self.request()
        await self.deliver(self.reply(destroy, 0))
        second = await self.request()
        self.assertEqual(second.actions[0].source.slot, 5)
        await self.deliver(b.DisconnectPacket())
        with self.assertRaises(ConnectionError) as caught:
            await pending
        progress = caught.exception.creative_progress
        self.assertFalse(progress.clear_result.complete)
        self.assertEqual([(item.slot, item.status) for item in progress.clear_result.slots],
                         [(4, "already_empty"), (3, "cleared"), (5, "pending"), (1, "pending")])
        self.assertIs(progress.clear_result.slots[1].verdict, progress.requests[0].verdict)
        self.assertEqual([(r.slot, r.submitted) for r in progress.requests], [(3, True), (5, True)])

    async def test_catalog_entry_fill_checks_authorization_and_returns_native_verdict(self):
        await self.join(self.mode.CREATIVE, self.catalog(), self.registry(7, "minecraft:fixture"))
        await self.deliver(self.open(), self.observed_slot(3))
        self.assertEqual(self.bot.game_mode, self.mode.CREATIVE)
        entry, = self.bot.creative_catalog.entries
        self.assertEqual((entry.net_id, entry.item.name, entry.item.aux_value, entry.item.user_data_buffer),
                         (73, "minecraft:fixture", 7, _NBT))
        with self.assertRaisesRegex(ValueError, "current creative catalog"):
            await self.bot.fill_creative_slot(3, replace(entry, net_id=7), 2)
        self.assertTrue(self.transport.sent.empty())

        fill = asyncio.create_task(self.bot.fill_creative_slot(3, entry, 2))
        request = await self.request()
        self.assertEqual(request.actions[0].creative_item_net_id, 73)
        await self.deliver(self.reply(request, 2))
        self.assertEqual((await fill)[0].containers[0].slots[0].amount, 2)

        await self.deliver(self.b.SetPlayerGameTypePacket(player_game_type=self.mode.SURVIVAL))
        self.assertEqual(self.bot.game_mode, self.mode.SURVIVAL)
        for action in (self.bot.fill_creative_slot(3, entry), self.bot.clear_creative_slot(3),
                       self.bot.clear_creative_inventory([3])):
            with self.assertRaises(PermissionError):
                await action
        self.assertTrue(self.transport.sent.empty())
        self.assertEqual(self.bot.inventory_slots[3].stack_size, 2)
