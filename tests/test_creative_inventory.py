import asyncio
from dataclasses import replace
import unittest
from unittest.mock import patch

from endpilot import Bot, CreativeArmorClearResult, CreativeClearResult
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

    def catalog(self, item_id=7, *, data=_NBT):
        b = self.b
        item = b.SerializedNetworkItemInstanceDescriptor(
            id=item_id, stack_size=1, aux_value=7, block_runtime_id=-2147483647,
            user_data_buffer=data,
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

    def item_slot(self, domain, slot, net_id, *, data=_NBT):
        b = self.b
        item = b.SerializedNetworkItemStackDescriptor(
            id=7 if net_id else 0, stack_size=1 if net_id else 0, net_id_variant=net_id,
            aux_value=7 if net_id else 0, block_runtime_id=0x80000001 if net_id else 0,
            user_data_buffer=data if net_id else b"",
        )
        return b.InventorySlotPacket(inventory_id=b.ContainerID[domain], slot=slot, item=item)

    def transfer_reply(self, request, *, slot=0, armor=0, net_id=51, amount=1,
                       domain="COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", dynamic_id=None, source_empty=True):
        b = self.b

        def correction(name, index, count, identity, dynamic=None):
            return b.ItemStackResponseContainerInfo(
                full_container_name=b.FullContainerName(name=b.ContainerEnumName[name], dynamic_id=dynamic),
                slots=[b.ItemStackResponseSlotInfo(
                    requested_slot=index, slot=index, amount=count,
                    item_stack_net_id=None if identity is None else b.ItemStackNetId(raw_id=identity),
                )],
            )

        containers = [correction(domain, slot, amount, net_id, dynamic_id)]
        if source_empty:
            containers.append(correction("ARMOR_CONTAINER", armor, 0, None))
        return b.ItemStackResponsePacket(responses=[b.ItemStackResponseInfo(
            client_request_id=request.client_request_id, result=b.ItemStackNetResult.SUCCESS, containers=containers,
        )])

    async def armor_ready(self, *extra):
        await self.join(self.mode.CREATIVE)
        await self.deliver(
            self.open(), self.item_slot("INVENTORY", 0, None), self.item_slot("ARMOR", 0, 25),
            self.item_slot("ARMOR", 1, 26), self.item_slot("ARMOR", 2, None),
            self.item_slot("ARMOR", 3, None), self.item_slot("OFFHAND", 0, 99), *extra,
        )

    def check_armor_action(self, request, kind, domain, slot, net_id, amount=1):
        action, = request.actions
        self.assertIsInstance(action, getattr(self.b.ItemStackRequestCereal, kind))
        self.assertEqual((action.source.full_container_name.name.name, action.source.slot,
                          action.source.net_id_variant, action.amount), (domain, slot, net_id, amount))
        return action

    async def test_bulk_option_keeps_default_shape_and_orders_main_then_armor(self):
        await self.armor_ready(self.observed_slot(8, 7))
        legacy = await self.bot.clear_creative_inventory([0, 0], include_armor=False)
        self.assertIs(type(legacy), CreativeClearResult)
        self.assertEqual([(row.slot, row.status) for row in legacy.slots], [(0, "already_empty")])
        self.assertTrue(self.transport.sent.empty())

        pending = asyncio.create_task(self.bot.clear_creative_inventory([8, 8], include_armor=True, timeout=1))
        main = await self.request()
        self.check_armor_action(main, "DestroyActionData", "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 8, 22, 3)
        await self.deliver(self.reply(main, 0, slot=8))
        for index, identity in enumerate((51, 52)):
            take = await self.request()
            action = self.check_armor_action(take, "TakeActionData", "ARMOR_CONTAINER", index, 25 + index)
            self.assertEqual((action.destination.full_container_name.name.name, action.destination.slot,
                              action.destination.net_id_variant), ("COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 0, 0))
            await self.deliver(self.transfer_reply(take, armor=index, net_id=identity))
            destroy = await self.request()
            self.check_armor_action(destroy, "DestroyActionData", "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 0, identity)
            if index == 0:
                # An independent empty-slot observation can precede the verdict.
                await self.deliver(self.item_slot("INVENTORY", 0, None), self.reply(destroy, 0, slot=0))
            else:
                await self.deliver(self.reply(destroy, 0, slot=0))

        result = await pending
        self.assertIs(type(result), CreativeArmorClearResult)
        self.assertEqual([(row.slot, row.status) for row in result.slots], [(8, "cleared")])
        self.assertEqual([(row.slot, row.status, row.staging_slot, row.staged) for row in result.armor], [
            ("head", "cleared", 0, None), ("chest", "cleared", 0, None),
            ("legs", "already_empty", None, None), ("feet", "already_empty", None, None),
        ])
        self.assertTrue(result.complete)
        self.assertEqual([(row.unequip.request_id, row.destroy.request_id) for row in result.armor[:2]],
                         [(-3, -5), (-7, -9)])
        self.assertEqual(self.bot.offhand_item.net_id_variant, 99)
        self.assertTrue(self.transport.sent.empty())

    async def test_armor_only_distinguishes_known_empty_and_unknown(self):
        await self.join(self.mode.CREATIVE)
        await self.deliver(self.open(), self.item_slot("ARMOR", 1, None), self.item_slot("OFFHAND", 0, 99))
        result = await self.bot.clear_creative_inventory([], include_armor=True)
        self.assertEqual(result.slots, ())
        self.assertEqual([row.status for row in result.armor], ["unknown", "already_empty", "unknown", "unknown"])
        self.assertFalse(result.complete)
        self.assertEqual(self.bot.offhand_item.net_id_variant, 99)
        self.assertTrue(self.transport.sent.empty())

    async def test_armor_without_empty_staging_slot_preserves_original_runtime_error(self):
        for count, message in ((35, "no empty main inventory slot is confirmed"), (36, "the main inventory is full")):
            with self.subTest(count=count):
                await self.join(self.mode.CREATIVE)
                await self.deliver(self.open(), self.item_slot("ARMOR", 0, 25),
                                   *(self.item_slot("INVENTORY", index, 50 + index) for index in range(count)))
                with self.assertRaisesRegex(RuntimeError, message) as caught:
                    await self.bot.clear_creative_inventory([], include_armor=True)
                self.assertEqual(caught.exception.creative_progress.requests, ())
                self.assertEqual([row.status for row in caught.exception.creative_progress.clear_result.armor],
                                 ["pending"] * 4)
                self.assertTrue(self.transport.sent.empty())
                await self.bot.close()

    async def test_armor_rejections_stop_and_report_the_staged_piece(self):
        for reject in ("take", "destroy"):
            with self.subTest(reject=reject):
                await self.armor_ready()
                pending = asyncio.create_task(self.bot.clear_creative_inventory([], include_armor=True, timeout=1))
                request = await self.request()
                if reject == "destroy":
                    await self.deliver(self.transfer_reply(request))
                    request = await self.request()
                await self.deliver(self.reply(request, 0, slot=0, result="ERROR"))
                result = await pending
                first = result.armor[0]
                self.assertEqual([row.status for row in result.armor], ["rejected", "pending", "pending", "pending"])
                if reject == "take":
                    self.assertEqual((first.unequip.verdict.result.name, first.destroy, first.staged), ("ERROR", None, None))
                    self.assertEqual(self.bot.armor_slots[0].net_id_variant, 25)
                else:
                    self.assertEqual((first.unequip.verdict.result.name, first.destroy.verdict.result.name, first.staged),
                                     ("SUCCESS", "ERROR", "confirmed"))
                    self.assertEqual(self.bot.inventory_slots[0].net_id_variant, 51)
                self.assertTrue(self.transport.sent.empty())
                await self.bot.close()

    async def test_armor_transfer_requires_explicit_receipt_and_matching_observations(self):
        for label, receipt, replacement in (
            ("missing identity", {"net_id": None}, None),
            ("zero identity with live fallback", {"net_id": 0}, (25, _NBT)),
            ("wrong domain", {"domain": "OFFHAND_CONTAINER"}, None),
            ("dynamic container", {"dynamic_id": 7}, None),
            ("wrong slot", {"slot": 1}, None),
            ("wrong amount", {"amount": 2}, None),
            ("source not empty", {"source_empty": False}, None),
            ("same item with a different identity", {}, (52, _NBT)),
            ("different item data with the receipt identity", {}, (51, b"")),
        ):
            with self.subTest(label=label):
                await self.armor_ready()
                pending = asyncio.create_task(self.bot.clear_creative_inventory([], include_armor=True, timeout=1))
                request = await self.request()
                packets = [self.transfer_reply(request, **receipt)]
                if replacement is not None:
                    identity, data = replacement
                    packets.append(self.item_slot("INVENTORY", 0, identity, data=data))
                await self.deliver(*packets)
                result = await pending
                first = result.armor[0]
                self.assertEqual([row.status for row in result.armor], ["unconfirmed", "pending", "pending", "pending"])
                self.assertEqual((first.unequip.verdict.result.name, first.destroy), ("SUCCESS", None))
                self.assertTrue(self.transport.sent.empty())
                await self.bot.close()

    async def test_armor_destroy_requires_an_observed_empty_slot(self):
        for receipt in ({"slot": 1}, {"dynamic_id": 7}):
            with self.subTest(receipt=receipt):
                await self.armor_ready()
                pending = asyncio.create_task(self.bot.clear_creative_inventory([], include_armor=True, timeout=1))
                await self.deliver(self.transfer_reply(await self.request()))
                destroy = await self.request()
                await self.deliver(self.transfer_reply(destroy, amount=0, net_id=None, source_empty=False, **receipt))
                result = await pending
                first = result.armor[0]
                self.assertEqual([row.status for row in result.armor],
                                 ["unconfirmed", "pending", "pending", "pending"])
                self.assertEqual((first.destroy.verdict.result.name, first.staged), ("SUCCESS", "unknown"))
                self.assertTrue(self.transport.sent.empty())
                await self.bot.close()

    async def test_armor_checks_current_window_mode_and_stacks_at_both_writes(self):
        b = self.b
        for stage, change, error_type, message in (
            ("take", "window", RuntimeError, "window changed"),
            ("take", "mode", PermissionError, "granted creative mode"),
            ("take", "slot", RuntimeError, "transfer slot changed"),
            ("take", "source", RuntimeError, "transfer slot changed"),
            ("destroy", "window", RuntimeError, "window changed"),
            ("destroy", "mode", PermissionError, "granted creative mode"),
            ("destroy", "slot", RuntimeError, "transfer changed before destruction"),
            ("destroy", "source", RuntimeError, "transfer changed before destruction"),
        ):
            with self.subTest(stage=stage, change=change):
                await self.armor_ready()
                send = self.bot.packets.send
                at_send, release = asyncio.Event(), asyncio.Event()
                delay_type = (b.ItemStackRequestCereal.TakeActionData if stage == "take"
                              else b.ItemStackRequestCereal.DestroyActionData)

                async def delay(packet, **kwargs):
                    if isinstance(packet, b.ItemStackRequestPacket) and isinstance(packet.requests[0].actions[0], delay_type):
                        at_send.set()
                        await release.wait()
                    await send(packet, **kwargs)

                with patch.object(self.bot.packets, "send", delay):
                    pending = asyncio.create_task(self.bot.clear_creative_inventory([], include_armor=True, timeout=1))
                    if stage == "destroy":
                        await self.deliver(self.transfer_reply(await self.request()))
                    await asyncio.wait_for(at_send.wait(), 1)
                    if change == "window":
                        await self.deliver(self.close(), self.open())
                    elif change == "mode":
                        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=self.mode.SURVIVAL))
                    elif change == "source":
                        await self.deliver(self.item_slot("ARMOR", 0, 52))
                    else:
                        await self.deliver(self.item_slot("INVENTORY", 0, 52))
                    release.set()
                    with self.assertRaisesRegex(error_type, message) as caught:
                        await pending
                progress = caught.exception.creative_progress
                first = progress.clear_result.armor[0]
                self.assertEqual([row.status for row in progress.clear_result.armor],
                                 ["unconfirmed", "pending", "pending", "pending"])
                self.assertFalse(progress.requests[-1].submitted)
                if stage == "destroy":
                    self.assertEqual(first.unequip.verdict.result.name, "SUCCESS")
                    self.assertIs(first.destroy, progress.requests[-1])
                    self.assertEqual(first.staged, "unknown" if change == "slot" else "confirmed")
                else:
                    self.assertIsNone(first.destroy)
                self.assertTrue(self.transport.sent.empty())
                await self.bot.close()

    async def test_cancel_before_second_armor_destroy_keeps_the_confirmed_prefix(self):
        await self.armor_ready()
        b = self.b
        send = self.bot.packets.send
        blocked = asyncio.Event()

        async def delay(packet, **kwargs):
            if (isinstance(packet, b.ItemStackRequestPacket)
                    and packet.requests[0].client_request_id.raw_id == -7):
                blocked.set()
                await asyncio.Future()
            await send(packet, **kwargs)

        with patch.object(self.bot.packets, "send", delay):
            pending = asyncio.create_task(self.bot.clear_creative_inventory([0], include_armor=True, timeout=1))
            await self.deliver(self.transfer_reply(await self.request()))
            await self.deliver(self.reply(await self.request(), 0, slot=0))
            chest = await self.request()
            await self.deliver(self.transfer_reply(chest, armor=1, net_id=52))
            await asyncio.wait_for(blocked.wait(), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await pending
        progress = caught.exception.creative_progress
        self.assertEqual([(row.slot, row.status) for row in progress.clear_result.slots], [(0, "unconfirmed")])
        self.assertEqual([(row.status, row.staged) for row in progress.clear_result.armor],
                         [("cleared", None), ("unconfirmed", "confirmed"), ("pending", None), ("pending", None)])
        self.assertEqual([(row.request_id, row.submitted) for row in progress.requests],
                         [(-1, True), (-3, True), (-5, True), (-7, False)])
        self.assertEqual(self.bot.inventory_slots[0].net_id_variant, 52)
        self.assertTrue(self.transport.sent.empty())

    async def test_armor_destroy_verdict_does_not_erase_a_replacement(self):
        for observed_ids in ((52,), (52, 51)):
            with self.subTest(observed_ids=observed_ids):
                await self.armor_ready()
                pending = asyncio.create_task(self.bot.clear_creative_inventory([], include_armor=True, timeout=1))
                await self.deliver(self.transfer_reply(await self.request()))
                destroy = await self.request()
                self.check_armor_action(destroy, "DestroyActionData", "COMBINED_HOTBAR_AND_INVENTORY_CONTAINER", 0, 51)
                await self.deliver(*(self.item_slot("INVENTORY", 0, identity) for identity in observed_ids),
                                   self.reply(destroy, 0, slot=0))
                result = await pending
                self.assertFalse(result.complete)
                self.assertEqual([row.status for row in result.armor], ["unconfirmed", "pending", "pending", "pending"])
                self.assertEqual(self.bot.inventory_slots[0].net_id_variant, observed_ids[-1])
                verdict = result.armor[0].destroy.verdict
                self.assertEqual(verdict.result.name, "SUCCESS")
                self.assertEqual(verdict.client_request_id.raw_id, destroy.client_request_id.raw_id)
                self.assertTrue(self.transport.sent.empty())
                await self.bot.close()

    async def test_armor_interruption_retains_submissions_and_already_received_verdicts(self):
        b = self.b
        for stage, interruption, expected_submitted, expected_verdict, error_type in (
            ("take", "disconnect", True, None, ConnectionError),
            ("take", "timeout", True, None, TimeoutError),
            ("take", "cancel", True, None, asyncio.CancelledError),
            ("take", "blocked write", None, None, asyncio.CancelledError),
            ("take", "cancel with verdict", True, "SUCCESS", asyncio.CancelledError),
            ("destroy", "disconnect", True, None, ConnectionError),
            ("destroy", "timeout", True, None, TimeoutError),
            ("destroy", "cancel", True, None, asyncio.CancelledError),
            ("destroy", "blocked write", None, None, asyncio.CancelledError),
            ("destroy", "cancel with verdict", True, "SUCCESS", asyncio.CancelledError),
        ):
            with self.subTest(stage=stage, interruption=interruption):
                await self.armor_ready(self.observed_slot(8, 7))
                timeout = 0.15 if interruption == "timeout" else 1
                pending = asyncio.create_task(self.bot.clear_creative_inventory([8], include_armor=True, timeout=timeout))
                main = await self.request()
                if stage == "take" and interruption == "blocked write":
                    self.transport.block_send = asyncio.Event()
                await self.deliver(self.reply(main, 0, slot=8))
                take = await self.request()
                if stage == "destroy":
                    if interruption == "blocked write":
                        self.transport.block_send = asyncio.Event()
                    await self.deliver(self.transfer_reply(take))
                    request = await self.request()
                else:
                    request = take

                if interruption == "disconnect":
                    await self.deliver(b.DisconnectPacket())
                elif interruption in ("cancel", "blocked write"):
                    pending.cancel()
                elif interruption == "cancel with verdict":
                    unsubscribe = self.bot.packets.subscribe_raw(
                        lambda raw: pending.cancel() if raw.packet_id == b.ItemStackResponsePacket.ID else None
                    )
                    try:
                        await self.deliver(self.transfer_reply(request) if stage == "take" else self.reply(request, 0, slot=0))
                    finally:
                        unsubscribe()
                with self.assertRaises(error_type) as caught:
                    await pending
                progress = caught.exception.creative_progress
                result = progress.clear_result
                self.assertEqual([(row.slot, row.status) for row in result.slots], [(8, "cleared")])
                row = result.armor[0]
                step = row.unequip if stage == "take" else row.destroy
                self.assertIs(step, progress.requests[-1])
                self.assertIs(step.submitted, expected_submitted)
                self.assertEqual(step.verdict.result.name if step.verdict else None, expected_verdict)
                self.assertEqual([other.status for other in result.armor[1:]], ["pending"] * 3)
                if stage == "take":
                    self.assertIsNone(row.destroy)
                    self.assertEqual((row.status, row.staged),
                                     ("unconfirmed", "confirmed" if expected_verdict else "unknown"))
                else:
                    self.assertEqual(row.unequip.verdict.result.name, "SUCCESS")
                    self.assertEqual((row.status, row.staged),
                                     ("cleared", None) if expected_verdict else ("unconfirmed", "unknown"))
                self.assertTrue(self.transport.sent.empty())
                if self.transport.block_send is not None:
                    self.transport.block_send.set()
                await self.bot.close()

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

    async def catalog_armor_ready(self):
        await self.join(self.mode.CREATIVE, self.catalog(), self.registry(7, "minecraft:iron_helmet"))
        await self.deliver(self.open(), self.item_slot("ARMOR", 0, None), self.item_slot("ARMOR", 1, 45),
                           self.item_slot("INVENTORY", 0, 43), self.item_slot("OFFHAND", 0, 44),
                           self.item_slot("PLAYER_ONLY_UI", 0, 46))
        return self.bot.creative_catalog.entries[0]

    async def test_equip_creative_armor_targets_only_the_observed_armor_slot(self):
        entry = await self.catalog_armor_ready()
        main, offhand, cursor = self.bot.inventory_slots, self.bot.offhand_item, self.bot.last_cursor_observation
        other = self.bot.armor_slots[1]
        pending = asyncio.create_task(self.bot.equip_creative_armor(entry, 0, timeout=1))
        request = await self.request()
        craft, place = request.actions
        self.assertIsInstance(craft, self.b.ItemStackRequestCereal.CraftCreativeActionData)
        self.assertIsInstance(place, self.b.ItemStackRequestCereal.PlaceActionData)
        self.assertEqual((craft.creative_item_net_id, craft.num_crafts, place.amount), (entry.net_id, 1, 1))
        self.assertEqual((place.source.full_container_name.name.name, place.source.slot, place.source.net_id_variant),
                         ("CREATED_OUTPUT_CONTAINER", 50, request.client_request_id.raw_id))
        self.assertEqual((place.destination.full_container_name.name.name, place.destination.slot,
                          place.destination.net_id_variant), ("ARMOR_CONTAINER", 0, 0))
        self.assertFalse(self.bot.armor_slots[0].id)
        with self.assertRaisesRegex(RuntimeError, "request in flight"):
            await self.bot.equip_creative_armor(entry, armor_slot=0, timeout=1)
        await self.deliver(self.catalog())
        await self.deliver(self.transfer_reply(request, slot=0, domain="ARMOR_CONTAINER", source_empty=False))
        self.assertEqual((await pending).result.name, "SUCCESS")
        helmet = self.bot.armor_slots[0]
        self.assertEqual((helmet.id, helmet.stack_size, helmet.net_id_variant, helmet.aux_value,
                          helmet.block_runtime_id, helmet.user_data_buffer), (7, 1, 51, 7, 0x80000001, _NBT))
        self.assertEqual(self.bot.inventory_slots, main)
        self.assertIs(self.bot.armor_slots[1], other)
        self.assertIs(self.bot.offhand_item, offhand)
        self.assertIs(self.bot.last_cursor_observation, cursor)
        self.assertTrue(self.transport.sent.empty())

    async def test_catalog_and_ordinary_armor_share_in_flight_slots_in_both_orders(self):
        for catalog_first in (True, False):
            with self.subTest(catalog_first=catalog_first):
                entry = await self.catalog_armor_ready()
                await self.deliver(self.item_slot("INVENTORY", 1, 47))

                async def catalog():
                    return await self.bot.equip_creative_armor(entry, 0, timeout=1)

                async def ordinary():
                    return await self.bot.equip_armor(0, 0, timeout=1)

                first, second = (catalog, ordinary) if catalog_first else (ordinary, catalog)
                pending = asyncio.create_task(first())
                head = await self.request()
                if catalog_first:
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                with self.assertRaisesRegex(RuntimeError, "request in flight"):
                    await second()
                self.assertTrue(self.transport.sent.empty())

                other = asyncio.create_task(self.bot.equip_armor(1, 1, timeout=1))
                chest = await self.request()
                self.assertEqual(chest.actions[0].destination.slot, 1)
                await self.deliver(self.transfer_reply(chest, slot=1, net_id=52, domain="ARMOR_CONTAINER",
                                                       source_empty=False))
                self.assertEqual((await other).result.name, "SUCCESS")
                await self.deliver(self.transfer_reply(head, slot=0, domain="ARMOR_CONTAINER", source_empty=False))
                if not catalog_first:
                    self.assertEqual((await pending).result.name, "SUCCESS")
                self.assertEqual([self.bot.armor_slots[i].net_id_variant for i in (0, 1)], [51, 52])
                with self.assertRaisesRegex(ValueError, "observed empty"):
                    await catalog()
                self.assertTrue(self.transport.sent.empty())
            await self.bot.close()

    async def test_equip_creative_armor_rejects_wrong_item_unknown_occupied_stale_or_disabled(self):
        b = self.b
        entry = await self.catalog_armor_ready()
        for selection in ("feet", 2, True, 4, "headwear"):
            with self.subTest(slot=selection), self.assertRaises(ValueError):
                await self.bot.equip_creative_armor(entry, armor_slot=selection)
        with self.assertRaisesRegex(ValueError, "current creative catalog"):
            await self.bot.equip_creative_armor(replace(entry), armor_slot=0)
        await self.deliver(b.InventoryContentPacket(inventory_id=b.ContainerID.ARMOR, slots=[]))
        with self.assertRaisesRegex(ValueError, "observed empty"):
            await self.bot.equip_creative_armor(entry, armor_slot=0)
        await self.deliver(self.item_slot("ARMOR", 0, 66))
        with self.assertRaisesRegex(ValueError, "observed empty"):
            await self.bot.equip_creative_armor(entry, armor_slot=0)
        await self.deliver(self.item_slot("ARMOR", 0, None), self.close())
        with self.assertRaisesRegex(RuntimeError, "open the player inventory screen"):
            await self.bot.equip_creative_armor(entry, armor_slot=0)
        await self.deliver(self.open(), b.SetPlayerGameTypePacket(player_game_type=self.mode.UNDEFINED))
        with self.assertRaises(PermissionError):
            await self.bot.equip_creative_armor(entry, armor_slot=0)
        await self.deliver(b.SetPlayerGameTypePacket(player_game_type=self.mode.CREATIVE),
                           self.registry(7, "minecraft:diamond_helmet"))
        with self.assertRaisesRegex(ValueError, "not supported"):
            await self.bot.equip_creative_armor(self.bot.creative_catalog.entries[0], armor_slot=0)
        self.assertTrue(self.transport.sent.empty())

    async def test_equip_creative_armor_rechecks_mode_catalog_window_and_empty_slot_at_write(self):
        b = self.b
        for packets, error in (
            ((b.SetPlayerGameTypePacket(player_game_type=self.mode.SURVIVAL),), PermissionError),
            ((self.catalog(),), ValueError), ((self.close(), self.open()), RuntimeError),
            ((self.item_slot("ARMOR", 0, 66),), RuntimeError),
        ):
            entry = await self.catalog_armor_ready()
            send = self.bot.packets.send
            reached, release = asyncio.Event(), asyncio.Event()

            async def delay(packet, **kwargs):
                reached.set()
                await release.wait()
                await send(packet, **kwargs)

            with patch.object(self.bot.packets, "send", delay):
                pending = asyncio.create_task(self.bot.equip_creative_armor(entry, armor_slot=0, timeout=1))
                await asyncio.wait_for(reached.wait(), 1)
                await self.deliver(*packets)
                release.set()
                with self.assertRaises(error):
                    await pending
            self.assertTrue(self.transport.sent.empty())
            await self.bot.close()

    async def test_catalog_armor_receipt_does_not_overwrite_newer_inventory_state(self):
        for packets, expected in (
            ((self.item_slot("ARMOR", 0, 66),), 66),
            ((self.item_slot("ARMOR", 0, 66), self.item_slot("ARMOR", 0, None)), 0),
            ((self.close(), self.open()), 0),
        ):
            entry = await self.catalog_armor_ready()
            pending = asyncio.create_task(self.bot.equip_creative_armor(entry, armor_slot=0, timeout=1))
            request = await self.request()
            await self.deliver(*packets, self.transfer_reply(request, slot=0, domain="ARMOR_CONTAINER",
                                                             source_empty=False))
            self.assertEqual((await pending).result.name, "SUCCESS")
            self.assertEqual(int(self.bot.armor_slots[0].net_id_variant or 0), expected)
            await self.bot.close()

    async def test_changed_catalog_armor_receipt_requires_new_observation(self):
        b = self.b
        updated = _NBT.replace(b"\x07\x00\x00\x00", b"\x08\x00\x00\x00", 1)
        for restored in (False, True):
            with self.subTest(restored=restored):
                entry = await self.catalog_armor_ready()
                pending = asyncio.create_task(self.bot.equip_creative_armor(entry, 0, timeout=1))
                request = await self.request()
                await self.deliver(self.catalog(data=updated))
                if restored:
                    await self.deliver(self.catalog())
                await self.deliver(self.transfer_reply(request, slot=0, domain="ARMOR_CONTAINER", source_empty=False))
                reply = await pending
                self.assertEqual(reply.result.name, "SUCCESS")
                self.assertEqual(reply.containers[0].slots[0].item_stack_net_id.raw_id, 51)
                self.assertNotIn(0, self.bot.armor_slots)
                with self.assertRaisesRegex(ValueError, "observed empty"):
                    await self.bot.equip_creative_armor(self.bot.creative_catalog.entries[0], 0, timeout=1)
                self.assertTrue(self.transport.sent.empty())

                observed = self.item_slot("ARMOR", 0, 51, data=updated).item
                await self.deliver(b.InventoryContentPacket(inventory_id=b.ContainerID.ARMOR, slots=[observed]))
                equipped = self.bot.armor_slots[0]
                self.assertEqual((equipped.net_id_variant, equipped.user_data_buffer), (51, updated))
            await self.bot.close()

    async def test_catalog_armor_changed_receipt_preserves_newer_slot_announcement(self):
        updated = _NBT.replace(b"\x07\x00\x00\x00", b"\x08\x00\x00\x00", 1)
        for net_id in (66, None):
            with self.subTest(net_id=net_id):
                entry = await self.catalog_armor_ready()
                pending = asyncio.create_task(self.bot.equip_creative_armor(entry, 0, timeout=1))
                request = await self.request()
                await self.deliver(self.catalog(data=updated), self.item_slot("ARMOR", 0, net_id, data=updated))
                observation = self.bot.armor_slots[0]
                if net_id is not None:
                    with self.assertRaisesRegex(RuntimeError, "request in flight"):
                        await self.bot.unequip_armor(0, destination_slot=1, timeout=1)
                    with self.assertRaisesRegex(RuntimeError, "request in flight"):
                        await self.bot.unequip_armor(0, timeout=1)
                    self.assertTrue(self.transport.sent.empty())
                await self.deliver(self.transfer_reply(request, slot=0, domain="ARMOR_CONTAINER", source_empty=False))
                self.assertEqual((await pending).result.name, "SUCCESS")
                self.assertIs(self.bot.armor_slots[0], observation)
            await self.bot.close()

    async def test_catalog_armor_requires_an_explicit_successful_armor_correction(self):
        entry = await self.catalog_armor_ready()
        for status in ("ERROR", "SUCCESS"):
            pending = asyncio.create_task(self.bot.equip_creative_armor(entry, 0, timeout=1))
            request = await self.request()
            # A success that describes only a non-target slot confirms no armor stack.
            await self.deliver(self.reply(request, 1, slot=0, result=status))
            self.assertEqual((await pending).result.name, status)
            self.assertEqual(self.bot.armor_slots[0].id, 0)
            self.assertEqual(int(self.bot.inventory_slots[0].net_id_variant), 43)

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
