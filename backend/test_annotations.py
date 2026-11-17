import concurrent.futures
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

import app
from annotations import Annotation, AnnotationStore, Selector, _blocks, post_annotations, render_with_annotations, resolve, validate_note, validate_selector


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
    assert [result.reason for result in resolve("abcde", [row(1, "ab"), row(2, "bcd"), row(3, "de")])] == ["overlap"] * 3


def test_note_validation_preserves_surrounding_whitespace():
    assert validate_note("  a note  ") == "  a note  "
    with pytest.raises(ValueError):
        validate_note("a" * 2001)


def test_unsupported_content_is_not_eligible():
    body = "`code`\n\ntext"
    results = resolve(body, [row(1, "code"), row(2, "text", index=0)])
    assert [result.reason for result in results] == ["not-found", None]


def test_eligible_text_around_unsupported_inline_child_keeps_block_offsets():
    body = "allowed `code` tail"
    results = resolve(body, [row(1, "allowed"), row(2, "tail"), row(3, "allowed `code` tail")])
    assert [result.status for result in results] == ["resolved", "resolved", "orphaned"]
    assert results[2].reason == "not-found"


def test_inline_segments_use_canonical_cursor_for_fixture_matrix():
    fixtures = [
        ("allowed `code` tail", "allowed tail"),
        ("`code` tail", "tail"),
        ("allowed `code`", "allowed"),
        ("one `code` two ![image](x) three", "one two three"),
        ("one  two\nthree", "one two three"),
        ("one  \ntwo", "one two"),
        ("one &amp; two&nbsp;three", "one & two three"),
        ("*one* [two](x) `code` three", "one two three"),
        ("# heading `code` tail", "heading tail"),
    ]
    for body, canonical in fixtures:
        _, blocks = _blocks(body)
        assert len(blocks) == 1
        block = blocks[0]
        assert block.text == canonical
        for segment, start in block.segments:
            end = start + len(segment)
            assert 0 <= start <= end <= len(canonical)
            assert canonical[start:end] == segment


def test_inline_segment_ranges_after_unsupported_child_are_exact():
    _, blocks = _blocks("allowed `code` tail")
    assert blocks[0].segments == [("allowed", 0), ("tail", 8)]


def test_context_uses_canonical_text_but_selection_stays_segment_local():
    body = "left `x` target\n\ntarget `x` right"
    prefix = resolve(body, [row(1, "target", index=0, prefix="left ")])[0]
    suffix = resolve(body, [row(2, "target", index=1, suffix=" right")])[0]
    cross = resolve("left `x` target", [row(3, "left x target")])[0]
    assert (prefix.status, prefix.start, prefix.end) == ("resolved", 5, 11)
    assert (suffix.status, suffix.start, suffix.end) == ("resolved", 0, 6)
    assert cross.reason == "not-found"


@pytest.mark.parametrize("body, inside, outside", [
    ("alpha <i>inside</i> omega", "inside", ("alpha", "omega")),
    ("<b><i>x</i></b>", "x", ()),
    ("alpha <br/><i>inside</i><img src='x'> omega", "inside", ("alpha", "omega")),
    ("alpha <!-- hidden --> omega", "hidden", ("alpha", "omega")),
    ("alpha <i>inside", "inside", ("alpha",)),
    ("*alpha <i>inside</i> omega*", "inside", ("alpha", "omega")),
    ("[alpha <i>inside</i> omega](x)", "inside", ("alpha", "omega")),
])
def test_inline_raw_html_makes_the_entire_block_not_annotatable(body, inside, outside):
    assert _blocks(body)[1] == []
    for number, text_value in enumerate((inside, *outside), 1):
        assert resolve(body, [row(number, text_value)])[0].reason == "not-found"


@pytest.mark.parametrize("body", [
    "alpha <div>one <div>two</div> three</div> omega",
    'alpha <span title="a > b">inside</span> omega',
    "alpha <!-- hidden --> omega",
    "alpha <br> omega",
    "alpha <i>inside",
])
def test_inline_raw_html_blocks_refuse_every_selection(body):
    for text_value in ("alpha", "inside", "omega", "one", "two", "three", "hidden"):
        assert resolve(body, [row(1, text_value)])[0].reason == "not-found"


