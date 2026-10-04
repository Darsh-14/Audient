"""Audient's tool-call coordination layer for the LiveKit agent (fdb_agent/coordination.py), on a simulated clock."""
import asyncio

from fdb_agent.coordination import CANCELLED, ToolGate


class Sim:
    """A fake clock; `at` schedules user speech events at simulated times."""

    def __init__(self):
        self.t = 0.0
        self.events = []  # (time, fn)
        self.executed = []
        self.logged = []

    def clock(self):
        return self.t

    async def sleep(self, dt):
        self.t += dt
        for ev in [e for e in self.events if e[0] <= self.t]:
            self.events.remove(ev)
            ev[1]()
        await asyncio.sleep(0)

    def gate(self, **kw):
        def execute(name, args):
            self.executed.append((name, dict(args)))
            return {"status": "success", "name": name, **args}
        return ToolGate(execute, lambda n, a, t0, t1: self.logged.append((n, a)), clock=self.clock, sleep=self.sleep, **kw)


def run(coro):
    return asyncio.run(coro)


def test_call_runs_after_the_user_has_been_quiet_for_the_settle_time():
    s = Sim()
    g = s.gate()
    r = run(g.call("search_flights", {"destination": "Berlin", "date": "September 3"}))
    assert r["status"] == "success" and s.executed == [("search_flights", {"destination": "Berlin", "date": "September 3"})]
    assert s.logged == s.executed and 0.8 <= s.t < 0.9


def test_a_call_made_during_a_pause_is_cancelled_when_the_user_goes_on():
    s = Sim()
    g = s.gate()
    s.events = [(0.4, g.user_started), (1.5, g.user_stopped)]  # "Paris... actually, no, make that Berlin"
    r = run(g.call("search_flights", {"destination": "Paris"}))
    assert r == CANCELLED and s.executed == [] and s.logged == [] and g.stats["cancelled"] == 1


def test_the_corrected_call_after_the_user_finishes_runs_once():
    s = Sim()
    g = s.gate()

    async def scene():
        s.events = [(0.4, g.user_started), (1.5, g.user_stopped)]
        first = await g.call("search_flights", {"destination": "Paris"})
        second = await g.call("search_flights", {"destination": "Berlin"})
        return first, second

    first, second = run(scene())
    assert first == CANCELLED and second["destination"] == "Berlin"
    assert s.executed == [("search_flights", {"destination": "Berlin"})]


def test_an_identical_call_is_never_executed_twice():
    s = Sim()
    g = s.gate()

    async def scene():
        a = await g.call("search_flights", {"destination": "Tokyo", "date": "2026-07-15"})
        b = await g.call("search_flights", {"date": "2026-07-15", "destination": " tokyo "})  # same call, other spelling
        return a, b

    a, b = run(scene())
    assert a == b and len(s.executed) == 1 and g.stats == {"executed": 1, "reused": 1, "cancelled": 0}


def test_identical_calls_arriving_together_share_one_execution():
    s = Sim()
    g = s.gate()

    async def scene():
        return await asyncio.gather(g.call("track_order", {"order_id": "BOB12"}), g.call("track_order", {"order_id": "BOB12"}))

    a, b = run(scene())
    assert a == b and s.executed == [("track_order", {"order_id": "BOB12"})]


def test_different_arguments_are_separate_calls():
    s = Sim()
    g = s.gate()

    async def scene():
        return await asyncio.gather(g.call("track_order", {"order_id": "BOB12"}), g.call("track_order", {"order_id": "BOB13"}))

    run(scene())
    assert sorted(a["order_id"] for _, a in s.executed) == ["BOB12", "BOB13"]


def test_a_stuck_speaking_signal_never_blocks_a_call_forever():
    s = Sim()
    g = s.gate()
    g.user_started()  # started before the call and never reported stopping
    r = run(g.call("get_card_benefits", {"card_type": "platinum"}))
    assert r["status"] == "success" and 6.0 <= s.t < 6.1


def test_cancelled_calls_are_not_remembered_as_done():
    s = Sim()
    g = s.gate()

    async def scene():
        s.events = [(0.3, g.user_started), (0.6, g.user_stopped)]
        first = await g.call("add_to_cart", {"product_id": "PROD1", "quantity": 2})
        again = await g.call("add_to_cart", {"product_id": "PROD1", "quantity": 2})  # the user confirmed it after all
        return first, again

    first, again = run(scene())
    assert first == CANCELLED and again["status"] == "success" and len(s.executed) == 1
