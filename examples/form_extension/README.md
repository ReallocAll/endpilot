# Action-form MCP extension

This directory is a separately installable package. It watches the public
packet stream and uses `Bot.get_pending_form()` and `Bot.answer_form()` for the
observed request.

From the repository root, with EndPilot and its optional `mcp` and transport
dependencies installed, install the extension and run its tests.

```sh
python -m pip install --no-deps ./examples/form_extension
python -m unittest discover -s examples/form_extension/tests -v
python -m unittest discover -s tests -p 'test_external_form.py' -v
```

Run either example against a server that sends action forms to the connected
player. Substitute your server's RakNet address or NetherNet signaling URL.

```sh
python examples/respond_form.py raknet://127.0.0.1:19132 --name Alex --button-index 0
python examples/respond_form_mcp.py raknet://127.0.0.1:19132 --name Alex --button-index 0
```

After the watcher is ready, use a server feature that sends an action form
to `Alex`, such as an invitation from another player. If the server opens
the form in response to chat, pass that text as `--message` to send it after
the watcher is ready. `--button-index 0` chooses the first button. Use
`--timeout` to allow more than the default 15 seconds for the form to arrive.

To use the tools directly, start `python -m endpilot_form_extension.mcp_server`
as a stdio MCP server. Call `connect` and `watch_form`, then trigger the form,
using `send_chat` if needed. Call `read_form` to get the server form ID and
content. Pass that ID and the chosen button index to `answer_form`, then call
`disconnect`.
`answer_form` confirms submission of the response packet. Check a server-side
callback or game state change for confirmation that the choice took effect.
