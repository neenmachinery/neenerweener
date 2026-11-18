from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence
import copy

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
BLOCK_KINDS = {"heading", "paragraph", "list_item", "blockquote_paragraph"}

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
    # one entry per canonical character: (inline child, raw character index), or None
    positions: list[tuple[object, int] | None]
    inline_token: object


def _markdown() -> MarkdownIt:
    parser = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    dollarmath_plugin(parser, allow_digits=False)
    parser.core.ruler.after("inline", "heading_anchors", add_heading_anchors)
    return parser


_FORMATTING = {"strong_open", "strong_close", "em_open", "em_close", "s_open", "s_close", "link_open", "link_close"}


def _contains_raw_html(children) -> bool:
    return any(child.type == "html_inline" for child in children)


def _normalized_run(run):
    out, whitespace = [], False
    for char, child, raw in run:
        if char.isspace():
            if not whitespace: out.append((" ", child, raw))
            whitespace = True
        else:
            out.append((char, child, raw)); whitespace = False
    while out and out[0][0] == " ": out.pop(0)
    while out and out[-1][0] == " ": out.pop()
    return out


def _inline_map(children) -> tuple[str, list[tuple[str, int]], list[tuple[object, int] | None]]:
    parts: list[str] = []
    segments: list[tuple[str, int]] = []
    positions: list[tuple[object, int] | None] = []
    run: list[tuple[str, object, int]] = []
    cursor = 0

    def flush() -> None:
        nonlocal cursor
        out = _normalized_run(run)
        if not out:
            run.clear()
            return
        if parts:
            parts.append(" "); positions.append(None); cursor += 1
        segment = "".join(char for char, _, _ in out)
        segments.append((segment, cursor)); parts.append(segment)
        positions.extend((child, raw) for _, child, raw in out)
        cursor += len(segment)
        run.clear()

    for child in children:
        if child.type == "text":
            run.extend((char, child, i) for i, char in enumerate(child.content))
        elif child.type in {"softbreak", "hardbreak"}:
            run.append((" ", child, -1))
        elif child.type in _FORMATTING:
            continue
        else:
            flush()
    flush()
    return "".join(parts), segments, positions


def _inline_segments(children) -> tuple[str, list[tuple[str, int]]]:
    text, segments, _ = _inline_map(children)
    return text, segments


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
    return None


def _block_from_token(token, parent, stack, index):
    if token.type != "inline" or not token.children or _contains_raw_html(token.children):
        return None
    kind = _block_kind(parent, stack)
    if kind is None:
        return None
    text, segments, positions = _inline_map(token.children)
    return _Block(kind, index, text, segments, positions, token) if text else None


def _blocks(body: str, *, heading_anchors: bool = False, parser: MarkdownIt | None = None):
    tokens = (parser or _markdown()).parse(body, {"heading_anchors": True} if heading_anchors else {})
    blocks, stack, index = [], [], 0
    for pos, token in enumerate(tokens):
        if _stack_change(token.type, stack):
            continue
        block = _block_from_token(token, tokens[pos - 1].type if pos else "", stack, index)
        if block is not None:
            blocks.append(block)
            index += 1
    return tokens, blocks


def _raw_map(text: str) -> tuple[str, list[int]]:
    """normalize DOM textContent and map every raw boundary to a canonical offset."""
    out, boundaries = [], [0] * (len(text) + 1)
    pending = False
    for index, char in enumerate(text):
        if char.isspace():
            boundaries[index] = len(out); pending = bool(out)
        else:
            if pending and out: out.append(" ")
            boundaries[index] = len(out)
            out.append(char); pending = False
        boundaries[index + 1] = len(out)
    if out and out[-1] == " ":
        out.pop()
        boundaries = [min(value, len(out)) for value in boundaries]
    return "".join(out), boundaries


def selector_from_dom(body: str, block_text: str, start: int, end: int, block_kind: str, block_index_hint: int) -> Selector:
    if not isinstance(block_text, str) or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        raise ValueError("not-found")
    _, blocks = _blocks(body)
    raw, offsets = _raw_map(block_text)
    candidates = [block for block in blocks if block.kind == block_kind and block.text == raw]
    if not candidates:
        raise ValueError("not-found")
    block = next((item for item in candidates if item.index == block_index_hint), candidates[0])
    if start >= len(offsets) or end >= len(offsets): raise ValueError("not-found")
    left = offsets[start]
    right = offsets[end]
    if right > len(raw) or raw[left:right].strip() == "": raise ValueError("not-found")
    exact = raw[left:right]
    return Selector(block.kind, block.index, exact, block.text[max(0, left - MAX_CONTEXT):left], block.text[right:right + MAX_CONTEXT])


