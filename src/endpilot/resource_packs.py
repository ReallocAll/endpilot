"""Join-time delivery and read-only lookup of ordinary Bedrock resource packs."""

from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
from io import BytesIO
from inspect import isawaitable
import json
from stat import S_IFLNK, S_IFMT
from struct import unpack
from types import MappingProxyType
from uuid import UUID
import zipfile
from zipfile import BadZipFile, ZipFile
from zlib import error as DeflateError


# The session retains the loaded resources in memory.
_MAX_ARCHIVES = 64 * 1024 * 1024
_MAX_RESOURCES = 128 * 1024 * 1024
_MAX_ENTRIES = 32_768


class ResourcePackDecision(str, Enum):
    ACCEPT = "accept"
    REFUSE = "refuse"
    CANCEL = "cancel"


class ResourcePackError(ConnectionError):
    """The resource-pack exchange could not proceed."""


class ResourcePackCancelled(ResourcePackError):
    """The caller cancelled resource-pack negotiation."""


@dataclass(frozen=True, slots=True)
class ResourcePackDetails:
    pack_id: UUID
    version: str
    size: int
    subpack_name: str
    encrypted: bool
    has_scripts: bool
    is_addon: bool


@dataclass(frozen=True, slots=True)
class ResourcePackOffer:
    required: bool
    packs: tuple[ResourcePackDetails, ...]


PackDecisionHandler = Callable[
    [ResourcePackOffer], ResourcePackDecision | Awaitable[ResourcePackDecision]
]


@dataclass(frozen=True, slots=True)
class LoadedResourcePack:
    pack_id: UUID
    version: str
    size: int
    sha256: str
    subpack_name: str = ""


@dataclass(frozen=True, slots=True)
class ResourcePackIndex:
    """Pack order and bytes, with later stack entries overriding earlier ones."""

    packs: tuple[LoadedResourcePack, ...]
    resources: Mapping[str, bytes]

    def read(self, path: str) -> bytes:
        """Read an exact pack path, raising KeyError when absent."""
        return self.resources[path]


_EMPTY_INDEX = ResourcePackIndex((), MappingProxyType({}))


def _version(value):
    if isinstance(value, list) and len(value) == 3 and all(type(part) is int and part >= 0 for part in value):
        return ".".join(map(str, value))
    if isinstance(value, str):
        return value
    raise ValueError("invalid manifest version")


def _is_subpack_folder_name(name):
    return (isinstance(name, str) and name not in ("", ".", "..")
            and not any(character in name for character in ("/", "\\", ":", "\x00")))


def _check_zip_directory(data, remaining_entries):
    # Use the same end record as ZipFile. A pack under the in-memory limits
    # fits in a conventional central directory, including an archive comment.
    end_record = zipfile._EndRecData(data)
    if not end_record or end_record[zipfile._ECD_SIGNATURE] != zipfile.stringEndArchive:
        raise ResourcePackError("resource packs require an ordinary ZIP central directory")
    count = end_record[zipfile._ECD_ENTRIES_TOTAL]
    if count > remaining_entries:
        raise ResourcePackError("resource packs exceed the ZIP entry limit")
    end = end_record[zipfile._ECD_LOCATION]
    position = end - end_record[zipfile._ECD_SIZE]
    if (end_record[zipfile._ECD_DISK_NUMBER] or end_record[zipfile._ECD_DISK_START]
            or count != end_record[zipfile._ECD_ENTRIES_THIS_DISK]
            or not 0 <= end_record[zipfile._ECD_OFFSET] <= position
            or end + zipfile.sizeEndCentDir + end_record[zipfile._ECD_COMMENT_SIZE] != data.seek(0, 2)):
        raise ResourcePackError("invalid resource-pack ZIP central directory")
    # Check only fixed record headers. A false count or truncated record cannot
    # make ZipFile allocate a longer ZipInfo list than this bounded scan.
    for _ in range(count):
        if end - position < zipfile.sizeCentralDir:
            raise ResourcePackError("resource-pack ZIP directory does not match its declared entries")
        data.seek(position)
        header = unpack(zipfile.structCentralDir, data.read(zipfile.sizeCentralDir))
        if header[zipfile._CD_SIGNATURE] != zipfile.stringCentralDir:
            raise ResourcePackError("invalid resource-pack ZIP central directory entry")
        position += (zipfile.sizeCentralDir + header[zipfile._CD_FILENAME_LENGTH]
                     + header[zipfile._CD_EXTRA_FIELD_LENGTH] + header[zipfile._CD_COMMENT_LENGTH])
    if position != end:
        raise ResourcePackError("resource-pack ZIP directory does not match its declared entries")


