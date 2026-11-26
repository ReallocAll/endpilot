"""Submit native book edits and observe the current book in a player slot."""

from dataclasses import dataclass
from typing import Literal

from .inventory import Inventory
from .item_user_data import read_item_user_data


BookEdit = Literal["replace_page", "add_page", "delete_page", "swap_pages"]
_WRITABLE_BOOK = "minecraft:writable_book"
_WRITTEN_BOOK = "minecraft:written_book"


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Book data observed in the main inventory, including the original user data."""

    slot: int
    item_id: str
    pages: tuple[str, ...]
    title: str | None
    author: str | None
    generation: int | None
    user_data: bytes

    @property
    def signed(self) -> bool:
        return self.item_id == _WRITTEN_BOOK


def _validate_slot(slot: int) -> None:
    if type(slot) is not int or not 0 <= slot < 36:
        raise ValueError("inventory slot must be between 0 and 35")


class Books:
    def __init__(self, inventory: Inventory, item_names: dict[int, str], player_name: str):
        self._inventory = inventory
        self._packets = inventory.packets
        self._item_names = item_names
        self._player_name = player_name

    def _item_name(self, item) -> str | None:
        return None if item is None or not item.stack_size else self._item_names.get(int(item.id))

    def get(self, slot: int) -> BookSnapshot | None:
        _validate_slot(slot)
        item = self._inventory.slots.get(slot)
        name = self._item_name(item)
        if name not in (_WRITABLE_BOOK, _WRITTEN_BOOK):
            return None
        user_data = bytes(item.user_data_buffer)
        metadata = read_item_user_data(user_data)
        pages = metadata.get("pages", [])
        if not isinstance(pages, list) or any(
            not isinstance(page, dict) or not isinstance(page.get("text"), str) for page in pages
        ):
            raise ValueError("invalid observed book pages")
        title, author, generation = (metadata.get(field) for field in ("title", "author", "generation"))
        return BookSnapshot(
            slot, name, tuple(page["text"] for page in pages),
            title if isinstance(title, str) else None, author if isinstance(author, str) else None,
            generation if isinstance(generation, int) else None, user_data,
        )

    async def wait_for_update(self, slot: int, *, timeout: float) -> BookSnapshot | None:
        before = self.get(slot)
        observed = before
        binding = self._packets.protocol.binding

        def changed(_raw):
            nonlocal observed
            # Inventory applies server slot changes before this waiter runs.
            observed = self.get(slot)
            return observed != before

        await self._packets.wait_for_packet(
            (binding.InventoryContentPacket.ID, binding.InventorySlotPacket.ID, binding.InventoryTransactionPacket.ID),
            timeout=timeout, match=changed,
        )
        return observed

    def _require_writable(self, slot: int):
        _validate_slot(slot)
        if self._item_name(self._inventory.slots.get(slot)) != _WRITABLE_BOOK:
            raise ValueError("slot does not contain an observed writable book")

    async def edit(self, slot: int, operation: BookEdit, page_index: int, text: str, other_index: int) -> None:
        self._require_writable(slot)
        binding = self._packets.protocol.binding
        match operation:
            case "replace_page":
                edit = binding.ReplacePage(page_index=page_index, page_text=text, photo_name="")
            case "add_page":
                edit = binding.AddPage(page_index=page_index, page_text=text, photo_name="")
            case "delete_page":
                edit = binding.DeletePage(page_index=page_index)
            case "swap_pages":
                edit = binding.SwapPages(page_index=page_index, swap_with_index=other_index)
            case _:
                raise ValueError(f"unknown book edit {operation!r}")
        await self._packets.send(binding.BookEditPacket(book_slot=slot, operation=edit))

    async def sign(self, slot: int, title: str, author: str | None) -> None:
        self._require_writable(slot)
        binding = self._packets.protocol.binding
        sign = binding.Finalize(title=title, author=author or self._player_name, xuid="")
        await self._packets.send(binding.BookEditPacket(book_slot=slot, operation=sign))