def test_autolinks_are_not_raw_html_and_keep_surrounding_text_annotatable():
    body = "alpha <https://example.com> omega"
    assert [result.status for result in resolve(body, [row(1, "alpha"), row(2, "omega")])] == ["resolved", "resolved"]


@pytest.mark.parametrize("seed", range(10))
def test_lock_order_is_sorted_for_every_hash_seed(seed):
    import subprocess
    script = '''
from annotations import AnnotationStore
class Engine:
    class Context:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def begin(self): return self
        dialect = type("Dialect", (), {"name": "sqlite"})()
    def begin(self): return self.Context()
store = AnnotationStore("sqlite://", engine=Engine())
calls = []
store._lock = lambda connection, slug: calls.append(slug)
store._write({"alpha", "zeta"}, lambda connection: None)
assert calls == ["alpha", "zeta"]
'''
    result = subprocess.run(["uv", "run", "python", "-c", script], env={**os.environ, "PYTHONHASHSEED": str(seed)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_lock_order_is_sorted():
    class Engine:
        class Context:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def begin(self): return self
            dialect = type("Dialect", (), {"name": "sqlite"})()
        def begin(self): return self.Context()
    store = AnnotationStore("sqlite://", engine=Engine())
    calls = []
    store._lock = lambda connection, slug: calls.append(slug)
    store._write(["zeta", "alpha", "zeta"], lambda connection: None)
    assert calls == ["alpha", "zeta"]


def test_validation_rejects_bad_selector_and_note():
    with pytest.raises(ValueError):
        validate_selector(Selector("paragraph", 0, "x" * 2001))
    with pytest.raises(ValueError):
        validate_note("\x00")


def test_resolved_note_is_rendered_for_readers():
    result = render_with_annotations("text", [row(1, "text")])
    assert 'class="sidenote"' in result.html
    assert "a note" in result.html


@pytest.fixture
def annotation_db():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured")
    store = AnnotationStore(url)
    post_annotations.metadata.create_all(store.engine)
    with store.engine.begin() as connection:
        connection.execute(delete(post_annotations))
    yield store
    with store.engine.begin() as connection:
        connection.execute(delete(post_annotations))
    store.close()


@pytest.mark.parametrize(
    "values",
    [
        {"post_slug": "BAD", "block_kind": "paragraph", "block_index": 0, "exact": "x", "note_text": "note"},
        {"post_slug": "sample", "block_kind": "bad", "block_index": 0, "exact": "x", "note_text": "note"},
        {"post_slug": "sample", "block_kind": "paragraph", "block_index": -1, "exact": "x", "note_text": "note"},
        {"post_slug": "sample", "block_kind": "paragraph", "block_index": 0, "exact": "x" * 2001, "note_text": "note"},
        {"post_slug": "sample", "block_kind": "paragraph", "block_index": 0, "exact": "x", "prefix": "x" * 65, "note_text": "note"},
        {"post_slug": "sample", "block_kind": "paragraph", "block_index": 0, "exact": "x", "note_text": "  "},
        {"post_slug": "sample", "block_kind": "paragraph", "block_index": 0, "exact": "x", "note_text": "x" * 2001},
    ],
)
def test_postgres_constraints_reject_invalid_rows(annotation_db, values):
    with pytest.raises(Exception):
        with annotation_db.engine.begin() as connection:
            connection.execute(post_annotations.insert().values(**values))


def test_postgres_create_and_repair_round_trip(annotation_db):
    created = annotation_db.create("sample", Selector("paragraph", 0, "old"), " note ", "old")
    repaired = annotation_db.repair(created.id, "sample", Selector("paragraph", 0, "new"), "new")
    assert repaired.id == created.id
    assert annotation_db.list_for_post("sample")[0].selector.exact == "new"
    assert annotation_db.list_for_post("sample")[0].note_text == " note "


def test_postgres_repair_can_move_annotation_to_changed_slug(annotation_db):
    created = annotation_db.create("sample", Selector("paragraph", 0, "old"), " note ", "old")
    repaired = annotation_db.repair(created.id, "other", Selector("paragraph", 0, "new"), "new")
    assert repaired.post_slug == "other"
    assert annotation_db.list_for_post("sample") == []
    assert annotation_db.list_for_post("other")[0].note_text == " note "


def test_postgres_repair_put_same_and_changed_slug(annotation_db, monkeypatch):
    created = annotation_db.create("sample", Selector("paragraph", 0, "old"), " note ", "old")
    monkeypatch.setattr(app, "annotation_store", annotation_db)
    monkeypatch.setattr(app, "post_catalog", _Catalog())
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: app.Access(None)
    try:
        with TestClient(app.app) as client:
            same = client.put(f"/api/admin/annotations/{created.id}/anchor", json={"post_slug":"sample", "selector":{"block_kind":"paragraph", "block_index":0, "exact":"text"}})
            moved = client.put(f"/api/admin/annotations/{created.id}/anchor", json={"post_slug":"other", "selector":{"block_kind":"paragraph", "block_index":0, "exact":"text"}})
        assert same.status_code == 200
        assert moved.status_code == 200
        assert moved.json()["post_slug"] == "other"
        assert moved.json()["note_text"] == " note "
    finally:
        app.app.dependency_overrides.clear()


def test_postgres_overlapping_creates_are_serialized(annotation_db):
    selectors = [Selector("paragraph", 0, "abc"), Selector("paragraph", 0, "bcd")]
    def create(selector):
        return annotation_db.create("sample", selector, "note", "abcd")
    def safe_create(selector):
        try:
            return create(selector)
        except ValueError as exc:
            return exc
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(safe_create, selectors))
    assert sum(result is not None and not isinstance(result, Exception) for result in results) == 1
    assert [str(result) for result in results if isinstance(result, Exception)] == ["overlap"]


def test_postgres_overlap_chain_orphans_all_three(annotation_db):
    with annotation_db.engine.begin() as connection:
        connection.execute(post_annotations.insert(), [{"post_slug": "sample", "block_kind": "paragraph", "block_index": 0, "exact": exact, "note_text": "note"} for exact in ("ab", "bcd", "de")])
    assert [r.reason for r in resolve("abcde", annotation_db.list_for_post("sample"))] == ["overlap"] * 3


class _Catalog:
    def __init__(self, body="text"):
        self.body = body
    def get_public(self, slug):
        return {"slug": slug, "body": self.body, "_annotation_source": True}


class _FakeStore:
    def __init__(self, error=None):
        self.error = error
        self.rows = []
    def list_for_post(self, slug): return self.rows
    def list_admin(self): return self.rows
    def create(self, slug, selector, note_text, body):
        if self.error: raise ValueError(self.error)
        row = Annotation(1, slug, selector, note_text)
        self.rows = [row]
        return row
    def repair(self, annotation_id, slug, selector, body):
        if self.error: raise ValueError(self.error)
        row = Annotation(annotation_id, slug, selector, "existing note")
        self.rows = [row]
        return row
    def update_note(self, annotation_id, note_text): return None
    def delete(self, annotation_id): return False
    def close(self): pass


@pytest.fixture
def annotation_api(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(app, "annotation_store", store)
    monkeypatch.setattr(app, "post_catalog", _Catalog())
    app.app.dependency_overrides[app.require_browser_read] = lambda: app.Access(None)
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: app.Access(None)
    yield store
    app.app.dependency_overrides.clear()


def test_annotation_api_unauthenticated_read_and_write_are_401(monkeypatch):
    monkeypatch.setattr(app, "auth_store", None)
    with TestClient(app.app) as client:
        assert client.get("/api/admin/annotations").status_code == 401
        assert client.post("/api/admin/annotations", json={}).status_code == 401


def test_annotation_api_mutation_requires_csrf(monkeypatch):
    monkeypatch.setattr(app, "auth_store", None)
    with TestClient(app.app) as client:
        assert client.post("/api/admin/annotations", json={}).status_code == 401


@pytest.mark.parametrize("error, detail", [("not-found", "selection is not annotatable"), ("ambiguous", "selection is ambiguous"), ("overlap", "annotation overlaps another annotation")])
def test_annotation_api_has_stable_domain_errors(monkeypatch, error, detail):
    store = _FakeStore(error)
    monkeypatch.setattr(app, "annotation_store", store)
    monkeypatch.setattr(app, "post_catalog", _Catalog())
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: app.Access(None)
    try:
        with TestClient(app.app) as client:
            response = client.post("/api/admin/annotations", json={"post_slug":"sample", "selector":{"block_kind":"paragraph", "block_index":0, "exact":"text"}, "note_text":"note"})
        assert response.status_code == 422
        assert response.json()["detail"] == detail
    finally:
        app.app.dependency_overrides.clear()


def test_annotation_api_store_outage_is_503(monkeypatch):
    monkeypatch.setattr(app, "annotation_store", None)
    app.app.dependency_overrides[app.require_browser_read] = lambda: app.Access(None)
    try:
        with TestClient(app.app) as client:
            assert client.get("/api/admin/annotations").status_code == 503
    finally:
        app.app.dependency_overrides.clear()


def test_annotation_api_delete_store_outage_is_503(monkeypatch):
    class Store(_FakeStore):
        def delete(self, annotation_id): raise RuntimeError("database down")
    monkeypatch.setattr(app, "annotation_store", Store())
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: app.Access(None)
    try:
        with TestClient(app.app) as client:
            assert client.delete("/api/admin/annotations/1").status_code == 503
    finally:
        app.app.dependency_overrides.clear()


def test_annotation_api_note_is_stored_exactly_as_sent(annotation_api):
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: app.Access(None)
    try:
        with TestClient(app.app) as client:
            response = client.post("/api/admin/annotations", json={"post_slug":"sample", "selector":{"block_kind":"paragraph", "block_index":0, "exact":"text"}, "note_text":"  exact  "})
        assert response.status_code == 201
        assert annotation_api.rows[0].note_text == "  exact  "
    finally:
        app.app.dependency_overrides.clear()


def test_annotation_api_missing_post_update_is_orphaned(monkeypatch):
    class Store(_FakeStore):
        def update_note(self, annotation_id, note_text): return Annotation(annotation_id, "missing", Selector("paragraph", 0, "text"), note_text)
    class Catalog(_Catalog):
        def get_public(self, slug):
            return {"slug": slug, "body": "text", "_annotation_source": True} if slug != "missing" else None
    monkeypatch.setattr(app, "annotation_store", Store())
    monkeypatch.setattr(app, "post_catalog", Catalog())
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: app.Access(None)
    try:
        with TestClient(app.app) as client:
            response = client.patch("/api/admin/annotations/1", json={"note_text":"note"})
        assert response.status_code == 200
        assert response.json()["reason"] == "missing-post"
    finally:
        app.app.dependency_overrides.clear()


def test_annotation_api_repair_preserves_note_and_moves_slug(monkeypatch):
    class Store(_FakeStore):
        def repair(self, annotation_id, slug, selector, body):
            return Annotation(annotation_id, slug, selector, "kept note")
    monkeypatch.setattr(app, "annotation_store", Store())
    monkeypatch.setattr(app, "post_catalog", _Catalog())
    app.app.dependency_overrides[app.require_browser_mutation] = lambda: app.Access(None)
    try:
        with TestClient(app.app) as client:
            response = client.put("/api/admin/annotations/1/anchor", json={"post_slug":"sample", "selector":{"block_kind":"paragraph", "block_index":0, "exact":"text"}})
        assert response.status_code == 200
        assert response.json()["note_text"] == "kept note"
    finally:
        app.app.dependency_overrides.clear()


def test_real_fixture_edit_preserves_and_orphans_annotations():
    before = [row(1, "keep"), row(2, "remove")]
    after = resolve("keep\n\nchanged", before)
    assert after[0].status == "resolved"
    assert after[1].reason == "not-found"
