import { useEffect } from "react";
import { EmbLogo, SUPPORT_LABEL, supportWa } from "../components/Brand";
import "./landing.css";

/** A link that leads nowhere on easemybill.com. */
export default function NotFound() {
  useEffect(() => { document.title = "Page not found · Ease My Bill"; }, []);
  return (
    <div className="lp lp-404">
      <header className="lp-top"><a href="/"><EmbLogo height={40} /></a></header>
      <main className="lp-hero">
        <h1>This page doesn't exist</h1>
        <p className="lp-sub">The link may be old or mistyped.</p>
        <p><a className="lp-btn" href="/">Go to Ease My Bill</a></p>
        <p className="lp-small">Already use Ease My Bill? <a href="/login">Sign in</a>. Need help? WhatsApp <a href={supportWa("Hi, a link on easemybill.com didn't work")}>{SUPPORT_LABEL}</a>.</p>
      </main>
    </div>
  );
}
