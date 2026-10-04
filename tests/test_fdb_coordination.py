"""Audient's tool-call coordination layer for the LiveKit agent (fdb_agent/coordination.py), on a simulated clock.

The scenes mirror cases seen in FDB-v3 runs: a premature call during a mid-sentence pause that the user then
corrects (travel_19), a call the user talked over without correcting it (ecommerce_23), and two different
calls to one tool in one request.
"""
import asyncio

from fdb_agent.coordination import REPLACED, ToolGate


class Sim:
    """A fake clock; `events` are (time, fn) pairs fired as simulated time passes."""

    def __init__(self):
        self.t = 0.0
        self.events = []
        self.executed = []

    def clock(self):
        return self.t

    async def until(self, t):
        """Let simulated time run (in small steps, so other tasks keep polling) until it reaches t."""
        while self.t < t:
            await self.sleep(0.05)

    async def sleep(self, dt):
        self.t += dt
        for ev in sorted([e for e in self.events if e[0] <= self.t], key=lambda e: e[0]):
            self.events.remove(ev)
            ev[1]()
        await asyncio.sleep(0)

    def gate(self, **kw):
        def execute(name, args):
            self.executed.append((name, dict(args), round(self.t, 2)))
            return {"status": "success", "name": name, **args}
        return ToolGate(execute, lambda *a: None, clock=self.clock, sleep=self.sleep, **kw)


def test_a_call_made_right_after_the_user_stops_waits_for_the_settle_time():
    s = Sim()
    g = s.gate()
    g.user_started()
    g.user_stopped()  # the user has just finished speaking
    r = asyncio.run(g.call("search_flights", {"destination": "Tokyo", "date": "July 15"}))
    assert r["status"] == "success" and len(s.executed) == 1 and 2.0 <= s.executed[0][2] < 2.1


def test_a_later_step_in_a_chain_runs_at_once_when_the_user_finished_long_ago():
    """3-tool chains: the user stopped 6 s ago; the model's third call must not wait another 2 s."""
    s = Sim()
    g = s.gate()
    g.user_started()
    g.user_stopped()
    s.t = 6.0
    asyncio.run(g.call("book_flight", {"passenger_name": "Casey Lee"}))
    assert s.executed[0][2] < 6.1


def test_a_corrected_premature_call_is_replaced_and_never_runs():
    """travel_19: 'June 1st' ... pause ... 'wait, actually, June 3rd'."""
    s = Sim()
    g = s.gate()

    async def scene():
        g.user_started(); g.user_stopped()  # the user has just paused mid-request
        first = asyncio.ensure_future(g.call("search_flights", {"destination": "Milan", "date": "June 1"}))
        await asyncio.sleep(0)  # the first call is issued at t=0, during the user's pause
        s.events = [(0.8, g.user_started), (1.6, lambda: g.user_said("well, wait, but actually, June 3rd")),
                    (3.0, g.user_stopped)]
        await s.until(3.4)  # the model hears the correction and asks again
        second = await g.call("search_flights", {"destination": "Milan", "date": "June 3"})
        return await first, second

    first, second = asyncio.run(scene())
    assert first == REPLACED and second["date"] == "June 3"
    assert [a["date"] for _, a, _ in s.executed] == ["June 3"] and g.stats["cancelled"] == 1


def test_a_call_the_user_talked_over_without_correcting_it_still_runs():
    """ecommerce_23: the user goes on to a second request; the first call must not be lost."""
    s = Sim()
    g = s.gate()

    async def scene():
        g.user_started(); g.user_stopped()  # the user has just paused mid-request
        s.events = [(0.5, g.user_started), (1.0, lambda: g.user_said("and also, um, add item X1 to my cart")),
                    (4.0, g.user_stopped)]
        return await g.call("track_order", {"order_id": "GG5"})

    r = asyncio.run(scene())
    assert r["status"] == "success" and s.executed[0][0] == "track_order"
    assert s.executed[0][2] >= 6.0 and g.stats == {"executed": 1, "reused": 0, "cancelled": 0, "deferred": 1}


def test_two_different_calls_to_one_tool_without_a_correction_both_run():
    """'track A ... um ... and B': talked over, newer call to the same tool, but nothing was corrected."""
    s = Sim()
    g = s.gate()

    async def scene():
        g.user_started(); g.user_stopped()  # the user has just paused mid-request
        a = asyncio.ensure_future(g.call("track_order", {"order_id": "A1"}))
        await asyncio.sleep(0)
        s.events = [(0.5, g.user_started), (0.8, lambda: g.user_said("um, and B2 as well")), (2.0, g.user_stopped)]
        await s.until(2.2)
        b = await g.call("track_order", {"order_id": "B2"})
        return await a, b

    a, b = asyncio.run(scene())
    assert a["status"] == b["status"] == "success" and g.stats["deferred"] >= 1  # A1 really was talked over
    assert sorted(x["order_id"] for _, x, _ in s.executed) == ["A1", "B2"]


def test_a_correction_without_a_replacement_call_still_runs_the_original():
    s = Sim()
    g = s.gate()

    async def scene():
        g.user_started(); g.user_stopped()  # the user has just paused mid-request
        s.events = [(0.5, g.user_started), (0.9, lambda: g.user_said("no, sorry, that's right")), (1.5, g.user_stopped)]
        return await g.call("get_card_benefits", {"card_type": "platinum"})

    r = asyncio.run(scene())
    assert r["status"] == "success" and len(s.executed) == 1
    assert s.executed[0][2] >= 1.5 + 2.0 + 2.5 - 0.1  # waited for a possible replacement first


def test_an_identical_call_is_never_executed_twice():
    s = Sim()
    g = s.gate()

    async def scene():
        a = await g.call("search_flights", {"destination": "Tokyo", "date": "2026-07-15"})
        b = await g.call("search_flights", {"date": "2026-07-15", "destination": " tokyo "})
        return a, b

    a, b = asyncio.run(scene())
    assert a == b and len(s.executed) == 1 and g.stats["reused"] == 1


def test_identical_calls_arriving_together_share_one_execution():
    s = Sim()
    g = s.gate()

    async def scene():
        return await asyncio.gather(g.call("track_order", {"order_id": "BOB12"}), g.call("track_order", {"order_id": "BOB12"}))

    a, b = asyncio.run(scene())
    assert a == b and len(s.executed) == 1


def test_a_stuck_speaking_signal_never_blocks_a_call_forever():
    s = Sim()
    g = s.gate()
    g.user_started()  # never reported stopping
    r = asyncio.run(g.call("get_card_benefits", {"card_type": "platinum"}))
    assert r["status"] == "success" and 20.0 <= s.executed[0][2] < 20.1
