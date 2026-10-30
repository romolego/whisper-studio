"use client";

import { useEffect, useMemo, useSyncExternalStore } from "react";
import { EMPTY_MATERIALS, projectMaterialsStore } from "./project-materials-store";
export type { ImageMaterial, Materials, TextKind } from "./project-materials-store";

const serverSnapshot = () => EMPTY_MATERIALS;

export function useProjectMaterials(api: string, projectId?: string) {
  const store = useMemo(() => projectMaterialsStore(api, projectId), [api, projectId]);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, serverSnapshot);
  useEffect(() => {
    const leaving = () => { void store.flush(); };
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (store.hasUnsaved()) { void store.flush(); event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("pagehide", leaving);
    window.addEventListener("beforeunload", beforeUnload);
    return () => {
      window.removeEventListener("pagehide", leaving);
      window.removeEventListener("beforeunload", beforeUnload);
    };
  }, [store]);
  return { ...snapshot, base: store.base, edit: store.edit, editNote: store.editNote,
    flush: store.flush, mutate: store.mutate, reload: store.reload, refresh: store.refresh, ready: store.ready, draftText: store.draftText };
}
