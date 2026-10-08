import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

export function Markdown({ text }: { text: string }) {
  return (
    <div className="chat-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {text || ""}
      </ReactMarkdown>
    </div>
  );
}

export function Thinking({ text, open = true }: { text?: string; open?: boolean }) {
  const body = (text || "").trim();
  if (!body) return null;
  const parts = body.split(/\n{2,}/).map((part) => part.trim()).filter(Boolean);
  return (
    <details className="mb-2 rounded-md bg-black/5 px-2 py-1 dark:bg-white/5" open={open}>
      <summary className="cursor-pointer text-xs font-medium text-muted">Thinking</summary>
      <div className="mt-1 space-y-2 text-[13px] text-muted">
        {parts.map((part, index) => (
          <p key={index}>{part}</p>
        ))}
      </div>
    </details>
  );
}
