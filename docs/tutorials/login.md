# Join a Bedrock server

Use `python -m endpilot join` to connect to a Bedrock server. It defaults to a
self-signed offline player named `EndPilot`. Use offline login on a server
configured to accept unauthenticated players. Add `--xbox` when the server
requires an Xbox account.

Follow the [SDK installation instructions](../../README.md#install) for
Python 3.12 or later and the native `bedrock-protocol` wheel. Install the
extra for your server's transport, either `nethernet` or `raknet`. For a bare
`host:port`, install at least one extra for a transport the server offers.
Install both extras to enable all transport choices. For NetherNet on Windows,
follow the pinned dependency instructions in the SDK installation guide. Run
editable installs from the source checkout. If you install an EndPilot wheel,
choose the same extras. The server must support the SDK's default protocol 2225.

## Choose a server address

Use the address provided by the server operator. The address selects the
transport for both offline and Xbox login.

| Address | Transport |
| --- | --- |
| `bedrock.example:19132` | Try NetherNet HTTPS, then NetherNet HTTP, then RakNet on the same host and port. |
| `https://bedrock.example:19132` | NetherNet signaling over HTTPS |
| `http://bedrock.example:19132` | NetherNet signaling directly over plaintext HTTP |
| `raknet://bedrock.example:19132` | RakNet |

An explicit scheme uses only the specified transport. Include a signaling base
path if the operator provides one. With a bare address, EndPilot moves to the
next candidate when the current transport is unavailable. An explicit rejection
during NetherNet signaling ends the join. Once a transport opens, EndPilot
performs Bedrock login on that connection.

## Join an offline-mode server

Use this command for a local offline server with NetherNet HTTP signaling.

```sh
python -m endpilot join http://127.0.0.1:19132 --hold 10
```

For a local offline RakNet server, install the `raknet` extra and use this URL.

```sh
python -m endpilot join raknet://127.0.0.1:19132 --hold 10
```

After spawning, the CLI prints `Joined.` and then `Disconnected.` after the
hold time. The default hold is two seconds. Press Ctrl+C to cancel.
Run `python -m endpilot join --help` for all join options. To join with a PNG
skin, see [Load a skin at login](skins.md#load-a-skin-at-login).

To join as `Knight` from Python, save this as `offline_join.py` and run
`python offline_join.py`. Omitting the credential provider selects offline login.
If you call `Bot()` without a name, it uses `EndPilot` for offline login.

```python
import asyncio

from endpilot import Bot


async def main():
    bot = Bot("Knight")
    try:
        await bot.join_nethernet("http://localhost:19132")
        print(f"Spawned with runtime ID {bot.player_id}")
        await asyncio.sleep(10)
    finally:
        await bot.close()


asyncio.run(main())
```

`Bot.join_address(address)` accepts the same addresses as the CLI. To choose
the transport directly in Python, pass the full signaling URL to
`Bot.join_nethernet(url)` or the host and integer port to
`Bot.join_raknet(host, port)`.

## Sign in to Xbox with the CLI

Install the `xbox` extra alongside your transport. For RakNet, use
`python -m pip install -e ".[xbox,raknet]"`. For NetherNet, use
`python -m pip install -e ".[xbox,nethernet]"`.

The CLI uses the Microsoft Authentication Library (MSAL) and EndPilot's
registered Microsoft application (`e7cfcfa2-c0bb-419c-bf4a-fd966b7fe716`).
Replace the example address below with your server address.

```sh
python -m endpilot join raknet://bedrock.example:19132 --xbox --hold 10
```

When device authorization is needed, run the command in an interactive terminal.
The CLI displays the cache location and a Microsoft verification address with
a one-time code. Enter the code on the official Microsoft page from any device.
If the prompt offers to open the page, press Enter to launch your default
browser. Press Ctrl+C during authorization to cancel the join. Keep the code
and credentials out of recorded logs. The first successful PlayFab sign-in
with this application may create the player's Minecraft game-side record in
PlayFab.
[Microsoft device authorization](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)

The same command continues to the server after authorization. It saves the
verified account if Microsoft provides renewable authorization. A successful
sign-in without renewable authorization is used for the current join only.
Every Xbox join, including a silent sign-in, uses a fresh P-384 session key
and newly issued Bedrock credentials.

### Manage saved Xbox accounts

```sh
python -m endpilot account add
python -m endpilot account list
python -m endpilot join raknet://bedrock.example:19132 --xbox --name Knight
python -m endpilot join raknet://bedrock.example:19132 --xbox --xuid 2533274790000000
python -m endpilot account forget --xuid 2533274790000000
```

Run `account add` in an interactive terminal to authorize and save an account
without joining a server. `account list` shows the XUID and gamertag of each
verified account. For `join --xbox`, use either `--name` or `--xuid` with a
value from that list. `--name` matches the full saved gamertag without regard
to case. If several accounts share that gamertag, use `--xuid` to select one.
Run `python -m endpilot account --help` for the available account commands.

- With an empty cache, `join --xbox` starts device authorization.
- With one account, the join tries it silently and prompts for device
  authorization if required and a terminal is available.
- With multiple accounts, choose by number from the list of names in an
  interactive terminal. Press Enter at the chooser to cancel. In a
  non-interactive session, pass `--name` or `--xuid` to choose the account.
  The CLI tries only the selected account. Selecting a verified account uses
  silent sign-in. If it needs fresh authorization, use `account add` for that
  account and retry the join.

A legacy record with no verified XUID can be selected from the interactive
menu. The CLI can ask you to authorize the same account to verify its identity.
If the upgrade succeeds, the next account list shows its XUID and gamertag.

To use a PNG skin whenever you join with a verified account, see
[Save a skin for an Xbox account](skins.md#save-a-skin-for-an-xbox-account).

The cache stores renewable credentials in plaintext. Anyone who can read the
file may reuse the authorization. The CLI shows the actual path before
displaying a device code.

| Platform | Local cache path |
| --- | --- |
| Windows | `%LOCALAPPDATA%\EndPilot\auth\<client-id>.json` |
| macOS | `~/Library/Application Support/EndPilot/auth/<client-id>.json` |
| Linux | `$XDG_STATE_HOME/endpilot/auth/<client-id>.json` for an absolute `XDG_STATE_HOME`, otherwise `~/.local/state/endpilot/auth/<client-id>.json` |

Check permissions if the CLI reports an unreadable cache. A temporary network
failure leaves the saved account available for a later retry. `account forget`
removes a selected record locally without contacting the server or Microsoft.
Without `--xuid`, it reports an empty cache, removes the only saved account,
or prompts you to choose from several. To revoke the application's
authorization with Microsoft, use your Microsoft account settings.

## Sign in to Xbox from Python

Pass an `XboxCredentialProvider` to a join method. For initial device sign-in,
supply a synchronous callback that displays the verification address and code.
Save this script as `online_join.py` to join over RakNet with the CLI's
default application.

```python
import asyncio

from endpilot import Bot
from endpilot.xbox import XboxCredentialProvider


def show_device_code(address, code):
    print(f"Visit {address} and enter {code}", flush=True)


async def main():
    provider = XboxCredentialProvider(on_device_code=show_device_code)
    bot = Bot("EndPilot")
    try:
        await bot.join_raknet("bedrock.example", 19132, credential_provider=provider)
        print(f"Spawned with runtime ID {bot.player_id}")
    finally:
        await bot.close()


asyncio.run(main())
```

Replace `bedrock.example` and the port, then run `python online_join.py`.
The script prints the bot's runtime ID after spawning and closes the connection.
For NetherNet, use `await bot.join_nethernet(url, credential_provider=provider)`
with the full signaling URL. For address-based transport selection, use
`await bot.join_address(address, credential_provider=provider)`.

In Python, saving a new account is opt-in. Pass `remember_account=True` to
the provider to store renewable credentials. You can use
`XboxCredentialProvider()` without a callback for silent sign-in with a single
already remembered account.

For a separate Microsoft application authorized for personal-account device
sign-in and the Xbox and Minecraft services, pass `client_id` to the provider.
Use the same ID with `account_cache_path(client_id)` to locate that application's
cache or `await forget_account(client_id)` to remove its locally saved accounts.
Both helpers are in `endpilot.xbox`.

## Accept server resource packs

The CLI refuses server resource pack offers by default. Add
`--accept-resource-packs` to accept supported packs for that join over RakNet
or NetherNet. The flag also works in a non-interactive Xbox join with silent
authorization.

```sh
python -m endpilot join raknet://bedrock.example:19132 --xbox --accept-resource-packs
```

See the [session reference](../reference/session.md#accept-server-resource-packs)
for the equivalent Python callback.

## Pin a NetherNet server identity

To pin a server, ask the operator for the lowercase SHA-256 hex digest of
the server public key in DER SubjectPublicKeyInfo (SPKI) form. Obtain the
digest through a trusted channel. Pass its 64 hex characters as
`--server-key-sha256 HEX` to `join`. In Python, pass the digest as
`server_key_sha256` to `Bot.join_address()` or `Bot.join_nethernet()`.
For a bare address, pinning limits transport selection to NetherNet.

With a pin, a missing, invalid, or mismatched NetherNet server identity fails
the join. The pin check happens after the signaling offer is sent. On RakNet,
the initial login precedes session encryption. Raw packet captures and login
diagnostics can contain identity material on either transport.

## Handle a refused join

During NetherNet signaling, a server can reject the player's identity before
the transport opens. Python raises `nethernet.ConnectionFailed` for this
signaling rejection. The CLI reports
`NetherNet connection refused. The player identity was rejected.` and exits
with status `1`.

After the transport opens, a server rejection during Bedrock login raises
`LoginRefused` in Python, a subclass of `ConnectionError`. The CLI exits with
status `1` and reports the refusal. For recognized reasons, it reports a
version mismatch, rejected player identity, or allowlist denial. To check
a Bedrock login refusal on a full test server, run `python -m endpilot join`
with its address while its player limit is occupied.
