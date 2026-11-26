/** Server-side render of the public pages, run once at build time (scripts/prerender.mjs). */
import { renderToString } from "react-dom/server";
import Landing from "./pages/Landing";
import NotFound from "./pages/NotFound";
import Policies from "./pages/Policies";

export function render(path: string): string {
  if (path === "/") return renderToString(<Landing />);
  if (path === "/404") return renderToString(<NotFound />);
  return renderToString(<Policies page={path.slice(1)} />);
}
export { APP_PREFIXES, PUBLIC_PAGES, SITE } from "./seo";
