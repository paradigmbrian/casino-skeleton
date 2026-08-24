import logging

from agents.logfmt import EventFormatter, event_kwargs


def record(msg="hello", level=logging.INFO, **extra):
    rec = logging.LogRecord("agents.test", level, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def plain():
    return EventFormatter(colour=False)


def test_a_structured_record_renders_aligned_columns():
    out = plain().format(record(**event_kwargs("merged", "reviewer",
                                               "$0.72   18t  docs/reviews/dd43412.md")))
    assert "MERGED" in out
    assert "reviewer" in out
    assert "docs/reviews/dd43412.md" in out
    assert out.index("reviewer") < out.index("$0.72")


def test_kinds_line_up_across_records():
    fmt = plain()
    lines = [fmt.format(record(**event_kwargs(k, "reviewer", "x")))
             for k in ("dispatch", "start", "sensor", "merged")]
    columns = {line.index("reviewer") for line in lines}
    assert len(columns) == 1, "actor column is not aligned across kinds"


def test_an_unstructured_record_still_renders_readably():
    out = plain().format(record("something a library logged", level=logging.WARNING))
    assert "something a library logged" in out


def test_colour_is_off_when_not_requested_and_on_when_it_is():
    rec_kwargs = event_kwargs("merged", "reviewer", "x")
    assert "\033[" not in plain().format(record(**rec_kwargs))
    assert "\033[" in EventFormatter(colour=True).format(record(**rec_kwargs))


def test_an_unknown_kind_does_not_blow_up():
    out = plain().format(record(**event_kwargs("something-new", "worker", "detail")))
    assert "something-new" in out
    assert "detail" in out


def test_event_kwargs_is_a_plain_extra_dict():
    assert event_kwargs("merged", "reviewer", "x") == {
        "extra": {"kind": "merged", "actor": "reviewer", "detail": "x"}
    }["extra"]
