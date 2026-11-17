from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

import sqlalchemy as sa
from timeline import CONNECT_TIMEOUT_SECONDS, POOL_TIMEOUT_SECONDS, STATEMENT_TIMEOUT_MS, database_engine
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from anchors import add_heading_anchors
from sqlalchemy.engine import Engine

MAX_EXACT = 2000
MAX_CONTEXT = 64
MAX_NOTE = 2000
SLUG = re.compile(r"^[a-z0-9-]{1,100}$")
BLOCK_KINDS = {"heading", "paragraph", "list_item", "blockquote_paragraph", "table_cell"}

metadata = sa.MetaData()
post_annotations = sa.Table(
    "post_annotations", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("post_slug", sa.Text, nullable=False),
    sa.Column("block_kind", sa.Text, nullable=False),
    sa.Column("block_index", sa.Integer, nullable=False),
    sa.Column("exact", sa.Text, nullable=False),
    sa.Column("prefix", sa.Text, nullable=False, server_default=""),
    sa.Column("suffix", sa.Text, nullable=False, server_default=""),
    sa.Column("note_text", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("post_slug ~ '^[a-z0-9-]{1,100}$'", name="post_annotations_slug_check"),
    sa.CheckConstraint("block_kind IN ('heading','paragraph','list_item','blockquote_paragraph','table_cell')", name="post_annotations_kind_check"),
    sa.CheckConstraint("block_index >= 0", name="post_annotations_index_check"),
    sa.CheckConstraint("length(exact) BETWEEN 1 AND 2000", name="post_annotations_exact_check"),
    sa.CheckConstraint("length(prefix) <= 64 AND length(suffix) <= 64", name="post_annotations_context_check"),
    sa.CheckConstraint("length(note_text) BETWEEN 1 AND 2000 AND length(trim(note_text)) >= 1", name="post_annotations_note_check"),
)
sa.Index("ix_post_annotations_slug_id", post_annotations.c.post_slug, post_annotations.c.id)


@dataclass(frozen=True)
class Selector:
    block_kind: str
    block_index: int
    exact: str
    prefix: str = ""
    suffix: str = ""

@dataclass(frozen=True)
class Annotation:
    id: int
    post_slug: str
    selector: Selector
    note_text: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

@dataclass(frozen=True)
class Resolution:
    annotation_id: int | None
    start: int | None
    end: int | None
    status: str
    reason: str | None = None
    block_index: int | None = None

@dataclass(frozen=True)
class RenderedPost:
    html: str
    resolutions: tuple[Resolution, ...]

class AnnotationStoreProtocol(Protocol):
    def list_for_post(self, slug: str) -> list[Annotation]: ...
    def list_admin(self) -> list[Annotation]: ...
    def create(self, slug: str, selector: Selector, note_text: str, body: str) -> Annotation: ...
    def repair(self, annotation_id: int, slug: str, selector: Selector, body: str) -> Annotation | None: ...
    def update_note(self, annotation_id: int, note_text: str) -> Annotation | None: ...
    def delete(self, annotation_id: int) -> bool: ...


def validate_selector(selector: Selector) -> None:
    if selector.block_kind not in BLOCK_KINDS or selector.block_index < 0:
        raise ValueError("invalid selector")
    for value, limit in ((selector.exact, MAX_EXACT), (selector.prefix, MAX_CONTEXT), (selector.suffix, MAX_CONTEXT)):
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError("invalid selector")
        if any(ord(ch) == 0 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise ValueError("invalid selector")
    if not selector.exact:
        raise ValueError("invalid selector")


def validate_note(note_text: str) -> str:
    if not isinstance(note_text, str) or any(ord(ch) == 0 or 0xD800 <= ord(ch) <= 0xDFFF for ch in note_text):
        raise ValueError("invalid note")
    if not note_text.strip() or len(note_text) > MAX_NOTE:
        raise ValueError("invalid note")
    return note_text


@dataclass
class _Block:
    kind: str
    index: int
    text: str
    segments: list[tuple[str, int]]


def _markdown() -> MarkdownIt:
    parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    dollarmath_plugin(parser, allow_digits=False)
    parser.core.ruler.after("inline", "heading_anchors", add_heading_anchors)
    return parser


_FORMATTING = {"strong_open", "strong_close", "em_open", "em_close", "s_open", "s_close", "link_open", "link_close"}


def _contains_raw_html(children) -> bool:
    return any(child.type == "html_inline" for child in children)


def _inline_segments(children) -> tuple[str, list[tuple[str, int]]]:
    parts: list[str] = []
    segments: list[tuple[str, int]] = []
    run: list[str] = []
    cursor = 0

    def flush() -> None:
        nonlocal cursor
        segment = re.sub(r"\s+", " ", "".join(run)).strip()
        if not segment:
            return
        if parts:
            parts.append(" ")
            cursor += 1
        segments.append((segment, cursor))
        parts.append(segment)
        cursor += len(segment)

    for child in children:
        if child.type in {"text", "softbreak", "hardbreak"}:
            run.append(" " if child.type != "text" else child.content)
        elif child.type in _FORMATTING:
            continue
        else:
            flush()
            run.clear()
    flush()
    return "".join(parts), segments


def _stack_change(token_type: str, stack: list[str]) -> bool:
    if token_type == "blockquote_open":
        stack.append("blockquote")
    elif token_type == "list_item_open":
        stack.append("list_item")
    elif token_type in ("blockquote_close", "list_item_close"):
        stack.pop()
    else:
        return False
    return True


def _block_kind(parent: str, stack: list[str]) -> str | None:
    if parent == "heading_open":
        return "heading"
    if parent == "paragraph_open":
        return stack[-1] + "_paragraph" if stack and stack[-1] == "blockquote" else ("list_item" if stack and stack[-1] == "list_item" else "paragraph")
    if parent in ("th_open", "td_open"):
        return "table_cell"
    return None


def _blocks(body: str, *, heading_anchors: bool = False):
    tokens = _markdown().parse(body, {"heading_anchors": True} if heading_anchors else {})
    blocks: list[_Block] = []
    stack: list[str] = []
    index = 0
    for pos, token in enumerate(tokens):
        if _stack_change(token.type, stack):
            continue
        if token.type != "inline" or not token.children:
            continue
        kind = _block_kind(tokens[pos - 1].type if pos else "", stack)
        if kind is None:
            continue
        if _contains_raw_html(token.children):
            continue
        text, segments = _inline_segments(token.children)
        if text:
            blocks.append(_Block(kind, index, text, segments))
            index += 1
    return tokens, blocks


def _candidates(blocks, selector):
    candidates = []
    for block in blocks:
        if block.kind != selector.block_kind: continue
        for segment, offset in block.segments:
            start = 0
            while True:
                at = segment.find(selector.exact, start)
                if at < 0: break
                canonical_at = offset + at
                before = block.text[max(0, canonical_at-len(selector.prefix)):canonical_at]
                after = block.text[canonical_at+len(selector.exact):canonical_at+len(selector.exact)+len(selector.suffix)]
                score = (before == selector.prefix, after == selector.suffix, block.index == selector.block_index)
                candidates.append((score, block, offset + at, offset + at + len(selector.exact)))
                start = at + 1
    return candidates


def _resolve_row(blocks, row):
    candidates = _candidates(blocks, row.selector)
    if not candidates:
        return Resolution(row.id, None, None, "orphaned", "not-found")
    if len(candidates) > 1:
        contextual = [c for c in candidates if (not row.selector.prefix or c[0][0]) and (not row.selector.suffix or c[0][1])]
        if len(contextual) != 1:
            return Resolution(row.id, None, None, "orphaned", "ambiguous")
        candidate = contextual[0]
    else:
        candidate = candidates[0]
    return Resolution(row.id, candidate[2], candidate[3], "resolved", block_index=candidate[1].index)


def resolve(body: str, rows: Sequence[Annotation]) -> tuple[Resolution, ...]:
    _, blocks = _blocks(body)
    results = [_resolve_row(blocks, row) for row in rows]
    conflicts = set()
    for i, left in enumerate(results):
        if left.status != "resolved": continue
        for j, right in enumerate(results[i + 1:], i + 1):
            if right.status == "resolved" and left.block_index == right.block_index and left.start < right.end and right.start < left.end:
                conflicts.update((i, j))
    for i in conflicts:
        row = results[i]
        results[i] = Resolution(row.annotation_id, None, None, "orphaned", "overlap")
    return tuple(results)


def _note_html(note: str) -> str:
    return html.escape(note, quote=True)


def _annotation_markup(number: int, row: Annotation, text: str, last: bool) -> str:
    mark = f'<mark>{html.escape(text, quote=True)}</mark>'
    if not last:
        return mark
    label = f"note {number}"
    sidenote = "" if len(row.note_text) > 400 else (
        f'<span class="sidenote" id="note-{number}" role="note">'
        f'<span class="n" aria-hidden="true">{number}</span> {_note_html(row.note_text)}</span>'
    )
    return (
        f'<span class="ann" id="ann-{number}">{mark}'
        f'<sup><a class="ann-ref" href="#note-{number}-narrow" aria-label="{label}">{number}</a></sup>'
        f'{sidenote}</span>'
    )


def _render_annotated_inline(token, ranges, numbers, rows, renderer, options, env) -> str:
    children = token.children or []
    # Use the resolver's segments to map source token text to canonical offsets.
    _, segments = _inline_segments(children)
    segment_index = 0
    segment_used = 0
    segment_active = False
    pieces = []
    for child in children:
        if child.type not in {"text", "softbreak", "hardbreak"}:
            pieces.append(child)
            if child.type not in _FORMATTING:
                if segment_active:
                    segment_index += 1
                segment_used = 0
                segment_active = False
            continue
        if child.type != "text":
            if segment_index < len(segments):
                segment, segment_start = segments[segment_index]
                left = segment_start + segment_used
                right = left + 1
                active = [(idx, a, b) for idx, a, b in ranges if a <= left and right <= b]
                if active:
                    idx, _, annotation_end = active[-1]
                    content = " " if child.type == "softbreak" else " <br>"
                    child.type = "html_inline"
                    child.content = _annotation_markup(numbers[idx], rows[idx], content, right >= annotation_end)
            pieces.append(child)
            segment_used += 1
            continue
        normalized = re.sub(r"\s+", " ", child.content).strip()
        if not normalized:
            pieces.append(child)
            continue
        segment, segment_start = segments[segment_index]
        found = segment.find(normalized, segment_used)
        if found < 0:
            # The resolver and renderer must share a cursor model. This is only
            # reachable if markdown-it changes its inline tokenization.
            pieces.append(child)
            continue
        leading = len(child.content) - len(child.content.lstrip())
        start = segment_start + found - leading
        end = start + len(child.content)
        segment_used = found + len(normalized)
        segment_active = True
        cuts = {start, end}
        for _, left, right in ranges:
            if left < end and right > start:
                cuts.update((max(start, left), min(end, right)))
        points = sorted(cuts)
        rendered = []
        for left, right in zip(points, points[1:]):
            chunk = child.content[left - start:right - start]
            active = [(idx, a, b) for idx, a, b in ranges if a <= left and right <= b]
            if not active:
                rendered.append(html.escape(chunk, quote=False))
                continue
            idx, _, annotation_end = active[-1]
            rendered.append(_annotation_markup(numbers[idx], rows[idx], chunk, right >= annotation_end))
        child.type = "html_inline"
        child.content = "".join(rendered)
        pieces.append(child)
    return renderer.renderInline(pieces, options, env)


def render_with_annotations(body_markdown: str, rows: Sequence[Annotation]) -> RenderedPost:
    tokens, blocks = _blocks(body_markdown, heading_anchors=True)
    resolutions = resolve(body_markdown, rows)
    resolved = [(i, row, result) for i, (row, result) in enumerate(zip(rows, resolutions)) if result.status == "resolved"]
    by_block: dict[int, list[tuple[int, int, int]]] = {}
    for i, _, result in resolved:
        by_block.setdefault(result.block_index, []).append((i, result.start or 0, result.end or 0))
    numbers = {i: n for n, (i, _, _) in enumerate(resolved, 1)}
    row_by_index = {i: row for i, row, _ in resolved}
    renderer = _markdown().renderer
    options = _markdown().options
    eligible_tokens = [
        token for token in tokens
        if token.type == "inline" and token.children and not _contains_raw_html(token.children)
        and _inline_segments(token.children)[0]
    ]
    block_lookup = {id(token): block.index for token, block in zip(eligible_tokens, blocks)}
    children_lookup = {id(token.children): block.index for token, block in zip(eligible_tokens, blocks)}
    original_render_inline = renderer.renderInline
    original_paragraph_close = renderer.rules.get("paragraph_close")

    def render_inline(children, render_options, env):
        block = children_lookup.get(id(children))
        if block is None:
            return original_render_inline(children, render_options, env)
        token = type("InlineToken", (), {"children": children})()
        return _render_annotated_inline(token, by_block.get(block, []), numbers, row_by_index, renderer, render_options, env)

    def render_paragraph_close(all_tokens, index, render_options, env):
        result = original_paragraph_close(all_tokens, index, render_options, env) if original_paragraph_close else "</p>\n"
        inline = all_tokens[index - 1] if index else None
        notes = by_block.get(block_lookup.get(id(inline)), []) if inline is not None else []
        for idx, _, _ in notes:
            number = numbers[idx]
            note = _note_html(row_by_index[idx].note_text)
            result += f'<details class="ann-note" id="note-{number}-narrow"><summary>note {number}</summary><p>{note}</p></details>'
        return result

    renderer.renderInline = render_inline
    renderer.rules["paragraph_close"] = render_paragraph_close
    try:
        html_output = renderer.render(tokens, options, {})
    finally:
        renderer.renderInline = original_render_inline
        if original_paragraph_close is None:
            renderer.rules.pop("paragraph_close", None)
        else:
            renderer.rules["paragraph_close"] = original_paragraph_close
    return RenderedPost(html_output, resolutions)


class AnnotationStore:
    def __init__(self, database_url: str, *, engine: Engine | None = None):
        if engine is not None:
            self.engine = engine
        else:
            options = {"pool_pre_ping": True, "pool_timeout": POOL_TIMEOUT_SECONDS}
            if database_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
                options["connect_args"] = {"connect_timeout": CONNECT_TIMEOUT_SECONDS, "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"}
            self.engine = database_engine(database_url, **options)
    def close(self): self.engine.dispose()
    def _rows(self, query):
        with self.engine.connect() as connection: return [self._row(r) for r in connection.execute(query).mappings()]
    @staticmethod
    def _row(row):
        return Annotation(row["id"], row["post_slug"], Selector(row["block_kind"], row["block_index"], row["exact"], row["prefix"], row["suffix"]), row["note_text"], row["created_at"], row["updated_at"])
    def list_for_post(self, slug): return self._rows(sa.select(post_annotations).where(post_annotations.c.post_slug == slug).order_by(post_annotations.c.id))
    def list_admin(self): return self._rows(sa.select(post_annotations).order_by(post_annotations.c.post_slug, post_annotations.c.id))
    def _lock(self, connection, slug):
        if connection.dialect.name == "postgresql": connection.execute(sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(slug, 0))))

    def _write(self, slugs, callback):
        with self.engine.begin() as connection:
            for slug in sorted(set(slugs)):
                self._lock(connection, slug)
            return callback(connection)

    def _for_slug(self, connection, slug):
        return [self._row(r) for r in connection.execute(sa.select(post_annotations).where(post_annotations.c.post_slug == slug).order_by(post_annotations.c.id)).mappings()]

    def _validate_resolution(self, body, rows, annotation_id):
        outcome = resolve(body, rows)
        result = outcome[next(i for i, row in enumerate(rows) if row.id == annotation_id)]
        if result.status != "resolved": raise ValueError(result.reason or "invalid")

    def create(self, slug, selector, note_text, body):
        validate_selector(selector)
        note_text = validate_note(note_text)
        def write(connection):
            rows = self._for_slug(connection, slug)
            candidate = Annotation(0, slug, selector, note_text)
            self._validate_resolution(body, rows + [candidate], 0)
            values = dict(post_slug=slug, block_kind=selector.block_kind, block_index=selector.block_index, exact=selector.exact, prefix=selector.prefix, suffix=selector.suffix, note_text=note_text)
            return self._row(connection.execute(sa.insert(post_annotations).values(**values).returning(post_annotations)).mappings().one())
        return self._write([slug], write)

    def repair(self, annotation_id, slug, selector, body):
        validate_selector(selector)
        def write(connection):
            current_row = connection.execute(sa.select(post_annotations).where(post_annotations.c.id == annotation_id)).mappings().first()
            if current_row is None: return None
            current = self._row(current_row)
            rows = [row for row in self._for_slug(connection, slug) if row.id != annotation_id]
            candidate = Annotation(annotation_id, slug, selector, current.note_text)
            self._validate_resolution(body, rows + [candidate], annotation_id)
            values = dict(post_slug=slug, block_kind=selector.block_kind, block_index=selector.block_index, exact=selector.exact, prefix=selector.prefix, suffix=selector.suffix, updated_at=sa.func.now())
            row = connection.execute(sa.update(post_annotations).where(post_annotations.c.id == annotation_id).values(**values).returning(post_annotations)).mappings().first()
            return self._row(row) if row else None
        # the initial lookup is repeated under both locks by the shared write helper
        with self.engine.connect() as connection:
            old = connection.execute(sa.select(post_annotations.c.post_slug).where(post_annotations.c.id == annotation_id)).scalar_one_or_none()
        return self._write([old, slug] if old is not None else [slug], write)
    def update_note(self, annotation_id, note_text):
        note_text = validate_note(note_text)
        with self.engine.begin() as c:
            row = c.execute(sa.update(post_annotations).where(post_annotations.c.id == annotation_id).values(note_text=note_text, updated_at=sa.func.now()).returning(post_annotations)).mappings().first()
            return self._row(row) if row else None
    def delete(self, annotation_id):
        with self.engine.begin() as c: return c.execute(sa.delete(post_annotations).where(post_annotations.c.id == annotation_id)).rowcount > 0
