import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const session = new Map();
const modules = {};
for (const name of ["dictation-text", "dictation-reload"]) {
  const source = await readFile(new URL(`../app/${name}.ts`, import.meta.url), "utf8");
  const exports = {};
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText, { exports, require: id => modules[id], sessionStorage: {
    setItem: (k, v) => session.set(k, v), getItem: k => session.get(k), removeItem: k => session.delete(k),
  } });
  modules["./" + name] = exports;
}
const { stashDictation, takeDictation } = modules["./dictation-reload"];
const fragment = { id: "one", text: "два" };
const original = "Один три\n\nдва";

test("reload restores a pending cursor insertion or cut without duplicating audio completed later", () => {
  for (const edit of ["Один два три", ""]) {
    stashDictation("draft", edit, original, ["one"]);
    assert.equal(takeDictation("draft", original, [fragment]), edit);
    assert.equal(takeDictation("draft", original, [fragment]), null);
    stashDictation("draft", edit, original, ["one"]);
    assert.equal(takeDictation("draft", original + "\n\nСледом", [fragment, {id:"two", text:"Следом"}]),
      edit ? edit + "\n\nСледом" : "Следом");
  }
});

test("reload never replaces a different draft, a server edit or a removed recording", () => {
  for (const [id, text, fragments] of [
    ["other", original, [fragment]], ["draft", "Другая правка", [fragment]], ["draft", original, []],
  ]) {
    stashDictation("draft", "Один два три", original, ["one"]);
    assert.equal(takeDictation(id, text, fragments), null);
  }
});
