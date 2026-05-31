import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";

// https://vitejs.dev/config/
export default defineConfig(() => ({
  server: {
    host: "::",
    port: 8080,
    watch: {
      ignored: [
        "**/runs/**",
        "**/puzzleeval-api/runs/**",
        "**/PuzzleEval-local/runs/**",
        "**/PuzzleEval-local/build/**",
        "**/dist/**",
      ],
    },
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
        // them. Disabling timeouts and taking over response handling with
        // an explicit pipe() ensures every chunk flushed by uvicorn
        // immediately flushes through to the browser.
        selfHandleResponse: true,
        proxyTimeout: 0,
        timeout: 0,
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes, _req, res) => {
            // Explicitly write the headers + pipe so http-proxy can't
            // hold streaming bodies in its internal buffer waiting for EOF.
            // With selfHandleResponse=true we own every proxied response,
            // so non-SSE JSON/file routes use the same transparent pipe.
            if (!res.headersSent) {
              res.writeHead(proxyRes.statusCode || 200, proxyRes.headers as Record<string, string | string[]>);
            }
            proxyRes.pipe(res);
          });
        },
      },
    },
  },
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
    dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime", "@tanstack/react-query", "@tanstack/query-core"],
  },
}));
