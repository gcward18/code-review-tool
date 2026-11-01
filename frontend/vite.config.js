import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds into the Python package so `autosde serve` needs no Node at runtime.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../autosde/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
