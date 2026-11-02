import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from peer import actor_packet, until
from trade_peer import (
    close_packet,
    deliver,
    inventory_packet,
    requests,
    response_packet,
    scene,
    sent,
    stack,
    trade_packet,
)

import endpilot as api


def load_example(name):
    path = Path(__file__).resolve().parents[2] / "examples" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("version", [1001, 2193])
async def test_callable_example_executes_finite_residual_cycle_and_rejects_tampering(monkeypatch, version):
    example = load_example("first_trade")
    events = example.Events()
    async with scene(monkeypatch, version, callback=events) as (bot, peer, _):
        p = peer.p
        await deliver(
            bot, peer, inventory_packet(p, {9: stack(p, 43, 4, 101), 20: stack(p, 44, 6, 701, data=b"unrelated item")})
        )
        await deliver(bot, peer, actor_packet(p))

        async def server():
            await until(lambda: any(packet.packet_id == 30 for packet in peer.received))
            peer.send(trade_packet(p), inventory_packet(p, inventory_id=5, size=3))
            await sent(peer, 1)
            peer.send(response_packet(p, -1, [(29, 9, 0, 0), (47, 4, 4, 301)]))
            await sent(peer, 2)
            peer.send(response_packet(p, -3, [(47, 4, 1, 301), (29, 10, 1, 501)]))
            await sent(peer, 3)
            peer.send(response_packet(p, -5, [(47, 4, 0, 0), (29, 9, 1, 401)]))
            async with asyncio.timeout(2):
                await events.wait(bot, lambda: bot.get_trade_window().closing, 2)
            peer.send(close_packet(p, 6))
            await asyncio.sleep(0)
            peer.send(close_packet(p))

        server_task = asyncio.create_task(server())
        try:
            result = await example.first_trade(
                bot,
                events,
                target_unique_id=-92,
                prepared_items={"minecraft:wheat": 4},
                prepare_timeout=2,
                operation_timeout=2,
                max_distance=1000,
            )
            await server_task
        finally:
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        assert result.returned == (1,)
        assert [request.client_request_id.raw_id for request in requests(peer)] == [-1, -3, -5]
        assert [operation.outcome for operation in result.operations] == [api.ItemRequestOutcome.completed] * 3
        assert result.before.inventories[0].slots[9].stack_size == 4
        assert result.after.inventories[0].slots[9].stack_size == 1
        assert result.after.inventories[0].slots[10].id == 42
        quote = result.quote
        after = example.inventory_record(bot)
        before = [dict(row) for row in after]
        before[9] = {**before[9], "count": 4}
        before[10] = {**before[10], "state": "empty", "identifier": None, "count": 0}
        example.check_conservation(before, after, {"minecraft:wheat": 3}, quote.output)
        tampered = [dict(row) for row in after]
        tampered[10]["count"] = 2
        with pytest.raises(RuntimeError, match="conservation"):
            example.check_conservation(before, tampered, {"minecraft:wheat": 3}, quote.output)
        tampered = [dict(row) for row in after]
        tampered[20] = {**tampered[20], "stack_id": 999}
        with pytest.raises(RuntimeError, match="unrelated"):
            example.check_conservation(before, tampered, {"minecraft:wheat": 3}, quote.output)
    assert result.operations[-1].touched_slots[0].state == api.SlotState.empty