def _load_archive(data, expected, remaining_bytes, remaining_entries):
    resources = {}
    try:
        _check_zip_directory(data, remaining_entries)
        with ZipFile(data) as archive:
            files = archive.infolist()
            unpacked_size = sum(file.file_size for file in files)
            if unpacked_size > remaining_bytes:
                raise ResourcePackError("resource packs exceed the in-memory index limit")
            for file in files:
                name = file.filename
                path = name.removesuffix("/")
                if (any(part in ("", ".", "..") for part in path.split("/"))
                        or any(character in path for character in ("\\", ":"))
                        or name != file.orig_filename or S_IFMT(file.external_attr >> 16) == S_IFLNK):
                    raise ResourcePackError("resource pack contains an unsupported archive path")
                if not file.is_dir():
                    if name in resources:
                        raise ResourcePackError("resource pack contains duplicate file paths")
                    with archive.open(file) as stream:
                        resources[name] = stream.read(file.file_size + 1)
                    if len(resources[name]) != file.file_size:
                        raise ResourcePackError("resource file length does not match the ZIP directory")
        manifest = json.loads(resources["manifest.json"])
        header = manifest["header"]
        if UUID(header["uuid"]) != expected.pack_id or _version(header["version"]) != expected.version:
            raise ResourcePackError("resource-pack manifest identity does not match the offer")
        modules = manifest["modules"]
        if not modules or any(module["type"] != "resources" for module in modules):
            raise ResourcePackError("only ordinary resource-pack modules are supported")

        subpacks = manifest.get("subpacks", [])
        if not isinstance(subpacks, list):
            raise ResourcePackError("invalid resource-pack subpack declarations")
        folders = set()
        for subpack in subpacks:
            name = subpack.get("folder_name") if isinstance(subpack, dict) else None
            if not _is_subpack_folder_name(name) or name in folders:
                raise ResourcePackError("invalid or duplicate resource-pack subpack folder name")
            folders.add(name)

        selected = expected.subpack_name
        if not selected and folders:
            raise ResourcePackError("server did not select a declared resource-pack subpack")
        if selected:
            if selected not in folders:
                raise ResourcePackError("offered resource-pack subpack is missing from the manifest")
            prefix = f"subpacks/{selected}/"
            if not any(file.filename.startswith(prefix) for file in files):
                raise ResourcePackError("selected resource-pack subpack directory is missing from the archive")
            overlay = {name[len(prefix):]: value for name, value in resources.items() if name.startswith(prefix)}
            resources = {name: value for name, value in resources.items() if not name.startswith("subpacks/")}
            resources.update(overlay)
        return resources, unpacked_size, len(files)
    except BaseException:
        resources.clear()
        raise


@dataclass(slots=True)
class _Download:
    details: ResourcePackDetails
    resource_name: str
    size: int
    chunk_size: int
    count: int
    file_hash: bytes
    data: BytesIO
    next_chunk: int = 0
    offset: int = 0


