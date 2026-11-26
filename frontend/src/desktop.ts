/** The Ease My Bill Windows app (EaseMyBill-Setup.exe) adds window.embDesktop: printing with no dialog. */
export interface DesktopPrinter { name: string; displayName: string; isDefault: boolean }
export interface DesktopSettings { billPrinter?: string; kotPrinter?: string; startWithWindows?: boolean }
interface EmbDesktop {
  print: (o?: { kind?: "bill" | "kot"; copies?: number }) => Promise<{ ok: boolean; error: string | null }>;
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
  let error: string | null;
  try {
    const r = await d.print({ kind, copies });
    error = r.ok ? null : r.error || "Printer didn't take the job";
  } catch (e) {
    error = e instanceof Error ? e.message : "Printing failed";
  }
  if (error && kind === "bill") window.print();   // never lose a bill: fall back to the print dialog
  return error;
}
