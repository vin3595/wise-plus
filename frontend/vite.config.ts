import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const d = new Date(Date.now() + 5.5 * 3600e3);   // IST
const BUILD = `${d.getUTCFullYear() % 100}.${d.getUTCMonth() + 1}.${d.getUTCDate()}.${String(d.getUTCHours()).padStart(2, "0")}${String(d.getUTCMinutes()).padStart(2, "0")}`;

export default defineConfig({
  plugins: [react()],
  define: { __EMB_BUILD__: JSON.stringify(BUILD) },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/uploads": "http://localhost:8000",
    },
  },
});
