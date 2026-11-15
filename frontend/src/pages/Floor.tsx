import { useEffect, useState } from "react";
import { api } from "../api";
import TableAlerts from "../components/TableAlerts";
import { useSession } from "../session";
import { Select } from "../ui";

/** For a waiter's phone: table requests full screen, with sound. Keep this page open. */
export default function Floor() {
  const { me, deviceOutletId } = useSession();
  const [outlets, setOutlets] = useState<{ id: string; name: string }[]>([]);
  const [outletId, setOutletId] = useState(me?.outlet_id || deviceOutletId || "");
  useEffect(() => { api<{ id: string; name: string }[]>("/tenant/outlets").then((o) => { setOutlets(o); setOutletId((c) => c || o[0]?.id || ""); }).catch(() => {}); }, []);
  return (
    <div className="stack">
      <div className="row between"><h1>Table requests</h1>
        {outlets.length > 1 && !me?.outlet_id && <Select value={outletId} onChange={(e) => setOutletId(e.target.value)} aria-label="Outlet">{outlets.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</Select>}
      </div>
      <p className="muted small">Keep this page open on a waiter's phone. Water, waiter and bill requests from table QRs appear here with a sound; tap Done when it's handled.</p>
      {outletId && <TableAlerts outletId={outletId} full />}
    </div>
  );
}
