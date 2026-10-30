import prompts from "./transcript-prompts.json";

export const transcriptPrompts = prompts;

export function promptWithTranscript(prompt: string, transcript: string): string {
  return `${prompt}\n\nТранскрибация:\n${transcript}`;
}
