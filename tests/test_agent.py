import datetime as dt

from audient.agent import CancelGraph
from audient.nlu import extract, turn_incomplete, clean_text

REF = dt.date(2026, 10, 1)


def test_self_repair_keeps_last_value():
    p = extract("fly from Pune to, uh, Delhi, no wait, Bangalore this Friday", REF)
    cities = [(m.value, m.role) for m in p.mentions if m.etype == "city"]
    assert ("Bangalore", "destination") in cities and ("Delhi", "destination") not in cities


def test_disfluency_normalisation():
    assert clean_text("I- I need to f-f-find flights to to Goa") == "I need to find flights to Goa"
    assert turn_incomplete("I want to fly from Mumbai to, um...")
    assert not turn_incomplete("Chennai on Sunday")


def test_indicator_mention():
    p = extract("wait, no, the red light stopped, now it's blinking blue twice", REF)
    assert [m.value for m in p.mentions if m.etype == "indicator"][-1] == "blue_blinking_2"


def test_cancel_graph_propagates():
    g = CancelGraph()
    g.add("a"); g.add("b", "a"); g.add("c", "b"); g.add("d")
    assert sorted(g.invalidate("a")) == ["a", "b", "c"] and "d" not in g.dead
