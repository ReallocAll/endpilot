"""Compose independent movement intents with one TickEvent input writer.

Run with python examples/behavior_composition.py --server-url raknet://127.0.0.1:19132.
This is a composition example, not a pathfinder or a client physics predictor.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys

from endpilot import BotOptions, BotState, EventKind, Input, Runtime, StateEvent, TickEvent, Vec2, event_mask


class Navigation:
    def __init__(self) -> None:
        self.enabled = False

    def start(self) -> None:
        self.enabled = True

    def stop(self) -> None:
        self.enabled = False


class ActorAvoidance:
    """Pause near visible actors; does not detect blocks or plan a route."""

    def __init__(self) -> None:
        self.enabled = False

    def start(self) -> None:
        self.enabled = True

    def stop(self) -> None:
        self.enabled = False

    def blocked(self, bot) -> bool:
        if not self.enabled:
            return False
        pos = bot.get_status().position
        return any(
            abs(actor.position.y - pos.y) < 2
            and (actor.position.x - pos.x) ** 2 + (actor.position.z - pos.z) ** 2 < 4
            for actor in bot.get_observed_entities()
        )


class Posture:
    def __init__(self) -> None:
        self.mode: str | None = None
        self._sent_mode: str | None = None

    def start(self, mode: str) -> None:
        if mode not in ("sprint", "sneak"):
            raise ValueError("mode must be sprint or sneak")
        self.mode = mode

    def stop(self) -> None:
        self.mode = None

    def reset(self) -> None:
        self.stop()
        self._sent_mode = None


def submit_input(bot, navigation: Navigation, avoidance: ActorAvoidance, posture: Posture) -> None:
    """The only writer of a complete frame; other behaviors publish intent only."""
    status = bot.get_status()
    blocked = navigation.enabled and avoidance.blocked(bot)
    moving = navigation.enabled and not blocked and status.alive
    if navigation.enabled:
        bot.set_look(0, 0)
    else:
        bot.clear_look()
    # BDS posture transitions require edge flags in addition to held flags.
    active = (
        "sneak" if posture.mode == "sneak" and status.alive
        else "sprint" if posture.mode == "sprint" and moving
        else None
    )
    flags = []
    if posture._sent_mode != active:
        if posture._sent_mode == "sprint":
            flags.append("STOP_SPRINTING")
        elif posture._sent_mode == "sneak":
            flags.extend(("STOP_SNEAKING", "SNEAK_RELEASED_RAW"))
        if active == "sprint":
            flags.append("START_SPRINTING")
        elif active == "sneak":
            flags.extend(("START_SNEAKING", "SNEAK_PRESSED_RAW"))
    if active == "sprint":
        flags.append("SPRINT_DOWN")
    elif active == "sneak":
        flags.extend(("SNEAK_DOWN", "SNEAK_CURRENT_RAW", "WANT_DOWN"))
    bot.set_input(Input(
        position=status.position,
        move=Vec2(y=1 if moving else 0),
        jumping=blocked and status.alive,
        sneaking=active == "sneak",
        sprinting=active == "sprint",
        flags=tuple(flags),
    ))
    posture._sent_mode = active


async def run_once(bot, navigation: Navigation, avoidance: ActorAvoidance, posture: Posture, seconds: float) -> None:
    await bot.wait_ready(60)
    navigation.start()
    avoidance.start()
    posture.start("sprint")
    try:
        await asyncio.sleep(seconds / 2)
        posture.start("sneak")  # Switch one behavior without restarting the others.
        await asyncio.sleep(seconds / 2)
    finally:
        posture.stop()
        avoidance.stop()
        navigation.stop()


def subscribe_behaviors(runtime, bot, navigation: Navigation, avoidance: ActorAvoidance, posture: Posture):
    """A single input writer; subscription ownership remains with the application."""
    def observe(source, event) -> None:
        if source is not bot:
            return
        if isinstance(event, StateEvent) and event.state in (BotState.closing, BotState.closed, BotState.failed):
            navigation.stop()
            avoidance.stop()
            posture.reset()
        elif isinstance(event, TickEvent):
            submit_input(bot, navigation, avoidance, posture)

    return runtime.subscribe(observe, events=event_mask(EventKind.tick) | event_mask(EventKind.state))


async def connect(server_url: str, seconds: float, reconnect: bool = False) -> None:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("seconds must be positive and finite")
    navigation, avoidance, posture = Navigation(), ActorAvoidance(), Posture()
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=server_url,
            transport="raknet" if server_url.startswith("raknet://") else "nethernet",
            protocol=2193,
            name="BehaviorBot",
        ))
        unsubscribe = subscribe_behaviors(runtime, bot, navigation, avoidance, posture)
        try:
            await run_once(bot, navigation, avoidance, posture, seconds)
            if reconnect:
                await bot.reconnect()
                await run_once(bot, navigation, avoidance, posture, seconds)
            await bot.disconnect()
        finally:
            navigation.stop()
            avoidance.stop()
            posture.stop()
            unsubscribe()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--seconds", type=float, default=4)
    parser.add_argument("--reconnect", action="store_true", help="run again after a new Session reaches Ready")
    args = parser.parse_args()
    asyncio.run(
        connect(args.server_url, args.seconds, args.reconnect),
        loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None,
    )


if __name__ == "__main__":
    main()
