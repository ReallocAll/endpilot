# Chat, display, and forms

## Match player chat

On a connected bot, register a regex or an ordered sequence of regexes and await
its next completion. Each regex searches the text of a server-relayed player
message. Unrelated messages can arrive between steps.

```python
pattern_id = bot.add_chat_pattern(
    "quest", (r"^Quest (?P<name>.+) started$", r"^Reward: (\d+) coins$"),
    repeat=False,
)
result = await bot.wait_for_chat_match(pattern_id, timeout=30)
print(result.matches[0].group("name"), result.matches[1].group(1))
```

`result.messages` contains the corresponding player messages in order. Start the
wait before the final message arrives. Registrations repeat by default. Use
`remove_chat_pattern(pattern_id)` to remove one registration or pass its name to
remove all registrations with that name. For chat observations by author, use
`wait_for_chat(author=...)` as in the [two-player example](../../examples/chat.py).

## Subscribe to chat entries

Use `bot.subscribe_chat(callback, author=...)` to observe new chat entries on
the current connection. The optional author filter matches the available
sender name. The callback runs synchronously in receive order. Call the
returned function to unsubscribe or register again after reconnecting.

`ChatMessage.kind` identifies the entry as `chat`, `whisper`, `join`, `leave`,
`death`, `server`, or `command`. A public player chat keeps its separate `author` and
verbatim message body. A received whisper also carries its author and body.
An entry whose sender is unavailable has `author=None`. Repeated messages
are delivered separately. Tips, popups, jukebox notices, titles, subtitles,
and action bar updates are outside this stream. A death announcement comes
from a received chat text packet.

Translation entries preserve `translation_key` and `params`. Load local
base language bytes using `Bot(chat_languages={"en_US": lang_bytes})`. The bot
selects the locale of each login and applies the effective `.lang` file from
that connection's resource pack stack. When a translation cannot be resolved,
the callback still receives the entry with `message=None`. Compound rawtext
keeps its original JSON in `raw_message`, which also carries original literal
command output where available.
Use `subscribe_text()` for the original wire fields, including text objects.

On protocol 2225, a player command response addressed to this session can
contribute one chat entry per message in its received order. An empty message
list contributes zero entries. The typed `execute_command_output()` method
also returns the original response. A submitted command can still produce chat
entries after its caller cancels or times out. This uses the current connection's
command UUIDs for correlation. The `/list` count and player names are two
separate entries. Localized output keeps the original key and parameters.
For example, `/help` preserves the green formatting of the page header and
footer. Native command output carries `successful` even when a translation is
unavailable. Unsuccessful rendered output uses the red `§c` formatting prefix.
Events without a native result flag have `successful=None`.

## Match display text

Use `add_display_pattern()` to match incoming player chat formatted as
`<author> message`, literal raw text, system messages, tips, and supported
text objects made entirely of literal text by default.
Start the wait before the message arrives. Its `events` are the original
`TextEvent` values, and `matches` contains the regex captures.

```python
pattern_id = bot.add_display_pattern("checkpoint", r"^Checkpoint (\d+) saved$", repeat=False)
result = await bot.wait_for_display_match(pattern_id, timeout=30)
print(result.matches[0].group(1))
```

On a test server, trigger the match by sending this from the server console
while the bot named `Knight` is waiting.

```text
tellraw Knight {"rawtext":[{"text":"Checkpoint 3 saved"}]}
```

For repeated matches, leave `repeat=True` when registering the pattern and use
`subscribe_display_match(pattern_id, callback)`. The synchronous callback
receives a `DisplayMatch` for each completion, including multiple completions in
one packet batch. Pass a pattern name to listen to all registrations of that
name in registration order. Call the returned function to unsubscribe. The
subscription ends with the current connection.

Pass an exact Bedrock locale such as `locale="fr_FR"` to
`add_display_pattern()` or `wait_for_message()` to include text objects whose
translations resolve in full using the resource packs loaded by this connection.
If a locale or translation key is unavailable, matching skips the whole object.
Each wait or registration can select its own locale. For literal objects only,
use `wait_for_message(..., include_text_objects=True)` or the default display
pattern behavior.

