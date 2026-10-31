import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";

const source = await readFile(new URL("../app/project-materials-store.ts", import.meta.url), "utf8");
const { ProjectMaterialsStore, projectMaterialsStore } = await import(`data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(source)).toString("base64")}`);
const panelSource = await readFile(new URL("../app/MaterialPanels.tsx", import.meta.url), "utf8");
const uploadSource = panelSource.slice(panelSource.indexOf("export async function uploadSchemes("), panelSource.indexOf("export function ImageViewer("));
const { uploadSchemes } = await import(`data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(uploadSource)).toString("base64")}`);
const empty = () => ({ revision: 0, summary: "", minutes: "", images: [], updated_at: null });
const response = (data, status = 200) => new Response(JSON.stringify(data), { status });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
function memory() {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
}
function fixture(t, initial = empty()) {
  let server = structuredClone(initial);
  let handler;
  const storage = memory();
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push({ url, options });
    if (handler) { const result = await handler(url, options); if (result) return result; }
    if (options?.method === "PUT") {
      const body = JSON.parse(options.body);
      if (body.revision !== server.revision) return response({ detail: "Конфликт" }, 409);
      const kind = url.split("/").at(-1);
      server = { ...server, revision: server.revision + 1, ...(kind === "images" ? { images: body.images } : { [kind]: body.text }) };
    }
    return response(structuredClone(server));
  };
  const store = new ProjectMaterialsStore("/api", "A", { fetcher, storage: () => storage, draftKey: "draft" });
  t.after(() => store.flush());
  return { store, storage, calls, fetcher, setHandler: fn => { handler = fn; }, get server() { return server; }, set server(value) { server = value; } };
}

test("scheme picker snapshots selected files before refreshing and resetting the input", async () => {
  const gate = deferred();
  const selection = [new File(["first"], "first.png"), new File(["second"], "second.png")];
  const uploaded = [];
  const uploading = uploadSchemes({
    refresh: () => gate.promise,
    mutate: async (path, build) => {
      assert.equal(path, "/images");
      const request = build(uploaded.length);
      assert.equal(request.method, "POST");
      uploaded.push(request.body.get("file").name);
    },
  }, selection);
  // Clearing the hidden input empties the live FileList while refresh is pending.
  selection.length = 0;
  gate.resolve();
  await uploading;
  assert.deepEqual(uploaded, ["first.png", "second.png"]);
});

test("project editors and readiness stay isolated", async t => {
  const a = fixture(t), b = fixture(t);
  await Promise.all([a.store.refresh(), b.store.refresh()]);
  a.store.edit("summary", "Проект А");
  assert.equal(a.store.getSnapshot().persisted.summary, "");
  assert.equal(b.store.getSnapshot().data.summary, "");
  await a.store.flush();
  assert.equal(a.store.getSnapshot().persisted.summary, "Проект А");
  assert.equal(b.store.getSnapshot().persisted.summary, "");
});

test("late polling response cannot revert an acknowledged save", async t => {
  const f = fixture(t); await f.store.refresh();
  const gate = deferred();
  f.setHandler((url, options) => !options?.method ? gate.promise : null);
  const poll = f.store.refresh();
  f.store.edit("summary", "Новый текст"); await f.store.flush();
  gate.resolve(response(empty())); await poll;
  assert.equal(f.store.getSnapshot().persisted.summary, "Новый текст");
  assert.equal(f.store.getSnapshot().persisted.revision, 1);
});

test("edits during a save are queued and checkpointed with the acknowledged base", async t => {
  const f = fixture(t); await f.store.refresh();
  const gate = deferred(), started = deferred();
  let writes = 0;
  f.setHandler(async (url, options) => {
    if (options?.method === "PUT") {
      if (++writes === 1) { started.resolve(); await gate.promise; }
      else return response({ detail: "offline" }, 503);
    }
  });
  f.store.edit("summary", "Версия 1"); const saving = f.store.flush(); await started.promise;
  f.store.edit("summary", "Версия 2"); f.store.edit("minutes", "Протокол");
  gate.resolve(); await saving;
  const draft = JSON.parse(f.storage.getItem("draft"));
  assert.equal(draft.pending.summary, "Версия 2");
  assert.equal(draft.original.summary, "Версия 1");
  assert.equal(draft.original.minutes, "");
  assert.equal(f.store.getSnapshot().data.summary, "Версия 2");
  f.setHandler(null); await f.store.flush();
  assert.equal(f.server.summary, "Версия 2"); assert.equal(f.server.minutes, "Протокол");
  assert.equal(f.storage.getItem("draft"), null);
});

