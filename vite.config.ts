/* Vite configuration for the eager frontend build and local backend proxies. */

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

const backendProxyTarget = process.env.VITE_BACKEND_PROXY_TARGET ?? "http://localhost:8000";

function eagerVendorChunk(moduleId: string): string | undefined {
  const id = moduleId.replaceAll("\\", "/");
  if (!id.includes("/node_modules/")) {
    return undefined;
  }
  if (
    id.includes("/node_modules/react/") ||
    id.includes("/node_modules/react-dom/") ||
    id.includes("/node_modules/react-is/") ||
    id.includes("/node_modules/scheduler/") ||
    id.includes("/node_modules/use-sync-external-store/")
  ) {
    return "vendor-react";
  }
  if (id.includes("/node_modules/@xyflow/") || id.includes("/node_modules/elkjs/")) {
    return "vendor-topology";
  }
  if (
    id.includes("/node_modules/xterm/") ||
    id.includes("/node_modules/xterm-addon-fit/")
  ) {
    return "vendor-terminal";
  }
  if (id.includes("/node_modules/recharts/")) {
    return "vendor-charts";
  }
  if (
    id.includes("/node_modules/react-markdown/") ||
    id.includes("/node_modules/remark-gfm/")
  ) {
    return "vendor-markdown";
  }
  return "vendor";
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "client", "src"),
      "@assets": path.resolve(import.meta.dirname, "attached_assets"),
    },
  },
  root: path.resolve(import.meta.dirname, "client"),
  build: {
    outDir: path.resolve(import.meta.dirname, "dist/public"),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: eagerVendorChunk,
      },
    },
  },
  server: {
    fs: {
      strict: true,
      deny: ["**/.*"],
    },
    proxy: {
      "/api": {
        target: backendProxyTarget,
        changeOrigin: true,
        timeout: 30000,
      },
      "/ws": {
        target: backendProxyTarget,
        changeOrigin: true,
        ws: true,
        timeout: 30000,
      },
    },
  },
});