class ResourcePackExchange:
    """Handles packets in the existing join reader."""

    def __init__(self, packets, decide: PackDecisionHandler | None):
        self._packets = packets
        self._binding = packets.protocol.binding
        self._decide = decide
        self._pending = {}
        self._download = None
        self._queued = deque()
        self._loaded = {}
        self._remaining_archives = _MAX_ARCHIVES
        self._remaining_bytes = _MAX_RESOURCES
        self._remaining_entries = _MAX_ENTRIES
        self._phase = "initial"
        self._required = False
        self.plain_pack_info = False
        self.plain_pack_stack = False
        self.index = _EMPTY_INDEX
        b = self._binding
        self.packet_ids = (
            b.ResourcePacksInfoPacket.ID, b.ResourcePackDataInfoPacket.ID, b.ResourcePackChunkDataPacket.ID,
            b.ResourcePackStackPacket.ID,
        )

    async def _response(self, variant):
        await self._packets.send(self._binding.ResourcePackClientResponsePacket(response=variant))

    def close(self):
        if self._download is not None:
            self._download.data.close()
            self._download = None
        for download in self._queued:
            download.data.close()
        self._queued.clear()
        for _, resources in self._loaded.values():
            resources.clear()
        self._loaded.clear()
        self._pending.clear()
        self.index = _EMPTY_INDEX

    async def handle(self, packet):
        b = self._binding
        if isinstance(packet, b.ResourcePacksInfoPacket):
            await self._info(packet)
        elif isinstance(packet, b.ResourcePackDataInfoPacket):
            await self._data_info(packet)
        elif isinstance(packet, b.ResourcePackChunkDataPacket):
            await self._chunk(packet)
        elif isinstance(packet, b.ResourcePackStackPacket):
            await self._stack(packet)

    async def _info(self, packet):
        if self._phase != "initial":
            raise ResourcePackError("server repeated the resource-pack offer")
        response = self._binding.ResourcePackClientResponsePacket
        self.plain_pack_info = not (packet.has_addon_packs or packet.has_scripts)
        self._required = bool(packet.resource_pack_required)
        if not packet.resource_packs:
            self._phase = "stack"
            await self._response(response.DownloadingFinished())
            return
        details = tuple(
            ResourcePackDetails(
                item.pack_id_version.id, item.pack_id_version.version.version, int(item.pack_size),
                item.subpack_name, bool(item.content_key), bool(item.has_scripts), bool(item.is_addon_pack),
            )
            for item in packet.resource_packs
        )
        offer = ResourcePackOffer(self._required, details)
        decision = ResourcePackDecision.REFUSE if self._decide is None else self._decide(offer)
        if isawaitable(decision):
            decision = await decision
        if not isinstance(decision, ResourcePackDecision):
            raise TypeError("resource-pack decision must be a ResourcePackDecision")
        if decision is not ResourcePackDecision.ACCEPT:
            self._phase = "refused"
            await self._response(response.Cancel())
            if decision is ResourcePackDecision.CANCEL:
                raise ResourcePackCancelled("resource-pack negotiation was cancelled")
            return
        if any(item.encrypted or item.has_scripts or item.is_addon for item in details):
            raise ResourcePackError("only ordinary unencrypted resource packs are supported")
        if any(item.subpack_name and not _is_subpack_folder_name(item.subpack_name) for item in details):
            raise ResourcePackError("server advertised an invalid resource-pack subpack folder name")
        if any(item.size <= 0 for item in details):
            raise ResourcePackError("server advertised an invalid resource-pack size")
        for item in details:
            name = f"{item.pack_id}_{item.version}"
            if name in self._pending:
                raise ResourcePackError("server repeated an identity in the resource-pack offer")
            self._pending[name] = item
        self._phase = "download"
        await self._response(response.Downloading(downloading_packs=list(self._pending)))

    async def _data_info(self, packet):
        if self._phase != "download":
            raise ResourcePackError("unexpected resource-pack download metadata")
        requested = self._pending.pop(packet.resource_name, None)
        if requested is None:
            raise ResourcePackError("download metadata does not identify an offered pack")
        if packet.is_premium or packet.pack_type != self._binding.PackType.RESOURCES:
            raise ResourcePackError("server offered an unsupported resource-pack format")
        size, chunk_size, count = int(packet.file_size), int(packet.chunk_size), int(packet.nb_chunks)
        # Vanilla can advertise unpacked source bytes in the offer. The transfer
        # descriptor specifies the actual length of the repackaged ZIP.
        if (not 0 < size <= self._remaining_archives or not 0 < chunk_size <= _MAX_ARCHIVES
                or count != (size + chunk_size - 1) // chunk_size or len(packet.file_hash) != 32):
            raise ResourcePackError("invalid resource-pack download size, chunk count or SHA-256")
        self._remaining_archives -= size
        download = _Download(requested, packet.resource_name, size, chunk_size, count,
                             bytes(packet.file_hash), BytesIO())
        if self._download is None:
            self._download = download
            await self._request_chunk()
        else:
            self._queued.append(download)

    async def _request_chunk(self):
        current = self._download
        await self._packets.send(self._binding.ResourcePackChunkRequestPacket(
            resource_name=current.resource_name, chunk=current.next_chunk,
        ))

    async def _chunk(self, packet):
        current = self._download
        if (current is None or packet.resource_name != current.resource_name
                or packet.chunk_id != current.next_chunk or packet.byte_offset != current.offset):
            raise ResourcePackError("unexpected resource-pack chunk identity, index or offset")
        expected_size = min(current.chunk_size, current.size - current.offset)
        if len(packet.data) != expected_size:
            raise ResourcePackError("resource-pack chunk length does not match the announced size")
        current.data.write(packet.data)
        current.offset += expected_size
        current.next_chunk += 1
        if current.next_chunk < current.count:
            await self._request_chunk()
            return
        digest = hashlib.sha256(current.data.getbuffer()).digest()
        if digest != current.file_hash:
            raise ResourcePackError("resource-pack SHA-256 does not match the received archive")
        try:
            resources, unpacked_size, file_count = _load_archive(
                current.data, current.details, self._remaining_bytes, self._remaining_entries,
            )
        except ResourcePackError:
            raise
        except (BadZipFile, DeflateError, EOFError, KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            archive_error = type(error).__name__
        else:
            archive_error = None
        # Standard-library parse errors can retain directory and decompression
        # buffers in their traceback. Report the failure without chaining them.
        if archive_error is not None:
            raise ResourcePackError(f"unsupported or invalid resource-pack archive ({archive_error})")
        current.data.close()
        self._remaining_bytes -= unpacked_size
        self._remaining_entries -= file_count
        pack = current.details
        loaded = LoadedResourcePack(pack.pack_id, pack.version, current.size, digest.hex(), pack.subpack_name)
        self._loaded[(pack.pack_id, pack.version)] = loaded, resources
        self._download = None
        if self._queued:
            self._download = self._queued.popleft()
            await self._request_chunk()
        elif not self._pending:
            self._phase = "stack"
            await self._response(self._binding.ResourcePackClientResponsePacket.DownloadingFinished())

    async def _stack(self, packet):
        stack = packet.texture_pack_ids_and_versions
        if self._phase not in ("initial", "stack", "refused"):
            raise ResourcePackError("server sent a resource-pack stack before downloads completed")
        if self._phase == "refused" and (self._required or packet.texture_pack_required or stack):
            raise ResourcePackError("server still requires a resource-pack stack after refusal")
        self.plain_pack_stack = not any(toggle.enabled for toggle in packet.experiments.toggles)
        ordered = []
        for item in stack:
            key = (item.pack_id, item.version)
            loaded = self._loaded.get(key)
            if loaded is None:
                raise ResourcePackError("server stack contains an unloaded resource pack")
            if item.subpack_name != loaded[0].subpack_name:
                raise ResourcePackError("resource-pack stack subpack does not match the offer")
            ordered.append(loaded)
        self.index = ResourcePackIndex(
            tuple(pack for pack, _ in ordered),
            MappingProxyType({name: value for _, files in ordered for name, value in files.items()}),
        )
        self._phase = "complete"
        await self._response(self._binding.ResourcePackClientResponsePacket.ResourcePackStackFinished())

    def check_ready(self):
        if self._phase in ("download", "stack") or (self._phase == "refused" and self._required):
            raise ResourcePackError("server started the game before resource-pack negotiation completed")
