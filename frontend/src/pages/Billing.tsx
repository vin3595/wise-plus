import { useEffect, useState } from "react";
import { api, download } from "../api";
import { formatPaise } from "../money";
import { useSession } from "../session";
import { SUPPORT_LABEL, supportWa } from "../components/Brand";
import { Button, Card, ErrorText, Field, Input, Select, useAction } from "../ui";

export interface BillingInfo {
  state: "house" | "trial" | "paid" | "locked" | "free";
  until: string | null;
  trial_ends_at: string | null;
  house: boolean;
  plan: { modules: string[]; period: "monthly" | "yearly" | "founding"; amount_paise: number; status: string; paid_until: string | null; outlets: number } | null;
  outlets: number;
  prices: { services: { key: string; name: string; monthly_paise: number }[]; bundle_monthly_paise: number };
  founding: { price_paise: number; left: number; slots: number; member: boolean };
  scans: { used: number; limit: number | null; bonus: number; per_month: number };
  packs: { key: string; scans: number; paise: number }[];
  messages: { used: number; allowance: number; bonus: number; left: number | null; per_outlet: number; unlimited: boolean };
  platform_admin?: boolean;
  msg_packs: { key: string; messages: number; paise: number }[];
  ready: boolean;
  key_id: string | null;
}
interface Quote { modules: string[]; period: string; amount_paise: number; monthly_paise: number; saving_paise: number; bundle: boolean; months_paid: number;
  outlets: number; per_outlet_paise: number }

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
    const o = await api<{ order_id: string; key_id: string; amount_paise: number; scans: number | null; messages: number | null; name: string; email: string; phone: string }>(`/billing/packs/${key}/order`, { method: "POST" });
    const what = o.messages ? `${o.messages} WhatsApp bill messages` : `${o.scans} bill scans`;
    const r = await pay({ key: o.key_id, order_id: o.order_id, amount: o.amount_paise, currency: "INR", name: "Ease My Bill", description: what,
      prefill: { name: o.name, email: o.email, contact: o.phone } });
    setB(await api<BillingInfo>("/billing/packs/verify", { body: r }));
    setMsg(`${what} added.`);
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
        {b.plan && <p className="small">Plan: {b.plan.period === "founding" ? "Founding member (everything)" : `${b.plan.modules.map(name).join(" + ")}, ${b.plan.period}`} · {formatPaise(b.plan.amount_paise)}{b.plan.outlets > 1 ? ` · ${b.plan.outlets} outlets` : ""}{b.plan.status === "cancelled" ? " · won't renew" : ""}</p>}
        {b.plan && b.outlets > b.plan.outlets && <p className="small error-text">You now have {b.outlets} outlets; your plan pays for {b.plan.outlets}. Choose the plan again below to cover them all.</p>}
        {msg && <p className="ok">{msg}</p>}
        <ErrorText error={error} />
        {!b.ready && !b.house && <p className="muted small">Online payment isn't set up yet.</p>}
      </Card>

      {!b.house && b.ready && <>
        {b.founding.left > 0 && !b.founding.member && (
          <Card title="Founding member offer">
            <div className="founding">
              <div><strong className="big">{formatPaise(b.founding.price_paise * b.outlets)}</strong> <span className="muted">for the first year{b.outlets > 1 ? ` (${formatPaise(b.founding.price_paise)} × ${b.outlets} outlets)` : ""}</span></div>
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
            {q.outlets > 1 && <p className="small muted">{formatPaise(q.per_outlet_paise)} per outlet × {q.outlets} outlets</p>}
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
      <Card title="Bills on WhatsApp">
        <p>{b.messages.unlimited ? `${b.messages.used} sent this month (no limit on this account).`
          : `${b.messages.used} of ${b.messages.allowance} free messages used this month`}{!b.messages.unlimited && b.messages.bonus ? ` · ${b.messages.bonus} bought messages left` : ""}</p>
        {!b.messages.unlimited && b.messages.left !== null && b.messages.left <= b.messages.allowance / 5 && (
          <p className="warn-text">{b.messages.left === 0 ? "Bills have stopped going on WhatsApp. Customers still get the printed bill." : `Only ${b.messages.left} left.`}</p>)}
        {!b.house && b.ready && <div className="row">
          {b.msg_packs.map((p) => <Button key={p.key} variant="ghost" disabled={busy} onClick={() => buyPack(p.key)}>{p.messages.toLocaleString("en-IN")} messages · {formatPaise(p.paise)}</Button>)}
        </div>}
        <p className="muted small">{b.messages.per_outlet} free each month for every outlet. Bought messages never expire and are used after the free ones. Messages to you (reports, dues, bank) are always free.</p>
      </Card>
      <ReferCard />
      <WhatsAppNumber />
      <YourData />
    </div>
  );
}

/** The owner can take all of the business's data any time: Excel to read, a restore file, and the bill photos. */
function YourData() {
  const { me } = useSession();
  const { busy, error, run } = useAction();
  if (me?.user.role !== "owner") return null;
  return (
    <Card title="Your data">
      <p className="muted">Everything this business has in Ease My Bill: bills, menu, customers, stock, purchases, suppliers, dues, bank lines,
        credit notes, and the bill photos and statements. Excel to read, plus a full restore file.</p>
      <Button variant="ghost" disabled={busy} onClick={() => run(() => download("/tenant/backup", "backup.zip"))}>{busy ? "Packing…" : "Download all my data"}</Button>
      <p className="muted small">A copy is also kept safely off the server every month. On WhatsApp, send <strong>backup</strong>.</p>
      <ErrorText error={error} />
    </Card>
  );
}


interface PRow { tenant_id: string; name: string; login_id: string; state: string; outlets: number; bill_msgs_used: number; bill_msgs_free: number | null;
  credits_left: number | null; left_this_month: number | null; sent: Record<string, number>; failed: number; meta_cost_paise: number;
  scans_used: number; scans_free: number | null; scans_bought_left: number; packs_bought: number; own_number: boolean;
  delivery: { total: number; delivered_pct: number | null; read_pct: number | null; failed_pct: number | null }; fail_reasons: { why: string; count: number }[] }
interface PUsage { month: string; restaurants: PRow[]; total: { sent: number; meta_cost_paise: number; credits_left: number; restaurants: number; running_out: number;
  delivery: PRow["delivery"] };
  health: { ok: boolean; configured: boolean; number: { number?: string; quality?: string; tier?: string; status?: string; name_status?: string; at?: string } | null;
    day: { sent: number; failed: number }; problem: { at: string; message: string } | null } | null }

/** Only you (the owner of your own businesses): every restaurant's WhatsApp messages, credits and scans. */
export function AllRestaurants() {
  const now = new Date();
  const ym = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  const [month, setMonth] = useState(ym(now));
  const [u, setU] = useState<PUsage | null>(null);
  const [sort, setSort] = useState<"sent" | "left" | "name">("sent");
  useEffect(() => { api<PUsage>(`/platform/usage?month=${month}`).then(setU).catch(() => setU(null)); }, [month]);
  const months = [0, 1, 2, 3, 4, 5].map((i) => ym(new Date(now.getFullYear(), now.getMonth() - i, 1)));
  const total = (r: PRow) => Object.values(r.sent).reduce((a, b) => a + b, 0);
  const low = (r: PRow) => r.left_this_month !== null && r.bill_msgs_free !== null && r.left_this_month <= Math.max(1, Math.floor(r.bill_msgs_free / 5));
  const rows = [...(u?.restaurants || [])].sort((a, b) => sort === "name" ? a.name.localeCompare(b.name)
    : sort === "left" ? (a.left_this_month ?? 1e9) - (b.left_this_month ?? 1e9) : total(b) - total(a));
  return (
    <Card title="All restaurants · WhatsApp and scans" actions={<span className="row"><Select value={month} onChange={(e) => setMonth(e.target.value)} aria-label="Month">
      {months.map((m) => <option key={m} value={m}>{new Date(m + "-01T00:00:00").toLocaleDateString("en-IN", { month: "long", year: "numeric" })}</option>)}</Select></span>}>
      {!u ? <p className="muted">Loading…</p> : <>
        <p><strong>{u.total.sent.toLocaleString("en-IN")}</strong> messages sent · Meta cost <strong>{formatPaise(u.total.meta_cost_paise)}</strong> ·
          {" "}<strong>{u.total.credits_left.toLocaleString("en-IN")}</strong> bought credits unused · {u.total.restaurants} restaurants
          {u.total.running_out ? <strong className="error-text"> · {u.total.running_out} running out</strong> : null}</p>
        {u.total.delivery.total > 0 && <p className="small">Reached the phone <strong>{u.total.delivery.delivered_pct}%</strong> · read {u.total.delivery.read_pct}% · failed {u.total.delivery.failed_pct}%</p>}
        {u.health && <p className={`small ${u.health.ok ? "muted" : "error-text"}`}>{u.health.ok ? "✅ WhatsApp is sending" : "⚠️ WhatsApp needs attention"}
          {u.health.number?.number ? ` · ${u.health.number.number}: quality ${u.health.number.quality || "?"}, daily limit ${u.health.number.tier || "?"}${u.health.number.status ? `, ${u.health.number.status.toLowerCase()}` : ""}` : ""}
          {u.health.problem ? <><br />Last problem: {u.health.problem.message}</> : null}</p>}
        <div className="row"><span className="muted small">Sort:</span>
          {(["sent", "left", "name"] as const).map((k) => <button key={k} className={`chip${sort === k ? " on" : ""}`} onClick={() => setSort(k)}>
            {k === "sent" ? "Most sent" : k === "left" ? "Least left" : "Name"}</button>)}</div>
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Restaurant</th><th>Plan</th><th className="num">Free bill msgs used</th><th className="num">Bought credits left</th>
            <th className="num">Sent (bills · broadcasts)</th><th className="num">Reached</th><th className="num">Meta cost</th><th className="num">Scans</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.tenant_id} className={low(r) ? "warn-row" : ""}>
              <td><strong>{r.name}</strong><div className="small muted">{r.login_id} · {r.outlets} outlet{r.outlets === 1 ? "" : "s"}{r.own_number ? " · own number" : ""}</div>
                {!r.own_number && <ManualApi login={r.login_id} onDone={() => setMonth((m) => m)} />}</td>
              <td>{r.state}</td>
              <td className="num">{r.bill_msgs_free === null ? <span className="muted">unlimited</span> : <>{r.bill_msgs_used} / {r.bill_msgs_free}{low(r) && " ⚠️"}</>}</td>
              <td className="num">{r.credits_left === null ? "—" : r.credits_left.toLocaleString("en-IN")}{r.packs_bought ? <div className="small muted">{r.packs_bought} pack{r.packs_bought === 1 ? "" : "s"} bought</div> : null}</td>
              <td className="num">{(r.sent.bill || 0) + (r.sent.otp || 0)} · {r.sent.promo || 0}{r.failed ? <div className="small error-text">{r.failed} failed</div> : null}</td>
              <td className="num">{r.delivery.total ? `${r.delivery.delivered_pct}%` : "—"}{r.fail_reasons[0] ? <div className="small error-text" title={r.fail_reasons.map((f) => `${f.why} (${f.count})`).join("\n")}>{r.fail_reasons[0].why} ({r.fail_reasons[0].count})</div> : null}</td>
              <td className="num">{formatPaise(r.meta_cost_paise)}</td>
              <td className="num">{r.scans_used}{r.scans_free !== null ? ` / ${r.scans_free}` : ""}{r.scans_bought_left ? <div className="small muted">+{r.scans_bought_left} bought</div> : null}</td>
            </tr>))}</tbody>
        </table></div>
        <p className="muted small">Only you see this (owner of your own businesses). Meta cost is at India's rate card: ₹0.115 a bill or OTP message, ₹0.86 a broadcast.
          Offer replies and messages to owners aren't counted. On WhatsApp, send <strong>credits</strong> for the same list.</p>
      </>}
    </Card>
  );
}


