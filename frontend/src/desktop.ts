/** The Ease My Bill Windows app (EaseMyBill-Setup.exe) adds window.embDesktop: printing with no dialog. */
export interface DesktopPrinter { name: string; displayName: string; isDefault: boolean }
export interface DesktopSettings { billPrinter?: string; kotPrinter?: string; startWithWindows?: boolean }
interface EmbDesktop {
  print: (o?: { kind?: "bill" | "kot"; copies?: number; heightMm?: number; widthMm?: number }) => Promise<{ ok: boolean; error: string | null }>;
  printRaw?: (o: { kind: "bill" | "kot"; data: string }) => Promise<{ ok: boolean; error: string | null }>;
  printers: () => Promise<DesktopPrinter[]>;
  settings: () => Promise<DesktopSettings>;
  saveSettings: (s: DesktopSettings) => Promise<boolean>;
  version: () => Promise<string>;
}
declare global { interface Window { embDesktop?: EmbDesktop } }

export const desktop = (): EmbDesktop | null => (typeof window !== "undefined" && window.embDesktop) || null;

/** Print what's on #print-root. In the Windows app it goes straight to the chosen printer;
 *  in a browser the print dialog opens as before. Resolves once the job has been handed over. */
export async function printNow(kind: "bill" | "kot" = "bill", copies = 1): Promise<string | null> {
  const d = desktop();
  if (!d) {
    window.print();
    return null;
  }
  // The page is exactly as long as what's printed, so the printer cuts right after it (no blank paper).
  const root = document.getElementById("print-root");
  const heightMm = root ? Math.ceil((root.scrollHeight * 25.4) / 96) + 8 : undefined;
  let error: string | null;
  try {
    const r = await d.print({ kind, copies, heightMm });
    error = r.ok ? null : r.error || "Printer didn't take the job";
  } catch (e) {
    error = e instanceof Error ? e.message : "Printing failed";
  }
  if (error && kind === "bill") window.print();   // never lose a bill: fall back to the print dialog
  return error;
}

// One print at a time: a bill and a KOT made together must not share the page while printing.
let chain: Promise<unknown> = Promise.resolve();
export function withPrintLock<T>(fn: () => Promise<T>): Promise<T> {
  const run = chain.then(fn, fn);
  chain = run.catch(() => undefined);
  return run;
}
export const paintDelay = (ms: number) => new Promise<void>((r) => requestAnimationFrame(() => setTimeout(r, ms)));

const b64 = (bytes: Uint8Array) => { let s = ""; bytes.forEach((b) => { s += String.fromCharCode(b); }); return btoa(s); };

/** Send printer text (ESC/POS) straight to the bill or KOT printer. Only in the Windows app (version with raw printing). */
export async function printRaw(kind: "bill" | "kot", bytes: Uint8Array): Promise<string | null> {
  const d = desktop();
  if (!d?.printRaw) return "This needs the latest Ease My Bill app";
  try {
    const r = await d.printRaw({ kind, data: b64(bytes) });
    return r.ok ? null : r.error || "Printer didn't take the job";
  } catch (e) {
    return e instanceof Error ? e.message : "Printing failed";
  }
}
export const canPrintRaw = () => !!desktop()?.printRaw;