def _candidates(blocks, selector):
    candidates = []
    for block in blocks:
        if block.kind != selector.block_kind: continue
        start = 0
        while True:
            canonical_at = block.text.find(selector.exact, start)
            if canonical_at < 0: break
            before = block.text[max(0, canonical_at-len(selector.prefix)):canonical_at]
            after = block.text[canonical_at+len(selector.exact):canonical_at+len(selector.exact)+len(selector.suffix)]
            score = (before == selector.prefix, after == selector.suffix, block.index == selector.block_index)
            candidates.append((score, block, canonical_at, canonical_at + len(selector.exact)))
            start = canonical_at + 1
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


def _annotation_suffix(number: int, row: Annotation) -> str:
    label = f"note {number}"
    sidenote = "" if len(row.note_text) > 400 else (
        f'<span class="sidenote" id="note-{number}" role="note">'
        f'<span class="n" aria-hidden="true">{number}</span>'
        f'<span class="sidenote-text">{_note_html(row.note_text)}</span></span>'
    )
    marker = (
        f'<sup class="ann-ref-wide"><a href="#note-{number}-narrow" aria-label="{label}">{number}</a></sup>'
        f'<sup class="ann-ref-narrow" aria-hidden="true">{number}</sup>'
    )
    return marker + sidenote


def _annotation_markup(number: int, row: Annotation, text: str, last: bool, *, escaped: bool = False, defer_suffix: bool = False) -> str:
    mark = f'<mark>{text if escaped else html.escape(text, quote=True)}</mark>'
    if not last:
        return mark
    if defer_suffix:
        return mark
    return f'<span class="ann" id="ann-{number}" data-annotation-id="{row.id}">{mark}{_annotation_suffix(number, row)}</span>'


def _token(type_: str, content: str):
    return type("InlineToken", (), {"type": type_, "content": content})()


def _active_at(canonical: int | None, ranges) -> int | None:
    return next((idx for idx, left, right in ranges if canonical is not None and left <= canonical < right), None)


def _fragment_markup(text, active, end, current_max, link_depth, numbers, rows, deferred):
    if active is None:
        return html.escape(text, quote=False)
    last = end - 1 <= current_max
    defer = last and link_depth > 0
    if defer:
        deferred.append((active, _annotation_suffix(numbers[active], rows[active])))
    return _annotation_markup(numbers[active], rows[active], text, last, escaped=True, defer_suffix=defer)


def _text_runs(child, by_source, ranges):
    runs, current, active, maximum = [], [], None, -1
    for raw in range(len(child.content)):
        source = by_source.get((id(child), raw), [])
        canonical = source[0] if source else None
        next_active = _active_at(canonical, ranges)
        if next_active != active and current:
            runs.append((''.join(current), active, maximum))
            current, maximum = [], -1
        active = next_active
        if canonical is not None and active is not None:
            maximum = max(maximum, canonical)
        current.append(html.escape(child.content[raw], quote=False))
    if current:
        runs.append((''.join(current), active, maximum))
    return runs


def _text_fragments(child, by_source, ranges, numbers, rows, link_depth, deferred):
    fragments = []
    for text, active, maximum in _text_runs(child, by_source, ranges):
        end = max((right for idx, _, right in ranges if idx == active), default=0)
        fragments.append(_fragment_markup(text, active, end, maximum, link_depth, numbers, rows, deferred))
    return _token("html_inline", ''.join(fragments))


def _source_map(block):
    result = {}
    for canonical, source in enumerate(block.positions):
        if source is not None:
            result.setdefault((id(source[0]), source[1]), []).append(canonical)
    return result


def _close_inline_link(pieces, deferred, numbers, rows, start):
    for index, suffix in deferred:
        pieces.insert(start, _token("html_inline", f'<span class="ann" id="ann-{numbers[index]}" data-annotation-id="{rows[index].id}">'))
        pieces.append(_token("html_inline", suffix + "</span>"))
    deferred.clear()


