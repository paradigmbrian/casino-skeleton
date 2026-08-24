from agents.scope import in_scope, out_of_scope

TESTS_ONLY = ("tests/",)
DEPS_ONLY = ("requirements.txt", "docs/dependencies.md")


def test_directory_prefix_matches_nested_paths():
    assert in_scope("tests/test_table.py", TESTS_ONLY)
    assert in_scope("tests/unit/test_deep.py", TESTS_ONLY)


def test_directory_prefix_does_not_match_a_sibling_with_the_same_stem():
    assert not in_scope("tests_extra/test_x.py", TESTS_ONLY)


def test_exact_file_entries_match_only_themselves():
    assert in_scope("requirements.txt", DEPS_ONLY)
    assert not in_scope("agents/requirements.txt", DEPS_ONLY)


def test_source_is_out_of_scope_for_the_test_author():
    assert not in_scope("casino/table.py", TESTS_ONLY)


def test_traversal_and_absolute_paths_are_always_out_of_scope():
    assert not in_scope("../secrets.env", TESTS_ONLY)
    assert not in_scope("tests/../casino/table.py", TESTS_ONLY)
    assert not in_scope("/etc/passwd", ("/",))


def test_out_of_scope_returns_only_the_offenders():
    changed = ["tests/test_a.py", "casino/table.py", "README.md"]
    assert out_of_scope(changed, TESTS_ONLY) == ["casino/table.py", "README.md"]
