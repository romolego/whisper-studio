import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = await readFile(new URL("../app/dictation-text.ts", import.meta.url), "utf8");
const script = ts.transpileModule(source.replace("export function", "function"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText;
const insert = vm.runInNewContext(script + "\ninsertDictationText");

test("end mode appends in recording order regardless of caret or selection", () => {
  const result = insert("Ручной текст  ", ["Первая запись.", "Вторая запись."], "end", 0, 5);
  assert.equal(result.text, "Ручной текст\n\nПервая запись.\n\nВторая запись.");
  assert.equal(result.caret, result.text.length);
});

test("cursor mode inserts between words and replaces only the selection", () => {
  assert.equal(insert("Первое последнее.", ["среднее"], "cursor", 6).text, "Первое среднее последнее.");
  assert.equal(insert("Первое старое последнее.", ["новое"], "cursor", 7, 13).text, "Первое новое последнее.");
  assert.equal(insert("Текст.", ["добавка"], "cursor", 5).text, "Текст добавка.");
});

test("cursor mode preserves line breaks, punctuation and UTF-16 selection positions", () => {
  const current = "🙂 (слово)\n\nКонец.";
  const result = insert(current, ["ещё"], "cursor", 4, 9);
  assert.equal(result.text, "🙂 (ещё)\n\nКонец.");
  assert.equal(result.caret, 7);
  assert.equal(insert("", ["Первый", "Второй"], "cursor", 0).text, "Первый\n\nВторой");
});

test("a fragment matching the selection still moves the caret for the next fragment", async () => {
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  const editorLayout = [...page.matchAll(/useBrowserLayoutEffect\(\(\) => \{[\s\S]*?\}, \[[^\]]*\]\);/g)]
    .find(match => match[0].includes("field.setSelectionRange"));
  assert.ok(editorLayout, "Missing editor layout effect");
  const effect = ts.transpileModule(editorLayout[0], {}).outputText;
  let start = 7, end = 10, lastDeps;
  const field = { setSelectionRange(from, to) { start = from; end = to; } };
  const context = {
    dictationText: "Привет мир", dictationMergedIds: new Set(),
    dictationTextareaRef: { current: field }, dictationInsertionRef: { current: null },
    useBrowserLayoutEffect(callback, deps) {
      if (!lastDeps || deps.some((dep, i) => !Object.is(dep, lastDeps[i]))) callback();
      lastDeps = deps;
    },
  };
  vm.runInNewContext(effect, context);
  for (const fragment of ["мир", "снова"]) {
    const result = insert(context.dictationText, [fragment], "cursor", start, end);
    context.dictationText = result.text;
    context.dictationInsertionRef.current = { caret: result.caret };
    context.dictationMergedIds = new Set([...context.dictationMergedIds, fragment]);
    vm.runInNewContext(effect, context);
  }
  assert.equal(context.dictationText, "Привет мир снова");
  assert.deepEqual([start, end], [16, 16]);
});
