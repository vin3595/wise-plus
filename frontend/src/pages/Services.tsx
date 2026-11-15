import { useEffect, useState } from "react";
import { BookOpenCheck, Boxes, NotebookPen, ReceiptText, type LucideIcon } from "lucide-react";
import { api } from "../api";
import { useSession } from "../session";
import { Card, ErrorText, useAction } from "../ui";

/** The services a business can switch on. Same list as the sign-up page. */
export const SERVICES: { key: string; name: string; about: string; icon: LucideIcon }[] = [
  { key: "pos", name: "POS billing", about: "Bills, KOTs, kitchen screen, cash and day close, QR ordering, loyalty.", icon: ReceiptText },
  { key: "stock", name: "Inventory", about: "Stock, recipes, closing counts, transfers, spot checks, menu profit.", icon: Boxes },
  { key: "bills", name: "Bill scan & supplier dues", about: "Photo or WhatsApp a supplier's bill and it's entered for you. See what you owe each supplier.", icon: BookOpenCheck },
  { key: "sheets", name: "Daily cash sheet", about: "Photo of your daily sheet, read and saved. Ask for any day's cash or the month's expenses.", icon: NotebookPen },
];

export function ServicePicker({ value, onChange, disabled }: { value: string[]; onChange: (v: string[]) => void; disabled?: boolean }) {
  return (
    <div className="services" role="group" aria-label="Services">
      {SERVICES.map(({ key, name, about, icon: Icon }) => {
        const on = value.includes(key);
        return (
          <button key={key} type="button" className={`service ${on ? "on" : ""}`} aria-pressed={on} disabled={disabled}
            onClick={() => onChange(on ? value.filter((k) => k !== key) : [...value, key])}>
            <Icon size={22} aria-hidden />
            <span><strong>{name}</strong><small>{about}</small></span>
            <span className="tick" aria-hidden>{on ? "✓" : ""}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Settings > Services: switch services on or off. Works straight away; nothing is deleted. */
export default function Services() {
  const { me, refresh } = useSession();
  const [on, setOn] = useState<string[]>(me?.modules || SERVICES.map((s) => s.key));
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();
  useEffect(() => { api<{ on: string[] }>("/tenant/modules").then((r) => setOn(r.on)); }, []);
  const change = (v: string[]) => {
    if (!v.length) return;
    setSaved(false);
    run(async () => {
      const r = await api<{ on: string[] }>("/tenant/modules", { method: "PUT", body: { modules: v } });
      setOn(r.on); setSaved(true);
      await refresh();
    });
  };
  return (
    <div className="stack">
      <h1>Services</h1>
      <Card>
        <p className="muted small">Tap to switch a service on or off. It changes straight away for everyone. Switching off hides it; nothing you entered is deleted. The WhatsApp assistant works with whatever is on.</p>
        <ServicePicker value={on} onChange={change} disabled={busy} />
        <ErrorText error={error} />
        {saved && <p className="ok">Saved.</p>}
      </Card>
    </div>
  );
}
