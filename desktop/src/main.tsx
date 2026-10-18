import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { IconContext } from "@phosphor-icons/react";
import "./styles.css";
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <IconContext.Provider value={{ weight: "bold" }}>
      <App />
    </IconContext.Provider>
  </StrictMode>,
);
