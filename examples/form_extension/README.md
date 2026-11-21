# Action-form MCP extension

This directory is a separately installable package, not an EndPilot SDK module.
It uses only `Bot.send_chat`, `bot.protocol` and `bot.packets` (public
subscriptions plus typed packet submission). No changes to `Bot`, the SDK
protocol dispatcher or the built-in MCP tools are required.

With an installed EndPilot SDK and its optional `mcp` dependencies:

```sh
python -m pip install --no-deps ./examples/form_extension
python -m unittest discover -s examples/form_extension/tests -v
python examples/respond_form_mcp.py http://localhost:19132
```

Use `raknet://127.0.0.1:19132` to switch transport. The example expects the
Bedrock Testbed form probe plugin and its chat trigger. For general use, run
`python -m endpilot_form_extension.mcp_server` as a stdio MCP server, then
call `connect`, `watch_form`, trigger the form (for example with
`send_chat`), `read_form`, `answer_form`, and `disconnect`.

`watch_form` subscribes before the trigger; `read_form` returns the server
form ID and content. `answer_form` validates the button against that form
and returns only that the typed response was **submitted**. Verify acceptance
using a separate server-side callback or state change.
