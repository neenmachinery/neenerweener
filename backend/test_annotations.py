from annotations import Annotation, Selector, render_with_annotations, resolve, validate_note, validate_selector


def row(number, exact, *, kind="paragraph", index=0, prefix="", suffix=""):
    return Annotation(number, "sample", Selector(kind, index, exact, prefix, suffix), "a note")


def test_resolves_rendered_text_across_emphasis_and_links():
    rendered = render_with_annotations("A **bold** and [linked](https://example.test).", [row(1, "bold and linked", prefix="A ", suffix=".")])
    assert rendered.resolutions[0].status == "resolved"


def test_resolves_lists_blockquotes_tables_and_headings():
    body = "# heading\n\n- first\n- second\n\n> quoted\n\n| left | right |\n| --- | --- |\n| cell | value |"
    rows = [row(1, "heading", kind="heading"), row(2, "second", kind="list_item", index=2), row(3, "quoted", kind="blockquote_paragraph", index=3), row(4, "cell", kind="table_cell", index=4)]
    assert [r.status for r in resolve(body, rows)] == ["resolved"] * 4


def test_changed_and_ambiguous_quotes_are_orphaned():
    assert resolve("one", [row(1, "gone")])[0].reason == "not-found"
    assert resolve("same\n\nsame", [row(1, "same")])[0].reason == "ambiguous"


def test_overlap_chains_mark_every_conflicting_row_from_immutable_ranges():
    rows = [row(1, "ab"), row(2, "bcd"), row(3, "de")]
    assert [result.reason for result in resolve("abcde", rows)] == ["overlap", "overlap", "overlap"]


def test_note_validation_preserves_surrounding_whitespace():
    assert validate_note("  a note  ") == "  a note  "
    try:
        validate_note("a" * 2001)
        assert False
    except ValueError:
        pass


def test_cross_unsupported_content_is_not_eligible():
    body = "`code`\n\n$$x$$\n\n<div>raw</div>\n\ntext"
    assert all(r.status == "orphaned" for r in resolve(body, [row(1, "code"), row(2, "x"), row(3, "raw"), row(4, "text", index=0)]))


def test_validation_rejects_bad_selector_and_note():
    try:
        validate_selector(Selector("paragraph", 0, "x" * 2001))
        assert False
    except ValueError:
        pass
    try:
        validate_note("\x00")
        assert False
    except ValueError:
        pass


def test_note_is_not_rendered_in_pr1():
    result = render_with_annotations("text", [row(1, "text")])
    assert "a note" not in result.html
    assert "<mark" not in result.html
