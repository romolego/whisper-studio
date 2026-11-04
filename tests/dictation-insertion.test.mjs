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
