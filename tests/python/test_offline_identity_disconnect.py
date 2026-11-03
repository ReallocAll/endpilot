import asyncio
import dataclasses
import json
from types import SimpleNamespace

import bedrock_protocol as bp
import pytest
from peer import verify_login_token
from test_identity_pin import unbase

import endpilot as api
from endpilot import transport
from endpilot.auth import build_login, create_identity
from endpilot.session import Session


@pytest.mark.parametrize("xuid", ["", "0", "2535400000000000", "00042", "18446744073709551615"])
def test_offline_xuid_roundtrips_signed_identity_and_login(xuid):
    options = api.BotOptions(server_url="http://peer", xuid=xuid)
    options._validate()
    identity = create_identity(options.name, options.uuid, xuid=options.xuid)
    verify_login_token(identity.token)
    claims = json.loads(unbase(identity.token.split(".")[1]))
    assert claims["xid"] == xuid
    assert claims["ipt"] == claims["mid"] == claims["tid"] == ""
    assert identity.xuid == xuid
    login = build_login(identity, options)
    size = int.from_bytes(login[:4], "little")
    envelope = json.loads(login[4:4 + size])
    assert envelope["Token"] == identity.token
    assert envelope["AuthenticationType"] == 2


@pytest.mark.parametrize("xuid", [None, True, 42, "-1", "+1", "1.0", " 1", "1 ", "１２", "١٢",
                                  "18446744073709551616", "000000000000000000000"])
def test_invalid_offline_xuid_rejected(xuid):
    with pytest.raises(ValueError, match="xuid"):
        api.BotOptions(server_url="http://peer", xuid=xuid)._validate()


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("messages", [None, ("server kick", "filtered kick"), ("", "filtered only"), ("  ", "")])
async def test_native_disconnect_fields_survive_session_cleanup(monkeypatch, protocol, messages):
    events, closes, failures = [], [], []
    loop = asyncio.get_running_loop()
    runtime = SimpleNamespace(_loop=loop, _stopping=False, _suppressed=False,
                              _emit=lambda _bot, _kind, factory: events.append(factory()),
                              _background_failure=failures.append)
    options = api.BotOptions(server_url="http://peer", protocol=protocol)
    options._validate()
    bot = SimpleNamespace(_runtime=runtime, _options=options, _generation=1, _wake=asyncio.Event(), _token=api.BotToken())
    ready = loop.create_future()
    session = Session(bot, 1, ready)
    p = session.p
    native_messages = None if messages is None else p.DisconnectPacketMessages(
        message=messages[0], filtered_message=messages[1]
    )
    packet = p.DisconnectPacket(reason=p.DisconnectFailReason.LEGACY_DISCONNECT,
                                messages=bp.Variant(int(messages is None), native_messages))
    decoded = session.codec.decode_packet(session.codec.encode_packet(packet)).payload

    async def reader():
        await session.handle(decoded)

    async def send(_data):
        pass

    async def close():
        closes.append(True)

    async def open_connection(_options, _identity):
        return SimpleNamespace(send=send, close=close)

    monkeypatch.setattr(transport, "_open_connection", open_connection)
    session.reader = reader
    await session.run()
    ready.exception()
    assert not failures and closes == [True] and session.finished
    lifecycle = [event for event in events if event.state in (api.BotState.closing, api.BotState.failed)]
    assert [event.state for event in lifecycle] == [api.BotState.closing, api.BotState.failed]
    expected = api.ServerDisconnectReason(13, *(messages if messages is not None else (None, None)))
    for event in lifecycle:
        assert event.disconnect == expected
        assert event.copy().disconnect == expected
        assert event.to_dict()["disconnect"] == expected.to_dict()
        if messages is not None and (messages[0].strip() or messages[1].strip()):
            assert event.reason == (messages[0] if messages[0].strip() else messages[1])
        else:
            assert event.reason == "server Disconnect packet (reason 13)"
    captured = lifecycle[-1].copy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        captured.disconnect.message = "changed"
    session.disconnect_reason = None
    assert captured.disconnect == expected