interface Conn { mode: "shared" | "own"; display_number: string | null; can_connect: boolean; how: string | null; webhook_url: string; app_id: string | null; config_id: string | null; graph_version: string }
declare global { interface Window { FB?: { init: (o: object) => void; login: (cb: (r: { authResponse?: { code?: string } }) => void, o: object) => void }; fbAsyncInit?: () => void } }

/** Which number customers get bills and offers from: the shared one, or the restaurant's own (Meta's sign-up popup). */
function WhatsAppNumber() {
  const { me } = useSession();
  const [c, setC] = useState<Conn | null>(null);
  const act = useAction();
  const load = () => api<Conn>("/whatsapp/connection").then(setC).catch(() => setC(null));
  useEffect(() => { load(); }, []);
  if (!c) return null;
  const owner = me?.user.role === "owner";
  const connect = () => act.run(async () => {
    if (!c.app_id || !c.config_id) return;
    if (!window.FB) {
      await new Promise<void>((ok, bad) => {
        window.fbAsyncInit = () => ok();
        const sc = document.createElement("script");
        sc.src = "https://connect.facebook.net/en_US/sdk.js"; sc.async = true; sc.onerror = () => bad(new Error("Couldn't open Meta's sign-up"));
        document.body.appendChild(sc);
      });
    }
    window.FB!.init({ appId: c.app_id, autoLogAppEvents: true, xfbml: false, version: c.graph_version });
    let ids: { phone_number_id?: string; waba_id?: string } = {};
    const onMsg = (e: MessageEvent) => {
      if (!String(e.origin).endsWith("facebook.com")) return;
      try { const d = typeof e.data === "string" ? JSON.parse(e.data) : e.data; if (d?.type === "WA_EMBEDDED_SIGNUP" && d.data) ids = d.data; } catch { /* other messages */ }
    };
    window.addEventListener("message", onMsg);
    const code = await new Promise<string | null>((ok) => window.FB!.login((r) => ok(r.authResponse?.code || null),
      { config_id: c.config_id, response_type: "code", override_default_response_type: true, extras: { setup: {}, sessionInfoVersion: 3 } }));
    window.removeEventListener("message", onMsg);
    if (!code || !ids.phone_number_id || !ids.waba_id) throw new Error("WhatsApp sign-up wasn't finished");
    await api("/whatsapp/connect", { body: { code, phone_number_id: ids.phone_number_id, waba_id: ids.waba_id } });
    await load();
  });
  return (
    <Card title="Your WhatsApp number">
      {c.mode === "own" ? <p>Bills and offers go from <strong>your own number</strong>{c.display_number ? ` (${c.display_number})` : ""}, with your name on the chat.</p>
        : <p>Bills and offers go from the shared Ease My Bill number{c.display_number ? ` (+${c.display_number})` : ""}, with your restaurant's name in every message. Nothing to set up.</p>}
      {owner && c.can_connect && c.mode === "shared" && <Button onClick={connect} disabled={act.busy}>{act.busy ? "Opening WhatsApp sign-up…" : "Use my own WhatsApp number"}</Button>}
      {owner && c.mode === "own" && <button className="link danger small" onClick={() => { if (confirm("Go back to the shared number?")) act.run(async () => { await api("/whatsapp/disconnect", { method: "POST" }); await load(); }); }}>Go back to the shared number</button>}
      {c.mode === "shared" && <p className="muted small">Bills, sign-in codes and offer QRs are free of setup on the shared number. <strong>Broadcasts</strong> to your
        customers go from your own WhatsApp number (your name on the chat, Meta bills you directly). Have one already, or want us to set it up? Below.</p>}
      {owner && c.mode === "shared" && <ManualApi onDone={load} />}
      {c.mode === "own" && c.how === "manual" && <p className="muted small">In your Meta app › WhatsApp › Configuration, set the webhook to <code>{c.webhook_url}</code> and
        subscribe to <em>messages</em>, so delivery ticks and 'Stop offers' taps reach us.</p>}
      <ErrorText error={act.error} />
    </Card>
  );
}


function ManualApi({ onDone, login }: { onDone: () => void; login?: string }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ phone_number_id: "", token: "", waba_id: "", app_secret: "" });
  const [done, setDone] = useState<{ display_number: string; templates: string[]; webhook_url: string; verify_token: string | null } | null>(null);
  const act = useAction();
  if (!open) return <p className="small"><button className="link" onClick={() => setOpen(true)}>{login ? "Set their WhatsApp API" : "I have my own WhatsApp API"}</button>
    {!login && <> · or <a href={supportWa("Hi, please set up our own WhatsApp API on Ease My Bill")} target="_blank" rel="noreferrer">message us on {SUPPORT_LABEL}</a> and we'll set it up for you</>}</p>;
  return (
    <div className="sub-form">
      <p className="muted small">From Meta's WhatsApp Manager / your Meta app: the phone number ID, a permanent (system user) token, the WhatsApp Business Account ID,
        and the app secret (for delivery ticks).</p>
      <div className="row wrap">
        <Field label="Phone number ID"><Input value={f.phone_number_id} onChange={(e) => setF({ ...f, phone_number_id: e.target.value.trim() })} inputMode="numeric" /></Field>
        <Field label="WhatsApp Business Account ID"><Input value={f.waba_id} onChange={(e) => setF({ ...f, waba_id: e.target.value.trim() })} inputMode="numeric" /></Field>
        <Field label="Permanent token"><Input type="password" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value.trim() })} autoComplete="off" /></Field>
        <Field label="App secret"><Input type="password" value={f.app_secret} onChange={(e) => setF({ ...f, app_secret: e.target.value.trim() })} autoComplete="off" /></Field>
      </div>
      <ErrorText error={act.error} />
      {done ? <p className="ok-text">Connected {done.display_number}. {done.templates.length ? `Templates sent to Meta for approval: ${done.templates.join(", ")}.` : ""}
        <br /><span className="muted small">Now in the Meta app › WhatsApp › Configuration: webhook <code>{done.webhook_url}</code>{done.verify_token ? <>, verify token <code>{done.verify_token}</code></> : null}, and subscribe to <em>messages</em>.</span></p>
        : <div className="row"><Button disabled={act.busy || !f.phone_number_id || f.token.length < 20} onClick={() => act.run(async () => {
          const body = { phone_number_id: f.phone_number_id, token: f.token, waba_id: f.waba_id || null, app_secret: f.app_secret || null };
          setDone(await api(login ? `/platform/whatsapp/${login}` : "/whatsapp/connect-manual", { body }));
          onDone();
        })}>Check and connect</Button><Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button></div>}
    </div>
  );
}


