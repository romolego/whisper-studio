import prompts from "./transcript-prompts.json";

export const transcriptPrompts = prompts;

export function promptWithTranscript(prompt: string, transcript: string): string {
  return `${prompt}\n\nТранскрибация:\n${transcript}`;
}

export function diagramWithMaterials(prompt: string, transcript: string, summary: string, minutes: string, extra: { label: string; text: string }[] = []): string {
  const blocks = [prompt];
  if (summary.trim()) blocks.push(`Резюме:\n${summary}`);
  if (minutes.trim()) blocks.push(`Цифровой протокол:\n${minutes}`);
  for (const item of extra) if (item.text.trim()) blocks.push(`${item.label}:\n${item.text}`);
  blocks.push(`Исходная транскрибация:\n${transcript}`);
  return blocks.join("\n\n---\n\n");
}
