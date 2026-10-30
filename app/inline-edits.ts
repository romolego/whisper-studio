// Serialize each entity's saves; retain failed drafts instead of clearing them.
export class InlineEdits {
  private entries = new Map<string, { body: Record<string, unknown>; running?: Promise<unknown> }>();
  draft(key: string): Record<string, unknown> | undefined {
    const active = this.entries.get(key);
    if (active) return active.body;
    try {
      const value = JSON.parse(localStorage.getItem(`whisper-inline:${key}`) || "null");
      return value && typeof value === "object" && !Array.isArray(value) ? value : undefined;
    } catch { return undefined; }
  }
  async save(key: string, body: Record<string, unknown>, write: (body: Record<string, unknown>) => Promise<unknown>) {
    let entry = this.entries.get(key);
    if (!entry) { entry = { body }; this.entries.set(key, entry); }
    entry.body = body;
    try { localStorage.setItem(`whisper-inline:${key}`, JSON.stringify(body)); } catch { /* Server save still runs. */ }
    if (entry.running) return entry.running;
    const current = entry;
    const task = Promise.resolve().then(async () => {
      let result: unknown;
      while (true) {
        const snapshot = current.body;
        result = await write(snapshot);
        if (current.body !== snapshot && JSON.stringify(current.body) !== JSON.stringify(snapshot)) continue;
        this.entries.delete(key);
        try { localStorage.removeItem(`whisper-inline:${key}`); } catch { /* Save is acknowledged. */ }
        return result;
      }
    }).finally(() => { current.running = undefined; });
    current.running = task;
    return task;
  }
}