Pass `translations=` to either API to match the full text rendered by an
explicit `TextTranslations` instance. This includes supported native
translation packets, rawtext, and ordinary chat. Each wait or pattern uses
the supplied instance for its lifetime on the current connection. Passing
both `locale` and `translations` raises `ValueError`. A supplied resolver
includes its supported text objects automatically.

## Observe server text and send private chat

`await bot.wait_for_text(kind=TextPacketType.WHISPER, author="Player One")`
waits for the next matching `TextPacket` dispatched after the wait starts.
Both filters are optional. Import `TextPacketType` from `endpilot` and use
the packet kind to select player chat, whispers, announcements, system text,
translations, tips, or text-object variants. `wait_for_chat()` and chat
patterns continue to match player chat. A wait raises `TimeoutError` at its
deadline or `ConnectionError` when the packet stream ends first.

A `TextEvent` contains the original `message`, the wire `kind` and `localize`
flag, and any `author`, `params`, and `filtered_message` fields. It also carries
the packet's `xuid` and `platform_id`. The `author` filter accepts names without
the trailing Bedrock formatting reset and preserves the raw source in the
returned event. Text-object variants expose decoded JSON through `text_object`.
Accessing it can raise `json.JSONDecodeError` for a malformed text object.
Translation keys and arguments are preserved for the client's locale. Command
feedback can arrive as a text object whose source field is absent.

Use `TextTranslations.from_lang(locale="fr_FR", vanilla_lang=lang_bytes,
resource_pack_index=index)` to resolve native translation packets and rawtext
using a locally supplied Bedrock `.lang` file. Pass the effective `index`
from the session that received the event, or omit it to use the base file.
The resolver parses both files once and can resolve each subsequent
`TextEvent` with `translations.resolve(event)`. No language files are
downloaded or included with the SDK. Resolution preserves the event fields
and sends no packets.

For non-localized `ANNOUNCEMENT` and `WHISPER` packets, `resolve()` returns
the received `message` verbatim. This includes any prefix already in the
message and preserves the packet's separate `author` field on the event.

A valid pack file overrides base keys, including empty values. Keys absent
from that file use the supplied base for the same locale. An invalid effective
file leaves translations unavailable. This resolver accepts a UTF-8 BOM and
spaces or tabs before `=` in both files. It skips malformed and ambiguous
entries in the base file. The resolver supports `%s`, `%N$s`, and numbered
decimal templates such as `%1$d`, as well as `%%N` in rawtext. It preserves
formatting on decorated server keys such as `§e%` and `§2%`. A missing key,
argument, or unsupported node leaves the entire message unresolved. `TextEvent.resolve_text()` still
uses its resource-pack-only rules. `wait_for_text()` continues to match the
original message field.

`await bot.send_whisper("Player One", "See you soon")` submits a `/tell` command
to a single quoted player name, including names with spaces or punctuation.
The message body is submitted verbatim. Emails such as `user@example.com`,
literal `@`, Unicode, and existing quotes and backslashes are supported. Empty
inputs, CR, LF, NUL, selector recipients, and active selectors in the body raise
`ValueError` before submission. BDS can interpret a selector inside other
text, as with `operator@a.io`, and can normalize message spacing.
The receiver's `WHISPER` confirms delivery on that connection. The sender can
observe the server's text-object echo separately.

## Observe titles and action bar text

`await bot.wait_for_title(timeout=15)` returns the first title event dispatched
after the wait begins. Pass `kind="title"`, `"subtitle"`, `"actionbar"`,
`"times"`, `"clear"`, or `"reset"` to select an event. Start the wait before
triggering the server action. The event records receipt of a packet by the bot.
A deadline raises `TimeoutError` and a stream that ends first raises `ConnectionError`.

Use `bot.subscribe_titles(callback, kind="actionbar")` to observe a sequence of
title events. Omit `kind` to observe all six kinds. The synchronous callback
receives an immutable `TitleEvent` for each matching packet dispatched after
registration, in wire order. Call the returned function to unsubscribe immediately,
including during delivery. Subscriptions end when the bot closes, the server
sends a disconnect packet, or the packet reader stops.

