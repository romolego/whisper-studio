import { insertDictationText } from "./dictation-text";

// Только перед уходом со страницы: короткий промежуток между правкой и её PATCH.
const key = "whisper:dictation-reload";

export function stashDictation(id: string, text: string, serverText: string, merged: string[]) {
  try { sessionStorage.setItem(key, JSON.stringify({ id, text, serverText, merged })); } catch { /* storage unavailable */ }
}

export function takeDictation(id: string, serverText: string, ready: { id: string; text: string }[]): string | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    sessionStorage.removeItem(key);
    const draft = JSON.parse(raw);
    if (draft.id !== id || typeof draft.text !== "string" || typeof draft.serverText !== "string" || !Array.isArray(draft.merged)) return null;
    const known = new Set(draft.merged);
    const available = new Set(ready.map(item => item.id));
    if (draft.merged.some((item: string) => !available.has(item))) return null;
    const extra = ready.filter(item => !known.has(item.id)).map(item => item.text);
    const resumed = insertDictationText(draft.text, extra, "end").text;
    // Не подменять правки другого клиента. После закрытия вкладки сервер мог
    // только принять наш PATCH или дописать расшифровку следующей записи.
    return serverText === draft.serverText || serverText === resumed ||
      serverText === insertDictationText(draft.serverText, extra, "end").text ? resumed : null;
  } catch { return null; }
}
