import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Port 5173 is not incidental: it is the origin allow-listed by the CORS
// middleware in app/api.py. Change it there too, or the dashboard goes blank.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true },
});
