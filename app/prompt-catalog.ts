"use client";

import { useEffect, useState } from "react";
import { transcriptPrompts } from "./transcript-prompts";

export type PromptDefinition = { id: string; label: string; title: string; text: string; type: "text" | "images"; enabled: boolean };
export type PromptCatalog = { revision: number; enabled: boolean; prompts: PromptDefinition[] };
export const defaultCatalog: PromptCatalog = { revision: 0, enabled: true, prompts: transcriptPrompts.map(item => ({
  ...item, type: item.id === "diagram" ? "images" : "text", enabled: true,
})) };
export const catalogEvent = "whisper-prompt-catalog";

export function usePromptCatalog(api: string) {
  const [catalog, setCatalog] = useState(defaultCatalog);
  useEffect(() => {
    let alive = true;
    const update = () => {
      void fetch(`${api}/material-prompt-settings`, { cache: "no-store" }).then(async response => {
        if (response.ok && alive) setCatalog(await response.json());
      }).catch(() => {});
    };
    update();
    window.addEventListener(catalogEvent, update);
    window.addEventListener("focus", update);
    return () => { alive = false; window.removeEventListener(catalogEvent, update); window.removeEventListener("focus", update); };
  }, [api]);
  return catalog;
}

export function promptTabKey(api: string, project: string | undefined, id: string) {
  return `whisper-prompt-tab:${JSON.stringify([api, project || "", id])}`;
}
