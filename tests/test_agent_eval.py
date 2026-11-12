import re

from txfr.agent_eval import _bill_rx, _money_rx, check


def norm(t):
    return re.sub(r"(?<=\d),(?=\d)", "", t)


def test_bill_regex():
    rx = _bill_rx("89R:HB500")
    assert re.search(rx, "HB 500 was", re.I) and re.search(rx, "(H.B. 500)", re.I) and re.search(rx, "hb500", re.I)
    assert not re.search(rx, "HB 5000", re.I)
    assert re.search(_bill_rx("89R:SB2"), "SB 2,", re.I) and not re.search(_bill_rx("89R:SB2"), "SB 22", re.I)


def test_money_regex():
    rx = _money_rx(193504000.0)
    for t in ["$193.5 million", "$193,504,000", "193.50M", "about $194 million"]:
        assert re.search(rx, norm(t), re.I), t
    assert not re.search(rx, norm("$93.5 million"), re.I)
    rb = _money_rx(2743461146.0)
    for t in ["$2.74 billion", "$2.7 billion", "$2,743,461,146", "2.74bn"]:
        assert re.search(rb, norm(t), re.I), t


def test_check_forbid_and_sets():
    case = {"q": "", "sets": [["1213"]], "forbid": [r"\$\s?\d"]}
    assert check(case, "1,213 bills passed")
    assert not check(case, "1,213 bills costing $5")
    assert check({"any_of": ["can't predict"]}, "I can't predict that.")
