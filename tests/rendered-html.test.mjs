import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";


async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}


test("server-renders the local Whisper shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Локальный Whisper<\/title>/i);
  assert.match(html, /локальная студия/i);
  assert.match(html, /Перетащите или нажмите, чтобы добавить файлы/i);
  assert.match(html, /Новый проект/i);
  assert.match(html, /Настройки/i);
});


test("keeps transcript variants and editing actions in the project UI", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /\["none", "segments", "words"\]/);
  assert.match(page, /\["off", "on"\]/);
  assert.match(page, /speakers:\s*viewSpeakers/);
  assert.match(page, /Редактировать папку/);
  assert.match(page, /Удалить папку и проекты/);
  assert.match(page, /Редактировать проект/);
  assert.match(page, /Показать в папке/);
  assert.doesNotMatch(page, />\s*▶ Открыть файл\s*</);
  assert.doesNotMatch(page, /Копировать этот вариант/);
  assert.match(page, /Прослушать выбранную дорожку/);
  assert.match(page, /track-preview\?stream_index=/);
});


test("keeps speaker names and filtering aligned with every result action", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /className="speaker-select"/);
  assert.match(page, />\s*Все\s*</);
  assert.match(page, />\s*Никого\s*</);
  assert.match(page, /speaker_keys/);
  assert.match(page, /Присвоить имя спикеру/);
  assert.match(page, /variantQuery\(\)/);
  assert.match(page, /speakerNamesSignature/);
  assert.doesNotMatch(page, /className="speaker-toggle"/);
  assert.doesNotMatch(page, /Отмеченные голоса попадут/);
});


test("keeps project pages free of removed helper copy and per-project storage buttons", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /default_import_mode/);
  assert.doesNotMatch(page, /Добавляйте сколько угодно проектов/);
  assert.doesNotMatch(page, /Выбрать без копирования/);
  assert.doesNotMatch(page, /Порядок совпадает с порядком добавления файлов/);
  assert.doesNotMatch(page, /Эти переключатели управляют просмотром/);
  assert.doesNotMatch(page, /Подойдут любые контейнеры и кодеки/);
  assert.doesNotMatch(page, /Можно выбрать сразу несколько/);
});


test("keeps durable local dictation controls in the UI", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, />\s*Диктовка\s*</);
  assert.match(page, /new MediaRecorder/);
  assert.match(page, /pending-recordings/);
  assert.doesNotMatch(page, /После остановки сначала фиксируется аудиофайл/);
  assert.match(page, /cancelDictationRecording/);
  assert.match(page, /finishDictationRecordingToHistory/);
  assert.match(
    page,
    /uploaded && stopMode === "confirm"[\s\S]*setAutoConfirmDictationId\(messageId\)/,
  );
  // Автосохранение по зелёной кнопке отправляет в историю текст поля правки
  // вместе с ручными правками, а не серверную копию.
  assert.match(page, /await confirmDictationMessage\(message, text, true\)/);
  assert.match(page, /const text = current \? dictationText : message\.text;/);
  assert.match(page, /Запись отменена/);
  assert.match(page, /Подключаю микрофон…/);
  assert.match(page, /getByteTimeDomainData/);
  assert.match(page, /previous\.slice\(1\)/);
  assert.match(page, /Продолжить диктовку/);
  assert.match(page, /Сообщение сохранено в истории/);
  assert.match(
    page,
    /confirmDictationMessage\(currentDictation, dictationText, true\)/,
  );
  assert.match(page, /Исходное аудио/);
  // Иконка папки рядом с записью открывает проводник с выделенным файлом:
  // без неё исходное аудио диктовки было неоткуда достать.
  assert.match(page, /Показать запись в папке/);
  assert.match(page, /dictation-fragments\/\$\{fragmentId\}\/reveal/);
  assert.match(page, /dictation_default_language/);
  assert.match(page, /dictation_default_quality/);
  assert.match(page, /\/dictations\/history/);
  assert.match(page, /Очистить все/);
  assert.doesNotMatch(page, /Текущее сообщение/);
  assert.doesNotMatch(page, /Новый черновик/);
  assert.doesNotMatch(page, /Черновик сохранён/);
});


test("exposes a protected in-app service shutdown control", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /Остановить и закрыть/);
  assert.match(page, /\/system\/shutdown/);
  assert.match(page, /"X-Whisper-Local": "shutdown"/);
  assert.match(page, /className="shutdown-overlay"/);
  assert.match(page, /window\.close\(\)/);
});


