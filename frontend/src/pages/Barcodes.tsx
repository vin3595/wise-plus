import { useCallback, useEffect, useMemo, useState } from "react";
import { Printer, ScanLine, Wand2 } from "lucide-react";
import { api } from "../api";
import { formatPaise } from "../money";
import { useSession } from "../session";
import { Button, Card, ErrorText, Input, Select, useAction } from "../ui";
import { CameraScan, Ean13, useHandScanner } from "../barcode";

interface Row { variant_id?: string; stock_item_id?: string; name: string; size?: string | null; unit?: string; price_paise?: number; barcode: string | null }

const SIZES = {
  a4: { label: "A4 sheet, 24 labels (3 × 8, 64 × 34 mm)", cls: "lab-a4" },
  roll: { label: "Label printer roll, 50 × 25 mm", cls: "lab-roll" },
  roll38: { label: "Label printer roll, 38 × 25 mm", cls: "lab-roll38" },
};

/** Menu › Barcodes: our own barcodes, linking a maker's barcode, and printing labels. */
export default function Barcodes() {
  const { me } = useSession();
  const [data, setData] = useState<{ items: Row[]; stock: Row[] } | null>(null);
  const [tab, setTab] = useState<"items" | "stock">("items");
  const [q, setQ] = useState("");
  const [pick, setPick] = useState<Record<string, number>>({});
  const [size, setSize] = useState<keyof typeof SIZES>("a4");
  const [showPrice, setShowPrice] = useState(true);
  const [linking, setLinking] = useState<Row | null>(null);
  const [camera, setCamera] = useState(false);
  const { run, busy, error, setError } = useAction();
  const outletId = me?.outlet_id || "";
  const load = useCallback(() => api<{ items: Row[]; stock: Row[] }>(`/barcodes${outletId ? `?outlet_id=${outletId}` : ""}`).then(setData), [outletId]);
  useEffect(() => { load(); }, [load]);

  const id = (r: Row) => (r.variant_id || r.stock_item_id)!;
  const rows = useMemo(() => (data ? data[tab] : []).filter((r) => !q || `${r.name} ${r.size || ""} ${r.barcode || ""}`.toLowerCase().includes(q.toLowerCase())), [data, tab, q]);
  const missing = rows.filter((r) => !r.barcode);

  async function link(r: Row, code: string | null) {
    const ok = await run(() => api(r.variant_id ? `/barcodes/variants/${r.variant_id}` : `/barcodes/stock/${r.stock_item_id}`, { method: "PUT", body: { barcode: code } }));
    if (ok) { setLinking(null); load(); }
  }
  // While "Link a barcode" is open, a hand scanner fills it in.
  useHandScanner((c) => { if (linking) void link(linking, c); }, !!linking);

  const make = () => run(async () => {
    const body = tab === "items" ? { variant_ids: missing.map((r) => r.variant_id) } : { stock_item_ids: missing.map((r) => r.stock_item_id) };
    await api("/barcodes/generate", { body });
    await load();
  });

  const labels = (data ? data[tab] : []).flatMap((r) => (r.barcode ? Array.from({ length: pick[id(r)] || 0 }, () => r) : []));

  return (
    <div className="stack">
      <h1>Barcodes</h1>
      <p className="muted">Scan to bill: a USB or Bluetooth scanner works straight away at the counter, or tap the scan button there to use the camera.
        Customers' WhatsApp offer QR codes are scanned the same way.</p>
      <div className="seg" role="tablist">
        <button role="tab" aria-selected={tab === "items"} className={tab === "items" ? "active" : ""} onClick={() => setTab("items")}>Menu items</button>
        <button role="tab" aria-selected={tab === "stock"} className={tab === "stock" ? "active" : ""} onClick={() => setTab("stock")}>Stock items</button>
      </div>
      <Card title={`${rows.length} ${tab === "items" ? "items" : "stock items"}`} actions={
        missing.length > 0 && <Button variant="ghost" disabled={busy} onClick={make}><Wand2 size={16} aria-hidden /> Make barcodes for {missing.length} without one</Button>}>
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find an item or barcode" aria-label="Find" />
        <table className="table">
          <thead><tr><th>Item</th>{tab === "items" && <th className="num">Price</th>}<th>Barcode</th><th className="num">Labels</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={id(r)}>
                <td>{r.name}{r.size ? <span className="muted"> {r.size}</span> : null}{r.unit ? <span className="muted small"> ({r.unit})</span> : null}</td>
                {tab === "items" && <td className="num">{formatPaise(r.price_paise || 0)}</td>}
                <td>
                  {r.barcode ? <span className="mono">{r.barcode}</span> : <span className="muted">—</span>}{" "}
                  <button className="link small" onClick={() => setLinking(r)}>{r.barcode ? "Change" : "Link"}</button>
                </td>
                <td className="num">
                  <Input type="number" min={0} max={500} style={{ width: "4.5rem" }} disabled={!r.barcode} value={pick[id(r)] || ""}
                    aria-label={`How many labels for ${r.name}`} onChange={(e) => setPick((p) => ({ ...p, [id(r)]: Math.max(0, Math.min(500, Number(e.target.value) || 0)) }))} />
                </td>
              </tr>))}
          </tbody>
        </table>
        <ErrorText error={error} />
      </Card>

      {linking && (
        <Card title={`Barcode for ${linking.name}${linking.size ? ` ${linking.size}` : ""}`}>
          <p className="muted">Scan the barcode printed on the product with your scanner, use the camera, or type it.</p>
          <LinkForm initial={linking.barcode || ""} busy={busy} onSave={(c) => link(linking, c || null)} onCamera={() => setCamera(true)}
            onCancel={() => { setLinking(null); setError(null); }} />
        </Card>)}
      {camera && <CameraScan title="Scan the product's barcode" onClose={() => setCamera(false)} onScan={(c) => { setCamera(false); if (linking) void link(linking, c); }} />}

      <Card title="Print labels">
        <div className="row wrap">
          <Select value={size} onChange={(e) => setSize(e.target.value as keyof typeof SIZES)} aria-label="Label size">
            {Object.entries(SIZES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
          </Select>
          {tab === "items" && <label className="check"><input type="checkbox" checked={showPrice} onChange={(e) => setShowPrice(e.target.checked)} /> Show price</label>}
          <Button disabled={!labels.length} onClick={() => window.print()}><Printer size={16} aria-hidden /> Print {labels.length} label{labels.length === 1 ? "" : "s"}</Button>
        </div>
        <p className="muted small">Enter how many labels next to each item above. In the print window, choose your label printer (or A4 at 100% scale, no margins).</p>
      </Card>
      <div className={`label-sheet ${SIZES[size].cls}`} aria-hidden={!labels.length}>
        {labels.map((r, i) => (
          <div className="label" key={i}>
            <div className="label-name">{r.name}{r.size ? ` ${r.size}` : ""}</div>
            <Ean13 code={r.barcode!} height={34} />
            {showPrice && r.price_paise !== undefined && <div className="label-price">{formatPaise(r.price_paise)}</div>}
          </div>))}
      </div>
    </div>
  );
}

function LinkForm({ initial, busy, onSave, onCamera, onCancel }: { initial: string; busy: boolean; onSave: (c: string) => void; onCamera: () => void; onCancel: () => void }) {
  const [code, setCode] = useState(initial);
  return (
    <div className="row wrap">
      <Input autoFocus data-scan="self" value={code} onChange={(e) => setCode(e.target.value.trim())} placeholder="e.g. 8901058851427" aria-label="Barcode"
        onKeyDown={(e) => { if (e.key === "Enter") onSave(code); }} />
      <Button variant="ghost" onClick={onCamera}><ScanLine size={16} aria-hidden /> Camera</Button>
      <Button disabled={busy} onClick={() => onSave(code)}>Save</Button>
      {initial && <Button variant="ghost" disabled={busy} onClick={() => onSave("")}>Remove</Button>}
      <Button variant="ghost" onClick={onCancel}>Cancel</Button>
    </div>
  );
}
