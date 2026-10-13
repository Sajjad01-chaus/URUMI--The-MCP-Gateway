import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` proxies /api to the gateway running in k3d, so the session cookie stays same-origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://app.127-0-0-1.nip.io", changeOrigin: true } },
  },
  build: { outDir: "dist", sourcemap: false },
});
