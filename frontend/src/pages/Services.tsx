import { useEffect, useState } from "react";
import { BookOpenCheck, Boxes, NotebookPen, ReceiptText, type LucideIcon } from "lucide-react";
import { api } from "../api";
import { useSession } from "../session";
import { Card, ErrorText, useAction } from "../ui";

/** The services a business can switch on. Same list as the sign-up page. */
export const SERVICES: { key: string; name: string; about: string; icon: LucideIcon; price?: string }[] = [
  { key: "pos", name: "POS billing", about: "Bills, KOTs, kitchen screen, cash and day close, QR ordering, loyalty.", icon: ReceiptText, price: "₹599/month" },
  { key: "stock", name: "Inventory", about: "Stock, recipes, closing counts, transfers, spot checks, menu profit.", icon: Boxes, price: "₹299/month" },
  { key: "bills", name: "Bill scan & supplier dues", about: "Photo or WhatsApp a supplier's bill and it's entered for you. See what you owe each supplier.", icon: BookOpenCheck, price: "₹299/month" },
  { key: "sheets", name: "Daily cash sheet", about: "Photo of your daily sheet, read and saved. Ask for any day's cash or the month's expenses.", icon: NotebookPen },
];

/** only: the services this business may pick (the daily cash sheet is only for your own accounts). */
export function ServicePicker({ value, onChange, disabled, only = ["pos", "stock", "bills"], prices = false }: {
  value: string[]; onChange: (v: string[]) => void; disabled?: boolean; only?: string[]; prices?: boolean }) {
  return (
    <div className="services" role="group" aria-label="Services">
      {SERVICES.filter((s) => only.includes(s.key)).map(({ key, name, about, icon: Icon, price }) => {
        const on = value.includes(key);
        return (
          <button key={key} type="button" className={`service ${on ? "on" : ""}`} aria-pressed={on} disabled={disabled}
            onClick={() => onChange(on ? value.filter((k) => k !== key) : [...value, key])}>
            <Icon size={22} aria-hidden />
            <span><strong>{name}{prices && price ? <em className="svc-price">{price}</em> : null}</strong><small>{about}</small></span>
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
  const [on, setOn] = useState<string[]>(me?.modules || ["pos", "stock", "bills"]);
  const [offered, setOffered] = useState<string[]>(["pos", "stock", "bills"]);
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();
  useEffect(() => { api<{ on: string[]; all: { key: string }[] }>("/tenant/modules").then((r) => { setOn(r.on); setOffered(r.all.map((x) => x.key)); }); }, []);
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
        <ServicePicker value={on} onChange={change} disabled={busy} only={offered} />
        <p className="muted small">After your trial, the services in your plan can be on. Change the plan in <a href="/billing">Billing</a>.</p>
        <ErrorText error={error} />
        {saved && <p className="ok">Saved.</p>}
      </Card>
    </div>
  );
}
