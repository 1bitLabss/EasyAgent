/** Split a streaming reply so only the open tail is re-parsed. */
export const STREAM_TAIL_CAP = 2000;

export function splitStreamingMarkdown(text: string, cap = STREAM_TAIL_CAP): { closed: string; tail: string } {
  const source = text || "";
  let fence = false;
  let lastBreak = -1;
  for (let i = 0; i < source.length; i += 1) {
    if (source.startsWith("```", i) && (i === 0 || source[i - 1] === "\n")) {
      fence = !fence;
    }
    if (!fence && source[i] === "\n" && source[i + 1] === "\n") lastBreak = i + 2;
  }
  let closed = lastBreak > 0 ? source.slice(0, lastBreak) : "";
  let tail = lastBreak > 0 ? source.slice(lastBreak) : source;
  if (tail.length <= cap) return { closed, tail };
  const cutFrom = tail.length - cap;
  let at = tail.lastIndexOf("\n", cutFrom);
  if (at < 1) at = tail.lastIndexOf(" ", cutFrom);
  if (at < 1) at = cutFrom;
  else at += 1;
  closed += tail.slice(0, at);
  tail = tail.slice(at);
  return { closed, tail };
}

/**
 * Plain words for the voice. Fenced code blocks are skipped entirely, inline
 * markers are removed, and links keep their text so the voice never reads
 * asterisks or code.
 */
export function toSpeakableText(markdown: string): string {
  let text = (markdown || "").replace(/```[\s\S]*?```/g, " ");
  text = text.replace(/`([^`]*)`/g, "$1");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1");
  text = text.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  text = text.replace(/^\s*>+\s?/gm, "");
  text = text.replace(/^\s*[-*+]\s+/gm, "");
  text = text.replace(/^\s*\d+\.\s+/gm, "");
  text = text.replace(/(\*\*|__)(.+?)\1/g, "$2");
  text = text.replace(/(\*|_)(.+?)\1/g, "$2");
  text = text.replace(/~~(.+?)~~/g, "$1");
  text = text.replace(/[*_`]+/g, "");
  return text.replace(/\s+/g, " ").trim();
}

/** Paragraphs that already ended. Each block stays put while the tail grows. */
export function settledBlocks(text: string): string[] {
  if (!text) return [];
  const blocks: string[] = [];
  let fence = false;
  let start = 0;
  for (let i = 0; i < text.length - 1; i += 1) {
    if (text.startsWith("```", i) && (i === 0 || text[i - 1] === "\n")) fence = !fence;
    if (!fence && text[i] === "\n" && text[i + 1] === "\n") {
      blocks.push(text.slice(start, i + 2));
      start = i + 2;
      i += 1;
    }
  }
  if (start < text.length) blocks.push(text.slice(start));
  return blocks;
}
