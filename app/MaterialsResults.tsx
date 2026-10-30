"use client";

import { useState } from "react";
import { downloadArtifact, ImageViewer, TextMaterialPanel } from "./MaterialPanels";
import { useProjectMaterials, type TextKind } from "./useProjectMaterials";

export default function MaterialsResults({ api, projectId, onNotice }: { api: string; projectId: string; onNotice: (message: string) => void }) {
  const store = useProjectMaterials(api, projectId);
  const [open, setOpen] = useState<TextKind | null>(null);
  const [imageId, setImageId] = useState<string | null>(null);
  const saved = store.persisted;
  if (!saved || (!saved.summary.trim() && !saved.minutes.trim() && !saved.images.length)) return null;
  const viewer = store.data?.images.find(image => image.id === imageId);
  const save = (url: string, name: string) => {
    void store.ready().then(() => downloadArtifact(url, name)).catch(reason => onNotice(reason.message));
  };
  return <section className="materials-results" aria-label="Материалы по транскрибации">
    <div className="section-title"><div><div className="eyebrow">Результаты обработки</div><h2>Материалы по транскрибации</h2></div>
      <button type="button" className="reveal-materials" onClick={() => {
        void fetch(`${store.base}/reveal`, { method: "POST" }).then(async response => {
          if (!response.ok) throw new Error((await response.json()).detail || "Не удалось открыть папку");
        }).catch(reason => onNotice(reason.message));
      }}>Открыть папку</button></div>
    {(["summary", "minutes"] as const).filter(kind => saved[kind].trim()).map(kind => {
      const label = kind === "summary" ? "Резюме" : "Протокол";
      return <article className="material-result" key={kind}>
        <div className="material-result-row"><strong>{label}</strong><div className="prompt-copy-actions">
          <button type="button" aria-expanded={open === kind} onClick={() => { void store.flush(); setOpen(open === kind ? null : kind); }}>{open === kind ? "Свернуть" : "Открыть"} {label.toLowerCase()}</button>
          {(["docx", "txt"] as const).map(extension => <button type="button" key={extension} disabled={store.busy}
            aria-label={`Скачать ${label.toLowerCase()} (${extension === "docx" ? "Word" : "TXT"})`}
            onClick={() => save(`${store.base}/text/${kind}/${extension}`, `${label}.${extension}`)}>↓ {extension === "docx" ? "Word" : "TXT"}</button>)}
        </div></div>
        {open === kind && <TextMaterialPanel kind={kind} store={store} onNotice={onNotice} />}
      </article>;
    })}
    {saved.images.map((image, index) => <article className="material-result" key={image.id}>
      <div className="material-result-row"><div><strong>Схема {index + 1}</strong><p>{image.name}{image.note ? ` · ${image.note}` : ""}</p></div><div className="prompt-copy-actions">
        <button type="button" onClick={() => setImageId(image.id)}>Открыть схему {index + 1}</button>
        <button type="button" onClick={() => save(`${store.base}/images/${image.id}?download=true`, image.name)}>↓ Скачать схему {index + 1}</button>
      </div></div>
    </article>)}
    {viewer && <ImageViewer image={viewer} images={store.data?.images || []} base={store.base} onNavigate={setImageId} onClose={() => setImageId(null)} />}
  </section>;
}