test("keeps URL download, quality and durable controls in the project UI", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /Скачать видео по ссылке/);
  assert.match(page, /Скачать видео по ссылке в этот проект/);
  assert.match(page, /\/url-downloads\/probe/);
  assert.match(page, /Скачать и транскрибировать/);
  assert.match(page, /project_id: urlTargetProjectId \|\| null/);
  assert.match(page, /openUrlImporter\(activeProject\.id\)/);
  // Подпись «Авто» теперь строится из настройки download_quality, а не
  // зашитых 1080p.
  assert.match(page, /Авто — только звук/);
  assert.match(page, /Авто — до \$\{settings\.download_quality\}p/);
  assert.match(page, /download_quality/);
  assert.match(page, /Примерный размер:/);
  assert.match(page, /transcription_quality/);
  assert.match(page, /transcription_diarization/);
  assert.match(page, /diarization_device/);
  assert.match(page, /Установлена локально/);
  assert.match(page, /Видеокарта/);
  assert.match(page, /Спикеры не были размечены/);
  assert.match(page, /Разметить спикеров/);
  assert.match(page, /Распознать точнее/);
  assert.doesNotMatch(page, /Распознать заново/);
  assert.match(page, /\/jobs\/\$\{job\.id\}\/diarize/);
  assert.match(page, /\/jobs\/\$\{rerunJob\.id\}\/rerun/);
  assert.match(page, /Ⅱ Пауза/);
  assert.match(page, /▶ Продолжить/);
  assert.match(page, /■ Остановить/);
  // Список поддерживаемых сайтов переехал в подсказку рядом с полем ссылки.
  assert.match(page, /YouTube, VK, RuTube, Яндекс\.Диск и Google Drive/);
});


test("keeps saved settings authoritative after reconnect", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /settingsReconnectRefreshNeeded/);
  assert.match(page, /shouldApplySavedDefaults/);
  assert.match(page, /shouldApplySavedViewDefaults/);
  assert.match(page, /const savedSettings = \(await response\.json\(\)\) as Settings/);
  assert.match(page, /applySettingToCurrentControls\(key, savedSettings\[key\]\)/);
  assert.match(page, /setNotice\("Не удалось сохранить настройку"\)/);
});

test("exposes the real shared queue with cancellation and priority controls", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /fetch\(`\$\{API\}\/queue`/);
  assert.match(page, /Общая очередь/);
  assert.match(page, /className="queue-popover"/);
  assert.match(page, /\/prioritize/);
  assert.match(page, /Переместить в начало очереди/);
  assert.match(page, /Убрать из очереди/);
});


test("rejects recycled service PIDs and never serves a stale UI bundle", async () => {
  const [launcher, stopper, frontendServer] = await Promise.all([
    readFile(new URL("../scripts/start.ps1", import.meta.url), "utf8"),
    readFile(new URL("../scripts/stop.ps1", import.meta.url), "utf8"),
    readFile(new URL("../scripts/start_frontend.mjs", import.meta.url), "utf8"),
  ]);

  assert.match(launcher, /Get-ServiceProcess/);
  assert.match(launcher, /Get-VerifiedBackendPortProcess/);
  assert.match(launcher, /Test-LocalPortAvailable 8765/);
  assert.match(launcher, /Процесс запуска Whisper завершился/);
  assert.match(launcher, /backend\\\.app:app/);
  assert.match(launcher, /Get-FreeLocalPort/);
  assert.match(stopper, /Get-VerifiedBackendPortProcess/);
  assert.match(stopper, /orphanBackend/);
  assert.match(frontendServer, /no-store, no-cache, must-revalidate/);
});

test("exposes the models and dependencies inventory with self-install", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /Модели и зависимости/);
  assert.match(page, /refresh \? "\?refresh=true" : ""/);
  assert.match(page, /\/components\/models\/download/);
  // Раздел обязан говорить, что именно не работает без компонента, а не просто
  // «установлено / не установлено».
  assert.match(page, /item\.impact/);
  assert.match(page, /Без этого: \$\{item\.impact\.toLowerCase\(\)\}/);
  // Программы ставятся руками — команда должна копироваться.
  assert.match(page, /Скопировать команду/);
  assert.match(page, /copyPlainText\(item\.command/);
  // Прогресс загрузки: проценты, объём, скорость и остаток.
  assert.match(page, /modelDownload\.percent/);
  assert.match(page, /modelDownload\.speed_bytes/);
  assert.match(page, /modelDownload\.eta_seconds/);
  assert.match(page, /Проверить заново/);
  // Необязательные компоненты помечаются, чтобы их отсутствие не читалось как поломка.
  assert.match(page, /необязательно/);
});

