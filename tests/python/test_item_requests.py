"""Item request tests over controlled native-codec bytes."""

import dataclasses

import bedrock_protocol as bp
import pytest
from peer import until
from trade_peer import (
    CRAFT_RESULT_BODIES,
    action,
    assert_unknown_touched,
    close_packet,
    compound,
    deliver,
    inventory_packet,
    open_packet,
    open_trade,
    recipe,
    registry_packet,
    request_events,
    requests,
    response_info,
    response_packet,
    scene,
    sent,
    side,
    slot_events,
    slot_packet,
    stack,
    tag,
    trade_packet,
    wire_slot,
)

import endpilot as api


@pytest.mark.parametrize("version", [1001, 2193])
def test_independent_craft_result_body_goldens(version):
    p = getattr(bp, f"v{version}")
    if version == 1001:
        item = p.SerializedNetworkItemInstanceDescriptor(
            id=42, stack_size=1, aux_value=0, block_runtime_id=0, user_data_buffer=b"\0" * 10
        )
    else:
        c = p.ItemStackRequestCereal
        item = c.NetworkItemInstanceDescriptorData(
            item_descriptor=bp.Variant(1, c.ItemNameDescriptorData(full_name="minecraft:emerald", aux_value=0)),
            stack_size=1,
            block_runtime_id=0,
            user_data_buffer=b"\0" * 10,
        )
    assert item.wire_bytes() == CRAFT_RESULT_BODIES[version]


@pytest.mark.parametrize("version", [1001, 2193])
async def test_server_packet_fixtures_round_trip_and_reach_observations(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, events):
        p = peer.p
        window = await open_trade(bot, peer, second=True)
        assert window.window_id == 5 and window.trader_unique_id == -92
        assert bot.get_item(bot.trade_input(window, 0)).count == 7
        assert bot.get_item(bot.trade_input(window, 1)).stack_id == 302
        packets = [
            response_packet(p, rows=[(29, 9, 7, 101), (28, 0, 3, 201)]),
            response_packet(p, rows=[(29, 9, 0, 0)]),
            response_packet(p, result=1),
            response_packet(p, absent=True),
            open_packet(p),
            close_packet(p),
            trade_packet(p, []),
            trade_packet(p, data=compound({"Recipes": tag("INT", 0)})),
            inventory_packet(p, {9: stack(p, stack_id=-1, unconfirmed=True)}),
        ]
        for extra in [{"requested_slot": 1}, {"dynamic_id": 1}, {"name": "conflict"}, {"missing_id": True}]:
            packets.append(response_packet(p, rows=[(28, 0, 3, 0, extra)]))
        for packet in packets:
            encoded = peer.codec.encode_packet(packet)
            decoded = peer.codec.decode_packet(encoded).payload
            assert decoded.packet_id == packet.packet_id
            assert peer.codec.encode_packet(decoded) == encoded
        assert bot.capture_inventory(0).complete
        assert not request_events(events)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_move_during_player_inventory_window(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, _):
        p = peer.p
        await deliver(
            bot, peer,
            p.ContainerOpenPacket(
                container_id=p.ContainerID(2),
                type=p.ContainerType(-1),
                pos=p.BlockPos(x=0, y=0, z=0),
                entity_unique_id=-1,
            ),
        )
        source, destination = bot.player_slot(9), bot.player_slot(0)
        operation = bot.begin_move_item(source, destination, 3)
        request = await sent(peer)
        place = action(request.actions[0])
        assert wire_slot(side(place, "source")) == (29, 9, 101)
        peer.send(response_packet(p, rows=[(29, 9, 7, 101), (28, 0, 3, 201)]))
        result = await operation.wait(2)
        assert result.outcome == api.ItemRequestOutcome.completed and result.server_result == 0
        assert bot.get_item(source).count == 7 and bot.get_item(destination).count == 3


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("entrypoint", ["request", "begin"])
async def test_move_exact_actions_no_optimism_and_odd_request_ids(monkeypatch, version, entrypoint):
    async with scene(monkeypatch, version) as (bot, peer, events):
        source, destination = bot.player_slot(9), bot.player_slot(0)
        started = (
            bot.request_move_item(source, destination, 3)
            if entrypoint == "request"
            else bot.begin_move_item(source, destination, 3)
        )
        key = started if entrypoint == "request" else started.key
        assert key.request_id == -1 and key.window is None
        assert bot.get_item(source).count == 10 and bot.get_item(destination).state == api.SlotState.empty
        with pytest.raises(RuntimeError):
            bot.begin_move_item(source, bot.player_slot(1), 1)
        request = await sent(peer)
        assert request.client_request_id.raw_id == -1 and not list(request.strings_to_filter)
        assert len(request.actions) == 1
        place = action(request.actions[0])
        assert "Place" in type(place).__name__ and place.amount == 3
        assert wire_slot(side(place, "source")) == (29, 9, 101)
        assert wire_slot(side(place, "destination")) == (28, 0, 0)
        events.clear()
        await deliver(bot, peer, response_packet(peer.p, -99, [(29, 9, 1, 999)]))
        assert not request_events(events) and bot.get_item(source).count == 10
        peer.send(response_packet(peer.p, -1, [(29, 9, 7, 101), (28, 0, 3, 201)]))
        await until(lambda: len(request_events(events)) == 1)
        assert bot.get_item(source).count == 7 and bot.get_item(destination).count == 3
        assert bot.get_item(destination).stack_id == 201
        assert not slot_events(events)
        if entrypoint == "begin":
            result = await started.wait(2)
            assert result.outcome == api.ItemRequestOutcome.completed and result.server_result == 0
            assert [slot.slot for slot in result.touched_slots] == [source, destination]
            assert [slot.count for slot in result.touched_slots] == [7, 3]
        second = bot.begin_move_item(destination, bot.player_slot(1), 1)
        assert second.key.request_id == -3
        await sent(peer, 2)
        peer.send(response_packet(peer.p, -3, result=1))
        assert (await second.wait(2)).outcome == api.ItemRequestOutcome.rejected
        assert bot.get_item(destination).count == 3 and bot.get_item(bot.player_slot(1)).count == 0


