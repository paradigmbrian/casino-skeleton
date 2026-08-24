import pytest

from agents.config import ROUTES
from agents.specs import SPECS, task_brief
from agents.types import Event


def test_every_route_target_has_a_spec():
    assert set(ROUTES.values()) <= set(SPECS)


def test_every_spec_is_reachable_from_at_least_one_route():
    assert set(SPECS) == set(ROUTES.values())


def test_spec_triggers_agree_with_the_routing_table():
    for event_type, worker in ROUTES.items():
        assert event_type in SPECS[worker].triggers, f"{worker} missing trigger {event_type}"


def test_write_scopes_are_pairwise_disjoint():
    """The core invariant: no two workers can touch the same path."""
    def collides(a, b):
        for x in a:
            for y in b:
                if x.endswith("/") and y.endswith("/") and (x.startswith(y) or y.startswith(x)):
                    return True
                if x.endswith("/") and y.startswith(x):
                    return True
                if y.endswith("/") and x.startswith(y):
                    return True
                if x == y:
                    return True
        return False

    names = sorted(SPECS)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not collides(SPECS[a].write_scope, SPECS[b].write_scope), f"{a} overlaps {b}"


def test_test_author_cannot_write_source_and_reviewer_cannot_write_tests():
    from agents.scope import in_scope
    assert not in_scope("casino/table.py", SPECS["test-author"].write_scope)
    assert not in_scope("tests/test_table.py", SPECS["reviewer"].write_scope)


def test_investigator_is_diagnose_only():
    assert SPECS["anomaly-investigator"].write_scope == ("docs/investigations/",)
    assert "Edit" not in SPECS["anomaly-investigator"].allowed_tools


def test_every_prompt_states_its_write_scope():
    for name, spec in SPECS.items():
        for entry in spec.write_scope:
            assert entry in spec.system_prompt, f"{name} prompt does not mention {entry}"


@pytest.mark.parametrize("event_type", sorted(ROUTES))
def test_task_brief_is_non_empty_for_every_routed_event(event_type):
    brief = task_brief(Event(type=event_type, payload={"module": "casino/table.py",
                                                       "sha": "abc1234", "package": "requests",
                                                       "pinned": "2.6.0", "latest": "2.32.3",
                                                       "z": 4.1, "detail": "x"}))
    assert brief.strip()
