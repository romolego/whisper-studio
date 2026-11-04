export function insertDictationText(
  current: string,
  fragments: string[],
  mode: "end" | "cursor",
  start = current.length,
  end = start,
) {
  if (mode === "end") {
    const text = fragments.reduce(
      (value, fragment) => value.trim() ? `${value.trimEnd()}\n\n${fragment}` : fragment,
      current,
    );
    return { text, caret: text.length };
  }
  const from = Math.max(0, Math.min(current.length, start));
  const to = Math.max(from, Math.min(current.length, end));
  const before = current.slice(0, from);
  const after = current.slice(to);
  const spoken = fragments.join("\n\n");
  const left = before && !/[\s([{«“]$/u.test(before) ? " " : "";
  const right = after && !/^[\s.,!?;:)\]}»”]/u.test(after) ? " " : "";
  const inserted = left + spoken + right;
  return { text: before + inserted + after, caret: before.length + inserted.length };
}
