import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

function parse(name, source) { return ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX); }
const settings = parse("PromptSettings.tsx", await readFile(new URL("../app/PromptSettings.tsx", import.meta.url), "utf8"));
const catalog = parse("prompt-catalog.ts", await readFile(new URL("../app/prompt-catalog.ts", import.meta.url), "utf8"));
function find(tree, predicate) {
  let found;
  function visit(node) { if (predicate(node)) found = node; ts.forEachChild(node, visit); }
  visit(tree); return found;
}
function compile(source, state) {
  return vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, state);
}
function action(tree, name, state) {
  const node = find(tree, node => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(node, `Missing ${name}`);
  return compile(`${node.getText(tree).replace(/^export\s+/, "")}\n${name}`, state);
}
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
const value = () => ({ revision: 0, enabled: true, prompts: [{ id: "summary", label: "Summary", title: "Summary", type: "text", enabled: true, text: "Original" }] });
const response = (value, status = 200) => new Response(JSON.stringify(value), { status });
const key = "whisper-prompt-settings-draft:/api";
function memory() { const values = new Map(); return { getItem: k => values.get(k) ?? null, setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k) }; }
let editorId = 0;
function editor(initial = value(), storage = memory(), session = memory()) {
  const observed = { draft: initial, dirty: false, error: "", blocked: false, downloads: [] };
  const state = { api: "/api", key, legacyKey: key, current: { current: initial }, server: { current: structuredClone(initial) },
    writing: { current: false }, changed: { current: false }, conflict: { current: false }, timer: { current: undefined },
    loadVersion: { current: 0 }, mounted: { current: true }, localStorage: storage, sessionStorage: session, crypto: { randomUUID: () => `editor-${++editorId}` },
    setDraft: v => { observed.draft = v; }, setDirty: v => { observed.dirty = v; }, setError: v => { observed.error = v; },
    setBlocked: v => { observed.blocked = v; }, setBusy: () => {},
    setTimeout: () => 1, clearTimeout: () => {}, window: new EventTarget(), Event, catalogEvent: "catalog", JSON,
    saveBlob: (name, blob) => observed.downloads.push({ name, blob }), Blob,
    fetch: async () => response(initial),
  };
  const same = find(settings, node => ts.isFunctionDeclaration(node) && node.name?.text === "sameCatalog");
  if (same) state.sameCatalog = action(settings, "sameCatalog", state);
  const draftKey = find(catalog, node => ts.isFunctionDeclaration(node) && node.name?.text === "promptSettingsDraftKey");
  if (draftKey) state.promptSettingsDraftKey = action(catalog, "promptSettingsDraftKey", state);
  const keyDeclaration = find(settings, node => ts.isVariableDeclaration(node) && node.name.getText(settings) === "key");
  state.key = compile(keyDeclaration.initializer.getText(settings), state);
  const load = action(settings, "load", state);
  state.load = (...args) => { state.lastLoad = load(...args); return state.lastLoad; };
  state.save = action(settings, "save", state);
  state.update = action(settings, "update", state);
  const node = find(settings, node => ts.isArrowFunction(node) && node.getText(settings).includes("void load(true)"));
  const reload = compile(`(${node.getText(settings)})`, state);
  return { state, observed, storage, session, load: state.load, save: state.save, update: state.update, reload };
}
const edit = text => state => ({ ...state, prompts: state.prompts.map(item => ({ ...item, text })) });

test("catalog polling never replaces newer visibility/text with a late response", async () => {
  const calls = [], events = new EventTarget();
  let displayed = value(), cleanup;
  const state = { defaultCatalog: value(), catalogEvent: "catalog", window: events,
    useState: () => [displayed, next => { displayed = typeof next === "function" ? next(displayed) : next; }],
    useEffect: effect => { cleanup = effect(); },
    fetch: () => { const gate = deferred(); calls.push(gate); return gate.promise; },
  };
  action(catalog, "usePromptCatalog", state)("/api");
  events.dispatchEvent(new Event("catalog"));
  const newer = { ...value(), revision: 1, enabled: false };
  calls[1].resolve(response(newer));
  await new Promise(resolve => setImmediate(resolve));
  calls[0].resolve(response(value()));
  await new Promise(resolve => setImmediate(resolve));
  cleanup();
  assert.equal(displayed.revision, 1);
  assert.equal(displayed.enabled, false);
});

