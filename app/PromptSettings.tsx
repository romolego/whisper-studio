"use client";

import { useEffect, useId, useRef, useState } from "react";
import { catalogEvent, promptSettingsDraftKey, type PromptCatalog, type PromptDefinition } from "./prompt-catalog";
import { saveBlob } from "./MaterialPanels";

function sameCatalog(left: PromptCatalog, right: PromptCatalog) {
  return left.enabled === right.enabled && left.prompts.length === right.prompts.length && left.prompts.every((item, index) => {
    const other = right.prompts[index];
    return (["id", "label", "title", "text", "type", "enabled"] as const).every(key => item[key] === other[key]);
  });
}

function PromptEditor({ item, onSave, onCancel }: { item: PromptDefinition | null; onSave: (value: PromptDefinition) => void; onCancel: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const [value, setValue] = useState<PromptDefinition>(() => item ? { ...item } : {
    id: `custom_${crypto.randomUUID().replaceAll("-", "")}`, type: "text", enabled: true, label: "", title: "", text: "",
  });
  const modified = !!item && (value.label !== item.label || value.title !== item.title || value.text !== item.text);
  const valid = !!value.label.trim() && !!value.text.trim();

  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    element?.showModal();
    nameInput.current?.focus();
    return () => { element?.close(); if (previousFocus?.isConnected) previousFocus.focus(); };
  }, []);

  return <dialog ref={dialog} className="modal prompt-settings-modal" aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); onCancel(); }}
    onClick={event => {
      if (event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onCancel();
    }}>
    <form onSubmit={event => { event.preventDefault(); if (valid && (!item || modified)) onSave(value); }}>
      <button className="modal-close" type="button" aria-label="Отменить редактирование промпта" onClick={onCancel}>×</button>
      <div className="modal-symbol" aria-hidden="true">{item ? "✎" : "＋"}</div>
      <h2 id={titleId}>{item ? "Редактирование промпта" : "Новый промпт"}</h2>
      <div className="prompt-editor-type">
        <span>Тип материала</span>
        {item ? <strong>{item.type === "text" ? "Текст" : "Картинки"}</strong> :
          <div className="choice" role="group" aria-label="Тип нового промпта">
            {(["text", "images"] as const).map(type => <button key={type} type="button" aria-pressed={value.type === type}
              className={value.type === type ? "active" : ""} onClick={() => setValue({ ...value, type })}>{type === "text" ? "Текст" : "Картинки"}</button>)}
          </div>}
      </div>
      <label><span>Название</span><input ref={nameInput} aria-label="Название промпта" value={value.label} maxLength={120} required
        onChange={event => setValue({ ...value, label: event.target.value, title: item && event.target.value === item.label ? item.title : event.target.value })} /></label>
      <label><span>Текст промпта</span><textarea aria-label="Текст промпта" rows={9} maxLength={2000000} required value={value.text}
        onChange={event => setValue({ ...value, text: event.target.value })} /></label>
      <div className="modal-actions">
        <button type="button" onClick={onCancel}>Отмена</button>
        {(item ? modified : valid) && <button className="primary-button" type="submit" disabled={!valid}>{item ? "Готово" : "Сохранить"}</button>}
      </div>
    </form>
  </dialog>;
}