def _inline_plan(token, block, ranges, numbers, rows):
    by_source, pieces, deferred, link_depth, link_starts = _source_map(block), [], [], 0, []
    for child in token.children or []:
        if child.type == "link_open":
            link_depth += 1
            link_starts.append(len(pieces))
            pieces.append(child)
        elif child.type == "link_close":
            link_depth -= 1
            pieces.append(child)
            _close_inline_link(pieces, deferred, numbers, rows, link_starts.pop())
        elif child.type in {"softbreak", "hardbreak"}:
            active = _active_at(next(iter(by_source.get((id(child), -1), [])), None), ranges)
            if active is not None:
                pieces.append(_token("html_inline", _annotation_markup(numbers[active], rows[active], " ", False, escaped=True)))
            pieces.append(child)
        elif child.type == "text":
            pieces.append(_text_fragments(child, by_source, ranges, numbers, rows, link_depth, deferred))
        else:
            pieces.append(child)
    pieces.extend(_token("html_inline", suffix) for _, suffix in deferred)
    return pieces


def _render_annotated_inline(token, block, ranges, numbers, rows, renderer, options, env) -> str:
    return renderer.renderInline(_inline_plan(token, block, ranges, numbers, rows), options, env)


def _annotation_context(resolutions, rows):
    resolved = [(i, row, result) for i, (row, result) in enumerate(zip(rows, resolutions)) if result.status == "resolved"]
    resolved.sort(key=lambda item: (item[2].block_index, item[2].start, item[0]))
    by_block = {}
    for i, _, result in resolved:
        by_block.setdefault(result.block_index, []).append((i, result.start or 0, result.end or 0))
    return by_block, {i: n for n, (i, _, _) in enumerate(resolved, 1)}, {i: row for i, row, _ in resolved}


def _render_resolved(tokens, blocks, resolutions, rows, parser):
    by_block, numbers, row_by_index = _annotation_context(resolutions, rows)
    renderer = copy.copy(parser.renderer)
    renderer.rules = dict(parser.renderer.rules)
    inline_blocks = [(block.inline_token, block) for block in blocks]
    block_for_inline = {id(token.children): block for token, block in inline_blocks}
    token_blocks = {id(token): block.index for token, block in inline_blocks}
    original_render_inline = renderer.renderInline
    original_rules = {name: renderer.rules.get(name) for name in ("paragraph_close", "heading_close", "th_close", "td_close")}

    def render_inline(children, render_options, env):
        block = block_for_inline.get(id(children))
        if block is None:
            return original_render_inline(children, render_options, env)
        token = type("InlineToken", (), {"children": children})()
        return _render_annotated_inline(token, block, by_block.get(block.index, []), numbers, row_by_index, renderer, render_options, env)

    def close_rule(all_tokens, index, render_options, env):
        token = all_tokens[index - 1] if index else None
        block_index = token_blocks.get(id(token)) if token is not None and token.type == "inline" else None
        rule = original_rules[all_tokens[index].type]
        result = rule(all_tokens, index, render_options, env) if rule else renderer.renderToken(all_tokens, index, render_options, env)
        for annotation_index, _, _ in by_block.get(block_index, []):
            number = numbers[annotation_index]
            row = row_by_index[annotation_index]
            mode = " ann-note-long" if len(row.note_text) > 400 else ""
            result += f'<details class="ann-note{mode}" id="note-{number}-narrow" data-annotation-id="{row.id}"><summary>note {number}</summary><p>{_note_html(row.note_text)}</p></details>'
        return result

    renderer.renderInline = render_inline
    for name in {"paragraph_close", "heading_close", "th_close", "td_close"}:
        renderer.rules[name] = close_rule
    try:
        return renderer.render(tokens, parser.options, {})
    finally:
        renderer.renderInline = original_render_inline
        for name, rule in original_rules.items():
            if rule is None: renderer.rules.pop(name, None)
            else: renderer.rules[name] = rule


def render_with_annotations(body_markdown: str, rows: Sequence[Annotation], parser: MarkdownIt | None = None) -> RenderedPost:
    parser = parser or _markdown()
    tokens, blocks = _blocks(body_markdown, heading_anchors=True, parser=parser)
    resolutions = resolve(body_markdown, rows)
    if not any(result.status == "resolved" for result in resolutions):
        return RenderedPost(_markdown().render(body_markdown, {"heading_anchors": True}), resolutions)
    return RenderedPost(_render_resolved(tokens, blocks, resolutions, rows, parser), resolutions)


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
