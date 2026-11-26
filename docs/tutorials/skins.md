# Use a classic PNG skin

Complete the [SDK and NetherNet installation](../../README.md#install) for
your platform. This example uses a local offline-mode NetherNet server.
From a source checkout, add the `skin` extra for Pillow.

```sh
python -m pip install -e ".[nethernet,skin]"
```

Use `.[raknet,skin]` for RakNet or select the corresponding extras when
installing an EndPilot wheel.

## Load a skin at login

`Skin.from_png()` accepts classic static PNG textures at 64×32, 64×64, or
128×128 pixels, with up to 8 bits per color sample. Choose `model="wide"` for
four-pixel arms (the default) or `model="slim"` for three-pixel arms. Classic
64×32 Steve textures typically use `"wide"`.

`Skin.from_png()` decodes the file into an immutable RGBA snapshot with its
original dimensions. Pass that snapshot as `skin=` when you create the bot.
The constructor skin is used for each login, including RakNet and joins that
use a credential provider.

## Change the skin after joining

Save the following as `skin_demo.py`, then run `python skin_demo.py`. It creates
a blue PNG in a temporary directory and loads it for login. After the bot
spawns, it overwrites the PNG with an orange texture, loads a new snapshot, and
then restores the original snapshot. The temporary file is removed at exit.

```python
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from endpilot import Bot, Skin


async def main():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "demo.png"
        Image.new("RGBA", (64, 64), (40, 110, 220, 255)).save(path)
        original = Skin.from_png(path)
        bot = Bot("Knight", skin=original)
        try:
            await bot.join_nethernet("http://127.0.0.1:19132")
            print(f"Spawned with runtime ID {bot.player_id}")
            await asyncio.to_thread(input, "Press Enter to switch to orange. ")
            Image.new("RGBA", (128, 128), (235, 125, 40, 255)).save(path)
            await bot.change_skin(Skin.from_png(path))
            await asyncio.to_thread(input, "Press Enter to switch back to blue. ")
            await bot.change_skin(original)
            await asyncio.to_thread(input, "Press Enter to leave. ")
        finally:
            await bot.close()


asyncio.run(main())
```

For RakNet, replace the join call with
`await bot.join_raknet("127.0.0.1", 19132)`. For a server that requires Xbox
authentication, follow the [login tutorial](login.md) to pass a credential
provider.

Each `change_skin()` call returns after sending the update for the active
session. Watch from a second client on a server that relays skin updates to see
the bot go from blue to orange and back to blue. Overwriting the PNG leaves the
earlier in-memory snapshot intact, so the last update can reuse `original`.

The `change_skin(skin_id, rgba)` form also submits a solid-color 64×32 skin.
