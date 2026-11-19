import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { EmbLogo, SUPPORT_LABEL, supportWa } from "../components/Brand";
import "./landing.css";

// Public policy pages for Ease My Bill. Razorpay and Paytm review these before they approve a merchant.

export const POLICY_PATHS = ["/terms", "/privacy", "/refunds", "/delivery", "/contact", "/pricing"];

type Info = { bundle_paise: number; trial_days: number; msgs_per_outlet?: number; legal_name: string | null; email: string | null; address: string | null };

const TITLES: Record<string, string> = {
  terms: "Terms & conditions", privacy: "Privacy policy", refunds: "Refunds & cancellation",
  delivery: "Delivery of service", contact: "Contact us", pricing: "Pricing",
};
const UPDATED = "18 November 2026";

export default function Policies({ page }: { page: string }) {
  const [info, setInfo] = useState<Info | null>(null);
  useEffect(() => {
    document.title = `${TITLES[page] || "Policies"} · Ease My Bill`;
    api<Info>("/public/growth/info").then(setInfo).catch(() => setInfo(null));
  }, [page]);

  const who = info?.legal_name ? `${info.legal_name} (trading as Ease My Bill)` : "Ease My Bill";
  const price = info ? `₹${Math.round(info.bundle_paise / 100).toLocaleString("en-IN")}` : "₹999";
  const trial = info?.trial_days ?? 30;
  const mail = info?.email;

  const contactLines = (
    <ul>
      {info?.legal_name && <li><b>Business:</b> {info.legal_name}, trading as Ease My Bill</li>}
      {mail && <li><b>Email:</b> <a href={`mailto:${mail}`}>{mail}</a></li>}
      <li><b>Phone / WhatsApp:</b> <a href={supportWa()}>{SUPPORT_LABEL}</a></li>
      {info?.address && <li><b>Address:</b> {info.address}</li>}
      <li><b>Hours:</b> Monday to Saturday, 10 am to 7 pm IST</li>
    </ul>
  );

  let body: ReactNode;
  if (page === "pricing") body = <>
    <p>Ease My Bill is a cloud billing (POS) system for restaurants and cafés, with WhatsApp bills, offers and reports built in.</p>
    <ul>
      <li><b>{price} per outlet per month</b>, billed monthly through Razorpay. Prices are in Indian rupees.</li>
      <li>A free trial of {trial} days. No card is needed to start.</li>
      {info?.msgs_per_outlet ? <li>Includes {info.msgs_per_outlet.toLocaleString("en-IN")} WhatsApp messages per outlet per month. Extra message packs are sold separately and shown in the app before you buy.</li> : null}
      <li>Taxes, where they apply, are added at checkout and shown on the invoice.</li>
    </ul>
  </>;
  else if (page === "terms") body = <>
    <p>These terms are an agreement between you (the restaurant or business using the service) and {who}. By signing up or using Ease My Bill you accept them.</p>
    <h3>The service</h3>
    <p>Ease My Bill provides billing, order, inventory, customer and WhatsApp messaging software over the internet. We may improve or change features over time.</p>
    <h3>Your account</h3>
    <p>You are responsible for the people you give access to, for keeping PINs and login codes private, and for the accuracy of the bills, taxes and prices you enter. You must have your customers' consent before sending them marketing messages.</p>
    <h3>Payments</h3>
    <p>Paid plans renew monthly until cancelled. If a payment fails, we may pause paid features after a reminder. Your data is kept for at least 90 days so you can resume.</p>
    <h3>Fair use</h3>
    <p>You may not use the service for anything unlawful, to send spam, or to break WhatsApp's or Meta's policies. We may suspend accounts that do.</p>
    <h3>Your data</h3>
    <p>Your business data belongs to you. You can export it at any time. See our <a href="/privacy">privacy policy</a>.</p>
    <h3>Liability</h3>
    <p>We work to keep the service available and accurate, but it is provided as is. Our total liability is limited to the fees you paid in the three months before the claim.</p>
    <h3>Law</h3>
    <p>These terms are governed by the laws of India. Courts in Jodhpur, Rajasthan have jurisdiction.</p>
  </>;
  else if (page === "privacy") body = <>
    <p>This policy explains how {who} handles information in Ease My Bill.</p>
    <h3>What we collect</h3>
    <ul>
      <li>Account details: business name, owner name, phone number, email and outlet addresses.</li>
      <li>Business data you enter: menus, bills, stock, purchases, staff and expenses.</li>
      <li>Your customers' names and phone numbers, when you record them on bills or they message your WhatsApp number.</li>
    </ul>
    <h3>How we use it</h3>
    <p>Only to run the service for you: printing and sending bills, reports, offers, support and billing. We do not sell personal data.</p>
    <h3>Who we share it with</h3>
    <p>Service providers that help us run Ease My Bill, such as cloud hosting (AWS, India region), Meta (WhatsApp messages), SMS providers and Razorpay or Paytm (payments). Each only gets what it needs.</p>
    <h3>Card and bank details</h3>
    <p>Payments are handled by Razorpay or Paytm. We never see or store your card or bank details.</p>
    <h3>Security and retention</h3>
    <p>Data is encrypted in transit, each business's data is kept separate, and backups are taken daily. We keep data while your account is active and up to 90 days after it closes, unless the law needs longer.</p>
    <h3>Your choices</h3>
    <p>You can ask us to export, correct or delete your data by contacting us. Customers can reply "Stop" to any marketing message.</p>
  </>;
  else if (page === "refunds") body = <>
    <h3>Cancellation</h3>
    <p>You can cancel your plan at any time from Billing in the app, or by contacting us. Cancellation takes effect at the end of the month you have paid for, and you keep access until then. No further charges are made.</p>
    <h3>Refunds</h3>
    <ul>
      <li>The {trial}-day free trial lets you try everything before you pay.</li>
      <li>If you are charged by mistake, charged twice, or cancel within 7 days of your first payment, we refund the full amount.</li>
      <li>Other part-month fees are not refunded after the month starts.</li>
      <li>WhatsApp message packs are refundable only if unused.</li>
      <li>Approved refunds go back to the original payment method within 5 to 7 working days.</li>
    </ul>
    <p>To ask for a refund, contact us with your registered phone number.</p>
  </>;
  else if (page === "delivery") body = <>
    <p>Ease My Bill is software delivered online. Nothing is shipped.</p>
    <ul>
      <li>Your account is ready as soon as you sign up, and paid features switch on right after payment succeeds.</li>
      <li>You get a payment confirmation and invoice on WhatsApp or email.</li>
      <li>If anything has not switched on within 24 hours of payment, contact us and we will fix it or refund you.</li>
    </ul>
  </>;
  else body = <>
    <p>We are happy to help with sales, setup, billing or support questions.</p>
    {contactLines}
  </>;

  return (
    <div className="lp">
      <header className="lp-top"><a href="/start"><EmbLogo height={28} /></a></header>
      <main className="lp-policy" style={{ maxWidth: 760, margin: "0 auto", padding: "24px 20px 48px", lineHeight: 1.6 }}>
        <h1>{TITLES[page]}</h1>
        {page !== "contact" && page !== "pricing" && <p className="lp-small">Last updated {UPDATED}</p>}
        {body}
        {page !== "contact" && <><h3>Contact</h3>{contactLines}</>}
      </main>
      <footer className="lp-foot">
        <p className="lp-small">{POLICY_PATHS.map((p, i) => <span key={p}>{i ? " · " : ""}<a href={p}>{TITLES[p.slice(1)]}</a></span>)}</p>
      </footer>
    </div>
  );
}