test("keeps the dictation editor from fighting the typist", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  // Причина рваного набора была в двусторонней связи поля с сервером: опрос
  // каждые 1.8 с подставлял в textarea серверный текст и откатывал набранное.
  // Обратной подстановки быть не должно ни в каком виде.
  assert.doesNotMatch(page, /syncedDictationRef/);
  assert.doesNotMatch(page, /setDictationText\(server\)/);

  // Серверный текст читается один раз — когда открылось другое сообщение.
  assert.match(page, /loadedDictationRef\.current !== currentDictationId/);
  assert.match(page, /setDictationText\(currentDictationServerText \|\| ""\)/);

  // Со стороны сервиса в поле попадает только расшифровка нового фрагмента и
  // только в конец. Отметка идёт по id фрагмента, а не сравнением текстов.
  assert.match(page, /dictationMergedIds/);
  assert.match(page, /!dictationMergedIds\.has\(item\.id\)/);
  assert.match(page, /\$\{text\.trimEnd\(\)\}\\n\\n\$\{item\.text\}/);

  // Позиция курсора переживает дописывание расшифровки.
  assert.match(page, /field\.setSelectionRange\(caret\.start, caret\.end\)/);

  // Поле не блокируется на время распознавания.
  const editor = page.slice(page.indexOf('className="dictation-textarea"'));
  const textareaEnd = editor.indexOf("/>");
  assert.doesNotMatch(
    editor.slice(0, textareaEnd),
    /disabled=\{currentDictationBusy\}/,
  );
});

test("keeps dictation-to-project and sidebar order", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /\/to-project/);
  assert.match(page, /Сохранить в проект/);
  assert.match(page, /сохранено в проект/);
  assert.match(page, /text_edited/);
  // Статус сервиса стоит над «Настройками».
  const bottom = page.slice(page.indexOf('className="sidebar-bottom"'));
  const status = bottom.indexOf('className="server-status"');
  const settings = bottom.indexOf("Настройки");
  assert.ok(status > -1 && settings > status, "статус должен быть над настройками");
});

