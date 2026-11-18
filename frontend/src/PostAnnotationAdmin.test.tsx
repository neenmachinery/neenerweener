// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  list: vi.fn(), create: vi.fn(), update: vi.fn(), repair: vi.fn(), remove: vi.fn(),
}));
vi.mock("./adminApi", async (original) => ({
  ...await original<typeof import("./adminApi")>(),
  listAnnotations: api.list, createAnnotation: api.create, updateAnnotation: api.update,
  repairAnnotation: api.repair, deleteAnnotation: api.remove,
}));
import { PostAnnotationAdmin, selectedAnchor } from "./PostAnnotationAdmin";
import type { Annotation } from "./adminApi";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const ok = <T,>(data: T) => Promise.resolve({ ok: true as const, data });
const fail = (message: string, status = 422) => Promise.resolve({ ok: false as const, status, message });
const row = (overrides: Partial<Annotation> = {}): Annotation => ({ id: 7, post_slug: "harness", selector: { block_kind: "paragraph", block_index: 0, exact: "selected" }, note_text: "note text", status: "resolved", reason: null, created_at: "2026-01-01", updated_at: "2026-01-02", ...overrides });
let reactRoot: Root | undefined;
const flush = () => act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));
const settle = async () => { for (let i = 0; i < 4; i++) await flush(); };
const type = (element: HTMLTextAreaElement, value: string) => act(() => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(element, value); element.dispatchEvent(new Event("input", { bubbles: true })); });
const button = (name: string) => [...document.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent?.startsWith(name))!;
const click = (name: string) => act(() => button(name).click());

beforeEach(() => {
  api.list.mockReset().mockImplementation(() => ok([])); api.create.mockReset().mockImplementation(() => ok(row())); api.update.mockReset().mockImplementation(() => ok(row())); api.repair.mockReset().mockImplementation(() => ok(row())); api.remove.mockReset().mockImplementation(() => ok(undefined));
  history.replaceState(null, "", "/posts/harness");
});
afterEach(() => { act(() => reactRoot?.unmount()); reactRoot = undefined; document.body.innerHTML = ""; getSelection()?.removeAllRanges(); vi.restoreAllMocks(); });

function article(markup = "<p>before <em>selected</em> after</p>") { const root = document.createElement("div"); root.innerHTML = markup; document.body.append(root); return root; }
function select(start: Node, startOffset: number, end: Node, endOffset: number) { const range = document.createRange(); range.setStart(start, startOffset); range.setEnd(end, endOffset); const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range); document.dispatchEvent(new Event("selectionchange")); }
async function render(root = article(), props: { slug?: string; expire?: () => void; onChanged?: () => Promise<unknown> } = {}) {
  const host = document.createElement("div"); document.body.append(host); reactRoot = createRoot(host);
  await act(async () => reactRoot!.render(<PostAnnotationAdmin root={root} slug={props.slug ?? "harness"} expire={props.expire ?? vi.fn()} onChanged={props.onChanged ?? (() => Promise.resolve())} />)); await settle();
  return root;
}
function selectWord(root: HTMLElement) { const text = root.querySelector("em")!.firstChild!; act(() => select(text, 0, text, text.textContent!.length)); }

it.each([
  ["emphasis", "<p>before <em>selected</em> after</p>", "em", "selected", "paragraph"],
  ["link", '<p>before <a href="/x">selected</a> after</p>', "a", "selected", "paragraph"],
  ["inline code", "<p>before <code>selected</code> after</p>", "code", "selected", "paragraph"],
  ["list item", "<ul><li>before <em>selected</em> after</li></ul>", "em", "selected", "list_item"],
])("computes dom range anchor across %s", (_name, markup, selector, exact, kind) => {
  const root = article(markup); const target = root.querySelector(selector)!.firstChild!; select(target, 0, target, target.textContent!.length);
  const anchor = selectedAnchor(root)!;
  expect(anchor).toMatchObject({ block_text: `before ${exact} after`, start: 7, end: 15, block_kind: kind, block_index_hint: 0 });
});

it("uses range offsets for the second repeated phrase", () => {
  const root = article("<p>same x same y</p>"); const text = root.querySelector("p")!.firstChild!; select(text, 7, text, 11);
  expect(selectedAnchor(root)).toMatchObject({ block_text: "same x same y", start: 7, end: 11 });
});

