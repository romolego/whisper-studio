// Editors for one API/project share a queue, even across a quick leave/reopen.
export type ImageMaterial = { id: string; name: string; file: string; note: string };
export type Materials = { revision: number; summary: string; minutes: string; images: ImageMaterial[]; updated_at: string | null };
export type TextKind = "summary" | "minutes";
type Draft = { value: string; original: string | undefined };
type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
type Options = { fetcher?: typeof fetch; storage?: () => DraftStorage; draftKey?: string; legacyDraftKey?: string; idle?: () => void };
export type MaterialSnapshot = { data: Materials | null; persisted: Materials | null; error: string; busy: boolean; dirty: boolean; blocked: boolean };
export const EMPTY_MATERIALS: MaterialSnapshot = { data: null, persisted: null, error: "", busy: false, dirty: false, blocked: false };

function original(data: Materials, key: string): string | undefined {
  return key.startsWith("image:") ? data.images.find(image => image.id === key.slice(6))?.note : data[key as TextKind];
}
function isDraftKey(key: string) { return key === "summary" || key === "minutes" || /^image:[a-f0-9]{32}$/.test(key); }
function message(reason: unknown) { return reason instanceof Error ? reason.message : "Ошибка сохранения материалов"; }

export class ProjectMaterialsStore {
  readonly base: string;
  private state = EMPTY_MATERIALS;
  private server: Materials | null = null;
  private pending = new Map<string, Draft>();
  private listeners = new Set<() => void>();
  private operation: Promise<void> | null = null;
  private reading: Promise<void> | null = null;
  private epoch = 0;
  private restored = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private poll: ReturnType<typeof setInterval> | undefined;
  private eviction: ReturnType<typeof setTimeout> | undefined;
  private readonly fetcher: typeof fetch;
  private readonly storage: () => DraftStorage;
  private readonly draftKey: string;
  private readonly options: Options;

