import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
const source = await readFile(new URL("../app/inline-edits.ts", import.meta.url), "utf8");
const { InlineEdits } = await import(`data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(source)).toString("base64")}`);

test("rapid edits to the same entity serialize and preserve the newest snapshot", async () => {
  const edits = new InlineEdits(); const values = []; let resume;
  const gate = new Promise(resolve => { resume = resolve; });
  const write = async body => { values.push(body.name); if (values.length === 1) await gate; return body; };
  const first = edits.save("project:A", { name: "Первое" }, write);
  await Promise.resolve();
  const second = edits.save("project:A", { name: "Последнее" }, write);
  resume(); await Promise.all([first, second]);
  assert.deepEqual(values, ["Первое", "Последнее"]);
  assert.equal(edits.draft("project:A"), undefined);
});
test("failed edits remain recoverable and cannot appear in another entity", async () => {
  const edits = new InlineEdits();
  await assert.rejects(edits.save("folder:A", { name: "Не потерять" }, async () => { throw Error("offline"); }));
  assert.deepEqual(edits.draft("folder:A"), { name: "Не потерять" });
  assert.equal(edits.draft("folder:B"), undefined);
  await edits.save("folder:A", { name: "Не потерять" }, async value => value);
  assert.equal(edits.draft("folder:A"), undefined);
});
test("independent editors never send their values to each other's writer", async () => {
  const edits = new InlineEdits(); const written = [];
  await Promise.all([
    edits.save("history:A", { text: "История" }, async body => written.push(["history", body])),
    edits.save("project:B", { name: "Проект", folder_id: null }, async body => written.push(["project", body])),
  ]);
  assert.deepEqual(written, [["history", { text: "История" }], ["project", { name: "Проект", folder_id: null }]]);
});
