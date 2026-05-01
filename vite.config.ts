import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";
import { componentTagger } from "lovable-tagger";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  server: {
    host: "::",
    port: 8080,
    hmr: {
      overlay: false,
    },
    proxy: {
      "/pzapi": {
        target: "http://localhost:8001",
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\/pzapi/, '/api'),
        // SSE fix: Vite 5.x's underlying http-proxy buffers chunked
        // responses, so the frontend's EventSource connection to
        // /pzapi/runs/{id}/events would hang indefinitely without ever
        // receiving pipeline_started / candidates_found / etc. The
        // backend was streaming events fine; the proxy was holding
        // them. Disabling proxyTimeout + selfHandleResponse + the
        // explicit pipe() ensures every chunk flushed by uvicorn
        // immediately flushes through to the browser.
        selfHandleResponse: false,
        proxyTimeout: 0,
        timeout: 0,
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes, _req, res) => {
            const ct = String(proxyRes.headers["content-type"] || "");
            if (ct.includes("text/event-stream")) {
              // Explicitly write the headers + pipe so http-proxy can't
              // hold the body in its internal buffer waiting for EOF.
              res.writeHead(proxyRes.statusCode || 200, proxyRes.headers as Record<string, string | string[]>);
              proxyRes.pipe(res);
            }
          });
        },
      },
    },
  },
  plugins: [react(), mode === "development" && componentTagger()].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
    dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime", "@tanstack/react-query", "@tanstack/query-core"],
  },
}));
