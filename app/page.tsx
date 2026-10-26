"use client";

import {
  ChangeEvent,
  DragEvent,
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";


// На сервере useLayoutEffect не выполняется и предупреждает в консоли: при
// пререндере страницы поля ещё нет, восстанавливать позицию курсора нечему.
const useBrowserLayoutEffect =
  typeof window === "undefined" ? useEffect : useLayoutEffect;

const API = "http://127.0.0.1:8765/api";
const exportKinds = ["docx", "md", "txt", "srt", "vtt", "json"] as const;

type ExportKind = (typeof exportKinds)[number];
type Screen = "projects" | "project" | "dictation" | "settings";
type Theme = "light" | "dark" | "auto";

// Размеры страницы для ленты и истории диктовок. «Все» остаётся ради
// небольших библиотек, где страницы только мешают.
const pageSizes = ["10", "20", "50", "100", "500", "1000", "all"] as const;
type PageSize = (typeof pageSizes)[number];

function pageSizeLabel(value: PageSize) {
  return value === "all" ? "Все" : value;
}

// null означает «без разбиения»: запрос уходит без per_page и возвращает всё.
function pageSizeCount(value: PageSize) {
  return value === "all" ? null : Number(value);
}

type Settings = {
  default_language: "ru" | "en" | "auto";
  default_quality: "fast" | "accurate";
  default_timestamps: "none" | "segments" | "words";
  default_formatting: "formatted" | "plain";
  default_export: ExportKind;
  default_view_timestamps: "none" | "segments" | "words";
  default_view_formatting: "formatted" | "plain";
  default_view_speakers: "off" | "on";
  default_diarization: "off" | "on";
  dictation_default_language: "ru" | "en" | "auto";
  dictation_default_quality: "fast" | "accurate";
  project_name_format:
    | "date_title"
    | "date_compact"
    | "date_dotted"
    | "date_time_title"
    | "title_date"
    | "title_only";
  project_view: "flat" | "folders";
  default_import_mode: "copy" | "reference";
  projects_page_size: PageSize;
  dictation_page_size: PageSize;
  keep_extracted_audio: "off" | "on";
  download_quality: "audio" | "360" | "480" | "720" | "1080" | "max";
  download_transcribe: "on" | "off";
  integration_feed: "show" | "hide";
  telegram_autostart: "on" | "off";
  youtube_cookies:
    | "off"
    | "auto"
    | "firefox"
    | "chrome"
    | "edge"
    | "opera"
    | "brave"
    | "vivaldi"
    | "chromium";
  diarization_device: "auto" | "cuda" | "cpu";
  diarization_engine: DiarizationEngine;
  theme: Theme;
  managed_storage_root: string;
  temporary_storage_root: string;
  // Только для показа: меняться не могут, приходят с сервера, чтобы в
  // интерфейсе не было захардкоженных путей.
  dictations_root: string;
  database_path: string;
  cookies_root: string;
};

// Чем размечать спикеров. Pyannote — прежний движок, Nemotron 3 — модель
// NVIDIA: до восьми голосов, перебивания, втрое меньше видеопамяти.
const diarizationEngines = ["nemotron", "pyannote"] as const;
type DiarizationEngine = (typeof diarizationEngines)[number];
const diarizationEngineLabels: Record<DiarizationEngine, string> = {
  pyannote: "Pyannote",
  nemotron: "Nemotron 3",
};

type Project = {
  // Внешний сервис, создавший проект. Пусто — проект свой, из интерфейса.
  integration?: string | null;
  integration_title?: string | null;
  id: string;
  name: string;
  folder_id: string | null;
  folder_name: string | null;
  job_count: number;
  completed_count: number;
  queued_count: number;
  processing_count: number;
  failed_count: number;
  cancelled_count: number;
  duration_seconds: number;
  created_at: string;
  updated_at: string;
  download_status?: UrlDownloadStatus | null;
  download_percent?: number | null;
  download_error?: string | null;
};

type Folder = {
  // Папка внешнего сервиса: её нельзя удалить, сервис создаст её заново.
  integration?: string | null;
  id: string;
  name: string;
  project_count: number;
  // Показывать ли проекты папки в общей ленте. Сама папка из навигации не
  // исчезает: снятая галочка — это архив, а не удалённый раздел.
  feed_visible?: boolean;
};

type ProjectCounts = {
  feed: number;
  loose: number;
  total: number;
};

type JobOptions = {
  language: Settings["default_language"];
  quality: Settings["default_quality"];
  timestamps: Settings["default_timestamps"];
  formatting: Settings["default_formatting"];
  audio_stream_index?: number;
  audio_track?: AudioStream;
  diarization?: boolean;
  diarization_engine?: DiarizationEngine;
  trim_start_seconds?: number;
  trim_end_seconds?: number | null;
  _rerun_mode?: "transcribe" | "diarization";
};

type DiarizationEngineStatus = {
  engine: DiarizationEngine;
  title: string;
  model_name: string;
  package_installed: boolean;
  model_ready: boolean;
  available: boolean;
  runtime?: string;
  device?: "cuda" | "cpu" | "unknown";
  cuda_available?: boolean;
  // Только у Nemotron: его модель скачивается кнопкой, без токена.
  download?: {
    status: "idle" | "running" | "done" | "error";
    message?: string;
    downloaded_bytes?: number;
  };
  download_total_bytes?: number;
};

// Ключи верхнего уровня описывают Pyannote — как до появления второго
// движка. Оба движка целиком лежат в engines.
type DiarizationStatus = {
  package_installed: boolean;
  token_saved: boolean;
  model_ready: boolean;
  available: boolean;
  model_name: string;
  runtime?: string;
  device?: "cuda" | "cpu" | "unknown";
  configured_device?: "auto" | "cuda" | "cpu";
  cuda_available?: boolean;
  any_available?: boolean;
  default_engine?: DiarizationEngine;
  engines?: Record<DiarizationEngine, DiarizationEngineStatus>;
};

function engineReady(
  status: DiarizationStatus | null,
  engine: DiarizationEngine,
) {
  if (status?.engines) return Boolean(status.engines[engine]?.available);
  return engine === "pyannote" && Boolean(status?.available);
}

function anyEngineReady(status: DiarizationStatus | null) {
  return diarizationEngines.some((engine) => engineReady(status, engine));
}

/** Один компонент сервиса: модель, программа или пакет Python. */
type ServiceComponent = {
  key: string;
  title: string;
  group: "model" | "runtime" | "python";
  // required=false означает, что без компонента сервис работает, но чего-то не
  // умеет. Именно поэтому его отсутствие не делает сервис «неготовым».
  required: boolean;
  present: boolean;
  detail: string;
  path: string;
  source: string;
  // Что перестанет работать без этого компонента.
  impact: string;
  // service — сервис умеет поставить сам; manual — только руками, командой.
  install: "none" | "service" | "manual";
  command: string;
  size_bytes: number;
  installed_at: string | null;
};

type ConnectedService = {
  key: string;
  title: string;
  header: string;
  projects: number;
  files: number;
  last_used_at: string | null;
};

type ComponentsInventory = {
  items: ServiceComponent[];
  ready: boolean;
  missing_required: string[];
  missing_optional: string[];
  total_size_bytes: number;
  project_root: string;
  database_path: string;
};

type ModelDownload = {
  status: "idle" | "running" | "done" | "error";
  message?: string;
  targets: { key: string; title: string; total: number; downloaded: number }[];
  downloaded_bytes?: number;
  total_bytes?: number;
  speed_bytes?: number;
  eta_seconds?: number | null;
  percent?: number | null;
  error?: string;
};

type AudioStream = {
  index: number;
  codec: string;
  channels: number;
  channel_layout: string;
  sample_rate: number;
  bit_rate: number;
  language: string;
  title: string;
};

type TrimRange = {
  start: number;
  end: number;
};

type TrackWaveform = {
  stream_index: number;
  duration_seconds: number;
  peaks: number[];
};

type StagedMedia = {
  id: string;
  session_id: string;
  original_name: string;
  size_bytes: number;
  duration_seconds: number;
  format_name: string;
  video_stream_count: number;
  audio_streams: AudioStream[];
  stored_path: string;
  source_path: string | null;
  storage_mode: "managed" | "reference";
};

type Job = {
  id: string;
  session_id: string;
  original_name: string;
  status: "queued" | "processing" | "completed" | "failed" | "cancelled";
  progress: number;
  position: number;
  queue_position: number | null;
  cancel_requested?: boolean;
  options: JobOptions;
  detected_language: string | null;
  duration_seconds: number | null;
  processing_seconds: number | null;
  // На чём считал Whisper. cpu означает откат с видеокарты и примерно
  // двадцатикратное замедление, поэтому это надо показывать, а не прятать.
  device?: "cuda" | "cpu" | null;
  error: string | null;
  created_at: string;
  outputs: Record<string, string>;
  stored_path: string;
  source_path: string | null;
  storage_mode: "managed" | "reference";
  // Путь к сохранённой аудиодорожке. null означает, что аудио не сохранялось:
  // либо настройка выключена, либо дорожку не извлекали вовсе.
  extracted_audio_path?: string | null;
};

type UrlDownloadStatus =
  | "idle"
  | "queued"
  | "extracting"
  | "downloading"
  | "reconnecting"
  | "resuming"
  | "finalizing"
  | "pausing"
  | "paused"
  | "stopping"
  | "completed"
  | "cancelled"
  | "interrupted"
  | "error";

type UrlQuality = {
  quality: string;
  label: string;
  height: number | null;
  ext: string;
  filesize_bytes: number | null;
};

type UrlMetadata = {
  provider: string;
  provider_label: string;
  title: string;
  uploader: string | null;
  duration_sec: number | null;
  filesize_bytes: number | null;
  auto_filesize_bytes: number | null;
  webpage_url: string;
  qualities: UrlQuality[];
};

type UrlDownload = {
  project_id: string;
  status: UrlDownloadStatus;
  url: string;
  provider: string;
  provider_label: string;
  quality: string;
  title: string;
  uploader: string | null;
  duration_sec: number | null;
  started_at: number | null;
  finished_at: number | null;
  percent: number | null;
  downloaded_bytes: number;
  total_bytes: number | null;
  speed_bps: number | null;
  eta_sec: number | null;
  filename: string | null;
  error: string | null;
  attempts: number;
  resumable: boolean;
  transcription_job_id: string | null;
};

type SpeakerEntry = {
  key: string;
  job_id: string;
  speaker_id: string;
  source: string;
  default_name: string;
  custom_name: string;
  name: string;
  segment_count: number;
  duration_seconds: number;
};

type DictationFragment = {
  id: string;
  message_id: string;
  position: number;
  audio_path: string;
  mime_type: string;
  duration_seconds: number;
  status: "queued" | "processing" | "completed" | "failed" | "cancelled";
  cancel_requested?: boolean;
  transcript: string;
  language: "auto" | "ru" | "en";
  quality: "fast" | "accurate";
  detected_language: string | null;
  processing_seconds: number | null;
  error: string | null;
  created_at: string;
};

type DictationMessage = {
  id: string;
  text: string;
  status: "draft" | "confirmed";
  created_at: string;
  updated_at: string;
  confirmed_at: string | null;
  // Проект, в который диктовку сохранили «на будущее». null — не сохраняли.
  project_id?: string | null;
  fragments: DictationFragment[];
};

type QueueItem = {
  id: string;
  kind: "job" | "dictation";
  operation: string;
  title: string;
  project_id: string | null;
  project_name: string;
  status: "queued" | "processing";
  position: number | null;
  priority: boolean;
  cancel_requested: boolean;
};

type QueueOverview = {
  active: QueueItem | null;
  waiting: QueueItem[];
  total: number;
  // Оба рабочих потока живы. Раньше это поле было только в /api/health,
  // который интерфейс не запрашивал, и вставшая очередь ничем не отличалась
  // от занятой.
  worker_alive?: boolean;
};

type PendingRecording = {
  id: string;
  messageId: string;
  blob: Blob;
  durationSeconds: number;
  language: "auto" | "ru" | "en";
  quality: "fast" | "accurate";
  createdAt: string;
};

type RecordingStopMode = "save" | "discard" | "confirm";

const initialSettings: Settings = {
  default_language: "ru",
  default_quality: "accurate",
  default_timestamps: "segments",
  default_formatting: "formatted",
  default_export: "docx",
  default_view_timestamps: "none",
  default_view_formatting: "formatted",
  default_view_speakers: "off",
  default_diarization: "off",
  dictation_default_language: "ru",
  dictation_default_quality: "fast",
  project_name_format: "date_title",
  project_view: "folders",
  default_import_mode: "copy",
  projects_page_size: "50",
  dictation_page_size: "20",
  keep_extracted_audio: "off",
  download_quality: "audio",
  download_transcribe: "on",
  integration_feed: "hide",
  telegram_autostart: "on",
  youtube_cookies: "off",
  diarization_device: "auto",
  diarization_engine: "nemotron",
  theme: "light",
  // Пустые строки, а не «E:\whisper\...»: настоящие пути приходят с сервера
  // первым же запросом, а зашитый здесь путь был бы неверным на любой другой
  // машине и вводил бы в заблуждение в первую секунду загрузки.
  managed_storage_root: "",
  temporary_storage_root: "",
  dictations_root: "",
  database_path: "",
  cookies_root: "",
};

const emptyQueueOverview: QueueOverview = {
  active: null,
  waiting: [],
  total: 0,
  worker_alive: true,
};

const languageLabels = { ru: "Русский", en: "Английский", auto: "Авто" };
const statusLabels = {
  queued: "В очереди",
  processing: "Распознаётся",
  completed: "Готово",
  failed: "Ошибка",
  cancelled: "Остановлено",
};
const activeUrlStatuses: UrlDownloadStatus[] = [
  "queued",
  "extracting",
  "downloading",
  "reconnecting",
  "resuming",
  "finalizing",
  "pausing",
  "stopping",
];

function urlDownloadStatusLabel(status: UrlDownloadStatus) {
  const labels: Record<UrlDownloadStatus, string> = {
    idle: "Ожидание",
    queued: "Подготовка",
    extracting: "Получаю данные",
    downloading: "Скачивание",
    reconnecting: "Восстанавливаю соединение",
    resuming: "Продолжаю скачивание",
    finalizing: "Готовлю транскрибацию",
    pausing: "Ставлю на паузу",
    paused: "На паузе",
    stopping: "Останавливаю",
    completed: "Передано в транскрибацию",
    cancelled: "Остановлено",
    interrupted: "Загрузка прервана",
    error: "Ошибка",
  };
  return labels[status];
}

function shortDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatDuration(value: number | null | undefined) {
  if (!value) return "—";
  const seconds = Math.round(value);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours) return `${hours} ч ${minutes} мин`;
  if (minutes) return `${minutes} мин ${rest} с`;
  return `${rest} с`;
}

function formatRecorderTime(value: number) {
  const seconds = Math.max(0, Math.floor(value));
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function fileSize(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

function textWithoutFragment(text: string, transcript: string) {
  /* Убрать вклад одного фрагмента из текста диктовки.
   *
   * Зеркало серверного _text_without_fragment: вырезается вхождение, а не
   * пересобирается весь текст, иначе правки рядом были бы потеряны. Если
   * расшифровку уже поправили руками и вхождение не находится, текст остаётся
   * как есть — лишнее убирается вручную, и это честнее, чем стереть остальное. */
  const clean = transcript.trim();
  if (!clean) return text;
  const position = text.indexOf(clean);
  if (position < 0) return text;
  const remainder = text.slice(0, position) + text.slice(position + clean.length);
  return remainder.replace(/\n{3,}/g, "\n\n").trim();
}

function projectStatus(project: Project) {
  if (
    project.download_status &&
    [
      "queued",
      "extracting",
      "downloading",
      "reconnecting",
      "resuming",
      "finalizing",
      "pausing",
      "stopping",
    ].includes(project.download_status)
  ) {
    return project.download_status === "pausing"
      ? "Ставится на паузу"
      : project.download_status === "stopping"
        ? "Останавливается"
        : `Скачивается${project.download_percent != null ? ` · ${Math.round(project.download_percent)}%` : ""}`;
  }
  if (project.download_status === "paused") return "Скачивание на паузе";
  if (project.download_status === "error") return "Ошибка скачивания";
  if (project.processing_count) return "Распознаётся";
  if (project.queued_count) return "Ждёт очереди";
  if (project.failed_count) return "Есть ошибки";
  if (project.job_count && project.completed_count === project.job_count) {
    return "Готов";
  }
  if (project.cancelled_count) return "Есть остановленные";
  return project.job_count ? "В работе" : "Пустой";
}

const NAME_FORMATS = [
  "date_title",
  "date_compact",
  "date_dotted",
  "date_time_title",
  "title_date",
  "title_only",
] as const;

/** Подпись варианта в списке — сразу на сегодняшней дате.
 *
 * Отдельная строка «Пример: …» под списком не нужна: сам вариант и есть
 * пример, а дублирующая подпись только занимала место.
 */
function nameFormatLabel(format: Settings["project_name_format"]) {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  const hours = String(now.getHours()).padStart(2, "0");
  const minutes = String(now.getMinutes()).padStart(2, "0");
  if (format === "date_title") return `${year} - ${month} - ${day} - Название`;
  if (format === "date_compact") return `${year}-${month}-${day} — Название`;
  if (format === "date_dotted") return `${day}.${month}.${year} — Название`;
  if (format === "date_time_title") {
    return `${year}-${month}-${day} ${hours}-${minutes} — Название`;
  }
  if (format === "title_date") return `Название — ${year}-${month}-${day}`;
  return "Только название";
}

/** Итоговое имя проекта для окна создания.
 *
 * Здесь предпросмотр по делу: он показывает не абстрактный пример формата, а
 * ровно то имя, которое получит создаваемый проект.
 */
function projectNamePreview(
  format: Settings["project_name_format"],
  title: string,
) {
  const clean = title.trim() || "Новый проект";
  return nameFormatLabel(format).replace("Название", clean);
}

function trackLabel(stream: AudioStream, position: number) {
  const details = [
    stream.codec.toUpperCase(),
    stream.channel_layout ||
      (stream.channels === 1 ? "mono" : `${stream.channels} каналов`),
    stream.language || "",
    stream.title || "",
  ].filter(Boolean);
  return `Дорожка ${position + 1} · ${details.join(" · ")}`;
}

function previewClock(value: number) {
  const seconds = Math.max(0, Math.round(value));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function parseClock(value: string) {
  const text = value.trim().replace(/[.,]/g, ":");
  if (!text) return null;
  const parts = text.split(":");
  if (parts.length > 3 || parts.some((part) => !/^\d*$/.test(part))) return null;
  const numbers = parts.map((part) => Number(part || 0));
  if (numbers.some((number) => !Number.isFinite(number))) return null;
  return numbers.reduce((total, number) => total * 60 + number, 0);
}

function trimSummary(range: TrimRange, duration: number) {
  const trimmed = Math.max(0, range.end - range.start);
  const cut = Math.max(0, duration - trimmed);
  if (cut < 1) return "Вся дорожка целиком";
  return `${previewClock(range.start)} — ${previewClock(range.end)} · ${formatDuration(
    trimmed,
  )} вместо ${formatDuration(duration)}`;
}

function jobTrimLabel(options: JobOptions) {
  const start = options.trim_start_seconds || 0;
  const end = options.trim_end_seconds;
  if (!start && (end === null || end === undefined)) return "";
  return `· фрагмент ${previewClock(start)}—${
    end === null || end === undefined ? "конец" : previewClock(end)
  } `;
}

/** Полосочный редактор границ: слева и справа отрезается, серое не читается. */
/** Пояснение, которое видно только по запросу.
 *
 * Раньше пояснения к каждой настройке висели подписью под названием. На экране
 * настроек их набиралось больше двадцати, и из-за этого казалось, что настроек
 * втрое больше, чем есть. Здесь тот же текст живёт за знаком вопроса и
 * появляется на наведении и по фокусу с клавиатуры.
 */
function Hint({ text }: { text: string }) {
  return (
    <span className="hint">
      <button
        type="button"
        className="hint-mark"
        aria-label={text}
        // Подсказка не должна отправлять формы и не должна перехватывать
        // клик по строке настройки.
        onClick={(event) => event.stopPropagation()}
      >
        ?
      </button>
      <span className="hint-bubble" role="tooltip">
        {text}
      </span>
    </span>
  );
}

/* Выбор разметки спикеров: не разделять или разделять выбранным движком.
 *
 * Один список вместо двух полей: «Разделить» без движка не бывает, а движок
 * без разметки ничего не значит. Недоступный движок виден, но выключен —
 * чтобы было понятно, что он есть и его можно поставить в настройках. */
function DiarizationSelect({
  enabled,
  engine,
  status,
  onChange,
  disabled = false,
  offLabel = "Не разделять",
}: {
  enabled: boolean;
  engine: DiarizationEngine;
  status: DiarizationStatus | null;
  onChange: (enabled: boolean, engine: DiarizationEngine) => void;
  disabled?: boolean;
  offLabel?: string;
}) {
  return (
    <select
      value={enabled ? engine : "off"}
      disabled={disabled}
      onChange={(event) => {
        const value = event.target.value;
        if (value === "off") onChange(false, engine);
        else onChange(true, value as DiarizationEngine);
      }}
    >
      <option value="off">{offLabel}</option>
      {diarizationEngines.map((item) => (
        <option key={item} value={item} disabled={!engineReady(status, item)}>
          Разделить · {diarizationEngineLabels[item]}
        </option>
      ))}
    </select>
  );
}

/* Пагинатор списка.
 *
 * Он не режет уже загруженный список, а сообщает наверх, какую страницу
 * запросить: с сервера приходит ровно она. Иначе на полутора тысячах
 * проектов каждое обновление ленты — а оно идёт раз в две секунды — тянуло
 * бы всю библиотеку целиком. */
function Pagination({
  page,
  pageSize,
  total,
  unit,
  onPage,
  onPageSize,
}: {
  page: number;
  pageSize: PageSize;
  total: number;
  unit: string;
  onPage: (value: number) => void;
  onPageSize: (value: PageSize) => void;
}) {
  const perPage = pageSizeCount(pageSize);
  const pages = perPage ? Math.max(1, Math.ceil(total / perPage)) : 1;
  const current = Math.min(Math.max(1, page), pages);
  const first = total === 0 ? 0 : perPage ? (current - 1) * perPage + 1 : 1;
  const last = perPage ? Math.min(total, current * perPage) : total;

  return (
    <div className="pagination">
      <label className="pagination-size">
        <span>Показывать</span>
        <select
          value={pageSize}
          aria-label={`Сколько показывать на странице (${unit})`}
          onChange={(event) => onPageSize(event.target.value as PageSize)}
        >
          {pageSizes.map((value) => (
            <option key={value} value={value}>
              {pageSizeLabel(value)}
            </option>
          ))}
        </select>
      </label>
      <span className="pagination-range">
        {total === 0
          ? `${unit}: пока нет`
          : `${first}–${last} из ${total}`}
      </span>
      {pages > 1 && (
        <div className="pagination-pages">
          <button
            type="button"
            title="В начало"
            aria-label="Первая страница"
            disabled={current <= 1}
            onClick={() => onPage(1)}
          >
            «
          </button>
          <button
            type="button"
            title="Предыдущая страница"
            aria-label="Предыдущая страница"
            disabled={current <= 1}
            onClick={() => onPage(current - 1)}
          >
            ‹
          </button>
          <b>
            {current} из {pages}
          </b>
          <button
            type="button"
            title="Следующая страница"
            aria-label="Следующая страница"
            disabled={current >= pages}
            onClick={() => onPage(current + 1)}
          >
            ›
          </button>
          <button
            type="button"
            title="В конец"
            aria-label="Последняя страница"
            disabled={current >= pages}
            onClick={() => onPage(pages)}
          >
            »
          </button>
        </div>
      )}
    </div>
  );
}

function TrackTrimmer({
  media,
  streamIndex,
  range,
  onChange,
  onOpenChange,
  onError,
}: {
  media: StagedMedia;
  streamIndex: number;
  range: TrimRange | null;
  onChange: (next: TrimRange | null) => void;
  onOpenChange: (open: boolean) => void;
  onError: (message: string) => void;
}) {
  const [waveform, setWaveform] = useState<TrackWaveform | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  // «play» — перетаскивание курсора прослушивания, остальное — границы.
  const [dragging, setDragging] = useState<"start" | "end" | "play" | null>(
    null,
  );
  // Пока поле в фокусе, показываем ровно то, что печатает пользователь;
  // в остальное время значения приходят из выбранных границ.
  const [editing, setEditing] = useState<"start" | "end" | null>(null);
  const [draft, setDraft] = useState("");
  const [barCount, setBarCount] = useState(120);
  const [playing, setPlaying] = useState(false);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // Курсор рисуется через ref, а не через состояние: перерисовывать сотню
  // столбиков шестьдесят раз в секунду ради одной вертикальной линии незачем.
  const playheadRef = useRef<HTMLDivElement | null>(null);
  const clockRef = useRef<HTMLElement | null>(null);
  const positionRef = useRef(0);

  const duration = Math.max(
    1,
    waveform?.duration_seconds || media.duration_seconds || 1,
  );
  const selection: TrimRange = range ?? { start: 0, end: duration };
  const trimmed = range !== null;

  const loadWaveform = useCallback(async () => {
    if (loading) return;
    setLoading(true);
    setFailed(false);
    try {
      const response = await fetch(
        `${API}/media/${media.id}/waveform?stream_index=${streamIndex}`,
      );
      if (!response.ok) throw new Error(await errorMessage(response));
      setWaveform((await response.json()) as TrackWaveform);
      onOpenChange(true);
    } catch (error) {
      setFailed(true);
      onError(
        error instanceof Error
          ? error.message
          : "Не удалось построить звуковую дорожку",
      );
    } finally {
      setLoading(false);
    }
  }, [loading, media.id, onError, onOpenChange, streamIndex]);

  // Столбиков ровно столько, сколько влезает в текущую ширину карточки:
  // на узком окне частокол из четырёхсот полосок сливается в кашу.
  useEffect(() => {
    const node = trackRef.current;
    if (!node) return;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      setBarCount(Math.max(40, Math.min(420, Math.floor(width / 3))));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [waveform]);

  const bars = useMemo(() => {
    const source = waveform?.peaks;
    if (!source?.length) return [] as number[];
    const count = Math.min(barCount, source.length);
    return Array.from({ length: count }, (_, index) => {
      const left = Math.floor((index * source.length) / count);
      const right = Math.max(
        left + 1,
        Math.floor(((index + 1) * source.length) / count),
      );
      let peak = 0;
      for (let position = left; position < right; position += 1) {
        peak = Math.max(peak, source[position]);
      }
      return peak;
    });
  }, [barCount, waveform]);

  // Пока звук играет, линию курсора двигает кадровый таймер: события
  // timeupdate приходят четыре раза в секунду и заметно дёргаются.
  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    const tick = () => {
      const audio = audioRef.current;
      if (audio) {
        positionRef.current = audio.currentTime;
        const percent = Math.min(100, (audio.currentTime / duration) * 100);
        if (playheadRef.current) {
          playheadRef.current.style.left = `${percent}%`;
        }
        if (clockRef.current) {
          clockRef.current.textContent = previewClock(audio.currentTime);
        }
      }
      frame = window.requestAnimationFrame(tick);
    };
    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [duration, playing]);

  // Первая отрисовка курсора и возврат его на место после сворачивания.
  useEffect(() => {
    if (!waveform) return;
    const seconds = positionRef.current;
    const percent = Math.min(100, Math.max(0, (seconds / duration) * 100));
    if (playheadRef.current) playheadRef.current.style.left = `${percent}%`;
    if (clockRef.current) clockRef.current.textContent = previewClock(seconds);
  }, [duration, waveform]);

  function paintPosition(seconds: number) {
    positionRef.current = seconds;
    const percent = Math.min(100, Math.max(0, (seconds / duration) * 100));
    if (playheadRef.current) playheadRef.current.style.left = `${percent}%`;
    if (clockRef.current) clockRef.current.textContent = previewClock(seconds);
  }

  function seekTo(seconds: number) {
    const safe = Math.min(Math.max(0, seconds), duration);
    paintPosition(safe);
    const audio = audioRef.current;
    if (audio) audio.currentTime = safe;
  }

  async function togglePlay() {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
      return;
    }
    try {
      // Курсор мог уехать, пока звук стоял на паузе.
      if (Math.abs(audio.currentTime - positionRef.current) > 0.2) {
        audio.currentTime = positionRef.current;
      }
      await audio.play();
    } catch {
      onError("Не удалось включить воспроизведение дорожки");
    }
  }

  function commit(next: TrimRange) {
    const start = Math.min(Math.max(0, next.start), duration - 1);
    const end = Math.min(Math.max(start + 1, next.end), duration);
    if (start <= 0.05 && end >= duration - 0.5) {
      onChange(null);
      return;
    }
    onChange({ start, end });
  }

  function positionFromEvent(clientX: number) {
    const box = trackRef.current?.getBoundingClientRect();
    if (!box || box.width <= 0) return 0;
    const ratio = (clientX - box.left) / box.width;
    return Math.min(1, Math.max(0, ratio)) * duration;
  }

  function moveHandle(handle: "start" | "end", seconds: number) {
    if (handle === "start") {
      commit({ start: seconds, end: selection.end });
    } else {
      commit({ start: selection.start, end: seconds });
    }
  }

  function onTrackPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (!waveform) return;
    // Клик по дорожке ставит курсор прослушивания. Границы двигаются только
    // за свои ручки — иначе невозможно просто послушать нужное место.
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging("play");
    seekTo(positionFromEvent(event.clientX));
  }

  function onHandlePointerDown(
    event: ReactPointerEvent<HTMLButtonElement>,
    handle: "start" | "end",
  ) {
    // Без preventDefault браузер начинает выделять текст соседних подписей.
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.focus();
    trackRef.current?.setPointerCapture(event.pointerId);
    setDragging(handle);
  }

  function onTrackPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragging) return;
    const seconds = positionFromEvent(event.clientX);
    if (dragging === "play") {
      seekTo(seconds);
      return;
    }
    moveHandle(dragging, seconds);
  }

  function stopDragging(event: ReactPointerEvent<HTMLDivElement>) {
    if (!dragging) return;
    if (trackRef.current?.hasPointerCapture(event.pointerId)) {
      trackRef.current.releasePointerCapture(event.pointerId);
    }
    setDragging(null);
  }

  function onHandleKeyDown(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    handle: "start" | "end",
  ) {
    const step = event.shiftKey ? 30 : 1;
    const current = handle === "start" ? selection.start : selection.end;
    if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
      event.preventDefault();
      moveHandle(handle, current - step);
    } else if (event.key === "ArrowRight" || event.key === "ArrowUp") {
      event.preventDefault();
      moveHandle(handle, current + step);
    } else if (event.key === "Home") {
      event.preventDefault();
      moveHandle(handle, 0);
    } else if (event.key === "End") {
      event.preventDefault();
      moveHandle(handle, duration);
    }
  }

  function applyDraft(handle: "start" | "end", text: string) {
    setEditing(null);
    const parsed = parseClock(text);
    if (parsed !== null) moveHandle(handle, parsed);
  }

  if (!waveform) {
    return (
      <div className="track-trim collapsed">
        <button
          type="button"
          className="trim-open"
          disabled={loading}
          onClick={loadWaveform}
        >
          {loading
            ? "Читаю звук…"
            : failed
              ? "Повторить построение дорожки"
              : "✂ Обрезать начало и конец"}
        </button>
        <small>
          {loading
            ? "Строю звуковую дорожку — на часовой записи это несколько секунд"
            : trimmed
              ? trimSummary(selection, duration)
              : "Сейчас в очередь уйдёт вся дорожка целиком"}
        </small>
      </div>
    );
  }

  const startPercent = (selection.start / duration) * 100;
  const endPercent = (selection.end / duration) * 100;

  return (
    <div className="track-trim">
      <div className="trim-heading">
        <span>✂ Границы распознавания</span>
        <button
          type="button"
          className="trim-reset"
          disabled={!trimmed}
          onClick={() => onChange(null)}
        >
          Вся дорожка
        </button>
        <button
          type="button"
          className="trim-reset"
          title="Свернуть редактор и вернуть быстрое прослушивание"
          onClick={() => {
            setPlaying(false);
            setWaveform(null);
            onOpenChange(false);
          }}
        >
          Свернуть
        </button>
      </div>

      <div
        className={`trim-track${dragging ? " dragging" : ""}`}
        ref={trackRef}
        onPointerDown={onTrackPointerDown}
        onPointerMove={onTrackPointerMove}
        onPointerUp={stopDragging}
        onPointerCancel={stopDragging}
      >
        <div className="trim-bars">
          {bars.map((peak, index) => {
            const at = ((index + 0.5) / bars.length) * duration;
            const inside = at >= selection.start && at <= selection.end;
            return (
              <i
                key={index}
                className={inside ? "" : "muted"}
                style={{ height: `${Math.max(6, peak * 100)}%` }}
              />
            );
          })}
        </div>
        <div className="trim-cut" style={{ width: `${startPercent}%` }} />
        <div
          className="trim-cut right"
          style={{ width: `${100 - endPercent}%` }}
        />
        <button
          type="button"
          className="trim-handle"
          style={{ left: `${startPercent}%` }}
          role="slider"
          aria-label={`Начало фрагмента файла ${media.original_name}`}
          aria-valuemin={0}
          aria-valuemax={Math.round(duration)}
          aria-valuenow={Math.round(selection.start)}
          aria-valuetext={previewClock(selection.start)}
          onPointerDown={(event) => onHandlePointerDown(event, "start")}
          onKeyDown={(event) => onHandleKeyDown(event, "start")}
        >
          <span className={startPercent > 80 ? "label-left" : "label-right"}>
            {previewClock(selection.start)}
          </span>
        </button>
        <button
          type="button"
          className="trim-handle end"
          style={{ left: `${endPercent}%` }}
          role="slider"
          aria-label={`Конец фрагмента файла ${media.original_name}`}
          aria-valuemin={0}
          aria-valuemax={Math.round(duration)}
          aria-valuenow={Math.round(selection.end)}
          aria-valuetext={previewClock(selection.end)}
          onPointerDown={(event) => onHandlePointerDown(event, "end")}
          onKeyDown={(event) => onHandleKeyDown(event, "end")}
        >
          <span className={endPercent < 20 ? "label-right" : "label-left"}>
            {previewClock(selection.end)}
          </span>
        </button>
        <div className="trim-playhead" ref={playheadRef} />
      </div>

      <div className="trim-player">
        <button
          type="button"
          className="trim-play"
          aria-label={
            playing
              ? `Остановить дорожку файла ${media.original_name}`
              : `Прослушать дорожку файла ${media.original_name}`
          }
          onClick={togglePlay}
        >
          {playing ? "❚❚" : "▶"}
        </button>
        {/* Текст курсора пишется в DOM напрямую, поэтому детей у <b> нет:
            иначе перерисовка формы затирала бы текущее время. */}
        <time>
          <b ref={clockRef} /> / {previewClock(duration)}
        </time>
        <button
          type="button"
          className="trim-mark"
          title="Поставить начало фрагмента там, где стоит курсор"
          onClick={() => moveHandle("start", positionRef.current)}
        >
          Начало здесь
        </button>
        <button
          type="button"
          className="trim-mark"
          title="Поставить конец фрагмента там, где стоит курсор"
          onClick={() => moveHandle("end", positionRef.current)}
        >
          Конец здесь
        </button>
        <a
          className="trim-download"
          href={
            `${API}/media/${media.id}/fragment?stream_index=${streamIndex}` +
            `&trim_start_seconds=${selection.start.toFixed(3)}` +
            `&trim_end_seconds=${selection.end.toFixed(3)}`
          }
          download
        >
          ⤓ Скачать фрагмент
        </a>
        <audio
          ref={audioRef}
          preload="metadata"
          src={`${API}/media/${media.id}/audio?stream_index=${streamIndex}`}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onError={() => {
            setPlaying(false);
            onError("Не удалось загрузить дорожку для прослушивания");
          }}
        />
      </div>

      <div className="trim-fields">
        {(["start", "end"] as const).map((handle) => (
          <label key={handle}>
            <span>{handle === "start" ? "С" : "По"}</span>
            <input
              value={
                editing === handle
                  ? draft
                  : previewClock(
                      handle === "start" ? selection.start : selection.end,
                    )
              }
              inputMode="numeric"
              placeholder="мм:сс"
              aria-label={`${
                handle === "start" ? "Начало" : "Конец"
              } фрагмента файла ${media.original_name}, минуты и секунды`}
              onFocus={() => {
                setEditing(handle);
                setDraft(
                  previewClock(
                    handle === "start" ? selection.start : selection.end,
                  ),
                );
              }}
              onChange={(event) => setDraft(event.target.value)}
              onBlur={(event) => applyDraft(handle, event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  event.currentTarget.blur();
                }
              }}
            />
          </label>
        ))}
        <small>{trimSummary(selection, duration)}</small>
      </div>
    </div>
  );
}