@pytest.mark.parametrize("version", [1001, 2193])
async def test_move_prevalidation_consumes_no_id_or_wire(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, _):
        source, destination = bot.player_slot(9), bot.player_slot(0)
        for count in [0, -1, 11, 65536]:
            with pytest.raises((ValueError, RuntimeError)):
                bot.begin_move_item(source, destination, count)
        for bad in [
            source,
            dataclasses.replace(destination, bot=api.BotToken()),
            dataclasses.replace(destination, session_id=destination.session_id + 1),
        ]:
            with pytest.raises((ValueError, RuntimeError)):
                bot.request_move_item(source, bad, 1)
        cases = [
            {9: stack(peer.p), 0: stack(peer.p, 43, 1, 202)},
            {9: stack(peer.p), 0: stack(peer.p, 42, 1, 202, data=b"different")},
            {9: stack(peer.p), 0: stack(peer.p, 42, 1, 202, aux=1)},
            {9: stack(peer.p), 0: stack(peer.p, 42, 1, 202, block=1)},
            {9: stack(peer.p), 0: stack(peer.p, 42, 65535, 202)},
            {9: stack(peer.p, stack_id=-1, unconfirmed=True)},
            {9: stack(peer.p, stack_id=0)},
        ]
        for slots in cases:
            await deliver(bot, peer, inventory_packet(peer.p, slots))
            with pytest.raises((ValueError, RuntimeError)):
                bot.begin_move_item(source, destination, 1)
        await deliver(bot, peer, inventory_packet(peer.p, {9: stack(peer.p)}, size=10))
        with pytest.raises((ValueError, RuntimeError)):
            bot.begin_move_item(source, bot.player_slot(10), 1)
        assert not requests(peer)
        await deliver(bot, peer, inventory_packet(peer.p, {9: stack(peer.p)}))
        operation = bot.begin_move_item(source, destination, 1)
        assert operation.key.request_id == -1
        assert bot.cancel_item_operation(operation) == api.ItemCancellation.cancelled
        next_key = bot.request_move_item(source, destination, 1)
        assert next_key.request_id == -3
        request = await sent(peer)
        assert request.client_request_id.raw_id == -3


