import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, queryClient } from "@/api";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";

type PhoneStatus = {
  enabled: boolean;
  lan_ip: string | null;
  port: number;
  pair_url: string;
  listening: boolean;
  devices: { id: string; name: string; paired_at: string }[];
  firewall: { hint: string; windows: boolean };
};

export function PhoneAccess() {
  const phone = useQuery({ queryKey: ["phone"], queryFn: () => api<PhoneStatus>("/api/phone") });
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  async function setEnabled(enabled: boolean) {
    setError("");
    setNote("");
    await api<PhoneStatus>("/api/phone", { method: "POST", json: { enabled } });
    await queryClient.invalidateQueries({ queryKey: ["phone"] });
  }

  async function revoke(id: string) {
    setError("");
    await api(`/api/phone/devices/${id}`, { method: "DELETE" });
    await queryClient.invalidateQueries({ queryKey: ["phone"] });
  }

  async function firewall() {
    setError("");
    const result = await api<{ added: boolean; detail: string }>("/api/phone/firewall", {
      method: "POST",
      json: { consent: true },
    });
    setNote(result.detail);
  }

  function notify() {
    if (typeof Notification === "undefined") {
      setNote("This browser does not show notifications.");
      return;
    }
    void Notification.requestPermission().then((result) => {
      setNote(result === "granted" ? "A finished reply can notify this phone." : "Notifications stay off.");
    });
  }

  const data = phone.data;

  return (
    <section id="phone" className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h2 className="text-base font-semibold">Phone access</h2>
      <p className="text-sm text-muted">
        Off until you turn it on. The computer keeps the chats. A phone on this home Wi-Fi pairs once, then the code changes.
      </p>
      {phone.isLoading ? <p className="text-sm text-muted">Checking the network…</p> : null}
      {phone.isError ? <p className="text-sm text-danger" role="alert">{(phone.error as Error).message}</p> : null}
      {data ? (
        <>
          <label className="flex min-h-11 items-center justify-between gap-3 text-sm">
            <span>Phone access</span>
            <Switch
              checked={data.enabled}
              aria-label="Phone access"
              onCheckedChange={(on) => {
                void setEnabled(on).catch((reason: Error) => setError(reason.message));
              }}
            />
          </label>
          {data.enabled && data.pair_url ? (
            <div className="space-y-2">
              <img
                src="/api/phone/qr.svg"
                alt="Pairing QR code"
                data-testid="phone-qr"
                width={240}
                height={240}
                className="h-60 w-60 rounded-md bg-[#f6f4ef]"
              />
              <p className="text-sm">On an iPhone, open the Camera app and point it at this code. Safari opens EasyAgent. From that page, Share, then Add to Home Screen.</p>
              <p className="break-all font-mono text-xs text-muted" data-testid="pair-url">{data.pair_url}</p>
            </div>
          ) : null}
          {data.enabled && !data.pair_url ? (
            <p className="text-sm text-muted">This computer has no LAN address, so a phone cannot open it yet.</p>
          ) : null}
          {data.devices.length ? (
            <ul className="space-y-2">
              {data.devices.map((device) => (
                <li key={device.id} className="flex min-h-11 items-center justify-between gap-3 text-sm">
                  <span>{device.name}</span>
                  <Button type="button" variant="outline" size="sm" aria-label={`Revoke ${device.name}`} onClick={() => void revoke(device.id).catch((reason: Error) => setError(reason.message))}>
                    Revoke
                  </Button>
                </li>
              ))}
            </ul>
          ) : data.enabled ? <p className="text-sm text-muted">No phone is paired yet.</p> : null}
          <p className="text-sm text-muted">{data.firewall.hint}</p>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" onClick={() => void firewall().catch((reason: Error) => setError(reason.message))}>
              Add the Windows Firewall rule
            </Button>
            <Button type="button" variant="outline" onClick={notify}>
              Notify when a reply finishes
            </Button>
          </div>
        </>
      ) : null}
      {note ? <p className="text-sm">{note}</p> : null}
      {error ? <p className="text-sm text-danger" role="alert">{error}</p> : null}
    </section>
  );
}