function trackPreviewPoints(duration: number) {
  const maximumStart = Math.max(0, duration - 30);
  const candidates = [
    { value: 0, label: "Начало" },
    { value: Math.min(60, maximumStart), label: "Через минуту" },
    { value: Math.min(duration * 0.25, maximumStart), label: "25% файла" },
    { value: Math.min(duration * 0.5, maximumStart), label: "Середина" },
    { value: Math.min(duration * 0.75, maximumStart), label: "75% файла" },
  ];
  const used = new Set<number>();
  return candidates
    .map((point) => ({ ...point, value: Math.max(0, Math.round(point.value)) }))
    .filter((point) => {
      if (used.has(point.value)) return false;
      used.add(point.value);
      return true;
    });
}

async function errorMessage(response: Response) {
  const payload = await response.json().catch(() => null);
  return payload?.detail || "Не удалось выполнить действие";
}

const pendingAudioDatabase = "local-whisper-dictation";
const pendingAudioStore = "pending-recordings";

function openPendingAudioDatabase() {
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(pendingAudioDatabase, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(pendingAudioStore)) {
        request.result.createObjectStore(pendingAudioStore, { keyPath: "id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function savePendingRecording(recording: PendingRecording) {
  const database = await openPendingAudioDatabase();
  await new Promise<void>((resolve, reject) => {
    const transaction = database.transaction(pendingAudioStore, "readwrite");
    transaction.objectStore(pendingAudioStore).put(recording);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
  database.close();
}

async function listPendingRecordings() {
  const database = await openPendingAudioDatabase();
  const recordings = await new Promise<PendingRecording[]>((resolve, reject) => {
    const request = database
      .transaction(pendingAudioStore, "readonly")
      .objectStore(pendingAudioStore)
      .getAll();
    request.onsuccess = () => resolve(request.result as PendingRecording[]);
    request.onerror = () => reject(request.error);
  });
  database.close();
  return recordings;
}

async function deletePendingRecording(id: string) {
  const database = await openPendingAudioDatabase();
  await new Promise<void>((resolve, reject) => {
    const transaction = database.transaction(pendingAudioStore, "readwrite");
    transaction.objectStore(pendingAudioStore).delete(id);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
  database.close();
}

export default function Home() {
  const [screen, setScreen] = useState<Screen>("projects");
  const [projects, setProjects] = useState<Project[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [queueOverview, setQueueOverview] =
    useState<QueueOverview>(emptyQueueOverview);
  const [media, setMedia] = useState<StagedMedia[]>([]);
  const [dictations, setDictations] = useState<DictationMessage[]>([]);
  const [settings, setSettings] = useState<Settings>(initialSettings);
  const [activeProjectId, setActiveProjectId] = useState("");
  // Открытый проект может не попасть на текущую страницу ленты — тогда он
  // догружается отдельным запросом, чтобы экран проекта не опустел.
  const [activeProjectDetail, setActiveProjectDetail] =
    useState<Project | null>(null);
  const [folderFilter, setFolderFilter] = useState<string>("all");
  const [projectsPage, setProjectsPage] = useState(1);
  const [projectsTotal, setProjectsTotal] = useState(0);
  const [projectCounts, setProjectCounts] = useState<ProjectCounts>({
    feed: 0,
    loose: 0,
    total: 0,
  });
  const [dictationPage, setDictationPage] = useState(1);
  const [dictationTotal, setDictationTotal] = useState(0);
  /* Размер страницы «на сейчас».
   *
   * Пусто — значит берём значение из настроек. Выбор в самой ленте меняет
   * только текущий сеанс: постоянное значение живёт в настройках, а здесь
   * человек подстраивает вид под конкретную задачу. */
  const [projectsPageSizeChoice, setProjectsPageSizeChoice] = useState<
    PageSize | ""
  >("");
  const [dictationPageSizeChoice, setDictationPageSizeChoice] = useState<
    PageSize | ""
  >("");
  const [online, setOnline] = useState<boolean | null>(null);
  const [shuttingDown, setShuttingDown] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [language, setLanguage] = useState<JobOptions["language"]>("ru");
  const [quality, setQuality] = useState<JobOptions["quality"]>("accurate");
  const [timestamps, setTimestamps] =
    useState<JobOptions["timestamps"]>("segments");
  const [formatting, setFormatting] =
    useState<JobOptions["formatting"]>("formatted");
  const [dragging, setDragging] = useState(false);
  const [quickDragging, setQuickDragging] = useState(false);
  const [quickUploading, setQuickUploading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [queueing, setQueueing] = useState(false);
  const [trackSelections, setTrackSelections] = useState<
    Record<string, number>
  >({});
  const [trackPreviewStarts, setTrackPreviewStarts] = useState<
    Record<string, number>
  >({});
  const [trimRanges, setTrimRanges] = useState<Record<string, TrimRange>>({});
  // Ключ включает дорожку: у другой дорожки своя звуковая картина.
  const [trimOpen, setTrimOpen] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [urlImportOpen, setUrlImportOpen] = useState(false);
  const [urlValue, setUrlValue] = useState("");
  const [urlPasscode, setUrlPasscode] = useState("");
  const [urlMetadata, setUrlMetadata] = useState<UrlMetadata | null>(null);
  const [urlTargetProjectId, setUrlTargetProjectId] = useState("");
  const [urlProjectName, setUrlProjectName] = useState("");
  const [urlFolderId, setUrlFolderId] = useState("");
  const [urlQuality, setUrlQuality] = useState("auto");
  const [urlLanguage, setUrlLanguage] =
    useState<JobOptions["language"]>("ru");
  const [urlRecognitionQuality, setUrlRecognitionQuality] =
    useState<JobOptions["quality"]>("accurate");
  const [urlDiarization, setUrlDiarization] = useState(false);
  const [urlDiarizationEngine, setUrlDiarizationEngine] =
    useState<DiarizationEngine>("nemotron");
  const [urlTranscribe, setUrlTranscribe] = useState(true);
  const [urlProbing, setUrlProbing] = useState(false);
  const [urlStarting, setUrlStarting] = useState(false);
  const [urlError, setUrlError] = useState("");
  const [urlDownloads, setUrlDownloads] = useState<UrlDownload[]>([]);
  const [activeUrlDownloadId, setActiveUrlDownloadId] = useState("");
  const [rerunJobId, setRerunJobId] = useState("");
  const [rerunLanguage, setRerunLanguage] =
    useState<JobOptions["language"]>("ru");
  const [rerunQuality, setRerunQuality] =
    useState<JobOptions["quality"]>("accurate");
  const [rerunDiarization, setRerunDiarization] = useState(false);
  const [rerunDiarizationEngine, setRerunDiarizationEngine] =
    useState<DiarizationEngine>("nemotron");
  // Повторная разметка готового файла: текст остаётся, меняются спикеры.
  const [rediarizeJobId, setRediarizeJobId] = useState("");
  const [rediarizeEngine, setRediarizeEngine] =
    useState<DiarizationEngine>("nemotron");
  const [rediarizeStarting, setRediarizeStarting] = useState(false);
  const [missingSpeakersEngine, setMissingSpeakersEngine] =
    useState<DiarizationEngine>("nemotron");
  // Образец голоса спикера: какой играет и какой ещё готовится.
  const [speakerSampleKey, setSpeakerSampleKey] = useState("");
  const [speakerSampleLoadingKey, setSpeakerSampleLoadingKey] = useState("");
  const [nemotronDownloadStarting, setNemotronDownloadStarting] =
    useState(false);
  const [rerunStarting, setRerunStarting] = useState(false);
  const [speakerDiarizationStarting, setSpeakerDiarizationStarting] =
    useState(false);
  const [newProjectTitle, setNewProjectTitle] = useState("");
  const [newProjectFolder, setNewProjectFolder] = useState("");
  const [viewTimestamps, setViewTimestamps] =
    useState<Settings["default_view_timestamps"]>("none");
  const [viewFormatting, setViewFormatting] =
    useState<Settings["default_view_formatting"]>("formatted");
  const [viewSpeakers, setViewSpeakers] =
    useState<Settings["default_view_speakers"]>("off");
  const [diarization, setDiarization] = useState(false);
  const [diarizationEngine, setDiarizationEngine] =
    useState<DiarizationEngine>("nemotron");
  const [diarizationStatus, setDiarizationStatus] =
    useState<DiarizationStatus | null>(null);
  const [diarizationToken, setDiarizationToken] = useState("");
  const [activatingDiarization, setActivatingDiarization] = useState(false);
  const [components, setComponents] = useState<ComponentsInventory | null>(null);
  const [componentsBusy, setComponentsBusy] = useState(false);
  const [modelDownload, setModelDownload] = useState<ModelDownload | null>(null);
  const [services, setServices] = useState<ConnectedService[]>([]);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedProjects, setSelectedProjects] = useState<Set<string>>(
    new Set(),
  );
  const [bulkBusy, setBulkBusy] = useState(false);
  const [editingProjectId, setEditingProjectId] = useState("");
  const [projectNameDraft, setProjectNameDraft] = useState("");
  const [projectFolderDraft, setProjectFolderDraft] = useState("");
  const [editingFolderId, setEditingFolderId] = useState("");
  const [folderNameDraft, setFolderNameDraft] = useState("");
  const [folderFeedDraft, setFolderFeedDraft] = useState(true);
  const [activeDictationId, setActiveDictationId] = useState("");
  const [dictationText, setDictationText] = useState("");
  const [dictationDirty, setDictationDirty] = useState(false);
  // Фрагменты, чья расшифровка уже вписана в поле. Отметка идёт по id, а не по
  // сравнению текстов: текст в поле правят руками, и узнать в нём расшифровку
  // после правки нельзя, а дописать её второй раз — можно.
  const [dictationMergedIds, setDictationMergedIds] = useState<Set<string>>(
    new Set(),
  );
  const [dictationLanguage, setDictationLanguage] =
    useState<"auto" | "ru" | "en">("ru");
  const [dictationQuality, setDictationQuality] =
    useState<"fast" | "accurate">("fast");
  const [recording, setRecording] = useState(false);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const [waveform, setWaveform] = useState<number[]>(
    () => Array.from({ length: 42 }, () => 0.08),
  );
  const [startingRecording, setStartingRecording] = useState(false);
  const [savingRecording, setSavingRecording] = useState(false);
  const [pendingRecordingCount, setPendingRecordingCount] = useState(0);
  const [autoConfirmDictationId, setAutoConfirmDictationId] = useState("");
  const [editingHistoryId, setEditingHistoryId] = useState("");
  const [historyTextDraft, setHistoryTextDraft] = useState("");
  const [transcriptText, setTranscriptText] = useState("");
  const [transcriptLoading, setTranscriptLoading] = useState(false);
  const [projectSpeakers, setProjectSpeakers] = useState<SpeakerEntry[]>([]);
  const [selectedSpeakerKeys, setSelectedSpeakerKeys] = useState<Set<string>>(
    new Set(),
  );
  const [editingSpeakerKey, setEditingSpeakerKey] = useState("");
  const [speakerNameDraft, setSpeakerNameDraft] = useState("");
  const speakerEditRef = useRef<{
    speaker: SpeakerEntry;
    projectId: string;
    name: string;
  } | null>(null);
  const speakerDraftsRef = useRef(new Map<string, string>());
  const speakerSavesRef = useRef(new Map<string, Promise<void>>());
  // id задач, по которым запрос на остановку уже отправлен, но ответ ещё не
  // пришёл: нужно, чтобы кнопка не позволяла нажать дважды.
  const [stoppingIds, setStoppingIds] = useState<Set<string>>(new Set());
  const [prioritizingIds, setPrioritizingIds] = useState<Set<string>>(
    new Set(),
  );
  const defaultsApplied = useRef(false);
  const viewDefaultsApplied = useRef(false);
  const settingsReconnectRefreshNeeded = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const quickInputRef = useRef<HTMLInputElement>(null);
  const quickDragDepthRef = useRef(0);
  const projectDragDepthRef = useRef(0);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const recorderChunksRef = useRef<Blob[]>([]);
  const recorderStopModeRef = useRef<RecordingStopMode>("save");
  const recorderStartedAtRef = useRef(0);
  const recorderStreamRef = useRef<MediaStream | null>(null);
  const recorderTimerRef = useRef<number | null>(null);
  const waveformFrameRef = useRef<number | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const flushingRecordingsRef = useRef(false);
  const autoConfirmInFlightRef = useRef(false);
  const speakerSelectionProjectRef = useRef("");
  const speakerSampleAudioRef = useRef<HTMLAudioElement | null>(null);
  const speakerSampleUrlRef = useRef("");
  // Какой образец запрошен последним. Ответ на более ранний клик, пришедший
  // позже, играть не должен.
  const speakerSampleRequestRef = useRef("");
  const knownSpeakerKeysRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const hasInlineEditor = Boolean(
      editingProjectId ||
      editingFolderId ||
      editingSpeakerKey ||
      editingHistoryId,
    );
    if (!hasInlineEditor && !createOpen && !urlImportOpen) return;

    const closeEditors = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (target?.closest("[data-inline-editor]")) return;
      setEditingProjectId("");
      setProjectNameDraft("");
      setProjectFolderDraft("");
      setEditingFolderId("");
      setFolderNameDraft("");
      setEditingHistoryId("");
      setHistoryTextDraft("");
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setEditingProjectId("");
      setProjectNameDraft("");
      setProjectFolderDraft("");
      setEditingFolderId("");
      setFolderNameDraft("");
      cancelSpeakerEdit();
      setEditingHistoryId("");
      setHistoryTextDraft("");
      setCreateOpen(false);
      setUrlImportOpen(false);
    };

    document.addEventListener("pointerdown", closeEditors);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeEditors);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [
    createOpen,
    editingFolderId,
    editingHistoryId,
    editingProjectId,
    editingSpeakerKey,
    urlImportOpen,
  ]);

  const projectsPageSize =
    projectsPageSizeChoice || settings.projects_page_size;
  const dictationPageSize =
    dictationPageSizeChoice || settings.dictation_page_size;
  /* Номер страницы удерживается в границах прямо здесь, а не эффектом.
   *
   * Список умеет укорачиваться под руками — проекты удалили пачкой, историю
   * очистили — и седьмая страница списка, в котором осталось две, показала бы
   * пустоту вместо содержимого. Считаем допустимый номер на месте: и запрос,
   * и пагинатор пользуются уже исправленным. */
  const projectsPerPage = pageSizeCount(projectsPageSize);
  const projectsPageCount = projectsPerPage
    ? Math.max(1, Math.ceil(projectsTotal / projectsPerPage))
    : 1;
  const projectsCurrentPage = Math.min(
    Math.max(1, projectsPage),
    projectsPageCount,
  );
  const dictationPerPage = pageSizeCount(dictationPageSize);
  const dictationPageCount = dictationPerPage
    ? Math.max(1, Math.ceil(dictationTotal / dictationPerPage))
    : 1;
  const dictationCurrentPage = Math.min(
    Math.max(1, dictationPage),
    dictationPageCount,
  );
  /* Порядок выдачи повторяет то, как лента рисует список.
   *
   * В виде «по папкам» сервер отдаёт проекты сгруппированными: иначе одна
   * папка расползлась бы по трём страницам, и группа на экране выглядела бы
   * обрезанной без всякой причины. */
  const projectsOrder =
    settings.project_view === "folders" && folderFilter === "all"
      ? "folder"
      : "recent";

  const refresh = useCallback(async () => {
    try {
      const projectQuery = new URLSearchParams({
        folder: folderFilter,
        order: projectsOrder,
        page: String(projectsCurrentPage),
      });
      if (projectsPerPage) {
        projectQuery.set("per_page", String(projectsPerPage));
      }
      const dictationQuery = new URLSearchParams({
        page: String(dictationCurrentPage),
      });
      if (dictationPerPage) {
        dictationQuery.set("per_page", String(dictationPerPage));
      }
      const [
        projectsResponse,
        foldersResponse,
        jobsResponse,
        mediaResponse,
        settingsResponse,
        diarizationResponse,
        dictationsResponse,
        urlDownloadsResponse,
        queueResponse,
        countsResponse,
      ] = await Promise.all([
          fetch(`${API}/projects?${projectQuery.toString()}`, {
            cache: "no-store",
          }),
          fetch(`${API}/folders`, { cache: "no-store" }),
          fetch(`${API}/jobs`, { cache: "no-store" }),
          fetch(`${API}/media`, { cache: "no-store" }),
          fetch(`${API}/settings`, { cache: "no-store" }),
          fetch(`${API}/diarization/status`, { cache: "no-store" }),
          fetch(`${API}/dictations?${dictationQuery.toString()}`, {
            cache: "no-store",
          }),
          fetch(`${API}/url-downloads`, { cache: "no-store" }),
          fetch(`${API}/queue`, { cache: "no-store" }),
          fetch(`${API}/projects/counts`, { cache: "no-store" }),
        ]);
      if (
        !projectsResponse.ok ||
        !countsResponse.ok ||
        !foldersResponse.ok ||
        !jobsResponse.ok ||
        !mediaResponse.ok ||
        !settingsResponse.ok ||
        !diarizationResponse.ok ||
        !dictationsResponse.ok ||
        !urlDownloadsResponse.ok ||
        !queueResponse.ok
      ) {
        throw new Error("Сервер не отвечает");
      }
      const nextSettings = (await settingsResponse.json()) as Settings;
      const nextProjects = (await projectsResponse.json()) as Project[];
      setProjects(nextProjects);
      // Сколько всего проектов подходит под текущий фильтр — знает только
      // сервер: на руках у страницы лишь её кусок списка.
      setProjectsTotal(
        Number(projectsResponse.headers.get("X-Total-Count")) ||
          nextProjects.length,
      );
      setProjectCounts((await countsResponse.json()) as ProjectCounts);
      // Отметка на исчезнувшем проекте — удалённом поштучно или из другой
      // вкладки — иначе продолжала бы считаться в «Удалить N».
      setSelectedProjects((current) => {
        if (!current.size) return current;
        const alive = new Set(nextProjects.map((project) => project.id));
        const next = new Set([...current].filter((id) => alive.has(id)));
        return next.size === current.size ? current : next;
      });
      setFolders((await foldersResponse.json()) as Folder[]);
      setJobs((await jobsResponse.json()) as Job[]);
      const nextMedia = (await mediaResponse.json()) as StagedMedia[];
      setMedia(nextMedia);
      setTrackSelections((current) => {
        const next = { ...current };
        nextMedia.forEach((item) => {
          if (next[item.id] === undefined && item.audio_streams.length) {
            next[item.id] = item.audio_streams[0].index;
          }
        });
        return next;
      });
      setSettings(nextSettings);
      setDiarizationStatus(
        (await diarizationResponse.json()) as DiarizationStatus,
      );
      setDictations(
        (await dictationsResponse.json()) as DictationMessage[],
      );
      // В заголовке — количество подтверждённых сообщений: черновик приходит
      // сверх страницы и в счёт истории не идёт.
      setDictationTotal(
        Number(dictationsResponse.headers.get("X-Total-Count")) || 0,
      );
      setUrlDownloads(
        (await urlDownloadsResponse.json()) as UrlDownload[],
      );
      setQueueOverview((await queueResponse.json()) as QueueOverview);
      const shouldApplySavedDefaults =
        !defaultsApplied.current || settingsReconnectRefreshNeeded.current;
      const shouldApplySavedViewDefaults =
        !viewDefaultsApplied.current || settingsReconnectRefreshNeeded.current;
      if (shouldApplySavedDefaults) {
        setLanguage(nextSettings.default_language);
        setQuality(nextSettings.default_quality);
        setTimestamps(nextSettings.default_timestamps);
        setFormatting(nextSettings.default_formatting);
        setDiarization(nextSettings.default_diarization === "on");
        setDiarizationEngine(nextSettings.diarization_engine);
        setMissingSpeakersEngine(nextSettings.diarization_engine);
        setDictationLanguage(nextSettings.dictation_default_language);
        setDictationQuality(nextSettings.dictation_default_quality);
        setUrlTranscribe(nextSettings.download_transcribe === "on");
        defaultsApplied.current = true;
      }
      if (shouldApplySavedViewDefaults) {
        setViewTimestamps(nextSettings.default_view_timestamps);
        setViewFormatting(nextSettings.default_view_formatting);
        setViewSpeakers(nextSettings.default_view_speakers);
        viewDefaultsApplied.current = true;
      }
      settingsReconnectRefreshNeeded.current = false;
      setOnline(true);
      /* Открытый проект держим отдельно, если его нет на странице.
       *
       * Иначе переход на вторую страницу ленты — или новый проект, сдвинувший
       * список, — обнулял бы уже открытый экран проекта: он ищет себя в
       * загруженном списке, а там его больше нет. */
      if (
        activeProjectId &&
        !nextProjects.some((project) => project.id === activeProjectId)
      ) {
        const detailResponse = await fetch(
          `${API}/projects/${activeProjectId}`,
          { cache: "no-store" },
        );
        setActiveProjectDetail(
          detailResponse.ok
            ? ((await detailResponse.json()) as Project)
            : null,
        );
      } else {
        setActiveProjectDetail(null);
      }
    } catch {
      settingsReconnectRefreshNeeded.current = true;
      setOnline(false);
    }
  }, [
    activeProjectId,
    dictationCurrentPage,
    dictationPerPage,
    folderFilter,
    projectsCurrentPage,
    projectsOrder,
    projectsPerPage,
  ]);

  useEffect(() => {
    const initial = window.setTimeout(refresh, 0);
    const timer = window.setInterval(refresh, 1800);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [refresh]);


  useEffect(() => {
    // В CSS живут только «light» и «dark». «Авто» разрешается здесь, чтобы не
    // держать вторую копию тёмных переменных под @media, и слушает смену темы
    // Windows на ходу — без перезагрузки страницы.
    if (settings.theme !== "auto") {
      document.documentElement.dataset.theme = settings.theme;
      return;
    }
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme = query.matches ? "dark" : "light";
    };
    apply();
    query.addEventListener("change", apply);
    return () => query.removeEventListener("change", apply);
  }, [settings.theme]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // Инвентарь читается один раз при открытии настроек: опрос запускает внешние
  // программы, и держать его в общем цикле обновления было бы расточительно.
  useEffect(() => {
    if (screen === "settings" && !components) {
      void loadComponents();
      void loadServices();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [screen]);

  // Пока идёт загрузка модели — частый опрос прогресса. Когда закончилась,
  // инвентарь перечитывается, чтобы состояние компонента стало «есть».
  useEffect(() => {
    if (modelDownload?.status !== "running") return;
    const timer = window.setInterval(async () => {
      const response = await fetch(`${API}/components/models/download`);
      if (!response.ok) return;
      const next = (await response.json()) as ModelDownload;
      setModelDownload(next);
      if (next.status !== "running") void loadComponents(true);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [modelDownload?.status]);

  const activeProject =
    projects.find((project) => project.id === activeProjectId) ||
    (activeProjectDetail?.id === activeProjectId
      ? activeProjectDetail
      : undefined);
  const urlTargetProject = projects.find(
    (project) => project.id === urlTargetProjectId,
  );
  const activeUrlDownload = urlDownloads.find(
    (download) => download.project_id === activeUrlDownloadId,
  );
  const selectedUrlEstimate =
    urlQuality === "auto"
      ? urlMetadata?.auto_filesize_bytes || urlMetadata?.filesize_bytes
      : urlMetadata?.qualities.find(
          (option) => option.quality === urlQuality,
        )?.filesize_bytes;
  const rerunJob = jobs.find((job) => job.id === rerunJobId);
  const rediarizeJob = jobs.find((job) => job.id === rediarizeJobId);
  const selectedSpeakerKeyList = useMemo(
    () => Array.from(selectedSpeakerKeys).sort(),
    [selectedSpeakerKeys],
  );
  const speakerNamesSignature = useMemo(
    () =>
      projectSpeakers
        .map((speaker) => `${speaker.key}:${speaker.name}`)
        .join("|"),
    [projectSpeakers],
  );
  const speakerFilterValue =
    viewSpeakers === "on" &&
    projectSpeakers.length > 0 &&
    selectedSpeakerKeyList.length < projectSpeakers.length
      ? selectedSpeakerKeyList.length
        ? selectedSpeakerKeyList.join(",")
        : "none"
      : "";

  useEffect(() => {
    if (
      screen !== "project" ||
      !activeProjectId ||
      !activeProject?.completed_count
    ) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setProjectSpeakers([]);
      setSelectedSpeakerKeys(new Set());
      speakerSelectionProjectRef.current = "";
      knownSpeakerKeysRef.current = new Set();
      return;
    }
    const controller = new AbortController();
    fetch(`${API}/projects/${activeProjectId}/speakers`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(await errorMessage(response));
        return response.json() as Promise<SpeakerEntry[]>;
      })
      .then((nextSpeakers) => {
        const available = new Set(nextSpeakers.map((speaker) => speaker.key));
        setProjectSpeakers(nextSpeakers);
        setSelectedSpeakerKeys((current) => {
          if (speakerSelectionProjectRef.current !== activeProjectId) {
            speakerSelectionProjectRef.current = activeProjectId;
            knownSpeakerKeysRef.current = available;
            return available;
          }
          const next = new Set(
            Array.from(current).filter((key) => available.has(key)),
          );
          available.forEach((key) => {
            if (!knownSpeakerKeysRef.current.has(key)) next.add(key);
          });
          knownSpeakerKeysRef.current = available;
          return next;
        });
      })
      .catch((error) => {
        if (error instanceof Error && error.name !== "AbortError") {
          setNotice(error.message);
        }
      });
    return () => controller.abort();
  }, [
    screen,
    activeProjectId,
    activeProject?.completed_count,
  ]);

  useEffect(() => {
    if (
      screen !== "project" ||
      !activeProjectId ||
      !activeProject?.completed_count
    ) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setTranscriptText("");
      return;
    }
    const controller = new AbortController();
    setTranscriptLoading(true);
    const query = new URLSearchParams({
      timestamps: viewTimestamps,
      formatting: viewFormatting,
      speakers: viewSpeakers,
    });
    if (speakerFilterValue) query.set("speaker_keys", speakerFilterValue);
    fetch(
      `${API}/projects/${activeProjectId}/combined/text?${query.toString()}`,
      { cache: "no-store", signal: controller.signal },
    )
      .then(async (response) => {
        if (!response.ok) throw new Error(await errorMessage(response));
        return response.text();
      })
      .then(setTranscriptText)
      .catch((error) => {
        if (error instanceof Error && error.name !== "AbortError") {
          setNotice(error.message);
        }
      })
      .finally(() => setTranscriptLoading(false));
    return () => controller.abort();
  }, [
    screen,
    activeProjectId,
    activeProject?.completed_count,
    viewTimestamps,
    viewFormatting,
    viewSpeakers,
    speakerFilterValue,
    speakerNamesSignature,
  ]);

  const projectJobs = useMemo(
    () =>
      jobs
        .filter((job) => job.session_id === activeProjectId)
        .sort((a, b) => a.position - b.position),
    [jobs, activeProjectId],
  );
  const completedJobsWithoutSpeakers = useMemo(
    () =>
      projectJobs.filter(
        (job) => job.status === "completed" && !job.options.diarization,
      ),
    [projectJobs],
  );
  const showSpeakerMissingBanner =
    viewSpeakers === "on" && completedJobsWithoutSpeakers.length > 0;
  const showSpeakerNotFoundBanner =
    viewSpeakers === "on" &&
    !completedJobsWithoutSpeakers.length &&
    Boolean(activeProject?.completed_count) &&
    projectSpeakers.length === 0;
  const currentDictation =
    dictations.find((message) => message.id === activeDictationId) ||
    dictations.find((message) => message.status === "draft");
  const currentDictationId = currentDictation?.id;
  const currentDictationServerText = currentDictation?.text;
  const dictationFragments = dictations.flatMap(
    (message) => message.fragments,
  );
  const currentDictationBusy = Boolean(
    currentDictation?.fragments.some(
      (fragment) =>
        fragment.status === "queued" || fragment.status === "processing",
    ),
  );
  // Готовые расшифровки текущего сообщения по порядку записи. Только они и
  // попадают в поле правки со стороны сервиса.
  const currentDictationFragments = currentDictation?.fragments;
  const currentDictationTranscripts = useMemo(
    () =>
      (currentDictationFragments ?? [])
        .filter(
          (fragment) =>
            fragment.status === "completed" && fragment.transcript.trim(),
        )
        .sort((left, right) => left.position - right.position)
        .map((fragment) => ({
          id: fragment.id,
          text: fragment.transcript.trim(),
        })),
    [currentDictationFragments],
  );
  const dictationHistory = dictations.filter(
    (message) => message.status === "confirmed",
  );
  // Нумерация сообщений сквозная по всей истории, а не по странице: на
  // третьей странице «Сообщение 1» сбивало бы с толку.
  const dictationHistoryOffset = dictationPerPage
    ? (dictationCurrentPage - 1) * dictationPerPage
    : 0;
  const projectMedia = useMemo(
    () => media.filter((item) => item.session_id === activeProjectId),
    [media, activeProjectId],
  );

  /* Поле правки живёт само по себе, а не отражает серверный текст.
   *
   * Раньше связь была двусторонней: правка уходила PATCH-ем через 700 мс, а
   * ответ опроса (каждые 1.8 с) подставлялся обратно в поле. Опрос и
   * сохранение шли вразнобой, поле откатывалось на предыдущую версию и через
   * секунду получало новую. Со стороны это и есть «текст дёргается, курсор
   * прыгает вверх, пустые строки схлопываются, что-то дописывается само».
   *
   * Теперь связь односторонняя. В поле попадает ровно два вида текста: то, что
   * человек набрал руками, и расшифровка очередного фрагмента — дописанная в
   * самый конец. Серверный текст читается один раз, когда открывается другое
   * сообщение диктовки. Обратно текст уходит отложенным PATCH-ем, и его ответ
   * на поле уже никак не влияет. */
  const dictationTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const dictationCaretRef = useRef<{ start: number; end: number } | null>(null);
  const loadedDictationRef = useRef("");
  // Текст поля для асинхронных ответов: пока PATCH был в пути, человек мог
  // набрать ещё. Тогда снимать отметку «есть несохранённое» нельзя — иначе
  // отложенное сохранение отменит само себя, и последние правки не уедут.
  const dictationTextRef = useRef("");

  useEffect(() => {
    dictationTextRef.current = dictationText;
  }, [dictationText]);

  useEffect(() => {
    if (!currentDictationId) return;

    if (loadedDictationRef.current !== currentDictationId) {
      // Открылось другое сообщение: его текст берётся с сервера как есть, а
      // все вошедшие в него расшифровки помечаются вписанными — иначе они
      // добавятся в поле по второму разу.
      loadedDictationRef.current = currentDictationId;
      setDictationText(currentDictationServerText || "");
      setDictationMergedIds(
        new Set(currentDictationTranscripts.map((item) => item.id)),
      );
      setDictationDirty(false);
      return;
    }

    const pending = currentDictationTranscripts.filter(
      (item) => !dictationMergedIds.has(item.id),
    );
    if (!pending.length) return;

    // Курсор мог стоять в середине набранного: дописывание в конец не должно
    // его трогать. Позиция снимается до смены value и возвращается сразу после.
    const field = dictationTextareaRef.current;
    if (field && document.activeElement === field) {
      dictationCaretRef.current = {
        start: field.selectionStart,
        end: field.selectionEnd,
      };
    }
    // Пришедшая расшифровка — это и есть внешнее событие, ради которого поле
    // вообще связано с сервисом: другого способа дописать её в конец, не трогая
    // набранное руками, нет.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDictationText((current) =>
      pending.reduce(
        (text, item) =>
          text.trim() ? `${text.trimEnd()}\n\n${item.text}` : item.text,
        current,
      ),
    );
    setDictationMergedIds((current) => {
      const next = new Set(current);
      pending.forEach((item) => next.add(item.id));
      return next;
    });
    setDictationDirty(true);
  }, [
    currentDictationId,
    currentDictationServerText,
    currentDictationTranscripts,
    dictationMergedIds,
  ]);

  useBrowserLayoutEffect(() => {
    const caret = dictationCaretRef.current;
    if (!caret) return;
    dictationCaretRef.current = null;
    const field = dictationTextareaRef.current;
    if (!field || document.activeElement !== field) return;
    field.setSelectionRange(caret.start, caret.end);
  }, [dictationText]);

  useEffect(() => {
    if (!dictationDirty || !currentDictationId || currentDictationBusy) return;
    const timer = window.setTimeout(async () => {
      const saved = dictationText;
      const response = await fetch(`${API}/dictations/${currentDictationId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: saved }),
      });
      if (response.ok && dictationTextRef.current === saved) {
        setDictationDirty(false);
      }
    }, 700);
    return () => window.clearTimeout(timer);
  }, [
    dictationDirty,
    dictationText,
    currentDictationId,
    currentDictationBusy,
  ]);

  useEffect(() => {
    if (!recording) return;
    const preventClose = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventClose);
    return () => window.removeEventListener("beforeunload", preventClose);
  }, [recording]);

  useEffect(() => {
    if (!recording) return;
    const cancelOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      cancelDictationRecording();
    };
    window.addEventListener("keydown", cancelOnEscape, true);
    return () => window.removeEventListener("keydown", cancelOnEscape, true);
  }, [recording]);

  // Скрытый сервис прячется целиком — и папка, и её проекты. Иначе выключенный
  // эксперимент продолжает занимать строку в списке папок, хотя внутри неё для
  // человека уже ничего нет: его проекты из общей ленты убраны.
  const hideIntegrations = settings.integration_feed === "hide";
  const visibleFolders = useMemo(
    () => folders.filter((folder) => !(hideIntegrations && folder.integration)),
    [folders, hideIntegrations],
  );
  const feedProjectCount = projectCounts.feed;

  /* Отбор целиком на сервере.
   *
   * Раньше фильтр по папке и скрытия применялись здесь, к загруженному
   * списку. С постраничной выдачей так нельзя: сервер уже вернул нужный
   * кусок нужного списка, и всё, что осталось на клиенте, — показать его. */
  const filteredProjects = projects;

  /* Действия работают только по видимым отметкам.
   *
   * Даже если в наборе как-то останется чужой id, удалить или перенести
   * невидимое нельзя, а счётчик на кнопке показывает ровно то, что человек
   * видит отмеченным. */
  const visibleProjectIdList = filteredProjects.map((project) => project.id);
  const selectedVisibleIds = visibleProjectIdList.filter((id) =>
    selectedProjects.has(id),
  );
  const allVisibleSelected =
    visibleProjectIdList.length > 0 &&
    selectedVisibleIds.length === visibleProjectIdList.length;

  const groupedProjects = useMemo(() => {
    const groups: Array<{
      id: string;
      name: string;
      items: Project[];
    }> = visibleFolders
      .map((folder) => ({
        id: folder.id,
        name: folder.name,
        items: filteredProjects.filter(
          (project) => project.folder_id === folder.id,
        ),
      }))
      .filter((group) => group.items.length > 0);
    const loose = filteredProjects.filter((project) => !project.folder_id);
    if (loose.length) {
      groups.push({ id: "none", name: "Без папки", items: loose });
    }
    return groups;
  }, [filteredProjects, visibleFolders]);

  function openProject(projectId: string) {
    setActiveProjectId(projectId);
    setScreen("project");
    setFiles([]);
  }

  async function ensureDictationDraft() {
    const existing =
      dictations.find(
        (message) =>
          message.id === activeDictationId && message.status === "draft",
      ) || dictations.find((message) => message.status === "draft");
    if (existing) {
      setActiveDictationId(existing.id);
      return existing.id;
    }
    const response = await fetch(`${API}/dictations`, { method: "POST" });
    if (!response.ok) throw new Error(await errorMessage(response));
    const created = (await response.json()) as DictationMessage;
    setActiveDictationId(created.id);
    setDictationText("");
    setDictationDirty(false);
    await refresh();
    return created.id;
  }

  async function openDictation() {
    setScreen("dictation");
    try {
      await ensureDictationDraft();
    } catch (error) {
      setNotice(
        error instanceof Error
          ? error.message
          : "Не удалось открыть диктовку",
      );
    }
  }

  function stopRecorderResources() {
    if (recorderTimerRef.current !== null) {
      window.clearInterval(recorderTimerRef.current);
      recorderTimerRef.current = null;
    }
    if (waveformFrameRef.current !== null) {
      window.cancelAnimationFrame(waveformFrameRef.current);
      waveformFrameRef.current = null;
    }
    recorderStreamRef.current?.getTracks().forEach((track) => track.stop());
    recorderStreamRef.current = null;
    audioContextRef.current?.close().catch(() => undefined);
    audioContextRef.current = null;
    setWaveform(Array.from({ length: 42 }, () => 0.08));
  }

  const uploadPendingRecording = useCallback(async (item: PendingRecording) => {
    const body = new FormData();
    const extension = item.blob.type.includes("ogg") ? "ogg" : "webm";
    body.append("audio", item.blob, `dictation-${item.id}.${extension}`);
    body.append("duration_seconds", String(item.durationSeconds));
    body.append("language", item.language);
    body.append("quality", item.quality);
    const response = await fetch(
      `${API}/dictations/${item.messageId}/fragments`,
      { method: "POST", body },
    );
    if (!response.ok) throw new Error(await errorMessage(response));
    await deletePendingRecording(item.id);
  }, []);

  const flushPendingRecordings = useCallback(async () => {
    if (flushingRecordingsRef.current || !("indexedDB" in window)) return;
    flushingRecordingsRef.current = true;
    try {
      const pending = await listPendingRecordings();
      setPendingRecordingCount(pending.length);
      for (const item of pending) {
        try {
          await uploadPendingRecording(item);
          setPendingRecordingCount((count) => Math.max(0, count - 1));
        } catch {
          break;
        }
      }
      await refresh();
    } catch {
      // Аварийный буфер недоступен только в очень старых браузерах.
    } finally {
      flushingRecordingsRef.current = false;
    }
  }, [refresh, uploadPendingRecording]);

  useEffect(() => {
    if (!online || screen !== "dictation") return;
    const timer = window.setTimeout(() => {
      void flushPendingRecordings();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [online, screen, flushPendingRecordings]);

  async function persistAndUploadRecording(
    blob: Blob,
    durationSeconds: number,
    messageId: string,
  ): Promise<boolean> {
    const pending: PendingRecording = {
      id: crypto.randomUUID(),
      messageId,
      blob,
      durationSeconds,
      language: dictationLanguage,
      quality: dictationQuality,
      createdAt: new Date().toISOString(),
    };
    setSavingRecording(true);
    let storedInFallback = false;
    try {
      if ("indexedDB" in window) {
        await savePendingRecording(pending);
        storedInFallback = true;
        setPendingRecordingCount((count) => count + 1);
      }
      await uploadPendingRecording(pending);
      if (storedInFallback) {
        setPendingRecordingCount((count) => Math.max(0, count - 1));
      }
      setNotice(
        "Аудио сохранено на диск и поставлено в локальную очередь",
      );
      await refresh();
      return true;
    } catch (error) {
      setNotice(
        storedInFallback
          ? "Аудио сохранено в аварийном буфере и будет передано сервису автоматически"
          : error instanceof Error
            ? error.message
            : "Не удалось сохранить запись",
      );
      return false;
    } finally {
      setSavingRecording(false);
    }
  }

  async function startDictationRecording() {
    if (recording || startingRecording) return;
    if (savingRecording) {
      setNotice("Предыдущий фрагмент ещё сохраняется");
      return;
    }
    if (
      !navigator.mediaDevices?.getUserMedia ||
      typeof MediaRecorder === "undefined"
    ) {
      setNotice("Этот браузер не поддерживает запись с микрофона");
      return;
    }
    setStartingRecording(true);
    try {
      const messageId = await ensureDictationDraft();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      const mimeType = [
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/ogg;codecs=opus",
      ].find((value) => MediaRecorder.isTypeSupported(value));
      const recorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      recorderRef.current = recorder;
      recorderStreamRef.current = stream;
      recorderChunksRef.current = [];
      recorderStopModeRef.current = "save";
      recorderStartedAtRef.current = performance.now();
      setRecordingSeconds(0);

      recorder.ondataavailable = (event) => {
        if (event.data.size) recorderChunksRef.current.push(event.data);
      };
      recorder.onstop = async () => {
        const stopMode = recorderStopModeRef.current;
        recorderStopModeRef.current = "save";
        const durationSeconds = Math.max(
          0.1,
          (performance.now() - recorderStartedAtRef.current) / 1000,
        );
        const blob = new Blob(recorderChunksRef.current, {
          type: recorder.mimeType || "audio/webm",
        });
        recorderChunksRef.current = [];
        stopRecorderResources();
        setRecording(false);
        setRecordingSeconds(0);
        if (stopMode === "discard") {
          setNotice("Запись отменена");
          return;
        }
        if (blob.size) {
          const uploaded = await persistAndUploadRecording(
            blob,
            durationSeconds,
            messageId,
          );
          if (uploaded && stopMode === "confirm") {
            setAutoConfirmDictationId(messageId);
          }
        } else {
          setNotice("Микрофон не передал аудиоданные");
        }
      };

      const context = new AudioContext();
      const analyser = context.createAnalyser();
      analyser.fftSize = 256;
      context.createMediaStreamSource(stream).connect(analyser);
      audioContextRef.current = context;
      const samples = new Uint8Array(analyser.fftSize);
      let lastWaveformSampleAt = 0;
      const draw = (timestamp: number) => {
        if (timestamp - lastWaveformSampleAt >= 85) {
          analyser.getByteTimeDomainData(samples);
          let energy = 0;
          for (const sample of samples) {
            const normalized = (sample - 128) / 128;
            energy += normalized * normalized;
          }
          const level = Math.sqrt(energy / samples.length);
          const nextHeight = Math.max(0.08, Math.min(1, level * 4.2));
          setWaveform((previous) => [
            ...previous.slice(1),
            nextHeight,
          ]);
          lastWaveformSampleAt = timestamp;
        }
        waveformFrameRef.current = window.requestAnimationFrame(draw);
      };
      waveformFrameRef.current = window.requestAnimationFrame(draw);

      recorder.start(1000);
      setRecording(true);
      recorderTimerRef.current = window.setInterval(() => {
        setRecordingSeconds(
          (performance.now() - recorderStartedAtRef.current) / 1000,
        );
      }, 200);
    } catch (error) {
      stopRecorderResources();
      setRecording(false);
      setNotice(
        error instanceof Error
          ? `Микрофон не запущен: ${error.message}`
          : "Микрофон не запущен",
      );
    } finally {
      setStartingRecording(false);
    }
  }

  function stopDictationRecording() {
    recorderStopModeRef.current = "save";
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }
  }

  function finishDictationRecordingToHistory() {
    recorderStopModeRef.current = "confirm";
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }
  }

  function cancelDictationRecording() {
    recorderStopModeRef.current = "discard";
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }
  }

  async function saveCurrentDictationText() {
    if (!currentDictation) return false;
    const response = await fetch(`${API}/dictations/${currentDictation.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: dictationText }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return false;
    }
    setDictationDirty(false);
    return true;
  }

  async function writeClipboardText(
    value: string,
    successMessage = "Текст диктовки скопирован",
  ) {
    try {
      await navigator.clipboard.writeText(value);
      setNotice(successMessage);
    } catch {
      const area = document.createElement("textarea");
      area.value = value;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      setNotice(successMessage);
    }
  }

  async function copyDictationText(value = dictationText, saveCurrent = true) {
    if (saveCurrent && !(await saveCurrentDictationText())) return;
    await writeClipboardText(value);
  }

  async function confirmDictationMessage(
    message: DictationMessage,
    text: string,
    copyAfterConfirm = false,
  ) {
    const clean = text.trim();
    if (!clean) {
      setNotice("Сначала надиктуйте или введите текст");
      return false;
    }
    const response = await fetch(`${API}/dictations/${message.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        status: "confirmed",
      }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return false;
    }
    const nextResponse = await fetch(`${API}/dictations`, { method: "POST" });
    if (!nextResponse.ok) {
      setNotice(await errorMessage(nextResponse));
      return false;
    }
    const next = (await nextResponse.json()) as DictationMessage;
    setActiveDictationId(next.id);
    // Сообщение ушло в историю: поле и отметки вписанных расшифровок начинают
    // новую диктовку с чистого листа.
    loadedDictationRef.current = next.id;
    setDictationMergedIds(new Set());
    setDictationText("");
    setDictationDirty(false);
    if (copyAfterConfirm) {
      await writeClipboardText(
        clean,
        "Сообщение сохранено в истории и скопировано",
      );
    } else {
      setNotice("Сообщение сохранено в истории");
    }
    await refresh();
    setDictationText("");
    setDictationDirty(false);
    return true;
  }

  async function confirmCurrentDictation() {
    if (!currentDictation || currentDictationBusy || savingRecording) return;
    await confirmDictationMessage(currentDictation, dictationText, true);
  }

  useEffect(() => {
    if (!autoConfirmDictationId || autoConfirmInFlightRef.current) return;
    const message = dictations.find(
      (item) => item.id === autoConfirmDictationId,
    );
    if (!message) return;
    const busy = message.fragments.some(
      (fragment) =>
        fragment.status === "queued" || fragment.status === "processing",
    );
    if (savingRecording || busy) return;
    const failed = message.fragments.some(
      (fragment) =>
        fragment.status === "failed" || fragment.status === "cancelled",
    );
    if (failed) {
      // Обновление состояния отложено на микротаск: синхронный setState в теле
      // эффекта запускает каскад рендеров, и eslint справедливо считает это
      // ошибкой. Поведение то же — оба значения меняются сразу после эффекта.
      queueMicrotask(() => {
        setAutoConfirmDictationId("");
        setNotice(
          "Автосохранение остановлено: один из фрагментов не распознан",
        );
      });
      return;
    }
    // В историю уходит ровно то, что человек видит в поле, — вместе с ручными
    // правками. Серверный текст для этого не годится: отложенный PATCH мог
    // ещё не уехать. Пока последняя расшифровка не легла в поле, ждём: она
    // добавится следующим же рендером.
    const current = message.id === currentDictationId;
    const awaitingTranscript =
      current &&
      currentDictationTranscripts.some(
        (item) => !dictationMergedIds.has(item.id),
      );
    if (awaitingTranscript) return;
    const text = current ? dictationText : message.text;
    if (!text.trim()) return;

    autoConfirmInFlightRef.current = true;
    void (async () => {
      try {
        await confirmDictationMessage(message, text, true);
      } finally {
        autoConfirmInFlightRef.current = false;
        setAutoConfirmDictationId("");
      }
    })();
  }, [
    autoConfirmDictationId,
    dictations,
    savingRecording,
    currentDictationId,
    currentDictationTranscripts,
    dictationMergedIds,
    dictationText,
  ]);

  async function retryDictationFragment(fragmentId: string) {
    const response = await fetch(
      `${API}/dictation-fragments/${fragmentId}/retry`,
      { method: "POST" },
    );
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    setNotice("Фрагмент снова поставлен в очередь");
    await refresh();
  }

  async function deleteDictationMessage(message: DictationMessage) {
    if (
      !window.confirm(
        "Удалить сообщение и все сохранённые аудиофрагменты без возможности восстановления?",
      )
    ) {
      return;
    }
    const response = await fetch(`${API}/dictations/${message.id}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    if (activeDictationId === message.id) setActiveDictationId("");
    if (loadedDictationRef.current === message.id) {
      // Удалили сообщение, открытое в поле правки: держать его текст на экране
      // не за чем, восстанавливать его уже некуда.
      loadedDictationRef.current = "";
      setDictationText("");
      setDictationMergedIds(new Set());
      setDictationDirty(false);
    }
    setEditingHistoryId("");
    await refresh();
  }

  async function clearDictationHistory() {
    if (!dictationTotal) {
      setNotice("История диктовок уже пустая");
      return;
    }
    if (
      !window.confirm(
        "Очистить подтверждённую историю диктовок и удалить её исходные аудиофайлы?",
      )
    ) {
      return;
    }
    const response = await fetch(`${API}/dictations/history`, {
      method: "DELETE",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    const result = (await response.json()) as { deleted: number };
    setNotice(`Удалено сообщений: ${result.deleted}`);
    await refresh();
  }

  async function saveHistoryMessage(message: DictationMessage) {
    const response = await fetch(`${API}/dictations/${message.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: historyTextDraft }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    setEditingHistoryId("");
    setNotice("Сообщение исправлено");
    await refresh();
  }

  function addFiles(incoming: FileList | File[]) {
    const next = Array.from(incoming);
    if (next.length && settings.default_import_mode === "reference") {
      // Перетаскивание отдаёт браузеру только содержимое файла, без пути на
      // диске. Поэтому режим «оставлять на месте» здесь физически недостижим:
      // файл всё равно будет скопирован в проект. Молчать об этом нельзя —
      // человек включил режим ровно для того, чтобы копий не было.
      setNotice(
        "Перетащенные файлы копируются: путь к исходнику браузер не передаёт. " +
          "Чтобы оставить файлы на месте, добавьте их кнопкой выбора файлов.",
      );
    }
    setFiles((current) => {
      const known = new Set(
        current.map((file) => `${file.name}:${file.size}:${file.lastModified}`),
      );
      return [
        ...current,
        ...next.filter(
          (file) => !known.has(`${file.name}:${file.size}:${file.lastModified}`),
        ),
      ];
    });
  }

  async function createProject(event: FormEvent) {
    event.preventDefault();
    const response = await fetch(`${API}/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: newProjectTitle.trim() || null,
        folder_id: newProjectFolder || null,
      }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    const project = (await response.json()) as Project;
    setCreateOpen(false);
    setNewProjectTitle("");
    setNewProjectFolder("");
    await refresh();
    openProject(project.id);
  }

  async function shutdownService() {
    if (shuttingDown) return;
    setShuttingDown(true);
    try {
      const response = await fetch(`${API}/system/shutdown`, {
        method: "POST",
        headers: { "X-Whisper-Local": "shutdown" },
      });
      if (!response.ok) {
        setNotice(await errorMessage(response));
        setShuttingDown(false);
        return;
      }
      setOnline(false);
      window.setTimeout(() => window.close(), 900);
    } catch {
      setNotice("Не удалось остановить сервис");
      setShuttingDown(false);
    }
  }

  function openUrlImporter(projectId = "") {
    const targetProject = projectId
      ? projects.find((project) => project.id === projectId)
      : null;
    const recoverable = urlDownloads.find(
      (download) =>
        (!projectId || download.project_id === projectId) &&
        (activeUrlStatuses.includes(download.status) ||
          download.status === "paused" ||
          download.status === "interrupted" ||
          (download.status === "error" && download.resumable)),
    );
    setUrlTargetProjectId(projectId);
    setActiveUrlDownloadId(recoverable?.project_id || "");
    setUrlFolderId(
      targetProject
        ? targetProject.folder_id || ""
        : folderFilter !== "all" && folderFilter !== "none"
          ? folderFilter
          : "",
    );
    if (!recoverable) {
      setUrlValue("");
      setUrlMetadata(null);
      setUrlProjectName(targetProject?.name || "");
      setUrlQuality("auto");
      setUrlLanguage(settings.default_language);
      setUrlRecognitionQuality(settings.default_quality);
      setUrlDiarizationEngine(settings.diarization_engine);
      setUrlDiarization(
        settings.default_diarization === "on" &&
          engineReady(diarizationStatus, settings.diarization_engine),
      );
      // Ставить ли расшифровку сразу — из настроек. Когда видеокарта занята
      // надолго, значение выключают один раз, а не снимают галочку у каждой
      // из сотни ссылок.
      setUrlTranscribe(settings.download_transcribe === "on");
      setUrlError("");
    }
    setUrlImportOpen(true);
  }

  async function probeUrl() {
    const cleanUrl = urlValue.trim();
    if (!cleanUrl || urlProbing) return;
    setUrlProbing(true);
    setUrlError("");
    try {
      const response = await fetch(`${API}/url-downloads/probe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: cleanUrl,
          passcode: urlPasscode.trim() || null,
        }),
      });
      if (!response.ok) {
        setUrlError(await errorMessage(response));
        setUrlMetadata(null);
        return;
      }
      const metadata = (await response.json()) as UrlMetadata;
      setUrlMetadata(metadata);
      setUrlProjectName(urlTargetProjectId ? "" : metadata.title);
      setUrlQuality("auto");
    } catch {
      setUrlError("Не удалось проверить ссылку");
      setUrlMetadata(null);
    } finally {
      setUrlProbing(false);
    }
  }

  async function startUrlDownload(event: FormEvent) {
    event.preventDefault();
    if (!urlMetadata || urlStarting) {
      if (!urlMetadata) await probeUrl();
      return;
    }
    setUrlStarting(true);
    setUrlError("");
    try {
      const response = await fetch(`${API}/url-downloads`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: urlValue.trim(),
          passcode: urlPasscode.trim() || null,
          quality: urlQuality,
          project_id: urlTargetProjectId || null,
          project_name: urlTargetProjectId
            ? null
            : urlProjectName.trim() || urlMetadata.title,
          folder_id: urlTargetProjectId ? null : urlFolderId || null,
          transcription_language: urlLanguage,
          transcription_quality: urlRecognitionQuality,
          transcription_diarization: urlDiarization,
          transcription_diarization_engine: urlDiarizationEngine,
          transcribe: urlTranscribe,
        }),
      });
      if (!response.ok) {
        setUrlError(await errorMessage(response));
        return;
      }
      const result = (await response.json()) as {
        project: Project;
        download: UrlDownload;
        metadata: UrlMetadata;
      };
      setActiveUrlDownloadId(result.download.project_id);
      setUrlDownloads((current) => [
        result.download,
        ...current.filter(
          (item) => item.project_id !== result.download.project_id,
        ),
      ]);
      setNotice(
        urlTargetProjectId
          ? "Видео скачивается в текущий проект"
          : "Проект создан, видео скачивается",
      );
      await refresh();
    } catch {
      setUrlError("Не удалось начать скачивание");
    } finally {
      setUrlStarting(false);
    }
  }

  async function controlUrlDownload(
    action: "pause" | "resume" | "cancel",
  ) {
    if (!activeUrlDownload) return;
    if (
      action === "cancel" &&
      !window.confirm(
        "Остановить скачивание? Уже загруженная часть файла будет удалена.",
      )
    ) {
      return;
    }
    setUrlError("");
    try {
      const response = await fetch(
        `${API}/url-downloads/${activeUrlDownload.project_id}/${action}`,
        { method: "POST" },
      );
      if (!response.ok) {
        setUrlError(await errorMessage(response));
        return;
      }
      const next = (await response.json()) as UrlDownload;
      setUrlDownloads((current) => [
        next,
        ...current.filter(
          (item) => item.project_id !== next.project_id,
        ),
      ]);
      await refresh();
    } catch {
      setUrlError("Сервис не ответил на команду");
    }
  }

  function targetFolderId() {
    return folderFilter !== "all" && folderFilter !== "none"
      ? folderFilter
      : null;
  }

  async function createBlankProject() {
    const response = await fetch(`${API}/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: null, folder_id: targetFolderId() }),
    });
    if (!response.ok) throw new Error(await errorMessage(response));
    return (await response.json()) as Project;
  }

  async function stageBrowserFiles(projectId: string, incoming: File[]) {
    const body = new FormData();
    incoming.forEach((file) => body.append("files", file));
    body.append("session_id", projectId);
    const response = await fetch(`${API}/media`, { method: "POST", body });
    if (!response.ok) throw new Error(await errorMessage(response));
    return (await response.json()) as StagedMedia[];
  }

  async function quickCreateProject(incoming: FileList | File[]) {
    const selected = Array.from(incoming);
    if (!selected.length || quickUploading) return;
    setQuickUploading(true);
    try {
      const project = await createBlankProject();
      await stageBrowserFiles(project.id, selected);
      await refresh();
      openProject(project.id);
      setNotice(
        `Проект создан: ${selected.length} файл${selected.length === 1 ? "" : "а"} проверены`,
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Неизвестная ошибка");
    } finally {
      setQuickUploading(false);
    }
  }

  async function pickLocalFiles(projectId = activeProjectId) {
    if (!projectId || uploading) return [] as StagedMedia[];
    setUploading(true);
    try {
      const response = await fetch(`${API}/local-files/pick`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: projectId }),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const selected = (await response.json()) as StagedMedia[];
      await refresh();
      if (selected.length) {
        setNotice(
          `${selected.length} файл${selected.length === 1 ? "" : "а"} подключены без копирования`,
        );
      }
      return selected;
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Неизвестная ошибка");
      return [] as StagedMedia[];
    } finally {
      setUploading(false);
    }
  }

  async function quickPickLocalFiles() {
    if (quickUploading) return;
    setQuickUploading(true);
    let project: Project | null = null;
    try {
      project = await createBlankProject();
      const response = await fetch(`${API}/local-files/pick`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: project.id }),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const selected = (await response.json()) as StagedMedia[];
      if (!selected.length) {
        await fetch(`${API}/projects/${project.id}`, { method: "DELETE" });
        await refresh();
        return;
      }
      await refresh();
      openProject(project.id);
      setNotice("Проект создан, исходники остаются на своих местах");
    } catch (error) {
      if (project) {
        await fetch(`${API}/projects/${project.id}`, { method: "DELETE" });
      }
      setNotice(error instanceof Error ? error.message : "Неизвестная ошибка");
    } finally {
      setQuickUploading(false);
    }
  }

  function chooseQuickFiles() {
    if (quickUploading) return;
    if (settings.default_import_mode === "reference") {
      quickPickLocalFiles();
    } else {
      quickInputRef.current?.click();
    }
  }

  function chooseProjectFiles() {
    if (uploading) return;
    if (settings.default_import_mode === "reference") {
      pickLocalFiles();
    } else {
      inputRef.current?.click();
    }
  }

  function startProjectEdit(project: Project) {
    setEditingProjectId(project.id);
    setProjectNameDraft(project.name);
    setProjectFolderDraft(project.folder_id || "");
  }

  function cancelProjectEdit() {
    setEditingProjectId("");
    setProjectNameDraft("");
    setProjectFolderDraft("");
  }

  async function saveProjectName(project: Project) {
    const name = projectNameDraft.trim();
    if (!name) {
      setNotice("Название проекта не может быть пустым");
      return;
    }
    const response = await fetch(`${API}/projects/${project.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        folder_id: projectFolderDraft || null,
      }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    cancelProjectEdit();
    setNotice("Название проекта сохранено");
    await refresh();
  }

  async function deleteProject(project: Project) {
    if (
      !window.confirm(
        `Удалить проект «${project.name}» вместе с управляемыми файлами и результатами? Оригиналы, подключённые без копирования, останутся на своих местах.`,
      )
    ) {
      return;
    }
    const response = await fetch(`${API}/projects/${project.id}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    if (project.id === activeProjectId) {
      setActiveProjectId("");
      setScreen("projects");
    }
    cancelProjectEdit();
    setNotice("Проект удалён");
    await refresh();
  }

  async function createFolder() {
    const name = window.prompt("Название новой папки");
    if (!name?.trim()) return;
    const response = await fetch(`${API}/folders`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (!response.ok) setNotice(await errorMessage(response));
    await refresh();
  }

  function startFolderEdit(folder: Folder) {
    setEditingFolderId(folder.id);
    setFolderNameDraft(folder.name);
    setFolderFeedDraft(folder.feed_visible !== false);
  }

  function cancelFolderEdit() {
    setEditingFolderId("");
    setFolderNameDraft("");
    setFolderFeedDraft(true);
  }

  async function saveFolder(folder: Folder) {
    const name = folderNameDraft.trim();
    if (!name) {
      setNotice("Название папки не может быть пустым");
      return;
    }
    const feedChanged = folderFeedDraft !== (folder.feed_visible !== false);
    const response = await fetch(`${API}/folders/${folder.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, feed_visible: folderFeedDraft }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    cancelFolderEdit();
    setNotice(
      feedChanged
        ? folderFeedDraft
          ? "Папка снова в общей ленте"
          : "Папка убрана из общей ленты — открыть её можно слева"
        : "Название папки сохранено",
    );
    await refresh();
  }

  async function deleteFolder(folder: Folder) {
    if (
      !window.confirm(
        `Удалить папку «${folder.name}» и все ${folder.project_count} проектов в ней вместе с управляемыми файлами и результатами? Оригиналы, подключённые без копирования, останутся на своих местах.`,
      )
    ) {
      return;
    }
    const response = await fetch(
      `${API}/folders/${folder.id}?delete_projects=true`,
      {
      method: "DELETE",
      },
    );
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    if (folderFilter === folder.id) showFolder("all");
    if (
      activeProject &&
      activeProject.folder_id === folder.id
    ) {
      setActiveProjectId("");
      setScreen("projects");
    }
    cancelFolderEdit();
    setNotice("Папка и проекты удалены");
    await refresh();
  }

  async function uploadAndProbe(event: FormEvent) {
    event.preventDefault();
    if (!files.length || !activeProjectId || uploading) return;
    setUploading(true);
    const body = new FormData();
    files.forEach((file) => body.append("files", file));
    body.append("session_id", activeProjectId);
    try {
      const response = await fetch(`${API}/media`, { method: "POST", body });
      if (!response.ok) throw new Error(await errorMessage(response));
      const count = files.length;
      setFiles([]);
      setNotice(
        `${count} ${count === 1 ? "файл проверен" : "файла проверены"}. Выберите дорожки и поставьте в очередь.`,
      );
      await refresh();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Неизвестная ошибка");
    } finally {
      setUploading(false);
    }
  }

  async function enqueueMedia() {
    if (!projectMedia.length || queueing) return;
    setQueueing(true);
    try {
      const response = await fetch(`${API}/media/enqueue`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: projectMedia.map((item) => {
            const trim = trimRanges[item.id];
            return {
              media_id: item.id,
              audio_stream_index:
                trackSelections[item.id] ?? item.audio_streams[0].index,
              trim_start_seconds: trim ? Math.round(trim.start * 1000) / 1000 : 0,
              trim_end_seconds: trim ? Math.round(trim.end * 1000) / 1000 : null,
            };
          }),
          language,
          quality,
          timestamps,
          formatting,
          diarization,
          diarization_engine: diarizationEngine,
        }),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const count = projectMedia.length;
      setTrimRanges((current) => {
        const next = { ...current };
        projectMedia.forEach((item) => delete next[item.id]);
        return next;
      });
      setNotice(
        `${count} ${count === 1 ? "файл добавлен" : "файла добавлены"} в общую очередь`,
      );
      await refresh();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Неизвестная ошибка");
    } finally {
      setQueueing(false);
    }
  }

  async function removeMedia(item: StagedMedia) {
    const response = await fetch(`${API}/media/${item.id}`, {
      method: "DELETE",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    setTrimRanges((current) => {
      const next = { ...current };
      delete next[item.id];
      return next;
    });
    await refresh();
  }

  function applySettingToCurrentControls<K extends keyof Settings>(
    key: K,
    value: Settings[K],
  ) {
    if (key === "default_language") setLanguage(value as JobOptions["language"]);
    if (key === "default_quality") setQuality(value as JobOptions["quality"]);
    if (key === "default_timestamps") {
      setTimestamps(value as JobOptions["timestamps"]);
    }
    if (key === "default_formatting") {
      setFormatting(value as JobOptions["formatting"]);
    }
    if (key === "default_view_timestamps") {
      setViewTimestamps(value as Settings["default_view_timestamps"]);
    }
    if (key === "default_view_formatting") {
      setViewFormatting(value as Settings["default_view_formatting"]);
    }
    if (key === "default_view_speakers") {
      setViewSpeakers(value as Settings["default_view_speakers"]);
    }
    if (key === "default_diarization") {
      setDiarization(value === "on");
    }
    if (key === "diarization_engine") {
      setDiarizationEngine(value as DiarizationEngine);
      setMissingSpeakersEngine(value as DiarizationEngine);
    }
    if (key === "dictation_default_language") {
      setDictationLanguage(value as Settings["dictation_default_language"]);
    }
    if (key === "dictation_default_quality") {
      setDictationQuality(value as Settings["dictation_default_quality"]);
    }
    if (key === "download_transcribe") {
      setUrlTranscribe(value === "on");
    }
    // Новое постоянное значение отменяет выбор «на сейчас» — иначе человек
    // поменял бы настройку и не увидел никакой разницы в ленте.
    if (key === "projects_page_size") {
      setProjectsPageSizeChoice("");
      setProjectsPage(1);
    }
    if (key === "dictation_page_size") {
      setDictationPageSizeChoice("");
      setDictationPage(1);
    }
  }

  async function saveSetting<K extends keyof Settings>(
    key: K,
    value: Settings[K],
  ) {
    const previousSettings = settings;
    setSettings((current) => ({ ...current, [key]: value }));
    try {
      const response = await fetch(`${API}/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: value }),
      });
      if (!response.ok) {
        setNotice(await errorMessage(response));
        setSettings(previousSettings);
        await refresh();
        return;
      }
      const savedSettings = (await response.json()) as Settings;
      setSettings(savedSettings);
      applySettingToCurrentControls(key, savedSettings[key]);
    } catch {
      setNotice("Не удалось сохранить настройку");
      setSettings(previousSettings);
      await refresh();
      return;
    }
    setNotice("Настройка сохранена");
  }

  async function pickStorageFolder(
    key: "managed_storage_root" | "temporary_storage_root",
  ) {
    const response = await fetch(`${API}/settings/pick-folder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    setSettings((await response.json()) as Settings);
    setNotice("Папка хранения сохранена");
  }

  async function openFolder(path: string) {
    const response = await fetch(`${API}/system/open-folder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!response.ok) setNotice(await errorMessage(response));
  }

  async function loadComponents(refresh = false) {
    setComponentsBusy(true);
    try {
      const response = await fetch(
        `${API}/components${refresh ? "?refresh=true" : ""}`,
      );
      if (!response.ok) throw new Error(await errorMessage(response));
      setComponents((await response.json()) as ComponentsInventory);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Неизвестная ошибка");
    } finally {
      setComponentsBusy(false);
    }
  }

  async function loadServices() {
    const response = await fetch(`${API}/integrations`);
    if (!response.ok) return;
    setServices((await response.json()) as ConnectedService[]);
  }

  async function downloadModels() {
    const response = await fetch(`${API}/components/models/download`, {
      method: "POST",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    setModelDownload((await response.json()) as ModelDownload);
  }

  async function revealExtractedAudio(jobId: string) {
    const response = await fetch(`${API}/jobs/${jobId}/reveal-audio`, {
      method: "POST",
    });
    if (!response.ok) setNotice(await errorMessage(response));
  }

  async function dropExtractedAudio(jobId: string) {
    // Расшифровка при этом остаётся: удаляется только копия аудио, чтобы
    // освободить место. Поэтому подтверждение здесь не нужно.
    const response = await fetch(`${API}/jobs/${jobId}/extracted-audio`, {
      method: "DELETE",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    setNotice("Извлечённое аудио удалено, расшифровка осталась");
    await refresh();
  }

  /* Показать запись диктовки в проводнике — с выделенным файлом.
   *
   * Просто открыть папку было мало: в ней лежат все фрагменты сообщения, и
   * какой из них нужен — по именам не понять. А достать исходное аудио из
   * диктовки иначе было нечем: путь к нему нигде не показывался. */
  async function revealDictationAudio(fragmentId: string) {
    const response = await fetch(
      `${API}/dictation-fragments/${fragmentId}/reveal`,
      { method: "POST" },
    );
    if (!response.ok) setNotice(await errorMessage(response));
  }

  async function revealSource(jobId: string) {
    const response = await fetch(`${API}/jobs/${jobId}/reveal-source`, {
      method: "POST",
    });
    if (!response.ok) setNotice(await errorMessage(response));
  }

  async function cancelJob(job: Job) {
    const isRerun = Boolean(job.options._rerun_mode);
    if (
      job.status === "processing" &&
      !window.confirm(
        `Остановить обработку «${job.original_name}»?\n\n` +
          (isRerun
            ? "Предыдущий готовый результат останется на месте."
            : "Уже проделанная работа по этому файлу не сохранится, " +
              "но исходный файл останется на месте и его можно будет " +
              "поставить в очередь заново."),
      )
    ) {
      return;
    }
    setStoppingIds((current) => new Set(current).add(job.id));
    const response = await fetch(`${API}/jobs/${job.id}/cancel`, {
      method: "POST",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
    } else {
      setNotice(
        job.status !== "processing"
          ? "Файл снят с очереди"
          : job.progress <= 0.02
            ? "Останавливаю. Файл ещё готовится — на многочасовой записи остановка займёт пару минут"
            : "Останавливаю: файл освободит очередь через несколько секунд",
      );
    }
    await refresh();
    setStoppingIds((current) => {
      const next = new Set(current);
      next.delete(job.id);
      return next;
    });
  }

  async function requeueJob(jobId: string) {
    const response = await fetch(`${API}/jobs/${jobId}/requeue`, {
      method: "POST",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    setNotice("Файл снова поставлен в очередь");
    await refresh();
  }

  async function diarizeProjectMissingSpeakers() {
    if (speakerDiarizationStarting) return;
    if (!completedJobsWithoutSpeakers.length) return;
    if (!engineReady(diarizationStatus, missingSpeakersEngine)) {
      setNotice(
        `${diarizationEngineLabels[missingSpeakersEngine]} не готов. Откройте Настройки → Спикеры`,
      );
      return;
    }
    setSpeakerDiarizationStarting(true);
    try {
      for (const job of completedJobsWithoutSpeakers) {
        const response = await fetch(`${API}/jobs/${job.id}/diarize`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ engine: missingSpeakersEngine }),
        });
        if (!response.ok) {
          setNotice(await errorMessage(response));
          return;
        }
      }
      setNotice(
        completedJobsWithoutSpeakers.length === 1
          ? "Разметка спикеров поставлена в общую очередь"
          : `Разметка спикеров поставлена для ${completedJobsWithoutSpeakers.length} файлов`,
      );
      await refresh();
    } finally {
      setSpeakerDiarizationStarting(false);
    }
  }

  function openJobRerun(job: Job) {
    if (job.options.quality !== "fast") return;
    setRerunJobId(job.id);
    setRerunLanguage(job.options.language);
    setRerunQuality("accurate");
    setRerunDiarization(Boolean(job.options.diarization));
    setRerunDiarizationEngine(
      job.options.diarization_engine || settings.diarization_engine,
    );
  }

  function openRediarize(job: Job) {
    setRediarizeJobId(job.id);
    setRediarizeEngine(settings.diarization_engine);
  }

  async function rediarizeCompletedJob(event: FormEvent) {
    event.preventDefault();
    if (!rediarizeJob || rediarizeStarting) return;
    setRediarizeStarting(true);
    try {
      const response = await fetch(`${API}/jobs/${rediarizeJob.id}/diarize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ engine: rediarizeEngine }),
      });
      if (!response.ok) {
        setNotice(await errorMessage(response));
        return;
      }
      setRediarizeJobId("");
      setNotice(
        `Разметка спикеров (${diarizationEngineLabels[rediarizeEngine]}) поставлена в общую очередь`,
      );
      await refresh();
    } finally {
      setRediarizeStarting(false);
    }
  }

  async function rerunCompletedJob(event: FormEvent) {
    event.preventDefault();
    if (!rerunJob || rerunStarting) return;
    setRerunStarting(true);
    const response = await fetch(`${API}/jobs/${rerunJob.id}/rerun`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        language: rerunLanguage,
        quality: rerunQuality,
        diarization: rerunDiarization,
        diarization_engine: rerunDiarizationEngine,
      }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      setRerunStarting(false);
      return;
    }
    setRerunJobId("");
    setRerunStarting(false);
    setNotice("Точное распознавание поставлено в общую очередь");
    await refresh();
  }

  async function cancelDictationFragment(fragment: DictationFragment) {
    setStoppingIds((current) => new Set(current).add(fragment.id));
    const response = await fetch(
      `${API}/dictation-fragments/${fragment.id}/cancel`,
      { method: "POST" },
    );
    if (!response.ok) {
      setNotice(await errorMessage(response));
    } else {
      setNotice("Останавливаю распознавание фрагмента. Аудио сохранено");
    }
    await refresh();
    setStoppingIds((current) => {
      const next = new Set(current);
      next.delete(fragment.id);
      return next;
    });
  }

  function toggleProjectSelection(projectId: string) {
    setSelectedProjects((current) => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  }

  function leaveSelectMode() {
    setSelectMode(false);
    setSelectedProjects(new Set());
  }

  /** Показать папку и снять отметки, оставшиеся от предыдущей.
   *
   * Отметки принадлежат конкретному фильтру. Унеси их в другую папку — и
   * «Удалить N» пообещает стереть то, чего на экране нет. Единственный вход
   * для смены фильтра, чтобы сброс нельзя было забыть.
   */
  function showFolder(filter: string) {
    setFolderFilter(filter);
    setSelectedProjects(new Set());
    // У каждой папки свой список — начинать его надо с начала.
    setProjectsPage(1);
  }

  async function bulkMoveProjects(folderId: string | null) {
    if (!selectedVisibleIds.length) return;
    setBulkBusy(true);
    try {
      const response = await fetch(`${API}/projects/bulk/folder`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ids: selectedVisibleIds,
          folder_id: folderId,
        }),
      });
      if (!response.ok) {
        setNotice(await errorMessage(response));
        return;
      }
      const result = (await response.json()) as {
        moved: number;
        skipped: { id: string; reason: string }[];
      };
      setNotice(
        result.skipped.length
          ? `Перенесено: ${result.moved}. Пропущено: ${result.skipped.length} — ${result.skipped[0].reason}`
          : `Перенесено проектов: ${result.moved}`,
      );
      leaveSelectMode();
      await refresh();
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkDeleteProjects() {
    const count = selectedVisibleIds.length;
    if (!count) return;
    if (
      !window.confirm(
        `Удалить ${count} ${count === 1 ? "проект" : "проектов"} вместе с файлами и результатами? Это необратимо.`,
      )
    ) {
      return;
    }
    setBulkBusy(true);
    try {
      const response = await fetch(`${API}/projects/bulk/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: selectedVisibleIds }),
      });
      if (!response.ok) {
        setNotice(await errorMessage(response));
        return;
      }
      const result = (await response.json()) as {
        deleted: number;
        skipped: { id: string; reason: string }[];
      };
      setNotice(
        result.skipped.length
          ? `Удалено: ${result.deleted}. Пропущено: ${result.skipped.length} — ${result.skipped[0].reason}`
          : `Удалено проектов: ${result.deleted}`,
      );
      leaveSelectMode();
      await refresh();
    } finally {
      setBulkBusy(false);
    }
  }

  async function saveDictationToProject(message: DictationMessage) {
    const suggested = message.text.trim().slice(0, 60);
    const name = window.prompt(
      "Название проекта для этой диктовки:",
      suggested || "Диктовка",
    );
    if (name === null) return;
    const response = await fetch(`${API}/dictations/${message.id}/to-project`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() || null }),
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    const result = (await response.json()) as {
      project: Project;
      files: number;
      text_edited: boolean;
    };
    setNotice(
      result.text_edited
        ? `Сохранено в проект «${result.project.name}». Правки текста остались только в диктовке — в проект ушли расшифровки фрагментов`
        : `Сохранено в проект «${result.project.name}» · файлов: ${result.files}`,
    );
    await refresh();
  }

  async function removeDictationFragment(fragment: DictationFragment) {
    // Подтверждение только когда есть что терять: у готового фрагмента уже
    // есть расшифровка, у пустого или неудачного терять нечего.
    const valuable = fragment.status === "completed" && fragment.transcript;
    if (
      valuable &&
      !window.confirm(
        "Убрать этот фрагмент? Его текст исчезнет из диктовки, а аудио будет удалено.",
      )
    ) {
      return;
    }
    setStoppingIds((current) => new Set(current).add(fragment.id));
    const response = await fetch(
      `${API}/dictation-fragments/${fragment.id}`,
      { method: "DELETE" },
    );
    if (!response.ok) {
      setNotice(await errorMessage(response));
    } else {
      // Поле правки серверный текст больше не догоняет, поэтому расшифровку
      // убранного фрагмента вырезаем из него здесь же. Отметка «вписан»
      // остаётся: до ответа опроса фрагмент ещё числится готовым, и без неё
      // его расшифровка тут же вернулась бы в поле.
      setDictationText((current) =>
        textWithoutFragment(current, fragment.transcript),
      );
      setDictationDirty(true);
      setNotice("Фрагмент убран из диктовки");
    }
    await refresh();
    setStoppingIds((current) => {
      const next = new Set(current);
      next.delete(fragment.id);
      return next;
    });
  }

  function dictationWait(fragmentId: string): string {
    // Что именно держит фрагмент. Разметка спикеров — отдельный процесс
    // Pyannote, прервать его посередине нельзя, поэтому на ней диктовка
    // действительно ждёт. Транскрибация файла уступает за один сегмент.
    const active = queueOverview.active;
    const position = queueOverview.waiting.findIndex(
      (item) => item.id === fragmentId,
    );
    if (position > 0) return ` · ${position + 1}-я в очереди диктовки`;
    if (active?.operation === "Разметка спикеров") {
      return " · ждёт окончания разметки спикеров";
    }
    if (active) return " · вот-вот начнётся";
    return "";
  }

  async function cancelQueueItem(item: QueueItem) {
    if (item.kind === "job") {
      const job = jobs.find((candidate) => candidate.id === item.id);
      if (job) {
        await cancelJob(job);
        return;
      }
    } else {
      const fragment = dictationFragments.find(
        (candidate) => candidate.id === item.id,
      );
      if (fragment) {
        await cancelDictationFragment(fragment);
        return;
      }
    }

    setStoppingIds((current) => new Set(current).add(item.id));
    const resource =
      item.kind === "job" ? "jobs" : "dictation-fragments";
    const response = await fetch(`${API}/${resource}/${item.id}/cancel`, {
      method: "POST",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
    } else {
      setNotice(
        item.status === "queued"
          ? "Задача снята с очереди"
          : "Останавливаю текущую задачу",
      );
    }
    await refresh();
    setStoppingIds((current) => {
      const next = new Set(current);
      next.delete(item.id);
      return next;
    });
  }

  async function prioritizeQueueItem(item: QueueItem) {
    if (
      item.status !== "queued" ||
      item.priority ||
      prioritizingIds.has(item.id)
    ) {
      return;
    }
    setPrioritizingIds((current) => new Set(current).add(item.id));
    const resource =
      item.kind === "job" ? "jobs" : "dictation-fragments";
    const response = await fetch(`${API}/${resource}/${item.id}/prioritize`, {
      method: "POST",
    });
    if (!response.ok) {
      setNotice(await errorMessage(response));
    } else {
      setQueueOverview((await response.json()) as QueueOverview);
      setNotice("Задача перенесена в начало очереди");
    }
    await refresh();
    setPrioritizingIds((current) => {
      const next = new Set(current);
      next.delete(item.id);
      return next;
    });
  }

  function variantQuery(selectedOnly = false) {
    const query = new URLSearchParams({
      timestamps: viewTimestamps,
      formatting: viewFormatting,
      speakers: "on",
    });
    if (selectedOnly && speakerFilterValue) query.set("speaker_keys", speakerFilterValue);
    return query.toString();
  }

  function toggleSpeaker(key: string) {
    setSelectedSpeakerKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const stopSpeakerSample = useCallback(() => {
    speakerSampleRequestRef.current = "";
    const audio = speakerSampleAudioRef.current;
    if (audio) {
      audio.onended = null;
      audio.pause();
    }
    speakerSampleAudioRef.current = null;
    if (speakerSampleUrlRef.current) {
      URL.revokeObjectURL(speakerSampleUrlRef.current);
      speakerSampleUrlRef.current = "";
    }
    setSpeakerSampleKey("");
    setSpeakerSampleLoadingKey("");
  }, []);

  // Образец играет только на странице своего проекта: ушли — замолчал.
  useEffect(
    () => stopSpeakerSample,
    [activeProjectId, screen, stopSpeakerSample],
  );

  async function toggleSpeakerSample(speaker: SpeakerEntry) {
    const busy =
      speakerSampleKey === speaker.key ||
      speakerSampleLoadingKey === speaker.key;
    stopSpeakerSample();
    if (busy) return;
    const requestKey = speaker.key;
    speakerSampleRequestRef.current = requestKey;
    setSpeakerSampleLoadingKey(requestKey);
    try {
      // Образец целиком, а не потоком: это полминуты MP3 на 96 кбит/с, около
      // 400 КБ. Зато ошибка приходит текстом, а не молчащим плеером.
      const response = await fetch(
        `${API}/jobs/${encodeURIComponent(speaker.job_id)}/speakers/${encodeURIComponent(speaker.speaker_id)}/sample`,
        { cache: "no-store" },
      );
      if (!response.ok) throw new Error(await errorMessage(response));
      const blob = await response.blob();
      if (speakerSampleRequestRef.current !== requestKey) return;
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      speakerSampleUrlRef.current = url;
      speakerSampleAudioRef.current = audio;
      audio.onended = () => stopSpeakerSample();
      await audio.play();
      if (speakerSampleRequestRef.current !== requestKey) return;
      setSpeakerSampleKey(requestKey);
      setSpeakerSampleLoadingKey("");
    } catch (error) {
      if (speakerSampleRequestRef.current !== requestKey) return;
      stopSpeakerSample();
      setNotice(
        error instanceof Error ? error.message : "Не удалось воспроизвести",
      );
    }
  }

  async function downloadNemotron() {
    if (nemotronDownloadStarting) return;
    setNemotronDownloadStarting(true);
    try {
      const response = await fetch(`${API}/diarization/nemotron/download`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      setDiarizationStatus((await response.json()) as DiarizationStatus);
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "Не удалось начать загрузку",
      );
    } finally {
      setNemotronDownloadStarting(false);
    }
  }

  function startSpeakerEdit(speaker: SpeakerEntry) {
    // Blur обычно уже сохранил предыдущее поле; этот вызов покрывает
    // переходы, при которых браузер не успел передать фокус.
    if (speakerEditRef.current) saveSpeakerName(speakerEditRef.current.speaker);
    const name = speakerDraftsRef.current.get(speaker.key) ?? (speaker.custom_name || speaker.name);
    speakerEditRef.current = { speaker, projectId: activeProjectId, name };
    setEditingSpeakerKey(speaker.key);
    setSpeakerNameDraft(name);
  }

  function cancelSpeakerEdit() {
    if (speakerEditRef.current) {
      speakerDraftsRef.current.delete(speakerEditRef.current.speaker.key);
    }
    speakerEditRef.current = null;
    setEditingSpeakerKey("");
    setSpeakerNameDraft("");
  }

  function saveSpeakerName(speaker: SpeakerEntry) {
    const edit = speakerEditRef.current;
    if (!edit || edit.speaker.key !== speaker.key) return;
    const name = edit.name.trim();
    speakerDraftsRef.current.set(speaker.key, edit.name);
    speakerEditRef.current = null;
    setEditingSpeakerKey((current) => current === speaker.key ? "" : current);
    // Снимок имени и проекта не зависит от уже открытого следующего поля.
    // Последовательная запись одного спикера не даёт старому ответу затереть новый.
    const previous = speakerSavesRef.current.get(speaker.key) ?? Promise.resolve();
    const saving = previous.then(async () => {
      try {
        const response = await fetch(
          `${API}/projects/${edit.projectId}/speakers/${encodeURIComponent(speaker.job_id)}/${encodeURIComponent(speaker.speaker_id)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
          },
        );
        if (!response.ok) throw new Error(await errorMessage(response));
        const updated = (await response.json()) as SpeakerEntry;
        setProjectSpeakers((current) =>
          current.map((item) => (item.key === updated.key ? updated : item)),
        );
        if (speakerDraftsRef.current.get(speaker.key) === edit.name) {
          speakerDraftsRef.current.delete(speaker.key);
        }
      } catch (error) {
        setNotice(`Не удалось сохранить имя «${name}»: ${error instanceof Error ? error.message : "ошибка сети"}. Введённое имя сохранено в поле переименования — повторите попытку.`);
      }
    });
    speakerSavesRef.current.set(speaker.key, saving);
    void saving.finally(() => {
      if (speakerSavesRef.current.get(speaker.key) === saving) {
        speakerSavesRef.current.delete(speaker.key);
      }
    });
  }

  async function activateDiarization() {
    if (!diarizationToken.trim() || activatingDiarization) return;
    setActivatingDiarization(true);
    try {
      const response = await fetch(`${API}/diarization/activate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: diarizationToken.trim() }),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      setDiarizationStatus(
        (await response.json()) as DiarizationStatus,
      );
      setDiarizationToken("");
      setNotice("Разделение по спикерам активировано");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Ошибка активации");
    } finally {
      setActivatingDiarization(false);
    }
  }

  async function copyPlainText(value: string, label: string) {
    // Отдельно от copyText: тот забирает текст по ссылке с сервера, а здесь
    // копируется готовая строка — например команда установки.
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setNotice(`${label} скопирована`);
    } catch {
      setNotice("Браузер не дал доступ к буферу обмена");
    }
  }

  async function copyText(url: string, label: string) {
    const response = await fetch(url);
    if (!response.ok) {
      setNotice(await errorMessage(response));
      return;
    }
    const text = await response.text();
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await Promise.race([
          navigator.clipboard.writeText(text),
          new Promise((_, reject) =>
            window.setTimeout(
              () => reject(new Error("Clipboard timeout")),
              1200,
            ),
          ),
        ]);
        copied = true;
      } catch {
        copied = false;
      }
    }
    if (!copied) {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      copied = document.execCommand("copy");
      area.remove();
    }
    setNotice(
      copied
        ? `${label} скопирован в буфер обмена`
        : "Браузер запретил доступ к буферу обмена",
    );
  }

  function renderProjectCard(project: Project) {
    const total = project.job_count || 0;
    const progress = total
      ? (project.completed_count / total) * 100
      : project.download_percent || 0;
    const hasDownloadProgress =
      Boolean(project.download_status) &&
      project.download_status !== "completed" &&
      project.download_status !== "cancelled";
    const editing = editingProjectId === project.id;
    const picked = selectedProjects.has(project.id);
    return (
      <article
        className={[
          "project-card",
          editing ? "editing" : "",
          selectMode ? "selectable" : "",
          picked ? "picked" : "",
        ]
          .filter(Boolean)
          .join(" ")}
        key={project.id}
        data-inline-editor={editing ? true : undefined}
        // В режиме выделения клик по карточке переключает отметку, а не
        // открывает проект: иначе выделять мышью было бы невозможно.
        onClick={() => {
          if (editing) return;
          if (selectMode) {
            toggleProjectSelection(project.id);
            return;
          }
          openProject(project.id);
        }}
      >
        {selectMode ? (
          <div className="project-pick" aria-hidden="true">
            <input
              type="checkbox"
              checked={picked}
              tabIndex={-1}
              onChange={() => toggleProjectSelection(project.id)}
              onClick={(event) => event.stopPropagation()}
              aria-label={`Выделить проект ${project.name}`}
            />
          </div>
        ) : (
          <div className="project-icon" aria-hidden="true">
            {hasDownloadProgress
              ? "↓"
              : project.processing_count
                ? "◌"
                : project.completed_count
                  ? "✓"
                  : "¶"}
          </div>
        )}
        <div className="project-copy">
          {editing ? (
            <input
              className="inline-name-input"
              autoFocus
              value={projectNameDraft}
              maxLength={120}
              aria-label="Название проекта"
              onClick={(event) => event.stopPropagation()}
              onChange={(event) => setProjectNameDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") saveProjectName(project);
                if (event.key === "Escape") cancelProjectEdit();
              }}
            />
          ) : (
            <h3>
              {project.name}
              {/* Метка источника нужна именно в общей ленте: там папок не
                  видно, и без неё чужую расшифровку не отличить от своей. */}
              {project.integration && (
                <em className="project-origin">
                  {project.integration_title || "Внешний сервис"}
                </em>
              )}
            </h3>
          )}
          <p>
            {project.folder_name || "Без папки"} · {shortDate(project.created_at)}
          </p>
        </div>
        <div className="project-progress">
          <strong>{projectStatus(project)}</strong>
          <span>
            {total
              ? `${project.completed_count} из ${total} файлов`
              : hasDownloadProgress
                ? "Видео по ссылке"
                : "Можно добавлять файлы"}
          </span>
          {(total > 0 || hasDownloadProgress) && (
            <div className="mini-progress">
              <i style={{ width: `${Math.max(progress, 3)}%` }} />
            </div>
          )}
        </div>
        <div className="card-actions">
          {editing ? (
            <>
              <button
                type="button"
                className="save-edit"
                title="Сохранить"
                onClick={(event) => {
                  event.stopPropagation();
                  saveProjectName(project);
                }}
              >
                ✓
              </button>
              <button
                type="button"
                className="danger-ghost"
                title="Удалить проект"
                onClick={(event) => {
                  event.stopPropagation();
                  deleteProject(project);
                }}
              >
                ×
              </button>
            </>
          ) : (
            <button
              type="button"
              title="Редактировать проект"
              aria-label={`Редактировать проект ${project.name}`}
              onClick={(event) => {
                event.stopPropagation();
                startProjectEdit(project);
              }}
            >
              ✎
            </button>
          )}
        </div>
      </article>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button
          className="brand"
          type="button"
          onClick={() => setScreen("projects")}
        >
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
            <i />
          </span>
          <span>
            <strong>Whisper</strong>
            <small>локальная студия</small>
          </span>
        </button>

        <nav className="main-nav" aria-label="Основное меню">
          <button
            type="button"
            className={
              screen === "projects" || screen === "project" ? "active" : ""
            }
            onClick={() => setScreen("projects")}
          >
            <span>▤</span> Проекты
            <b>{projects.length}</b>
          </button>
          <button
            type="button"
            className={screen === "dictation" ? "active" : ""}
            onClick={openDictation}
          >
            <span>●</span> Диктовка
            <b>{dictationTotal}</b>
          </button>
        </nav>

        <div className="folder-heading">
          <span>Папки</span>
          <button type="button" onClick={createFolder} title="Создать папку">
            +
          </button>
        </div>
        <nav className="folder-list" aria-label="Папки">
          <button
            type="button"
            className={folderFilter === "all" ? "active" : ""}
            onClick={() => {
              showFolder("all");
              setScreen("projects");
            }}
          >
            <span>Все проекты</span>
            <b>{feedProjectCount}</b>
          </button>
          {visibleFolders.map((folder) => {
            const editing = editingFolderId === folder.id;
            return (
              <div
                className={`folder-nav-item ${editing ? "editing" : ""}`}
                key={folder.id}
                data-inline-editor={editing ? true : undefined}
              >
                {editing ? (
                  <div className="folder-inline-editor">
                    <input
                      autoFocus
                      value={folderNameDraft}
                      maxLength={120}
                      aria-label="Название папки"
                      onChange={(event) =>
                        setFolderNameDraft(event.target.value)
                      }
                      onKeyDown={(event) => {
                        if (event.key === "Enter") saveFolder(folder);
                        if (event.key === "Escape") cancelFolderEdit();
                      }}
                    />
                    {/* Галочка живёт здесь же, а не только в настройках:
                        решение «эта папка мне в ленте не нужна» приходит
                        как раз тогда, когда смотришь на саму папку. */}
                    <label className="folder-feed-toggle">
                      <input
                        type="checkbox"
                        checked={folderFeedDraft}
                        onChange={(event) =>
                          setFolderFeedDraft(event.target.checked)
                        }
                      />
                      <span>Показывать в общей ленте</span>
                    </label>
                    <div>
                      <button
                        type="button"
                        title="Сохранить"
                        onClick={() => saveFolder(folder)}
                      >
                        ✓
                      </button>
                      {/* У папки внешнего сервиса удаления нет: сервис создаст
                          её заново, а расшифровки, которых в нём может уже не
                          быть, пропадут. Убрать её из ленты — галочкой выше. */}
                      {!folder.integration && (
                        <button
                          type="button"
                          className="danger-ghost"
                          title="Удалить папку и все проекты"
                          onClick={() => deleteFolder(folder)}
                        >
                          ×
                        </button>
                      )}
                    </div>
                  </div>
                ) : (
                  <>
                    <button
                      type="button"
                      className={folderFilter === folder.id ? "active" : ""}
                      title={
                        folder.feed_visible === false
                          ? `${folder.name} — скрыта из общей ленты, но открывается отсюда`
                          : folder.name
                      }
                      onClick={() => {
                        showFolder(folder.id);
                        setScreen("projects");
                      }}
                    >
                      <span>
                        {folder.feed_visible === false ? "◌" : "⌑"}{" "}
                        {folder.name}
                      </span>
                      <b>{folder.project_count}</b>
                    </button>
                    <button
                      className="folder-edit-trigger"
                      type="button"
                      title="Редактировать папку"
                      aria-label={`Редактировать папку ${folder.name}`}
                      onClick={() => startFolderEdit(folder)}
                    >
                      ✎
                    </button>
                  </>
                )}
              </div>
            );
          })}
          {projectCounts.loose > 0 && (
            <button
              type="button"
              className={folderFilter === "none" ? "active" : ""}
              onClick={() => {
                showFolder("none");
                setScreen("projects");
              }}
            >
              <span>Без папки</span>
              <b>{projectCounts.loose}</b>
            </button>
          )}
        </nav>

        {/* Статус над «Настройками», а не под ними: «Настройки» — это пункт
            навигации, и он должен быть последним в списке пунктов. Когда под
            ним стояла ещё и строка состояния, самый низ панели читался как
            продолжение навигации. */}
        <div className="sidebar-bottom">
          <div
            className="server-status"
            tabIndex={online ? 0 : -1}
            title={online ? "Навести, чтобы остановить сервис" : undefined}
          >
            {online && (
              <button
                className="shutdown-service"
                type="button"
                disabled={shuttingDown}
                onClick={shutdownService}
              >
                <span aria-hidden="true">⏻</span>
                {shuttingDown ? "Останавливаю…" : "Остановить и закрыть"}
              </button>
            )}
            <i className={online ? "online" : ""} />
            <span>
              <strong>
                {online === null
                  ? "Подключение…"
                  : online
                    ? "Сервис работает"
                    : "Сервис недоступен"}
              </strong>
            </span>
          </div>
          <button
            type="button"
            className={screen === "settings" ? "active" : ""}
            onClick={() => setScreen("settings")}
          >
            <span>⚙</span> Настройки
          </button>
        </div>
      </aside>

      <main className="workspace">
        {queueOverview.worker_alive === false && (
          // Очередь встала: рабочий поток не жив. Сторож на сервере поднимает
          // его сам, но пользователь должен видеть, что пауза не нормальная.
          <div className="worker-down" role="alert">
            <strong>Обработчик очереди остановился.</strong> Сервис пытается
            поднять его сам. Если через минуту ничего не сдвинулось,
            перезапустите Whisper и посмотрите <code>logs\backend.err.log</code>.
          </div>
        )}
        <div className="queue-bar">
          {/* Состояние очереди живёт ровно в одном месте. Раньше слева стояло
              «Обработка сейчас не выполняется», а справа — «Очередь пуста»: два
              разных текста про один и тот же факт. */}
          <div className="queue-inspector">
            <button
              type="button"
              className="queue-summary"
              aria-haspopup="dialog"
              title={queueOverview.active?.title}
            >
              <span className={queueOverview.active ? "pulse" : ""}>●</span>
              {queueOverview.active ? (
                <span className="queue-summary-text">
                  Выполняется <strong>{queueOverview.active.title}</strong>
                  {queueOverview.active.cancel_requested && " · останавливаю…"}
                  {queueOverview.waiting.length > 0 &&
                    ` · в очереди ${queueOverview.waiting.length}`}
                </span>
              ) : (
                <span className="queue-summary-text">
                  {queueOverview.waiting.length
                    ? `В очереди: ${queueOverview.waiting.length}`
                    : "Очередь пуста"}
                </span>
              )}
              <span aria-hidden="true">⌄</span>
            </button>
            {queueOverview.active && (
              <button
                type="button"
                className="stop-button"
                disabled={
                  stoppingIds.has(queueOverview.active.id) ||
                  queueOverview.active.cancel_requested
                }
                onClick={() => cancelQueueItem(queueOverview.active!)}
              >
                {queueOverview.active.cancel_requested
                  ? "Останавливаю…"
                  : "◼ Остановить"}
              </button>
            )}

            <section
              className="queue-popover"
              role="dialog"
              aria-label="Общая очередь"
            >
              <header>
                <div>
                  <strong>Общая очередь</strong>
                  <small>
                    {queueOverview.active ? "1 в работе" : "Нет активных"}
                    {" · "}
                    {queueOverview.waiting.length} ожидают
                  </small>
                </div>
              </header>

              {queueOverview.active && (
                <article className="queue-item queue-item-active">
                  <span className="queue-item-number">●</span>
                  <div className="queue-item-copy">
                    <strong title={queueOverview.active.title}>
                      {queueOverview.active.title}
                    </strong>
                    <small>
                      {queueOverview.active.operation}
                      {queueOverview.active.project_name
                        ? ` · ${queueOverview.active.project_name}`
                        : ""}
                    </small>
                  </div>
                  <span className="queue-item-state">
                    {queueOverview.active.cancel_requested
                      ? "Остановка…"
                      : "В работе"}
                  </span>
                  <button
                    type="button"
                    className="queue-item-action danger"
                    title="Остановить"
                    aria-label={`Остановить ${queueOverview.active.title}`}
                    disabled={
                      stoppingIds.has(queueOverview.active.id) ||
                      queueOverview.active.cancel_requested
                    }
                    onClick={() => cancelQueueItem(queueOverview.active!)}
                  >
                    ×
                  </button>
                </article>
              )}

              {queueOverview.waiting.length ? (
                <div className="queue-waiting-list">
                  {queueOverview.waiting.map((item) => (
                    <article className="queue-item" key={`${item.kind}-${item.id}`}>
                      <span className="queue-item-number">{item.position}</span>
                      <div className="queue-item-copy">
                        <strong title={item.title}>{item.title}</strong>
                        <small>
                          {item.operation}
                          {item.project_name ? ` · ${item.project_name}` : ""}
                        </small>
                      </div>
                      {item.priority && (
                        <span className="queue-priority-mark">Приоритет</span>
                      )}
                      <button
                        type="button"
                        className={`queue-item-action ${
                          item.priority ? "is-priority" : ""
                        }`}
                        title={
                          item.priority
                            ? "Уже в приоритете"
                            : "Переместить в начало очереди"
                        }
                        aria-label={`Переместить в начало очереди: ${item.title}`}
                        disabled={
                          item.priority || prioritizingIds.has(item.id)
                        }
                        onClick={() => prioritizeQueueItem(item)}
                      >
                        {item.priority ? "★" : "↑"}
                      </button>
                      <button
                        type="button"
                        className="queue-item-action danger"
                        title="Убрать из очереди"
                        aria-label={`Убрать из очереди: ${item.title}`}
                        disabled={stoppingIds.has(item.id)}
                        onClick={() => cancelQueueItem(item)}
                      >
                        ×
                      </button>
                    </article>
                  ))}
                </div>
              ) : (
                <div className="queue-popover-empty">Новых задач нет</div>
              )}
            </section>
          </div>
        </div>

        {screen === "projects" && (
          <section className="page projects-page">
            <header className="page-header">
              <div>
                <div className="eyebrow">Локальная библиотека</div>
                <h1>Проекты</h1>
              </div>
              <div className="project-create-actions">
                <div
                  className={`quick-drop ${quickDragging ? "dragging" : ""}`}
                  onDragEnter={(event) => {
                    event.preventDefault();
                    quickDragDepthRef.current += 1;
                    setQuickDragging(true);
                  }}
                  onDragOver={(event) => event.preventDefault()}
                  onDragLeave={() => {
                    quickDragDepthRef.current = Math.max(
                      0,
                      quickDragDepthRef.current - 1,
                    );
                    if (quickDragDepthRef.current === 0) {
                      setQuickDragging(false);
                    }
                  }}
                  onDrop={(event: DragEvent<HTMLDivElement>) => {
                    event.preventDefault();
                    quickDragDepthRef.current = 0;
                    setQuickDragging(false);
                    quickCreateProject(event.dataTransfer.files);
                  }}
                  onClick={chooseQuickFiles}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      chooseQuickFiles();
                    }
                  }}
                >
                  <input
                    ref={quickInputRef}
                    type="file"
                    multiple
                    onClick={(event) => event.stopPropagation()}
                    onChange={(event) => {
                      if (event.target.files) {
                        quickCreateProject(event.target.files);
                      }
                      event.target.value = "";
                    }}
                  />
                  <span>⇧</span>
                  <b>
                    {quickUploading
                      ? "Создаю проект…"
                      : "Перетащите или нажмите, чтобы добавить файлы"}
                  </b>
                </div>
                <button
                  className="url-import-button"
                  type="button"
                  title="Скачать видео по ссылке"
                  aria-label="Скачать видео по ссылке"
                  // Без обёртки React передал бы сюда объект события, и он
                  // попадал бы в urlTargetProjectId вместо id проекта.
                  onClick={() => openUrlImporter()}
                >
                  🔗
                </button>
                <button
                  className="primary-button"
                  type="button"
                  onClick={() => setCreateOpen(true)}
                >
                  <span>＋</span> Новый проект
                </button>
              </div>
            </header>

            {/* Одна строка: слева название фильтра, справо у края —
                переключатель вида, а между ними управление выделением.
                «В папку» и «Удалить» появляются только когда что-то отмечено:
                до этого они бесполезны и лишь занимают место. Счётчик
                отдельным элементом не выводится — он стоит на самой кнопке
                удаления, где и нужен. */}
            <div className="list-toolbar">
              <strong>
                {folderFilter === "all"
                  ? "Все проекты"
                  : folderFilter === "none"
                    ? "Без папки"
                    : folders.find((folder) => folder.id === folderFilter)
                        ?.name || "Папка"}
              </strong>

              {selectMode && (
                <>
                  <button
                    type="button"
                    className="secondary-button toolbar-button"
                    onClick={() =>
                      setSelectedProjects(
                        allVisibleSelected
                          ? new Set()
                          : new Set(visibleProjectIdList),
                      )
                    }
                  >
                    {/* Сравнение по составу, а не по количеству: пять
                        отметок при пяти видимых ещё не значит, что отмечены
                        именно эти пять. */}
                    {allVisibleSelected ? "Снять все" : "Выделить все"}
                  </button>
                  {selectedVisibleIds.length > 0 && (
                    <>
                      <select
                        className="toolbar-select"
                        value=""
                        disabled={bulkBusy}
                        aria-label="Перенести выделенные в папку"
                        onChange={(event) => {
                          const value = event.target.value;
                          if (!value) return;
                          void bulkMoveProjects(
                            value === "none" ? null : value,
                          );
                        }}
                      >
                        <option value="">В папку…</option>
                        <option value="none">Без папки</option>
                        {visibleFolders.map((folder) => (
                          <option key={folder.id} value={folder.id}>
                            {folder.name}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        className="danger-ghost toolbar-button"
                        disabled={bulkBusy}
                        onClick={bulkDeleteProjects}
                      >
                        {bulkBusy
                          ? "Выполняю…"
                          : `Удалить ${selectedVisibleIds.length}`}
                      </button>
                    </>
                  )}
                </>
              )}

              <button
                type="button"
                className="secondary-button toolbar-button"
                onClick={() =>
                  selectMode ? leaveSelectMode() : setSelectMode(true)
                }
              >
                {selectMode ? "Готово" : "Выбрать"}
              </button>

              <div className="view-switch">
                <button
                  type="button"
                  className={settings.project_view === "flat" ? "active" : ""}
                  onClick={() => saveSetting("project_view", "flat")}
                >
                  Лента
                </button>
                <button
                  type="button"
                  className={
                    settings.project_view === "folders" ? "active" : ""
                  }
                  onClick={() => saveSetting("project_view", "folders")}
                >
                  По папкам
                </button>
              </div>
            </div>

            {filteredProjects.length === 0 ? (
              <div className="empty-projects">
                <span>¶</span>
                <h2>Здесь пока нет проектов</h2>
                <p>
                  Создайте проект, добавьте один или несколько файлов и оставьте
                  сервис работать.
                </p>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => setCreateOpen(true)}
                >
                  Создать проект
                </button>
              </div>
            ) : settings.project_view === "flat" ||
              folderFilter !== "all" ? (
              <div className="project-list">
                {filteredProjects.map(renderProjectCard)}
              </div>
            ) : (
              <div className="project-groups">
                {groupedProjects.map((group) => (
                  <section key={group.id} className="project-group">
                    <div className="group-title">
                      <span>⌑</span>
                      <h2>{group.name}</h2>
                      <b>{group.items.length}</b>
                    </div>
                    <div className="project-list">
                      {group.items.map(renderProjectCard)}
                    </div>
                  </section>
                ))}
              </div>
            )}

            {projectsTotal > 0 && (
              <Pagination
                page={projectsCurrentPage}
                pageSize={projectsPageSize}
                total={projectsTotal}
                unit="проекты"
                onPage={setProjectsPage}
                onPageSize={(value) => {
                  // Новый размер страницы начинается с первой: иначе
                  // «показать 100» унесло бы на середину списка.
                  setProjectsPageSizeChoice(value);
                  setProjectsPage(1);
                }}
              />
            )}
          </section>
        )}

        {screen === "project" && activeProject && (
          <section className="page project-page">
            <button
              className="back-button"
              type="button"
              onClick={() => setScreen("projects")}
            >
              ← Все проекты
            </button>
            <header className="project-header">
              <div>
                <div className="eyebrow">Проект</div>
                {editingProjectId === activeProject.id ? (
                  <div className="project-title-edit" data-inline-editor>
                    <input
                      autoFocus
                      value={projectNameDraft}
                      maxLength={120}
                      aria-label="Название проекта"
                      onChange={(event) =>
                        setProjectNameDraft(event.target.value)
                      }
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          saveProjectName(activeProject);
                        }
                        if (event.key === "Escape") cancelProjectEdit();
                      }}
                    />
                    <select
                      value={projectFolderDraft}
                      aria-label="Папка проекта"
                      onChange={(event) =>
                        setProjectFolderDraft(event.target.value)
                      }
                    >
                      <option value="">Без папки</option>
                      {visibleFolders.map((folder) => (
                        <option value={folder.id} key={folder.id}>
                          {folder.name}
                        </option>
                      ))}
                    </select>
                    <button
                      className="save-edit"
                      type="button"
                      title="Сохранить"
                      aria-label="Сохранить изменения проекта"
                      onClick={() => saveProjectName(activeProject)}
                    >
                      ✓
                    </button>
                    <button
                      className="danger-ghost"
                      type="button"
                      title="Удалить проект"
                      aria-label="Удалить проект"
                      onClick={() => deleteProject(activeProject)}
                    >
                      ×
                    </button>
                  </div>
                ) : (
                  <div className="project-title-row">
                    <h1>{activeProject.name}</h1>
                    <button
                      type="button"
                      title="Редактировать проект"
                      aria-label="Редактировать проект"
                      onClick={() => startProjectEdit(activeProject)}
                    >
                      ✎
                    </button>
                  </div>
                )}
                <p>
                  {activeProject.job_count
                    ? `${activeProject.completed_count} из ${activeProject.job_count} файлов готово`
                    : "Добавьте первый аудио- или видеофайл"}
                </p>
              </div>
            </header>

            {activeProject.completed_count > 0 && (
              <section className="combined-card">
                <div className="combined-symbol">≋</div>
                <div className="combined-copy">
                  <div className="eyebrow">Объединённый результат</div>
                  <h2>Все готовые файлы собраны в один документ</h2>
                  {activeProject.completed_count < activeProject.job_count && (
                    <p>Документ пополняется по мере готовности очереди.</p>
                  )}
                </div>
                <div className="combined-actions">
                  <button
                    type="button"
                    onClick={() =>
                      copyText(
                        `${API}/projects/${activeProject.id}/combined/text?${variantQuery()}`,
                        "Текст проекта",
                      )
                    }
                  >
                    ⧉ Копировать полный текст
                  </button>
                  <a
                    className="download-primary"
                    href={`${API}/projects/${activeProject.id}/combined/${settings.default_export}?${variantQuery()}`}
                  >
                    Скачать полный {settings.default_export.toUpperCase()}
                  </a>
                  <div className="format-links">
                    {exportKinds
                      .filter((kind) => kind !== settings.default_export)
                      .map((kind) => (
                      <a
                        key={kind}
                        href={`${API}/projects/${activeProject.id}/combined/${kind}?${variantQuery()}`}
                      >
                        Скачать полный {kind.toUpperCase()}
                      </a>
                      ))}
                  </div>
                </div>
              </section>
            )}

            {activeProject.completed_count > 0 && (
              <section className="transcript-panel">
                <div className="transcript-toolbar">
                  <div>
                    <div className="eyebrow">Просмотр результата</div>
                    <h2>Транскрипция прямо в проекте</h2>
                  </div>
                  <div className="transcript-controls">
                    <div className="choice">
                      {(["none", "segments", "words"] as const).map((value) => (
                        <button
                          type="button"
                          key={value}
                          className={viewTimestamps === value ? "active" : ""}
                          onClick={() => setViewTimestamps(value)}
                        >
                          {value === "none"
                            ? "Без таймкодов"
                            : value === "segments"
                              ? "По фразам"
                              : "По словам"}
                        </button>
                      ))}
                    </div>
                    <div className="choice">
                      {(["formatted", "plain"] as const).map((value) => (
                        <button
                          type="button"
                          key={value}
                          className={viewFormatting === value ? "active" : ""}
                          onClick={() => setViewFormatting(value)}
                        >
                          {value === "formatted"
                            ? "Со знаками"
                            : "Без знаков"}
                        </button>
                      ))}
                    </div>
                    <div className="choice">
                      {(["off", "on"] as const).map((value) => (
                        <button
                          type="button"
                          key={value}
                          className={viewSpeakers === value ? "active" : ""}
                          onClick={() => setViewSpeakers(value)}
                        >
                          {value === "on" ? "По спикерам" : "Без спикеров"}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
                {showSpeakerMissingBanner && (
                  <div className="speaker-missing-banner" role="status">
                    <div>
                      <strong>
                        Спикеры не были размечены{" "}
                        <Hint text="Чтобы увидеть текст по голосам, запустите разметку спикеров для готовых файлов." />
                      </strong>
                    </div>
                    <select
                      className="speaker-engine-select"
                      aria-label="Модель разметки спикеров"
                      value={missingSpeakersEngine}
                      onChange={(event) =>
                        setMissingSpeakersEngine(
                          event.target.value as DiarizationEngine,
                        )
                      }
                    >
                      {diarizationEngines.map((engine) => (
                        <option
                          key={engine}
                          value={engine}
                          disabled={!engineReady(diarizationStatus, engine)}
                        >
                          {diarizationEngineLabels[engine]}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      disabled={
                        speakerDiarizationStarting ||
                        !engineReady(diarizationStatus, missingSpeakersEngine)
                      }
                      onClick={diarizeProjectMissingSpeakers}
                    >
                      {speakerDiarizationStarting
                        ? "Ставлю в очередь…"
                        : "Разметить спикеров"}
                    </button>
                  </div>
                )}
                {showSpeakerNotFoundBanner && (
                  <div className="speaker-missing-banner" role="status">
                    <div>
                      <strong>Спикеры не найдены</strong>
                      <span>
                        Разметка уже выполнена, но отдельных голосов в
                        результате нет.
                      </span>
                    </div>
                  </div>
                )}
                {viewSpeakers === "on" && projectSpeakers.length > 0 && (
                  <div className="speaker-manager">
                    {speakerFilterValue && (
                      <div className="speaker-filter-notice" role="status">
                        <strong>Показана часть текста: спикеров {selectedSpeakerKeyList.length} из {projectSpeakers.length}.</strong>
                        <span>Скрыты: {projectSpeakers.filter((speaker) => !selectedSpeakerKeys.has(speaker.key)).map((speaker) => speaker.name).join(", ")}.</span>
                        <span>Кнопки выше скачивают полный текст со всеми спикерами.</span>
                        <details>
                          <summary>Экспорт выбранных спикеров</summary>
                          <div className="format-links">
                            {exportKinds.map((kind) => (
                              <a
                                key={kind}
                                href={`${API}/projects/${activeProject.id}/combined/${kind}?${variantQuery(true)}`}
                              >
                                Только выбранные · {kind.toUpperCase()}
                              </a>
                            ))}
                          </div>
                        </details>
                      </div>
                    )}
                    <div className="speaker-manager-head">
                      <span>Фильтр просмотра · выбрано {selectedSpeakerKeyList.length} из {projectSpeakers.length}</span>
                      <div className="speaker-manager-actions">
                        <button
                          type="button"
                          onClick={() =>
                            setSelectedSpeakerKeys(
                              new Set(
                                projectSpeakers.map((speaker) => speaker.key),
                              ),
                            )
                          }
                        >
                          Все
                        </button>
                        <button
                          type="button"
                          onClick={() => setSelectedSpeakerKeys(new Set())}
                        >
                          Никого
                        </button>
                      </div>
                    </div>
                    <div className="speaker-list">
                      {projectSpeakers.map((speaker) => (
                        <div
                          className={`speaker-card ${
                            selectedSpeakerKeys.has(speaker.key)
                              ? "selected"
                              : ""
                          }`}
                          key={speaker.key}
                          data-inline-editor={
                            editingSpeakerKey === speaker.key ? true : undefined
                          }
                        >
                          {editingSpeakerKey === speaker.key ? (
                            <div
                              className="speaker-name-editor"
                              onBlur={(event) => {
                                if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
                                  saveSpeakerName(speaker);
                                }
                              }}
                            >
                              <input
                                value={speakerNameDraft}
                                maxLength={80}
                                autoFocus
                                aria-label={`Имя для ${speaker.default_name}`}
                                onChange={(event) => {
                                  const name = event.target.value;
                                  if (speakerEditRef.current?.speaker.key === speaker.key) {
                                    speakerEditRef.current.name = name;
                                  }
                                  speakerDraftsRef.current.set(speaker.key, name);
                                  setSpeakerNameDraft(name);
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter") {
                                    event.preventDefault();
                                    saveSpeakerName(speaker);
                                  }
                                  if (event.key === "Escape") {
                                    cancelSpeakerEdit();
                                  }
                                }}
                              />
                              <button
                                type="button"
                                className="save-inline"
                                title="Сохранить имя"
                                aria-label="Сохранить имя спикера"
                                onClick={() => saveSpeakerName(speaker)}
                              >
                                ✓
                              </button>
                            </div>
                          ) : (
                            <>
                              <button
                                className="speaker-select"
                                type="button"
                                aria-pressed={selectedSpeakerKeys.has(
                                  speaker.key,
                                )}
                                onClick={() => toggleSpeaker(speaker.key)}
                              >
                                <strong>{speaker.name}</strong>
                                <span>
                                  {speaker.custom_name
                                    ? speaker.default_name
                                    : speaker.source}
                                </span>
                                <small>
                                  {formatDuration(speaker.duration_seconds)}
                                </small>
                              </button>
                              <button
                                className="speaker-edit"
                                type="button"
                                title="Присвоить имя спикеру"
                                aria-label={`Переименовать ${speaker.name}`}
                                onClick={() => startSpeakerEdit(speaker)}
                              >
                                ✎
                              </button>
                              <button
                                className={`speaker-play ${
                                  speakerSampleKey === speaker.key
                                    ? "playing"
                                    : speakerSampleLoadingKey === speaker.key
                                      ? "loading"
                                      : ""
                                }`}
                                type="button"
                                title={
                                  speakerSampleKey === speaker.key ||
                                  speakerSampleLoadingKey === speaker.key
                                    ? "Остановить"
                                    : "Послушать голос: несколько самых длинных реплик этого спикера"
                                }
                                aria-label={
                                  speakerSampleKey === speaker.key
                                    ? `Остановить голос ${speaker.name}`
                                    : `Послушать голос ${speaker.name}`
                                }
                                aria-pressed={speakerSampleKey === speaker.key}
                                onClick={() => toggleSpeakerSample(speaker)}
                              >
                                {speakerSampleKey === speaker.key
                                  ? "■"
                                  : speakerSampleLoadingKey === speaker.key
                                    ? "…"
                                    : "▶"}
                              </button>
                            </>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <div className="transcript-document">
                  {transcriptLoading ? (
                    <p className="transcript-loading">Готовлю вариант текста…</p>
                  ) : (
                    <pre>{transcriptText}</pre>
                  )}
                </div>
              </section>
            )}

            <section className="composer">
              <div className="section-title">
                <div>
                  <div className="eyebrow">Добавить материалы</div>
                  <h2>Файлы проекта</h2>
                </div>
                {activeProject && (
                  <button
                    className="url-import-button composer-url-button"
                    type="button"
                    title="Скачать видео по ссылке в этот проект"
                    aria-label="Скачать видео по ссылке в этот проект"
                    onClick={() => openUrlImporter(activeProject.id)}
                  >
                    🔗
                  </button>
                )}
              </div>
              <form onSubmit={uploadAndProbe}>
                <div
                  className={`dropzone ${dragging ? "dragging" : ""}`}
                  onDragEnter={(event) => {
                    event.preventDefault();
                    projectDragDepthRef.current += 1;
                    setDragging(true);
                  }}
                  onDragOver={(event) => event.preventDefault()}
                  onDragLeave={() => {
                    projectDragDepthRef.current = Math.max(
                      0,
                      projectDragDepthRef.current - 1,
                    );
                    if (projectDragDepthRef.current === 0) {
                      setDragging(false);
                    }
                  }}
                  onDrop={(event: DragEvent<HTMLDivElement>) => {
                    event.preventDefault();
                    projectDragDepthRef.current = 0;
                    setDragging(false);
                    addFiles(event.dataTransfer.files);
                  }}
                  onClick={chooseProjectFiles}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      chooseProjectFiles();
                    }
                  }}
                >
                  <input
                    ref={inputRef}
                    type="file"
                    multiple
                    onClick={(event) => event.stopPropagation()}
                    onChange={(event: ChangeEvent<HTMLInputElement>) => {
                      if (event.target.files) addFiles(event.target.files);
                      event.target.value = "";
                    }}
                  />
                  <span className="upload-symbol">↑</span>
                  <div>
                    <strong>Перетащите аудио или видео сюда</strong>
                  </div>
                </div>

                {files.length > 0 && (
                  <>
                    <div className="selected-files">
                      {files.map((file, index) => (
                        <div
                          className="selected-file"
                          key={`${file.name}:${file.lastModified}`}
                        >
                          <span>
                            {file.name
                              .split(".")
                              .pop()
                              ?.slice(0, 4)
                              .toUpperCase()}
                          </span>
                          <div>
                            <strong>
                              {index + 1}. {file.name}
                            </strong>
                            <small>{fileSize(file.size)}</small>
                          </div>
                          <button
                            type="button"
                            onClick={() =>
                              setFiles((current) =>
                                current.filter(
                                  (_, itemIndex) => itemIndex !== index,
                                ),
                              )
                            }
                          >
                            ×
                          </button>
                        </div>
                      ))}
                    </div>
                    <div className="probe-row">
                      <span>
                        Сначала файл загружается локально и проверяется. Если в
                        видео несколько аудиодорожек, вы сможете выбрать нужную.
                      </span>
                      <button
                        className="secondary-button"
                        type="submit"
                        disabled={uploading || !online}
                      >
                        {uploading
                          ? "Загружаю и анализирую…"
                          : `Проверить ${files.length} файл${files.length === 1 ? "" : "а"}`}
                      </button>
                    </div>
                  </>
                )}

                {projectMedia.length > 0 && (
                  <>
                    <div className="track-heading">
                      <div>
                        <div className="eyebrow">Проверено FFmpeg</div>
                        <h3>Выберите аудиодорожки</h3>
                      </div>
                      <span>{projectMedia.length} файлов готовы к очереди</span>
                    </div>
                    <div className="staged-media-list">
                      {projectMedia.map((item) => {
                        const selectedStreamIndex =
                          trackSelections[item.id] ??
                          item.audio_streams[0].index;
                        const selectedStream =
                          item.audio_streams.find(
                            (stream) => stream.index === selectedStreamIndex,
                          ) || item.audio_streams[0];
                        const trimRange = trimRanges[item.id] ?? null;
                        const trimEditorKey = `${item.id}:${selectedStream.index}`;
                        const trimEditorOpen = Boolean(trimOpen[trimEditorKey]);
                        const basePoints = trackPreviewPoints(
                          item.duration_seconds,
                        );
                        // С обрезкой первым делом хочется послушать именно то
                        // место, с которого начнётся распознавание.
                        const trimPoint = trimRange
                          ? {
                              value: Math.round(trimRange.start),
                              label: "Начало фрагмента",
                            }
                          : null;
                        const previewPoints = trimPoint
                          ? [
                              trimPoint,
                              ...basePoints.filter(
                                (point) => point.value !== trimPoint.value,
                              ),
                            ]
                          : basePoints;
                        const previewStart =
                          trackPreviewStarts[item.id] ??
                          (trimPoint
                            ? trimPoint.value
                            : previewPoints[1]?.value) ??
                          previewPoints[0]?.value ??
                          0;
                        const selectedStreamPosition =
                          item.audio_streams.findIndex(
                            (stream) => stream.index === selectedStream.index,
                          );
                        return (
                        <article className="staged-media" key={item.id}>
                          <div className="media-kind">
                            {item.video_stream_count ? "▶" : "♫"}
                          </div>
                          <div className="media-copy">
                            <strong>{item.original_name}</strong>
                            <small>
                              {formatDuration(item.duration_seconds)} ·{" "}
                              {fileSize(item.size_bytes)} ·{" "}
                              {item.format_name.toUpperCase()}
                            </small>
                            <code title={item.source_path || item.stored_path}>
                              {item.source_path || item.stored_path}
                            </code>
                            <small>
                              {item.storage_mode === "reference"
                                ? "Оригинал останется на своём месте"
                                : "Локальная управляемая копия"}
                            </small>
                          </div>
                          <div className="track-picker">
                            <label>
                              <span>
                                {item.audio_streams.length > 1
                                  ? `${item.audio_streams.length} аудиодорожки`
                                  : "Аудиодорожка"}
                              </span>
                              <select
                                aria-label={`Аудиодорожка файла ${item.original_name}`}
                                value={selectedStreamIndex}
                                onChange={(event) =>
                                  setTrackSelections((current) => ({
                                    ...current,
                                    [item.id]: Number(event.target.value),
                                  }))
                                }
                              >
                                {item.audio_streams.map((stream, index) => (
                                  <option value={stream.index} key={stream.index}>
                                    {trackLabel(stream, index)}
                                  </option>
                                ))}
                              </select>
                            </label>
                            {/* Пока открыт редактор границ, играть можно прямо
                                по его дорожке — второй плеер только мешал бы. */}
                            <div
                              className="track-preview"
                              hidden={trimEditorOpen}
                            >
                              <div className="track-preview-heading">
                                <span>
                                  ▶ Прослушать выбранную дорожку
                                </span>
                                <select
                                  aria-label={`Место прослушивания файла ${item.original_name}`}
                                  value={previewStart}
                                  onChange={(event) =>
                                    setTrackPreviewStarts((current) => ({
                                      ...current,
                                      [item.id]: Number(event.target.value),
                                    }))
                                  }
                                >
                                  {previewPoints.map((point) => (
                                    <option value={point.value} key={point.value}>
                                      {point.label} · {previewClock(point.value)}
                                    </option>
                                  ))}
                                </select>
                              </div>
                              <audio
                                key={`${item.id}:${selectedStream.index}:${previewStart}`}
                                controls
                                preload="none"
                                aria-label={`Прослушивание ${trackLabel(
                                  selectedStream,
                                  Math.max(0, selectedStreamPosition),
                                )}`}
                                src={`${API}/media/${item.id}/track-preview?stream_index=${selectedStream.index}&start_seconds=${previewStart}`}
                                onError={() =>
                                  setNotice(
                                    "Не удалось загрузить превью дорожки. Проверьте, что исходный файл доступен.",
                                  )
                                }
                              />
                              <small>
                                Фрагмент 30 секунд · дорожка{" "}
                                {Math.max(1, selectedStreamPosition + 1)} ·{" "}
                                {selectedStream.codec.toUpperCase()} ·{" "}
                                {selectedStream.channel_layout ||
                                  `${selectedStream.channels} канала`}
                              </small>
                            </div>
                            <TrackTrimmer
                              key={`${item.id}:${selectedStream.index}`}
                              media={item}
                              streamIndex={selectedStream.index}
                              range={trimRanges[item.id] ?? null}
                              onChange={(next) =>
                                setTrimRanges((current) => {
                                  const updated = { ...current };
                                  if (next) {
                                    updated[item.id] = next;
                                  } else {
                                    delete updated[item.id];
                                  }
                                  return updated;
                                })
                              }
                              onOpenChange={(open) =>
                                setTrimOpen((current) => ({
                                  ...current,
                                  [trimEditorKey]: open,
                                }))
                              }
                              onError={setNotice}
                            />
                          </div>
                          <button
                            type="button"
                            title="Убрать файл"
                            onClick={() => removeMedia(item)}
                          >
                            ×
                          </button>
                        </article>
                        );
                      })}
                    </div>

                    <div className="processing-options">
                      <label>
                        <span>Язык</span>
                        <select
                          value={language}
                          onChange={(event) =>
                            setLanguage(
                              event.target.value as JobOptions["language"],
                            )
                          }
                        >
                          <option value="ru">Русский</option>
                          <option value="en">Английский</option>
                          <option value="auto">Авто</option>
                        </select>
                      </label>
                      <label>
                        <span>Режим</span>
                        <select
                          value={quality}
                          onChange={(event) =>
                            setQuality(
                              event.target.value as JobOptions["quality"],
                            )
                          }
                        >
                          <option value="accurate">Точно</option>
                          <option value="fast">Быстро</option>
                        </select>
                      </label>
                      <label>
                        <span>Таймкоды</span>
                        <select
                          value={timestamps}
                          onChange={(event) =>
                            setTimestamps(
                              event.target.value as JobOptions["timestamps"],
                            )
                          }
                        >
                          <option value="none">Без таймкодов</option>
                          <option value="segments">По фразам</option>
                          <option value="words">По словам</option>
                        </select>
                      </label>
                      <label>
                        <span>Текст</span>
                        <select
                          value={formatting}
                          onChange={(event) =>
                            setFormatting(
                              event.target.value as JobOptions["formatting"],
                            )
                          }
                        >
                          <option value="formatted">
                            Со знаками препинания
                          </option>
                          <option value="plain">Без знаков препинания</option>
                        </select>
                      </label>
                      <label>
                        <span>Спикеры</span>
                        <DiarizationSelect
                          enabled={diarization}
                          engine={diarizationEngine}
                          status={diarizationStatus}
                          onChange={(enabled, engine) => {
                            setDiarization(enabled);
                            setDiarizationEngine(engine);
                          }}
                        />
                      </label>
                      <button
                        className="primary-button submit-files"
                        type="button"
                        onClick={enqueueMedia}
                        disabled={queueing || !online}
                      >
                        {queueing
                          ? "Добавляю…"
                          : `В общую очередь · ${projectMedia.length}`}
                        <span>→</span>
                      </button>
                    </div>
                  </>
                )}
              </form>
            </section>

            <section className="files-section">
              <div className="section-title">
                <div>
                  <div className="eyebrow">Порядок объединения</div>
                  <h2>Файлы и результаты</h2>
                </div>
                <span>{projectJobs.length}</span>
              </div>
              {projectJobs.length === 0 ? (
                <div className="compact-empty">
                  После добавления файлы появятся здесь и автоматически пойдут
                  в общую очередь.
                </div>
              ) : (
                <div className="job-list">
                  {projectJobs.map((job, index) => {
                    const speed =
                      job.duration_seconds && job.processing_seconds
                        ? job.duration_seconds / job.processing_seconds
                        : null;
                    return (
                      <article className={`job-card ${job.status}`} key={job.id}>
                        <span className="job-order">
                          {String(index + 1).padStart(2, "0")}
                        </span>
                        <div className="job-main">
                          <div className="job-heading">
                            <div>
                              <h3>{job.original_name}</h3>
                              <p>
                                {languageLabels[job.options.language]} ·{" "}
                                {job.options.quality === "fast"
                                  ? "быстро"
                                  : "точно"}
                                {job.options.diarization
                                  ? ` · спикеры: ${diarizationEngineLabels[job.options.diarization_engine || "pyannote"]}`
                                  : " · без спикеров"}{" "}
                                {job.options.audio_track &&
                                  `· дорожка ${job.options.audio_stream_index} · ${job.options.audio_track.codec.toUpperCase()} `}
                                {jobTrimLabel(job.options)}· {shortDate(
                                  job.created_at,
                                )}
                              </p>
                            </div>
                            <span className={`job-status ${job.status}`}>
                              {job.cancel_requested &&
                              job.status === "processing"
                                ? "Останавливаю…"
                                : job.options._rerun_mode === "diarization" &&
                                    (job.status === "queued" ||
                                      job.status === "processing")
                                  ? "Разметка спикеров"
                                  : job.options._rerun_mode === "transcribe" &&
                                      (job.status === "queued" ||
                                        job.status === "processing")
                                    ? "Точное распознавание"
                                    : statusLabels[job.status]}
                              {job.status === "queued" &&
                                job.queue_position &&
                                ` · №${job.queue_position}`}
                            </span>
                          </div>

                          <div className="source-row">
                            <div>
                              <span>
                                {job.storage_mode === "reference"
                                  ? "Исходник · без копирования"
                                  : job.source_path
                                    ? "Исходник · локальная копия хранится отдельно"
                                    : "Файл проекта · управляемая копия"}
                              </span>
                              <code
                                title={job.source_path || job.stored_path}
                              >
                                {job.source_path || job.stored_path}
                              </code>
                            </div>
                            <div>
                              <button
                                type="button"
                                onClick={() => revealSource(job.id)}
                              >
                                Показать в папке
                              </button>
                              {(job.status === "processing" ||
                                job.status === "queued") && (
                                <button
                                  type="button"
                                  className="stop-button"
                                  disabled={
                                    stoppingIds.has(job.id) ||
                                    job.cancel_requested
                                  }
                                  onClick={() => cancelJob(job)}
                                >
                                  {job.cancel_requested
                                    ? "Останавливаю…"
                                    : job.status === "queued"
                                      ? "◼ Снять с очереди"
                                      : "◼ Остановить"}
                                </button>
                              )}
                              {(job.status === "cancelled" ||
                                job.status === "failed") && (
                                <button
                                  type="button"
                                  onClick={() => requeueJob(job.id)}
                                >
                                  ↻ В очередь
                                </button>
                              )}
                            </div>
                          </div>

                          {/* Извлечённое аудио — это ровно то, что слышал
                              Whisper. Строка появляется только когда файл
                              действительно сохранён, чтобы не показывать пустой
                              путь. */}
                          {job.extracted_audio_path && (
                            <div className="source-row">
                              <div>
                                <span>Извлечённое аудио · хранится в проекте</span>
                                <code title={job.extracted_audio_path}>
                                  {job.extracted_audio_path}
                                </code>
                              </div>
                              <div>
                                <button
                                  type="button"
                                  onClick={() => revealExtractedAudio(job.id)}
                                >
                                  Показать в папке
                                </button>
                                <button
                                  type="button"
                                  className="secondary-button"
                                  onClick={() => dropExtractedAudio(job.id)}
                                >
                                  Удалить аудио
                                </button>
                              </div>
                            </div>
                          )}

                          {(job.status === "processing" ||
                            job.status === "queued") && (
                            <div className="progress-row">
                              <div>
                                <i
                                  style={{
                                    width: `${job.status === "queued" ? 2 : Math.max(job.progress * 100, 3)}%`,
                                  }}
                                />
                              </div>
                              <small>
                                {job.cancel_requested
                                  ? job.progress <= 0.02
                                    ? "Останавливаю. Файл ещё готовится к распознаванию — на многочасовой записи это может занять пару минут"
                                    : "Останавливаю — дожидаюсь текущего фрагмента"
                                  : job.status === "queued"
                                    ? "Можно закрыть страницу — очередь сохранится"
                                    : job.options._rerun_mode === "diarization"
                                      ? "Определяю и сопоставляю голоса"
                                    : job.progress <= 0.02
                                      ? "Готовлю аудио к распознаванию"
                                      : `${Math.round(job.progress * 100)}%`}
                              </small>
                            </div>
                          )}

                          {job.status === "cancelled" && (
                            <div className="cancelled-message">
                              Распознавание остановлено. Исходный файл на месте —
                              нажмите «В очередь», чтобы начать заново.
                            </div>
                          )}

                          {job.status === "completed" && (
                            <div className="result-row">
                              <div className="metrics">
                                <span>
                                  Запись{" "}
                                  <b>{formatDuration(job.duration_seconds)}</b>
                                </span>
                                <span>
                                  Обработка{" "}
                                  <b>
                                    {formatDuration(job.processing_seconds)}
                                  </b>
                                </span>
                                <span>
                                  Скорость{" "}
                                  <b>
                                    {speed ? `${speed.toFixed(1)}×` : "—"}
                                  </b>
                                </span>
                                {job.device && (
                                  <span
                                    className={
                                      job.device === "cpu"
                                        ? "metric-warning"
                                        : undefined
                                    }
                                    title={
                                      job.device === "cpu"
                                        ? "Видеокарта была недоступна, считал процессор — это примерно в 20 раз медленнее"
                                        : "Распознавание шло на видеокарте"
                                    }
                                  >
                                    Считал{" "}
                                    <b>
                                      {job.device === "cpu"
                                        ? "процессор"
                                        : "видеокарта"}
                                    </b>
                                  </span>
                                )}
                              </div>
                              <div className="result-actions">
                                {anyEngineReady(diarizationStatus) && (
                                  <button
                                    type="button"
                                    title="Разметить спикеров по готовому тексту, без повторного распознавания"
                                    onClick={() => openRediarize(job)}
                                  >
                                    ◐{" "}
                                    {job.options.diarization
                                      ? "Спикеры заново"
                                      : "Разметить спикеров"}
                                  </button>
                                )}
                                {job.options.quality === "fast" && (
                                  <button
                                    type="button"
                                    title="Перераспознать этот файл в точном режиме"
                                    onClick={() => openJobRerun(job)}
                                  >
                                    ↻ Распознать точнее
                                  </button>
                                )}
                              </div>
                            </div>
                          )}
                          {job.status === "completed" && job.error && (
                            <div className="rerun-warning">{job.error}</div>
                          )}
                          {job.status === "failed" && (
                            <div className="error-message">{job.error}</div>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
            </section>
          </section>
        )}

        {screen === "dictation" && (
          <section className="page dictation-page">
            <header className="page-header dictation-page-header">
              <div>
                <div className="eyebrow">Быстрая локальная запись</div>
                <div className="dictation-title-line">
                  <h1>Диктовка</h1>
                  <button
                    className="help-dot"
                    type="button"
                    title="Esc отменяет текущую запись без сохранения. ✓ останавливает запись, отправляет текст в историю и копирует его."
                    aria-label="Подсказка по диктовке"
                  >
                    ?
                  </button>
                </div>
              </div>
            </header>

            {pendingRecordingCount > 0 && (
              <div className="pending-audio-banner" role="status">
                <strong>Аудио в аварийном буфере: {pendingRecordingCount}</strong>
                <span>Переношу на диск сервиса.</span>
              </div>
            )}

            <div className="dictation-workspace">
              <section
                className={`recorder-card ${recording ? "recording" : ""}`}
              >
                <div className="recorder-topline">
                  <div>
                    <span className="recorder-state-dot" />
                    <strong>
                      {recording
                        ? "Идёт запись"
                        : startingRecording
                          ? "Подключаю микрофон…"
                          : savingRecording
                            ? "Сохраняю аудио…"
                            : currentDictationBusy
                              ? "Whisper распознаёт фрагмент"
                              : "Микрофон готов"}
                    </strong>
                  </div>
                  {/* Язык и режим стоят здесь, а не под дорожкой: между
                      дорожкой и кнопкой записи они попадали на глаза при каждом
                      нажатии, хотя меняются редко. В верхней строке они рядом с
                      состоянием микрофона и не спорят с кнопкой за внимание.
                      Подписей нет — варианты внутри списков говорят сами. */}
                  <div className="dictation-options">
                    <select
                      title="Язык распознавания"
                      aria-label="Язык распознавания"
                      value={dictationLanguage}
                      disabled={
                        recording || startingRecording || savingRecording
                      }
                      onChange={(event) =>
                        setDictationLanguage(
                          event.target.value as "auto" | "ru" | "en",
                        )
                      }
                    >
                      <option value="ru">Русский</option>
                      <option value="en">Английский</option>
                      <option value="auto">Авто</option>
                    </select>
                    <select
                      title="Режим распознавания"
                      aria-label="Режим распознавания"
                      value={dictationQuality}
                      disabled={
                        recording || startingRecording || savingRecording
                      }
                      onChange={(event) =>
                        setDictationQuality(
                          event.target.value as "fast" | "accurate",
                        )
                      }
                    >
                      <option value="accurate">Точно</option>
                      <option value="fast">Быстро</option>
                    </select>
                  </div>
                  <time>{formatRecorderTime(recordingSeconds)}</time>
                </div>

                <div
                  className="live-waveform"
                  aria-label={
                    recording
                      ? "Живая аудиодорожка"
                      : startingRecording
                        ? "Подключение к микрофону"
                        : "Микрофон ожидает"
                  }
                >
                  {waveform.map((height, index) => (
                    <i
                      key={index}
                      style={{ height: `${Math.max(8, height * 74)}px` }}
                    />
                  ))}
                </div>

                <div className="recorder-controls">
                  {recording ? (
                    <>
                      <button
                        className="record-mini record-cancel"
                        type="button"
                        title="Отменить запись"
                        aria-label="Отменить запись"
                        onClick={cancelDictationRecording}
                      >
                        ×
                      </button>
                      <button
                        className="record-stop"
                        type="button"
                        onClick={stopDictationRecording}
                      >
                        <span>■</span>
                        Остановить и сохранить
                      </button>
                      <button
                        className="record-mini record-confirm"
                        type="button"
                        title="В историю и скопировать"
                        aria-label="В историю и скопировать"
                        onClick={finishDictationRecordingToHistory}
                      >
                        ✓
                      </button>
                    </>
                  ) : (
                    <button
                      className="record-start"
                      type="button"
                      onClick={startDictationRecording}
                      disabled={savingRecording || startingRecording}
                    >
                      <span>●</span>
                      {startingRecording
                        ? "Подключаю микрофон…"
                        : currentDictation?.fragments.length
                          ? "Продолжить диктовку"
                          : "Начать диктовку"}
                    </button>
                  )}
                </div>
              </section>

              <section
                className="dictation-editor-card"
                aria-busy={currentDictationBusy}
              >
                <textarea
                  className="dictation-textarea"
                  ref={dictationTextareaRef}
                  value={dictationText}
                  // Поле не блокируется на время распознавания: расшифровка
                  // дописывается в конец и набранному не мешает.
                  onChange={(event) => {
                    setDictationText(event.target.value);
                    setDictationDirty(true);
                  }}
                  placeholder={
                    currentDictationBusy
                      ? "Whisper обрабатывает сохранённое аудио…"
                      : "Здесь появится транскрипция"
                  }
                  aria-label="Текст текущей диктовки"
                />

                {currentDictation?.fragments.length ? (
                  <div className="dictation-fragments">
                    {currentDictation.fragments.map((fragment) => (
                      <article key={fragment.id}>
                        <div className="fragment-number">
                          {String(fragment.position).padStart(2, "0")}
                        </div>
                        <div className="fragment-copy">
                          <strong>
                            {fragment.cancel_requested &&
                            fragment.status === "processing"
                              ? "Останавливаю…"
                              : fragment.status === "queued"
                                ? "В очереди"
                                : fragment.status === "processing"
                                  ? "Распознаётся"
                                  : fragment.status === "completed"
                                    ? "Готово"
                                    : fragment.status === "cancelled"
                                      ? "Остановлено — аудио сохранено"
                                      : "Нужна повторная попытка"}
                          </strong>
                          <small>
                            {formatDuration(fragment.duration_seconds)} ·{" "}
                            {languageLabels[fragment.language]}
                            {fragment.processing_seconds
                              ? ` · обработка ${formatDuration(fragment.processing_seconds)}`
                              : ""}
                            {/* Диктовка идёт отдельной очередью и вперёд
                                файлов, но если она всё-таки чего-то ждёт —
                                разметки спикеров, например — это должно быть
                                видно, а не выглядеть как зависание. */}
                            {fragment.status === "queued" &&
                              dictationWait(fragment.id)}
                          </small>
                          {fragment.error && <em>{fragment.error}</em>}
                        </div>
                        <audio
                          controls
                          preload="metadata"
                          src={`${API}/dictation-fragments/${fragment.id}/audio`}
                        />
                        <div className="fragment-actions">
                          <button
                            type="button"
                            className="reveal-audio"
                            title="Показать запись в папке — файл можно забрать"
                            aria-label="Показать запись в папке"
                            onClick={() => revealDictationAudio(fragment.id)}
                          >
                            🗂
                          </button>
                          {(fragment.status === "queued" ||
                            fragment.status === "processing") && (
                            <button
                              type="button"
                              className="stop-button"
                              disabled={
                                stoppingIds.has(fragment.id) ||
                                fragment.cancel_requested
                              }
                              onClick={() => cancelDictationFragment(fragment)}
                            >
                              {fragment.cancel_requested
                                ? "Останавливаю…"
                                : "◼ Остановить"}
                            </button>
                          )}
                          {(fragment.status === "failed" ||
                            fragment.status === "cancelled") && (
                            <button
                              type="button"
                              onClick={() =>
                                retryDictationFragment(fragment.id)
                              }
                            >
                              Повторить
                            </button>
                          )}
                          {/* Убрать один неудачный фрагмент, не удаляя всё
                              сообщение. Работает и на том, который считается
                              прямо сейчас: сервис сначала остановит его. */}
                          <button
                            type="button"
                            className="fragment-remove"
                            title="Убрать этот фрагмент из диктовки"
                            aria-label="Убрать этот фрагмент из диктовки"
                            onClick={() => removeDictationFragment(fragment)}
                          >
                            ✕
                          </button>
                        </div>
                      </article>
                    ))}
                  </div>
                ) : (
                  <div className="dictation-empty-fragments">
                    Аудиофрагментов нет
                  </div>
                )}

                <div className="dictation-editor-actions">
                  <button
                    type="button"
                    onClick={() => copyDictationText()}
                    disabled={!dictationText.trim()}
                  >
                    ⧉ Копировать
                  </button>
                  <button
                    className="primary-button"
                    type="button"
                    onClick={confirmCurrentDictation}
                    disabled={
                      recording ||
                      savingRecording ||
                      currentDictationBusy ||
                      !dictationText.trim()
                    }
                  >
                    OK · В историю
                  </button>
                </div>
              </section>
            </div>

            <section className="dictation-history-section">
              <div className="section-title">
                <div>
                  <div className="eyebrow">Сохранённые сообщения</div>
                  <h2>История диктовок</h2>
                </div>
                <div className="dictation-history-title-actions">
                  <span>{dictationTotal}</span>
                  {dictationTotal > 0 && (
                    <button type="button" onClick={clearDictationHistory}>
                      Очистить все
                    </button>
                  )}
                </div>
              </div>
              {dictationHistory.length ? (
                <div className="dictation-history">
                  {dictationHistory.map((message, index) => (
                    <article
                      key={message.id}
                      data-inline-editor={
                        editingHistoryId === message.id ? true : undefined
                      }
                    >
                      <header>
                        <div>
                          <strong>
                            Сообщение{" "}
                            {dictationTotal - dictationHistoryOffset - index}
                          </strong>
                          <small>
                            {shortDate(
                              message.confirmed_at || message.updated_at,
                            )}{" "}
                            · {message.fragments.length} аудиофрагментов
                            {message.project_id && (
                              <em className="history-saved">
                                сохранено в проект
                              </em>
                            )}
                          </small>
                        </div>
                        <div>
                          <button
                            type="button"
                            onClick={() => copyDictationText(message.text, false)}
                          >
                            ⧉ Копировать
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              setEditingHistoryId(message.id);
                              setHistoryTextDraft(message.text);
                            }}
                          >
                            ✎
                          </button>
                          <button
                            className="danger-ghost"
                            type="button"
                            onClick={() => deleteDictationMessage(message)}
                          >
                            ×
                          </button>
                        </div>
                      </header>
                      {editingHistoryId === message.id ? (
                        <div className="history-editor">
                          <textarea
                            value={historyTextDraft}
                            onChange={(event) =>
                              setHistoryTextDraft(event.target.value)
                            }
                            aria-label="Редактирование сообщения"
                          />
                          <div>
                            <button
                              className="save-edit"
                              type="button"
                              title="Сохранить"
                              aria-label="Сохранить сообщение"
                              onClick={() => saveHistoryMessage(message)}
                            >
                              ✓
                            </button>
                            {/* Копия в проект: диктовка остаётся в истории, но
                                важное рассуждение переживёт её очистку. */}
                            <button
                              type="button"
                              className="secondary-button"
                              onClick={() => saveDictationToProject(message)}
                            >
                              {message.project_id
                                ? "Сохранить в проект ещё раз"
                                : "Сохранить в проект"}
                            </button>
                          </div>
                        </div>
                      ) : (
                        <p>{message.text}</p>
                      )}
                      <details>
                        <summary>
                          Исходное аудио сохранено · открыть фрагменты
                        </summary>
                        <div className="history-audio-list">
                          {message.fragments.map((fragment) => (
                            <div key={fragment.id}>
                              <span>
                                Фрагмент {fragment.position} ·{" "}
                                {formatDuration(fragment.duration_seconds)}
                              </span>
                              <audio
                                controls
                                preload="metadata"
                                src={`${API}/dictation-fragments/${fragment.id}/audio`}
                              />
                              <button
                                type="button"
                                className="reveal-audio"
                                title="Показать запись в папке — файл можно забрать"
                                aria-label="Показать запись в папке"
                                onClick={() => revealDictationAudio(fragment.id)}
                              >
                                🗂
                              </button>
                            </div>
                          ))}
                        </div>
                      </details>
                    </article>
                  ))}
                </div>
              ) : (
                <div className="compact-empty">
                  История пуста
                </div>
              )}

              {dictationTotal > 0 && (
                <Pagination
                  page={dictationCurrentPage}
                  pageSize={dictationPageSize}
                  total={dictationTotal}
                  unit="сообщения"
                  onPage={setDictationPage}
                  onPageSize={(value) => {
                    setDictationPageSizeChoice(value);
                    setDictationPage(1);
                  }}
                />
              )}
            </section>
          </section>
        )}

        {screen === "settings" && (
          <section className="page settings-page">
            <header className="page-header">
              <div>
                <div className="eyebrow">Постоянные значения</div>
                <h1>
                  Настройки
                  <Hint text="Изменения сохраняются на этом компьютере и применяются ко всем новым проектам и файлам." />
                </h1>
              </div>
            </header>

            <div className="settings-layout">
              <section className="settings-card">
                <div className="settings-card-title">
                  <span>01</span>
                  <div>
                    <h2>
                      Интерфейс и проекты <Hint text="Внешний вид и организация библиотеки." />
                    </h2>
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Тема <Hint text="«Авто» берёт тему из системных настроек Windows и переключается вместе с ней." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["light", "dark", "auto"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={settings.theme === value ? "active" : ""}
                        onClick={() => saveSetting("theme", value)}
                      >
                        {value === "light"
                          ? "Светлая"
                          : value === "dark"
                            ? "Тёмная"
                            : "Авто"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Отображение проектов <Hint text="Общая лента либо группы по папкам." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["flat", "folders"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.project_view === value ? "active" : ""
                        }
                        onClick={() => saveSetting("project_view", value)}
                      >
                        {value === "flat" ? "Лента" : "По папкам"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Добавление файлов <Hint text="Единый режим для зон добавления во всех проектах." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["copy", "reference"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.default_import_mode === value ? "active" : ""
                        }
                        onClick={() =>
                          saveSetting("default_import_mode", value)
                        }
                      >
                        {value === "copy"
                          ? "Копировать в проект"
                          : "Оставлять на месте"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row stacked">
                  <div>
                    <strong>
                      Проектов на странице <Hint text="Лента загружает ровно одну страницу, а не всю библиотеку: на сотнях проектов это разница между мгновенным открытием и ожиданием при каждом обновлении. В самой ленте количество можно поменять на время — здесь задаётся значение, с которого она открывается." />
                    </strong>
                  </div>
                  <select
                    value={settings.projects_page_size}
                    onChange={(event) =>
                      saveSetting(
                        "projects_page_size",
                        event.target.value as PageSize,
                      )
                    }
                  >
                    {pageSizes.map((value) => (
                      <option key={value} value={value}>
                        {pageSizeLabel(value)}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="setting-row stacked">
                  <div>
                    <strong>
                      Формат названия нового проекта <Hint text="Название можно изменить в любой момент." />
                    </strong>
                  </div>
                  <select
                    value={settings.project_name_format}
                    onChange={(event) =>
                      saveSetting(
                        "project_name_format",
                        event.target
                          .value as Settings["project_name_format"],
                      )
                    }
                  >
                    {NAME_FORMATS.map((value) => (
                      <option key={value} value={value}>
                        {nameFormatLabel(value)}
                      </option>
                    ))}
                  </select>
                </div>
              </section>

              <section className="settings-card">
                <div className="settings-card-title">
                  <span>02</span>
                  <div>
                    <h2>
                      Распознавание по умолчанию <Hint text="Эти значения подставляются при добавлении файлов." />
                    </h2>
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Язык <Hint text="Ручной выбор ускоряет обработку." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["ru", "en", "auto"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.default_language === value ? "active" : ""
                        }
                        onClick={() => saveSetting("default_language", value)}
                      >
                        {languageLabels[value]}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Режим <Hint text="«Точно» используется для основного качества." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["accurate", "fast"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.default_quality === value ? "active" : ""
                        }
                        onClick={() => saveSetting("default_quality", value)}
                      >
                        {value === "accurate" ? "Точно" : "Быстро"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Таймкоды <Hint text="Сохраняются в исходных результатах файла." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["none", "segments", "words"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.default_timestamps === value ? "active" : ""
                        }
                        onClick={() => saveSetting("default_timestamps", value)}
                      >
                        {value === "none"
                          ? "Без"
                          : value === "segments"
                            ? "По фразам"
                            : "По словам"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Знаки препинания <Hint text="Оба варианта доступны без повторного распознавания." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["formatted", "plain"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.default_formatting === value ? "active" : ""
                        }
                        onClick={() => saveSetting("default_formatting", value)}
                      >
                        {value === "formatted" ? "Сохранять" : "Убирать"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Спикеры <Hint text="Отдельный локальный этап после распознавания. Модель выбирается строкой ниже." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["off", "on"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        disabled={
                          value === "on" && !anyEngineReady(diarizationStatus)
                        }
                        className={
                          settings.default_diarization === value ? "active" : ""
                        }
                        onClick={() => saveSetting("default_diarization", value)}
                      >
                        {value === "off" ? "Не разделять" : "Разделять"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Модель спикеров <Hint text="Pyannote — прежний проверенный вариант, на видеокарте занимает около 5.3 ГБ. Nemotron 3 от NVIDIA — до восьми голосов, размечает перебивания, около 1.4 ГБ видеопамяти. Это модель по умолчанию; для отдельного файла её можно выбрать при постановке в очередь или разметить готовый файл заново другой." />
                    </strong>
                  </div>
                  <div className="choice">
                    {diarizationEngines.map((engine) => (
                      <button
                        type="button"
                        key={engine}
                        disabled={
                          !engineReady(diarizationStatus, engine) &&
                          settings.diarization_engine !== engine
                        }
                        className={
                          settings.diarization_engine === engine ? "active" : ""
                        }
                        onClick={() => saveSetting("diarization_engine", engine)}
                      >
                        {diarizationEngineLabels[engine]}
                      </button>
                    ))}
                  </div>
                </div>
              </section>

              <section className="settings-card">
                <div className="settings-card-title">
                  <span>03</span>
                  <div>
                    <h2>
                      Просмотр и скачивание <Hint text="Общий режим для всех проектов. Внутри проекта его можно временно переключить." />
                    </h2>
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Таймкоды по умолчанию <Hint text="Применяется к просмотру, копированию и экспорту." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["none", "segments", "words"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.default_view_timestamps === value
                            ? "active"
                            : ""
                        }
                        onClick={() =>
                          saveSetting("default_view_timestamps", value)
                        }
                      >
                        {value === "none"
                          ? "Без"
                          : value === "segments"
                            ? "По фразам"
                            : "По словам"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Спикеры по умолчанию <Hint text="Показывает метки «Спикер 1», если файл распознан с разделением голосов." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["off", "on"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.default_view_speakers === value
                            ? "active"
                            : ""
                        }
                        onClick={() =>
                          saveSetting("default_view_speakers", value)
                        }
                      >
                        {value === "off" ? "Без спикеров" : "По спикерам"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Пунктуация по умолчанию <Hint text="Можно скачать оба варианта без повторного распознавания." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["formatted", "plain"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.default_view_formatting === value
                            ? "active"
                            : ""
                        }
                        onClick={() =>
                          saveSetting("default_view_formatting", value)
                        }
                      >
                        {value === "formatted" ? "Со знаками" : "Без знаков"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Формат по умолчанию <Hint text="Остальные форматы всегда остаются доступными." />
                    </strong>
                  </div>
                  <select
                    value={settings.default_export}
                    onChange={(event) =>
                      saveSetting(
                        "default_export",
                        event.target.value as ExportKind,
                      )
                    }
                  >
                    <option value="docx">DOCX — документ Word</option>
                    <option value="md">MD — разметка для передачи в ИИ</option>
                    <option value="txt">TXT — обычный текст</option>
                    <option value="srt">SRT — субтитры</option>
                    <option value="vtt">VTT — веб-субтитры</option>
                    <option value="json">JSON — структурированные данные</option>
                  </select>
                </div>
              </section>

              <section className="settings-card">
                <div className="settings-card-title">
                  <span>04</span>
                  <div>
                    <h2>
                      Хранение на диске <Hint text="Все пути видны и меняются через обычное окно Windows." />
                    </h2>
                  </div>
                </div>
                <div className="storage-setting">
                  <div>
                    <strong>
                      Проекты, управляемые копии и результаты <Hint text="Новый путь применяется к новым проектам. Старые проекты остаются там, где были созданы." />
                    </strong>
                    <code>{settings.managed_storage_root}</code>
                  </div>
                  <div>
                    <button
                      type="button"
                      onClick={() => openFolder(settings.managed_storage_root)}
                    >
                      Открыть
                    </button>
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => pickStorageFolder("managed_storage_root")}
                    >
                      Изменить…
                    </button>
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Сохранять извлечённое аудио <Hint text="Whisper распознаёт не видео, а извлечённую из него аудиодорожку. Час записи в этом виде весит около 115 МБ против нескольких гигабайт видео. Если сохранять — дорожка остаётся в папке проекта и её путь виден в карточке файла; если нет — удаляется сразу после распознавания, как раньше." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["on", "off"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.keep_extracted_audio === value ? "active" : ""
                        }
                        onClick={() =>
                          saveSetting("keep_extracted_audio", value)
                        }
                      >
                        {value === "on" ? "В проекте" : "Не сохранять"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row stacked">
                  <div>
                    <strong>
                      Качество скачивания по ссылке <Hint text="Подставляется как «Авто» в окне загрузки; выбрать другое качество для конкретного видео всегда можно. Точного совпадения обычно нет: сервис берёт ближайшее качество не выше выбранного, а если все доступные выше — самое низкое из них. Для расшифровки достаточно звука, и он весит в десятки раз меньше видео." />
                    </strong>
                  </div>
                  <select
                    value={settings.download_quality}
                    onChange={(event) =>
                      saveSetting(
                        "download_quality",
                        event.target.value as Settings["download_quality"],
                      )
                    }
                  >
                    <option value="audio">Только звук</option>
                    <option value="360">360p и ниже</option>
                    <option value="480">480p и ниже</option>
                    <option value="720">720p и ниже</option>
                    <option value="1080">1080p и ниже</option>
                    <option value="max">Максимальное</option>
                  </select>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Расшифровывать скачанное сразу <Hint text="Обычно после загрузки запись сразу уходит в очередь распознавания. Если видеокарта занята другой работой, выключите: проекты будут создаваться со скачанными файлами и ждать, пока вы не откроете их и не нажмёте «Распознать». В окне загрузки то же самое переключается галочкой на один раз." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["on", "off"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.download_transcribe === value ? "active" : ""
                        }
                        onClick={() =>
                          saveSetting("download_transcribe", value)
                        }
                      >
                        {value === "on" ? "Сразу" : "Только скачать"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row stacked">
                  <div>
                    <strong>
                      Cookies YouTube из браузера <Hint text="Нужно, только если YouTube требует подтвердить, что запрос не от робота. Обычно загрузка обходится без этого: сервис перебирает способы сам. Cookies привязывают загрузки к вашему аккаунту Google, а YouTube за выгрузки может его ограничить — поэтому по умолчанию выключено. На Windows читается только Firefox: Chrome, Edge и Opera шифруют свою базу. Надёжнее положить файл cookies.txt в папку ниже." />
                    </strong>
                  </div>
                  <select
                    value={settings.youtube_cookies}
                    onChange={(event) =>
                      saveSetting(
                        "youtube_cookies",
                        event.target.value as Settings["youtube_cookies"],
                      )
                    }
                  >
                    <option value="off">Не брать</option>
                    <option value="auto">Автоматически</option>
                    <option value="firefox">Firefox</option>
                    <option value="chrome">Chrome</option>
                    <option value="edge">Edge</option>
                    <option value="opera">Opera</option>
                    <option value="brave">Brave</option>
                    <option value="vivaldi">Vivaldi</option>
                    <option value="chromium">Chromium</option>
                  </select>
                </div>
                <div className="storage-setting">
                  <div>
                    <strong>
                      Папка для cookies <Hint text="Сюда кладётся файл cookies.txt, выгруженный из браузера расширением. Он нужен, когда YouTube просит подтвердить, что вы не робот, и обходные пути не помогли. В папке лежит инструкция." />
                    </strong>
                    <code>{settings.cookies_root}</code>
                  </div>
                  <div>
                    <button
                      type="button"
                      onClick={() => openFolder(settings.cookies_root)}
                    >
                      Открыть
                    </button>
                  </div>
                </div>
                <div className="storage-setting">
                  <div>
                    <strong>
                      Папка для временного аудио <Hint text="Здесь дорожка лежит во время распознавания, когда сохранение в проект выключено. После файла она удаляется." />
                    </strong>
                    <code>{settings.temporary_storage_root}</code>
                  </div>
                  <div>
                    <button
                      type="button"
                      onClick={() => openFolder(settings.temporary_storage_root)}
                    >
                      Открыть
                    </button>
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() =>
                        pickStorageFolder("temporary_storage_root")
                      }
                    >
                      Изменить…
                    </button>
                  </div>
                </div>
              </section>

              <section className="settings-card">
                <div className="settings-card-title">
                  <span>05</span>
                  <div>
                    <h2>
                      Спикеры <Hint text="Локальное разделение голосов. Моделей две: Pyannote Community-1 и NVIDIA Nemotron 3 Diarization — какая лучше на ваших записях, покажет сравнение." />
                    </h2>
                  </div>
                  <span
                    className={`activation-badge ${
                      anyEngineReady(diarizationStatus) ? "ready" : ""
                    }`}
                  >
                    {anyEngineReady(diarizationStatus)
                      ? "Активировано"
                      : "Нужна активация"}
                  </span>
                </div>
                <div className="diarization-setup">
                  <div className="diarization-model-card">
                    <div>
                      <strong>
                        {diarizationStatus?.model_name ||
                          "pyannote/speaker-diarization-community-1"}
                      </strong>
                      {/* Путь установки ушёл в подсказку: в глаза он не нужен,
                          а когда понадобится — доступен по наведению. */}
                      <small>
                        {diarizationStatus?.model_ready
                          ? "Установлена локально"
                          : "Пока не установлена"}
                        {diarizationStatus?.runtime && (
                          <Hint
                            text={`Среда: ${diarizationStatus.runtime}`}
                          />
                        )}
                      </small>
                    </div>
                    <span
                      className={
                        diarizationStatus?.model_ready ? "ready" : ""
                      }
                    >
                      {diarizationStatus?.model_ready
                        ? "Модель есть"
                        : "Нужно установить"}
                    </span>
                  </div>
                  {(() => {
                    const nemotron = diarizationStatus?.engines?.nemotron;
                    const downloading =
                      nemotron?.download?.status === "running";
                    const downloaded = nemotron?.download?.downloaded_bytes || 0;
                    const total = nemotron?.download_total_bytes || 0;
                    return (
                      <div className="diarization-model-card">
                        <div>
                          <strong>
                            {nemotron?.model_name ||
                              "nvidia/Nemotron-3-Diarization"}
                          </strong>
                          <small>
                            {nemotron?.model_ready
                              ? `Установлена локально${
                                  nemotron.available && nemotron.device
                                    ? nemotron.device === "cuda"
                                      ? " · видеокарта"
                                      : " · процессор"
                                    : ""
                                }`
                              : !nemotron?.package_installed
                                ? "В среде разметки нет нужной версии transformers — запустите «Установить Whisper.bat»"
                                : downloading
                                  ? `Скачиваю… ${Math.round(downloaded / 1048576)} из ~${Math.round(total / 1048576)} МБ`
                                  : "Модель не скачана · около 400 МБ, токен не нужен"}
                            {nemotron?.runtime && (
                              <Hint text={`Среда: ${nemotron.runtime}`} />
                            )}
                          </small>
                          {nemotron?.download?.status === "error" &&
                            nemotron.download.message && (
                              <small className="setup-warning">
                                {nemotron.download.message}
                              </small>
                            )}
                        </div>
                        {nemotron?.model_ready ? (
                          <span className="ready">Модель есть</span>
                        ) : (
                          <button
                            className="primary-button"
                            type="button"
                            disabled={
                              !nemotron?.package_installed ||
                              downloading ||
                              nemotronDownloadStarting
                            }
                            onClick={downloadNemotron}
                          >
                            {downloading || nemotronDownloadStarting
                              ? "Скачиваю…"
                              : "Скачать модель"}
                          </button>
                        )}
                      </div>
                    );
                  })()}
                  <div className="setting-row diarization-device-row">
                    <div>
                      <strong>
                      Устройство <Hint text="CPU нужен как запасной режим, видеокарта быстрее." />
                    </strong>
                    </div>
                    <div className="choice">
                      {(["auto", "cuda", "cpu"] as const).map((value) => (
                        <button
                          type="button"
                          key={value}
                          disabled={
                            value === "cuda" &&
                            !diarizationStatus?.cuda_available
                          }
                          className={
                            settings.diarization_device === value
                              ? "active"
                              : ""
                          }
                          onClick={() =>
                            saveSetting("diarization_device", value)
                          }
                        >
                          {value === "auto"
                            ? "Авто"
                            : value === "cuda"
                              ? "Видеокарта"
                              : "Процессор"}
                        </button>
                      ))}
                    </div>
                  </div>
                  {diarizationStatus?.available ? (
                    <div className="activation-ready">
                      <strong>
                        Pyannote готов к работе
                        {diarizationStatus.device === "cuda"
                          ? " · видеокарта"
                          : diarizationStatus.device === "cpu"
                            ? " · процессор"
                            : ""}
                      </strong>
                      {/* Пояснение и путь — в подсказку. Предупреждение про
                          процессор ниже остаётся видимым: это не пояснение, а
                          последствие, о котором надо знать заранее. */}
                      <Hint
                        text={`Для новых файлов можно включать «Разделить · Pyannote». Модель работает локально${
                          diarizationStatus.runtime
                            ? ` через ${diarizationStatus.runtime}`
                            : ""
                        }.`}
                      />
                      {diarizationStatus.device === "cpu" && (
                        <small className="diarization-slow-note">
                          Считает на процессоре — это примерно в 20 раз
                          медленнее видеокарты. Час записи займёт около двух
                          часов.
                        </small>
                      )}
                    </div>
                  ) : (
                    <>
                      <div>
                        <strong>Одноразовая активация Pyannote</strong>
                        <p>
                          Примите условия Community-1 на Hugging Face, создайте
                          token с правом чтения и вставьте его сюда. Token
                          сохранится только локально.
                        </p>
                        {!diarizationStatus?.package_installed && (
                          <small className="setup-warning">
                            Компонент Pyannote сейчас устанавливается. Если
                            статус не изменится после перезапуска — повторите
                            установку сервиса.
                          </small>
                        )}
                        <a
                          href="https://huggingface.co/pyannote/speaker-diarization-community-1"
                          target="_blank"
                          rel="noreferrer"
                        >
                          Открыть страницу Community-1 ↗
                        </a>
                      </div>
                      <div className="token-form">
                        <input
                          type="password"
                          value={diarizationToken}
                          onChange={(event) =>
                            setDiarizationToken(event.target.value)
                          }
                          placeholder="hf_…"
                          autoComplete="off"
                          aria-label="Hugging Face token"
                        />
                        <button
                          className="primary-button"
                          type="button"
                          disabled={
                            activatingDiarization ||
                            !diarizationStatus?.package_installed ||
                            !diarizationToken.trim()
                          }
                          onClick={activateDiarization}
                        >
                          {activatingDiarization
                            ? "Проверяю и загружаю…"
                            : "Активировать"}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </section>

              <section className="settings-card">
                <div className="settings-card-title">
                  <span>06</span>
                  <div>
                    <h2>
                      Подключённые сервисы <Hint text="Другие программы на этом компьютере, которые используют этот Whisper как транскрибатор. Каждая получает свою папку в списке проектов, а её проекты помечены в общей ленте — чтобы не путать их со своими. Сервис опознаётся по заголовку запроса." />
                    </h2>
                  </div>
                  <span className="card-badge">
                    {services.filter((item) => item.projects > 0).length} из{" "}
                    {services.length}
                  </span>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Проекты сервисов в общей ленте <Hint text="Скрытый сервис уходит с экрана проектов целиком — и его проекты, и его папка слева. Ничего не удаляется: переключите на «Показывать», и папка со всем содержимым снова на месте. В управлении папками ниже она видна всегда." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["hide", "show"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.integration_feed === value ? "active" : ""
                        }
                        onClick={() => {
                          saveSetting("integration_feed", value);
                          // Папка сервиса исчезает из навигации прямо сейчас.
                          // Если человек стоял внутри неё, фильтр надо вернуть
                          // к общей ленте: иначе он останется в списке, которого
                          // больше нет среди папок, и не поймёт, где находится.
                          if (value === "hide") showFolder("all");
                        }}
                      >
                        {value === "hide" ? "Скрывать" : "Показывать"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Телеграм-бот при запуске <Hint text="Бот принимает голосовые, файлы и ссылки в Телеграме и присылает расшифровку обратно. Выключение не стирает токен и не отзывает бота: он просто не поднимается вместе с сервисом, пока не понадобится снова. Настройка применяется при следующем запуске Whisper." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["on", "off"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.telegram_autostart === value ? "active" : ""
                        }
                        onClick={() => saveSetting("telegram_autostart", value)}
                      >
                        {value === "on" ? "Запускать" : "Не запускать"}
                      </button>
                    ))}
                  </div>
                </div>
                {services.length === 0 ? (
                  <div className="settings-note">
                    Пока ни один внешний сервис не настроен.
                  </div>
                ) : (
                  <div className="component-group">
                    {services.map((service) => (
                      <div className="component-row" key={service.key}>
                        <i
                          className={
                            service.projects
                              ? "component-dot ready"
                              : "component-dot optional"
                          }
                          aria-hidden="true"
                        />
                        <div className="component-main">
                          <strong>
                            {service.title}
                            <Hint
                              text={`Папка «${service.title}» в списке проектов. Сервис опознаётся заголовком X-Whisper-Integration: ${service.header}`}
                            />
                          </strong>
                          <small>
                            {service.projects
                              ? `Проектов: ${service.projects} · файлов: ${service.files}` +
                                (service.last_used_at
                                  ? ` · последний раз ${shortDate(service.last_used_at)}`
                                  : "")
                              : "Ещё ни разу не обращался"}
                          </small>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              <section className="settings-card">
                <div className="settings-card-title">
                  <span>07</span>
                  <div>
                    <h2>
                      Модели и зависимости <Hint text="Из чего собран сервис: модели распознавания, программы и пакеты Python. Здесь видно, что установлено, где лежит и откуда скачано — чтобы восстановить всё на новой машине или после переустановки Windows." />
                    </h2>
                  </div>
                  <span
                    className={
                      components?.ready ? "card-badge ready" : "card-badge"
                    }
                  >
                    {!components
                      ? "Проверяю…"
                      : components.ready
                        ? "Всё на месте"
                        : `Не хватает: ${components.missing_required.length}`}
                  </span>
                </div>

                {components && (
                  <>
                    {/* Сервис говорит не «я не работаю», а что именно не
                        работает и что с этим сделать. */}
                    {components.missing_required.length > 0 && (
                      <div className="settings-note components-alert">
                        Без этих компонентов распознавание не пойдёт. Модели
                        сервис скачает сам, программы ставятся командой из
                        подсказки.
                      </div>
                    )}

                    {(["model", "runtime", "python"] as const).map((group) => {
                      const list = components.items.filter(
                        (item) => item.group === group,
                      );
                      if (!list.length) return null;
                      return (
                        <div className="component-group" key={group}>
                          <div className="component-group-title">
                            {group === "model"
                              ? "Модели распознавания"
                              : group === "runtime"
                                ? "Программы в системе"
                                : "Пакеты Python"}
                          </div>
                          {list.map((item) => (
                            <div className="component-row" key={item.key}>
                              <i
                                className={
                                  item.present
                                    ? "component-dot ready"
                                    : item.required
                                      ? "component-dot missing"
                                      : "component-dot optional"
                                }
                                aria-hidden="true"
                              />
                              <div className="component-main">
                                <strong>
                                  {item.title}
                                  {!item.required && (
                                    <em className="component-optional">
                                      необязательно
                                    </em>
                                  )}
                                  <Hint
                                    text={[
                                      item.present
                                        ? "Установлено."
                                        : `Не установлено. Без этого: ${item.impact.toLowerCase()}.`,
                                      item.path && `Путь: ${item.path}`,
                                      item.source && `Источник: ${item.source}`,
                                      item.installed_at &&
                                        `Установлено: ${shortDate(item.installed_at)}`,
                                      item.command &&
                                        !item.present &&
                                        `Команда: ${item.command}`,
                                    ]
                                      .filter(Boolean)
                                      .join(" · ")}
                                  />
                                </strong>
                                <small>
                                  {item.present
                                    ? item.detail
                                    : `Нет · ${item.impact}`}
                                  {item.size_bytes
                                    ? ` · ${fileSize(item.size_bytes)}`
                                    : ""}
                                </small>
                              </div>
                              {!item.present && item.install === "manual" && (
                                <button
                                  type="button"
                                  className="secondary-button"
                                  title={item.command}
                                  onClick={() =>
                                    copyPlainText(item.command, "Команда")
                                  }
                                >
                                  Скопировать команду
                                </button>
                              )}
                            </div>
                          ))}
                        </div>
                      );
                    })}

                    {/* Загрузка того, что сервис умеет поставить сам. */}
                    {(components.missing_required.includes("model_ru") ||
                      components.missing_optional.includes("model_en")) && (
                      <div className="component-download">
                        {modelDownload?.status === "running" ? (
                          <>
                            <div className="component-download-line">
                              <strong>{modelDownload.message}</strong>
                              <span>
                                {modelDownload.percent == null
                                  ? "…"
                                  : `${modelDownload.percent}%`}
                              </span>
                            </div>
                            <div className="progress-row">
                              <div>
                                <i
                                  style={{
                                    width: `${Math.max(modelDownload.percent || 0, 2)}%`,
                                  }}
                                />
                              </div>
                            </div>
                            <small>
                              {fileSize(modelDownload.downloaded_bytes || 0)}
                              {modelDownload.total_bytes
                                ? ` из ${fileSize(modelDownload.total_bytes)}`
                                : ""}
                              {modelDownload.speed_bytes
                                ? ` · ${fileSize(modelDownload.speed_bytes)}/с`
                                : ""}
                              {modelDownload.eta_seconds
                                ? ` · осталось ${formatDuration(modelDownload.eta_seconds)}`
                                : ""}
                            </small>
                          </>
                        ) : (
                          <>
                            <div className="component-download-line">
                              <strong>
                                {modelDownload?.status === "error"
                                  ? "Загрузка не удалась"
                                  : "Скачать отсутствующие модели"}
                              </strong>
                              <button
                                type="button"
                                className="primary-button"
                                onClick={downloadModels}
                              >
                                {modelDownload?.status === "error"
                                  ? "Повторить"
                                  : "Скачать"}
                              </button>
                            </div>
                            <small>
                              {modelDownload?.error ||
                                "Загрузка идёт с Hugging Face и продолжается с места обрыва, если связь пропадёт."}
                            </small>
                          </>
                        )}
                      </div>
                    )}

                    <div className="component-footer">
                      <button
                        type="button"
                        className="secondary-button"
                        disabled={componentsBusy}
                        onClick={() => loadComponents(true)}
                      >
                        {componentsBusy ? "Проверяю…" : "Проверить заново"}
                      </button>
                      <small>
                        Всего на диске: {fileSize(components.total_size_bytes)}
                        <Hint
                          text={`Папка сервиса: ${components.project_root} · База проектов: ${components.database_path}`}
                        />
                      </small>
                    </div>
                  </>
                )}
              </section>

              <section className="settings-card">
                <div className="settings-card-title">
                  <span>08</span>
                  <div>
                    <h2>
                      Диктовка <Hint text="Отдельные настройки быстрого микрофонного режима." />
                    </h2>
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Язык по умолчанию <Hint text="Подставляется при новой записи с микрофона." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["ru", "en", "auto"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.dictation_default_language === value
                            ? "active"
                            : ""
                        }
                        onClick={() =>
                          saveSetting("dictation_default_language", value)
                        }
                      >
                        {languageLabels[value]}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>
                      Режим по умолчанию <Hint text="Для диктовки обычно удобнее быстрый режим." />
                    </strong>
                  </div>
                  <div className="choice">
                    {(["fast", "accurate"] as const).map((value) => (
                      <button
                        type="button"
                        key={value}
                        className={
                          settings.dictation_default_quality === value
                            ? "active"
                            : ""
                        }
                        onClick={() =>
                          saveSetting("dictation_default_quality", value)
                        }
                      >
                        {value === "fast" ? "Быстро" : "Точно"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="setting-row stacked">
                  <div>
                    <strong>
                      Сообщений истории на странице <Hint text="История диктовок подгружается страницами: сама запись и правка от этого не зависят, черновик виден на любой странице." />
                    </strong>
                  </div>
                  <select
                    value={settings.dictation_page_size}
                    onChange={(event) =>
                      saveSetting(
                        "dictation_page_size",
                        event.target.value as PageSize,
                      )
                    }
                  >
                    {pageSizes.map((value) => (
                      <option key={value} value={value}>
                        {pageSizeLabel(value)}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="storage-setting">
                  <div>
                    <strong>
                      Исходные аудиофайлы{" "}
                      <Hint
                        text={`Хранятся после распознавания, правки, копирования и переноса в историю. Папка: ${settings.dictations_root}`}
                      />
                    </strong>
                  </div>
                  <div>
                    <button
                      type="button"
                      onClick={() => openFolder(settings.dictations_root)}
                    >
                      Открыть
                    </button>
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <strong>История диктовок</strong>
                    <small>
                      Сейчас сохранено сообщений: {dictationTotal}.
                    </small>
                  </div>
                  <button
                    className="danger-ghost"
                    type="button"
                    disabled={!dictationTotal}
                    onClick={clearDictationHistory}
                  >
                    Очистить историю
                  </button>
                </div>
              </section>

              <section className="settings-card">
                <div className="settings-card-title">
                  <span>09</span>
                  <div>
                    <h2>
                      Папки <Hint text="Объединяйте связанные проекты в коллекции." />
                    </h2>
                  </div>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={createFolder}
                  >
                    ＋ Новая папка
                  </button>
                </div>
                {folders.length === 0 ? (
                  <div className="settings-note">
                    Папок пока нет. Проекты отображаются в разделе «Без папки».
                  </div>
                ) : (
                  <div className="folder-manager">
                    {folders.map((folder) => {
                      const editing = editingFolderId === folder.id;
                      return (
                        <div
                          key={folder.id}
                          className={editing ? "editing" : ""}
                          data-inline-editor={editing ? true : undefined}
                        >
                          <span>⌑</span>
                          <div>
                            {editing ? (
                              <input
                                autoFocus
                                value={folderNameDraft}
                                maxLength={120}
                                aria-label="Название папки"
                                onChange={(event) =>
                                  setFolderNameDraft(event.target.value)
                                }
                                onKeyDown={(event) => {
                                  if (event.key === "Enter") {
                                    saveFolder(folder);
                                  }
                                  if (event.key === "Escape") cancelFolderEdit();
                                }}
                              />
                            ) : (
                              <strong>{folder.name}</strong>
                            )}
                            {editing ? (
                              <label className="folder-feed-toggle">
                                <input
                                  type="checkbox"
                                  checked={folderFeedDraft}
                                  onChange={(event) =>
                                    setFolderFeedDraft(event.target.checked)
                                  }
                                />
                                <span>Показывать в общей ленте</span>
                              </label>
                            ) : null}
                            <small>
                              {folder.project_count} проектов
                              {folder.feed_visible === false && (
                                <em className="project-origin">
                                  не в общей ленте
                                </em>
                              )}
                              {folder.integration && (
                                <em className="project-origin">
                                  внешний сервис
                                </em>
                              )}
                            </small>
                          </div>
                          {editing ? (
                            <div className="folder-manager-actions">
                              <button
                                className="save-edit"
                                type="button"
                                title="Сохранить"
                                aria-label="Сохранить папку"
                                onClick={() => saveFolder(folder)}
                              >
                                ✓
                              </button>
                              {!folder.integration && (
                                <button
                                  className="danger-ghost"
                                  type="button"
                                  title="Удалить папку и проекты"
                                  aria-label="Удалить папку и проекты"
                                  onClick={() => deleteFolder(folder)}
                                >
                                  ×
                                </button>
                              )}
                            </div>
                          ) : (
                            <button
                              type="button"
                              title="Редактировать папку"
                              aria-label={`Редактировать папку ${folder.name}`}
                              onClick={() => startFolderEdit(folder)}
                            >
                              ✎
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>
            </div>
          </section>
        )}
      </main>

      {urlImportOpen && (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={() => setUrlImportOpen(false)}
        >
          <form
            className="modal url-download-modal"
            onSubmit={startUrlDownload}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <button
              className="modal-close"
              type="button"
              aria-label="Закрыть"
              title="Закрыть"
              onClick={() => setUrlImportOpen(false)}
            >
              ×
            </button>
            <div className="modal-symbol">🔗</div>
            <h2>Скачать по ссылке</h2>

            {!activeUrlDownload ? (
              <>
                {urlTargetProject && (
                  <div className="url-target-project">
                    В проект: <strong>{urlTargetProject.name}</strong>
                  </div>
                )}
                <label>
                  <span>
                    Ссылка{" "}
                    <Hint text="Вставьте ссылку на видео или публичный файл. После скачивания проект автоматически попадёт в очередь транскрибации. Поддерживаются YouTube, VK, RuTube, Яндекс.Диск и Google Drive." />
                  </span>
                  <div className="url-probe-row">
                    <input
                      autoFocus
                      type="url"
                      value={urlValue}
                      placeholder="https://…"
                      onChange={(event) => {
                        setUrlValue(event.target.value);
                        setUrlMetadata(null);
                        setUrlError("");
                      }}
                    />
                    <button
                      type="button"
                      disabled={!urlValue.trim() || urlProbing}
                      onClick={probeUrl}
                    >
                      {urlProbing ? "Проверяю…" : "Проверить"}
                    </button>
                  </div>
                </label>

                {/* Поле появляется только для источников, где код нужен. Признак
                    проверяется по домену: у Zoom запись закрыта кодом, который
                    вводится на её странице, а не передаётся в ссылке. */}
                {/zoom\.us$|\.zoom\.us/i.test(
                  (() => {
                    try {
                      return new URL(urlValue.trim()).hostname;
                    } catch {
                      return "";
                    }
                  })(),
                ) && (
                  <label>
                    <span>
                      Код доступа{" "}
                      <Hint text="Zoom закрывает запись кодом доступа: его выдают вместе со ссылкой. Код нужен и для проверки ссылки, и для самой загрузки." />
                    </span>
                    <input
                      type="text"
                      value={urlPasscode}
                      placeholder="Код из приглашения"
                      autoComplete="off"
                      onChange={(event) => {
                        setUrlPasscode(event.target.value);
                        setUrlMetadata(null);
                        setUrlError("");
                      }}
                    />
                  </label>
                )}

                {urlMetadata && (
                  <>
                    <div className="url-metadata">
                      <strong>{urlMetadata.title}</strong>
                      <span>
                        {urlMetadata.provider_label}
                        {urlMetadata.uploader
                          ? ` · ${urlMetadata.uploader}`
                          : ""}
                      </span>
                      <span>
                        {formatDuration(urlMetadata.duration_sec)}
                        {urlMetadata.filesize_bytes
                          ? ` · около ${fileSize(urlMetadata.filesize_bytes)}`
                          : ""}
                      </span>
                    </div>
                    <div
                      className={`url-download-fields ${
                        urlTargetProject ? "single" : ""
                      }`}
                    >
                      {!urlTargetProject && (
                        <>
                          <label>
                            <span>Название проекта</span>
                            <input
                              value={urlProjectName}
                              maxLength={120}
                              onChange={(event) =>
                                setUrlProjectName(event.target.value)
                              }
                            />
                          </label>
                          <label>
                            <span>Папка</span>
                            <select
                              value={urlFolderId}
                              onChange={(event) =>
                                setUrlFolderId(event.target.value)
                              }
                            >
                              <option value="">Без папки</option>
                              {visibleFolders.map((folder) => (
                                <option key={folder.id} value={folder.id}>
                                  {folder.name}
                                </option>
                              ))}
                            </select>
                          </label>
                        </>
                      )}
                      <label className="url-quality-field">
                        <span>Качество</span>
                        <select
                          value={urlQuality}
                          onChange={(event) =>
                            setUrlQuality(event.target.value)
                          }
                        >
                          {/* Подпись зависит от настройки: раньше здесь были
                              зашитые «до 1080p», и после появления настройки это
                              стало неправдой. */}
                          <option value="auto">
                            {settings.download_quality === "audio"
                              ? "Авто — только звук"
                              : settings.download_quality === "max"
                                ? "Авто — максимальное"
                                : `Авто — до ${settings.download_quality}p`}
                          </option>
                          {urlMetadata.qualities.map((option) => (
                            <option
                              key={option.quality}
                              value={option.quality}
                            >
                              {option.label}
                              {option.filesize_bytes
                                ? ` · около ${fileSize(option.filesize_bytes)}`
                                : ""}
                            </option>
                          ))}
                        </select>
                        <strong className="url-size-estimate">
                          {selectedUrlEstimate
                            ? `Примерный размер: ≈ ${fileSize(selectedUrlEstimate)}`
                            : "Размер станет известен после начала загрузки"}
                        </strong>
                      </label>
                    </div>
                    <label className="url-transcribe-toggle">
                      <input
                        type="checkbox"
                        checked={!urlTranscribe}
                        onChange={(event) =>
                          setUrlTranscribe(!event.target.checked)
                        }
                      />
                      <span>
                        Только скачать, без расшифровки <Hint text="Запись ляжет в проект готовым файлом и будет ждать. Поставить её в очередь можно потом — когда освободится очередь распознавания или когда дойдут руки." />
                      </span>
                    </label>
                    <div
                      className="url-transcription-settings"
                      hidden={!urlTranscribe}
                    >
                      <label>
                        <span>Язык</span>
                        <select
                          value={urlLanguage}
                          onChange={(event) =>
                            setUrlLanguage(
                              event.target.value as JobOptions["language"],
                            )
                          }
                        >
                          <option value="ru">Русский</option>
                          <option value="en">Английский</option>
                          <option value="auto">Авто</option>
                        </select>
                      </label>
                      <label>
                        <span>Распознавание</span>
                        <select
                          value={urlRecognitionQuality}
                          onChange={(event) =>
                            setUrlRecognitionQuality(
                              event.target.value as JobOptions["quality"],
                            )
                          }
                        >
                          <option value="accurate">Точно</option>
                          <option value="fast">Быстро</option>
                        </select>
                      </label>
                      <label>
                        <span>Спикеры</span>
                        <DiarizationSelect
                          enabled={urlDiarization}
                          engine={urlDiarizationEngine}
                          status={diarizationStatus}
                          disabled={!anyEngineReady(diarizationStatus)}
                          offLabel="Без спикеров"
                          onChange={(enabled, engine) => {
                            setUrlDiarization(enabled);
                            setUrlDiarizationEngine(engine);
                          }}
                        />
                      </label>
                    </div>
                  </>
                )}

                {urlError && <div className="url-download-error">{urlError}</div>}
                {/* Нижняя кнопка появляется только после успешной проверки.
                    Раньше она показывалась сразу с текстом «Проверить ссылку»,
                    и в окне было две кнопки проверки одновременно. */}
                {urlMetadata && (
                  <div className="modal-actions">
                    <button
                      className="primary-button"
                      type="submit"
                      disabled={urlStarting || urlProbing || !urlValue.trim()}
                    >
                      {urlStarting
                        ? "Запускаю…"
                        : urlTranscribe
                          ? "Скачать и транскрибировать"
                          : "Только скачать"}
                    </button>
                  </div>
                )}
              </>
            ) : (
              <div className="url-download-state">
                <div className="url-download-title">
                  <div>
                    <strong>{activeUrlDownload.title}</strong>
                    <span>
                      {activeUrlDownload.provider_label} ·{" "}
                      {urlDownloadStatusLabel(activeUrlDownload.status)}
                    </span>
                  </div>
                  <b>
                    {activeUrlDownload.percent == null
                      ? "—"
                      : `${Math.round(activeUrlDownload.percent)}%`}
                  </b>
                </div>
                <div className="url-progress">
                  <i
                    style={{
                      width: `${Math.max(activeUrlDownload.percent || 0, 2)}%`,
                    }}
                  />
                </div>
                <div className="url-download-stats">
                  <span>
                    {fileSize(activeUrlDownload.downloaded_bytes)}
                    {activeUrlDownload.total_bytes
                      ? ` из ${fileSize(activeUrlDownload.total_bytes)}`
                      : ""}
                  </span>
                  {activeUrlDownload.speed_bps ? (
                    <span>{fileSize(activeUrlDownload.speed_bps)}/с</span>
                  ) : null}
                  {activeUrlDownload.eta_sec ? (
                    <span>Осталось {formatDuration(activeUrlDownload.eta_sec)}</span>
                  ) : null}
                </div>

                {activeUrlDownload.status === "reconnecting" && (
                  <div className="url-download-note">
                    Соединение оборвалось. Докачка продолжится автоматически с
                    сохранённого места.
                  </div>
                )}
                {activeUrlDownload.status === "completed" && (
                  <div className="url-download-success">
                    Файл скачан и поставлен в общую очередь транскрибации.
                  </div>
                )}
                {activeUrlDownload.error && (
                  <div className="url-download-error">
                    {activeUrlDownload.error}
                  </div>
                )}

                <div className="url-download-controls">
                  {activeUrlStatuses.includes(activeUrlDownload.status) &&
                    !["pausing", "stopping", "finalizing"].includes(
                      activeUrlDownload.status,
                    ) && (
                      <button
                        type="button"
                        onClick={() => controlUrlDownload("pause")}
                      >
                        Ⅱ Пауза
                      </button>
                    )}
                  {(activeUrlDownload.status === "paused" ||
                    activeUrlDownload.status === "interrupted" ||
                    (activeUrlDownload.status === "error" &&
                      activeUrlDownload.resumable)) && (
                    <button
                      className="primary-button"
                      type="button"
                      onClick={() => controlUrlDownload("resume")}
                    >
                      ▶ Продолжить
                    </button>
                  )}
                  {!["completed", "cancelled", "stopping"].includes(
                    activeUrlDownload.status,
                  ) && (
                    <button
                      className="danger-ghost"
                      type="button"
                      onClick={() => controlUrlDownload("cancel")}
                    >
                      ■ Остановить
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      setUrlImportOpen(false);
                      openProject(activeUrlDownload.project_id);
                    }}
                  >
                    Открыть проект
                  </button>
                  {["completed", "cancelled"].includes(
                    activeUrlDownload.status,
                  ) && (
                    <button
                      type="button"
                      onClick={() => {
                        setActiveUrlDownloadId("");
                        setUrlValue("");
                        setUrlMetadata(null);
                        setUrlProjectName("");
                        setUrlQuality("auto");
                        setUrlError("");
                      }}
                    >
                      Скачать ещё
                    </button>
                  )}
                </div>
                {urlError && <div className="url-download-error">{urlError}</div>}
              </div>
            )}
          </form>
        </div>
      )}

      {rerunJob && rerunJob.options.quality === "fast" && (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={() => setRerunJobId("")}
        >
          <form
            className="modal rerun-modal"
            onSubmit={rerunCompletedJob}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <button
              className="modal-close"
              type="button"
              aria-label="Закрыть"
              title="Закрыть"
              onClick={() => setRerunJobId("")}
            >
              ×
            </button>
            <div className="modal-symbol">↻</div>
            <h2>Распознать точнее</h2>
            <p className="rerun-source">{rerunJob.original_name}</p>
            <div className="rerun-fields">
              <label>
                <span>Язык</span>
                <select
                  value={rerunLanguage}
                  onChange={(event) =>
                    setRerunLanguage(
                      event.target.value as JobOptions["language"],
                    )
                  }
                >
                  <option value="ru">Русский</option>
                  <option value="en">Английский</option>
                  <option value="auto">Авто</option>
                </select>
              </label>
              <label>
                <span>Режим</span>
                <div className="locked-field">Точно</div>
              </label>
              <label>
                <span>Спикеры</span>
                <DiarizationSelect
                  enabled={rerunDiarization}
                  engine={rerunDiarizationEngine}
                  status={diarizationStatus}
                  disabled={!anyEngineReady(diarizationStatus)}
                  offLabel="Без спикеров"
                  onChange={(enabled, engine) => {
                    setRerunDiarization(enabled);
                    setRerunDiarizationEngine(engine);
                  }}
                />
              </label>
            </div>
            <div className="rerun-note">
              Результат заменится только после успешного точного распознавания.
              При ошибке останется предыдущая версия.
            </div>
            <div className="modal-actions">
              <button
                className="primary-button"
                type="submit"
                disabled={rerunStarting}
              >
                {rerunStarting ? "Ставлю в очередь…" : "Запустить точный режим"}
              </button>
            </div>
          </form>
        </div>
      )}

      {rediarizeJob && (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={() => setRediarizeJobId("")}
        >
          <form
            className="modal rerun-modal"
            onSubmit={rediarizeCompletedJob}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <button
              className="modal-close"
              type="button"
              aria-label="Закрыть"
              title="Закрыть"
              onClick={() => setRediarizeJobId("")}
            >
              ×
            </button>
            <div className="modal-symbol">◐</div>
            <h2>
              {rediarizeJob.options.diarization
                ? "Разметить спикеров заново"
                : "Разметить спикеров"}
            </h2>
            <p className="rerun-source">{rediarizeJob.original_name}</p>
            <div className="rerun-fields">
              <label>
                <span>Модель</span>
                <select
                  value={rediarizeEngine}
                  onChange={(event) =>
                    setRediarizeEngine(event.target.value as DiarizationEngine)
                  }
                >
                  {diarizationEngines.map((engine) => (
                    <option
                      key={engine}
                      value={engine}
                      disabled={!engineReady(diarizationStatus, engine)}
                    >
                      {diarizationEngineLabels[engine]}
                      {rediarizeJob.options.diarization &&
                      (rediarizeJob.options.diarization_engine ||
                        "pyannote") === engine
                        ? " · сейчас"
                        : ""}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="rerun-note">
              Whisper заново не запускается: текст останется прежним, поменяются
              только метки спикеров. Имена, присвоенные спикерам этого файла,
              сбросятся — у новой разметки своя нумерация.
            </div>
            <div className="modal-actions">
              <button
                className="primary-button"
                type="submit"
                disabled={
                  rediarizeStarting ||
                  !engineReady(diarizationStatus, rediarizeEngine)
                }
              >
                {rediarizeStarting ? "Ставлю в очередь…" : "Разметить"}
              </button>
            </div>
          </form>
        </div>
      )}

      {createOpen && (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={() => setCreateOpen(false)}
        >
          <form
            className="modal"
            onSubmit={createProject}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="modal-symbol">＋</div>
            <h2>
                      Новый проект <Hint text="Название можно не вводить — сервис создаст его по выбранному шаблону." />
                    </h2>
            <label>
              <span>Название</span>
              <input
                autoFocus
                value={newProjectTitle}
                onChange={(event) => setNewProjectTitle(event.target.value)}
                placeholder="Например, Лекции в Риме"
                maxLength={120}
              />
            </label>
            <label>
              <span>Папка</span>
              <select
                value={newProjectFolder}
                onChange={(event) => setNewProjectFolder(event.target.value)}
              >
                <option value="">Без папки</option>
                {visibleFolders.map((folder) => (
                  <option key={folder.id} value={folder.id}>
                    {folder.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="modal-preview">
              Будет создано:{" "}
              <strong>
                {projectNamePreview(
                  settings.project_name_format,
                  newProjectTitle,
                )}
              </strong>
            </div>
            <div className="modal-actions">
              <button className="primary-button" type="submit">
                Создать проект
              </button>
            </div>
          </form>
        </div>
      )}

      {shuttingDown && (
        <div className="shutdown-overlay" role="status" aria-live="assertive">
          <div>
            <span aria-hidden="true">✓</span>
            <h2>Сервис остановлен</h2>
            <p>
              Если вкладка не закрылась автоматически, её уже можно закрыть
              вручную.
            </p>
          </div>
        </div>
      )}

      {notice && <div className="toast">{notice}</div>}
    </div>
  );
}
