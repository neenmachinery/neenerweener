import { useEffect, useState } from "react";
import { deleteAnnotation, updateAnnotation, type Annotation } from "./adminApi";
import { Button } from "./EntryEditor";
import type { AdminPageProps } from "./adminPages";
import { useRequest } from "./useRequest";

export default function AdminAnnotations({ send, active, expire }: AdminPageProps) {
  const [rows, setRows] = useState<Annotation[]>([]); const [error, setError] = useState(""); const { run, busy } = useRequest("load");
  const load = async () => { const r = await run((signal) => send("GET", "/api/admin/annotations", undefined, signal)); if (r.ok) { setRows(r.data as unknown as Annotation[]); setError(""); } else if (r.status === 401) expire(); else setError(r.message); };
  useEffect(() => { if (active) load(); }, [active]);
  const ordered = [...rows].sort((a, b) => Number(b.status !== "resolved") - Number(a.status !== "resolved") || a.id - b.id);
  return <><h1 tabIndex={-1}>annotations</h1>{error && <p className="error" role="alert">{error}</p>}<ul className="admin-links">{ordered.map((a) => <Row key={a.id} annotation={a} send={send} expire={expire} busy={busy} onChanged={load} />)}</ul></>;
}
function Row({ annotation, send, expire, busy, onChanged }: { annotation: Annotation; send: AdminPageProps["send"]; expire: () => void; busy: boolean; onChanged: () => void }) {
  const [editing, setEditing] = useState(false); const [text, setText] = useState(annotation.note_text); const [confirm, setConfirm] = useState(false); const [target, setTarget] = useState(""); const [error, setError] = useState(""); const { run } = useRequest("submit");
  const save = async () => { const r = await run((s) => send("PATCH", `/api/admin/annotations/${annotation.id}`, { note_text: text }, s)); if (r.ok) { setEditing(false); onChanged(); } else if (r.status === 401) expire(); else setError(r.message); };
  const remove = async () => { const r = await run((s) => deleteAnnotation(annotation.id, s)); if (r.ok) onChanged(); else if (r.status === 401) expire(); else setError(r.message); };
  const targetLink = target ? `/posts/${target}?repair=${annotation.id}` : `/posts/${annotation.post_slug}?repair=${annotation.id}`;
  const targetAction = annotation.status === "orphaned" && annotation.reason === "missing-post" ? <><label>target post <input aria-label="target post" value={target} onChange={(event) => setTarget(event.target.value)} /></label> <a href={targetLink}>repair</a></> : <a href={`/posts/${annotation.post_slug}`}>open post</a>;
  return <li><blockquote>{annotation.selector.exact}</blockquote>{editing ? <><textarea aria-label="note" value={text} onChange={(e) => setText(e.target.value)} /><Button onClick={save} label="save" /></> : <span className="label">{annotation.note_text}</span>}<span className="muted">{annotation.status}{annotation.reason ? ` · ${annotation.reason}` : ""} · {annotation.created_at} · {annotation.updated_at}</span> {targetAction} {confirm ? <><Button onClick={remove} busy={busy} label="confirm delete" /><Button onClick={() => setConfirm(false)} label="cancel" /></> : <><Button onClick={() => setEditing(true)} label="edit" /><Button onClick={() => setConfirm(true)} label="delete" /></>}{error && <span className="error">{error}</span>}</li>;
}