test("draft recovery recognizes a save whose acknowledgement was lost", async t => {
  const f = fixture(t, { ...empty(), revision: 1, summary: "Уже записано" });
  f.storage.setItem("draft", JSON.stringify({ pending: { summary: "Уже записано" }, original: { summary: "" } }));
  await f.store.refresh(); await f.store.flush();
  assert.equal(f.store.getSnapshot().dirty, false); assert.equal(f.store.getSnapshot().blocked, false);
  assert.equal(f.calls.filter(call => call.options?.method).length, 0);
});

test("retrying an initial read restores the draft before editing server data", async t => {
  const f = fixture(t);
  f.storage.setItem("draft", JSON.stringify({ pending: { summary: "Локальный черновик" }, original: { summary: "" } }));
  f.setHandler(() => response({ detail: "Unavailable" }, 503));
  await f.store.refresh();
  assert.equal(f.store.getSnapshot().data, null);
  assert.notEqual(f.storage.getItem("draft"), null);
  f.setHandler(null);
  await f.store.refresh(); await f.store.flush();
  assert.equal(f.server.summary, "Локальный черновик");
  assert.equal(f.store.getSnapshot().error, "");
});

test("a real agent conflict preserves the local draft and server result", async t => {
  const f = fixture(t); await f.store.refresh();
  f.server = { ...empty(), revision: 1, summary: "Агент" };
  f.store.edit("summary", "Человек"); await f.store.flush();
  assert.equal(f.server.summary, "Агент"); assert.equal(f.store.getSnapshot().data.summary, "Человек");
  assert.equal(f.store.getSnapshot().blocked, true);
  await assert.rejects(f.store.ready());
  await f.store.reload(); assert.equal(f.store.getSnapshot().data.summary, "Агент");
});

test("reload never discards edits typed while its GET is in flight", async t => {
  const f = fixture(t); await f.store.refresh();
  const gate = deferred(); f.setHandler(() => gate.promise);
  const loading = f.store.reload();
  f.store.edit("summary", "Свежее изменение"); gate.resolve(response(empty()));
  await assert.rejects(loading, /Текст изменился/);
  assert.equal(f.store.getSnapshot().data.summary, "Свежее изменение");
  f.setHandler(null); await f.store.flush();
});

test("keepalive limit measures the entire UTF-8 JSON body for all image notes", async t => {
  const images = [0, 1, 2, 3, 4].map(i => ({ id: String(i).repeat(32), file: `${i}.png`, name: `${i}.png`, note: "я".repeat(9000) }));
  const f = fixture(t, { ...empty(), images }); await f.store.refresh();
  f.store.editNote(images[0].id, "Изменение"); await f.store.flush();
  const put = f.calls.find(call => call.options?.method === "PUT");
  assert.ok(new TextEncoder().encode(put.options.body).length > 64000);
  assert.equal(put.options.keepalive, false);
});

test("shared project instance survives unsubscribe/reopen while saving", async t => {
  const f = fixture(t); const previousFetch = globalThis.fetch;
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  globalThis.fetch = f.fetcher;
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: memory() });
  t.after(() => { globalThis.fetch = previousFetch; if (previousStorage) Object.defineProperty(globalThis, "localStorage", previousStorage); else delete globalThis.localStorage; });
  const store = projectMaterialsStore("/shared", "same-project");
  const unsubscribe = store.subscribe(() => {}); await store.refresh();
  const gate = deferred(), started = deferred();
  f.setHandler(async (url, options) => { if (options?.method) { started.resolve(); await gate.promise; } });
  store.edit("summary", "Не потерять"); const write = store.flush(); await started.promise;
  unsubscribe();
  const reopened = projectMaterialsStore("/shared", "same-project");
  assert.equal(reopened, store);
  const close = reopened.subscribe(() => {});
  assert.equal(reopened.getSnapshot().data.summary, "Не потерять");
  gate.resolve(); await write; close();
});

test("ready waits for sequential operations before exporting", async t => {
  const f = fixture(t); await f.store.refresh();
  const firstGate = deferred(), secondGate = deferred(), firstStarted = deferred(), secondStarted = deferred();
  let count = 0;
  f.setHandler(async (url, options) => {
    if (options?.method === "PUT") {
      if (++count === 1) { firstStarted.resolve(); await firstGate.promise; }
      else { secondStarted.resolve(); await secondGate.promise; }
    }
  });
  const build = (revision, current) => ({ method: "PUT", body: JSON.stringify({ revision, images: current.images }) });
  const first = f.store.mutate("/images", build), second = f.store.mutate("/images", build);
  await firstStarted.promise;
  let ready = false;
  const exporting = f.store.ready().then(() => { ready = true; });
  firstGate.resolve(); await secondStarted.promise;
  try { await new Promise(resolve => setTimeout(resolve, 5)); assert.equal(ready, false); }
  finally { secondGate.resolve(); await Promise.all([first, second, exporting]); }
  assert.equal(ready, true);
});
