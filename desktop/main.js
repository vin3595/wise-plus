// Ease My Bill for Windows: the EMB web app in its own window, with printing that needs no dialog.
// The app itself always loads from the website, so it is up to date without reinstalling.
const { app, BrowserWindow, ipcMain, shell, Menu } = require("electron");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFile } = require("child_process");

const HOME = process.env.EMB_HOME || "https://www.easemybill.com";
const ORIGIN = new URL(HOME).origin;
const settingsFile = () => path.join(app.getPath("userData"), "printers.json");

function readSettings() {
  try { return JSON.parse(fs.readFileSync(settingsFile(), "utf8")); } catch { return {}; }
}
function writeSettings(s) {
  try { fs.writeFileSync(settingsFile(), JSON.stringify(s, null, 2)); } catch { /* disk full: keep defaults */ }
}

if (!app.requestSingleInstanceLock()) app.quit();
// Look like plain Chrome to websites (Google sign-in refuses "Electron" browsers).
app.userAgentFallback = app.userAgentFallback.replace(/\s?(Electron|ease-my-bill)\/[^\s]+/g, "");

let win;
function createWindow() {
  win = new BrowserWindow({
    width: 1366, height: 820, minWidth: 900, minHeight: 600, show: false,
    title: "Ease My Bill", icon: path.join(__dirname, "build", "icon.png"), autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, spellcheck: false },
  });
  Menu.setApplicationMenu(null);
  win.maximize();
  win.once("ready-to-show", () => win.show());
  win.loadURL(HOME);
  // Links to other sites (Razorpay, WhatsApp, Google) open in the normal browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(ORIGIN) || url.startsWith("https://accounts.google.com")) return { action: "allow" };
    shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(ORIGIN) && !url.startsWith("https://accounts.google.com")) { e.preventDefault(); shell.openExternal(url); }
  });
  win.webContents.on("did-fail-load", (_e, code) => {
    if (code === -3) return; // aborted (navigation replaced)
    setTimeout(() => win && win.loadURL(HOME), 5000);  // internet down: try again
  });
  win.webContents.on("before-input-event", (_e, input) => {
    if (input.type === "keyDown" && (input.key === "F5" || (input.control && input.key.toLowerCase() === "r"))) win.webContents.reload();
    if (input.type === "keyDown" && input.key === "F11") win.setFullScreen(!win.isFullScreen());
  });
}