@pytest.mark.parametrize("version", [1001, 2193])
async def test_submission_boundary_and_queued_cancellation_preserve_ids(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, _):
        peer.pause_sender(bot)
        cancelled = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3)
        assert cancelled.phase == api.ItemOperationPhase.queued
        await deliver(bot, peer, response_packet(peer.p, rows=[(29, 9, 7, 101), (28, 0, 3, 201)]))
        assert cancelled.result is None and bot.get_item(bot.player_slot(9)).count == 10
        assert bot.get_item(bot.player_slot(0)).count == 0
        assert bot.cancel_item_operation(cancelled) == api.ItemCancellation.cancelled
        cancelled_result = await cancelled.wait(2)
        assert [slot.count for slot in cancelled_result.touched_slots] == [10, 0]
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3)
        assert operation.key.request_id == -3
        observed = []
        peer.before_send = lambda: observed.append(operation.phase)
        peer.resume_sender()
        await sent(peer)
        peer.before_send = None
        assert observed and all(phase == api.ItemOperationPhase.submitted for phase in observed)
        assert [request.client_request_id.raw_id for request in requests(peer)] == [-3]
        peer.send(response_packet(peer.p, -3, result=1))
        assert (await operation.wait(2)).outcome == api.ItemRequestOutcome.rejected
        assert cancelled.result is cancelled_result


