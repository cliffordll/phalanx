import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "@/App";
import "@/index.css";

// Token injected at server-render time (see hermes_cli/web_server.py:mount_spa).
declare global {
  interface Window {
    __HERMES_SESSION_TOKEN__?: string;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
