"""Answer cache policy: which answers may be served to everyone who asks the same question today."""
from txfr.cache_policy import cacheable

OK = "RunStatus.COMPLETED"


def g(pct=None, ungrounded=None, checked=True):
    return {"guard": {"groundedness": {"checked": checked, "ungrounded": ungrounded, "ungrounded_pct": pct}}}


def test_grounded_answer_is_cached():
    assert cacheable({"status": OK, **g(0.0, False)})


def test_unchecked_answer_is_cached():          # refusals and figure-free answers skip the check
    assert cacheable({"status": OK, **g(checked=False)})
    assert cacheable({"status": "refused_by_policy"})


def test_flagged_answer_is_not_cached():         # the 70%-flagged SB 2206 answer from 2026-11-11
    assert not cacheable({"status": OK, **g(0.70, True)})


def test_small_flag_is_tolerated():
    assert cacheable({"status": OK, **g(0.10, True)})
    assert not cacheable({"status": OK, **g(0.10, True)}, max_ungrounded=0.05)


def test_failed_or_errored_runs_are_not_cached():
    assert not cacheable({"status": "RunStatus.FAILED"})
    assert not cacheable({"status": OK, "error": "rate_limit_exceeded"})
