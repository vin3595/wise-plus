// After `vite build`: write each public page as its own HTML file with its title, description and content
// already in it (search engines and link previews read these), plus robots.txt, sitemap.xml and the 404 page.
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");
const { render } = await import(pathToFileURL(join(root, "dist-ssr", "prerender.js")).href);
const { PUBLIC_PAGES, SITE, APP_PREFIXES } = await import(pathToFileURL(join(root, "dist-ssr", "prerender.js")).href);
const shell = readFileSync(join(dist, "index.html"), "utf8");
const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");

function page(path, title, description, html, { index = true } = {}) {
  const url = SITE + (path === "/" ? "/" : path);
  let out = shell
    .replace(/<title>[^<]*<\/title>/, `<title>${esc(title)}</title>`)
    .replace(/(<meta name="description" content=")[^"]*(")/, `$1${esc(description)}$2`)
    .replace(/(<meta property="og:title" content=")[^"]*(")/, `$1${esc(title)}$2`)
    .replace(/(<meta property="og:description" content=")[^"]*(")/, `$1${esc(description)}$2`)
    .replace(/(<meta property="og:url" content=")[^"]*(")/, `$1${url}$2`)
    .replace("</head>", `  <link rel="canonical" href="${url}" />\n${index ? "" : '  <meta name="robots" content="noindex" />\n'}  </head>`)
    .replace('<div id="root"></div>', `<div id="root"><div data-prerendered>${html}</div></div>`);
  if (!out.includes("data-prerendered")) throw new Error(`root div not found for ${path}`);
  return out;
}

for (const p of PUBLIC_PAGES) {
  const html = page(p.path, p.title, p.description, render(p.path));
  const file = p.path === "/" ? join(dist, "home.html") : join(dist, p.path.slice(1), "index.html");
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, html);
}
// /start is the same page as the home page
mkdirSync(join(dist, "start"), { recursive: true });
writeFileSync(join(dist, "start", "index.html"), page("/", PUBLIC_PAGES[0].title, PUBLIC_PAGES[0].description, render("/")));
writeFileSync(join(dist, "404.html"), page("/404", "Page not found · Ease My Bill", "This page doesn't exist.", render("/404"), { index: false }));

// "/x$" and "/x/" (not "/x"): a bare prefix would also block e.g. /brand for "b".
const disallow = APP_PREFIXES.filter((p) => p !== "start").flatMap((p) => [`Disallow: /${p}$`, `Disallow: /${p}/`]).join("\n");
writeFileSync(join(dist, "robots.txt"), `User-agent: *\nAllow: /\n${disallow}\nDisallow: /api/\nDisallow: /uploads/\n\nSitemap: ${SITE}/sitemap.xml\n`);
const today = new Date().toISOString().slice(0, 10);
writeFileSync(join(dist, "sitemap.xml"), `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${PUBLIC_PAGES.map((p) => `  <url><loc>${SITE}${p.path}</loc><lastmod>${today}</lastmod><priority>${p.path === "/" ? "1.0" : p.path === "/pricing" ? "0.8" : "0.4"}</priority></url>`).join("\n")}
</urlset>
`);
rmSync(join(root, "dist-ssr"), { recursive: true, force: true });
console.log(`prerendered ${PUBLIC_PAGES.length + 2} pages, robots.txt, sitemap.xml`);