def test_examples_validate_cli_before_any_connection():
    first = load_example("first_trade")
    cycle = load_example("villager_cycle")
    base = ["--server-url", "http://127.0.0.1:19132", "--server-key-sha256", "ab" * 32, "--archive", "test.json"]
    first_args = [*base, "--target-unique-id", "-92", "--prepared-item", "minecraft:wheat=4"]
    cycle_args = [*base, "--trader-unique-id", "-92", "--attack-target-unique-id", "-93"]
    assert first.parse_args(first_args).prepared_items == {"minecraft:wheat": 4}
    assert cycle.parse_args(cycle_args).prepared_wheat == 2048
    base_without_pin = ["--server-url", "http://127.0.0.1:19132", "--archive", "test.json"]
    first_without_pin = [*base_without_pin, "--target-unique-id", "-92", "--prepared-item", "minecraft:wheat=4"]
    cycle_without_pin = [*base_without_pin, "--trader-unique-id", "-92", "--attack-target-unique-id", "-93"]
    assert first.parse_args(first_without_pin).options.server_key_sha256 == ""
    assert cycle.parse_args(cycle_without_pin).options.server_key_sha256 == ""
    for module, args, extra in (
        (first, first_args, ["--operation-timeout", "nan"]),
        (first, first_args, ["--stage-extra", "0"]),
        (first, first_args, ["--target-unique-id", str(1 << 63)]),
        (first, first_args, ["--prepared-item", "wheat=-1"]),
        (first, first_without_pin, ["--server-key-sha256", "not-a-pin"]),
        (cycle, cycle_args, ["--restock-timeout", "inf"]),
        (cycle, cycle_args, ["--attack-target-unique-id", "-92"]),
        (cycle, cycle_without_pin, ["--server-key-sha256", "not-a-pin"]),
    ):
        with pytest.raises(SystemExit):
            module.parse_args([*args, *extra])


async def test_cycle_callable_validates_before_touching_bot():
    load_example("first_trade")
    cycle = load_example("villager_cycle")
    with pytest.raises(ValueError, match="distinct"):
        await cycle.villager_cycle(None, None, trader_unique_id=1, attack_target_unique_id=1)
    with pytest.raises(ValueError, match="positive"):
        await cycle.villager_cycle(None, None, trader_unique_id=1, attack_target_unique_id=2, restock_timeout=-1)


async def test_behavior_example_combines_and_stops_independent_intents(monkeypatch):
    """Exercise the example against the existing decoded-packet peer, not a second harness."""
    from types import SimpleNamespace

    from peer import Peers

    example = load_example("behavior_composition")
    peers = Peers(monkeypatch)
    navigation, avoidance, posture = example.Navigation(), example.ActorAvoidance(), example.Posture()
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=2193))
        unsubscribe = example.subscribe_behaviors(runtime, bot, navigation, avoidance, posture)
        try:
            await bot.wait_ready(2)
            peer = peers.connections[-1]
            flags = peer.p.PlayerAuthInputPacket.InputData

            def frames():
                return [packet for packet in peer.received if packet.packet_id == 144]

            def has(packet, name):
                return getattr(flags, name) in packet.input_data

            navigation.start()
            avoidance.start()
            posture.start("sprint")
            start = len(frames())
            await until(lambda: any(p.move.y == 1 and has(p, "SPRINTING") for p in frames()[start:]))

            # Nearby actors suppress forward/sprint but preserve the independent jump intent.
            monkeypatch.setattr(
                bot, "get_observed_entities",
                lambda: [SimpleNamespace(position=bot.get_status().position)],
            )
            start = len(frames())
            await until(lambda: any(
                p.move.y == 0 and has(p, "JUMPING") and not has(p, "SPRINTING")
                for p in frames()[start:]
            ))

            posture.start("sneak")
            avoidance.stop()
            start = len(frames())
            await until(lambda: any(
                p.move.y == 1 and has(p, "SNEAKING") and has(p, "START_SNEAKING")
                and has(p, "SNEAK_DOWN") and not has(p, "SPRINTING")
                for p in frames()[start:]
            ))

            navigation.stop()
            posture.stop()
            start = len(frames())
            await until(lambda: any(
                p.move.y == 0 and not has(p, "JUMPING")
                and not has(p, "SPRINTING") and not has(p, "SNEAKING")
                and has(p, "STOP_SNEAKING") and has(p, "SNEAK_RELEASED_RAW")
                for p in frames()[start:]
            ))

            navigation.start()
            posture.start("sprint")
            await bot.reconnect()
            await bot.wait_ready(2)
            assert not navigation.enabled and not avoidance.enabled and posture.mode is None
            assert peers.connections[-1] is not peer and peer.closed
        finally:
            navigation.stop()
            avoidance.stop()
            posture.stop()
            unsubscribe()
