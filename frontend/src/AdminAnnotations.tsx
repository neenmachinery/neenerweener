import { useEffect, useState } from "react";
import type { Annotation } from "./adminApi";
import { Button } from "./EntryEditor";
import type { AdminPageProps } from "./adminPages";
import { useRequest } from "./useRequest";

type Post = { slug: string; title: string };
export default function AdminAnnotations({ send, active, expire }: AdminPageProps) {
  const [rows, setRows] = useState<Annotation[]>([]); const [posts, setPosts] = useState<Post[]>([]); const [error, setError] = useState(""); const { run } = useRequest("load");
  const load = async () => { const r = await run(async (signal) => { const annotations = await send<Annotation[]>("GET", "/api/admin/annotations", undefined, signal); if (!annotations.ok) return annotations; const response = await fetch("/api/posts", { signal }); return response.ok ? { ok: true as const, data: { annotations: annotations.data, posts: await response.json() as Post[] } } : { ok: false as const, status: response.status, message: "could not load posts" }; }); if (r.ok) { setRows(r.data.annotations); setPosts(r.data.posts); setError(""); } else if (r.status === 401) expire(); else setError(r.message); };
  useEffect(() => { if (active) void load(); }, [active]);
  const ordered = [...rows].sort((a, b) => Number(b.status !== "resolved") - Number(a.status !== "resolved") || a.post_slug.localeCompare(b.post_slug) || a.id - b.id);
  return <><h1 tabIndex={-1}>annotations</h1>{error && <p className="error" role="alert">{error}</p>}<ul className="admin-links">{ordered.map((a) => <Row key={a.id} annotation={a} posts={posts} send={send} expire={expire} onChanged={load} />)}</ul></>;
}
function Row({ annotation, posts, send, expire, onChanged }: { annotation: Annotation; posts: Post[]; send: AdminPageProps["send"]; expire: () => void; onChanged: () => void }) {
  const [editing, setEditing] = useState(false); const [text, setText] = useState(annotation.note_text); const [confirm, setConfirm] = useState(false); const [target, setTarget] = useState(""); const [error, setError] = useState(""); const { run, busy } = useRequest("submit");
  const save = async () => { const r = await run((s) => send("PATCH", `/api/admin/annotations/${annotation.id}`, { note_text: text }, s)); if (r.ok) { setEditing(false); onChanged(); } else if (r.status === 401) expire(); else setError(r.message); };
  const remove = async () => { const r = await run((s) => send("DELETE", `/api/admin/annotations/${annotation.id}`, undefined, s)); if (r.ok) onChanged(); else if (r.status === 401) expire(); else setError(r.message); };
  const missing = annotation.status === "orphaned" && annotation.reason === "missing-post";
  const targetAction = missing ? <><label>target post <select aria-label="target post" value={target} onChange={(event) => setTarget(event.target.value)}><option value="">select post</option>{posts.map((post) => <option key={post.slug} value={post.slug}>{post.title}</option>)}</select></label> {target && <a href={`/posts/${target}?repair=${annotation.id}`}>repair</a>}</> : annotation.status === "orphaned" ? <a href={`/posts/${annotation.post_slug}?repair=${annotation.id}`}>repair</a> : <a href={`/posts/${annotation.post_slug}`}>open post</a>;
  return <li><blockquote>{annotation.selector.exact}</blockquote>{editing ? <><textarea aria-label="note" value={text} onChange={(e) => setText(e.target.value)} /><Button onClick={save} busy={busy} label="save" /></> : <span className="label">{annotation.note_text}</span>}<span className="muted">{annotation.status}{annotation.reason ? ` · ${annotation.reason}` : ""} · {annotation.created_at} · {annotation.updated_at}</span> {targetAction} {confirm ? <><Button onClick={remove} busy={busy} label="confirm delete" /><Button onClick={() => setConfirm(false)} label="cancel" /></> : <><Button onClick={() => setEditing(true)} label="edit" /><Button onClick={() => setConfirm(true)} label="delete" /></>}{error && <span className="error">{error}</span>}</li>;
}
