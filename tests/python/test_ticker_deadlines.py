import asyncio
import dataclasses
import math
from types import SimpleNamespace

import pytest

import endpilot as api
from endpilot import session as session_module


class Clock:
    def __init__(self, *, delays=(), wakes=()):
        self.now = 1000.0
        self.delays = iter(delays)
        self.wakes = iter(wakes)
        self.waits = []
        self.session = None

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.waits.append(delay)
        wake = next(self.wakes, None)
        if wake is not None:
            self.now = wake
        else:
            lateness = next(self.delays, None)
            if lateness is None:
                self.session.active = False
            else:
                self.now += delay + lateness
        await asyncio.sleep(0)


def ticker_session(monkeypatch, clock, *, count, work=0, protocol=2193):
    events, packets, serviced = [], [], []
    runtime = SimpleNamespace(_loop=clock, _stopping=False)
    runtime._emit = lambda _bot, _kind, factory: events.append(factory())
    bot = SimpleNamespace(_runtime=runtime, _generation=1,
                          _options=api.BotOptions(server_url="http://unused", protocol=protocol),
                          _token=api.BotToken())
    session = session_module.Session(bot, 1, asyncio.get_running_loop().create_future())
    session.status = dataclasses.replace(session.status, state=api.BotState.ready)
    session.negotiation.login = True
    session.next_tick = clock.time() + 0.05
    session.enqueue = lambda packet, **_kwargs: packets.append(packet)
    original_tick = session.tick

    def tick():
        serviced.append((clock.time(), session.next_tick))
        original_tick()
        clock.now += work
        if len(serviced) == count:
            session.active = False

    session.tick = tick
    clock.session = session
    monkeypatch.setattr(session_module, "asyncio", SimpleNamespace(sleep=clock.sleep))
    return session, events, packets, serviced


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_repeated_wakeup_and_work_delay_preserves_100_nominal_opportunities(monkeypatch, protocol):
    clock = Clock(delays=[0.011] * 100)
    session, events, packets, serviced = ticker_session(monkeypatch, clock, count=100, work=0.0005, protocol=protocol)
    first_deadline = session.next_tick
    await session.ticker()
    assert len(clock.waits) == len(serviced) == len(events) == len(packets) == 100
    assert [packet.client_tick for packet in packets] == list(range(1, 101))
    assert [event.tick for event in events] == list(range(1, 101))
    for index, (now, following) in enumerate(serviced):
        due = first_deadline + index * 0.05
        assert now == pytest.approx(due + 0.011, rel=0, abs=1e-9)
        assert following == pytest.approx(due + 0.05, rel=0, abs=1e-9)
        assert following > now
    previous = first_deadline
    skipped = 0
    for _, following in serviced:
        skipped += round((following - previous) / 0.05) - 1
        previous = following
    assert skipped == 0
    assert clock.now < first_deadline + 100 * 0.05


async def test_late_wakeup_skips_whole_periods_without_replaying_frames(monkeypatch):
    clock = Clock(delays=[0.210, 0])
    session, events, packets, serviced = ticker_session(monkeypatch, clock, count=2, work=0.0005)
    first_deadline = session.next_tick
    await session.ticker()
    assert len(serviced) == len(events) == len(packets) == len(clock.waits) == 2
    assert [packet.client_tick for packet in packets] == [1, 2]
    assert round((serviced[0][1] - first_deadline) / 0.05) - 1 == 4
    assert serviced[0][1] > serviced[0][0]
    assert events[0].lateness_us == pytest.approx(210000, abs=1)
    assert clock.waits[1] == pytest.approx(0.0395, rel=0, abs=1e-9)
    assert serviced[1][0] == serviced[0][1]
    assert serviced[1][1] > serviced[1][0]


@pytest.mark.parametrize(("now", "steps"), [
    (math.nextafter(1000.0, -math.inf), 0), (1000.0, 1), (math.nextafter(1000.0, math.inf), 1),
    (math.nextafter(1000.05, -math.inf), 1), (1000.05, 2), (math.nextafter(1000.05, math.inf), 2),
])
async def test_boundary_never_emits_early_and_serviced_deadline_is_strictly_future(monkeypatch, now, steps):
    clock = Clock(wakes=[now])
    clock.now = 999.95
    session, events, packets, serviced = ticker_session(monkeypatch, clock, count=1)
    session.next_tick = 1000.0
    await session.ticker()
    assert len(events) == len(packets) == len(serviced) == int(steps > 0)
    assert session.next_tick == pytest.approx(1000.0 + steps * 0.05, rel=0, abs=1e-10)
    if steps:
        assert session.next_tick > now
    else:
        assert session.next_tick == 1000.0


async def test_ready_retains_first_deadline_50ms_after_ready_and_pre_ready_wait(monkeypatch):
    clock = Clock()
    session, events, packets, serviced = ticker_session(monkeypatch, clock, count=1)
    session.next_tick = None
    session.status = dataclasses.replace(session.status, state=api.BotState.spawning, chunk_radius=1)
    session.negotiation.spawn_deadline = clock.now + 10
    session.negotiation.start_game = session.negotiation.spawn = True
    ready_at = []

    async def flush():
        pass

    async def sleep(delay):
        clock.waits.append(delay)
        clock.now += delay
        if len(clock.waits) == 1:
            await session.try_ready()
            ready_at.append(clock.now)
            assert not serviced
        await asyncio.sleep(0)

    session.flush = flush
    monkeypatch.setattr(session_module, "asyncio", SimpleNamespace(sleep=sleep))
    await session.ticker()
    assert session.ready.done()
    assert clock.waits == pytest.approx([0.05, 0.05], rel=0, abs=1e-10)
    assert serviced[0][0] == pytest.approx(ready_at[0] + 0.05, rel=0, abs=1e-10)
    assert [event.tick for event in events if isinstance(event, api.TickEvent)] == [1]
    assert [packet.client_tick for packet in packets if packet.packet_id == 144] == [1]


@pytest.mark.parametrize("interruption", ["cancel", "generation"])
@pytest.mark.parametrize("overdue", [False, True])
async def test_wait_is_cancellable_and_generation_invalidation_cannot_emit_stale_frame(monkeypatch, interruption, overdue):
    clock = Clock()
    session, events, packets, serviced = ticker_session(monkeypatch, clock, count=1)
    if overdue:
        session.next_tick = clock.now - 0.1
    entered, release = asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        clock.waits.append(delay)
        entered.set()
        await release.wait()
        clock.now += delay

    monkeypatch.setattr(session_module, "asyncio", SimpleNamespace(sleep=sleep))
    task = asyncio.create_task(session.ticker())
    await entered.wait()
    if interruption == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        session.bot._generation += 1
        release.set()
        await task
    assert task.done()
    assert clock.waits == pytest.approx([0 if overdue else 0.05], rel=0, abs=1e-10)
    assert events == packets == serviced == []
