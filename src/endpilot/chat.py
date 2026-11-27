"""Player chat, server text observations and ordered regex matches."""

import asyncio
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import count
from uuid import UUID, uuid4

from bedrock_protocol import TextPacketType

from .authentication import _validate_locale
from .compression import PacketConnection
from .protocol import RawPacket
from .resource_packs import ResourcePackIndex


type ChatPatterns = str | re.Pattern[str] | Sequence[str | re.Pattern[str]]

_COMMAND_BREAKS = "\r\n\0"
_SELECTOR = r"@(?:initiator|[aenprs])(?![A-Za-z0-9])"
_RECIPIENT_SELECTOR = re.compile(_SELECTOR)
_MESSAGE_PARTS = re.compile(
    r'"(?:\\(?:.|$)|[^"\\])*"?'
    rf"|'{_SELECTOR}'|(?P<selector>(?<!\\){_SELECTOR})"
)
_TEXT_OBJECT_KINDS = (
    TextPacketType.TEXT_OBJECT, TextPacketType.TEXT_OBJECT_WHISPER,
    TextPacketType.TEXT_OBJECT_ANNOUNCEMENT,
)
_LITERAL_TEXT_KINDS = (TextPacketType.RAW, TextPacketType.SYSTEM_MESSAGE, TextPacketType.TIP)
_TRANSLATION_SLOT = re.compile(r"%%[0-9]*|%[A-Za-z0-9]")
_MAX_LANG_BYTES = 2 * 1024 * 1024
_MAX_RAWTEXT_CHARS = 128 * 1024
_MAX_RAWTEXT_DEPTH = 32


def _contains_message_selector(message: str) -> bool:
    # BDS shields double-quoted text, a single-quoted selector token, and
    # a selector prefixed with a backslash. Apostrophes in prose are literal.
    return any(part.lastgroup == "selector" for part in _MESSAGE_PARTS.finditer(message))


def _author_without_reset(author: str) -> str:
    # BDS appends a formatting reset to relayed player chat author names.
    return author.removesuffix("\u00a7r")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    author: str
    message: str


@dataclass(frozen=True, slots=True)
class TextEvent:
    """Text as delivered by the server, with the fields of its wire variant."""

    kind: TextPacketType | int
    message: str
    localize: bool
    author: str | None = None
    params: tuple[str, ...] | None = None
    xuid: str = ""
    platform_id: str = ""
    filtered_message: str | None = None

    @property
    def text_object(self) -> object | None:
        """Decode text-object JSON on demand, retaining the original message."""
        if self.kind in _TEXT_OBJECT_KINDS:
            return json.loads(self.message)
        return None

    def resolve_text(self, *, locale: str, resource_pack_index: ResourcePackIndex | None) -> str | None:
        """Resolve literal text and rawtext translations from a session's effective resource packs.

        Pass the index of the session that received this event. Translation uses
        the exact Bedrock locale and returns None if the full text is unavailable.
        """
        if self.kind in _TEXT_OBJECT_KINDS and not self.localize:
            return _resolve_rawtext(self.message, locale, resource_pack_index)
        return _text_content(self)


def _text_content(event: TextEvent, *, include_text_objects: bool = False) -> str | None:
    if event.localize:
        return None
    if event.kind == TextPacketType.CHAT and event.author is not None:
        return f"<{_author_without_reset(event.author)}> {event.message}"
    if event.kind in _LITERAL_TEXT_KINDS:
        return event.message
    if include_text_objects and event.kind in _TEXT_OBJECT_KINDS:
        return _literal_rawtext(event.message)
    return None


def _rawtext_object(pairs):
    node = dict(pairs)
    if len(node) != len(pairs):
        raise ValueError("duplicate rawtext member")
    return node


def _literal_rawtext(message: str) -> str | None:
    try:
        pending = [json.loads(message, object_pairs_hook=_rawtext_object)]
    except (ValueError, RecursionError):
        return None
    parts = []
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or len(node) != 1:
            return None
        if isinstance(text := node.get("text"), str):
            parts.append(text)
        elif isinstance(children := node.get("rawtext"), list):
            pending.extend(reversed(children))
        else:
            return None
    return "".join(parts)


