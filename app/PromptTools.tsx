"use client";

import { useEffect, useRef, useState } from "react";
import { diagramWithMaterials, promptWithTranscript, transcriptPrompts } from "./transcript-prompts";
import type { Materials } from "./useProjectMaterials";
import { useProjectMaterials } from "./useProjectMaterials";
import { DiagramMaterialPanel, PackagePanel, TextMaterialPanel } from "./MaterialPanels";

type Props = {
  api?: string;
  projectId?: string;
  transcriptQuery?: string;
  defaultIncludeTranscript?: boolean;
  transcriptFormat?: "txt" | "docx";
  transcript?: string;
  transcriptLoading: boolean;
  onNotice: (message: string) => void;
};

export default function PromptTools({ transcript, transcriptLoading, onNotice, api = "http://127.0.0.1:8765/api", projectId,
  transcriptQuery = "", defaultIncludeTranscript = true, transcriptFormat = "txt" }: Props) {
  const [open, setOpen] = useState<string | null>(null);
  const [tabs, setTabs] = useState<Record<string, "prompt" | "saved">>({});
  const tab = tabs[open || ""] || "prompt";
  const store = useProjectMaterials(api, projectId);
  const flushMaterials = store.flush;
  const [copying, setCopying] = useState(false);
  const root = useRef<HTMLElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  function cancelClose() {
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }

  function close() { setOpen(null); void store.flush(); }
  function activate(id: string) { cancelClose(); setOpen(id); }
  const saved = store.persisted;
  const hasMaterials = !!saved && (!!saved.summary.trim() || !!saved.minutes.trim() || saved.images.length > 0);

  useEffect(() => { if (store.error) onNotice(store.error); }, [store.error, onNotice]);

  useEffect(() => {
    function outside(event: PointerEvent) {
      if (event.target instanceof Node && !root.current?.contains(event.target)) { setOpen(null); void flushMaterials(); }
    }
    function escape(event: KeyboardEvent) {
      if (event.key !== "Escape" || !open || document.querySelector(".scheme-viewer[open]")) return;
      event.preventDefault();
      if (root.current?.contains(document.activeElement)) {
        root.current.querySelector<HTMLButtonElement>(`[aria-controls="prompt-${open}"]`)?.focus();
      }
      setOpen(null); void flushMaterials();
    }
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
      if (closeTimer.current) clearTimeout(closeTimer.current);
    };
  }, [open, flushMaterials]);

  async function copy(text: string, withTranscript: boolean, success?: string) {
    setCopying(true);
    try {
      let copied = false;
      if (navigator.clipboard?.writeText) {
        try {
          await Promise.race([
            navigator.clipboard.writeText(text),
            new Promise((_, reject) => setTimeout(() => reject(new Error("Clipboard timeout")), 1200)),
          ]);
          copied = true;
        } catch { /* Older browsers can still copy through a selected text field. */ }
      }
      if (!copied) {
        const area = document.createElement("textarea");
        area.value = text;
        area.readOnly = true;
        area.style.cssText = "position:fixed;opacity:0;pointer-events:none";
        document.body.appendChild(area);
        area.select();
        try { copied = document.execCommand("copy"); } finally { area.remove(); }
      }
      onNotice(copied
        ? success || (withTranscript ? "Промпт с текущей транскрибацией скопирован" : "Промпт скопирован")
        : "Браузер запретил доступ к буферу обмена");
    } catch {
      onNotice("Не удалось скопировать текст в буфер обмена");
    } finally { setCopying(false); }
  }

  return (
    <nav
      ref={root}
      className="prompt-tools"
      aria-label="Промпты для транскрибации"
      onMouseEnter={cancelClose}
      onMouseLeave={() => {
        cancelClose();
        closeTimer.current = setTimeout(() => {
          const focused = document.activeElement;
          const editing = root.current?.contains(focused) && (focused instanceof HTMLTextAreaElement || focused instanceof HTMLInputElement);
          if (!editing && !document.querySelector(".scheme-viewer[open]")) close();
        }, 900);
      }}
      onBlur={(event) => {
        // Temporarily disabled save/reorder buttons can lose focus to the body.
        // Only actual keyboard navigation out of the toolbar closes it here.
        if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) close();
      }}
    >
      {transcriptPrompts.map((prompt) => (
        <div className="prompt-tool" key={prompt.id} onMouseEnter={() => activate(prompt.id)}>
          <button
            type="button"
            className={`prompt-trigger${open === prompt.id ? " active" : ""}`}
            aria-expanded={open === prompt.id}
            aria-haspopup="dialog"
            aria-controls={`prompt-${prompt.id}`}
            onFocus={() => activate(prompt.id)}
            onClick={() => activate(prompt.id)}
          >
            {prompt.label}
            {projectId && saved && (prompt.id === "diagram" ? saved.images.length > 0 : !!saved[prompt.id as "summary" | "minutes"].trim()) &&
              <span className="material-check" aria-label="Материал готов">✓</span>}
          </button>
          {open === prompt.id && (
            <div className="prompt-popover-wrap">
              <section className={`prompt-popover${tab === "saved" && projectId ? " material-popover" : ""}`} id={`prompt-${prompt.id}`} role="dialog" aria-label={prompt.title}>
                <strong className="prompt-title">{prompt.title}</strong>
                {projectId && <div className="material-tabs" role="tablist" aria-label="Содержимое раздела">
                  <button type="button" role="tab" aria-selected={tab === "prompt"} onClick={() => { void store.flush(); setTabs(current => ({ ...current, [prompt.id]: "prompt" })); }}>Промпт</button>
                  <button type="button" role="tab" aria-selected={tab === "saved"} onClick={() => setTabs(current => ({ ...current, [prompt.id]: "saved" }))}>Сохранённое</button>
                </div>}
                {tab === "saved" && projectId ? (prompt.id === "diagram"
                  ? <DiagramMaterialPanel store={store} onNotice={onNotice} />
                  : <TextMaterialPanel key={`${prompt.id}:${store.data ? "loaded" : "loading"}`} kind={prompt.id as "summary" | "minutes"} store={store} onNotice={onNotice} />)
                  : <>
                <div className="prompt-copy-actions">
                  <button type="button" disabled={copying} onClick={() => copy(prompt.text, false)}>
                    Скопировать промпт
                  </button>
                  {transcript !== undefined && (
                    <button
                      type="button"
                      disabled={copying || transcriptLoading || !transcript.trim()}
                      title={transcriptLoading ? "Готовлю текущий вариант текста…" : !transcript.trim() ? "В текущем виде нет текста для копирования" : "Добавить текст с текущими настройками просмотра"}
                      onClick={() => {
                        if (!transcriptLoading && transcript.trim()) copy(promptWithTranscript(prompt.text, transcript), true);
                      }}
                    >
                      Промпт + транскрибация
                    </button>
                  )}
                  {prompt.id === "diagram" && transcript !== undefined && store.data &&
                    (store.data.summary.trim() || store.data.minutes.trim()) && <button type="button"
                      disabled={copying || store.busy || transcriptLoading || !transcript.trim()}
                      title="Промпт, заполненные резюме и протокол, затем текущая транскрибация"
                      onClick={() => {
                        const text = transcript;
                        setCopying(true);
                        void (async () => {
                          try {
                            await store.ready();
                            const response = await fetch(store.base, { cache: "no-store" });
                            if (!response.ok) throw new Error("Не удалось получить сохранённые материалы");
                            const material = await response.json() as Materials;
                            if (alive.current) await copy(diagramWithMaterials(prompt.text, text, material.summary, material.minutes), true,
                              "Промпт для схемы со всеми материалами скопирован");
                          } catch (reason) {
                            if (alive.current) onNotice(reason instanceof Error ? reason.message : "Не удалось скопировать материалы");
                          } finally { if (alive.current) setCopying(false); }
                        })();
                      }}>Промпт + все материалы</button>}
                </div>
                <pre className="prompt-content" tabIndex={0}>{prompt.text}</pre>
                </>}
              </section>
            </div>
          )}
        </div>
      ))}
      {projectId && hasMaterials && <div className="prompt-tool" onMouseEnter={() => activate("package")}>
        <button type="button" className={`prompt-trigger${open === "package" ? " active" : ""}`} aria-label="Скачать пакет материалов"
          aria-expanded={open === "package"} aria-haspopup="dialog" aria-controls="prompt-package"
          onFocus={() => activate("package")} onClick={() => activate("package")}>↓</button>
        {open === "package" && <div className="prompt-popover-wrap"><section className="prompt-popover" id="prompt-package" role="dialog" aria-label="Скачать пакет материалов">
          <PackagePanel store={store} query={transcriptQuery} defaultInclude={defaultIncludeTranscript} transcriptFormat={transcriptFormat} onNotice={onNotice} />
        </section></div>}
      </div>}
    </nav>
  );
}
