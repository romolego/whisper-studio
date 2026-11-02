"use client";

import { useEffect, useRef, useState } from "react";
import { catalogEvent, type PromptCatalog, type PromptDefinition } from "./prompt-catalog";
import { saveBlob } from "./MaterialPanels";

export default function PromptSettings({ api }: { api: string }) {
  const [draft, setDraft] = useState<PromptCatalog | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [newType, setNewType] = useState<"text" | "images">("text");
  const [editing, setEditing] = useState<string | null>(null);
  const current = useRef<PromptCatalog | null>(null);
  const server = useRef<PromptCatalog | null>(null);
  const writing = useRef(false);
  const changed = useRef(false);
  const conflict = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const key = `whisper-prompt-settings-draft:${api}`;

  async function load(discard = false) {
    try {
      const response = await fetch(`${api}/material-prompt-settings`, { cache: "no-store" });
      if (!response.ok) throw new Error((await response.json()).detail || "Не удалось загрузить промпты");
      const saved = await response.json() as PromptCatalog;
      server.current = saved;
      let restored: PromptCatalog | null = null;
      if (!discard) { try { restored = JSON.parse(localStorage.getItem(key) || "null"); } catch {} }
      current.current = restored && Array.isArray(restored.prompts) ? restored : saved;
      changed.current = current.current !== saved;
      conflict.current = changed.current && current.current!.revision !== saved.revision;
      setDraft(current.current); setDirty(changed.current); setBlocked(conflict.current);
      setError(conflict.current ? "На сервере другая версия. Черновик сохранён в браузере." : "");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось загрузить промпты"); }
  }
  async function save() {
    clearTimeout(timer.current);
    if (writing.current || !server.current || conflict.current) return;
    writing.current = true; setBusy(true);
    try {
      while (changed.current && current.current) {
        const submitted = current.current;
        const response = await fetch(`${api}/material-prompt-settings`, { method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...submitted, revision: server.current.revision }) });
        if (!response.ok) {
          if (response.status === 409) { conflict.current = true; setBlocked(true); }
          throw new Error((await response.json()).detail || "Не удалось сохранить промпты");
        }
        server.current = await response.json() as PromptCatalog;
        if (current.current === submitted) {
          current.current = server.current; changed.current = false; setDraft(server.current); setDirty(false);
          try { localStorage.removeItem(key); } catch {}
        } else {
          current.current = { ...current.current, revision: server.current.revision };
          localStorage.setItem(key, JSON.stringify(current.current));
        }
        setError(""); window.dispatchEvent(new Event(catalogEvent));
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось сохранить промпты"); }
    finally { writing.current = false; setBusy(false); }
  }
  function update(change: (value: PromptCatalog) => PromptCatalog) {
    if (!current.current) return;
    current.current = { ...change(current.current), revision: server.current!.revision };
    changed.current = true; setDirty(true); setDraft(current.current);
    try { localStorage.setItem(key, JSON.stringify(current.current)); }
    catch { setError("Черновик не сохранён в браузере. Дождитесь сохранения на сервере."); }
    clearTimeout(timer.current); timer.current = setTimeout(() => void save(), 650);
  }
  function changePrompt(id: string, change: Partial<PromptDefinition>) {
    update(value => ({ ...value, prompts: value.prompts.map(item => item.id === id ? { ...item, ...change } : item) }));
  }
  function move(index: number, offset: number) {
    update(value => { const prompts = [...value.prompts]; const [item] = prompts.splice(index, 1); prompts.splice(index + offset, 0, item); return { ...value, prompts }; });
  }
  useEffect(() => {
    void load();
    const leave = () => { void save(); };
    const unloading = (event: BeforeUnloadEvent) => { if (changed.current || writing.current) { void save(); event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("pagehide", leave); window.addEventListener("beforeunload", unloading);
    return () => { clearTimeout(timer.current); void save(); window.removeEventListener("pagehide", leave); window.removeEventListener("beforeunload", unloading); };
    // The editor owns this draft until navigation; polling must not replace it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  return <section className="settings-card prompt-settings" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) void save();
  }} onKeyDown={event => { if (event.key === "Escape") { void save(); setEditing(null); } }}>
    <div className="settings-card-title"><span>✎</span><div><h2>Промпты и материалы</h2></div></div>
    {!draft ? <button type="button" onClick={() => void load()}>Загрузить настройки промптов</button> : <>
      <label className="setting-row"><strong>Показывать блок промптов</strong><input type="checkbox" checked={draft.enabled}
        onChange={event => update(value => ({ ...value, enabled: event.target.checked }))} /></label>
      <p>Кнопки можно выключать, перемещать и удалять. Сохранённые материалы остаются в проектах. Тип после создания не меняется.</p>
      {draft.prompts.map((item, index) => <article className="prompt-settings-item" key={item.id}>
        <div className="prompt-settings-row prompt-copy-actions">
          <input type="checkbox" aria-label={`Показывать: ${item.label}`} checked={item.enabled} onChange={event => changePrompt(item.id, { enabled: event.target.checked })} />
          <strong>{item.label}</strong><small>{item.type === "text" ? "Текст" : "Картинки"}</small>
          <button type="button" aria-label={`Выше: ${item.label}`} disabled={!index} onClick={() => move(index, -1)}>↑</button>
          <button type="button" aria-label={`Ниже: ${item.label}`} disabled={index === draft.prompts.length - 1} onClick={() => move(index, 1)}>↓</button>
          <button type="button" aria-expanded={editing === item.id} onClick={() => { void save(); setEditing(editing === item.id ? null : item.id); }}>✎ Править</button>
          <button type="button" aria-label={`Удалить промпт: ${item.label}`} onClick={() => {
            if (window.confirm(`Удалить кнопку «${item.label}»? Сохранённые материалы останутся в проектах.`)) update(value => ({ ...value, prompts: value.prompts.filter(prompt => prompt.id !== item.id) }));
          }}>×</button>
        </div>
        {editing === item.id && <div className="prompt-settings-editor">
          <label>Название<input aria-label="Название промпта" value={item.label} maxLength={120} onChange={event => changePrompt(item.id, { label: event.target.value, title: event.target.value })} /></label>
          <label>Текст промпта<textarea aria-label="Текст промпта" rows={9} maxLength={2000000} value={item.text} onChange={event => changePrompt(item.id, { text: event.target.value })} /></label>
          <button type="button" onClick={() => { void save(); setEditing(null); }}>✓ Готово</button>
        </div>}
      </article>)}
      <div className="prompt-settings-row prompt-copy-actions">
        <select aria-label="Тип нового промпта" value={newType} onChange={event => setNewType(event.target.value as "text" | "images")}><option value="text">Текст</option><option value="images">Картинки</option></select>
        <button type="button" onClick={() => { const id = `custom_${crypto.randomUUID().replaceAll("-", "")}`;
          update(value => ({ ...value, prompts: [...value.prompts, { id, type: newType, enabled: true, label: "Новый промпт", title: "Новый промпт", text: "Введите текст своего промпта." }] })); setEditing(id);
        }}>＋ Добавить промпт</button>
        <button type="button" disabled={busy || blocked || !dirty} onClick={() => void save()}>{busy ? "Сохраняю…" : "Сохранить"}</button>
        <span aria-live="polite">{dirty ? "Есть несохранённые изменения" : "Сохранено"}</span>
      </div>
    </>}
    {error && <div role="alert"><p>{error}</p>
      {!blocked && <button type="button" onClick={() => void save()}>Повторить сохранение</button>}
      <button type="button" onClick={() => {
        if (current.current && changed.current) saveBlob("Черновик промптов.json", new Blob([JSON.stringify(current.current, null, 2)], { type: "application/json" }));
        try { localStorage.removeItem(key); } catch {} void load(true);
      }}>Загрузить серверный вариант{dirty ? " (черновик скачается)" : ""}</button>
    </div>}
  </section>;
}
