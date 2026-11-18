import { useEffect, useState } from "react";
import { createAnnotation, deleteAnnotation, listAnnotations, repairAnnotation, updateAnnotation, type Annotation, type AnnotationAnchor } from "./adminApi";
import { Button } from "./EntryEditor";
import { useRequest } from "./useRequest";

const blocks = "h1,h2,h3,h4,h5,h6,p,li,td,th";
export function selectedAnchor(root: HTMLElement): AnnotationAnchor | null {
  const selection = getSelection();
  if (!selection || selection.isCollapsed || !root.contains(selection.anchorNode) || !root.contains(selection.focusNode)) return null;
  const range = selection.getRangeAt(0);
  const node = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE ? range.commonAncestorContainer as Element : range.commonAncestorContainer.parentElement;
  const block = node?.closest(blocks) as HTMLElement | null;
  if (!block || !root.contains(block) || block.closest("pre") || !block.textContent) return null;
  const startRange = document.createRange(); startRange.selectNodeContents(block); startRange.setEnd(range.startContainer, range.startOffset);
  const endRange = document.createRange(); endRange.selectNodeContents(block); endRange.setEnd(range.endContainer, range.endOffset);
  const start = startRange.toString().length, end = endRange.toString().length;
  if (start >= end || selection.toString().includes("\n")) return null;
  const kind = block.matches("h1,h2,h3,h4,h5,h6") ? "heading" : block.closest("li") ? "list_item" : block.closest("blockquote") ? "blockquote_paragraph" : "paragraph";
  const same = [...root.querySelectorAll<HTMLElement>(blocks)].filter((item) => item.textContent && !item.closest("pre") && (item.matches("h1,h2,h3,h4,h5,h6") ? "heading" : item.closest("li") ? "list_item" : item.closest("blockquote") ? "blockquote_paragraph" : "paragraph") === kind);
  return { block_text: block.textContent, start, end, block_kind: kind, block_index_hint: same.indexOf(block) };
}

export function PostAnnotationAdmin({ root, slug, onChanged, expire }: { root: HTMLElement; slug: string; onChanged: () => void; expire: () => void }) {
  const [anchor, setAnchor] = useState<AnnotationAnchor | null>(null); const [note, setNote] = useState(""); const [error, setError] = useState(""); const [editing, setEditing] = useState<Annotation | null>(null); const [rows, setRows] = useState<Annotation[]>([]);
  const { run, busy } = useRequest("submit"); const { run: load } = useRequest("load");
  const repairId = Number(new URLSearchParams(location.search).get("repair"));
  const refresh = async () => { const r = await load((signal) => listAnnotations(repairId ? undefined : slug, signal)); if (r.ok) { setRows(r.data); const repair = repairId && r.data.find((item) => item.id === repairId); if (repair && repair.status !== "resolved") { setEditing(repair); setNote(repair.note_text); } } else if (r.status === 401) expire(); };
  useEffect(() => { refresh(); }, [slug]);
  useEffect(() => { const handler = () => { const next = selectedAnchor(root); if (next) { setAnchor(next); setEditing(null); setError(""); } }; document.addEventListener("selectionchange", handler); return () => document.removeEventListener("selectionchange", handler); }, [root]);
  const save = async () => { if (!anchor || !note.trim()) return; const r = await run((signal) => editing && editing.status !== "resolved" ? repairAnnotation(editing.id, slug, anchor, signal) : editing ? updateAnnotation(editing.id, note, signal) : createAnnotation(slug, anchor, note, signal)); if (r.ok) { setAnchor(null); setEditing(null); setNote(""); setError(""); await refresh(); onChanged(); } else if (r.status === 401) expire(); else setError(r.message); };
  return <>{(anchor || editing) && <div className="annotation-editor" role="dialog" aria-label="annotate"><textarea readOnly={!!editing && editing.status !== "resolved" && !anchor} value={note} onChange={(e) => setNote(e.target.value)} aria-label="note" /><div><Button onClick={save} busy={busy} label="save" /><Button onClick={() => { setAnchor(null); setEditing(null); setError(""); }} label="cancel" /></div>{error && <p className="error" role="alert">{error}</p>}</div>}{rows.map((a) => <span className="annotation-admin" key={a.id}><Button onClick={() => { setEditing(a); setNote(a.note_text); setAnchor(null); }} label="edit" /><Delete annotation={a} expire={expire} onDeleted={async () => { await refresh(); onChanged(); }} /></span>)}</>;
}
function Delete({ annotation, expire, onDeleted }: { annotation: Annotation; expire: () => void; onDeleted: () => void }) { const [confirm, setConfirm] = useState(false); const { run, busy } = useRequest("submit"); const remove = async () => { const r = await run((s) => deleteAnnotation(annotation.id, s)); if (r.ok) onDeleted(); else if (r.status === 401) expire(); }; return confirm ? <><Button onClick={remove} busy={busy} label="confirm delete" /><Button onClick={() => setConfirm(false)} label="cancel" /></> : <Button onClick={() => setConfirm(true)} label="delete" />; }
