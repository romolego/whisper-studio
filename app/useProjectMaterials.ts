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

// One project queue/revision, with each image button showing only its own group.
export function imageMaterialStore(store: ReturnType<typeof useProjectMaterials>, kind: string) {
  const belongs = (image: import("./project-materials-store").ImageMaterial) => (image.prompt_id || "diagram") === kind;
  const scoped = (data: import("./project-materials-store").Materials | null) => data ? { ...data, images: data.images.filter(belongs) } : null;
  return { ...store, data: scoped(store.data), persisted: scoped(store.persisted),
    mutate: (path: string, build: Parameters<typeof store.mutate>[1]) => store.mutate(
      path === "/images" ? `${path}?kind=${encodeURIComponent(kind)}` : path,
      (revision, current) => {
        const request = build(revision, scoped(current)!);
        if (path === "/images" && request.method === "PUT") {
          const body = JSON.parse(request.body as string);
          const first = current.images.findIndex(belongs);
          const others = current.images.filter(image => !belongs(image));
          others.splice(first < 0 ? others.length : first, 0, ...body.images);
          return { ...request, body: JSON.stringify({ ...body, images: others.map(({ id, note }) => ({ id, note })) }) };
        }
        return request;
      }),
  };
}
