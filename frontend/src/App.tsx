import { useEffect, useState, type ReactNode } from "react";
import InstallApp from "./components/InstallApp";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { BarChart3, ChefHat, ChevronDown, ClipboardList, LayoutDashboard, LogOut, Menu, NotebookPen, Package, PanelLeftClose, PanelLeftOpen, Receipt,
  Rocket, Settings as SettingsIcon, SlidersHorizontal, Store, Truck, UserCog, Users as UsersIcon, UtensilsCrossed, Wallet, X, type LucideIcon } from "lucide-react";
import { useSession } from "./session";
import Setup from "./pages/Setup";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import Home from "./pages/Home";
import Branding from "./pages/Branding";
import Outlets from "./pages/Outlets";
import Users from "./pages/Users";
import MenuEditor from "./pages/MenuEditor";
import MenuImport from "./pages/MenuImport";
import OutletMenu from "./pages/OutletMenu";
import TaxGroups from "./pages/TaxGroups";
import Kds from "./pages/Kds";
import Orders from "./pages/Orders";
import Kitchen from "./pages/Kitchen";
import PrintStation from "./pages/PrintStation";
import EBill from "./pages/EBill";
import Sms from "./pages/Sms";
import CustomerScreen from "./pages/CustomerScreen";
import ScreenAds from "./pages/ScreenAds";
import KitchenScreen from "./pages/KitchenScreen";
import Screens from "./pages/Screens";
import CustomerDisplay from "./pages/CustomerDisplay";
import SelfOrder from "./pages/SelfOrder";
import Bills from "./pages/Bills";
import Inventory from "./pages/Inventory";
import Payments from "./pages/Payments";
import DayClose from "./pages/DayClose";
import Customers from "./pages/Customers";
import Accounts from "./pages/Accounts";
import Access from "./pages/Access";
import Cash from "./pages/Cash";
import Dues from "./pages/Dues";
import Services from "./pages/Services";
import Sheets from "./pages/Sheets";
import Reports from "./pages/Reports";
import GettingStarted from "./pages/GettingStarted";

const ICONS: Record<string, LucideIcon> = {
  "Home": LayoutDashboard, "Orders": ClipboardList, "Kitchen display": ChefHat, "Bills": Receipt,
  "Cash": Wallet, "Supplier dues": Truck, "Daily sheets": NotebookPen, "Reports": BarChart3, "Stock": Package, "Purchases": Package, "Customers": UsersIcon, "Getting started": Rocket,
  "Menu": UtensilsCrossed, "Outlets and screens": Store, "Staff": UserCog, "Settings": SettingsIcon,
};

