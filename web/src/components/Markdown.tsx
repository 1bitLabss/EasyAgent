import { memo, useDeferredValue } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { splitStreamingMarkdown } from "@/lib/markdown";

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

const Closed = memo(function Closed({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={remarkPlugins}>{text}</ReactMarkdown>;
});

export const Markdown = memo(function Markdown({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const shown = useDeferredValue(text || "");
  if (!streaming) {
    return (
      <div className="chat-md">
        <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>{shown}</ReactMarkdown>
      </div>
    );
  }
  const parts = splitStreamingMarkdown(shown);
  return (
    <div className="chat-md">
      {parts.closed ? <Closed text={parts.closed} /> : null}
      {parts.tail ? <ReactMarkdown remarkPlugins={remarkPlugins}>{parts.tail}</ReactMarkdown> : null}
    </div>
  );
});
