import { urlAttributes } from "html-url-attributes";
import { toJsxRuntime } from "hast-util-to-jsx-runtime";
import type { Root, RootContent } from "hast";
import { Fragment, type ReactNode } from "react";
import { jsx, jsxs } from "react/jsx-runtime";
import { defaultUrlTransform } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import remarkRehype from "remark-rehype";
import { unified } from "unified";
import { visit } from "unist-util-visit";
import { VFile } from "vfile";

/**
 * One parser for the session. Streaming never loads the highlighter.
 * Finished paragraphs are cached after the URL pass, so a new token does
 * not re-tokenize or sanitize them. The open tail is parsed and not stored.
 */
const processor = unified().use(remarkParse).use(remarkGfm).use(remarkRehype, { allowDangerousHtml: true });
const parsed = new Map<string, Root>();
let parses = 0;
// Same highlighter the finished transcript uses. Applied only after the reply
// settles, and only on a copy, so a cached paragraph is not tokenized again.
const applyHighlight = rehypeHighlight() as unknown as (tree: Root, file: VFile) => void;
const highlightFile = new VFile();

function treeFor(source: string, settle: boolean): Root {
  if (settle) {
    const cached = parsed.get(source);
    if (cached) return cached;
  }
  parses += 1;
  const tree = processor.runSync(processor.parse(source)) as Root;
  visit(tree, (node, index, parent) => {
    const current = node as RootContent | { type: "raw"; value?: unknown };
    if (current.type === "raw" && parent && typeof index === "number") {
      const text: RootContent = { type: "text", value: String(current.value ?? "") };
      parent.children[index] = text;
      return index;
    }
    if (current.type !== "element") return;
    const properties = current.properties || {};
    for (const key of Object.keys(urlAttributes)) {
      if (!Object.hasOwn(properties, key)) continue;
      const test = urlAttributes[key];
      if (test !== null && !test.includes(current.tagName)) continue;
      properties[key] = defaultUrlTransform(String(properties[key] || ""));
    }
  });
  if (settle) {
    parsed.set(source, tree);
    if (parsed.size > 500) {
      const oldest = parsed.keys().next().value;
      if (oldest) parsed.delete(oldest);
    }
  }
  return tree;
}

/** How many blocks have actually been parsed. Cached paragraphs do not add to this. */
export function streamingParseCount(): number {
  return parses;
}

function paintHighlight(tree: Root): Root {
  const copy = structuredClone(tree);
  applyHighlight(copy, highlightFile);
  return copy;
}

/** React for one markdown block. `settle` keeps the sanitized tree for the next token. */
export function renderStreamingBlock(source: string, settle = false, highlight = false): ReactNode {
  if (!source) return null;
  const tree = treeFor(source, settle);
  return toJsxRuntime(highlight ? paintHighlight(tree) : tree, {
    Fragment,
    ignoreInvalidStyle: true,
    jsx,
    jsxs,
    passKeys: true,
    passNode: true,
  });
}