function Shell({ children }: { children: ReactNode }) {
  const { branding, me, can, has, signOut } = useSession();
  const loc = useLocation();
  // One menu entry per job. An entry with several pages opens on the first one you may use, and the
  // others show as tabs along the top of the page, so the menu stays short.
  type Page = [string, string, boolean];
  type Entry = { label: string; section: string; pages: Page[] };
  const pos = has("pos");
  const entries: Entry[] = [
    { label: "Home", section: "Counter", pages: [["/", "Home", can("tenant.settings") || can("menu.override")]] },
    { label: "Orders", section: "Counter", pages: [["/orders", "Orders", pos && can("kot.send")]] },
    { label: "Kitchen display", section: "Counter", pages: [["/kds", "Kitchen display", pos && can("kds.view")]] },
    { label: "Bills", section: "Counter", pages: [["/bills", "Bills", pos && can("billing")]] },
    { label: "Cash", section: "Money", pages: [["/cash", "Cash", pos && can("cash.expected")], ["/day-close", "Day close", pos && can("day.close")]] },
    { label: "Daily sheets", section: "Money", pages: [["/sheets", "Daily sheets", has("sheets") && can("reports.view")]] },
    { label: "Supplier dues", section: "Money", pages: [["/dues", "Supplier dues", has("bills") && can("reports.view")]] },
    { label: "Reports", section: "Money", pages: [["/reports", "Reports", can("reports.view")], ["/accounts", "Accounts and Tally", can("reports.view")]] },
    { label: has("stock") ? "Stock" : "Purchases", section: "Stock and customers", pages: [["/stock", has("stock") ? "Stock" : "Purchases", (has("stock") || has("bills")) && can("stock.count")]] },
    { label: "Customers", section: "Stock and customers", pages: [["/customers", "Customers", pos && can("loyalty.manage")]] },
    { label: "Getting started", section: "Setup", pages: [["/start", "Getting started", can("tenant.settings")]] },
    { label: "Menu", section: "Setup", pages: [["/menu", "Items", (pos || has("stock")) && can("menu.manage")], ["/outlet-menu", "Outlet prices", pos && can("menu.override")],
      ["/tax", "Tax", pos && can("menu.manage")], ["/kitchen", "Kitchen and printers", pos && (can("menu.manage") || can("outlets.manage"))]] },
    { label: "Outlets and screens", section: "Setup", pages: [["/outlets", "Outlets", can("outlets.manage")], ["/screens", "Screens", pos && can("outlets.manage")],
      ["/screen-ads", "Screen ads", pos && can("ads.manage")]] },
    { label: "Staff", section: "Setup", pages: [["/users", "Staff", can("users.manage")], ["/access", "Who can do what", can("users.manage")]] },
    { label: "Settings", section: "Setup", pages: [["/services", "Services", can("tenant.settings")], ["/payments", "Payments", pos && can("tenant.settings")], ["/sms", "SMS", pos && can("tenant.settings")],
      ["/branding", "Branding", can("tenant.settings")]] },
  ];
  const path = loc.pathname;
  const onPage = (to: string) => to === "/" ? path === "/" : path === to || path.startsWith(to + "/");
  const visible = entries.map((e) => ({ ...e, pages: e.pages.filter(([, , ok]) => ok) })).filter((e) => e.pages.length > 0);
  const daily = visible.filter((e) => e.section !== "Setup");
  const setupEntries = visible.filter((e) => e.section === "Setup");
  const current = visible.find((e) => e.pages.some(([to]) => onPage(to)));
  const onSetup = current?.section === "Setup";
  const [showSetup, setShowSetup] = useState(() => { try { return localStorage.getItem("wise.nav_setup") === "1"; } catch { return false; } });
  const setupOpen = showSetup || onSetup;
  // The billing counter and the kitchen screen need the whole width: there the side menu shrinks to icons
  // unless the person pinned it open.
  const [pinned, setPinned] = useState<null | boolean>(() => { try { const v = localStorage.getItem("wise.nav_wide"); return v === null ? null : v === "1"; } catch { return null; } });
  const wideScreen = path === "/orders" || path === "/kds";
  const narrow = pinned === null ? wideScreen : !pinned;
  // Phones: the links live in a drawer opened from the top bar; it closes when a page opens.
  const [drawer, setDrawer] = useState(false);
  useEffect(() => { setDrawer(false); }, [path]);
  useEffect(() => {
    if (!drawer) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setDrawer(false); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [drawer]);
  // Keep the open tab in view when the tab row is wider than a phone.
  useEffect(() => { try { document.querySelector(".page-tabs a.on")?.scrollIntoView({ block: "nearest", inline: "nearest" }); } catch { /* old browsers */ } }, [path]);
  const entryLink = (e: Entry) => {
    const Icon = ICONS[e.label] || LayoutDashboard;
    const on = current?.label === e.label;
    return (
      <Link key={e.label} to={e.pages[0][0]} className={`side-link ${on ? "active" : ""}`} aria-current={on ? "page" : undefined} title={e.label}>
        <Icon size={18} strokeWidth={1.75} aria-hidden /><span className="side-label">{e.label}</span>
      </Link>
    );
  };
  const sections = (list: Entry[]) => {
    const out: [string, Entry[]][] = [];
    for (const e of list) { const g = out.find(([n]) => n === e.section); if (g) g[1].push(e); else out.push([e.section, [e]]); }
    return out;
  };
  const appName = branding?.app_name || "Wise";
  const logo = (size: "sm" | "md") => branding?.logo_url
    ? <img className={`brand-logo ${size}`} src={branding.logo_url} alt="" />
    : <span className={`logo-fallback ${size}`}>{appName[0]}</span>;
  const who = (
    <div className="side-who">
      <span className="avatar" aria-hidden>{(me?.user.name || "U")[0].toUpperCase()}</span>
      <span className="side-label who-text"><span className="who-name">{me?.user.name}</span><span className="who-role">{me?.user.role}</span></span>
      <button className="icon-btn" onClick={signOut} aria-label="Sign out" title="Sign out"><LogOut size={16} aria-hidden /></button>
    </div>
  );
  const groups = (list: Entry[], heads: boolean) => sections(list).map(([name, l]) => (
    <section key={name} className="side-group" aria-label={name}>
      {heads && <p className="side-head side-label">{name}</p>}
      {l.map(entryLink)}
    </section>
  ));
  return (
    <div className={`shell has-side ${narrow ? "side-narrow" : ""} ${me?.training ? "is-training" : ""}`}>
      {me?.training && <div className="training-bar" role="status">Training: practice only. Nothing here is a real bill, payment, stock or sale.</div>}
      <div className="shell-body">
        {visible.length > 1 && (
          <aside className="side" aria-label="Menu">
            <div className="side-brand">{logo("md")}<span className="side-label"><strong>{appName}</strong></span></div>
            <nav className="side-nav">
              {groups(daily, visible.length > 4)}
              {setupEntries.length > 0 && (
                <section className="side-group" aria-label="Setup">
                  <button className={`side-link side-fold ${setupOpen ? "open" : ""}`} aria-expanded={setupOpen} title="Setup"
                    onClick={() => { const v = !setupOpen; setShowSetup(v); try { localStorage.setItem("wise.nav_setup", v ? "1" : "0"); } catch { /* */ } }}>
                    <SlidersHorizontal size={18} strokeWidth={1.75} aria-hidden /><span className="side-label">Setup</span>
                    <ChevronDown size={15} className="side-label fold-chev" aria-hidden />
                  </button>
                  {setupOpen && <div className="side-sub">{setupEntries.map(entryLink)}</div>}
                </section>
              )}
            </nav>
            <button className="side-pin" onClick={() => { const v = narrow; setPinned(v); try { localStorage.setItem("wise.nav_wide", v ? "1" : "0"); } catch { /* */ } }}
              aria-label={narrow ? "Widen the menu" : "Shrink the menu to icons"} title={narrow ? "Widen the menu" : "Shrink the menu to icons"}>
              {narrow ? <PanelLeftOpen size={17} aria-hidden /> : <PanelLeftClose size={17} aria-hidden />}
            </button>
            {who}
          </aside>
        )}
        <div className="shell-col">
          <header className="topbar">
            {visible.length > 1 && (
              <button className="menu-btn" aria-expanded={drawer} aria-controls="app-drawer" onClick={() => setDrawer(!drawer)}>
                <Menu size={21} aria-hidden /><span className="sr-only">Open menu</span>
              </button>
            )}
            <div className="brand-mark">{logo("sm")}<strong>{appName}</strong></div>
            <div className="top-title">{current?.label}</div>
            <div className="topbar-right">
              <InstallApp />
              {visible.length <= 1 && <><span className="muted who">{me?.user.name} · {me?.user.role}</span>
                <button className="link" onClick={signOut}>Sign out</button></>}
            </div>
          </header>
          {current && current.pages.length > 1 && (
            <nav className="page-tabs" aria-label={current.label}>
              {current.pages.map(([to, label]) => <Link key={to} to={to} className={onPage(to) ? "on" : ""} aria-current={onPage(to) ? "page" : undefined}>{label}</Link>)}
            </nav>
          )}
          <main className="main" key={current?.label || path}>{children}</main>
          {branding?.show_powered_by !== false && <footer className="powered">Powered by Wise</footer>}
        </div>
      </div>
      <div className={`drawer-back ${drawer ? "open" : ""}`} onClick={() => setDrawer(false)} aria-hidden={!drawer} />
      <nav id="app-drawer" className={`drawer ${drawer ? "open" : ""}`} aria-label="Menu" aria-hidden={!drawer} inert={!drawer}>
        <div className="drawer-top">
          <div className="side-brand">{logo("md")}<strong>{appName}</strong></div>
          <button className="icon-btn" onClick={() => setDrawer(false)} aria-label="Close menu"><X size={19} aria-hidden /></button>
        </div>
        <div className="drawer-links">{groups(visible, visible.length > 4)}</div>
        {who}
      </nav>
    </div>
  );
}

/** Where each role lands after signing in. */
function landing(can: (p: string) => boolean, has: (m: string) => boolean = () => true): string {
  if (can("tenant.settings") || can("menu.override")) return "/";
  if (!has("pos")) return has("sheets") && !has("stock") && !has("bills") ? "/sheets" : "/stock";   // wise-plus: no POS
  if (can("kot.send")) return "/orders";
  return "/kds";
}

export default function App() {
  const { loading, loginId, me, can, has } = useSession();
  // The customer TV screen needs no restaurant setup or login.
  const display = location.pathname.match(/^\/display\/([^/]+)/);
  if (display) return <CustomerDisplay displayKey={decodeURIComponent(display[1])} />;
  // The second screen of a dual-screen POS, facing the customer (opened from Orders on the same computer).
  if (location.pathname === "/customer-screen") return <CustomerScreen />;
  // The customer's e-bill from the SMS link.
  const ebill = location.pathname.match(/^\/b\/([^/]+)/);
  if (ebill) return <EBill shareKey={decodeURIComponent(ebill[1])} />;
  // KOT print station on the PC with the USB printer.
  const station = location.pathname.match(/^\/print\/([^/]+)/);
  if (station) return <PrintStation printKey={decodeURIComponent(station[1])} />;
  // Customer self-ordering: QR at the table, or the kiosk touch screen.
  const self = location.pathname.match(/^\/(q|kiosk)\/([^/]+)/);
  if (self) return <SelfOrder key={self[0]} mode={self[1] === "q" ? "qr" : "kiosk"} outletKey={decodeURIComponent(self[2])} />;
  // Kitchen display from its link: signs itself in, no PIN (Setup > Screens).
  const kitchen = location.pathname.match(/^\/kitchen\/([^/]+)/);
  if (kitchen) return <KitchenScreen kitchenKey={decodeURIComponent(kitchen[1])} />;
  if (loading) return <div className="splash">Loading…</div>;

  if (!me) {
    return (
      <Routes>
        <Route path="/signup" element={<Signup />} />
        <Route path="/login" element={loginId ? <Login /> : <Navigate to="/setup" replace />} />
        <Route path="/setup" element={<Setup />} />
        <Route path="*" element={<Navigate to={loginId ? "/login" : "/setup"} replace />} />
      </Routes>
    );
  }

  return (
    <Shell>
      <Routes>
        <Route path="/" element={landing(can, has) === "/" ? <Home /> : <Navigate to={landing(can, has)} replace />} />
        <Route path="/orders" element={<Orders />} />
        <Route path="/kds" element={<Kds />} />
        <Route path="/kitchen" element={<Kitchen />} />
        <Route path="/bills" element={<Bills />} />
        <Route path="/stock" element={<Inventory />} />
        <Route path="/day-close" element={<DayClose />} />
        <Route path="/customers" element={<Customers />} />
        <Route path="/accounts" element={<Accounts />} />
        <Route path="/access" element={<Access />} />
        <Route path="/cash" element={<Cash />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/dues" element={<Dues />} />
        <Route path="/sheets" element={<Sheets />} />
        <Route path="/services" element={<Services />} />
        <Route path="/menu" element={<MenuEditor />} />
        <Route path="/menu/import" element={<MenuImport />} />
        <Route path="/outlet-menu" element={<OutletMenu />} />
        <Route path="/tax" element={<TaxGroups />} />
        <Route path="/outlets" element={<Outlets />} />
        <Route path="/users" element={<Users />} />
        <Route path="/branding" element={<Branding />} />
        <Route path="/start" element={<GettingStarted />} />
        <Route path="/sms" element={<Sms />} />
        <Route path="/payments" element={<Payments />} />
        <Route path="/screen-ads" element={<ScreenAds />} />
        <Route path="/screens" element={<Screens />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