def _read_translations(locale: str, index: ResourcePackIndex | None) -> dict[str, str]:
    _validate_locale(locale)
    if index is None:
        raise ValueError("resource-pack translations are unavailable")
    data = index.read(f"texts/{locale}.lang")
    if len(data) > _MAX_LANG_BYTES or data.startswith(b"\xef\xbb\xbf"):
        raise ValueError("unsupported language file")
    translations = {}
    for line in data.decode("utf-8").split("\n"):
        line = line.removesuffix("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key != key.strip() or key in translations:
            raise ValueError("invalid or duplicate language key")
        translations[key] = value.partition("\t#")[0]
    return translations


def _fill_translation(template: str, args: list[str], budget: int) -> str:
    parts = []
    numbered = None
    sequential_index = 0
    end = 0
    for match in _TRANSLATION_SLOT.finditer(template):
        slot = match.group()
        is_numbered = slot.startswith("%%")
        if numbered is not None and numbered != is_numbered:
            raise ValueError("mixed translation slot formats")
        numbered = is_numbered
        if slot == "%s":
            index = sequential_index
            sequential_index += 1
        elif is_numbered and len(slot) == 3 and slot[-1] in "123456789":
            index = int(slot[-1]) - 1
        else:
            raise ValueError("unsupported translation slot")
        if index >= len(args):
            raise ValueError("missing translation argument")
        value = args[index]
        budget -= match.start() - end + len(value)
        if budget < 0:
            raise ValueError("expanded translation exceeds the text limit")
        parts.extend((template[end:match.start()], value))
        end = match.end()
    if not numbered and sequential_index != len(args):
        raise ValueError("extra sequential translation argument")
    if len(template) - end > budget:
        raise ValueError("expanded translation exceeds the text limit")
    parts.append(template[end:])
    return "".join(parts)


def _resolve_rawtext(message: str, locale: str, index: ResourcePackIndex | None) -> str | None:
    if len(message) > _MAX_RAWTEXT_CHARS:
        return None
    translations = None
    # Count templates and intermediate strings so nested substitutions have
    # bounded work even when a slot is repeated or unused.
    budget = _MAX_RAWTEXT_CHARS

    def spend(characters: int) -> None:
        nonlocal budget
        budget -= characters
        if budget < 0:
            raise ValueError("rawtext exceeds the text limit")

    def resolve(node: object, depth: int) -> str:
        nonlocal translations
        if depth > _MAX_RAWTEXT_DEPTH or not isinstance(node, dict):
            raise ValueError("invalid rawtext node")
        if node.keys() == {"text"} and isinstance(value := node["text"], str):
            spend(len(value))
            return value
        if node.keys() == {"rawtext"} and isinstance(children := node["rawtext"], list):
            return "".join(resolve(child, depth + 1) for child in children)
        if node.keys() not in ({"translate"}, {"translate", "with"}) or not isinstance(
            key := node["translate"], str,
        ):
            raise ValueError("unresolved rawtext node")
        if translations is None:
            translations = _read_translations(locale, index)
        template = translations[key]
        spend(len(template))
        with_value = node.get("with", [])
        if isinstance(with_value, list) and all(isinstance(arg, str) for arg in with_value):
            args = with_value
        elif (isinstance(with_value, dict) and with_value.keys() == {"rawtext"}
              and isinstance(children := with_value["rawtext"], list)):
            args = [resolve(child, depth + 1) for child in children]
        else:
            raise ValueError("invalid translate arguments")
        value = _fill_translation(template, args, budget)
        spend(len(value))
        return value

    try:
        return resolve(json.loads(message, object_pairs_hook=_rawtext_object), 0)
    except (KeyError, ValueError, RecursionError):
        return None


@dataclass(frozen=True, slots=True)
class ChatMatch:
    """The messages and regex matches in the order of a completed chat pattern."""

    pattern_id: int
    name: str
    messages: tuple[ChatMessage, ...]
    matches: tuple[re.Match[str], ...]


@dataclass(frozen=True, slots=True)
class DisplayMatch:
    """The source events and regex captures for a completed display pattern."""

    pattern_id: int
    name: str
    events: tuple[TextEvent, ...]
    matches: tuple[re.Match[str], ...]


@dataclass(slots=True)
class _IncomingText:
    """Share one packet's content across display patterns and content waits."""

    event: TextEvent
    resource_pack_index: ResourcePackIndex | None
    _contents: dict[str | None, str | None] = field(default_factory=dict)

    def content(self, locale: str | None, *, include_text_objects: bool = True) -> str | None:
        if locale is None and not include_text_objects:
            return _text_content(self.event)
        if locale not in self._contents:
            self._contents[locale] = (
                _text_content(self.event, include_text_objects=True) if locale is None
                else self.event.resolve_text(locale=locale, resource_pack_index=self.resource_pack_index)
            )
        return self._contents[locale]


@dataclass(slots=True)
class _RegisteredPattern[T]:
    name: str
    patterns: tuple[re.Pattern[str], ...]
    repeat: bool
    locale: str | None = None
    events: list[T] = field(default_factory=list)
    matches: list[re.Match[str]] = field(default_factory=list)


class Chat:
    def __init__(
        self, packets, name: str, *, resource_pack_index: ResourcePackIndex | None = None, _xuid: str = "",
    ):
        self.packets = packets
        self.name = name
        self._xuid = _xuid
        self._resource_pack_index = resource_pack_index
        self._text = packets.protocol.binding.packets[9]
        self._pattern_ids = count(1)
        self._patterns: dict[int, _RegisteredPattern[ChatMessage]] = {}
        self._display_patterns: dict[int, _RegisteredPattern[TextEvent]] = {}
        self._completed: list[ChatMatch] = []
        self._display_completed: list[DisplayMatch] = []
        self._subscriptions_closed = False
        self._message_observers: list[Callable[[ChatMessage], None]] = []
        self._text_observers: list[Callable[[_IncomingText], None]] = []
        self._match_observers: list[Callable[[ChatMatch], None]] = []
        self._content_waits: set[asyncio.Future[str]] = set()
        # A disconnect can precede the first subscription while the reader is still running.
        self._unsubscribe = packets.subscribe_raw(self._on_packet)
        self._unsubscribe_reader_end = packets.subscribe_reader_end(self._end_subscriptions)

    async def send(self, message: str) -> None:
        """Send a chat packet; this does not confirm BDS relayed it."""
        text = self._text
        packet = text(
            localize=False,
            body=text.AuthorAndMessage(type=TextPacketType.CHAT, author=self.name, message=message),
            xuid=self._xuid,
            platform_id="",
            filtered_message=None,
        )
        await self.packets.send(packet)

    async def send_whisper(
        self, recipient: str, message: str, *, player_uuid: UUID, player_unique_id: int,
    ) -> None:
        if (
            not recipient.strip(" \t") or any(char in recipient for char in _COMMAND_BREAKS)
            or _RECIPIENT_SELECTOR.match(recipient.lstrip(" \t"))
        ):
            raise ValueError("whisper recipient must be a literal player name")
        if not message.strip(" ") or any(char in message for char in _COMMAND_BREAKS):
            raise ValueError("whisper text must be a nonempty line")
        if _contains_message_selector(message):
            raise ValueError("whisper text contains an unquoted Bedrock selector")

        target = json.dumps(recipient, ensure_ascii=False)
        binding = self.packets.protocol.binding
        origin = binding.CommandOriginData(
            type=binding.CommandOriginType.PLAYER, uuid=player_uuid,
            request_id=uuid4().hex, player_id=player_unique_id,
        )
        await self.packets.send(binding.CommandRequestPacket(
            command=f"/tell {target} {message}", origin=origin,
            internal_source=False, version=binding.CurrentCmdVersion.LATEST,
        ))

    async def wait_for_message(self, *, author: str | None = None, timeout: float = 15) -> ChatMessage:
        """Wait for the next server-relayed player chat; ignore system text and other authors."""
        received = None

        def matches(raw: RawPacket) -> bool:
            nonlocal received
            message = self._read_message(raw)
            if message is None or (author is not None and message.author != author):
                return False
            received = message
            return True

        await self.packets.wait_for_packet(self._text.ID, match=matches, timeout=timeout)
        assert received is not None
        return received

    async def wait_for_text(
        self, *, kind: TextPacketType | None = None, author: str | None = None,
        message: str | re.Pattern[str] | None = None, timeout: float = 15,
    ) -> TextEvent:
        if message is not None and not (
            isinstance(message, str) or isinstance(message, re.Pattern) and isinstance(message.pattern, str)
        ):
            raise TypeError("message must be a string, a compiled string regex, or None")

        received = None

        def matches(raw: RawPacket) -> bool:
            nonlocal received
            packet = self.packets.protocol.decode(raw)
            body = packet.body
            source = body.author if isinstance(body, self._text.AuthorAndMessage) else None
            if not self._matches_text(body.type, source, kind, author):
                return False
            if isinstance(message, str) and body.message != message:
                return False
            if isinstance(message, re.Pattern) and message.search(body.message) is None:
                return False
            received = self._text_event(packet)
            return True

        await self.packets.wait_for_packet(self._text.ID, match=matches, timeout=timeout)
        assert received is not None
        return received

    async def wait_for_content(
        self, *matchers: str | re.Pattern[str] | Sequence[str | re.Pattern[str]], timeout: float = 15,
        include_text_objects: bool = False, locale: str | None = None,
    ) -> str:
        """Return the first matching text, resolving pack translations for an explicit locale."""
        if locale is not None:
            _validate_locale(locale)
        alternatives = tuple(
            item for matcher in matchers
            for item in (matcher if isinstance(matcher, Sequence)
                         and not isinstance(matcher, (str, bytes, bytearray)) else (matcher,))
        )
        if not alternatives:
            raise ValueError("provide at least one text matcher")
        if any(not (isinstance(item, str)
                    or isinstance(item, re.Pattern) and isinstance(item.pattern, str))
               for item in alternatives):
            raise TypeError("text matchers must be strings or compiled string regexes")

        self._require_subscription_stream()
        received = asyncio.get_running_loop().create_future()

        def observe(incoming: _IncomingText) -> None:
            if received.done():
                return
            content = incoming.content(locale, include_text_objects=include_text_objects)
            if content is not None and any(
                content == item if isinstance(item, str) else item.search(content)
                for item in alternatives
            ):
                unsubscribe()
                self._content_waits.discard(received)
                received.set_result(content)

        unsubscribe = PacketConnection._subscribe(self._text_observers, observe)
        self._content_waits.add(received)
        try:
            async with asyncio.timeout(timeout):
                return await received
        finally:
            unsubscribe()
            self._content_waits.discard(received)

    def add_pattern(self, name: str, pattern: ChatPatterns, *, repeat: bool = True) -> int:
        return self._add_pattern(self._patterns, name, pattern, repeat=repeat)

    def add_display_pattern(
        self, name: str, pattern: ChatPatterns, *, repeat: bool = True, locale: str | None = None,
    ) -> int:
        if locale is not None:
            _validate_locale(locale)
        self._require_subscription_stream()
        return self._add_pattern(self._display_patterns, name, pattern, repeat=repeat, locale=locale)

    def _add_pattern[T](
        self, registrations: dict[int, _RegisteredPattern[T]], name: str, pattern: ChatPatterns, *, repeat: bool,
        locale: str | None = None,
    ) -> int:
        if isinstance(pattern, (str, re.Pattern)):
            pattern = (pattern,)
        elif not isinstance(pattern, Sequence):
            raise TypeError("expected a regex or an ordered sequence of regexes")
        patterns = tuple(re.compile(regex) for regex in pattern)
        if not patterns:
            raise ValueError("chat pattern sequence must not be empty")
        if any(not isinstance(regex.pattern, str) for regex in patterns):
            raise TypeError("chat patterns must match strings")
        pattern_id = next(self._pattern_ids)
        registrations[pattern_id] = _RegisteredPattern(name, patterns, repeat, locale=locale)
        return pattern_id

    def remove_pattern(self, name_or_id: str | int) -> int:
        return self._remove_pattern(self._patterns, name_or_id)

    def remove_display_pattern(self, name_or_id: str | int) -> int:
        return self._remove_pattern(self._display_patterns, name_or_id)

    @staticmethod
    def _remove_pattern[T](registrations: dict[int, _RegisteredPattern[T]], name_or_id: str | int) -> int:
        if isinstance(name_or_id, int):
            return int(registrations.pop(name_or_id, None) is not None)
        matching_ids = [pattern_id for pattern_id, pattern in registrations.items()
                        if pattern.name == name_or_id]
        for pattern_id in matching_ids:
            del registrations[pattern_id]
        return len(matching_ids)

    async def wait_for_match(self, name_or_id: str | int, *, timeout: float = 15) -> ChatMatch:
        self._require_pattern(self._patterns, name_or_id)
        return await self._wait_for_match(name_or_id, self._completed, timeout=timeout)

    async def wait_for_display_match(self, name_or_id: str | int, *, timeout: float = 15) -> DisplayMatch:
        self._require_subscription_stream()
        self._require_pattern(self._display_patterns, name_or_id)
        return await self._wait_for_match(name_or_id, self._display_completed, timeout=timeout, end_on_disconnect=True)

    async def _wait_for_match[M: (ChatMatch, DisplayMatch)](
        self, name_or_id: str | int, completions: list[M], *, timeout: float, end_on_disconnect: bool = False,
    ) -> M:
        received = None
        disconnect = self.packets.protocol.binding.DisconnectPacket.ID if end_on_disconnect else None

        def matches(raw: RawPacket) -> bool:
            nonlocal received
            if raw.packet_id == disconnect:
                raise ConnectionError("chat subscription stream closed while waiting for a display match")
            # The registered pattern observer runs before this packet waiter.
            for completed in completions:
                if name_or_id in (completed.pattern_id, completed.name):
                    received = completed
                    return True
            return False

        packet_ids = self._text.ID if disconnect is None else (self._text.ID, disconnect)
        await self.packets.wait_for_packet(packet_ids, match=matches, timeout=timeout)
        assert received is not None
        return received

    def subscribe_message(
        self, callback: Callable[[ChatMessage], None], *, author: str | None = None,
    ) -> Callable[[], None]:
        self._require_subscription_stream()
        return self._subscribe(self._message_observers, callback, lambda event: author is None or event.author == author)

    def subscribe_text(
        self, callback: Callable[[TextEvent], None], *, kind: TextPacketType | None = None,
        author: str | None = None,
    ) -> Callable[[], None]:
        self._require_subscription_stream()

        def observe(incoming: _IncomingText) -> None:
            event = incoming.event
            if self._matches_text(event.kind, event.author, kind, author):
                callback(event)

        return PacketConnection._subscribe(self._text_observers, observe)

    def subscribe_match(
        self, name_or_id: str | int, callback: Callable[[ChatMatch], None],
    ) -> Callable[[], None]:
        self._require_subscription_stream()
        self._require_pattern(self._patterns, name_or_id)
        return self._subscribe(self._match_observers, callback,
                               lambda event: name_or_id in (event.pattern_id, event.name))

    @staticmethod
    def _require_pattern[T](registrations: dict[int, _RegisteredPattern[T]], name_or_id: str | int) -> None:
        if isinstance(name_or_id, int):
            registered = name_or_id in registrations
        else:
            registered = any(pattern.name == name_or_id for pattern in registrations.values())
        if not registered:
            raise KeyError(name_or_id)

    def _require_subscription_stream(self) -> None:
        reader = self.packets._reader_task
        if self._subscriptions_closed or (reader is not None and reader.done()):
            raise ConnectionError("chat subscription stream is closed")

    @staticmethod
    def _subscribe[T](
        observers: list[Callable[[T], None]], callback: Callable[[T], None], matches: Callable[[T], bool],
    ) -> Callable[[], None]:
        def observe(event: T) -> None:
            if matches(event):
                callback(event)

        return PacketConnection._subscribe(observers, observe)

    def _end_subscriptions(self, _cause=None) -> None:
        self._subscriptions_closed = True
        if self._unsubscribe_reader_end is not None:
            self._unsubscribe_reader_end()
            self._unsubscribe_reader_end = None
        self._message_observers.clear()
        self._text_observers.clear()
        self._match_observers.clear()
        self._display_patterns.clear()
        self._display_completed.clear()
        for waiting in self._content_waits:
            if not waiting.done():
                waiting.set_exception(ConnectionError("chat subscription stream closed while waiting for text"))
        self._content_waits.clear()

    def close(self) -> None:
        self._end_subscriptions()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._patterns.clear()
        self._completed.clear()

    def _read_message(self, raw: RawPacket) -> ChatMessage | None:
        return self._chat_message(self.packets.protocol.decode(raw).body)

    def _chat_message(self, body) -> ChatMessage | None:
        if isinstance(body, self._text.AuthorAndMessage) and body.type == TextPacketType.CHAT:
            return ChatMessage(_author_without_reset(body.author), body.message)
        return None

    def _text_event(self, packet) -> TextEvent:
        body = packet.body
        author = body.author if isinstance(body, self._text.AuthorAndMessage) else None
        params = tuple(body.params) if isinstance(body, self._text.MessageAndParams) else None
        return TextEvent(kind=body.type, message=body.message, localize=packet.localize, author=author, params=params,
                         xuid=packet.xuid, platform_id=packet.platform_id, filtered_message=packet.filtered_message)

    @staticmethod
    def _matches_text(
        text_kind: TextPacketType | int, source: str | None, kind: TextPacketType | None, author: str | None,
    ) -> bool:
        return ((kind is None or text_kind == kind)
                and (author is None or (source is not None and _author_without_reset(source) == author)))

    @staticmethod
    def _notify[T](observers: list[Callable[[T], None]], event: T) -> None:
        for callback in tuple(observers):
            try:
                callback(event)
            except (Exception, asyncio.CancelledError) as error:
                asyncio.get_running_loop().call_exception_handler({
                    "message": "chat observer failed", "exception": error, "callback": callback,
                })

    def _on_packet(self, raw: RawPacket) -> None:
        if raw.packet_id == self.packets.protocol.binding.DisconnectPacket.ID:
            self._end_subscriptions()
            return
        if raw.packet_id != self._text.ID:
            return
        self._completed.clear()
        self._display_completed.clear()
        if not (self._patterns or self._display_patterns or self._text_observers or self._message_observers):
            return
        packet = self.packets.protocol.decode(raw)
        message = self._chat_message(packet.body)
        if message is not None and self._patterns:
            self._advance_patterns(self._patterns, self._completed, message, lambda _: message.message, ChatMatch)
        if self._display_patterns or self._text_observers:
            incoming = _IncomingText(self._text_event(packet), self._resource_pack_index)
            self._advance_patterns(
                self._display_patterns, self._display_completed, incoming.event, incoming.content, DisplayMatch,
            )
            self._notify(self._text_observers, incoming)
        if message is not None:
            self._notify(self._message_observers, message)
        for completed in self._completed:
            self._notify(self._match_observers, completed)

    @staticmethod
    def _advance_patterns[T, M](
        registrations: dict[int, _RegisteredPattern[T]], completions: list[M], event: T,
        content_for: Callable[[str | None], str | None],
        make_match: Callable[[int, str, tuple[T, ...], tuple[re.Match[str], ...]], M],
    ) -> None:
        for pattern_id, pattern in tuple(registrations.items()):
            content = content_for(pattern.locale)
            if content is None:
                continue
            match = pattern.patterns[len(pattern.matches)].search(content)
            if match is None:
                continue
            pattern.events.append(event)
            pattern.matches.append(match)
            if len(pattern.matches) == len(pattern.patterns):
                completions.append(make_match(
                    pattern_id, pattern.name, tuple(pattern.events), tuple(pattern.matches),
                ))
                if pattern.repeat:
                    pattern.events.clear()
                    pattern.matches.clear()
                else:
                    del registrations[pattern_id]
