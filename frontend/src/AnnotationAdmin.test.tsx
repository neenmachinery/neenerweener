// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { selectedSelector } from "./AnnotationAdmin";

describe("annotation selectors", () => {
  it.each([
    ["emphasis", "<p>before <em>selected text</em> after</p>", "selected text"],
    ["links", '<p>read <a href="/x">the selected link</a> now</p>', "the selected link"],
    ["code", "<p>run <code>selected command</code> now</p>", "selected command"],
  ])("builds a canonical selector across %s", (_name, markup, exact) => {
    const root = document.createElement("div"); root.innerHTML = markup; document.body.append(root);
    const target = root.querySelector("em,a,code")!.firstChild!;
    const range = document.createRange(); range.setStart(target, 0); range.setEnd(target, target.textContent!.length);
    const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
    expect(selectedSelector(root)).toMatchObject({ block_kind: "paragraph", block_index: 0, exact });
  });
});
