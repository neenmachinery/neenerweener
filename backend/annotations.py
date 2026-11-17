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
    def create_or_repair(self, slug: str, selector: Selector, note_text: str, body: str, annotation_id: int | None = None) -> Annotation | None: ...
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
    segments: list[str]


def _markdown() -> MarkdownIt:
    parser = MarkdownIt("commonmark") .enable(["table", "strikethrough"])
    dollarmath_plugin(parser, allow_digits=False)
    return parser


def _blocks(body: str):
    tokens = _markdown().parse(body)
    blocks: list[_Block] = []
    stack: list[str] = []
    index = 0
    for pos, token in enumerate(tokens):
        if token.type.endswith("_open"):
            if token.type == "blockquote_open": stack.append("blockquote")
            elif token.type == "list_item_open": stack.append("list_item")
            continue
        if token.type.endswith("_close"):
            if token.type in ("blockquote_close", "list_item_close"): stack.pop()
            continue
        if token.type != "inline" or not token.children:
            continue
        parent = tokens[pos - 1].type if pos else ""
        if parent == "heading_open": kind = "heading"
        elif parent == "paragraph_open": kind = "blockquote_paragraph" if stack and stack[-1] == "blockquote" else ("list_item" if stack and stack[-1] == "list_item" else "paragraph")
        elif parent in ("th_open", "td_open"): kind = "table_cell"
        else: continue
        if any(c.type in {"code_inline", "html_inline", "math_inline", "image"} for c in token.children):
            continue
        segments = []
        chars = []
        for child in token.children:
            if child.type in {"text", "softbreak", "hardbreak", "strong_open", "strong_close", "em_open", "em_close", "s_open", "s_close", "link_open", "link_close"}:
                if child.type in {"text", "softbreak", "hardbreak"}: chars.append(" " if child.type != "text" else child.content)
            else:
                if chars:
                    segment = re.sub(r"\s+", " ", "".join(chars)).strip()
                    if segment: segments.append(segment)
                    chars = []
        if chars:
            segment = re.sub(r"\s+", " ", "".join(chars)).strip()
            if segment: segments.append(segment)
        text = " ".join(segments)
        if text:
            blocks.append(_Block(kind, index, text, segments))
            index += 1
    return tokens, blocks


def resolve(body: str, rows: Sequence[Annotation]) -> tuple[Resolution, ...]:
    _, blocks = _blocks(body)
    results = []
    for row in rows:
        selector = row.selector
        candidates = []
        for block in blocks:
            if block.kind != selector.block_kind: continue
            for segment in block.segments:
                start = 0
                while True:
                    at = segment.find(selector.exact, start)
                    if at < 0: break
                    before = segment[max(0, at-len(selector.prefix)):at]
                    after = segment[at+len(selector.exact):at+len(selector.exact)+len(selector.suffix)]
                    score = (before == selector.prefix, after == selector.suffix, block.index == selector.block_index)
                    candidates.append((score, block, at, at + len(selector.exact)))
                    start = at + 1
        if not candidates:
            results.append(Resolution(row.id, None, None, "orphaned", "not-found")); continue
        if len(candidates) > 1:
            contextual = [c for c in candidates if (not selector.prefix or c[0][0]) and (not selector.suffix or c[0][1])]
            if len(contextual) != 1:
                results.append(Resolution(row.id, None, None, "orphaned", "ambiguous")); continue
            best = contextual[0]
        else:
            best = candidates[0]
        results.append(Resolution(row.id, best[2], best[3], "resolved", block_index=best[1].index))
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


def render_with_annotations(body_markdown: str, rows: Sequence[Annotation]) -> RenderedPost:
    tokens, blocks = _blocks(body_markdown)
    resolutions = resolve(body_markdown, rows)
    # pr 1 keeps public rendering unchanged; pr 2 owns the reader markup seam.
    return RenderedPost(_markdown().render(body_markdown), resolutions)


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
    def create_or_repair(self, slug, selector, note_text, body, annotation_id=None):
        validate_selector(selector); note_text = validate_note(note_text)
        candidate = Annotation(annotation_id or 0, slug, selector, note_text)
        with self.engine.begin() as c:
            self._lock(c, slug)
            existing = [self._row(r) for r in c.execute(sa.select(post_annotations).where(post_annotations.c.post_slug == slug).order_by(post_annotations.c.id)).mappings()]
            if annotation_id is not None and not any(row.id == annotation_id for row in existing): return None
            if annotation_id is not None:
                current = next(row for row in existing if row.id == annotation_id)
                candidate = Annotation(annotation_id, slug, selector, current.note_text)
            rows = [candidate if annotation_id is not None and row.id == annotation_id else row for row in existing]
            if annotation_id is None: rows.append(candidate)
            outcome = resolve(body, rows)
            result = outcome[-1] if annotation_id is None else outcome[next(i for i, row in enumerate(existing) if row.id == annotation_id)]
            if result.status != "resolved": raise ValueError(result.reason or "invalid")
            values = dict(post_slug=slug, block_kind=selector.block_kind, block_index=selector.block_index, exact=selector.exact, prefix=selector.prefix, suffix=selector.suffix)
            if annotation_id is None:
                values["note_text"] = note_text
                return self._row(c.execute(sa.insert(post_annotations).values(**values).returning(post_annotations)).mappings().one())
            row = c.execute(sa.update(post_annotations).where(post_annotations.c.id == annotation_id).values(**{**values, "updated_at": sa.func.now()}).returning(post_annotations)).mappings().first()
            return self._row(row) if row else None
    def update_note(self, annotation_id, note_text):
        note_text = validate_note(note_text)
        with self.engine.begin() as c:
            row = c.execute(sa.update(post_annotations).where(post_annotations.c.id == annotation_id).values(note_text=note_text, updated_at=sa.func.now()).returning(post_annotations)).mappings().first()
            return self._row(row) if row else None
    def delete(self, annotation_id):
        with self.engine.begin() as c: return c.execute(sa.delete(post_annotations).where(post_annotations.c.id == annotation_id)).rowcount > 0