RESPONSE_CASES = [
    "details",
    "identical_alias",
    "conflicting_alias_amount",
    "conflicting_alias_metadata",
    "requested_alias",
    "unsupported_container",
    "unsupported_hotbar_alias",
    "dynamic_container",
    "missing_touched",
    "unconfirmed_id",
    "authoritative_conflict",
    "atomic_unrelated",
    "zero_source",
    "opaque_identity",
    "additional_resolved_slot",
]


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("case", RESPONSE_CASES)
async def test_response_merge_is_atomic_and_invalidates_only_touched(monkeypatch, version, case):
    async with scene(monkeypatch, version) as (bot, peer, events):
        if case == "opaque_identity":
            await deliver(bot, peer, slot_packet(peer.p, 9, stack(peer.p, data=b"opaque full identity")))
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 10 if case == "zero_source" else 3)
        await sent(peer)
        rows = [(29, 9, 7, 101), (28, 0, 3, 201)]
        if case == "identical_alias":
            rows.append((28, 0, 3, 201))
        elif case.startswith("conflicting_alias"):
            rows.append(
                (
                    28,
                    0,
                    5 if case.endswith("amount") else 3,
                    201,
                    {} if case.endswith("amount") else {"name": "conflict"},
                )
            )
        elif case == "requested_alias":
            rows[1] = (28, 0, 3, 201, {"requested_slot": 1})
        elif case == "unsupported_container":
            rows[1] = (60, 0, 3, 201)
        elif case == "unsupported_hotbar_alias":
            rows.append((29, 0, 3, 201))
        elif case == "dynamic_container":
            rows[1] = (28, 0, 3, 201, {"dynamic_id": 1})
        elif case == "missing_touched":
            rows.pop()
        elif case == "unconfirmed_id":
            rows[1] = (28, 0, 3, 0, {"missing_id": True})
        elif case == "authoritative_conflict":
            await deliver(bot, peer, slot_packet(peer.p, 0, stack(peer.p, 43, 3, 999, data=b"foreign identity")))
        elif case == "atomic_unrelated":
            rows = [(29, 20, 9, 701), *rows, (28, 0, 3, 999)]
        elif case == "additional_resolved_slot":
            rows.append((29, 20, 9, 701))
        elif case == "zero_source":
            rows = [(29, 9, 0, 0), (28, 0, 10, 201)]
        events.clear()
        info = response_info(peer.p, rows=rows)
        peer.send(peer.p.ItemStackResponsePacket(responses=[info]))
        result = await operation.wait(2)
        assert result.response.wire_bytes() == info.wire_bytes()
        assert [slot.slot.index for slot in result.touched_slots] == [9, 0]
        assert not slot_events(events)
        if case in {"details", "identical_alias", "zero_source", "opaque_identity", "additional_resolved_slot"}:
            assert result.outcome == api.ItemRequestOutcome.completed
            assert [slot.count for slot in result.touched_slots] == ([0, 10] if case == "zero_source" else [7, 3])
            assert bot.get_item(bot.player_slot(0)).stack_id == 201
            if case == "opaque_identity":
                assert bytes(bot.get_item(bot.player_slot(0)).item.user_data_buffer) == b"opaque full identity"
            if case == "additional_resolved_slot":
                assert bot.get_item(bot.player_slot(20)).count == 9
        else:
            assert result.outcome == api.ItemRequestOutcome.indeterminate
            assert_unknown_touched(bot, 9, 0)
            assert all(slot.state == api.SlotState.unknown for slot in result.touched_slots)


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize(
    "authority", ["submitted_full", "submitted_slots", "queued", "unchanged", "queued_matching_id"]
)
async def test_only_post_submission_authority_can_fill_missing_response(monkeypatch, version, authority):
    async with scene(monkeypatch, version) as (bot, peer, _):
        queued = authority.startswith("queued")
        if queued:
            peer.pause_sender(bot)
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3)
        if not queued:
            await sent(peer)
        p = peer.p
        slots = {
            9: stack(p, count=7),
            0: stack(p, count=3, stack_id=201, data=b"authoritative full identity"),
            20: stack(p, 44, 6, 701, data=b"unrelated item"),
        }
        if authority == "unchanged":
            slots.pop(0)
            slots[9] = stack(p)
        if authority == "submitted_slots":
            await deliver(bot, peer, *(slot_packet(p, index, item) for index, item in slots.items()))
        else:
            await deliver(bot, peer, inventory_packet(p, slots))
        if queued:
            assert operation.phase == api.ItemOperationPhase.queued
            peer.resume_sender()
            await sent(peer)
        rows = [(29, 9, 7, 101), (28, 0, 3, 201)] if authority == "queued_matching_id" else []
        peer.send(response_packet(p, rows=rows, absent=not rows))
        result = await operation.wait(2)
        if authority in {"queued", "unchanged"}:
            assert result.outcome == api.ItemRequestOutcome.indeterminate
            assert_unknown_touched(bot, 9, 0)
        else:
            assert result.outcome == api.ItemRequestOutcome.completed
            assert [slot.count for slot in result.touched_slots] == [7, 3]
            assert bytes(bot.get_item(bot.player_slot(0)).item.user_data_buffer) == b"authoritative full identity"


