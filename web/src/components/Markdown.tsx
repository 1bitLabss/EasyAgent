import { memo, useDeferredValue, useRef } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { splitStreamingMarkdown } from "@/lib/markdown";

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

const Closed = memo(function Closed({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={remarkPlugins}>{text}</ReactMarkdown>;
});

/** Freeze earlier paragraphs so a growing reply does not re-parse the whole document. */
function closedBlocks(text: string): string[] {
  if (!text) return [];
  const blocks: string[] = [];
  let fence = false;
  let start = 0;
  for (let i = 0; i < text.length - 1; i += 1) {
    if (text.startsWith("```", i) && (i === 0 || text[i - 1] === "\n")) fence = !fence;
    if (!fence && text[i] === "\n" && text[i + 1] === "\n" && i + 2 - start >= 1200) {
      const end = i + 2;
      blocks.push(text.slice(start, end));
      start = end;
    }
  }
  if (start < text.length) blocks.push(text.slice(start));
  return blocks;
}

function Lite({ text }: { text: string }) {
  const parts = splitStreamingMarkdown(text);
  const blocks = closedBlocks(parts.closed);
  return (
    <div className="chat-md">
      {blocks.map((block, index) => <Closed key={index} text={block} />)}
      {parts.tail ? <ReactMarkdown remarkPlugins={remarkPlugins}>{parts.tail}</ReactMarkdown> : null}
    </div>
  );
}

export const Markdown = memo(function Markdown({ text, streaming = false, freeze = false }: { text: string; streaming?: boolean; freeze?: boolean }) {
  const shown = useDeferredValue(text || "");
  const locked = useRef<string | null>(null);
  if (freeze) {
    if (locked.current === null) locked.current = shown;
    return <Lite text={locked.current} />;
  }
  if (!streaming) {
    return (
      <div className="chat-md">
        <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>{shown}</ReactMarkdown>
      </div>
    );
  }
  return <Lite text={shown} />;
});
