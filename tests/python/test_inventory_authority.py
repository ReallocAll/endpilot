import bedrock_protocol as bp
import pytest
from peer import empty_item
from trade_peer import (
    deliver,
    inventory_packet,
    requests,
    response_packet,
    scene,
    sent,
    slot_events,
    slot_packet,
    stack,
)

import endpilot as api


@pytest.mark.parametrize("version", [1001, 2193])
async def test_normal_transaction_authority_precedes_callback_and_missing_response(monkeypatch, version):
    observations = []

    def callback(bot, event):
        if isinstance(event, api.InventoryEvent) and event.inventory.slots[0].id:
            observations.append((bot.get_item(bot.player_slot(0)).stack_id, event.inventory.complete))

    async with scene(monkeypatch, version, callback=callback) as (bot, peer, events):
        p = peer.p
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3)
        await sent(peer)
        source = p.InventorySource(
            type=p.InventorySourceType.CONTAINER_INVENTORY, container_id=p.ContainerID(0), flags=None
        )
        actions = [
            p.InventoryAction(source=source, slot=index, from_item_descriptor=empty_item(p), to_item_descriptor=item)
            for index, item in (
                (9, stack(p, count=7)),
                (0, stack(p, count=3, stack_id=201, data=b"richer transaction metadata")),
            )
        ]
        events.clear()
        await deliver(
            bot,
            peer,
            p.InventoryTransactionPacket(
                legacy_request_id=0,
                legacy_set_item_slots=None,
                transaction=bp.Variant(0, p.NormalTransactionData(transaction=p.InventoryTransaction(actions=actions))),
            ),
        )
        peer.send(response_packet(p, absent=True))
        result = await operation.wait(2)
        assert result.outcome == api.ItemRequestOutcome.completed
        assert observations == [(201, True)]
        assert bytes(result.touched_slots[1].item.user_data_buffer) == b"richer transaction metadata"
        assert not slot_events(events)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_last_negative_odd_request_id_is_admitted_once(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, _):
        bot._session.inventory.next_request_id = -2147483647
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 1)
        assert operation.key.request_id == -2147483647
        await sent(peer)
        peer.send(response_packet(peer.p, operation.key.request_id, result=1))
        assert (await operation.wait(2)).outcome == api.ItemRequestOutcome.rejected
        with pytest.raises(RuntimeError, match="IDs exhausted"):
            bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 1)
        assert len(requests(peer)) == 1


@pytest.mark.parametrize("version", [1001, 2193])
async def test_slot_event_precedes_inventory_event_and_keeps_earlier_snapshots(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, events):
        before = bot.capture_inventory(0)
        events.clear()
        await deliver(bot, peer, slot_packet(peer.p, 9, stack(peer.p, count=7)))
        updates = [event for event in events if isinstance(event, (api.InventorySlotEvent, api.InventoryEvent))]
        assert [type(event) for event in updates] == [api.InventorySlotEvent, api.InventoryEvent]
        assert updates[0].item.stack_size == updates[1].inventory.slots[9].stack_size == 7
        assert before.slots[9].stack_size == 10
        after_slot = updates[1].inventory
        assert after_slot.complete and after_slot.full_container_name is not None

        events.clear()
        await deliver(bot, peer, inventory_packet(peer.p, {9: stack(peer.p, count=5)}))
        full_updates = [event for event in events if isinstance(event, (api.InventorySlotEvent, api.InventoryEvent))]
        assert [type(event) for event in full_updates] == [api.InventoryEvent]
        assert full_updates[0].inventory.slots[9].stack_size == 5
        assert after_slot.slots[9].stack_size == 7
        assert before.slots[9].stack_size == 10