@pytest.mark.parametrize("version", [1001, 2193])
async def test_one_response_packet_cannot_complete_callback_created_request(monkeypatch, version):
    operations, seen = [], []

    def callback(bot, event):
        if isinstance(event, api.ItemRequestEvent):
            seen.append(event)
            if event.request_id == -1:
                assert operations[0].result is not None
                assert operations[0].phase == api.ItemOperationPhase.terminal
                assert bot.get_item(bot.player_slot(0)).count == 3
                operations.append(bot.begin_move_item(bot.player_slot(0), bot.player_slot(1), 1))

    async with scene(monkeypatch, version, callback=callback) as (bot, peer, _):
        operations.append(bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3))
        await sent(peer)
        p = peer.p
        await deliver(
            bot,
            peer,
            p.ItemStackResponsePacket(
                responses=[
                    response_info(p, -1, [(29, 9, 7, 101), (28, 0, 3, 201)]),
                    response_info(p, -1, [(28, 0, 99, 999)]),
                    response_info(p, -3, [(28, 0, 1, 999), (28, 1, 2, 999)]),
                ]
            ),
        )
        await sent(peer, 2)
        assert len(seen) == 1 and operations[1].result is None
        assert bot.get_item(bot.player_slot(0)).count == 3 and bot.get_item(bot.player_slot(1)).count == 0
        peer.send(response_packet(p, -3, [(28, 0, 2, 201), (28, 1, 1, 202)]))
        assert (await operations[1].wait(2)).outcome == api.ItemRequestOutcome.completed


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("terminal", ["complete", "reject", "timeout"])
async def test_late_duplicate_and_timeout_results_are_immutable(monkeypatch, version, terminal):
    async with scene(monkeypatch, version, timeout_ms=100) as (bot, peer, events):
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3)
        await sent(peer)
        events.clear()
        if terminal == "complete":
            peer.send(response_packet(peer.p, rows=[(29, 9, 7, 101), (28, 0, 3, 201)]))
        elif terminal == "reject":
            peer.send(response_packet(peer.p, result=1))
        retained = await operation.wait(2)
        expected = {
            "complete": api.ItemRequestOutcome.completed,
            "reject": api.ItemRequestOutcome.rejected,
            "timeout": api.ItemRequestOutcome.indeterminate,
        }[terminal]
        assert retained.outcome == expected
        if terminal == "timeout":
            assert retained.response is None and retained.server_result is None
            assert_unknown_touched(bot, 9, 0)
        before = [(slot.state, slot.count, slot.stack_id) for slot in bot.get_player_inventory()]
        await deliver(bot, peer, response_packet(peer.p, rows=[(29, 9, 1, 999), (28, 0, 9, 999)]))
        assert operation.result is retained and operation.phase == api.ItemOperationPhase.terminal
        assert [(slot.state, slot.count, slot.stack_id) for slot in bot.get_player_inventory()] == before
        assert len(request_events(events)) == 1 and len(requests(peer)) == 1
        assert not slot_events(events)
        with pytest.raises(dataclasses.FrozenInstanceError):
            retained.reason = "changed"


