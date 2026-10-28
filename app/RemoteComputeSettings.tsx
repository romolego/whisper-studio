"use client";

import { useEffect, useState } from "react";

const API = "http://127.0.0.1:8765/api/remote";
type State = { mode: "local" | "remote"; configured: boolean; url: string;
  host: { running: boolean; url: string } };
type Capabilities = { cuda_available: boolean; device_preference: string;
  models: Record<string, boolean>; engines: Record<string, { available: boolean; device: string }> };

export default function RemoteComputeSettings({ onChange }: { onChange: () => void }) {
  const [state, setState] = useState<State | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);

  useEffect(() => {
    let alive = true;
    fetch(`${API}/client`).then(async (response) => {
      if (!response.ok) throw new Error("Не удалось прочитать настройки вычислений");
      const value = await response.json() as State;
      if (alive) setState(value);
    }).catch((error: Error) => { if (alive) setNotice(error.message); });
    return () => { alive = false; };
  }, []);

  async function action(kind: "check" | "remote" | "local") {
    setBusy(true);
    setNotice("");
    setCapabilities(null);
    try {
      const response = await fetch(`${API}/client${kind === "check" ? "/check" : ""}`, {
        method: kind === "check" ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: kind === "local" ? "local" : "remote", code: code.trim() || null }),
      });
      const value = await response.json();
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "Не удалось подключиться");
      if (kind === "check") {
        setCapabilities(value as Capabilities);
        setNotice("Соединение работает. Модели проверяются настоящим распознаванием записи.");
      } else {
        setState(value as State);
        setCode("");
        setNotice(kind === "local" ? "Выбраны локальные вычисления" : "Выбран удалённый компьютер. Подключение сохранено.");
        onChange();
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось подключиться");
    } finally {
      setBusy(false);
    }
  }

  return <section className="settings-card remote-compute-card">
    <div className="settings-card-title"><span>GPU</span><div><h2>Вычисления</h2></div></div>
    <p>Сейчас: <strong>{state?.mode === "remote" ? "удалённый компьютер" : "этот компьютер"}</strong>
      {state?.mode === "remote" && <> · {state.url}</>}</p>
    <p className="settings-note">На ноутбуке готовятся аудио и файлы результата. Whisper и разметка спикеров
      выполняются на выбранном компьютере. Выбор действует для задач, которые ещё не начали обработку.</p>
    <label className="remote-code-label" htmlFor="remote-connection-code">Код подключения с основного компьютера</label>
    <input id="remote-connection-code" type="password" autoComplete="off" spellCheck={false}
      placeholder={state?.configured ? "Подключение сохранено; новый код нужен только для замены" : "Вставьте код ws1.…"}
      value={code} onChange={(event) => setCode(event.target.value)} />
    <div className="remote-compute-actions">
      <button className="secondary-button" type="button" disabled={busy || (!code.trim() && !state?.configured)} onClick={() => action("check")}>
        Проверить подключение</button>
      <button className="primary-button" type="button" disabled={busy || !state || state.host.running || (!code.trim() && !state.configured)}
        onClick={() => action("remote")}>Использовать удалённый компьютер</button>
      <button className="secondary-button" type="button" disabled={busy || !state || state.mode === "local"} onClick={() => action("local")}>
        Использовать этот компьютер</button>
    </div>
    {capabilities && <p>Whisper: {capabilities.device_preference === "cpu" ? "CPU" : capabilities.cuda_available ? "CUDA доступна" : "CPU"}
      {" · "}Русская модель: {capabilities.models.ru ? "установлена" : "не установлена"}
      {" · "}Nemotron: {capabilities.engines.nemotron?.available ? capabilities.engines.nemotron.device.toUpperCase() : "не готов"}</p>}
    {notice && <p role="status">{notice}</p>}
    <p className="settings-note">На мощном компьютере запустите «Запустить удалённый GPU.bat».
      Для работы через интернет подключите оба устройства к одной сети Tailscale.
      Закройте окно адаптера, чтобы выключить удалённый доступ. Обычный запуск Whisper его не включает.</p>
    {state?.host.running && <p role="status">Адаптер включён: {state.host.url}</p>}
  </section>;
}