Title, subtitle, and action bar events carry `text` and an optional
server-supplied `filtered_text`.
`text_object=True` marks a Bedrock text-object string, preserved as supplied by
the server. The `kind` filter matches both plain text and text objects for the
selected title kind. `times` events carry `fade_in_ticks`, `stay_ticks`, and
`fade_out_ticks`. Text and timing fields that do not apply are `None`.

Call `event.resolve_text(locale="fr_FR", resource_pack_index=index)` to
resolve the event's text. Keep `index = bot.resource_pack_index` from the
session that received the event. Plain text is returned verbatim, including
an empty string. Text objects use the requested locale and the supplied
index. A missing translation, unsupported node, or non-text event returns
`None`. Resolution preserves `text` and `filtered_text` and sends no packets.

Run `python examples/observe_title.py URL --protocol 2225 --kind subtitle`
against a compatible offline server, then send `title Knight subtitle Example`
from the server console after the example prints `title_watcher_ready`.
[Title example](../../examples/observe_title.py)

Use `bot.wait_for_title(kind="actionbar")` for the native title actionbar packet.
Use `bot.subscribe_text(callback, kind=TextPacketType.TIP)` or
`bot.wait_for_text(kind=TextPacketType.TIP)` for tips carried by text packets.

## Observe boss bars

`bot.boss_bars` returns immutable snapshots of the bars visible to that bot,
including bars sent during join. Each `BossBar` has a Bedrock actor unique ID
(`boss_id`), `title`, optional `filtered_title`, `progress`, `color`, and `style`.
Progress is the server's fraction, and `color` and `style` are the numeric Bedrock
`BossBarColor` and `BossBarOverlay` values. Bedrock protocol 2225 omits sky, fog,
and dragon-bar flag fields.

`await bot.wait_for_boss_bar(kind="created", timeout=15)` returns the next
matching `BossBarEvent` after the wait starts. Filter by `kind` (also `"updated"`
or `"removed"`) and optionally `boss_id`. A removal includes the bar's last
snapshot, and a repeated add for the same visible ID refreshes it as an update.
Each connection starts with its own bar state. Snapshots previously returned to
you retain their values across updates, removal, and reconnects.

## Read the scoreboard

`bot.scoreboard` returns a fresh snapshot of the objectives, display slots, scores,
and player identity mappings received in the current session. Keep a snapshot to
compare it with a later one. `scores_in_slot()` applies that slot's server-supplied
sort order.

```python
board = bot.scoreboard
for score in board.scores_in_slot("sidebar"):
    print(score.kind, score.scoreboard_id, score.value)
```

Score entries carry the player or entity unique ID or the fake player name sent
with the score. Separate player identity updates are available in
`board.player_identities`, keyed by scoreboard ID. An objective seen only in score
packets has `None` for its display name and criteria.

`unsubscribe = bot.subscribe_scoreboard(callback)` receives changes from packets
dispatched after registration. The synchronous callback receives one immutable
tuple per packet that changes the scoreboard. Each change contains `before` and
`after` values and identifies the objective, display slot, score, or identity.
`None` marks an absent value. Changes follow wire order, so a score updated and
then removed in the same packet appears twice. Objective removal includes the
removed objective's scores and any affected display slot changes. A batch goes
to the observers registered when delivery starts. Subscribing or calling
`unsubscribe()` during a callback changes delivery starting with the next batch.
A subscription ends when the bot closes.

## Answer player forms

`bot.get_pending_form()` reads the most recently received pending form. Pass a
`form_id` to look up a specific request. The result contains `form_id` and an
independent copy of `form`, the server's JSON. If the request is absent, the
result is `None`.

Use `await bot.answer_form(response, form_id)` with an action form's zero-based
button index, a boolean for a confirmation form, or one value per control for a
custom form. A custom form encodes labels, headers and dividers as `null`.
`await bot.close_form(form_id=form_id)` sends `USER_CLOSED`. Pass `"USER_BUSY"`
as `reason` to report a busy player. Both calls default to the most recent
pending form when the ID is omitted.

Successful calls submit a response to the transport and remove that pending
request. Use the server's form callback to confirm that it handled the answer.
A server close-form packet clears all pending forms. Pending forms belong to
one connection. An extension watching the public packet stream can pass the
received `RawPacket` to select that exact request even when the server later
reuses the form ID. [Testing forms](testing.md#observe-and-answer-forms)
