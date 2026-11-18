import { useEffect, useRef, useState } from "react";
import { createAnnotation, deleteAnnotation, listAnnotations, repairAnnotation, updateAnnotation, type Annotation, type AnnotationAnchor } from "./adminApi";
import { Button } from "./EntryEditor";
import { useRequest } from "./useRequest";

const candidates = "h1,h2,h3,h4,h5,h6,p,li";
const canonicalBlocks = (root: HTMLElement) => [...root.querySelectorAll<HTMLElement>(candidates)].filter((item) => {
  if (!item.textContent || item.closest("pre,.ann-note,.annotation-editor")) return false;
  if (item.matches("li") && item.querySelector(":scope > p")) return false;
  return !item.matches("p") || !item.closest("li") || item.parentElement?.matches("li");
});
const kindOf = (block: HTMLElement) => block.matches("h1,h2,h3,h4,h5,h6") ? "heading" : block.closest("li") ? "list_item" : block.closest("blockquote") ? "blockquote_paragraph" : "paragraph";

export function selectedAnchor(root: HTMLElement): AnnotationAnchor | null {
  const selection = getSelection();
  if (!selection || selection.isCollapsed || !root.contains(selection.anchorNode) || !root.contains(selection.focusNode)) return null;
  const range = selection.getRangeAt(0);
  const startElement = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer as Element : range.startContainer.parentElement;
  const endElement = range.endContainer.nodeType === Node.ELEMENT_NODE ? range.endContainer as Element : range.endContainer.parentElement;
  const startBlock = startElement?.closest(candidates) as HTMLElement | null;
  const endBlock = endElement?.closest(candidates) as HTMLElement | null;
  if (!startBlock || startBlock !== endBlock || !root.contains(startBlock) || startBlock.closest("pre,.ann-note,.annotation-editor") || !startBlock.textContent) return null;
  const block = startBlock.matches("p") || !startBlock.closest("li") ? startBlock : (startBlock.querySelector(":scope > p") as HTMLElement | null) ?? startBlock;
  const startRange = document.createRange(); startRange.selectNodeContents(block); startRange.setEnd(range.startContainer, range.startOffset);
  const endRange = document.createRange(); endRange.selectNodeContents(block); endRange.setEnd(range.endContainer, range.endOffset);
  const start = startRange.toString().length, end = endRange.toString().length;
  if (start >= end) return null;
  const blocks = canonicalBlocks(root);
  return { block_text: block.textContent, start, end, block_kind: kindOf(block), block_index_hint: blocks.indexOf(block) };
}

type Props = { root: HTMLElement; slug: string; onChanged: () => Promise<unknown>; expire: () => void };
export function PostAnnotationAdmin({ root, slug, onChanged, expire }: Props) {
  const [anchor, setAnchor] = useState<AnnotationAnchor | null>(null); const [note, setNote] = useState(""); const [error, setError] = useState(""); const [editing, setEditing] = useState<Annotation | null>(null); const [rows, setRows] = useState<Annotation[]>([]); const [open, setOpen] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null); const { run, busy } = useRequest("submit"); const { run: load } = useRequest("load");
  const repairId = Number(new URLSearchParams(location.search).get("repair"));
  const refresh = async () => { const r = await load((signal) => listAnnotations(repairId ? undefined : slug, signal)); if (r.ok) { setRows(r.data); const repair = repairId && r.data.find((item) => item.id === repairId); if (repair && repair.status !== "resolved") { setEditing(repair); setNote(repair.note_text); } } else if (r.status === 401) expire(); };
  useEffect(() => { void refresh(); }, [slug]);
  useEffect(() => { const handler = () => { const next = selectedAnchor(root); if (next) { setAnchor(next); setError(""); } }; document.addEventListener("selectionchange", handler); return () => document.removeEventListener("selectionchange", handler); }, [root]);
  useEffect(() => { if (open) editor.current?.focus(); }, [open]);
  const close = () => { setAnchor(null); setEditing(null); setOpen(false); setError(""); };
  const save = async () => {
    if (!note.trim() || (!editing?.status || editing.status !== "resolved") && !anchor) return;
    const r = await run((signal) => editing?.status === "resolved" ? updateAnnotation(editing.id, note, signal) : editing ? repairAnnotation(editing.id, slug, anchor!, signal) : createAnnotation(slug, anchor!, note, signal));
    if (r.ok) { close(); setNote(""); await onChanged(); await refresh(); } else if (r.status === 401) expire(); else setError(r.message);
  };
  return <><Button onClick={() => setOpen(true)} off={!anchor} label="annotate selection" />{(open || editing) && <div className="annotation-editor" role="dialog" aria-label="annotate"><textarea ref={editor} readOnly={!!editing && editing.status !== "resolved"} value={note} onChange={(e) => setNote(e.target.value)} aria-label="note" /><div><Button onClick={save} busy={busy} label="save" /><Button onClick={close} label="cancel" /></div>{error && <p className="error" role="alert">{error}</p>}</div>}{rows.map((a) => <span className="annotation-admin" key={a.id}><Button onClick={() => { setEditing(a); setNote(a.note_text); setAnchor(null); setOpen(true); }} label="edit" /><Delete annotation={a} expire={expire} onDeleted={async () => { await onChanged(); await refresh(); }} /></span>)}</>;
}
function Delete({ annotation, expire, onDeleted }: { annotation: Annotation; expire: () => void; onDeleted: () => void }) { const [confirm, setConfirm] = useState(false); const { run, busy } = useRequest("submit"); const remove = async () => { const r = await run((s) => deleteAnnotation(annotation.id, s)); if (r.ok) onDeleted(); else if (r.status === 401) expire(); }; return confirm ? <><Button onClick={remove} busy={busy} label="confirm delete" /><Button onClick={() => setConfirm(false)} label="cancel" /></> : <Button onClick={() => setConfirm(true)} label="delete" />; }