test("keeps bulk project selection safe and scoped", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /\/projects\/bulk\/folder/);
  assert.match(page, /\/projects\/bulk\/delete/);
  assert.match(page, /selectMode/);
  assert.match(page, /Выделить все/);
  assert.match(page, /Снять все/);
  // «В папку» и «Удалить» появляются только после выделения — до этого они
  // бесполезны. Управление живёт в одной строке с переключателем вида.
  assert.match(page, /selectedVisibleIds\.length > 0 && \(/);
  assert.match(page, /Удалить \$\{selectedVisibleIds\.length\}/);
  assert.doesNotMatch(page, /className="bulk-bar"/);
  // «Выделить все» обязано брать только видимые: иначе из одной папки можно
  // незаметно удалить проекты всех остальных.
  assert.match(
    page,
    /const visibleProjectIdList = filteredProjects\.map\(\(project\) => project\.id\)/,
  );

  // Смена папки сбрасывает отметки. Иначе, отметив проекты в одной папке и
  // перейдя в другую, можно нажать «Удалить N» и стереть то, чего не видно.
  assert.match(
    page,
    /function showFolder\(filter: string\) \{\s*\n\s*setFolderFilter\(filter\);\s*\n\s*setSelectedProjects\(new Set\(\)\);/,
  );
  // Единственный вход: setFolderFilter вызывается только внутри showFolder,
  // иначе новый способ сменить папку снова унесёт чужие отметки.
  assert.equal(page.match(/setFolderFilter\(/g).length, 1);
  assert.match(page, /showFolder\("all"\)/);
  assert.match(page, /showFolder\("none"\)/);
  assert.match(page, /showFolder\(folder\.id\)/);

  // Массовые действия отправляют только видимые отметки, а не весь набор.
  assert.match(page, /ids: selectedVisibleIds,\s*\n\s*folder_id: folderId,/);
  assert.match(page, /body: JSON\.stringify\(\{ ids: selectedVisibleIds \}\)/);
  assert.doesNotMatch(page, /ids: \[\.\.\.selectedProjects\]/);

  // «Снять все» показывается по составу, а не по совпадению количеств:
  // пять отметок при пяти видимых ещё не значит, что отмечены именно эти.
  assert.match(
    page,
    /const allVisibleSelected =\s*\n\s*visibleProjectIdList\.length > 0 &&\s*\n\s*selectedVisibleIds\.length === visibleProjectIdList\.length;/,
  );
  assert.match(page, /\{allVisibleSelected \? "Снять все" : "Выделить все"\}/);
  assert.doesNotMatch(page, /selectedProjects\.size >= visible/);

  // Исчезнувший проект не должен продолжать считаться отмеченным.
  assert.match(page, /const alive = new Set\(nextProjects\.map\(\(project\) => project\.id\)\)/);
  // Удаление необратимо — обязателен явный вопрос.
  assert.match(page, /window\.confirm\(\s*`Удалить \$\{count\}/);
  // В режиме выделения клик по карточке отмечает, а не открывает проект.
  assert.match(page, /if \(selectMode\) \{\s*toggleProjectSelection\(project\.id\);/);
});

test("shows which external service a project came from", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /\/api\/integrations|\$\{API\}\/integrations/);
  assert.match(page, /Подключённые сервисы/);
  // Метка источника обязана быть в карточке: в плоской ленте папок не видно.
  assert.match(page, /project-origin/);
  assert.match(page, /project\.integration_title/);
  assert.match(page, /X-Whisper-Integration/);
});

test("protects integration folders and can hide them from the feed", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  /* Сам отбор ушёл на сервер: лента запрашивает страницу уже без скрытого,
     а не отфильтровывает загруженное. Проверяется он в
     tests/test_pagination_and_folder_feed.py. Здесь остаётся то, что видно
     в интерфейсе: переключатель и счётчики, которые не зависят от
     открытой страницы. */
  assert.match(page, /const filteredProjects = projects;/);
  assert.match(page, /projects\/counts/);
  assert.match(page, /integration_feed/);
  assert.match(page, /Проекты сервисов в общей ленте/);
  // Та же настройка у отдельной папки — галочкой в её редакторе.
  assert.match(page, /Показывать в общей ленте/);
  assert.match(page, /feed_visible: folderFeedDraft/);
  // У папки внешнего сервиса кнопки удаления быть не должно — в обоих списках.
  const guards = page.match(/\{!folder\.integration && \(/g) || [];
  assert.equal(guards.length, 2, "удаление должно быть закрыто в двух местах");
});

test("loads the feed and the dictation history page by page", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  // Главное требование: страница именно запрашивается, а не вырезается из
  // загруженного целиком списка. Иначе на полутора тысячах проектов каждое
  // обновление ленты тянуло бы всю библиотеку.
  assert.match(page, /projectQuery\.set\("per_page"/);
  assert.match(page, /dictationQuery\.set\("per_page"/);
  assert.match(page, /X-Total-Count/);
  assert.doesNotMatch(page, /projects\.slice\(/);

  // Размеры страницы — те же в ленте и в настройках.
  assert.match(
    page,
    /const pageSizes = \["10", "20", "50", "100", "500", "1000", "all"\]/,
  );
  assert.match(page, /projects_page_size/);
  assert.match(page, /dictation_page_size/);

  // Открытый проект догружается отдельно: на своей странице ленты его может
  // не оказаться, а экран проекта от этого пустеть не должен.
  assert.match(page, /setActiveProjectDetail/);

  // Счётчики в навигации берутся с сервера, а не считаются по странице.
  assert.match(page, /projectCounts\.feed/);
  assert.match(page, /projectCounts\.loose/);
});

test("lets a download skip transcription by default", async () => {
  const page = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  // Галочка на одну загрузку и постоянное значение в настройках: когда
  // видеокарта занята, снимать галочку у каждой ссылки незачем.
  assert.match(page, /Только скачать, без расшифровки/);
  assert.match(page, /Расшифровывать скачанное сразу/);
  assert.match(
    page,
    /setUrlTranscribe\(settings\.download_transcribe === "on"\)/,
  );
  assert.match(page, /transcribe: urlTranscribe/);
});
