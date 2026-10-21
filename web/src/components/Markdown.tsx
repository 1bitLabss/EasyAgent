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

function Lite({ text }: { text: string }) {
  const parts = splitStreamingMarkdown(text);
  return (
    <div className="chat-md">
      {parts.closed ? <Closed text={parts.closed} /> : null}
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
