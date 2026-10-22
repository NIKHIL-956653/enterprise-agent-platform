import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server runs on 5173, which is the origin the API allows by default (EAP-51).
// VITE_API_BASE overrides where the API lives; it defaults to the local uvicorn in api.ts.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
