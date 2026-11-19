import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { clearShared, sharedFiles } from "../shared";
import { useSession } from "../session";
import { Button, Card, ErrorText, Select, useAction } from "../ui";

/** A photo or PDF shared into Ease My Bill from WhatsApp, Gallery or Files: what is it? */
export default function Share() {
  const { can, deviceOutletId } = useSession();
  const navigate = useNavigate();
  const [files, setFiles] = useState<File[] | null>(null);
  const [outlets, setOutlets] = useState<{ id: string; name: string }[]>([]);
  const [outlet, setOutlet] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const act = useAction();

  useEffect(() => {
    sharedFiles().then(setFiles).catch(() => setFiles([]));
    api<{ id: string; name: string }[]>("/tenant/outlets").then((o) => {
      setOutlets(o); setOutlet(deviceOutletId && o.some((x) => x.id === deviceOutletId) ? deviceOutletId : o[0]?.id || "");
    }).catch(() => setOutlets([]));
  }, [deviceOutletId]);

  if (files === null) return <p className="muted">Opening what you shared…</p>;
  if (!files.length) return (
    <Card title="Nothing to add">
      <p className="muted">The shared file didn't come through. Share it again, or use Scan bills in Stock › Purchases.</p>
      <Button onClick={() => navigate("/")}>Home</Button>
    </Card>);

  const purchases = () => act.run(async () => {
    let ok = 0; const bad: string[] = [];
    for (let i = 0; i < files.length; i++) {
      setDone(`Reading ${i + 1} of ${files.length}…`);
      const fd = new FormData();
      fd.append("outlet_id", outlet); fd.append("file", files[i]); fd.append("batch", "true");
      try { await api("/inventory/purchases/scan", { form: fd }); ok++; }
      catch (e) { bad.push(e instanceof Error ? e.message : "couldn't read it"); }
    }
    await clearShared();
    if (!bad.length) { navigate("/stock"); return; }
    setDone(`${ok} of ${files.length} read and waiting in Stock › Purchases › Bills to check. Not read: ${bad.join("; ")}`);
  });

  const petty = () => navigate("/expenses?shared=1");   // Petty expenses reads the first file and shows the lines to save

  return (
    <div className="stack">
      <h1>Add to Ease My Bill</h1>
      <Card title={`${files.length} file${files.length === 1 ? "" : "s"} shared`}>
        <div className="row wrap" style={{ gap: 8 }}>
          {files.slice(0, 8).map((f, i) => f.type.startsWith("image/")
            ? <img key={i} src={URL.createObjectURL(f)} alt={f.name} style={{ width: 84, height: 84, objectFit: "cover", borderRadius: 8 }} />
            : <span key={i} className="chip">📄 {f.name}</span>)}
        </div>
        {outlets.length > 1 && <p><Select value={outlet} onChange={(e) => setOutlet(e.target.value)} aria-label="Outlet">
          {outlets.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select></p>}
        <p className="small muted">What is it?</p>
        <div className="row wrap">
          {can("stock.count") && <Button disabled={act.busy || !outlet} onClick={purchases}>Supplier bill{files.length > 1 ? "s" : ""} (purchase)</Button>}
          {can("billing") && <Button variant="ghost" disabled={act.busy} onClick={petty}>Petty cash slip</Button>}
          <Button variant="ghost" disabled={act.busy} onClick={async () => { await clearShared(); navigate("/"); }}>Cancel</Button>
        </div>
        {done && <p className="small" role="status">{done}</p>}
        <ErrorText error={act.error} />
      </Card>
    </div>
  );
}
