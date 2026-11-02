# Using Endpilot

`Runtime` owns the bots and their asynchronous work. Enter it on the caller's
asyncio loop, use bot methods on that same loop, and await context exit to close
connections. Only `Runtime.request_stop()` is thread-safe. Windows callers must
use `asyncio.SelectorEventLoop`; see the [connection example](../README.md#quick-start).

## Observations and references

Gameplay captures, events, operation results, nested collections and native buffers are
immutable snapshots. Retained children and iterators keep their storage alive
across updates, reconnect and runtime destruction. `copy()` detaches a selected
subtree. `to_dict()`, `to_list()` and binary `to_bytes()` explicitly materialize
values; `wire_bytes()` re-encodes them and does not promise the original wire bytes.

Dictionary output retains domain enums and opaque `BotToken` identity, converts
protocol enums and numeric ID wrappers to integers, and preserves binary values
as bytes. NBT retains tag kinds, including the element type of an empty list.

A reconnect preserves the Bot handle and token but invalidates session, entity,
window, offer, slot and request references from the previous connection. Retained
snapshots remain readable; old references cannot authorize new operations.

## Inventory and trading

Player slots are `bot.player_slot(0..35)`; trade inputs are
`bot.trade_input(window, 0..1)`. `get_item()` distinguishes an unknown slot, a known
empty slot and an occupied item with an unresolved registry identity. Missing
counts and stack IDs remain `None`. Complete inventory totals require all 36
player slots and unambiguous item identities. Trade recipe item names are
resolved through the observed server Item Registry rather than a built-in
item-name allowlist; unique non-component-based identifiers remain required.

`get_trade_offers(window)` returns `None` until trade data arrives, an empty
collection for an empty Recipes list, and rows with reasons for unsupported or
malformed recipes. A malformed top-level Recipes value raises. Offer references
carry the window generation and revision; refreshed trade data invalidates old
references for new requests.

Use `begin_move_item(source, destination, count)` or
`begin_trade(offer.ref, destination)` to retain an operation, then await
`operation.wait(timeout)`. The corresponding `request_*` methods return a request
key. Only one item request may be pending.

Admission checks ownership, current references, known item identities, confirmed
stack IDs, available counts and destination compatibility. Failed validation
consumes no request ID. Accepted requests use -1, -3, -5, and so on per connection,
including explicitly cancelled queued requests. Admission records intent without
changing observed inventory.

An operation becomes `submitted` immediately before sending can have a side
effect. Successful responses settle validated changes atomically. Only updates
observed after submission can substitute for omitted touched-slot responses.
Rejection preserves known state; an indeterminate result invalidates only touched
slots. Results are immutable and published before item-request callbacks.

A wait timeout or waiter cancellation leaves the operation tracked. Explicit
cancellation succeeds only while queued. Encoding or send failure ends the
session. Failure or cancellation after submission leaves the outcome indeterminate:
**do not automatically retry an indeterminate transaction**.

Queued hotbar selections survive position corrections and precede subsequent
interactions. Death, dimension changes and disconnect invalidate queued selections.

## Typed packet sending

`bot.send_packet(packet)` accepts a native typed packet from the configured
`bedrock_protocol.v1001` or `v2193` namespace after the bot is Ready.
For example, a third-party extension can submit a packet without accessing
private Session methods:

```python
from bedrock_protocol import v2193

bot.send_packet(v2193.NetworkStackLatencyPacket(create_time=42, from_server=False))
```

The packet is encoded on admission and uses the same bounded, ordered Session
queue, compression and encryption as built-in operations. Successful return
means **queued**, not delivered or acknowledged. Invalid packet objects,
unsupported encodings, phase errors and full queues raise to the caller without
failing an otherwise healthy session. Actual asynchronous encoding, transport
or session failures still close the session.

For a packet ID *unknown to the selected protocol schema*, the same API also
accepts `bedrock_protocol.UnknownPacket(packet_id=id, body=raw_body)`: this
supplies an opaque packet **body**, not raw encrypted or compressed transport
bytes. It follows normal ordered framing, compression, encryption and queue
limits. IDs already defined by the protocol must use their typed packet class;
the native codec rejects raw bodies for known IDs. Servers may reject unknown
packet IDs, so application code remains responsible for compatibility and
session outcomes. This is not a wire-injection API.

Use the packet namespace matching `BotOptions.protocol`. This low-level API
does not update high-level movement, inventory, transaction or probe tracking;
callers must handle the protocol response and state consequences themselves.
See [a standalone extension example](../examples/typed_packet.py).

## Movement, spawning and respawn

Player observations retain velocity, rotation and links. Remote player movement
updates position and rotation and emits `actor_motion`; retained captures do not
change.

The initial chunk-radius request is 16. After spawn, another request is sent only
when `BotOptions.chunk_radius` differs. `load_screen` defaults to `True`;
`False` omits initial and dimension loading-screen messages while retaining
dimension acknowledgements and server-confirmed respawn behavior.

`bot.respawn(*, retry=False)` starts a respawn while dead. Repeated default calls
are suppressed while pending. `retry=True` resends the current stage: readiness
before the server's `READY_TO_SPAWN`, or the respawn action afterward. The
server-chosen position is preserved; success waits for positive health. Calls
while alive raise. The caller supplies retry timing and limits.

Before `StartGame` identifies the local player, up to 4096 compact position and
health effects are retained in receipt order. Matching effects apply when the
identity becomes known, including teleport acknowledgements and death state.
Early health events may therefore accompany `StartGame`. Remote actor events
still arrive normally; packet observations and counts are not replayed. Overflow
ends the session.

## Events and decode errors

`runtime.subscribe(callback, events=mask)` registers independent synchronous
observers and returns an idempotent unsubscribe function. Each observer receives
`(bot, event)`, is filtered by its own mask, and does not need to share the
legacy `Runtime(callback=...)` handler. A failing observer reports its exception
to the owner loop's `call_exception_handler` and does not stop the Bot or prevent
other observers receiving the event. This deliberately differs from the legacy
callback's fail-fast behavior. Exceptions inheriting directly from `BaseException`
retain fail-fast semantics.

Registration, unsubscription and event delivery run on the Runtime owner loop.
Unsubscribing during a dispatch prevents subsequent delivery to that observer.
Only synchronous observers are supported: a returned awaitable is rejected and
reported as an observer error. For asynchronous processing, an observer may
place immutable events into an application-owned bounded `asyncio.Queue` and
manage its own consumer task and overflow policy; EndPilot does not create a
background Task per packet or own that consumer's lifecycle.


`StateEvent.disconnect` is `ServerDisconnectReason | None`. A server Disconnect
packet supplies its numeric `reason_code`, optional `message` and optional
`filtered_message`, retained through closing and failed events.
`StateEvent.reason` prefers a nonblank message, then the filtered message, then
the numeric code. Localization keys are not translated. Transport drops have
`disconnect=None`.

Subscribe to `EventKind.decode_error` for `DecodeErrorEvent`.
`packet_id == -1` means a fatal batch error had no packet identifier. Fatal errors
have `recoverable=False`, increment `Statistics.decode_errors` and close the
session. Python business-validation failures are also fatal.

`BotOptions.validate_all_packets=True` decodes every incoming packet; the default
selects packets needed for gameplay state. `recover_packet_errors=True` reports
and skips isolated malformed packet bodies in wire order. Each error increments
`decode_errors` but contributes no play count. Batch framing, headers, compression,
checksum, resource limits and unexpected decoder errors remain fatal. Recovery
defaults to `False`. Unknown IDs are successful opaque packets; all-packet decoding
includes their play counts, while default selection skips them.

`EventKind.packet_decoded` is opt-in and excluded from `DEFAULT_EVENTS`.
`PacketDecodedEvent(packet)` observes each successful selected native decode after
its play histogram increment and before subclient filtering or gameplay handling.
Recovered body errors emit only `DecodeErrorEvent`; both event types preserve wire
order. `PacketDecodedEvent` is a frozen notification with a `.packet` attribute,
not a gameplay snapshot with `copy()` or `to_dict()`. Its owned native packet
envelope, header and payload remain read-only and valid after cleanup. No packet
journal is retained.

## Statistics

Captures retain their values; reconnect starts new application counters.
`connected_bytes_sent` and `connected_bytes_received` are always `None` with
both transports, which do not expose connected-byte counters.

| Counter | Boundary |
| --- | --- |
| `batch_bytes_in` | Received native batch payload bytes before decoding, including malformed batches. RakNet's FE marker and transport framing are excluded. |
| `packets_in`, `packet_counts_in` | Every validated header in a successfully returned batch, including unknown, unselected and subclient-filtered packets. The histogram has 1024 entries. |
| `play_packet_counts_in` | Successfully decoded packets after spawn completion, including later packets in the batch that completes spawn. Unselected packets and recovered body errors do not count; decoded subclient packets do. |
| `batch_bytes_out`, `packets_out`, `packet_counts_out` | Advance only after awaited `Connection.send()` returns successfully, independently of an operation's submitted phase. RakNet's FE marker and transport framing are excluded from batch bytes. |
| `play_packet_counts_out` | Successful sends after spawn completion. Excludes login and `SetLocalPlayerAsInitialized`; includes the later radius override and loading-screen messages. |

A fatal native batch decode counts input bytes and a decode error, but **no packet
or partial histogram prefix**. A recovered batch counts all its headers in the
totals and keeps successful decoded packets available. Later Python validation
failure does not undo already counted headers. Both play histograms are immutable
1024-entry sequences.

## Transport and limits

NetherNet is the default transport. It uses HTTP signaling and DTLS encryption,
and always verifies the server identity statement signature and SDP fingerprint
signature through the underlying library. The optional `server_key_sha256` pin
strictly checks the expected server identity key when supplied. NetherNet
supports protocols 1001 and 2193 without a second game-layer encryption stream.

RakNet is opt-in with `BotOptions(transport="raknet")`, the optional `raknet`
extra and a `server_url` of the form `raknet://host:port`. The URI cannot contain
user information, a path, a query or a fragment. RakNet supports protocols 1001
and 2193. When the server sends a signed game handshake, RakNet enables native game
encryption and uses the native batch codec over reliable ordered FE framing; it
does not use DTLS. Without a pin, the server may omit the handshake and still
report login success.

For RakNet, `server_key_sha256` optionally pins the SHA-256 digest of the DER
SubjectPublicKeyInfo for the server's game-handshake key. When a pin is set, the
server must complete that handshake before login succeeds, and the key must
match. Without a pin, any handshake's self-signature is checked, but server
identity is unauthenticated and a man-in-the-middle attack is possible. Neither
transport provides Xbox account authentication: `BotOptions.xuid` declares an
offline identity field. It defaults to empty; a supplied value is at most 20
ASCII decimal digits fitting an unsigned 64-bit integer, with its decimal
spelling preserved.

The application send queue accepts at most 4096 packets and 4 MiB of estimated
encoded data, including a batch being submitted. Commands are revalidated before
batch encoding. Complete wire messages are limited to 32 MiB plus 1 KiB each way;
the receive check runs after transport reassembly. Batch byte statistics count
the native batch payload and exclude RakNet's FE marker and other transport
framing. These limits do not bound transport backlog or reassembly memory.
`Connection.send()` provides no capacity wait or atomic fragment reservation.

Runtime exit joins bot-owned reader, sender, ticker and connection-attempt work,
settles queued boundaries and operations, and awaits connection close. Cancelling
a close waiter does not cancel this cleanup. With RakNet, cancellation during
native connection acquisition waits for the acquisition to settle and closes
any late connection; name resolution and native cleanup can extend beyond the
configured connection timeout.

## Examples

[RakNet connection](../examples/raknet_connect.py) waits for readiness and prints
the player's position. [First trade](../examples/first_trade.py) demonstrates
observed inventory, a single exchange and returning residual input. [Villager cycle](../examples/villager_cycle.py)
adds exhaustion, natural restock, another trade, an attack and reconnect. They
preserve unrelated inventory and reject stale references or uncertain outcomes.

Prepare the world and target entities before using these examples. Their CLIs
require `--server-url`, target unique IDs and an output `--archive`;
`--server-key-sha256` is optional. Run either script with `--help` for preparation
and timeout arguments.