  constructor(api: string, projectId: string, options: Options = {}) {
    this.options = options;
    this.base = projectId ? `${api}/projects/${projectId}/materials` : "";
    this.fetcher = options.fetcher || ((...args) => fetch(...args));
    this.storage = options.storage || (() => localStorage);
    this.draftKey = options.draftKey || `whisper-materials-draft:${api}:${projectId}`;
  }
  getSnapshot = () => this.state;
  private publish(patch: Partial<MaterialSnapshot> = {}) {
    const data = this.server ? { ...this.server,
      summary: this.pending.get("summary")?.value ?? this.server.summary,
      minutes: this.pending.get("minutes")?.value ?? this.server.minutes,
      images: this.server.images.map(image => ({ ...image, note: this.pending.get(`image:${image.id}`)?.value ?? image.note })),
    } : null;
    this.state = { ...this.state, ...patch, data, persisted: this.server, dirty: this.pending.size > 0 };
    this.listeners.forEach(listener => listener());
  }
  subscribe = (listener: () => void) => {
    clearTimeout(this.eviction);
    this.listeners.add(listener);
    if (this.listeners.size === 1 && this.base) {
      void this.refresh();
      this.poll = setInterval(() => void this.refresh(), 5000);
    }
    return () => {
      this.listeners.delete(listener);
      if (!this.listeners.size) {
        clearInterval(this.poll);
        void this.flush().finally(() => this.idle());
      }
    };
  };
  private idle() {
    if (!this.listeners.size && !this.operation && !this.reading && !this.pending.size) {
      clearTimeout(this.eviction);
      this.eviction = setTimeout(() => {
        if (!this.listeners.size && !this.operation && !this.reading && !this.pending.size) this.options.idle?.();
      }, 0);
    }
  }
  private async request(url: string, options?: RequestInit): Promise<Materials> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await this.fetcher(url, { ...options, signal: controller.signal });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        if (response.status === 409 && options?.method && options.method !== "GET") this.publish({ blocked: true });
        throw new Error(typeof payload.detail === "string" ? payload.detail : `Ошибка материалов (${response.status})`);
      }
      return await response.json();
    } catch (reason) {
      if (controller.signal.aborted) throw new Error("Сервер не ответил вовремя. Повторите запрос.");
      throw reason;
    } finally { clearTimeout(timeout); }
  }
  private checkpoint() {
    try {
      if (!this.pending.size) this.storage().removeItem(this.draftKey);
      else this.storage().setItem(this.draftKey, JSON.stringify({
        pending: Object.fromEntries([...this.pending].map(([key, draft]) => [key, draft.value])),
        original: Object.fromEntries([...this.pending].map(([key, draft]) => [key, draft.original])),
      }));
      return true;
    } catch {
      if (this.pending.size) this.publish({ error: "Черновик не сохранён в браузере. Дождитесь сохранения на сервере перед закрытием страницы." });
      return false;
    }
  }
  private restore(next: Materials) {
    if (this.restored) return;
    this.restored = true;
    try {
      const storage = this.storage();
      const legacy = this.options.legacyDraftKey;
      const source = storage.getItem(this.draftKey) ?? (legacy ? storage.getItem(legacy) : null);
      const draft = JSON.parse(source || "null");
      if (draft?.pending && typeof draft.pending === "object") {
        for (const [key, value] of Object.entries(draft.pending)) {
          if (!isDraftKey(key) || typeof value !== "string" || value.length > (key.startsWith("image:") ? 10000 : 2000000)) continue;
          const current = original(next, key);
          if (current === value) continue;
          this.pending.set(key, { value, original: draft.original?.[key] });
          if (current !== draft.original?.[key] || current === undefined) {
            this.state = { ...this.state, blocked: true, error: "Есть несохранённый черновик и другая версия на сервере. Сохраните черновик перед загрузкой серверного варианта." };
          }
        }
        const stored = this.checkpoint();
        if (stored && legacy && storage.getItem(legacy) === source) storage.removeItem(legacy);
      }
    } catch { /* Invalid browser storage must never become server data. */ }
  }
  refresh = async () => {
    if (!this.base || this.operation || this.pending.size || this.state.blocked) return;
    if (this.reading) return this.reading;
    const epoch = this.epoch;
    const work = (async () => {
      try {
        const next = await this.request(this.base, { cache: "no-store" });
        if (epoch !== this.epoch || this.operation || this.pending.size || next.revision < (this.server?.revision ?? 0)) return;
        this.server = next;
        this.restore(next);
        this.publish({ error: this.state.blocked ? this.state.error : "" });
      } catch (reason) { if (epoch === this.epoch) this.publish({ error: message(reason) }); }
    })();
    this.reading = work;
    try { await work; } finally { this.reading = null; }
    if (this.pending.size && !this.state.blocked) void this.flush();
    this.idle();
  };
  private async exclusive<T>(action: () => Promise<T>): Promise<T> {
    while (this.operation) await this.operation;
    let release!: () => void;
    this.operation = new Promise<void>(resolve => { release = resolve; });
    this.epoch++;
    this.publish({ busy: true });
    try { return await action(); }
    catch (reason) { this.publish({ error: message(reason) }); throw reason; }
    finally { this.operation = null; release(); this.publish({ busy: false }); this.idle(); }
  }
  flush = async () => {
    clearTimeout(this.timer);
    while (this.operation) await this.operation;
    if (!this.server || this.state.blocked || !this.pending.size) return;
    try {
      await this.exclusive(async () => {
        if (this.state.blocked) return;
        this.publish({ error: "" });
        while (this.pending.size) {
          const [key, draft] = this.pending.entries().next().value as [string, Draft];
          const noteId = key.startsWith("image:") ? key.slice(6) : null;
          if (noteId && !this.server!.images.some(image => image.id === noteId)) {
            this.publish({ blocked: true });
            throw new Error("Схема удалена в другом окне. Сохраните черновик примечания перед обновлением материалов.");
          }
          const body = JSON.stringify(noteId ? { revision: this.server!.revision,
            images: this.server!.images.map(image => ({ id: image.id, note: image.id === noteId ? draft.value : image.note })) }
            : { text: draft.value, revision: this.server!.revision });
          const next = await this.request(noteId ? `${this.base}/images` : `${this.base}/text/${key}`, {
            method: "PUT", headers: { "Content-Type": "application/json" }, body,
            keepalive: new TextEncoder().encode(body).length < 55000,
          });
          this.server = next;
          const remaining = this.pending.get(key);
          if (remaining?.value === draft.value) this.pending.delete(key);
          else if (remaining) remaining.original = original(next, key);
          this.checkpoint();
          this.publish();
        }
        this.publish({ error: "" });
      });
    } catch { /* The error and draft remain visible to every editor. */ }
  };
  edit = (key: string, value: string) => {
    if (!this.server || !isDraftKey(key)) return;
    if (value.length > (key.startsWith("image:") ? 10000 : 2000000)) throw new Error("Превышен размер материала");
    this.epoch++;
    const previous = this.pending.get(key);
    this.pending.set(key, { value, original: previous ? previous.original : original(this.server, key) });
    this.publish(); this.checkpoint();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.flush(), 650);
  };
  editNote = (id: string, note: string) => this.edit(`image:${id}`, note);
  draftText = () => [...this.pending].map(([key, draft]) => `${key}:\n${draft.value}`).join("\n\n");
  hasUnsaved = () => this.pending.size > 0 || this.operation !== null;
  ready = async () => {
    await this.flush();
    if (!this.server || this.state.blocked || this.pending.size) throw new Error("Материалы ещё не загружены или есть несохранённые изменения.");
  };
  mutate = async (path: string, build: (revision: number, current: Materials) => RequestInit) => {
    await this.ready();
    await this.exclusive(async () => {
      if (this.pending.size || this.state.blocked) throw new Error("Сначала сохраните изменения текста");
      this.server = await this.request(`${this.base}${path}`, build(this.server!.revision, this.server!));
      this.publish({ error: "" });
    });
  };
  reload = async () => {
    await this.exclusive(async () => {
      const epoch = this.epoch;
      const next = await this.request(this.base, { cache: "no-store" });
      if (epoch !== this.epoch) throw new Error("Текст изменился во время обновления. Новые изменения оставлены в черновике.");
      this.server = next; this.pending.clear(); this.restored = true; this.checkpoint();
      this.publish({ blocked: false, error: "" });
    });
  };
}

const stores = new Map<string, ProjectMaterialsStore>();
let editorId: string | undefined;
function editorKey() {
  if (!editorId) {
    try { editorId = sessionStorage.getItem("whisper-materials-editor") || crypto.randomUUID(); sessionStorage.setItem("whisper-materials-editor", editorId); }
    catch { editorId = "default"; }
  }
  return editorId;
}
export function projectMaterialsStore(api: string, projectId = "") {
  const key = JSON.stringify([api, projectId]);
  let store = stores.get(key);
  if (!store) {
    const legacy = `whisper-materials-draft:${projectId}`;
    const storageKey = (key: string) => key === legacy ? key : `${key}:${editorKey()}`;
    store = new ProjectMaterialsStore(api, projectId, {
      legacyDraftKey: legacy,
      storage: () => ({
        getItem: key => localStorage.getItem(storageKey(key)),
        setItem: (key, value) => localStorage.setItem(storageKey(key), value),
        removeItem: key => localStorage.removeItem(storageKey(key)),
      }),
      idle: () => { if (stores.get(key) === store) stores.delete(key); },
    });
    stores.set(key, store);
  }
  return store;
}
