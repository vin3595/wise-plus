// The page HTML has the home page built in for visitors and search engines. Signed-in people, and
// restaurants' own domains, get their app instead (React replaces it a moment later anyway).
try {
  if (localStorage.getItem("wise.token") || !/easemybill\.com$/.test(location.hostname)) document.documentElement.classList.add("signed-in");
} catch (e) { /* storage blocked */ }
