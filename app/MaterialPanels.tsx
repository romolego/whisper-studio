"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Hint from "./Hint";
import { useEffect, useRef, useState } from "react";
import type { ImageMaterial, TextKind, useProjectMaterials } from "./useProjectMaterials";

export type MaterialStore = ReturnType<typeof useProjectMaterials>;

export function saveBlob(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

export async function downloadArtifact(url: string, name: string) {
  const response = await fetch(url);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "Не удалось скачать материал");
  }
  saveBlob(name, await response.blob());
}

export function SaveStatus({ store }: { store: MaterialStore }) {
  if (!store.error && store.data && !store.busy && !store.dirty) return null;
  return <div className="material-save-status" aria-live="polite">
    {store.error ? <>
      <span role="alert">{store.error}</span>
      {!store.data && <button type="button" onClick={() => void store.refresh()}>Повторить загрузку</button>}
      {store.dirty && !store.blocked && <button type="button" onClick={() => void store.flush()}>Повторить сохранение</button>}
      {store.data && <button type="button" onClick={() => {
        if (store.dirty) saveBlob("Несохранённые материалы.txt", new Blob([
          store.draftText(),
        ], { type: "text/plain;charset=utf-8" }));
        void store.reload().catch(() => {});
      }}>Загрузить серверный вариант{store.dirty ? " (черновик скачается)" : ""}</button>}
    </> : store.busy || store.dirty ? <span aria-label="Сохранение изменений">⟳</span> : "Загружаю…"}
  </div>;
}

export function TextMaterialPanel({ kind, store, onNotice }: {
  kind: TextKind; store: MaterialStore; onNotice: (message: string) => void;
}) {
  const text = store.data?.[kind] || "";
  const [editing, setEditing] = useState(!text.trim());
  const area = useRef<HTMLTextAreaElement>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const label = kind === "summary" ? "Резюме" : "Протокол";
  async function paste() {
    try {
      const value = await navigator.clipboard.readText();
      if (!mounted.current) return;
      if (!value) { onNotice("В буфере обмена нет текста"); return; }
      if (value.length > 2000000) { onNotice("Слишком большой текст: максимум 2 миллиона символов"); return; }
      setEditing(true); store.edit(kind, value);
    } catch {
      if (!mounted.current) return;
      setEditing(true);
      onNotice("Браузер не разрешил чтение буфера. Вставьте текст в поле через Ctrl+V.");
      setTimeout(() => area.current?.focus(), 0);
    }
  }
  return <>
    <div className="prompt-copy-actions material-actions">
      {editing ? <>
        <button type="button" disabled={!store.data} onClick={() => void paste()}><span aria-hidden="true">📋</span> Вставить</button>
        {text.trim() && <button type="button" title="Завершить редактирование" disabled={!store.data} onClick={() => {
          void store.ready().then(() => setEditing(false)).catch(reason => onNotice(reason.message));
        }}>✓ Готово</button>}
      </> : <button type="button" onClick={() => setEditing(true)} title="Редактировать материал">✎ Исправить</button>}
      {!editing && text.trim() && (["docx", "txt"] as const).map(extension => <button type="button" key={extension}
        disabled={store.busy} onClick={() => {
          void store.ready().then(() => downloadArtifact(`${store.base}/text/${kind}/${extension}`, `${label}.${extension}`))
            .catch(reason => onNotice(reason.message));
        }}>↓ {extension === "docx" ? "Word" : "TXT"}</button>)}
    </div>
    <SaveStatus store={store} />
    {editing ? <textarea ref={area} className="material-editor" aria-label={`${label}: сохранённый текст`}
      value={text} disabled={!store.data} placeholder={`Вставьте сюда ${kind === "summary" ? "резюме" : "протокол"} из нейросети…`}
      maxLength={2000000} onChange={event => store.edit(kind, event.target.value)} onBlur={() => void store.flush()} />
      : <div className="prompt-content material-text material-markdown" tabIndex={0}><Markdown remarkPlugins={[remarkGfm]}>{text || "Материал пока не добавлен"}</Markdown></div>}
  </>;
}

