import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "frontend",
  plugins: [react()],
  base: "/static/",
  build: {
    outDir: "../backend/frontend_dist",
    emptyOutDir: true,
    sourcemap: true
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://127.0.0.1:8088",
      "/health": "http://127.0.0.1:8088",
      "/media": "http://127.0.0.1:8088"
    }
  }
});

