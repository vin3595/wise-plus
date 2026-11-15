import { useEffect, useState } from "react";
import { api } from "../api";
import { formatPaise } from "../money";
import { useSession } from "../session";
import { Button, Card, ErrorText, useAction } from "../ui";

export interface BillingInfo {
  state: "house" | "trial" | "paid" | "locked" | "free";
  until: string | null;
  trial_ends_at: string | null;
  house: boolean;
  plan: { modules: string[]; period: "monthly" | "yearly" | "founding"; amount_paise: number; status: string; paid_until: string | null } | null;
  prices: { services: { key: string; name: string; monthly_paise: number }[]; bundle_monthly_paise: number };
  founding: { price_paise: number; left: number; slots: number; member: boolean };
  scans: { used: number; limit: number | null; bonus: number; per_month: number };
  packs: { key: string; scans: number; paise: number }[];
  ready: boolean;
  key_id: string | null;
}
interface Quote { modules: string[]; period: string; amount_paise: number; monthly_paise: number; saving_paise: number; bundle: boolean; months_paid: number }

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "");
export const daysLeft = (iso: string | null) => (iso ? Math.ceil((new Date(iso).getTime() - Date.now()) / 864e5) : 0);

type RzResponse = Record<string, string>;
declare global { interface Window { Razorpay?: new (o: Record<string, unknown>) => { open: () => void; on: (e: string, f: (r: { error?: { description?: string } }) => void) => void } } }

function loadCheckout(): Promise<void> {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = "https://checkout.razorpay.com/v1/checkout.js";
    s.onload = () => ok();
    s.onerror = () => fail(new Error("Couldn't open Razorpay. Check the internet connection and try again."));
    document.body.appendChild(s);
  });
}

/** Razorpay's own payment window; resolves with what Razorpay hands back after paying. */
async function pay(opts: Record<string, unknown>): Promise<RzResponse> {
  await loadCheckout();
  return new Promise((ok, fail) => {
    const rz = new window.Razorpay!({ ...opts, handler: (r: RzResponse) => ok(r), modal: { ondismiss: () => fail(new Error("Payment window closed")) },
      theme: { color: getComputedStyle(document.documentElement).getPropertyValue("--primary").trim() || "#138996" } });
    rz.on("payment.failed", (r) => fail(new Error(r.error?.description || "Payment failed")));
    rz.open();
  });
}

