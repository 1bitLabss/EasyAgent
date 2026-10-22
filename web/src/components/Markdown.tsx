import { Fragment, memo, useDeferredValue, useRef } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { settledBlocks, splitStreamingMarkdown } from "@/lib/markdown";
import { renderStreamingBlock } from "@/lib/streaming-markdown";

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

const Settled = memo(function Settled({ text, highlight }: { text: string; highlight: boolean }) {
  return <>{renderStreamingBlock(text, true, highlight)}</>;
});

function Lite({ text, highlight }: { text: string; highlight: boolean }) {
  const parts = splitStreamingMarkdown(text);
  const blocks = settledBlocks(parts.closed);
  return (
    <div className="chat-md">
      {blocks.map((block, index) =>
        block.endsWith("\n\n") ? (
          <Settled key={index} highlight={highlight} text={block} />
        ) : (
          <Fragment key={index}>{renderStreamingBlock(block, false, highlight)}</Fragment>
        ),
      )}
      {parts.tail ? renderStreamingBlock(parts.tail, false, highlight) : null}
    </div>
  );
}

export const Markdown = memo(function Markdown({ text, streaming = false, freeze = false }: { text: string; streaming?: boolean; freeze?: boolean }) {
  const shown = useDeferredValue(text || "");
  const locked = useRef<string | null>(null);
  // A reply that arrived live stays on this tree when it finishes. Highlighting
  // turns on in the code blocks that are already on screen. Hermes does the
  // same: the highlighter is deferred while status is running, then the same
  // block paints highlighted. A chat opened from disk never streamed, so it
  // uses the highlighted parser directly.
  const streamed = useRef(streaming);
  if (streaming) streamed.current = true;
  if (freeze) {
    if (locked.current === null) locked.current = shown;
    return <Lite highlight={false} text={locked.current} />;
  }
  if (streaming) return <Lite highlight={false} text={shown} />;
  if (streamed.current) return <Lite highlight text={text || ""} />;
  return (
    <div className="chat-md">
      <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>{text || ""}</ReactMarkdown>
    </div>
  );
});
