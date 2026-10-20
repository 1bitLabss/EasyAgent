import { ArrowUp, Mic, Plus, Square } from "lucide-react";
import { forwardRef, memo, useEffect, useImperativeHandle, useRef, useState } from "react";
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
  presence: string;
  presenceKind: string;
  resumeRetry: boolean;
  resumeCont: boolean;
  onSend: (text: string, file: File | null) => Promise<void> | void;
  onStop: () => void;
  onStopAndSend: (text: string, file: File | null) => Promise<void> | void;
  onRetry: () => void;
  onContinue: () => void;
  onError: (message: string) => void;
  focusKey?: string;
};

export const Composer = memo(forwardRef<ComposerHandle, ComposerProps>(function Composer({
  running,
  asking,
  error,
  presence,
  presenceKind,
  resumeRetry,
  resumeCont,
  onSend,
  onStop,
  onStopAndSend,
  onRetry,
  onContinue,
  onError,
  focusKey,
}, ref) {
  const box = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [filled, setFilled] = useState(false);
  const [fileName, setFileName] = useState("");
  const [listening, setListening] = useState(false);

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
    writeText("");
    clearFile();
    try {
      await run(text, file);
    } catch (reason) {
      writeText(snapshot);
      if (file && fileRef.current) {
        setFileName(file.name);
      }
      onError(reason instanceof Error ? reason.message : "The reply failed.");
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
      className="composer-dock shrink-0 px-4 pb-5 pt-1"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="mx-auto w-full max-w-[760px]">
        {error ? <p className="mb-2 text-center text-sm text-danger" role="alert">{error}</p> : null}
        {fileName ? <p className="mb-1 text-center text-xs text-muted">{fileName}</p> : null}
        <div className="mb-2 flex justify-center gap-3 text-xs text-muted">
          {resumeRetry ? <button type="button" onClick={onRetry}>Retry</button> : null}
          {resumeCont ? <button type="button" onClick={onContinue}>Continue</button> : null}
          {running ? (
            <button type="button" className="inline-flex items-center gap-1" onClick={onStop}>
              <Square className="h-3 w-3" /> Stop
            </button>
          ) : null}
        </div>
        {running ? <p className="mb-1 text-center text-xs text-muted">Add to the current job</p> : null}
        <p className="mb-1 text-center text-xs text-muted" role="status" data-testid="run-state" data-state={presenceKind}>{presence}</p>
        <div className="composer">
          <button type="button" className="mb-1 flex h-8 w-8 items-center justify-center rounded-full text-foreground" aria-label="Attach" onClick={() => fileRef.current?.click()}>
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
            }}
            onPaste={(event) => {
              const el = event.currentTarget;
              window.setTimeout(() => noteComposerInput(el, true), 0);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
          />
          <button type="button" className={cn("mb-1 flex h-8 w-8 items-center justify-center rounded-full text-muted", listening && "text-foreground")} aria-label="Microphone" onClick={listen}>
            <Mic className="h-4 w-4" />
          </button>
          {running ? (
            <button
              type="button"
              className="mb-1 rounded-full px-3 text-sm"
              data-testid="stop-and-send"
              disabled={!filled && !fileName}
              onClick={() => {
                const text = readText().trim();
                const file = takeFile();
                if (!text && !file) return;
                void send(text, file, onStopAndSend);
              }}
            >
              Stop and send
            </button>
          ) : null}
          <button type="submit" className="send-btn" aria-label="Send" disabled={!filled && !fileName}>
            <ArrowUp className="h-4 w-4" />
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
