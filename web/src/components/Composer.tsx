import { ArrowUp, Mic, Plus } from "lucide-react";
import { forwardRef, memo, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";
import { browseBackward, browseForward, DRAFT_PERSIST_MS, isBrowsing, readDraft, resetBrowse, writeDraft } from "@/lib/composer-history";
import { noteComposerInput, resetComposer } from "@/lib/composer";
import { cn } from "@/lib/utils";

export type ComposerHandle = {
  focus: () => void;
  text: () => string;
  clear: () => void;
};

type ComposerProps = {
  running: boolean;
  asking: boolean;
  error: string;
  presence?: ReactNode;
  resumeRetry: boolean;
  resumeCont: boolean;
  onSend: (text: string, file: File | null) => Promise<void> | void;
  onStop: () => void;
  onStopAndSend: (text: string, file: File | null) => Promise<void> | void;
  onRetry: () => void;
  onContinue: () => void;
  onError: (message: string) => void;
  focusKey?: string;
  botId: string;
  chatId: string;
  menuOpen?: boolean;
  getHistory: () => string[];
};

export const Composer = memo(forwardRef<ComposerHandle, ComposerProps>(function Composer({
  running,
  asking,
  error,
  presence,
  resumeRetry,
  resumeCont,
  onSend,
  onStop,
  onStopAndSend,
  onRetry,
  onContinue,
  onError,
  focusKey,
  botId,
  chatId,
  menuOpen = false,
  getHistory,
}, ref) {
  const box = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [filled, setFilled] = useState(false);
  const [fileName, setFileName] = useState("");
  const [listening, setListening] = useState(false);
  const composing = useRef(false);
  const saveTimer = useRef(0);
  const stopRef = useRef(onStop);
  stopRef.current = onStop;
  const scope = `${botId}:${chatId}`;

  function focusSoon() {
    window.requestAnimationFrame(() => box.current?.focus());
  }

  function persistDraft(text: string) {
    writeDraft(botId, chatId, text);
  }

  function scheduleSave() {
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => persistDraft(box.current?.value || ""), DRAFT_PERSIST_MS);
  }

  function readText() {
    return box.current?.value || "";
  }

  function writeText(value: string) {
    const el = box.current;
    if (!el) return;
    el.value = value;
    setFilled(Boolean(value.trim()));
    if (!value) resetComposer(el);
    else noteComposerInput(el, true);
  }

  function takeFile() {
    const file = fileRef.current?.files?.[0] || null;
    return file;
  }

  function clearFile() {
    setFileName("");
    if (fileRef.current) fileRef.current.value = "";
  }

  useEffect(() => {
    box.current?.focus();
  }, [focusKey]);

  useEffect(() => {
    const saved = readDraft(botId, chatId);
    const el = box.current;
    if (el) {
      el.value = saved;
      setFilled(Boolean(saved.trim()));
      if (saved) noteComposerInput(el, true);
      else resetComposer(el);
    }
    const flush = () => writeDraft(botId, chatId, box.current?.value || "");
    window.addEventListener("pagehide", flush);
    return () => {
      window.clearTimeout(saveTimer.current);
      window.removeEventListener("pagehide", flush);
      flush();
    };
  }, [botId, chatId]);

  useEffect(() => {
    if (!running) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || menuOpen) return;
      event.preventDefault();
      stopRef.current();
      focusSoon();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [running, menuOpen]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || event.key.length !== 1) return;
      const target = event.target as HTMLElement | null;
      if (!target) return;
      const tag = target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable) return;
      if (target.closest("button, a")) return;
      const el = box.current;
      if (!el) return;
      event.preventDefault();
      el.focus();
      el.value += event.key;
      setFilled(true);
      noteComposerInput(el);
      scheduleSave();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [botId, chatId]);

  useImperativeHandle(ref, () => ({
    focus: () => box.current?.focus(),
    text: () => readText(),
    clear: () => {
      writeText("");
      clearFile();
    },
  }));

  async function send(text: string, file: File | null, run: (text: string, file: File | null) => Promise<void> | void) {
    const snapshot = readText();
    window.clearTimeout(saveTimer.current);
    writeText("");
    clearFile();
    persistDraft("");
    resetBrowse(scope);
    try {
      await run(text, file);
      focusSoon();
    } catch (reason) {
      writeText(snapshot);
      persistDraft(snapshot);
      if (file && fileRef.current) {
        setFileName(file.name);
      }
      onError(reason instanceof Error ? reason.message : "The reply failed.");
      focusSoon();
    }
  }

  function submit() {
    const text = readText().trim();
    const file = takeFile();
    if (!text && !file) return;
    void send(text, file, onSend);
  }

  function listen() {
    const Speech = (window as Window & {
      SpeechRecognition?: new () => SpeechEar;
      webkitSpeechRecognition?: new () => SpeechEar;
    }).SpeechRecognition
      || (window as Window & { webkitSpeechRecognition?: new () => SpeechEar }).webkitSpeechRecognition;
    if (!Speech) {
      onError("This browser cannot use the microphone.");
      return;
    }
    const ear = new Speech();
    ear.lang = "en-US";
    ear.onresult = (event) => writeText(event.results[0][0].transcript);
    ear.onend = () => setListening(false);
    setListening(true);
    ear.start();
  }

  const placeholder = listening ? "Listening" : running ? "Add to the current job" : asking ? "Or type an answer" : "Message";

  return (
    <form
      className="px-4 pb-5 pt-1"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="mx-auto w-full max-w-[760px]">
        {error ? <p className="mb-2 text-center text-sm text-danger" role="alert">{error}</p> : null}
        {fileName ? <p className="mb-1 text-center text-xs text-muted">{fileName}</p> : null}
        {presence || resumeRetry || resumeCont ? (
          <p className="composer-status mb-1 text-xs text-muted" role="status">
            {presence}
            {resumeRetry ? <button type="button" className="ml-2" onMouseDown={(event) => event.preventDefault()} onClick={() => { onRetry(); focusSoon(); }}>Retry</button> : null}
            {resumeCont ? <button type="button" className="ml-2" onMouseDown={(event) => event.preventDefault()} onClick={() => { onContinue(); focusSoon(); }}>Continue</button> : null}
          </p>
        ) : null}
        <div className="composer">
          <button type="button" className="mb-1 flex h-8 w-8 items-center justify-center rounded-full text-foreground" aria-label="Attach" onMouseDown={(event) => event.preventDefault()} onClick={() => fileRef.current?.click()}>
            <Plus className="h-5 w-5" />
          </button>
          <input ref={fileRef} className="hidden" type="file" onChange={(event) => setFileName(event.target.files?.[0]?.name || "")} />
          <label className="sr-only" htmlFor="draft">{running ? "Add to the current job" : "Message"}</label>
          <textarea
            id="draft"
            ref={box}
            rows={1}
            defaultValue=""
            placeholder={placeholder}
            onInput={(event) => {
              const el = event.currentTarget;
              const next = Boolean(el.value.trim());
              setFilled((current) => (current === next ? current : next));
              noteComposerInput(el);
              scheduleSave();
            }}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onBlur={() => {
              composing.current = false;
            }}
            onPaste={(event) => {
              const el = event.currentTarget;
              window.setTimeout(() => noteComposerInput(el, true), 0);
            }}
            onKeyDown={(event) => {
              if (composing.current && !event.nativeEvent.isComposing) composing.current = false;
              if (composing.current || event.nativeEvent.isComposing || (event.key === "Enter" && event.keyCode === 229)) return;
              if (event.key === "Escape" && running) {
                event.preventDefault();
                onStop();
                focusSoon();
                return;
              }
              const el = event.currentTarget;
              if (event.key === "ArrowUp" || event.key === "ArrowDown") {
                const browsing = isBrowsing(scope);
                const empty = !el.value.trim();
                if (event.key === "ArrowUp") {
                  if (!empty && !browsing) return;
                  if (!empty && el.selectionStart !== 0) return;
                  const next = browseBackward(scope, el.value, getHistory());
                  if (next == null) return;
                  event.preventDefault();
                  writeText(next);
                  return;
                }
                if (!browsing) return;
                const next = browseForward(scope, getHistory());
                if (!next) return;
                event.preventDefault();
                writeText(next.text);
                return;
              }
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
          />
          <button type="button" className={cn("mb-1 flex h-8 w-8 items-center justify-center rounded-full text-muted", listening && "text-foreground")} aria-label="Microphone" onMouseDown={(event) => event.preventDefault()} onClick={listen}>
            <Mic className="h-4 w-4" />
          </button>
          <button
            type="button"
            className={cn("stop-send-slot", running && (filled || fileName) && "is-on")}
            data-testid="stop-and-send"
            tabIndex={running && (filled || fileName) ? 0 : -1}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              if (!running) return;
              const text = readText().trim();
              const file = takeFile();
              if (!text && !file) return;
              void send(text, file, onStopAndSend);
            }}
          >
            Stop and send
          </button>
          <button
            type={running && !filled && !fileName ? "button" : "submit"}
            className="send-btn"
            aria-label={running && !filled && !fileName ? "Stop" : "Send"}
            disabled={!(running && !filled && !fileName) && !filled && !fileName}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              if (running && !filled && !fileName) {
                onStop();
                focusSoon();
              }
            }}
          >
            {running && !filled && !fileName ? <span className="stop-square" /> : <ArrowUp className="h-4 w-4" />}
          </button>
        </div>
      </div>
    </form>
  );
}));

type SpeechEar = {
  lang: string;
  onresult: ((event: { results: { 0: { 0: { transcript: string } } } }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
};
