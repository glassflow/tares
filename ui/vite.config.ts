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
      // The enterprise console code is one lazy chunk, dist/assets/ee-<hash>.js (TR-926): the chunk
      // Rollup makes for the dynamic import of src/ee/index.ts is named ee. No manualChunks here: it
      // would pull React and shared components into the ee chunk and make the main chunk import it.
      output: {
        chunkFileNames: (chunk: { facadeModuleId?: string | null }) =>
          chunk.facadeModuleId?.endsWith("/src/ee/index.ts") ? "assets/ee-[hash].js" : "assets/[name]-[hash].js",
      },
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
