import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
const page = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function action(name, state) {
  let declaration;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) declaration = node;
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.ok(declaration, `Missing action ${name}`);
  const script = ts.transpileModule(declaration.getText(page), {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return vm.runInNewContext(`${script}\n${name}`, state);
}
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

test("confirming a dictation never clears text typed into the next draft during refresh", async () => {
  const gate = deferred(), started = deferred();
  let text = "Confirmed text", dirty = true;
  let messages = [{ id: "old", status: "draft", text }];
  const state = {
    API: "/api", loadedDictationRef: { current: "old" },
    fetch: async url => new Response(JSON.stringify(url === "/api/dictations" ? { id: "next" } : {})),
    setDictations: update => { messages = update(messages); },
    setActiveDictationId: () => {}, setDictationMergedIds: () => {}, setNotice: () => {},
    setDictationText: value => { text = value; }, setDictationDirty: value => { dirty = value; },
    refresh: async () => { started.resolve(); await gate.promise; },
  };
  const confirm = action("confirmDictationMessage", state);
  const confirming = confirm({ id: "old" }, text);
  await started.promise;
  assert.equal(text, "");
  assert.equal(state.loadedDictationRef.current, "next");
  assert.ok(messages.some(message => message.id === "next"));
  text = "New draft typed during the request"; dirty = true;
  gate.resolve();
  assert.equal(await confirming, true);
  assert.equal(text, "New draft typed during the request");
  assert.equal(dirty, true);
});

test("copy-save acknowledges unchanged text but leaves another message's draft dirty", async () => {
  for (const selectedId of ["draft", "other"]) {
    let dirty = true;
    const state = {
      API: "/api", currentDictation: { id: "draft" }, dictationText: "Same text",
      dictationTextRef: { current: "Same text" }, loadedDictationRef: { current: selectedId },
      fetch: async () => new Response("{}"), setDictationDirty: value => { dirty = value; }, setNotice: () => {},
    };
    assert.equal(await action("saveCurrentDictationText", state)(), true);
    assert.equal(dirty, selectedId !== "draft");
  }
});

test("copy-save acknowledgement cannot mark newer dictation edits as saved", async () => {
  const gate = deferred();
  let dirty = true;
  const state = {
    API: "/api", currentDictation: { id: "draft" }, dictationText: "Copied version",
    dictationTextRef: { current: "Copied version" }, loadedDictationRef: { current: "draft" },
    fetch: () => gate.promise, setDictationDirty: value => { dirty = value; }, setNotice: () => {},
  };
  const saving = action("saveCurrentDictationText", state)();
  state.dictationTextRef.current = "Newer unsaved edit";
  gate.resolve(new Response("{}"));
  assert.equal(await saving, true);
  assert.equal(dirty, true);
});

test("history copy uses the visible edit, including unsaved and empty drafts", async () => {
  let callback;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(page) === "copyDictationText"
      && node.arguments[1]?.kind === ts.SyntaxKind.FalseKeyword) callback = node.parent;
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.ok(callback && ts.isArrowFunction(callback));
  const script = ts.transpileModule(`(${callback.getText(page)})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  for (const [editingHistoryId, historyTextDraft, expected] of [
    ["history", " Unsaved\nedit ", " Unsaved\nedit "],
    ["history", "", ""],
    ["other", "Other message", "Saved history"],
  ]) {
    let copied;
    const copy = vm.runInNewContext(script, {
      message: { id: "history", text: "Saved history" }, editingHistoryId, historyTextDraft,
      copyDictationText: (value, saveCurrent) => { assert.equal(saveCurrent, false); copied = value; },
    });
    await copy();
    assert.equal(copied, expected);
  }
});
