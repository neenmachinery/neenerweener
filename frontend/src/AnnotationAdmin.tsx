import { useEffect, useRef, useState } from "react";
import { createAnnotation, deleteAnnotation, listAnnotations, repairAnnotation, updateAnnotation, type Annotation, type AnnotationSelector, type Result } from "./adminApi";
import { Button } from "./EntryEditor";
import type { AdminPageProps } from "./adminPages";
import { useRequest } from "./useRequest";

const eligible = "h1,h2,h3,h4,h5,h6,p,li,blockquote p";
const normalize = (s: string) => s.replace(/\s+/g, " ").trim();
function selectedSelector(root: HTMLElement): AnnotationSelector | null {
  const selection = getSelection();
  if (!selection || selection.isCollapsed || !root.contains(selection.anchorNode) || !root.contains(selection.focusNode)) return null;
  const range = selection.getRangeAt(0);
  const ancestor = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE ? range.commonAncestorContainer as Element : range.commonAncestorContainer.parentElement;
  let block = ancestor?.closest(eligible) as HTMLElement | null;
  if (!block || !root.contains(block)) return null;
  const blocks = [...root.querySelectorAll<HTMLElement>(eligible)].filter((el) => !el.closest("pre") && el.textContent);
  const index = blocks.indexOf(block);
  if (index < 0) return null;
  const exact = normalize(selection.toString());
  const full = normalize(block.textContent || "");
  if (!exact || !full.includes(exact)) return null;
  const at = full.indexOf(exact);
  return { block_kind: block.matches("h1,h2,h3,h4,h5,h6") ? "heading" : block.matches("li") ? "list_item" : block.matches("blockquote p") ? "blockquote_paragraph" : "paragraph", block_index: index, exact, prefix: full.slice(Math.max(0, at - 64), at), suffix: full.slice(at + exact.length, at + exact.length + 64) };
}

export function PostAnnotationAdmin({ root, slug, onChanged, expire }: { root: HTMLElement; slug: string; onChanged: () => void; expire: () => void }) {
  const [selector, setSelector] = useState<AnnotationSelector | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Annotation | null>(null);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const { run, busy } = useRequest("submit");
  const { run: loadRun } = useRequest("load");
  const refresh = async () => {
    const r = await loadRun((signal) => listAnnotations(slug, signal));
    if (r.ok) setAnnotations(r.data); else if (r.status === 401) expire();
  };
  useEffect(() => { refresh(); }, [slug]);
  useEffect(() => {
    const changed = () => { const s = selectedSelector(root); if (s) setSelector(s); };
    document.addEventListener("selectionchange", changed);
    return () => document.removeEventListener("selectionchange", changed);
  }, [root]);
  const save = async () => {
    if (!selector || !note.trim()) return;
    const r = await run((signal) => editing ? updateAnnotation(editing.id, note, signal) : createAnnotation(slug, selector, note, signal));
    if (r.ok) { setSelector(null); setEditing(null); setNote(""); setError(""); onChanged(); refresh(); }
    else if (r.status === 401) expire(); else setError(r.message);
  };
  const edit = (a: Annotation) => { setEditing(a); setNote(a.note_text); setSelector(a.selector); };
  return <>
    {selector && <div className="annotation-editor" role="dialog" aria-label="annotate"><textarea autoFocus value={note} onChange={(e) => setNote(e.target.value)} aria-label="note" /><div><Button onClick={save} busy={busy} label="save" /><Button onClick={() => { setSelector(null); setEditing(null); setError(""); }} label="cancel" /></div>{error && <p className="error" role="alert">{error}</p>}</div>}
    {annotations.map((a) => <AnnotationControls key={a.id} annotation={a} onEdit={() => edit(a)} onDeleted={() => { refresh(); onChanged(); }} expire={expire} />)}
  </>;
}

function AnnotationControls({ annotation, onEdit, onDeleted, expire }: { annotation: Annotation; onEdit: () => void; onDeleted: () => void; expire: () => void }) {
  const [confirm, setConfirm] = useState(false); const { run, busy } = useRequest("submit");
  const remove = async () => { const r = await run((s) => deleteAnnotation(annotation.id, s)); if (r.ok) onDeleted(); else if (r.status === 401) expire(); };
  return <span className="annotation-admin"><Button onClick={onEdit} label="edit" />{confirm ? <><Button onClick={remove} busy={busy} label="confirm delete" /><Button onClick={() => setConfirm(false)} label="cancel" /></> : <Button onClick={() => setConfirm(true)} label="delete" />}</span>;
}

export default function AdminAnnotations({ send, active }: AdminPageProps) {
  const [rows, setRows] = useState<Annotation[]>([]); const [error, setError] = useState(""); const { run } = useRequest("load");
  const load = async () => { const r = await run((s) => listAnnotations(undefined, s)); if (r.ok) { setRows(r.data); setError(""); } else setError(r.message); };
  useEffect(() => { if (active) load(); }, [active]);
  const groups = [...new Set(rows.map((r) => r.post_slug))];
  return <><h1 tabIndex={-1}>annotations</h1>{error && <p className="error" role="alert">{error}</p>}{groups.map((slug) => <section className="admin-section" key={slug}><h2><a href={`/posts/${slug}`}>{slug}</a></h2><ul className="admin-links">{rows.filter((r) => r.post_slug === slug).map((a) => <AdminAnnotationRow key={a.id} annotation={a} onChanged={load} />)}</ul></section>)}</>;
}

function AdminAnnotationRow({ annotation, onChanged }: { annotation: Annotation; onChanged: () => void }) {
  const [editing, setEditing] = useState(false); const [text, setText] = useState(annotation.note_text); const [confirm, setConfirm] = useState(false); const [error, setError] = useState(""); const { run, busy } = useRequest("submit");
  const save = async () => { const r = await run((s) => updateAnnotation(annotation.id, text, s)); if (r.ok) { setEditing(false); onChanged(); } else setError(r.message); };
  const remove = async () => { const r = await run((s) => deleteAnnotation(annotation.id, s)); if (r.ok) onChanged(); else setError(r.message); };
  return <li>{editing ? <><textarea aria-label="note" value={text} onChange={(e) => setText(e.target.value)} /><Button onClick={save} busy={busy} label="save" /><Button onClick={() => setEditing(false)} label="cancel" /></> : <><span className="label">{annotation.note_text}</span><button type="button" onClick={() => setEditing(true)}>edit</button></>}{error && <span className="error">{error}</span>}<span className="muted">{annotation.status}{annotation.reason ? ` · ${annotation.reason}` : ""}</span>{confirm ? <><Button onClick={remove} busy={busy} label="confirm delete" /><Button onClick={() => setConfirm(false)} label="cancel" /></> : <Button onClick={() => setConfirm(true)} label="delete" />}{annotation.status !== "resolved" && <a href={`/posts/${annotation.post_slug}#annotate-${annotation.id}`}>repair</a>}</li>;
}
