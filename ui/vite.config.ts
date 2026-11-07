import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";
import { defineConfig } from "vite";

// dev: `npm run dev` proxies to a running taresd; prod: `npm run build` emits ui/dist,
// which taresd serves directly at /.
const TARESD = "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      // The enterprise console code is one lazy chunk, dist/assets/ee-<hash>.js (TR-926).
      output: { manualChunks: (id: string) => (id.includes("/src/ee/") ? "ee" : undefined) },
    },
  },
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    proxy: Object.fromEntries(
      ["/api", "/query", "/read", "/catalog", "/health", "/ingest", "/subscribe", "/unsubscribe"].map(
        (p) => [p, { target: TARESD, changeOrigin: true }],
      ),
    ),
  },
});
