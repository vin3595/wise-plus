/** wise-plus product brand: Ease My Bill. A restaurant's own logo always comes first; this is the fallback
 *  and the "Powered by" line. */
export function EmbMark({ size = 34, className = "" }: { size?: number; className?: string }) {
  return <img src="/brand/logo-icon.png" alt="Ease My Bill" width={size} height={size} className={`emb-mark ${className}`} style={{ width: size, height: size }} />;
}

export function EmbLogo({ height = 44 }: { height?: number }) {
  return <img src="/brand/logo-full.png" alt="Ease My Bill" className="emb-logo" style={{ height }} />;
}

export function PoweredBy({ className = "powered" }: { className?: string }) {
  return (
    <footer className={`${className} powered-emb`}>
      <a href="https://easemybill.com" target="_blank" rel="noreferrer">Powered by <img src="/brand/logo-full.png" alt="Ease My Bill" /></a>
    </footer>
  );
}
