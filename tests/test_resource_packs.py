import asyncio
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from struct import pack_into
import unittest
from unittest.mock import patch
from uuid import UUID
from zipfile import ZipFile

from endpilot import Bot, LoginRefused, ResourcePackCancelled, ResourcePackDecision, ResourcePackError, TextPacketType
from endpilot import resource_packs
from endpilot.protocol import read_batch

from test_boss_bars import BatchTransport


_FIRST = UUID("57470537-6208-4b41-83f5-5c2bf2b559ab")
_SECOND = UUID("13370e9e-90dc-4b16-84b4-7c112cc36581")
_THIRD = UUID("36d8a980-36f5-44b5-8848-67a79505cca6")
_MARKER = "texts/en_US.lang"
_SUBPACKS = [
    {"folder_name": "blue", "name": "Blue Variant", "memory_tier": 6, "memory_performance_tier": 1},
    {"folder_name": "green", "name": "Green Variant", "memory_tier": 12, "memory_performance_tier": 2},
]


class PackTransport(BatchTransport):
    def __init__(self, protocol):
        super().__init__(protocol)
        self.outgoing = asyncio.Queue()
        self.network_settings_sent = False

    async def send(self, data):
        if not self.network_settings_sent:
            self.network_settings_sent = True
            return
        for raw in read_batch(data[1:]):
            if raw.packet_id in (8, 84, 340):
                self.outgoing.put_nowait(self.protocol.decode(raw))


def archive_bytes(pack_id, value, comment=b"", *, manifest_fields=None, files=None):
    manifest = {
        "format_version": 2,
        "header": {"uuid": str(pack_id), "version": [1, 0, 0]},
        "modules": [{"type": "resources"}],
    }
    if manifest_fields is not None:
        manifest.update(manifest_fields)
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.comment = comment
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(_MARKER, value)
        if files is not None:
            for name, payload in files.items():
                archive.writestr(name, payload)
    return data.getvalue()


class ResourcePackArchiveTests(unittest.TestCase):
    def setUp(self):
        self.details = resource_packs.ResourcePackDetails(_FIRST, "1.0.0", 0, "", False, False, False)

    def test_ordinary_archive_at_the_entry_limit_with_comment(self):
        data = archive_bytes(_FIRST, b"fixture", comment=b"fixture archive")
        files, size, count = resource_packs._load_archive(BytesIO(data), self.details, 4096, 2)
        self.assertEqual(count, 2)
        self.assertEqual(size, sum(map(len, files.values())))
        self.assertEqual(files[_MARKER], b"fixture")

    def test_entry_limit_applies_before_opening_zipfile(self):
        data = archive_bytes(_FIRST, b"fixture")
        with patch.object(resource_packs, "ZipFile") as open_archive:
            with self.assertRaisesRegex(ResourcePackError, "ZIP entry limit"):
                resource_packs._load_archive(BytesIO(data), self.details, 4096, 1)
            open_archive.assert_not_called()

    def test_declared_count_matches_the_small_archive_directory_before_opening_zipfile(self):
        data = bytearray(archive_bytes(_FIRST, b"fixture"))
        footer = data.rindex(b"PK\x05\x06")
        for declared in (1, 3):
            with self.subTest(declared=declared):
                pack_into("<HH", data, footer + 8, declared, declared)
                with patch.object(resource_packs, "ZipFile") as open_archive:
                    with self.assertRaisesRegex(ResourcePackError, "does not match its declared entries"):
                        resource_packs._load_archive(BytesIO(data), self.details, 4096, 3)
                    open_archive.assert_not_called()

    def test_invalid_subpack_declarations_are_rejected(self):
        expected = replace(self.details, subpack_name="blue")
        for declarations in (None, {}, ["blue"], [{}], [_SUBPACKS[0], _SUBPACKS[0]],
                             [_SUBPACKS[0], {"folder_name": "../green"}],
                             [_SUBPACKS[0], {"folder_name": "green\\nested"}],
                             [_SUBPACKS[0], {"folder_name": "green\x00"}]):
            with self.subTest(declarations=declarations):
                data = archive_bytes(_FIRST, b"base", manifest_fields={"subpacks": declarations},
                                     files={f"subpacks/blue/{_MARKER}": b"blue"})
                with self.assertRaisesRegex(ResourcePackError, "subpack declarations|subpack folder name"):
                    resource_packs._load_archive(BytesIO(data), expected, 4096, 20)

    def test_subpack_choice_requires_a_manifest_folder_and_archive_directory(self):
        data = archive_bytes(_FIRST, b"base", manifest_fields={"subpacks": _SUBPACKS},
                             files={f"subpacks/green/{_MARKER}": b"green", "subpacks/blue": b"file"})
        for selected, message in (("", "did not select"), ("red", "missing from the manifest"),
                                  ("blue", "directory is missing")):
            with self.subTest(selected=selected):
                with self.assertRaisesRegex(ResourcePackError, message):
                    resource_packs._load_archive(BytesIO(data), replace(self.details, subpack_name=selected),
                                                 4096, 20)

    def test_unselected_members_remain_subject_to_all_archive_checks(self):
        fields = {"subpacks": _SUBPACKS}
        selected = {f"subpacks/blue/{_MARKER}": b"blue"}
        expected = replace(self.details, subpack_name="blue")
        data = archive_bytes(_FIRST, b"base", manifest_fields=fields,
                             files={**selected, "subpacks/green/../outside.txt": b"unsafe"})
        with self.assertRaisesRegex(ResourcePackError, "unsupported archive path"):
            resource_packs._load_archive(BytesIO(data), expected, 4096, 20)
        data = archive_bytes(_FIRST, b"base", manifest_fields=fields,
                             files={**selected, "subpacks/green/large.txt": b"a" * 4096})
        with self.assertRaisesRegex(ResourcePackError, "in-memory index limit"):
            resource_packs._load_archive(BytesIO(data), expected, 4096, 20)
        with self.assertRaisesRegex(ResourcePackError, "ZIP entry limit"):
            resource_packs._load_archive(BytesIO(data), expected, 8192, 3)


class ResourcePackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("PackFixture")
        self.addAsyncCleanup(self.bot.close)
        self.b = self.bot.protocol.binding
        self.transport = PackTransport(self.bot.protocol)
        self.transport.deliver(self.b.NetworkSettingsPacket(
            compression_algorithm=self.b.PacketCompressionAlgorithm.ZLIB, compression_threshold=0,
        ), compressed_connection=False)

    async def join(self, on_offer=None):
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=2, on_resource_packs=on_offer)

    async def outgoing(self):
        async with asyncio.timeout(1):
            return await self.transport.outgoing.get()

    def info(self, pack_ids, *, required=False, key="", subpacks=None):
        b = self.b
        subpacks = {} if subpacks is None else subpacks
        return b.ResourcePacksInfoPacket(resource_pack_required=required, resource_packs=[
            b.PackInfoData(pack_id_version=b.PackIdVersion(id=pack_id, version=b.SemVersion(version="1.0.0")),
                           pack_size=50, content_key=key, subpack_name=subpacks.get(pack_id, ""))
            for pack_id in pack_ids
        ])

    def stack(self, *pack_ids, subpacks=None):
        subpacks = {} if subpacks is None else subpacks
        return self.b.ResourcePackStackPacket(texture_pack_ids_and_versions=[
            self.b.PackInstanceId(pack_id=str(pack_id), version="1.0.0", subpack_name=subpacks.get(pack_id, ""))
            for pack_id in pack_ids
        ])

    def metadata(self, pack_id, data, chunk_size, *, hash_data=None, premium=False):
        return self.b.ResourcePackDataInfoPacket(
            resource_name=f"{pack_id}_1.0.0", file_size=len(data), chunk_size=chunk_size,
            nb_chunks=(len(data) + chunk_size - 1) // chunk_size, pack_type=self.b.PackType.RESOURCES,
            file_hash=hashlib.sha256(data if hash_data is None else hash_data).digest(), is_premium=premium,
        )

    def chunk(self, pack_id, index, offset, data):
        return self.b.ResourcePackChunkDataPacket(
            resource_name=f"{pack_id}_1.0.0", chunk_id=index, byte_offset=offset, data=data,
        )

    def start_game(self):
        self.transport.deliver(self.b.StartGamePacket(runtime_id=11, entity_id=45),
                               self.b.PlayStatusPacket(status=3))

    async def test_stack_loads_from_sequential_data_with_pipelined_descriptors(self):
        b = self.b
        decisions = []

        async def accept(offer):
            decisions.append(offer)
            return ResourcePackDecision.ACCEPT

        pending = asyncio.create_task(self.join(accept))
        self.transport.deliver(self.info((_FIRST, _SECOND), required=True))
        downloading = (await self.outgoing()).response
        self.assertEqual(downloading.downloading_packs, [f"{_FIRST}_1.0.0", f"{_SECOND}_1.0.0"])
        self.assertTrue(decisions[0].required)
        self.assertEqual([(item.pack_id, item.version, item.size) for item in decisions[0].packs],
                         [(_FIRST, "1.0.0", 50), (_SECOND, "1.0.0", 50)])
        first_title = "First resource pack"
        first = archive_bytes(_FIRST, b"first", manifest_fields={
            "header": {"name": first_title, "uuid": str(_FIRST), "version": [1, 0, 0]},
        })
        second = archive_bytes(_SECOND, b"second")
        split = len(first) - 15
        self.transport.deliver(self.metadata(_FIRST, first, split), self.metadata(_SECOND, second, len(second)))
        for pack_id, index, offset, data in (
            (_FIRST, 0, 0, first[:split]), (_FIRST, 1, split, first[split:]), (_SECOND, 0, 0, second),
        ):
            request = await self.outgoing()
            self.assertEqual((request.resource_name, request.chunk), (f"{pack_id}_1.0.0", index))
            self.transport.deliver(self.chunk(pack_id, index, offset, data))
        self.assertIsInstance((await self.outgoing()).response, b.ResourcePackClientResponsePacket.DownloadingFinished)
        self.assertIsNone(self.bot.resource_pack_index)

        # Stack order, rather than download order, decides the selected file.
        self.transport.deliver(self.stack(str(_SECOND).upper(), _FIRST.hex))
        self.assertIsInstance((await self.outgoing()).response,
                              b.ResourcePackClientResponsePacket.ResourcePackStackFinished)
        self.start_game()
        await pending
        index = self.bot.resource_pack_index
        self.assertEqual(index.read(_MARKER), b"first")
        header = json.loads(index.read("manifest.json"))["header"]
        self.assertEqual((header["name"], UUID(header["uuid"])), (first_title, _FIRST))
        self.assertEqual([(pack.pack_id, pack.size, pack.sha256) for pack in index.packs], [
            (_SECOND, len(second), hashlib.sha256(second).hexdigest()),
            (_FIRST, len(first), hashlib.sha256(first).hexdigest()),
        ])
        self.assertEqual([pack.subpack_name for pack in index.packs], ["", ""])
        with self.assertRaises(TypeError):
            index.resources[_MARKER] = b"mutated"
        await self.bot.close()
        self.assertIsNone(self.bot.resource_pack_index)
        self.assertEqual(index.read(_MARKER), b"first")

    async def deliver_complete_archive(self, pack_id, data):
        self.transport.deliver(self.metadata(pack_id, data, len(data)))
        request = await self.outgoing()
        self.assertEqual((request.resource_name, request.chunk), (f"{pack_id}_1.0.0", 0))
        self.transport.deliver(self.chunk(pack_id, 0, 0, data))

    async def test_server_subpacks_overlay_package_roots_in_stack_order(self):
        selections = {_FIRST: "blue", _SECOND: "green"}

        def accept(offer):
            self.assertEqual({pack.pack_id: pack.subpack_name for pack in offer.packs}, selections)
            return ResourcePackDecision.ACCEPT

        pending = asyncio.create_task(self.join(accept))
        self.transport.deliver(self.info((_FIRST, _SECOND), subpacks=selections))
        downloading = (await self.outgoing()).response
        self.assertEqual(downloading.downloading_packs, [f"{_FIRST}_1.0.0", f"{_SECOND}_1.0.0"])
        fields = {"subpacks": _SUBPACKS}
        first_blue = b"fixture.message=First blue: %%2, %%1, %%2"
        french = b"fixture.message=Racine: %s et %s"
        first = archive_bytes(_FIRST, b"fixture.message=First root", manifest_fields=fields, files={
            "subpacks/blue/": b"",
            f"subpacks/blue/{_MARKER}": first_blue,
            "subpacks/blue/textures/blue.png": b"blue texture",
            "subpacks/green/texts/de_DE.lang": b"fixture.message=First green",
            "subpacks/green/unselected-first.txt": b"first green",
            "texts/fr_FR.lang": french,
            "root-only.txt": b"first fallback",
            "shared.txt": b"first root overrides earlier subpack",
        })
        second = archive_bytes(_SECOND, b"fixture.message=Second root", manifest_fields=fields, files={
            f"subpacks/green/{_MARKER}": b"fixture.message=Second green\nfixture.lower=Lower stack key",
            "subpacks/green/textures/green.png": b"green texture",
            "textures/green.png": b"second root texture",
            "subpacks/green/shared.txt": b"second green",
            "subpacks/blue/unselected-second.txt": b"second blue",
        })
        await self.deliver_complete_archive(_FIRST, first)
        await self.deliver_complete_archive(_SECOND, second)
        response = self.b.ResourcePackClientResponsePacket
        self.assertIsInstance((await self.outgoing()).response, response.DownloadingFinished)
        self.assertIsNone(self.bot.resource_pack_index)

        self.transport.deliver(self.stack(_SECOND, _FIRST, subpacks=selections))
        self.assertIsInstance((await self.outgoing()).response, response.ResourcePackStackFinished)
        self.start_game()
        await pending
        index = self.bot.resource_pack_index
        self.assertEqual([(pack.pack_id, pack.subpack_name) for pack in index.packs], [
            (_SECOND, "green"), (_FIRST, "blue"),
        ])
        self.assertEqual({name: value for name, value in index.resources.items() if name != "manifest.json"}, {
            _MARKER: first_blue,
            "root-only.txt": b"first fallback",
            "shared.txt": b"first root overrides earlier subpack",
            "texts/fr_FR.lang": french,
            "textures/blue.png": b"blue texture",
            "textures/green.png": b"green texture",
        })

        events = asyncio.Queue()
        self.bot.subscribe_text(events.put_nowait, kind=TextPacketType.TEXT_OBJECT_WHISPER)
        text = self.b.TextPacket
        self.transport.deliver(*(text(
            localize=False,
            body=text.MessageOnly(type=TextPacketType.TEXT_OBJECT_WHISPER, message=json.dumps({"rawtext": [node]})),
            xuid="", platform_id="", filtered_message=None,
        ) for node in (
            {"translate": "fixture.message", "with": {"rawtext": [{"text": "left"}, {"text": "right"}]}},
            {"translate": "fixture.lower"},
        )))
        async with asyncio.timeout(1):
            event, lower = await events.get(), await events.get()
        self.assertEqual(event.resolve_text(locale="en_US", resource_pack_index=index), "First blue: right, left, right")
        self.assertEqual(event.resolve_text(locale="fr_FR", resource_pack_index=index), "Racine: left et right")
        self.assertIsNone(event.resolve_text(locale="de_DE", resource_pack_index=index))
        self.assertIsNone(lower.resolve_text(locale="en_US", resource_pack_index=index))

    async def assert_invalid_stack(self, pack_id, subpack, message):
        pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.ACCEPT))
        self.transport.deliver(self.info((_FIRST,), subpacks={_FIRST: "blue"}))
        await self.outgoing()
        data = archive_bytes(_FIRST, b"base", manifest_fields={"subpacks": _SUBPACKS}, files={
            f"subpacks/blue/{_MARKER}": b"blue", f"subpacks/green/{_MARKER}": b"green",
        })
        await self.deliver_complete_archive(_FIRST, data)
        self.assertIsInstance((await self.outgoing()).response, self.b.ResourcePackClientResponsePacket.DownloadingFinished)
        self.transport.deliver(self.stack(pack_id, subpacks={pack_id: subpack}))
        with self.assertRaisesRegex(ResourcePackError, message):
            await pending
        self.assertIsNone(self.bot.resource_pack_index)
        self.assertTrue(self.transport.closed and self.transport.outgoing.empty())

    async def test_stack_cannot_change_an_offered_subpack_even_to_another_valid_choice(self):
        await self.assert_invalid_stack(_FIRST, "green", "stack subpack does not match the offer")

    async def test_stack_cannot_omit_the_offered_subpack_name(self):
        await self.assert_invalid_stack(_FIRST, "", "stack subpack does not match the offer")

    async def test_stack_rejects_an_invalid_pack_id(self):
        await self.assert_invalid_stack("invalid-uuid", "blue", "invalid resource-pack ID")

    async def test_stack_rejects_braces_around_a_loaded_pack_id(self):
        await self.assert_invalid_stack(f"{{{_FIRST}}}", "blue", "invalid resource-pack ID")

    async def test_stack_rejects_an_unloaded_pack_id(self):
        await self.assert_invalid_stack(_SECOND, "blue", "unloaded resource pack")

    async def test_subpack_filtering_does_not_release_the_unpacked_budget_for_later_archives(self):
        first = archive_bytes(_FIRST, b"base", manifest_fields={"subpacks": _SUBPACKS}, files={
            f"subpacks/blue/{_MARKER}": b"blue", f"subpacks/green/{_MARKER}": b"green",
        })
        second = archive_bytes(_SECOND, b"ordinary")
        limit = 0
        for data in (first, second):
            with ZipFile(BytesIO(data)) as archive:
                limit += sum(file.file_size for file in archive.infolist())
        with patch.object(resource_packs, "_MAX_RESOURCES", limit - 1):
            pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.ACCEPT))
            self.transport.deliver(self.info((_FIRST, _SECOND), subpacks={_FIRST: "blue"}))
            await self.outgoing()
        await self.deliver_complete_archive(_FIRST, first)
        await self.deliver_complete_archive(_SECOND, second)
        with self.assertRaisesRegex(ResourcePackError, "in-memory index limit"):
            await pending
        self.assertIsNone(self.bot.resource_pack_index)
        self.assertTrue(self.transport.closed and self.transport.outgoing.empty())

    async def test_empty_offer_keeps_join_without_asking_for_consent(self):
        pending = asyncio.create_task(self.join(lambda offer: self.fail("empty offer prompted for consent")))
        self.transport.deliver(self.info(()), self.stack())
        b = self.b.ResourcePackClientResponsePacket
        self.assertIsInstance((await self.outgoing()).response, b.DownloadingFinished)
        self.assertIsInstance((await self.outgoing()).response, b.ResourcePackStackFinished)
        self.start_game()
        await pending
        self.assertFalse(self.bot.resource_pack_index.resources)

    async def test_default_refusal_records_server_disconnect_instead_of_completion(self):
        pending = asyncio.create_task(self.join())
        self.transport.deliver(self.info((_FIRST,), required=True))
        self.assertIsInstance((await self.outgoing()).response, self.b.ResourcePackClientResponsePacket.Cancel)
        self.transport.deliver(self.b.DisconnectPacket())
        with self.assertRaises(LoginRefused):
            await pending
        self.assertTrue(self.transport.closed)
        self.assertIsNone(self.bot.resource_pack_index)
        self.assertTrue(self.transport.outgoing.empty())

    async def test_optional_refusal_can_follow_server_empty_stack(self):
        pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.REFUSE))
        self.transport.deliver(self.info((_FIRST,)))
        b = self.b.ResourcePackClientResponsePacket
        self.assertIsInstance((await self.outgoing()).response, b.Cancel)
        self.transport.deliver(self.stack())
        self.assertIsInstance((await self.outgoing()).response, b.ResourcePackStackFinished)
        self.start_game()
        await pending
        self.assertEqual(self.bot.resource_pack_index.packs, ())

    async def test_cancel_closes_connection_without_reporting_completion(self):
        pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.CANCEL))
        self.transport.deliver(self.info((_FIRST,)))
        self.assertIsInstance((await self.outgoing()).response, self.b.ResourcePackClientResponsePacket.Cancel)
        with self.assertRaises(ResourcePackCancelled):
            await pending
        self.assertTrue(self.transport.closed)
        self.assertTrue(self.transport.outgoing.empty())

    async def test_encrypted_offer_refuses_to_start_download(self):
        pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.ACCEPT))
        self.transport.deliver(self.info((_FIRST,), key="fixture-encryption", subpacks={_FIRST: "blue"}))
        with self.assertRaisesRegex(ResourcePackError, "unencrypted"):
            await pending
        self.assertTrue(self.transport.outgoing.empty())

    async def test_unsafe_subpack_offer_refuses_to_start_download(self):
        pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.ACCEPT))
        self.transport.deliver(self.info((_FIRST,), subpacks={_FIRST: "../blue"}))
        with self.assertRaisesRegex(ResourcePackError, "invalid resource-pack subpack folder name"):
            await pending
        self.assertTrue(self.transport.closed and self.transport.outgoing.empty())

    async def test_premium_subpack_metadata_refuses_to_request_chunks(self):
        pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.ACCEPT))
        self.transport.deliver(self.info((_FIRST,), subpacks={_FIRST: "blue"}))
        await self.outgoing()
        self.transport.deliver(self.metadata(_FIRST, b"fixture metadata only", 64, premium=True))
        with self.assertRaisesRegex(ResourcePackError, "unsupported resource-pack format"):
            await pending
        self.assertTrue(self.transport.closed and self.transport.outgoing.empty())

    async def test_out_of_order_resource_chunk_fails(self):
        pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.ACCEPT))
        self.transport.deliver(self.info((_FIRST,)))
        await self.outgoing()
        data = archive_bytes(_FIRST, b"fixture")
        self.transport.deliver(self.metadata(_FIRST, data, len(data)))
        self.assertEqual((await self.outgoing()).chunk, 0)
        self.transport.deliver(self.chunk(_FIRST, 1, 0, data))
        with self.assertRaisesRegex(ResourcePackError, "index or offset"):
            await pending
        self.assertTrue(self.transport.outgoing.empty())

    async def assert_invalid_archive(self, data, message, *, hash_data=None):
        pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.ACCEPT))
        self.transport.deliver(self.info((_FIRST,)))
        await self.outgoing()
        self.transport.deliver(self.metadata(_FIRST, data, len(data), hash_data=hash_data))
        await self.outgoing()
        self.transport.deliver(self.chunk(_FIRST, 0, 0, data))
        with self.assertRaisesRegex(ResourcePackError, message):
            await pending
        self.assertTrue(self.transport.outgoing.empty())

    async def test_incorrect_archive_hash_does_not_complete_download(self):
        await self.assert_invalid_archive(archive_bytes(_FIRST, b"fixture"), "SHA-256", hash_data=b"other")

    async def test_archive_manifest_must_match_offer_even_with_correct_hash(self):
        await self.assert_invalid_archive(archive_bytes(_SECOND, b"fixture"), "manifest identity")

    async def test_script_manifest_is_still_rejected(self):
        await self.assert_invalid_archive(archive_bytes(_FIRST, b"fixture", manifest_fields={
            "modules": [{"type": "resources"}, {"type": "script"}],
        }), "only ordinary resource-pack modules")

    async def start_partial_download(self):
        exchanges = []

        def track(*args):
            exchange = resource_packs.ResourcePackExchange(*args)
            exchanges.append(exchange)
            return exchange

        with patch("endpilot.login.ResourcePackExchange", side_effect=track):
            pending = asyncio.create_task(self.join(lambda offer: ResourcePackDecision.ACCEPT))
            self.transport.deliver(self.info((_FIRST, _SECOND, _THIRD), subpacks={_FIRST: "blue"}))
            await self.outgoing()
        first = archive_bytes(_FIRST, b"base", manifest_fields={"subpacks": _SUBPACKS}, files={
            f"subpacks/blue/{_MARKER}": b"first", f"subpacks/green/{_MARKER}": b"unused",
        })
        second = archive_bytes(_SECOND, b"second")
        third = archive_bytes(_THIRD, b"third")
        split = len(second) - 7
        self.transport.deliver(self.metadata(_FIRST, first, len(first)), self.metadata(_SECOND, second, split),
                               self.metadata(_THIRD, third, len(third)))
        self.assertEqual((await self.outgoing()).resource_name, f"{_FIRST}_1.0.0")
        self.transport.deliver(self.chunk(_FIRST, 0, 0, first))
        self.assertEqual((await self.outgoing()).resource_name, f"{_SECOND}_1.0.0")
        self.transport.deliver(self.chunk(_SECOND, 0, 0, second[:split]))
        self.assertEqual((await self.outgoing()).chunk, 1)

        exchange = exchanges[0]
        active, queued = exchange._download.data, exchange._queued[0].data
        _, previous = exchange._loaded[_FIRST, "1.0.0"]
        self.assertEqual(active.tell(), split)
        self.assertFalse(queued.closed)
        self.assertEqual(previous[_MARKER], b"first")
        return pending, active, queued, previous, split, second

    async def test_cancel_releases_active_and_queued_buffers_and_loaded_pack(self):
        pending, active, queued, previous, _, _ = await self.start_partial_download()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(active.closed and queued.closed and self.transport.closed)
        self.assertFalse(previous)
        self.assertIsNone(self.bot.resource_pack_index)

    async def test_failed_download_releases_buffers_and_loaded_pack_with_error_retained(self):
        pending, active, queued, previous, split, second = await self.start_partial_download()
        self.transport.deliver(self.chunk(_SECOND, 1, split, second[split:] + b"!"))
        try:
            await pending
        except ResourcePackError as error:
            self.assertRegex(str(error), "chunk length")
            self.assertIsNotNone(error.__traceback__)
            self.assertTrue(active.closed and queued.closed and self.transport.closed)
            self.assertFalse(previous)
        else:
            self.fail("expected mismatched final chunk to abort the download")
        self.assertIsNone(self.bot.resource_pack_index)
