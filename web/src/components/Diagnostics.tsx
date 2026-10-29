import { useState } from "react";

export function CopyDiagnostics() {
  const [note, setNote] = useState("");
  return (
    <section id="help" className="space-y-2 rounded-lg border border-border bg-card p-4">
      <h2 className="font-medium">Help</h2>
      <p className="text-sm text-muted">Copy the logs from this computer. They stay here. Nothing is uploaded.</p>
      <button
        type="button"
        data-testid="copy-diagnostics"
        className="rounded-full border border-border px-3 py-1 text-sm"
        onClick={() => {
          void fetch("/api/diag/bundle")
            .then(async (response) => {
              if (!response.ok) throw new Error("Could not collect diagnostics.");
              const blob = await response.blob();
              const url = URL.createObjectURL(blob);
              const link = document.createElement("a");
              link.href = url;
              link.download = "easyagent-diagnostics.zip";
              link.click();
              URL.revokeObjectURL(url);
              setNote("Saved easyagent-diagnostics.zip.");
            })
            .catch((reason: Error) => setNote(reason.message));
        }}
      >
        Copy diagnostics
      </button>
      {note ? <p className="text-sm text-muted">{note}</p> : null}
    </section>
  );
}
