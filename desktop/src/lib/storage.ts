import type { Workspace } from "../types";
import { parseWorkspace, isAllowedExternalUrl } from "./domain";
import { sampleWorkspace } from "./sample";
const KEY = "margin.workspace.v1";
export async function loadWorkspace(): Promise<Workspace> {
  const raw = window.margin
    ? await window.margin.loadWorkspace()
    : localStorage.getItem(KEY);
  if (raw === null || raw === undefined) return sampleWorkspace();
  return parseWorkspace(typeof raw === "string" ? JSON.parse(raw) : raw);
}
export async function saveWorkspace(state: Workspace): Promise<void> {
  if (window.margin) await window.margin.saveWorkspace(state);
  else localStorage.setItem(KEY, JSON.stringify(state));
}
export async function openExternal(url: string): Promise<void> {
  if (!isAllowedExternalUrl(url))
    throw new Error("This destination is not allowed.");
  if (window.margin) await window.margin.openExternal(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}
