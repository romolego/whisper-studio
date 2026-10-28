"use client";

import { useEffect, useRef, useState } from "react";
import { promptWithTranscript, transcriptPrompts } from "./transcript-prompts";

type Props = {
  transcript?: string;
  transcriptLoading: boolean;
  onNotice: (message: string) => void;
};

export default function PromptTools({ transcript, transcriptLoading, onNotice }: Props) {
  const [open, setOpen] = useState<string | null>(null);
  const [copying, setCopying] = useState(false);
  const root = useRef<HTMLElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function cancelClose() {
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }

  useEffect(() => {
    function outside(event: PointerEvent) {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(null);
    }
    function escape(event: KeyboardEvent) {
      if (event.key !== "Escape" || !open) return;
      event.preventDefault();
      if (root.current?.contains(document.activeElement)) {
        root.current.querySelector<HTMLButtonElement>(`[aria-controls="prompt-${open}"]`)?.focus();
      }
      setOpen(null);
    }
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
      if (closeTimer.current) clearTimeout(closeTimer.current);
    };
  }, [open]);

  async function copy(text: string, withTranscript: boolean) {
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
        ? withTranscript ? "Промпт с текущей транскрибацией скопирован" : "Промпт скопирован"
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
          if (!root.current?.contains(document.activeElement)) setOpen(null);
        }, 180);
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(null);
      }}
    >
      {transcriptPrompts.map((prompt) => (
        <div className="prompt-tool" key={prompt.id} onMouseEnter={() => setOpen(prompt.id)}>
          <button
            type="button"
            className={`prompt-trigger${open === prompt.id ? " active" : ""}`}
            aria-expanded={open === prompt.id}
            aria-haspopup="dialog"
            aria-controls={`prompt-${prompt.id}`}
            onFocus={() => setOpen(prompt.id)}
            onClick={() => setOpen(prompt.id)}
          >
            {prompt.label}
          </button>
          {open === prompt.id && (
            <div className="prompt-popover-wrap">
              <section className="prompt-popover" id={`prompt-${prompt.id}`} role="dialog" aria-label={prompt.title}>
                <strong className="prompt-title">{prompt.title}</strong>
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
                </div>
                <pre className="prompt-content" tabIndex={0}>{prompt.text}</pre>
              </section>
            </div>
          )}
        </div>
      ))}
    </nav>
  );
}
