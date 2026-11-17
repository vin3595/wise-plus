import { useEffect, useRef, useState } from "react";
import { ScanLine, X } from "lucide-react";

/* ------------------------------------------------------------------ EAN-13 drawing (for labels) */

const L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"];
const G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111"];
const R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000", "1110100"];
const FIRST = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"];

export const isEan13 = (c: string) => /^\d{13}$/.test(c) && eanCheck(c.slice(0, 12)) === c[12];
export function eanCheck(d12: string): string {
  const s = [...d12].reduce((t, c, i) => t + Number(c) * (i % 2 ? 3 : 1), 0);
  return String((10 - (s % 10)) % 10);
}

function eanBits(code: string): string {
  const d = [...code].map(Number);
  let bits = "101";
  const pat = FIRST[d[0]];
  for (let i = 1; i <= 6; i++) bits += (pat[i - 1] === "L" ? L : G)[d[i]];
  bits += "01010";
  for (let i = 7; i <= 12; i++) bits += R[d[i]];
  return bits + "101";
}

/** An EAN-13 barcode as SVG (scales to any label size). Other codes are shown as text. */
export function Ean13({ code, height = 40 }: { code: string; height?: number }) {
  if (!isEan13(code)) return <span className="mono small">{code}</span>;
  const bits = eanBits(code);
  const guard = new Set([0, 1, 2, 45, 46, 47, 48, 49, 92, 93, 94]);
  const w = bits.length + 18;
  return (
    <svg viewBox={`0 0 ${w} ${height + 10}`} className="ean13" role="img" aria-label={`Barcode ${code}`} preserveAspectRatio="xMidYMid meet">
      {[...bits].map((b, i) => b === "1" && <rect key={i} x={i + 9} y={0} width={1} height={guard.has(i) ? height + 5 : height} fill="#000" />)}
      <text x={3} y={height + 9} fontSize={9} fontFamily="monospace">{code[0]}</text>
      <text x={9 + 24} y={height + 9} fontSize={9} fontFamily="monospace" textAnchor="middle" letterSpacing={1.2}>{code.slice(1, 7)}</text>
      <text x={9 + 70} y={height + 9} fontSize={9} fontFamily="monospace" textAnchor="middle" letterSpacing={1.2}>{code.slice(7)}</text>
    </svg>
  );
}

/* ------------------------------------------------------------------ a hand scanner types fast and presses Enter */

/** Calls onScan when a USB / Bluetooth scanner "types" a code anywhere on the page (not into a text box someone is
 * typing in): characters arrive under ~40 ms apart and end with Enter. */
export function useHandScanner(onScan: (code: string) => void, on = true) {
  const cb = useRef(onScan);
  cb.current = onScan;
  useEffect(() => {
    if (!on) return;
    let buf = "";
    let last = 0;
    const k = (e: KeyboardEvent) => {
      const now = performance.now();
      if (now - last > 60) buf = "";
      last = now;
      if (e.key === "Enter") {
        if (buf.length >= 6) {
          const t = e.target as HTMLElement;
          // in the search box the POS handles Enter itself (and checks for a code there)
          if (!(t.tagName === "INPUT" && (t as HTMLInputElement).dataset.scan === "self")) { e.preventDefault(); cb.current(buf); }
        }
        buf = "";
        return;
      }
      if (e.key.length === 1) buf += e.key;
    };
    window.addEventListener("keydown", k, true);
    return () => window.removeEventListener("keydown", k, true);
  }, [on]);
}

/** Looks like something scanned, not an item name typed into search. */
export const looksScanned = (s: string) => /^\d{8,14}$/.test(s.trim()) || /^OF[2-9A-HJ-NP-Z]{8}$/i.test(s.replace(/[\s-]/g, ""));

/* ------------------------------------------------------------------ the phone or tablet camera */

/** Full-screen camera that reads barcodes and QR codes. Uses the browser's own reader when it has one. */
export function CameraScan({ onScan, onClose, title = "Scan a barcode or offer QR" }: { onScan: (code: string) => void; onClose: () => void; title?: string }) {
  const video = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let stop = () => {};
    let done = false;
    const hit = (code: string) => { if (done || !code) return; done = true; try { navigator.vibrate?.(60); } catch { /* no vibration */ } onScan(code); };
    (async () => {
      try {
        const BD = (window as unknown as { BarcodeDetector?: new (o: { formats: string[] }) => { detect: (v: HTMLVideoElement) => Promise<{ rawValue: string }[]> } }).BarcodeDetector;
        if (BD) {
          const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
          stop = () => stream.getTracks().forEach((t) => t.stop());
          if (!video.current) return;
          video.current.srcObject = stream;
          await video.current.play();
          const det = new BD({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "code_128", "code_39", "qr_code"] });
          const tick = async () => {
            if (done || !video.current) return;
            try { const r = await det.detect(video.current); if (r[0]) return hit(r[0].rawValue); } catch { /* next frame */ }
            requestAnimationFrame(tick);
          };
          tick();
        } else {
          const { BrowserMultiFormatReader } = await import("@zxing/browser");
          const reader = new BrowserMultiFormatReader();
          const ctl = await reader.decodeFromVideoDevice(undefined, video.current!, (res) => { if (res) hit(res.getText()); });
          stop = () => ctl.stop();
        }
      } catch {
        setError("Couldn't open the camera. Allow camera access for this site, or use a barcode scanner.");
      }
    })();
    return () => { done = true; stop(); };
  }, [onScan]);
  return (
    <div className="cam-scan" role="dialog" aria-label={title}>
      <div className="cam-top"><span><ScanLine size={18} aria-hidden /> {title}</span><button className="icon-btn" onClick={onClose} aria-label="Close"><X size={20} /></button></div>
      {error ? <p className="cam-error">{error}</p> : <video ref={video} playsInline muted />}
      <div className="cam-frame" aria-hidden />
    </div>
  );
}