export function ImageViewer({ image, base, onClose }: { image: ImageMaterial; base: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="scheme-viewer" aria-label={`Просмотр: ${image.name}`} onCancel={event => {
    event.preventDefault(); onClose();
  }}>
    <div className="scheme-viewer-toolbar">
      <span>{image.name}</span>
      <button type="button" aria-label="Уменьшить" onClick={() => setZoom(value => Math.max(.25, value / 1.3))}>−</button>
      <button type="button" title="Вписать в окно" onClick={() => { setZoom(1); setOffset({ x: 0, y: 0 }); }}>{Math.round(zoom * 100)}%</button>
      <button type="button" aria-label="Увеличить" onClick={() => setZoom(value => Math.min(10, value * 1.3))}>+</button>
      <button type="button" onClick={onClose}>Закрыть ×</button>
    </div>
    <div className="scheme-viewer-canvas" onWheel={event => {
      setZoom(value => Math.max(.25, Math.min(10, value * (event.deltaY > 0 ? .9 : 1.1))));
    }} onPointerDown={event => {
      if (event.button !== 0) return;
      event.currentTarget.setPointerCapture(event.pointerId);
      drag.current = { x: event.clientX, y: event.clientY, ox: offset.x, oy: offset.y };
    }} onPointerMove={event => {
      if (drag.current) setOffset({ x: drag.current.ox + event.clientX - drag.current.x, y: drag.current.oy + event.clientY - drag.current.y });
    }} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
      {/* Local attachments must retain their original bytes and size. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={`${base}/images/${image.id}`} alt={image.note || image.name} draggable={false}
        style={{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})` }} />
    </div>
    {image.note && <p className="scheme-viewer-note">{image.note}</p>}
  </dialog>;
}

export function DiagramMaterialPanel({ store, onNotice }: { store: MaterialStore; onNotice: (message: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [viewerId, setViewerId] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const images = store.data?.images || [];
  const viewer = images.find(image => image.id === viewerId);
  async function upload(files: FileList | File[]) {
    if (files.length + images.length > 5) { onNotice("В проекте можно сохранить до пяти схем"); return; }
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        await store.mutate("/images", revision => {
          const body = new FormData(); body.set("revision", String(revision)); body.set("file", file);
          return { method: "POST", body };
        });
      }
    } catch (reason) { onNotice(reason instanceof Error ? reason.message : "Не удалось загрузить картинку"); }
    finally { setUploading(false); }
  }
  function update(change: (current: ImageMaterial[]) => ImageMaterial[]) {
    void store.mutate("/images", (revision, current) => ({ method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision, images: change(current.images).map(({ id, note }) => ({ id, note })) }),
    })).catch(reason => onNotice(reason.message));
  }
  return <>
    <div className="prompt-copy-actions material-actions">
      <button type="button" disabled={!store.data || store.busy || uploading || images.length >= 5} onClick={() => input.current?.click()}>+ Добавить схемы</button>
      <span className="material-counter">{images.length}/5 · <strong>PNG</strong>, JPEG, WebP · до 20 МБ</span>
      <input ref={input} type="file" accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" multiple hidden onChange={event => {
        if (event.target.files) void upload(event.target.files); event.target.value = "";
      }} />
    </div>
    <SaveStatus store={store} />
    <div className={`scheme-dropzone${dragging ? " dragging" : ""}`} onDragOver={event => { event.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)} onDrop={event => {
        event.preventDefault(); setDragging(false);
        if (!uploading && !store.busy && store.data) void upload(event.dataTransfer.files);
      }}>
      {images.length === 0 ? <p>Перетащите сюда сохранённые схемы или нажмите «Добавить схемы».</p> :
        <div className="scheme-grid">{images.map((image, index) => <article className="scheme-card" key={image.id}>
          <div className="scheme-card-actions">
            <span>Вариант {index + 1}</span>
            <button type="button" aria-label={`Переместить вариант ${index + 1} раньше`} disabled={index === 0 || store.busy} onClick={() => {
              update(current => { const next = [...current]; const i = next.findIndex(item => item.id === image.id);
                if (i > 0) [next[i - 1], next[i]] = [next[i], next[i - 1]]; return next; });
            }}>←</button>
            <button type="button" aria-label={`Переместить вариант ${index + 1} позже`} disabled={index === images.length - 1 || store.busy} onClick={() => {
              update(current => { const next = [...current]; const i = next.findIndex(item => item.id === image.id);
                if (i >= 0 && i < next.length - 1) [next[i + 1], next[i]] = [next[i], next[i + 1]]; return next; });
            }}>→</button>
            <button type="button" title="Скачать схему" aria-label={`Скачать вариант ${index + 1}`} disabled={store.busy} onClick={() => {
              void downloadArtifact(`${store.base}/images/${image.id}?download=true`, image.name).catch(reason => onNotice(reason.message));
            }}>↓</button>
            <button type="button" title="Удалить схему" aria-label={`Удалить вариант ${index + 1}`} disabled={store.busy} onClick={() => {
              if (window.confirm(`Удалить вариант ${index + 1}?`)) update(current => current.filter(item => item.id !== image.id));
            }}>×</button>
          </div>
          <button type="button" className="scheme-thumbnail" onClick={() => setViewerId(image.id)} aria-label={`Открыть вариант ${index + 1}`}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={`${store.base}/images/${image.id}`} alt={image.name} />
          </button>
          <textarea aria-label={`Примечание: ${image.name}`} value={image.note} placeholder="Примечание к варианту…" maxLength={10000}
            onChange={event => store.editNote(image.id, event.target.value)} onBlur={() => void store.flush()} />
        </article>)}</div>}
      {uploading && <p aria-live="polite">Загружаю схемы…</p>}
    </div>
    {viewer && <ImageViewer key={viewer.id} image={viewer} base={store.base} onClose={() => setViewerId(null)} />}
  </>;
}

export function PackagePanel({ store, query, defaultInclude, transcriptFormat, onNotice }: {
  store: MaterialStore; query: string; defaultInclude: boolean; transcriptFormat: "txt" | "docx"; onNotice: (message: string) => void;
}) {
  const [include, setInclude] = useState(defaultInclude);
  const [downloading, setDownloading] = useState(false);
  async function run(zip: boolean) {
    setDownloading(true);
    try {
      await store.ready();
      const params = new URLSearchParams(query);
      params.set("include_transcript", String(include)); params.set("transcript_format", transcriptFormat);
      const url = `${store.base}/package/${zip ? "zip" : "files"}?${params}`;
      if (zip) await downloadArtifact(url, "Материалы проекта.zip");
      else {
        const response = await fetch(url);
        if (!response.ok) { const payload = await response.json(); throw new Error(payload.detail || "Ошибка скачивания"); }
        const payload = await response.json() as { files: { name: string; base64: string }[] };
        for (const file of payload.files) {
          const bytes = Uint8Array.from(atob(file.base64), char => char.charCodeAt(0));
          saveBlob(file.name, new Blob([bytes]));
          await new Promise(resolve => setTimeout(resolve, 200));
        }
        onNotice("Файлы отправлены на скачивание. Если браузер спросит — разрешите несколько загрузок.");
      }
    } catch (reason) { onNotice(reason instanceof Error ? reason.message : "Ошибка скачивания"); }
    finally { setDownloading(false); }
  }
  return <>
    <strong className="prompt-title">Скачать материалы проекта <Hint text="Резюме и протокол — Word, схемы — в исходном формате. Транскрибация — с текущими настройками просмотра." /></strong>
    <div className="prompt-copy-actions">
      <button type="button" disabled={downloading || store.busy} onClick={() => void run(false)}>↓ По отдельным файлам</button>
      <button type="button" disabled={downloading || store.busy} onClick={() => void run(true)}>↓ ZIP</button>
    </div>
    <label className="material-include"><input type="checkbox" checked={include} onChange={event => setInclude(event.target.checked)} />
      Включить исходную транскрибацию ({transcriptFormat.toUpperCase()})</label>
    <SaveStatus store={store} />
  </>;
}
