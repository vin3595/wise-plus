import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useSession } from "../session";
import { daysLeft, type BillingInfo } from "../pages/Billing";

/** wise-plus: a line above every page in the last week of the trial, or once it has ended. */
export default function TrialBanner() {
  const { can } = useSession();
  const [b, setB] = useState<BillingInfo | null>(null);
  useEffect(() => { api<BillingInfo>("/billing").then(setB).catch(() => setB(null)); }, []);
  if (!b || !b.ready) return null;
  const owner = can("tenant.settings");
  if (b.state === "locked") {
    return <div className="trial-banner locked" role="status">Your free trial has ended, so the app is read-only. Nothing is lost. {owner ? <Link to="/billing">Choose a plan</Link> : "Ask the owner to choose a plan."}</div>;
  }
  if (b.state === "trial" && !b.plan && daysLeft(b.until) <= 7) {
    const d = daysLeft(b.until);
    return <div className="trial-banner" role="status">Free trial: {d} day{d === 1 ? "" : "s"} left. {owner && <Link to="/billing">Subscribe: ₹999/month, everything included</Link>}</div>;
  }
  return null;
}