app.on("second-instance", () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
app.whenReady().then(() => {
  app.setLoginItemSettings({ openAtLogin: readSettings().startWithWindows !== false });
  createWindow();
});
app.on("window-all-closed", () => app.quit());

// ---- printing (only for pages from the EMB site)
const fromEmb = (e) => { try { return new URL(e.senderFrame.url).origin === ORIGIN; } catch { return false; } };

ipcMain.handle("emb:printers", async (e) => {
  if (!fromEmb(e)) return [];
  const list = await e.sender.getPrintersAsync();
  return list.map((p) => ({ name: p.name, displayName: p.displayName || p.name, isDefault: !!p.isDefault }));
});
ipcMain.handle("emb:settings", (e) => (fromEmb(e) ? readSettings() : {}));
ipcMain.handle("emb:save-settings", (e, s) => {
  if (!fromEmb(e) || typeof s !== "object" || !s) return false;
  const cur = readSettings();
  const next = { ...cur };
  for (const k of ["billPrinter", "kotPrinter", "paperMm", "startWithWindows"]) if (k in s) next[k] = s[k];
  writeSettings(next);
  if ("startWithWindows" in s) app.setLoginItemSettings({ openAtLogin: s.startWithWindows !== false });
  return true;
});
// Prints what the page shows for printing (its print styles), straight to the chosen printer.
ipcMain.handle("emb:print", async (e, opts) => {
  if (!fromEmb(e)) return { ok: false, error: "Not allowed" };
  const s = readSettings();
  const kind = opts && opts.kind === "kot" ? "kot" : "bill";
  let deviceName = (kind === "kot" ? s.kotPrinter || s.billPrinter : s.billPrinter) || undefined;
  if (deviceName) {   // the chosen printer was removed or renamed: use Windows' default instead
    const names = (await e.sender.getPrintersAsync()).map((p) => p.name);
    if (!names.includes(deviceName)) deviceName = undefined;
  }
  return new Promise((resolve) => {
  const copies = Math.max(1, Math.min(5, Number(opts && opts.copies) || 1));
  // Page exactly as long as the bill: the printer cuts right after it instead of feeding a full page.
  const width = Number(s.paperMm) === 58 ? 58 : 80;
  const h = Number(opts && opts.heightMm);
  const pageSize = h > 20 && h < 3000 ? { width: width * 1000, height: Math.round(h * 1000) } : undefined;
  e.sender.print({ silent: true, printBackground: true, deviceName, copies, margins: { marginType: "none" }, ...(pageSize ? { pageSize } : {}) },
    (ok, reason) => resolve({ ok, error: ok ? null : reason || "Printer didn't take the job" }));
  });
});
ipcMain.handle("emb:version", () => app.getVersion());

// ---- printer text (ESC/POS) straight to a Windows printer, like Petpooja's KOTs. Works with
// "Generic / Text Only" drivers (which print graphics as blank paper) and ends with the printer's cut command.
const RAW_PS = `param([string]$Printer, [string]$File)
$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class EmbRaw {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  public class DOCINFO { public string pDocName; public string pOutputFile; public string pDataType; }
  [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)] public static extern bool OpenPrinter(string n, out IntPtr h, IntPtr d);
  [DllImport("winspool.drv", SetLastError = true)] public static extern bool ClosePrinter(IntPtr h);
  [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)] public static extern int StartDocPrinter(IntPtr h, int l, [In] DOCINFO d);
  [DllImport("winspool.drv", SetLastError = true)] public static extern bool EndDocPrinter(IntPtr h);
  [DllImport("winspool.drv", SetLastError = true)] public static extern bool StartPagePrinter(IntPtr h);
  [DllImport("winspool.drv", SetLastError = true)] public static extern bool EndPagePrinter(IntPtr h);
  [DllImport("winspool.drv", SetLastError = true)] public static extern bool WritePrinter(IntPtr h, byte[] b, int n, out int w);
  public static string Send(string name, byte[] data) {
    IntPtr h;
    if (!OpenPrinter(name, out h, IntPtr.Zero)) return "Can't open printer (" + Marshal.GetLastWin32Error() + ")";
    try {
      DOCINFO di = new DOCINFO(); di.pDocName = "Ease My Bill KOT"; di.pDataType = "RAW";
      if (StartDocPrinter(h, 1, di) == 0) return "Printer refused the job (" + Marshal.GetLastWin32Error() + ")";
      StartPagePrinter(h); int w; bool ok = WritePrinter(h, data, data.Length, out w); EndPagePrinter(h); EndDocPrinter(h);
      return ok && w == data.Length ? "" : "Write failed (" + Marshal.GetLastWin32Error() + ")";
    } finally { ClosePrinter(h); }
  }
}
"@
$r = [EmbRaw]::Send($Printer, [IO.File]::ReadAllBytes($File))
if ($r) { Write-Output $r; exit 2 }
`;
function rawScript() {
  const f = path.join(app.getPath("userData"), "emb-raw-print.ps1");
  try { if (!fs.existsSync(f) || fs.readFileSync(f, "utf8") !== RAW_PS) fs.writeFileSync(f, RAW_PS); } catch { /* read-only: fails below */ }
  return f;
}
ipcMain.handle("emb:print-raw", async (e, opts) => {
  if (!fromEmb(e) || !opts || typeof opts.data !== "string") return { ok: false, error: "Not allowed" };
  const s = readSettings();
  const list = await e.sender.getPrintersAsync();
  let name = opts.kind === "kot" ? s.kotPrinter || s.billPrinter : s.billPrinter;
  if (!name || !list.some((p) => p.name === name)) name = (list.find((p) => p.isDefault) || {}).name;
  if (!name) return { ok: false, error: "No printer found" };
  const file = path.join(os.tmpdir(), `emb-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.bin`);
  fs.writeFileSync(file, Buffer.from(opts.data, "base64"));
  return new Promise((resolve) => {
    execFile("powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", rawScript(), "-Printer", name, "-File", file],
      { windowsHide: true, timeout: 30000 }, (err, stdout) => {
        fs.unlink(file, () => {});
        const msg = String(stdout || "").trim();
        resolve(err ? { ok: false, error: msg || err.message } : { ok: true, error: null });
      });
  });
});