export default function PromptSettings({ api }: { api: string }) {
  const [draft, setDraft] = useState<PromptCatalog | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [editing, setEditing] = useState<PromptDefinition | "new" | null>(null);
  const current = useRef<PromptCatalog | null>(null);
  const server = useRef<PromptCatalog | null>(null);
  const writing = useRef(false);
  const changed = useRef(false);
  const conflict = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const loadVersion = useRef(0);
  const mounted = useRef(true);
  const legacyKey = `whisper-prompt-settings-draft:${api}`;
  const key = promptSettingsDraftKey(api);

  async function load(discard = false) {
    if (writing.current) return;
    const version = ++loadVersion.current;
    const startingDraft = current.current;
    try {
      const response = await fetch(`${api}/material-prompt-settings`, { cache: "no-store" });
      if (!response.ok) throw new Error((await response.json()).detail || "Не удалось загрузить промпты");
      const saved = await response.json() as PromptCatalog;
      if (!mounted.current || version !== loadVersion.current) return;
      if (current.current !== startingDraft) {
        setError("Текст изменился во время обновления. Новые изменения оставлены в черновике.");
        return;
      }
      server.current = saved;
      let restored: PromptCatalog | null = null;
      let source: string | null = null;
      if (!discard) { try {
        source = localStorage.getItem(key) ?? (key !== legacyKey ? localStorage.getItem(legacyKey) : null);
        restored = JSON.parse(source || "null");
      } catch {} }
      current.current = restored && Array.isArray(restored.prompts) && !sameCatalog(restored, saved) ? restored : saved;
      changed.current = current.current !== saved;
      conflict.current = changed.current && current.current!.revision !== saved.revision;
      setDraft(current.current); setDirty(changed.current); setBlocked(conflict.current);
      setError(conflict.current ? "На сервере другая версия. Черновик сохранён в браузере." : "");
      try {
        if (changed.current) localStorage.setItem(key, JSON.stringify(current.current));
        else localStorage.removeItem(key);
        if (key !== legacyKey && source && localStorage.getItem(legacyKey) === source) localStorage.removeItem(legacyKey);
      } catch { if (changed.current) setError("Черновик не сохранён в браузере. Дождитесь сохранения на сервере."); }
    } catch (reason) { if (mounted.current && version === loadVersion.current) setError(reason instanceof Error ? reason.message : "Не удалось загрузить промпты"); }
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
          if (mounted.current) { try { localStorage.removeItem(key); } catch {} }
        } else {
          current.current = { ...current.current, revision: server.current.revision };
          if (mounted.current) localStorage.setItem(key, JSON.stringify(current.current));
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
    mounted.current = true;
    void load();
    const leave = () => { void save(); };
    const unloading = (event: BeforeUnloadEvent) => { if (changed.current || writing.current) { void save(); event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("pagehide", leave); window.addEventListener("beforeunload", unloading);
    return () => { mounted.current = false; clearTimeout(timer.current); void save(); window.removeEventListener("pagehide", leave); window.removeEventListener("beforeunload", unloading); };
    // The editor owns this draft until navigation; polling must not replace it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  return <section className="settings-card prompt-settings" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) void save();
  }}>
    <div className="settings-card-title"><span>10</span><div><h2>Промпты и материалы</h2></div></div>
    {!draft ? <div className="setting-row"><button className="secondary-button" type="button" onClick={() => void load()}>Загрузить настройки промптов</button></div> : <>
      <div className="setting-row">
        <div><strong>Показывать блок промптов</strong><small>Сохранённые материалы остаются в проектах при отключении и удалении кнопок.</small></div>
        <div className="choice" role="group" aria-label="Показывать блок промптов">
          {[false, true].map(enabled => <button type="button" key={String(enabled)} aria-pressed={draft.enabled === enabled}
            className={draft.enabled === enabled ? "active" : ""} onClick={() => update(value => ({ ...value, enabled }))}>{enabled ? "Вкл" : "Выкл"}</button>)}
        </div>
      </div>
      {draft.prompts.map((item, index) => <article className="prompt-settings-item" key={item.id}>
        <div className="prompt-settings-name"><strong>{item.label}</strong><small>{item.type === "text" ? "Текст" : "Картинки"}</small></div>
        <div className="prompt-settings-actions">
          <div className="choice" role="group" aria-label={`Показывать: ${item.label}`}>
            {[false, true].map(enabled => <button type="button" key={String(enabled)} aria-pressed={item.enabled === enabled}
              className={item.enabled === enabled ? "active" : ""} onClick={() => changePrompt(item.id, { enabled })}>{enabled ? "Вкл" : "Выкл"}</button>)}
          </div>
          <div className="prompt-settings-order">
            <button className="prompt-settings-action" type="button" aria-label={`Выше: ${item.label}`} title="Выше" disabled={!index} onClick={() => move(index, -1)}>↑</button>
            <button className="prompt-settings-action" type="button" aria-label={`Ниже: ${item.label}`} title="Ниже" disabled={index === draft.prompts.length - 1} onClick={() => move(index, 1)}>↓</button>
          </div>
          <button className="prompt-settings-action" type="button" aria-label={`Править промпт: ${item.label}`} aria-haspopup="dialog" onClick={() => setEditing(item)}>Править</button>
          <button className="prompt-settings-action" type="button" aria-label={`Удалить промпт: ${item.label}`} title="Удалить промпт" onClick={() => {
            if (window.confirm(`Удалить кнопку «${item.label}»? Сохранённые материалы останутся в проектах.`)) update(value => ({ ...value, prompts: value.prompts.filter(prompt => prompt.id !== item.id) }));
          }}>×</button>
        </div>
      </article>)}
      <div className="setting-row">
        <button className="secondary-button" type="button" onClick={() => setEditing("new")}>＋ Добавить промпт</button>
      </div>
    </>}
    {error && <div className="prompt-settings-error" role="alert"><p>{error}</p>
      <div className="prompt-settings-actions">
        {!blocked && <button className="secondary-button" type="button" disabled={busy} onClick={() => void save()}>Повторить сохранение</button>}
        <button className="secondary-button" type="button" disabled={busy} onClick={() => {
          if (current.current && changed.current) saveBlob("Черновик промптов.json", new Blob([JSON.stringify(current.current, null, 2)], { type: "application/json" }));
          void load(true);
        }}>Загрузить серверный вариант{dirty ? " (черновик скачается)" : ""}</button>
      </div>
    </div>}
    {editing && <PromptEditor item={editing === "new" ? null : editing} onCancel={() => setEditing(null)} onSave={prompt => {
      update(value => ({ ...value, prompts: editing === "new" ? [...value.prompts, prompt] : value.prompts.map(item => item.id === prompt.id ? prompt : item) }));
      setEditing(null); void save();
    }} />}
  </section>;
}
