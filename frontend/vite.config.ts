import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the FastAPI backend under `/app`, so assets must resolve to `/app/assets/...`.
// Build output stays the default `dist` (backend reads frontend/dist/index.html + /assets).
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: {
    outDir: "dist",
    // Keep the entry HTML untouched enough that the literal `%SHOPIFY_API_KEY%`
    // placeholder survives into dist/index.html for the backend to substitute.
    emptyOutDir: true,
  },
});
