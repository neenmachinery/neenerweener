// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { document.cookie = "admin_hint=; Max-Age=0; Path=/"; document.body.innerHTML = ""; vi.unstubAllGlobals(); vi.resetModules(); });

it("a signed-out fresh context makes no session request and does not import the annotation chunk", async () => {
  let annotationChunkImported = 0;
  vi.doMock("./PostAnnotationAdmin", () => { annotationChunkImported++; return { PostAnnotationAdmin: () => null }; });
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  history.replaceState(null, "", "/posts/harness");
  const post = { title: "Harness", date: "2026-01-01", excerpt: null, tags: [], draft: false, html: "<p>text</p>" };
  document.body.innerHTML = `<script id="preload" type="application/json">${JSON.stringify({ page: { kind: "post", slug: "harness" }, "/api/posts/harness": post })}</script><div id="root"></div>`;
  const { default: App } = await import("./App"); const root = createRoot(document.getElementById("root")!); await act(async () => root.render(<App />)); await act(async () => await new Promise((resolve) => setTimeout(resolve, 0)));
  expect(fetch.mock.calls.filter(([url]) => url === "/api/admin/session")).toHaveLength(0); expect(annotationChunkImported).toBe(0); act(() => root.unmount());
});
