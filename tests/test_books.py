import asyncio
import struct
import unittest
from unittest.mock import patch

from endpilot import Bot
from endpilot.protocol import read_batch, write_batch


WRITABLE = 199
WRITTEN = 200
SLOT = 4
PAGES = ("First \u4e66", "Second\npage")


def _string(value):
    text = value.encode("utf-8")
    return struct.pack("<H", len(text)) + text


def book_data(pages, *, title=None, author=None):
    page_data = b"".join(b"\x08" + _string("text") + _string(page) + b"\0" for page in pages)
    data = b"\xff\xff\x01\x0a\0\0\x09" + _string("pages") + struct.pack("<Bi", 10, len(pages)) + page_data
    if title is not None:
        data += b"\x08" + _string("title") + _string(title)
        data += b"\x08" + _string("author") + _string(author)
        data += b"\x03" + _string("generation") + struct.pack("<i", 0)
    return data + b"\0"


class BatchTransport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.incoming = asyncio.Queue()
        self.edits = []

    def deliver(self, *packets):
        self.incoming.put_nowait(write_batch(self.protocol.encode(packet) for packet in packets))

    async def recv(self):
        return await self.incoming.get()

    async def send(self, data):
        for packet in read_batch(data):
            if packet.packet_id == self.protocol.binding.BookEditPacket.ID:
                self.edits.append(self.protocol.decode(packet))

    async def close(self):
        pass


class BookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("BookWriter", protocol=2225)
        self.addAsyncCleanup(self.bot.close)
        self.binding = self.bot.protocol.binding
        self.transport = BatchTransport(self.bot.protocol)
        b = self.binding
        self.transport.deliver(b.NetworkSettingsPacket(compression_algorithm=b.PacketCompressionAlgorithm.NONE))
        self.writable = self.item(WRITABLE, book_data(PAGES))
        self.transport.deliver(
            b.StartGamePacket(entity_id=7, runtime_id=42),
            b.ItemRegistryPacket(items=[
                b.ItemData(name="minecraft:writable_book", id=WRITABLE),
                b.ItemData(name="minecraft:written_book", id=WRITTEN),
            ]),
            self.slot(SLOT, self.writable), b.PlayStatusPacket(status=3),
        )
        with patch("endpilot.transport.nethernet.connect", return_value=self.transport):
            await self.bot.join_nethernet("http://127.0.0.1:19132", timeout=1)

    def item(self, item_id, data):
        return self.binding.SerializedNetworkItemStackDescriptor(id=item_id, stack_size=1, user_data_buffer=data)

    def slot(self, index, item):
        return self.binding.InventorySlotPacket(inventory_id=self.binding.ContainerID.INVENTORY, slot=index, item=item)

    async def deliver(self, *packets):
        waiter = asyncio.create_task(self.bot.packets.wait_for_packet(packets[-1].ID, timeout=1))
        await asyncio.sleep(0)
        self.transport.deliver(*packets)
        await waiter

    async def test_page_operations_submit_native_edits_without_changing_the_observed_book(self):
        initial = self.bot.get_book(SLOT)
        self.assertEqual((initial.pages, initial.title, initial.author), (PAGES, None, None))
        for action, index, text, other in (
            ("replace_page", 0, "Revised \u4e66", 0), ("add_page", 1, "Inserted", 0),
            ("swap_pages", 0, "", 2), ("delete_page", 1, "", 0),
        ):
            await self.bot.edit_book(SLOT, action, index, text, other)
        self.assertEqual([packet.book_slot for packet in self.transport.edits], [SLOT] * 4)
        replace, add, swap, delete = [packet.operation for packet in self.transport.edits]
        b = self.binding
        self.assertIsInstance(replace, b.ReplacePage)
        self.assertEqual((replace.page_index, replace.page_text), (0, "Revised \u4e66"))
        self.assertIsInstance(add, b.AddPage)
        self.assertEqual((add.page_index, add.page_text), (1, "Inserted"))
        self.assertIsInstance(swap, b.SwapPages)
        self.assertEqual((swap.page_index, swap.swap_with_index), (0, 2))
        self.assertIsInstance(delete, b.DeletePage)
        self.assertEqual(delete.page_index, 1)
        self.assertEqual(self.bot.get_book(SLOT), initial)
        for invalid in (-1, 36, True, 5):
            with self.subTest(slot=invalid), self.assertRaises(ValueError):
                await self.bot.edit_book(invalid, "add_page", 0, "Not submitted")
        self.assertEqual(len(self.transport.edits), 4)

    async def test_signed_book_is_read_from_the_target_slot_and_can_no_longer_be_edited(self):
        await self.bot.sign_book(SLOT, "Draft")
        await self.bot.sign_book(SLOT, "Tales", "Qa Author")
        b = self.binding
        first, second = [packet.operation for packet in self.transport.edits]
        self.assertIsInstance(first, b.Finalize)
        self.assertEqual((first.title, first.author, first.xuid), ("Draft", "BookWriter", ""))
        self.assertEqual((second.title, second.author, second.xuid), ("Tales", "Qa Author", ""))
        self.assertFalse(self.bot.get_book(SLOT).signed)

        data = book_data(PAGES, title="Tales", author="Qa Author")
        signed = self.item(WRITTEN, data)
        pending = asyncio.create_task(self.bot.wait_for_book_update(SLOT, timeout=1))
        await asyncio.sleep(0)
        empty = b.SerializedNetworkItemStackDescriptor()
        slots = [empty] * 36
        slots[SLOT], slots[SLOT + 1] = self.writable, signed
        await self.deliver(self.slot(SLOT + 1, signed), b.InventoryContentPacket(
            inventory_id=b.ContainerID.INVENTORY, slots=slots,
        ))
        self.assertFalse(pending.done())

        await self.deliver(self.slot(SLOT, signed))
        result = await pending
        self.assertTrue(result.signed)
        self.assertEqual((result.pages, result.title, result.author, result.generation, result.user_data),
                         (PAGES, "Tales", "Qa Author", 0, data))
        with self.assertRaisesRegex(ValueError, "writable book"):
            await self.bot.edit_book(SLOT, "replace_page", 0, "Forbidden")
        self.assertEqual(len(self.transport.edits), 2)
