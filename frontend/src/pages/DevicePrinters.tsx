import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { desktop, printNow, type DesktopPrinter, type DesktopSettings } from "../desktop";
import { kotsHere, setKotsHere } from "../components/KotAutoPrinter";
import { Button, Card, ErrorText, Field, Select, Toggle } from "../ui";

const DOWNLOAD = "/download/EaseMyBill-Setup.exe";

/** Printers on this computer, for the Ease My Bill Windows app: bills and KOTs print with no dialog. */
export default function DevicePrinters() {
  const d = desktop();
  const [list, setList] = useState<DesktopPrinter[]>([]);
  const [s, setS] = useState<DesktopSettings>({});
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [kots, setKots] = useState(kotsHere());
  useEffect(() => {
    if (!d) return;
    d.printers().then(setList).catch(() => undefined);
    d.settings().then(setS).catch(() => undefined);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!d) {
    return (
      <div className="stack">
        <div className="page-head"><h1>Printing</h1></div>
        <Card title="Print bills with no print dialog">
          <p>Install <strong>Ease My Bill for Windows</strong> on the billing computer. It's EMB in its own window: pick your
            bill printer once and every bill (with its UPI QR) and KOT prints the moment you save. No dialog, no browser settings.</p>
          <p><a className="btn" href={DOWNLOAD} download>Download for Windows</a></p>
          <ol className="small">
            <li>Open the downloaded <strong>EaseMyBill-Setup.exe</strong>. If Windows says "protected your PC", tap <strong>More info › Run anyway</strong>.</li>
            <li>Ease My Bill opens. Sign in as usual.</li>
            <li>Come back to <strong>Settings › Printing</strong> in the app and pick your bill printer.</li>
          </ol>
          <p className="muted small">It updates itself: the app always opens the latest EMB. On an Android tablet, use Chrome as now.</p>
        </Card>
      </div>
    );
  }

  const save = async (next: DesktopSettings) => {
    setS(next);
    setError(null);
    if (await d.saveSettings(next)) { setSaved("Saved"); setTimeout(() => setSaved(null), 2000); } else setError("Couldn't save");
  };
  const test = async () => {
    setTesting(true);
    setError(null);
    await new Promise((r) => setTimeout(r, 300));
    const err = await printNow("bill");
    setTesting(false);
    if (err) setError(`Test didn't print: ${err}`); else setSaved("Test page sent to the printer");
  };
  const def = list.find((p) => p.isDefault);
  const opts = (
    <>
      <option value="">Windows default{def ? ` (${def.displayName})` : ""}</option>
      {list.map((p) => <option key={p.name} value={p.name}>{p.displayName}</option>)}
    </>
  );
  return (
    <div className="stack">
      <div className="page-head"><h1>Printing on this computer</h1></div>
      <Card title="Printers">
        <p className="muted small">Bills and KOTs print straight away, with no print dialog.</p>
        <Field label="Bill printer"><Select value={s.billPrinter || ""} onChange={(e) => save({ ...s, billPrinter: e.target.value || undefined })}>{opts}</Select></Field>
        <Field label="KOT printer (kitchen slips)"><Select value={s.kotPrinter || ""} onChange={(e) => save({ ...s, kotPrinter: e.target.value || undefined })}>
          <option value="">Same as the bill printer</option>
          {list.map((p) => <option key={p.name} value={p.name}>{p.displayName}</option>)}
        </Select></Field>
        <Toggle checked={kots} onChange={(v) => { setKots(v); setKotsHere(v); }}
          label="Print KOTs on this computer (while the order screen is open). Turn off if another PC or a network printer prints them" />
        <Toggle checked={s.startWithWindows !== false} onChange={(v) => save({ ...s, startWithWindows: v })} label="Open Ease My Bill when the computer starts" />
        <div className="row"><Button variant="ghost" disabled={testing} onClick={test}>{testing ? "Printing…" : "Print a test bill"}</Button>
          {saved && <span className="ok small">{saved}</span>}</div>
        <ErrorText error={error} />
        {list.length === 0 && <p className="warn small">No printers found. Install the printer's Windows driver, then reopen this page.</p>}
      </Card>
      {testing && createPortal(<div id="print-root">
        <div className="receipt"><div className="r-center"><strong>Ease My Bill</strong></div><div className="r-center">Test print</div>
          <div className="r-center">{new Date().toLocaleString("en-IN")}</div><div className="r-center">If you can read this, bills will print here.</div></div>
      </div>, document.body)}
    </div>
  );
}