it("excludes rendered annotation controls from the dom payload", () => {
  const root = article('<p><span class="ann"><mark>before</mark><sup class="ann-ref-wide">1</sup><span class="sidenote">note text</span></span> selected after</p>');
  const text = root.querySelector("p")!.lastChild!; select(text, 1, text, 9);
  expect(selectedAnchor(root)).toMatchObject({ block_text: "before selected after", start: 7, end: 15 });
});

it("refuses a selection across two blocks", () => {
  const root = article("<p>first</p><p>second</p>"); select(root.firstElementChild!.firstChild!, 0, root.lastElementChild!.firstChild!, 6);
  expect(selectedAnchor(root)).toBeNull();
});

it("drops a double submit", async () => {
  let answer!: (value: Awaited<ReturnType<typeof ok<Annotation>>>) => void;
  api.create.mockImplementation(() => new Promise((resolve) => { answer = resolve; }));
  const root = await render(); selectWord(root); click("annotate selection"); type(document.querySelector("textarea")!, "note text");
  act(() => { button("save").click(); button("save").click(); }); await flush();
  expect(api.create).toHaveBeenCalledTimes(1); await act(async () => answer(await ok(row())));
});

it("a newer load supersedes the old load", async () => {
  const replies: ((value: Awaited<ReturnType<typeof ok<Annotation[]>>>) => void)[] = []; const signals: AbortSignal[] = [];
  api.list.mockImplementation((_slug, signal) => new Promise((resolve) => { replies.push(resolve); signals.push(signal); }));
  const root = article('<p><span class="ann" data-annotation-id="9"><span class="sidenote"></span></span></p>'); const host = document.createElement("div"); document.body.append(host); reactRoot = createRoot(host);
  await act(async () => reactRoot!.render(<PostAnnotationAdmin root={root} slug="first" expire={vi.fn()} onChanged={() => Promise.resolve()} />));
  await act(async () => reactRoot!.render(<PostAnnotationAdmin root={root} slug="second" expire={vi.fn()} onChanged={() => Promise.resolve()} />));
  expect(signals[0].aborted).toBe(true); await act(async () => replies[1](await ok([row({ id: 9 })]))); expect([...document.querySelectorAll("button")].filter((item) => item.textContent?.startsWith("edit"))).toHaveLength(1);
  await act(async () => replies[0](await ok([row({ id: 8 })]))); expect([...document.querySelectorAll("button")].filter((item) => item.textContent?.startsWith("edit"))).toHaveLength(1);
});

it("does not call a setter callback after unmount", async () => {
  let answer!: (value: Awaited<ReturnType<typeof fail>>) => void; const expire = vi.fn(); api.list.mockImplementation(() => new Promise((resolve) => { answer = resolve; }));
  await render(article(), { expire }); act(() => reactRoot!.unmount()); reactRoot = undefined; await act(async () => answer(await fail("", 401))); expect(expire).not.toHaveBeenCalled();
});

it.each(["selection is not annotatable", "selection is ambiguous", "annotation overlaps another annotation"])("shows %s inline and keeps the typed note", async (message) => {
  api.create.mockImplementation(() => fail(message)); const root = await render(); selectWord(root); click("annotate selection"); const note = document.querySelector("textarea")!; type(note, "note text"); click("save"); await settle();
  expect(document.querySelector('[role="alert"]')!.textContent).toBe(message); expect(note.value).toBe("note text");
});

it("uses two-step delete", async () => {
  api.list.mockImplementation(() => ok([row()])); await render(article('<p><span class="ann" data-annotation-id="7"><span class="sidenote"></span></span></p>')); click("delete"); expect(api.remove).not.toHaveBeenCalled(); click("confirm delete"); await settle(); expect(api.remove).toHaveBeenCalledTimes(1);
});

it("repairs with put semantics, keeps the note, and never creates", async () => {
  history.replaceState(null, "", "/posts/harness?repair=7"); api.list.mockImplementation(() => ok([row({ status: "orphaned", reason: "not-found" })])); const root = await render();
  expect((document.querySelector("textarea") as HTMLTextAreaElement).value).toBe("note text"); selectWord(root); click("save"); await settle();
  expect(api.repair).toHaveBeenCalledWith(7, "harness", expect.objectContaining({ block_text: "before selected after", start: 7, end: 15 }), expect.any(AbortSignal)); expect(api.create).not.toHaveBeenCalled(); expect(api.update).not.toHaveBeenCalled();
});
