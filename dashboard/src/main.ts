import { startApp } from "./app";
import { apiBaseUrl } from "./config";
import "./style.css";

const root = document.getElementById("app");
if (root) {
  try {
    startApp(root, apiBaseUrl());
  } catch {
    root.textContent = "The dashboard is misconfigured (VITE_API_BASE_URL).";
  }
}
