import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { namesMatch } from "@/lib/names";
import { useApp } from "@/store";

export function ConfirmDialog() {
  const confirm = useApp((state) => state.confirm);
  const close = useApp((state) => state.closeConfirm);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTyped("");
    setError("");
    setBusy(false);
  }, [confirm]);

  const needsName = Boolean(confirm?.name);
  const ready = !needsName || namesMatch(typed, confirm?.name || "");

  return (
    <Dialog open={Boolean(confirm)} onOpenChange={(open) => { if (!open) close(); }}>
      <DialogContent>
        <DialogTitle>{confirm?.title}</DialogTitle>
        <DialogDescription>{confirm?.copy}</DialogDescription>
        {needsName ? (
          <label className="mt-4 block text-sm">
            Type {confirm?.name} to confirm
            <Input className="mt-1" value={typed} autoFocus onChange={(event) => setTyped(event.target.value)} />
          </label>
        ) : null}
        {error ? <p className="mt-2 text-sm text-danger" role="alert">{error}</p> : null}
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" type="button" onClick={close}>Cancel</Button>
          <Button
            variant="danger"
            type="button"
            disabled={!ready || busy}
            onClick={() => {
              if (!confirm) return;
              setBusy(true);
              setError("");
              void confirm.run().then(close).catch((reason: Error) => {
                setError(reason.message);
                setBusy(false);
              });
            }}
          >
            {confirm?.submit || "Remove"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
