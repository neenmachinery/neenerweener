// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import AdminAnnotations from "./AdminAnnotations";
import type { Annotation } from "./adminApi";
import type { AdminPageProps } from "./adminPages";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const annotation: Annotation = { id: 4, post_slug: "missing", selector: { block_kind: "paragraph", block_index: 0, exact: "quote" }, note_text: "note text", status: "orphaned", reason: "missing-post", created_at: "created", updated_at: "updated" };
let root: Root | undefined;
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.innerHTML = ""; vi.unstubAllGlobals(); });
const flush = () => act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));
async function render(send: AdminPageProps["send"], expire = vi.fn()) { vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve([{ slug: "harness", title: "Harness" }]) }))); const host = document.createElement("div"); document.body.append(host); root = createRoot(host); await act(async () => root!.render(<AdminAnnotations send={send} expire={expire} profile={null} loadProfile={vi.fn()} setProfile={vi.fn()} active />)); for (let i = 0; i < 4; i++) await flush(); return expire; }

it("expires the admin session on 401", async () => {
  const send = vi.fn(() => Promise.resolve({ ok: false as const, status: 401, message: "" })); const expire = await render(send as AdminPageProps["send"]); expect(expire).toHaveBeenCalledTimes(1);
});

it("chooses a missing-post repair target from existing posts", async () => {
  const send = vi.fn(() => Promise.resolve({ ok: true as const, data: [annotation] })); await render(send as AdminPageProps["send"]); const select = document.querySelector("select")!;
  expect(document.querySelector('a[href*="repair"]')).toBeNull();
  act(() => { select.value = "harness"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(document.querySelector('a[href="/posts/harness?repair=4"]')).not.toBeNull(); expect(document.querySelector('input[aria-label="target post"]')).toBeNull();
});

it("requires confirmation before deleting and uses the supplied send", async () => {
  const send = vi.fn((method: string) => Promise.resolve({ ok: true as const, data: method === "GET" ? [annotation] : undefined })); await render(send as AdminPageProps["send"]);
  act(() => [...document.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent?.startsWith("delete"))!.click()); expect(send).not.toHaveBeenCalledWith("DELETE", expect.anything(), expect.anything(), expect.anything());
  act(() => [...document.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent?.startsWith("confirm delete"))!.click()); await flush(); expect(send).toHaveBeenCalledWith("DELETE", "/api/admin/annotations/4", undefined, expect.any(AbortSignal));
});
