import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Build output goes directly into the Python package's static dir so the
// FastAPI server can serve it via `mount_spa(app)` in hermes_cli/web_server.py.
// `npm run build` therefore double-acts as "stage assets for the wheel".
//
// Dev mode (npm run dev) starts Vite at :5173 and proxies /api → :9119,
// so the developer can run `hermes web --no-open` in one terminal and edit
// React in the other with HMR.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:9119",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: path.resolve(__dirname, "..", "hermes_cli", "web_dist"),
    emptyOutDir: true,
    target: "es2022",
  },
});
