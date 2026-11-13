/** A light tap of vibration when a button is pressed, on phones and tablets that support it (Android; iPhones
 *  ignore it). Per device: Setup > Branding has the switch. Never on the customer's own pages. */
const KEY = "wise.haptics";

export function hapticsOn(): boolean {
  try { return localStorage.getItem(KEY) !== "0"; } catch { return true; }
}

export function setHaptics(on: boolean) {
  try { localStorage.setItem(KEY, on ? "1" : "0"); } catch { /* private mode */ }
  if (on) buzz(12);
}

export function buzz(ms = 8) {
  if (!hapticsOn()) return;
  try { navigator.vibrate?.(ms); } catch { /* not allowed */ }
}

const TAPPABLE = "button, a.btn, [role='button'], [role='tab'], .tile-main, .side-link, .choice";

/** One listener for the whole staff app: every press of a control gives a short buzz. */
export function startHaptics() {
  if (typeof navigator === "undefined" || !("vibrate" in navigator)) return;
  document.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse") return;
    const el = (e.target as HTMLElement | null)?.closest<HTMLElement>(TAPPABLE);
    if (!el || (el as HTMLButtonElement).disabled || el.getAttribute("aria-disabled") === "true") return;
    if (!el.closest(".shell, .auth-page")) return;
    // Payment and save buttons get a slightly firmer tap than ordinary ones.
    buzz(el.matches(".pay, .btn-primary, .tender .btn") ? 16 : 8);
  }, { passive: true, capture: true });
}
