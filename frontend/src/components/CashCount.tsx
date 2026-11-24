import { useState } from "react";
import { formatPaise } from "../money";

export type Denoms = Record<string, number>;
const COINS = [20, 10, 5, 2, 1];
const NOTES = [500, 200, 100, 50, 20, 10];
/** "coins" holds all coins as one rupee total (when nobody sorts them by face value). */
export const COIN_TOTAL = "coins";
const faceValue = (k: string) => (k === COIN_TOTAL ? 1 : Number(k.slice(1)));
export const denomTotal = (d: Denoms) => Object.entries(d).reduce((s, [k, n]) => s + faceValue(k) * (n || 0) * 100, 0);
/** "₹500 × 2, ₹10 coin × 3, Coins ₹137" — big notes first, the coin total last. */
export const denomText = (d: Denoms | null | undefined) => (d ? Object.entries(d).filter(([, n]) => n)
  .sort((a, b) => (a[0] === COIN_TOTAL ? 1 : 0) - (b[0] === COIN_TOTAL ? 1 : 0) || faceValue(b[0]) - faceValue(a[0]) || a[0].localeCompare(b[0]))
  .map(([k, n]) => (k === COIN_TOTAL ? `Coins ${formatPaise(n * 100)}` : `₹${k.slice(1)}${k[0] === "c" ? " coin" : ""} × ${n}`)).join(", ") : "");

/** Count the drawer note by note and coin by coin; the total adds itself up.
 *  Big notes first (that's how a drawer is counted); coins fold away until needed. */
export default function CashCount({ value, onChange }: { value: Denoms; onChange: (d: Denoms) => void }) {
  const set = (k: string, n: number) => onChange({ ...value, [k]: Math.max(0, Math.min(n, 100000)) });
  const lump = value[COIN_TOTAL] || 0;
  const coinTotal = COINS.reduce((s, v) => s + v * (value[`c${v}`] || 0) * 100, 0) + lump * 100;
  const [coinsOpen, setCoinsOpen] = useState(coinTotal > 0);
  const [asTotal, setAsTotal] = useState(lump > 0);
  // Switching how coins are counted clears the other way, so nothing is counted twice.
  const switchTo = (total: boolean) => {
    if (total === asTotal) return;
    const next = { ...value };
    if (total) COINS.forEach((v) => delete next[`c${v}`]); else delete next[COIN_TOTAL];
    setAsTotal(total);
    onChange(next);
  };
  const count = (p: string) => Object.entries(value).filter(([k]) => k[0] === p && k !== COIN_TOTAL).reduce((s, [, n]) => s + (n || 0), 0);
  const notes = count("n"), coins = count("c");
  const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const pieces = [notes && plural(notes, "note"), coins && plural(coins, "coin"), lump && "coins as a total"].filter(Boolean).join(", ");

  const row = (v: number, coin: boolean) => {
    const k = `${coin ? "c" : "n"}${v}`, n = value[k] || 0, what = coin ? "coin" : "note";
    return (
      <li key={k} className={`cc-row ${n ? "has" : ""}`}>
        <span className={`cc-face ${coin ? "coin" : `note n${v}`}`}>₹{v}</span>
        <span className="cc-step">
          <button type="button" className="cc-btn" disabled={!n} onClick={() => set(k, n - 1)} aria-label={`One less ₹${v} ${what}`}>−</button>
          <input className="cc-n" inputMode="numeric" aria-label={`${coin ? "Coins" : "Notes"} of ₹${v}`}
            value={n || ""} placeholder="0" onFocus={(e) => e.target.select()}
            onChange={(e) => { const x = parseInt(e.target.value.replace(/\D/g, ""), 10); set(k, Number.isFinite(x) ? x : 0); }} />
          <button type="button" className="cc-btn plus" onClick={() => set(k, n + 1)} aria-label={`One more ₹${v} ${what}`}>+</button>
        </span>
        <span className="cc-sum">{n ? formatPaise(v * n * 100) : ""}</span>
      </li>
    );
  };
  return (
    <div className="cc">
      <ul className="cc-list" aria-label="Notes">{NOTES.map((v) => row(v, false))}</ul>
      <div className="cc-coins">
        <span>Coins</span>
        {coinTotal > 0 && <span className="muted">{formatPaise(coinTotal)}</span>}
        <div className="cc-mode" role="radiogroup" aria-label="How to count coins">
          <button type="button" role="radio" aria-checked={coinsOpen && !asTotal} className={coinsOpen && !asTotal ? "on" : ""}
            onClick={() => { switchTo(false); setCoinsOpen(true); }}>Each coin</button>
          <button type="button" role="radio" aria-checked={coinsOpen && asTotal} className={coinsOpen && asTotal ? "on" : ""}
            onClick={() => { switchTo(true); setCoinsOpen(true); }}>Total</button>
        </div>
        {coinsOpen && <button type="button" className="cc-fold" aria-label="Hide coins" onClick={() => setCoinsOpen(false)}>▴</button>}
      </div>
      {coinsOpen && (
        <div className="cc-coin-body">
          {asTotal ? (
            <label className="cc-lump">
              <span>All coins together</span>
              <span className="cc-lump-in">₹<input className="cc-n" inputMode="numeric" aria-label="Total of all coins in rupees" autoFocus
                value={lump || ""} placeholder="0" onFocus={(e) => e.target.select()}
                onChange={(e) => { const x = parseInt(e.target.value.replace(/\D/g, ""), 10); set(COIN_TOTAL, Number.isFinite(x) ? x : 0); }} /></span>
            </label>
          ) : <ul className="cc-list" aria-label="Coins">{COINS.map((v) => row(v, true))}</ul>}
        </div>
      )}
      <div className="cc-total" aria-live="polite">
        <span>Total{pieces ? ` · ${pieces}` : ""}</span>
        <strong>{formatPaise(denomTotal(value))}</strong>
      </div>
    </div>
  );
}