/** Settings > Billing: plan, founding offer, trial, bill-scan allowance and packs. */
export default function Billing() {
  const { refresh } = useSession();
  const [b, setB] = useState<BillingInfo | null>(null);
  const [pick, setPick] = useState<string[]>(["pos", "stock", "bills"]);
  const [period, setPeriod] = useState<"monthly" | "yearly">("yearly");
  const [q, setQ] = useState<Quote | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const { busy, error, run } = useAction();
  const load = () => api<BillingInfo>("/billing").then((x) => { setB(x); if (x.plan?.modules) setPick(x.plan.modules); });
  useEffect(() => { load(); }, []);
  useEffect(() => { if (pick.length) api<Quote>("/billing/quote", { body: { modules: pick, period } }).then(setQ).catch(() => setQ(null)); else setQ(null); }, [pick, period]);
  if (!b) return <p className="muted">Loading…</p>;

  const checkout = (plan: { modules: string[]; period: string }) => run(async () => {
    setMsg(null);
    const s = await api<{ subscription_id: string; key_id: string; quote: Quote; starts: string | null; name: string; email: string; phone: string }>(
      "/billing/subscribe", { body: plan });
    const r = await pay({ key: s.key_id, subscription_id: s.subscription_id, name: "wise-plus",
      description: plan.period === "founding" ? "Founding member · first year" : `${plan.period === "yearly" ? "Yearly" : "Monthly"} plan`,
      prefill: { name: s.name, email: s.email, contact: s.phone } });
    setB(await api<BillingInfo>("/billing/verify", { body: r }));
    setMsg(s.starts ? `Done. Your first charge is on ${day(s.starts)}, when your free trial ends.` : "Done. Your plan is on.");
    await refresh();
  });
  const buyPack = (key: string) => run(async () => {
    setMsg(null);
    const o = await api<{ order_id: string; key_id: string; amount_paise: number; scans: number; name: string; email: string; phone: string }>(`/billing/packs/${key}/order`, { method: "POST" });
    const r = await pay({ key: o.key_id, order_id: o.order_id, amount: o.amount_paise, currency: "INR", name: "wise-plus", description: `${o.scans} bill scans`,
      prefill: { name: o.name, email: o.email, contact: o.phone } });
    setB(await api<BillingInfo>("/billing/packs/verify", { body: r }));
    setMsg(`${o.scans} scans added.`);
  });
  const cancel = () => run(async () => { setB(await api<BillingInfo>("/billing/cancel", { method: "POST" })); setMsg("Your plan won't renew. Everything works until the paid time ends."); });

  const name = (k: string) => b.prices.services.find((s) => s.key === k)?.name || k;
  const left = daysLeft(b.until);
  const status = b.state === "house" ? "Your own business account: never billed."
    : b.state === "trial" ? `Free trial: ${left} day${left === 1 ? "" : "s"} left (till ${day(b.until)}).`
    : b.state === "paid" ? `Paid till ${day(b.until)}.`
    : b.state === "free" ? "Free for now." : "Your free trial has ended. The app is read-only until you choose a plan; nothing is lost.";
  return (
    <div className="stack">
      <h1>Billing</h1>
      <Card>
        <p className={b.state === "locked" ? "error" : ""}><strong>{status}</strong></p>
        {b.plan && <p className="small">Plan: {b.plan.period === "founding" ? "Founding member (everything)" : `${b.plan.modules.map(name).join(" + ")}, ${b.plan.period}`} · {formatPaise(b.plan.amount_paise)}{b.plan.status === "cancelled" ? " · won't renew" : ""}</p>}
        {msg && <p className="ok">{msg}</p>}
        <ErrorText error={error} />
        {!b.ready && !b.house && <p className="muted small">Online payment isn't set up yet.</p>}
      </Card>

      {!b.house && b.ready && <>
        {b.founding.left > 0 && !b.founding.member && (
          <Card title="Founding member offer">
            <div className="founding">
              <div><strong className="big">{formatPaise(b.founding.price_paise)}</strong> <span className="muted">for the first year</span></div>
              <p>Everything: POS billing, Inventory and Bill scan. Regular price {formatPaise(b.prices.bundle_monthly_paise * 12)} a year.</p>
              <p className="small warn-text">Only {b.founding.left} of {b.founding.slots} places left.</p>
            </div>
            <Button disabled={busy} onClick={() => checkout({ modules: ["pos", "stock", "bills"], period: "founding" })}>Become a founding member</Button>
          </Card>
        )}

        <Card title={b.plan ? "Change plan" : "Choose a plan"}>
          <div className="seg" role="group" aria-label="Pay">
            {(["monthly", "yearly"] as const).map((p) => <button key={p} type="button" className={period === p ? "on" : ""} aria-pressed={period === p} onClick={() => setPeriod(p)}>{p === "monthly" ? "Monthly" : "Yearly · 2 months free"}</button>)}
          </div>
          <div className="plan-list">
            {b.prices.services.map((s) => {
              const on = pick.includes(s.key);
              return (
                <label key={s.key} className={`plan-row ${on ? "on" : ""}`}>
                  <input type="checkbox" checked={on} onChange={() => setPick(on ? pick.filter((k) => k !== s.key) : [...pick, s.key])} />
                  <span className="grow">{s.name}</span><span className="num">{formatPaise(s.monthly_paise)}/month</span>
                </label>);
            })}
          </div>
          {q && <div className="plan-total">
            {q.bundle && <p className="small ok">All three together: {formatPaise(b.prices.bundle_monthly_paise)}/month{period === "yearly" ? ", and a year costs 9 months" : ""}.</p>}
            <p><strong>{formatPaise(q.amount_paise)}</strong> {period === "yearly" ? `a year (pay ${q.months_paid} months, get 12 · you save ${formatPaise(q.saving_paise)})` : "a month"}</p>
            {b.state === "trial" && <p className="muted small">Nothing is charged until your trial ends on {day(b.trial_ends_at)}.</p>}
          </div>}
          <Button disabled={busy || !q} onClick={() => checkout({ modules: pick, period })}>{b.state === "trial" ? "Add payment method" : `Pay ${q ? formatPaise(q.amount_paise) : ""}`}</Button>
          {b.plan && b.plan.status !== "cancelled" && <Button variant="ghost" disabled={busy} onClick={cancel}>Stop renewing</Button>}
        </Card>
      </>}

      {(pick.includes("bills") || b.plan?.modules.includes("bills") || b.house) && (
        <Card title="Bill scans">
          <p>{b.scans.limit === null ? `${b.scans.used} scanned this month (no limit).` : `${b.scans.used} of ${b.scans.per_month} used this month`}{b.scans.bonus ? ` · ${b.scans.bonus} extra scans left` : ""}</p>
          {!b.house && b.ready && <div className="row">
            {b.packs.map((p) => <Button key={p.key} variant="ghost" disabled={busy} onClick={() => buyPack(p.key)}>{p.scans} scans · {formatPaise(p.paise)}</Button>)}
          </div>}
          <p className="muted small">Extra scans never expire and are used after the month's {b.scans.per_month}.</p>
        </Card>
      )}
    </div>
  );
}
