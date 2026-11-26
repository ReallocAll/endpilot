# Sessions and resource packs

See the [login tutorial](../tutorials/login.md) for transport setup and Xbox sign-in.

## Accept server resource packs

Either join method accepts an `on_resource_packs` callback. For a nonempty
offer, the callback receives the required flag and each pack's UUID, version,
advertised size, and server-selected `subpack_name`. It can return directly or
await a decision. In this example, the server offers a pack containing
`texts/en_US.lang`.

```python
import asyncio

from endpilot import Bot, ResourcePackDecision


def decide(offer):
    print("Required:", offer.required)
    for pack in offer.packs:
        print(pack.pack_id, pack.version, pack.size, pack.subpack_name)
    return ResourcePackDecision.ACCEPT


async def main():
    bot = Bot("Knight")
    try:
        await bot.join_nethernet("http://127.0.0.1:19132", on_resource_packs=decide)
        print(bot.resource_pack_index.read("texts/en_US.lang").decode("utf-8"))
    finally:
        await bot.close()


asyncio.run(main())
```

Use `ACCEPT` for ordinary unencrypted resource packs on the current connection.
Use `REFUSE` to tell the server and wait for its login decision. Refusal is the
default for a nonempty offer. Use `CANCEL` to end the attempt with
`ResourcePackCancelled`, or cancel the join task to interrupt a transfer.
Empty offers complete automatically.

The SDK checks the transferred ZIP size and SHA-256, matches the manifest UUID
and version, then builds the read-only session index in server stack order.
If the server selects a subpack, its name must match the offer, stack, and
manifest, and its directory must exist in the ZIP. The selected files override
the pack root at the same relative path, and the other subpack directories are
omitted from the index. Later stack entries override earlier files at the same
path. For each loaded pack, the index contains the selected `subpack_name`
(empty for ordinary packs without subpacks), actual transferred size, and digest.
Missing paths raise `KeyError`. The index handles ordinary resource files in
memory, with aggregate limits of 64 MiB transferred, 128 MiB unpacked, and
32,768 entries in classic single-disk ZIP central directories. Pack scripts,
encrypted or premium packs, and invalid subpack selections raise
`ResourcePackError`.

## Observe session end

For a joined bot, `await bot.wait_for_session_end(timeout=10)` returns its
first `SessionEnd` observation. It raises `TimeoutError` if the session is
still running at the deadline. Pass `timeout=None` to wait without a deadline.

| `SessionEnd.kind` | What happened |
| --- | --- |
| `server_disconnect` | The server sent a disconnect packet. Read `reason_code`, `reason_name`, `message`, and `filtered_message` when present. |
| `reader_end` | The packet reader ended first. `error_type` identifies its error when available. |
| `local_close` | The caller closed the bot first. |

`bot.session_end` returns `None` until an end is observed. Once set, the
immutable snapshot stays available through `close()` and is cleared on the
next join attempt. Confirm a server crash from BDS process status or logs.

If you need the native `DisconnectPacket`, call
`await bot.wait_for_server_disconnect(timeout=10)` on an active bot. It raises
`ConnectionError` if the stream ends first and `TimeoutError` if it receives
neither event by the deadline.

A server that explicitly rejects a player before spawn raises `LoginRefused`,
a `ConnectionError` subclass. The [login tutorial](../tutorials/login.md#handle-a-refused-join)
shows how to check a refusal with the CLI.

## Observe client ticks

`bot.subscribe_ticks(callback)` calls a synchronous function for each future
executed tick of the active connection, including during loading. The callback
receives the same client tick count as `bot.get_tick()`. The clock uses a 50 ms
schedule and can skip missed slots. The subscription follows executed client
ticks, so it does not establish the server tick or input delivery.

```python
observed_ticks = []
unsubscribe = bot.subscribe_ticks(observed_ticks.append)
try:
    await bot.wait_ticks(3)
finally:
    unsubscribe()
print(observed_ticks)
```

Subscribe while a connection is active. The returned function ends this
registration early. All registrations for the connection end when its
clock stops, so subscribe again after reconnecting.