/** Only you: the Razorpay account Ease My Bill's own ₹999 plans are charged to. */
export function YourRazorpay() {
  const [k, setK] = useState<{ key_id: string | null; has_secret: boolean; has_webhook_secret: boolean; webhook_url: string } | null>(null);
  const [f, setF] = useState({ key_id: "", key_secret: "" });
  const [done, setDone] = useState<{ key_id: string; webhook_url: string; webhook_secret: string | null } | null>(null);
  const [open, setOpen] = useState(false);
  const act = useAction();
  const load = () => api<NonNullable<typeof k>>("/platform/razorpay").then(setK).catch(() => setK(null));
  useEffect(() => { load(); }, []);
  if (!k) return null;
  const ready = !!(k.key_id && k.has_secret);
  return (
    <Card title="Your Razorpay (Ease My Bill's own billing)">
      <p className="small">{ready ? <>Connected: <code>{k.key_id}</code>. Restaurants can pay for their plan.</> : "Not connected yet: restaurants can't pay for a plan until you add your keys."}
        {ready && !k.has_webhook_secret ? " Webhook secret missing." : ""}</p>
      {done ? <div className="ok-text small">
          <p>Saved and checked with Razorpay ✓</p>
          {done.webhook_secret && <p>Now in Razorpay › Account &amp; Settings › Webhooks › Add new webhook:<br />
            URL <code>{done.webhook_url}</code><br />Secret <code>{done.webhook_secret}</code> (shown once)<br />
            Events: all <em>subscription.*</em> events, <em>payment.captured</em> and <em>order.paid</em>.</p>}
        </div>
        : !open ? <p className="small"><button className="link" onClick={() => setOpen(true)}>{ready ? "Change keys" : "Add your keys"}</button></p>
        : <div className="sub-form">
            <p className="muted small">Razorpay › Account &amp; Settings › Websites &amp; API keys. Use the live Key Id and its Key Secret.</p>
            <div className="row wrap">
              <Field label="Key Id"><Input value={f.key_id} placeholder="rzp_live_…" onChange={(e) => setF({ ...f, key_id: e.target.value.trim() })} autoComplete="off" /></Field>
              <Field label="Key Secret"><Input type="password" value={f.key_secret} onChange={(e) => setF({ ...f, key_secret: e.target.value.trim() })} autoComplete="off" /></Field>
            </div>
            <ErrorText error={act.error} />
            <div className="row"><Button disabled={act.busy || !f.key_id.startsWith("rzp_") || f.key_secret.length < 10} onClick={() => act.run(async () => {
              setDone(await api("/platform/razorpay", { body: f })); setF({ key_id: "", key_secret: "" }); load();
            })}>{act.busy ? "Checking…" : "Check and save"}</Button><Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button></div>
          </div>}
    </Card>
  );
}

/** A restaurant's own link: each restaurant that joins with it and pays gives both a month free. */
function ReferCard() {
  const [r, setR] = useState<{ code: string; link: string; signed: number; rewarded: number; share: string } | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => { api<NonNullable<typeof r>>("/growth/referral").then(setR).catch(() => setR(null)); }, []);
  if (!r) return null;
  return (
    <Card title="🎁 Refer a restaurant, get a month free">
      <p>Share your link. When a restaurant joins with it and starts paying, <strong>you both get a month free</strong>. No limit.</p>
      <p className="mono small">{r.link}</p>
      <div className="row wrap">
        <a className="btn" href={`https://wa.me/?text=${encodeURIComponent(r.share)}`} target="_blank" rel="noreferrer">Share on WhatsApp</a>
        <Button variant="ghost" onClick={() => { navigator.clipboard?.writeText(r.link).then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); }); }}>{copied ? "Copied ✓" : "Copy link"}</Button>
      </div>
      {(r.signed > 0) && <p className="muted small">{r.signed} joined with your link · {r.rewarded} free month{r.rewarded === 1 ? "" : "s"} earned</p>}
    </Card>
  );
}