test("reload cannot erase catalog edits made while its GET is pending", async () => {
  const h = editor(), gate = deferred();
  h.state.fetch = () => gate.promise;
  const loading = h.load(true);
  h.update(edit("Typed during reload"));
  gate.resolve(response(value())); await loading;
  assert.equal(h.state.current.current.prompts[0].text, "Typed during reload");
  assert.equal(h.observed.dirty, true);
  assert.match(h.storage.getItem(h.state.key), /Typed during reload/);
});

test("failed server reload keeps the local catalog checkpoint", async () => {
  const h = editor();
  h.update(edit("Keep offline"));
  h.state.fetch = async () => response({ detail: "offline" }, 503);
  h.reload(); await h.state.lastLoad;
  assert.equal(h.observed.downloads.length, 1);
  assert.match(h.storage.getItem(h.state.key) ?? "", /Keep offline/);
  assert.equal(h.observed.dirty, true);
});

test("reopening recognizes a catalog save whose acknowledgement was lost", async () => {
  const h = editor();
  h.update(edit("Already saved"));
  let saved;
  h.state.fetch = async (_, options) => {
    saved = { ...JSON.parse(options.body), revision: 1 };
    throw Error("lost acknowledgement");
  };
  await h.save();
  const reopened = editor(value(), h.storage, h.session);
  reopened.state.current.current = null; reopened.state.server.current = null;
  // Property order differs from the local creation order; content is identical.
  saved.prompts = saved.prompts.map(({ type, text, ...rest }) => ({ type, text, ...rest }));
  reopened.state.fetch = async () => response(saved);
  await reopened.load();
  assert.equal(reopened.observed.blocked, false);
  assert.equal(reopened.observed.dirty, false);
  assert.equal(reopened.storage.getItem(reopened.state.key), null);
});

test("server reload cannot start while a catalog save is active", async () => {
  const h = editor(), gate = deferred(); let reads = 0;
  h.update(edit("Saving"));
  h.state.fetch = (_, options) => {
    if (options?.method) return gate.promise;
    reads++; return Promise.resolve(response(value()));
  };
  const saving = h.save();
  await h.load(true);
  gate.resolve(response({ ...h.state.current.current, revision: 1 }));
  await saving;
  assert.equal(reads, 0);
  assert.equal(h.state.current.current.prompts[0].text, "Saving");
  assert.equal(h.observed.dirty, false);
});

test("saving catalog settings in another tab cannot erase a failed draft", async () => {
  const storage = memory(), a = editor(value(), storage), b = editor(value(), storage);
  a.update(edit("Tab A")); b.update(edit("Tab B offline"));
  a.state.fetch = async (_, options) => response({ ...JSON.parse(options.body), revision: 1 });
  await a.save();
  assert.match(storage.getItem(b.state.key) ?? "", /Tab B offline/);
  const reopened = editor(value(), storage, b.session);
  reopened.state.current.current = null; reopened.state.server.current = null;
  reopened.state.fetch = async () => response(a.state.server.current);
  await reopened.load();
  assert.equal(reopened.state.current.current.prompts[0].text, "Tab B offline");
  assert.equal(reopened.observed.blocked, true);
});

test("legacy catalog drafts migrate to the current tab before the shared copy is removed", async () => {
  const h = editor();
  h.state.current.current = null; h.state.server.current = null;
  h.storage.setItem(key, JSON.stringify(edit("Legacy draft")(value())));
  await h.load();
  assert.equal(h.observed.draft.prompts[0].text, "Legacy draft");
  assert.equal(h.observed.dirty, true);
  assert.notEqual(h.state.key, key);
  assert.match(h.storage.getItem(h.state.key), /Legacy draft/);
  assert.equal(h.storage.getItem(key), null);
});

test("a late save from the closed editor cannot erase the reopened editor's draft", async () => {
  const a = editor(), gate = deferred();
  a.update(edit("First save"));
  a.state.fetch = () => gate.promise;
  const saving = a.save();
  a.state.mounted.current = false;
  const b = editor(value(), a.storage, a.session);
  b.state.current.current = null; b.state.server.current = null;
  await b.load();
  b.update(edit("Newer edit after reopening"));
  gate.resolve(response({ ...a.state.current.current, revision: 1 }));
  await saving;
  assert.match(b.storage.getItem(b.state.key) ?? "", /Newer edit after reopening/);
  assert.equal(b.observed.dirty, true);
});