@pytest.mark.parametrize("version", [1001, 2193])
async def test_trade_offers_none_empty_malformed_unsupported_and_duplicate_are_distinct(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, _):
        window = await open_trade(bot, peer, only_open=True)
        assert bot.get_trade_offers(window) is None
        await deliver(bot, peer, trade_packet(peer.p, []))
        empty = bot.get_trade_offers(window)
        assert empty is not None and list(empty.offers) == [] and empty.revision == 1
        await deliver(bot, peer, trade_packet(peer.p, data=compound({"Recipes": tag("INT", 0)})))
        with pytest.raises((ValueError, RuntimeError)):
            bot.get_trade_offers(window)
        malformed = recipe(netId=tag("STRING", "123"))
        duplicate = recipe()
        await deliver(bot, peer, trade_packet(peer.p, [malformed, duplicate, duplicate]))
        offers = bot.get_trade_offers(window)
        assert [offer.index for offer in offers.offers] == [0, 1, 2]
        assert all(not offer.supported and offer.ref is None and offer.unsupported_reason for offer in offers.offers)
        await deliver(bot, peer, trade_packet(peer.p))
        current = bot.get_trade_offers(window)
        offer = current.offers[0]
        assert current.window == window and current.revision == 4
        assert offer.supported and offer.ref.window == window and offer.ref.revision == 4
        assert offer.ref.recipe_net_id == 123
        assert (offer.input.identifier, offer.input.network_id, offer.input.count) == ("minecraft:wheat", 43, 3)
        assert (offer.output.identifier, offer.output.network_id, offer.output.count) == ("minecraft:emerald", 42, 1)
        assert (offer.uses, offer.max_uses, offer.tier, offer.second_input) == (1, 8, 0, None)
        # The registry, not a narrow hard-coded whitelist, determines support.
        potato = compound({
            "Name": tag("STRING", "minecraft:potato"),
            "Count": tag("BYTE", 1),
            "Damage": tag("SHORT", 0),
        })
        await deliver(bot, peer, trade_packet(peer.p, [recipe(sell=potato)]))
        observed = bot.get_trade_offers(window).offers[0]
        assert observed.supported and observed.ref is not None
        assert (observed.output.identifier, observed.output.network_id, observed.output.count) == (
            "minecraft:potato", 44, 1,
        )
        await deliver(bot, peer, open_packet(peer.p))
        assert bot.get_trade_window().offer_revision == 5 and bot.get_trade_window().ref == window
        await deliver(bot, peer, close_packet(peer.p))
        assert bot.get_trade_window() is None
        with pytest.raises((ValueError, RuntimeError)):
            bot.get_trade_offers(window)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_offer_strict_tag_types_and_unsupported_rows_never_produce_refs(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, _):
        window = await open_trade(bot, peer)
        canonical = {"Name": tag("STRING", "minecraft:emerald"), "Count": tag("BYTE", 1), "Damage": tag("SHORT", 0)}
        for field, value in [
            ("Count", tag("INT", 1)),
            ("Count", tag("BYTE", 0)),
            ("Count", tag("BYTE", 65)),
            ("Name", tag("STRING", "minecraft:diamond")),
            ("Damage", tag("SHORT", 32767)),
            ("Damage", tag("INT", 0)),
            ("WasPickedUp", tag("BYTE", 1)),
            ("WasPickedUp", tag("INT", 0)),
            ("tag", compound({})),
        ]:
            await deliver(bot, peer, trade_packet(peer.p, [recipe(sell=compound({**canonical, field: value}))]))
            offer = bot.get_trade_offers(window).offers[0]
            assert not offer.supported and offer.ref is None and offer.unsupported_reason
        await deliver(
            bot, peer, trade_packet(peer.p, [recipe(sell=compound({**canonical, "WasPickedUp": tag("BYTE", 0)}))])
        )
        assert bot.get_trade_offers(window).offers[0].supported


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("second", [False, True])
async def test_trade_actions_and_craft_result_body_match_legacy_goldens(monkeypatch, version, second):
    async with scene(monkeypatch, version) as (bot, peer, events):
        window = await open_trade(bot, peer, second=second)
        offer = bot.get_trade_offers(window).offers[0]
        operation = bot.begin_trade(offer.ref, bot.player_slot(10))
        assert operation.key.window == window and operation.key.request_id == -1
        assert bot.get_item(bot.trade_input(window, 0)).count == 7
        assert bot.get_item(bot.player_slot(10)).count == 0
        request = await sent(peer)
        actions = [action(value) for value in request.actions]
        assert len(actions) == (5 if second else 4)
        craft, results, consume = actions[:3]
        assert craft.recipe_net_id.raw_id == 123 and craft.num_crafts == 1
        assert results.num_crafts == 1 and len(results.craft_results) == 1
        assert results.craft_results[0].wire_bytes() == CRAFT_RESULT_BODIES[version]
        assert consume.amount == 3 and wire_slot(side(consume, "source")) == (47, 4, 301)
        if second:
            assert actions[3].amount == 2 and wire_slot(side(actions[3], "source")) == (48, 5, 302)
        take = actions[-1]
        assert "Take" in type(take).__name__ and take.amount == 1
        assert wire_slot(side(take, "source")) == (60, 50, -1)
        assert wire_slot(side(take, "destination")) == (29, 10, 0)
        rows = [(47, 4, 4, 301), *(([(48, 5, 3, 302)]) if second else []), (29, 10, 1, 501)]
        events.clear()
        peer.send(response_packet(peer.p, rows=rows))
        result = await operation.wait(2)
        assert result.outcome == api.ItemRequestOutcome.completed
        assert [slot.count for slot in result.touched_slots] == ([4, 3, 1] if second else [4, 1])
        assert result.touched_slots[0].slot.window == window and result.touched_slots[-1].slot.window is None
        output = bot.get_item(bot.player_slot(10))
        assert (output.network_id, output.count, output.stack_id) == (42, 1, 501)
        assert bytes(output.item.user_data_buffer) == b"\0" * 10
        assert not slot_events(events)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_trade_prevalidation_and_refresh_do_not_consume_ids_or_cancel_pending(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, events):
        window = await open_trade(bot, peer)
        original = bot.get_trade_offers(window).offers[0].ref
        await deliver(bot, peer, trade_packet(peer.p, [recipe(uses=tag("INT", 8))]))
        exhausted = bot.get_trade_offers(window).offers[0]
        assert exhausted.supported and exhausted.ref is not None and exhausted.uses == exhausted.max_uses
        with pytest.raises((ValueError, RuntimeError)):
            bot.request_trade(exhausted.ref, bot.player_slot(10))
        await deliver(bot, peer, trade_packet(peer.p, new_screen=False))
        assert not bot.get_trade_offers(window).offers[0].supported
        await deliver(bot, peer, trade_packet(peer.p))
        fresh = bot.get_trade_offers(window).offers[0].ref
        for bad in [
            original,
            dataclasses.replace(fresh, revision=fresh.revision - 1),
            dataclasses.replace(fresh, recipe_net_id=999),
            dataclasses.replace(fresh, window=dataclasses.replace(window, bot=api.BotToken())),
        ]:
            with pytest.raises((ValueError, RuntimeError)):
                bot.request_trade(bad, bot.player_slot(10))
        for packet in [
            registry_packet(peer.p, missing=True),
            registry_packet(peer.p, component=True),
            registry_packet(peer.p, duplicate=True),
        ]:
            await deliver(bot, peer, packet)
            with pytest.raises((ValueError, RuntimeError)):
                bot.request_trade(fresh, bot.player_slot(10))
        await deliver(bot, peer, registry_packet(peer.p))
        for item in [
            stack(peer.p, 43, 2, 301),
            stack(peer.p, 43, 7, 301, aux=32767),
            stack(peer.p, 43, 7, 301, block=1),
            stack(peer.p, 43, 7, 301, data=b""),
            stack(peer.p, 43, 7, -1, unconfirmed=True),
        ]:
            await deliver(bot, peer, slot_packet(peer.p, 0, item, inventory_id=5))
            with pytest.raises((ValueError, RuntimeError)):
                bot.request_trade(fresh, bot.player_slot(10))
        await deliver(bot, peer, slot_packet(peer.p, 0, stack(peer.p, 43, 7, 301), inventory_id=5))
        with pytest.raises((ValueError, RuntimeError)):
            bot.request_trade(fresh, bot.player_slot(9))
        assert not requests(peer)
        key = bot.request_trade(fresh, bot.player_slot(10))
        assert key.request_id == -1 and key.window == window
        await sent(peer)
        await deliver(bot, peer, trade_packet(peer.p, [recipe(), recipe(netId=tag("STRING", "bad"))]))
        refreshed = bot.get_trade_offers(window)
        assert len(refreshed.offers) == 2 and not refreshed.offers[1].supported
        peer.send(response_packet(peer.p, rows=[(47, 4, 4, 301), (29, 10, 1, 501)]))
        await until(lambda: any(event.key == key for event in request_events(events)))
        with pytest.raises((ValueError, RuntimeError)):
            bot.request_trade(fresh, bot.player_slot(11))
        next_key = bot.request_trade(refreshed.offers[0].ref, bot.player_slot(11))
        assert next_key.request_id == -3
