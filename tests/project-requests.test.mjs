import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function action(name, state) {
  let fn;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) fn = node;
    else ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(fn);
  const js = ts.transpileModule(fn.getText(tree), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return vm.runInNewContext(js + "\n" + name, state);
}
const noop = () => {};
const submit = { preventDefault: noop };

test("a pending creation handles two submits once and unlocks after failure", async () => {
  let release, calls = 0;
  const opened = [], notices = [];
  const state = { API: "/api", projectCreationPending: { current: false }, setCreatingProject: noop, newProjectTitle: "Meeting",
    newProjectFolder: "", setCreateOpen: noop, setNewProjectTitle: noop, setNewProjectFolder: noop, refresh: noop,
    openProject: id => opened.push(id), setNotice: text => notices.push(text),
    fetch: () => { calls++; return new Promise((resolve, reject) => { release = reject; }); } };
  const create = action("createProject", state);
  const first = create(submit);
  await create(submit);
  assert.equal(calls, 1);
  release(Error("offline"));
  await first;
  assert.equal(notices.length, 1);
  state.fetch = async () => ({ ok: true, json: async () => ({ id: "project" }) });
  await create(submit);
  assert.deepEqual(opened, ["project"]);
});

test("a lost file-picker response preserves the project; explicit empty selection cleans it up", async () => {
  const methods = [], opened = [];
  const state = { API: "/api", quickUploading: false, setQuickUploading: noop, setNotice: noop,
    refresh: noop, createBlankProject: async () => ({ id: "project" }), openProject: id => opened.push(id),
    fetch: async (_, init) => { methods.push(init.method); throw Error("lost reply"); } };
  const pick = action("quickPickLocalFiles", state);
  await pick();
  assert.deepEqual(methods, ["POST"]);
  assert.deepEqual(opened, ["project"]);
  state.fetch = async (_, init) => { methods.push(init.method); return { ok: true, json: async () => [] }; };
  await pick();
  assert.deepEqual(methods, ["POST", "POST", "DELETE"]);
});

test("finishing an upload keeps files selected while it was being probed", async () => {
  const oldFile = new File(["one"], "one.wav"), newFile = new File(["two"], "two.wav");
  let release, selection = [oldFile];
  const state = { API: "/api", activeProjectId: "first", files: selection, uploading: false, setUploading: noop, FormData,
    setFiles: update => { selection = typeof update === "function" ? update(selection) : update; },
    setNotice: noop, refresh: noop, fetch: () => new Promise(resolve => { release = resolve; }) };
  const uploading = action("uploadAndProbe", state)(submit);
  selection = [oldFile, newFile];
  release({ ok: true });
  await uploading;
  assert.deepEqual(selection, [newFile]);
});

test("an active job outside the open project goes through normal cancellation confirmation", async () => {
  const called = [];
  const job = { id: "elsewhere", status: "processing", options: { _rerun_mode: "diarization" } };
  const state = { API: "/api", jobs: [], fetch: async (url, init) => {
    assert.equal(url, "/api/jobs/elsewhere");
    assert.equal(init.method, undefined);
    return { ok: true, json: async () => job };
  }, setNotice: noop, cancelJob: async value => { called.push(value); } };
  await action("cancelQueueItem", state)({ id: job.id, kind: "job", status: "processing" });
  assert.deepEqual(called, [job]);
});
