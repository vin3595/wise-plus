/** The public pages of easemybill.com: titles and descriptions (built into each page's HTML for search
 *  engines and link previews), and which paths belong to the app (anything else is "not found"). */
export const SITE = "https://www.easemybill.com";

export const PUBLIC_PAGES: { path: string; title: string; description: string }[] = [
  { path: "/", title: "Ease My Bill · Restaurant POS with WhatsApp built in",
    description: "Billing, KOTs, WhatsApp bills, UPI QR on every bill, stock and bill scanning for restaurants and cafés. Made in Jodhpur. Free trial." },
  { path: "/pricing", title: "Pricing · Ease My Bill",
    description: "Simple monthly pricing for Ease My Bill restaurant POS. Free trial, no setup fee, cancel any time." },
  { path: "/contact", title: "Contact us · Ease My Bill",
    description: "Talk to the Ease My Bill team in Jodhpur on WhatsApp or phone. Demos in person or on a call." },
  { path: "/terms", title: "Terms & conditions · Ease My Bill", description: "Terms for using Ease My Bill, the restaurant POS and billing app." },
  { path: "/privacy", title: "Privacy policy · Ease My Bill", description: "How Ease My Bill collects, uses and protects restaurant and customer data." },
  { path: "/refunds", title: "Refunds & cancellation · Ease My Bill", description: "How cancellations and refunds work for Ease My Bill subscriptions." },
  { path: "/delivery", title: "Delivery of service · Ease My Bill", description: "How Ease My Bill is delivered: online, on your phone, tablet or PC, right after sign-up." },
];

/** First path segments the app answers (signed-in screens, links on bills, QR codes, screens). */
export const APP_PREFIXES = [
  "access", "accounts", "activity", "admin", "b", "banks", "barcodes", "bill-keeper", "billing", "bills", "branding", "broadcast", "cash", "customer-screen", "customers", "day-close", "display", "dues", "expenses", "feedback", "floor", "kds", "kiosk", "kitchen", "login", "menu", "offers", "orders", "outlet-menu", "outlets", "payments", "pos-import", "print", "printing", "purchase-bills", "q", "reports", "screen-ads", "screens", "services", "setup", "share", "sheets", "signup", "sms", "start", "stock", "tax", "users",
];
