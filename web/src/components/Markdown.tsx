import { Fragment, memo, useDeferredValue, useRef } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { settledBlocks, splitStreamingMarkdown } from "@/lib/markdown";
import { renderStreamingBlock } from "@/lib/streaming-markdown";

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

const Settled = memo(function Settled({ text }: { text: string }) {
  return <>{renderStreamingBlock(text, true)}</>;
});

function Lite({ text }: { text: string }) {
  const parts = splitStreamingMarkdown(text);
  const blocks = settledBlocks(parts.closed);
  return (
    <div className="chat-md">
      {blocks.map((block, index) =>
        block.endsWith("\n\n") ? (
          <Settled key={index} text={block} />
        ) : (
          <Fragment key={index}>{renderStreamingBlock(block)}</Fragment>
        ),
      )}
      {parts.tail ? renderStreamingBlock(parts.tail) : null}
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
