import {
  applyAIDraft,
  ApiError,
  createCharacter,
  createChapter,
  deleteAllCharacters,
  deleteCharacter,
  deleteChapter,
  getBook,
  getChapter,
  listAIConfigs,
  listBooks,
  listCharacters,
  listChapters,
  listSnapshots,
  previewAI,
  refineAIDraft,
  streamAIDraft,
  updateCharacter,
  updateChapter,
  updateChapterKeepalive,
} from "./api.js";
import {
  aiModuleLabel,
  aiProviderLabel,
  aiScopeLabel,
  chapterNodeLabel,
  chapterStatusLabel,
  escapeHtml,
  formatCount,
  formatDateTime,
  setAssistantContext,
  showToast,
  statusTone,
  truncate,
} from "./shell.js";
import { isWriterGenerationEffectEnabled } from "./ui-preferences.js";

const writerPreviewState = {
  systemPromptDraft: "",
  hasSavedSystemPrompt: false,
};

const writerDraftState = {
  isOpen: false,
  bookId: null,
  chapterId: null,
  response: null,
  basePayload: null,
};

const writerConfirmState = {
  isOpen: false,
  bookId: null,
  chapterId: null,
  payload: null,
  previewResponse: null,
};

const WRITER_CHARACTER_STATUS_LABELS = {
  alive: "活着",
  dead: "死亡",
  serious_injury: "重伤",
  minor_injury: "轻伤",
  disabled: "残疾",
};

const WRITER_CHARACTER_PRESET_STATUSES = [
  "活着",
  "死亡",
  "重伤",
  "轻伤",
  "残疾",
  "失踪",
  "失忆",
];

const writerCharacterState = {
  items: [],
  chapters: [],
  editingId: null,
  lifeStatuses: [],
  timelineDraft: [],
};

const WRITER_GENERATION_FX_LEAF_COUNT = 56;
const WRITER_GENERATION_FX_TEXT_REVEAL_MS = 1320;
const WRITER_GENERATION_FX_MIN_DURATION_MS = 2100;
const WRITER_GENERATION_FX_SUCCESS_HOLD_MS = 860;
const WRITER_GENERATION_FX_ERROR_HOLD_MS = 620;
const WRITER_GENERATION_FX_FADE_MS = 360;
const WRITER_GENERATION_FX_STREAM_TICK_MS = 42;
const WRITER_GENERATION_FX_STREAM_SETTLE_MS = 120;
const WRITER_GENERATION_FX_STREAM_VISIBLE_LIMIT = 1800;
const WRITER_GENERATION_FX_STREAM_CHARS_PER_TICK = 2;

const writerGenerationFxState = {
  initialized: false,
  startAt: 0,
  revealTimer: null,
  streamTimer: null,
  scrollFrame: null,
  actionEnabled: false,
  streamActivated: false,
  sourceText: "",
  pendingText: "",
  displayedText: "",
  renderedWallText: "",
};

function waitFor(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function writerGenerationFxElements() {
  return {
    overlay: document.getElementById("writer-generation-fx"),
    leaves: document.getElementById("writer-generation-fx-leaves"),
    stream: document.getElementById("writer-generation-fx-stream"),
    streamViewport: document.getElementById("writer-generation-fx-stream-viewport"),
    streamWall: document.getElementById("writer-generation-fx-stream-wall"),
    copy: document.getElementById("writer-generation-fx-copy"),
    text: document.getElementById("writer-generation-fx-text"),
    subtext: document.getElementById("writer-generation-fx-subtext"),
  };
}

function seededUnit(index, seed) {
  const value = Math.sin(index * 12.9898 + seed * 78.233) * 43758.5453123;
  return value - Math.floor(value);
}

function seededRange(index, seed, min, max) {
  return min + seededUnit(index, seed) * (max - min);
}

function clearWriterGenerationFxTimers() {
  if (writerGenerationFxState.revealTimer) {
    window.clearTimeout(writerGenerationFxState.revealTimer);
    writerGenerationFxState.revealTimer = null;
  }
  if (writerGenerationFxState.streamTimer) {
    window.clearInterval(writerGenerationFxState.streamTimer);
    writerGenerationFxState.streamTimer = null;
  }
  if (writerGenerationFxState.scrollFrame) {
    window.cancelAnimationFrame(writerGenerationFxState.scrollFrame);
    writerGenerationFxState.scrollFrame = null;
  }
}

function pulseWriterGenerationFxCopy() {
  const { copy } = writerGenerationFxElements();
  if (!copy) return;
  copy.classList.remove("is-flashing");
  void copy.offsetWidth;
  copy.classList.add("is-flashing");
}

function renderWriterGenerationFxCopyText(text) {
  const safeText = escapeHtml(text);
  if (!writerGenerationFxState.actionEnabled || !safeText.includes("谱写")) {
    return safeText;
  }
  return safeText.replace(
    "谱写",
    '<button class="bamboo-writing-fx-copy-action" id="writer-generation-fx-copy-action" type="button">谱写</button>'
  );
}

function setWriterGenerationFxCopy(text, subtext) {
  const { text: textElement, subtext: subtextElement } = writerGenerationFxElements();
  if (textElement) {
    textElement.innerHTML = renderWriterGenerationFxCopyText(text);
  }
  if (subtextElement) {
    subtextElement.textContent = subtext;
  }
}

function normalizeWriterGenerationFxStreamText(text) {
  return String(text || "")
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

function renderWriterGenerationFxStreamWallText(sourceText) {
  const value = String(sourceText || "");
  if (!value.trim()) {
    return "";
  }
  return value.slice(Math.max(0, value.length - WRITER_GENERATION_FX_STREAM_VISIBLE_LIMIT));
}

function findWriterGenerationFxOverlap(previousText, nextText) {
  const maxLength = Math.min(previousText.length, nextText.length);
  for (let length = maxLength; length >= 0; length -= 1) {
    if (previousText.slice(previousText.length - length) === nextText.slice(0, length)) {
      return length;
    }
  }
  return 0;
}

function scheduleWriterGenerationFxWallFollow() {
  const { streamViewport } = writerGenerationFxElements();
  if (!streamViewport || writerGenerationFxState.scrollFrame) {
    return;
  }
  writerGenerationFxState.scrollFrame = window.requestAnimationFrame(() => {
    writerGenerationFxState.scrollFrame = null;
    streamViewport.scrollTop = streamViewport.scrollHeight;
  });
}

function createWriterGenerationFxCharNode(char, index, animate = true) {
  if (char === "\n") {
    return document.createTextNode("\n");
  }

  const span = document.createElement("span");
  span.className = "bamboo-writing-fx-char";
  if (!animate || /\s/.test(char)) {
    span.classList.add("is-static");
  } else {
    span.classList.add("is-new");
  }
  span.textContent = char === " " ? "\u00A0" : char;
  span.style.setProperty("--char-leaf-x-a", `${seededRange(index, 31, -1.15, -0.22).toFixed(3)}em`);
  span.style.setProperty("--char-leaf-y-a", `${seededRange(index, 32, -0.92, 0.38).toFixed(3)}em`);
  span.style.setProperty("--char-leaf-x-b", `${seededRange(index, 33, 0.22, 1.18).toFixed(3)}em`);
  span.style.setProperty("--char-leaf-y-b", `${seededRange(index, 34, -0.24, 0.92).toFixed(3)}em`);
  span.style.setProperty("--char-leaf-rotate-a", `${seededRange(index, 35, -52, -14).toFixed(2)}deg`);
  span.style.setProperty("--char-leaf-rotate-b", `${seededRange(index, 36, 18, 58).toFixed(2)}deg`);
  span.style.setProperty("--char-leaf-scale-a", seededRange(index, 37, 0.66, 1.12).toFixed(3));
  span.style.setProperty("--char-leaf-scale-b", seededRange(index, 38, 0.62, 1.08).toFixed(3));
  span.style.setProperty("--char-reveal-delay", `${seededRange(index, 39, 0, 120).toFixed(0)}ms`);
  return span;
}

function rebuildWriterGenerationFxStreamWall(text, overlapLength = 0) {
  const { streamWall } = writerGenerationFxElements();
  if (!streamWall) return;
  streamWall.textContent = "";
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < text.length; index += 1) {
    fragment.appendChild(createWriterGenerationFxCharNode(text[index], index, index >= overlapLength));
  }
  streamWall.appendChild(fragment);
  writerGenerationFxState.renderedWallText = text;
  scheduleWriterGenerationFxWallFollow();
}

function appendWriterGenerationFxStreamWall(text, startIndex) {
  const { streamWall } = writerGenerationFxElements();
  if (!streamWall || startIndex >= text.length) return;
  const fragment = document.createDocumentFragment();
  for (let index = startIndex; index < text.length; index += 1) {
    fragment.appendChild(createWriterGenerationFxCharNode(text[index], index, true));
  }
  streamWall.appendChild(fragment);
  writerGenerationFxState.renderedWallText = text;
  scheduleWriterGenerationFxWallFollow();
}

function updateWriterGenerationFxStreamWall(text) {
  const wallText = renderWriterGenerationFxStreamWallText(text);
  if (!wallText) {
    rebuildWriterGenerationFxStreamWall("");
    return;
  }
  if (!writerGenerationFxState.renderedWallText) {
    rebuildWriterGenerationFxStreamWall(wallText);
    return;
  }
  if (wallText.startsWith(writerGenerationFxState.renderedWallText)) {
    appendWriterGenerationFxStreamWall(wallText, writerGenerationFxState.renderedWallText.length);
    return;
  }
  const overlapLength = findWriterGenerationFxOverlap(writerGenerationFxState.renderedWallText, wallText);
  rebuildWriterGenerationFxStreamWall(wallText, overlapLength);
}

function updateWriterGenerationFxStreamSource(text) {
  const normalizedText = normalizeWriterGenerationFxStreamText(text);
  if (!normalizedText) {
    return;
  }
  const previousSnapshot = writerGenerationFxState.sourceText;
  const overlapLength = normalizedText.startsWith(previousSnapshot)
    ? previousSnapshot.length
    : findWriterGenerationFxOverlap(previousSnapshot, normalizedText);
  const deltaText = normalizedText.slice(Math.max(0, overlapLength));
  writerGenerationFxState.sourceText = normalizedText;
  if (deltaText) {
    writerGenerationFxState.pendingText += deltaText;
  }
  if (!writerGenerationFxState.streamActivated) {
    return;
  }
  const { overlay } = writerGenerationFxElements();
  if (overlay && overlay.classList.contains("flex") && writerGenerationFxState.pendingText) {
    overlay.dataset.stream = "actual";
  }
  ensureWriterGenerationFxStreamRunning();
  if (!writerGenerationFxState.displayedText && writerGenerationFxState.pendingText) {
    tickWriterGenerationFxStream();
  }
}

function updateWriterGenerationFxProgress(message) {
  const progressText = String(message || "").trim();
  if (!progressText) return;
  const { overlay } = writerGenerationFxElements();
  if (!overlay || !overlay.classList.contains("flex")) return;
  setWriterGenerationFxCopy("“竹林”正在谱写", progressText);
}

function tickWriterGenerationFxStream() {
  if (!writerGenerationFxState.pendingText) return;
  const chunkSize = Math.min(
    writerGenerationFxState.pendingText.length,
    WRITER_GENERATION_FX_STREAM_CHARS_PER_TICK
  );
  writerGenerationFxState.displayedText += writerGenerationFxState.pendingText.slice(0, chunkSize);
  writerGenerationFxState.pendingText = writerGenerationFxState.pendingText.slice(chunkSize);
  updateWriterGenerationFxStreamWall(writerGenerationFxState.displayedText);
}

function ensureWriterGenerationFxStreamRunning() {
  if (writerGenerationFxState.streamTimer) return;
  writerGenerationFxState.streamTimer = window.setInterval(
    tickWriterGenerationFxStream,
    WRITER_GENERATION_FX_STREAM_TICK_MS
  );
}

function activateWriterGenerationFxStream() {
  const { overlay } = writerGenerationFxElements();
  if (!overlay || !overlay.classList.contains("flex")) return;
  if (writerGenerationFxState.streamActivated) return;

  writerGenerationFxState.streamActivated = true;
  writerGenerationFxState.displayedText = "";
  writerGenerationFxState.pendingText = "";
  updateWriterGenerationFxStreamWall(writerGenerationFxState.displayedText);
  overlay.dataset.stream = writerGenerationFxState.sourceText ? "actual" : "active";
  setWriterGenerationFxCopy("“竹林”正在谱写", writerGenerationFxState.sourceText ? "字流已起，青叶化墨" : "等待 AI 首字落笔…");
  pulseWriterGenerationFxCopy();
  if (writerGenerationFxState.sourceText) {
    writerGenerationFxState.pendingText = writerGenerationFxState.sourceText;
    ensureWriterGenerationFxStreamRunning();
    tickWriterGenerationFxStream();
  }
}

async function settleWriterGenerationFxStream(text) {
  const actualText = normalizeWriterGenerationFxStreamText(text);
  if (actualText) {
    const { overlay } = writerGenerationFxElements();
    if (overlay && overlay.classList.contains("flex")) {
      overlay.dataset.stream = writerGenerationFxState.streamActivated ? "actual" : overlay.dataset.stream || "idle";
    }
    updateWriterGenerationFxStreamSource(actualText);
    await waitFor(WRITER_GENERATION_FX_STREAM_SETTLE_MS);
    return;
  }
  await waitFor(WRITER_GENERATION_FX_STREAM_SETTLE_MS);
}

function ensureWriterGenerationFx() {
  const { overlay, leaves, text } = writerGenerationFxElements();
  if (!overlay || !leaves || writerGenerationFxState.initialized) return;

  const fragment = document.createDocumentFragment();
  for (let index = 0; index < WRITER_GENERATION_FX_LEAF_COUNT; index += 1) {
    const leaf = document.createElement("span");
    leaf.className = "bamboo-writing-fx-leaf";
    leaf.style.setProperty("--leaf-top", `${seededRange(index, 1, 4, 94).toFixed(2)}%`);
    leaf.style.setProperty("--leaf-size", `${seededRange(index, 2, 1.6, 4.8).toFixed(2)}rem`);
    leaf.style.setProperty("--leaf-scale", seededRange(index, 3, 0.72, 1.28).toFixed(3));
    leaf.style.setProperty("--leaf-rotate", `${seededRange(index, 4, -46, 34).toFixed(2)}deg`);
    leaf.style.setProperty("--leaf-enter-y", `${seededRange(index, 5, -48, 48).toFixed(2)}px`);
    leaf.style.setProperty("--leaf-sweep-y", `${seededRange(index, 6, -56, 72).toFixed(2)}px`);
    leaf.style.setProperty("--leaf-drift-y", `${seededRange(index, 7, -36, 56).toFixed(2)}px`);
    leaf.style.setProperty("--leaf-outro-y", `${seededRange(index, 8, -24, 78).toFixed(2)}px`);
    leaf.style.setProperty("--leaf-spin", `${seededRange(index, 9, -52, 38).toFixed(2)}deg`);
    leaf.style.setProperty("--leaf-outro-spin", `${seededRange(index, 10, -66, 52).toFixed(2)}deg`);
    leaf.style.setProperty("--leaf-delay", `${seededRange(index, 11, 0, 0.68).toFixed(3)}s`);
    leaf.style.setProperty("--leaf-running-delay", `${seededRange(index, 12, -6.2, 0).toFixed(3)}s`);
    leaf.style.setProperty("--leaf-outro-delay", `${seededRange(index, 13, 0, 0.22).toFixed(3)}s`);
    leaf.style.setProperty("--leaf-intro-duration", `${seededRange(index, 14, 1.2, 1.95).toFixed(3)}s`);
    leaf.style.setProperty("--leaf-drift-duration", `${seededRange(index, 15, 4.8, 7.2).toFixed(3)}s`);
    leaf.style.setProperty("--leaf-outro-duration", `${seededRange(index, 16, 0.92, 1.28).toFixed(3)}s`);
    leaf.style.setProperty("--leaf-blur", `${seededRange(index, 17, 0, 1.35).toFixed(2)}px`);
    fragment.appendChild(leaf);
  }

  leaves.innerHTML = "";
  leaves.appendChild(fragment);
  overlay.dataset.phase = "idle";
  overlay.dataset.stream = "idle";
  text?.addEventListener("click", (event) => {
    if (event.target instanceof HTMLElement && event.target.closest("#writer-generation-fx-copy-action")) {
      activateWriterGenerationFxStream();
    }
  });
  text?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if (event.target instanceof HTMLElement && event.target.closest("#writer-generation-fx-copy-action")) {
      event.preventDefault();
      activateWriterGenerationFxStream();
    }
  });
  writerGenerationFxState.initialized = true;
}

function showWriterGenerationFx() {
  const { overlay } = writerGenerationFxElements();
  if (!overlay) return;
  overlay.classList.remove("hidden");
  overlay.classList.add("flex");
  overlay.setAttribute("aria-hidden", "false");
  overlay.style.transition = `opacity ${WRITER_GENERATION_FX_FADE_MS}ms cubic-bezier(0.2, 0, 0, 1)`;
  overlay.style.opacity = "0";
  document.body.classList.add("bamboo-writing-fx-lock");
  window.requestAnimationFrame(() => {
    overlay.style.opacity = "1";
  });
}

async function hideWriterGenerationFx() {
  const { overlay, copy } = writerGenerationFxElements();
  if (!overlay) return;
  overlay.style.opacity = "0";
  await waitFor(WRITER_GENERATION_FX_FADE_MS);
  overlay.classList.add("hidden");
  overlay.classList.remove("flex");
  overlay.setAttribute("aria-hidden", "true");
  overlay.dataset.phase = "idle";
  overlay.dataset.stream = "idle";
  overlay.style.opacity = "";
  overlay.style.transition = "";
  if (copy) {
    copy.classList.remove("is-flashing");
  }
  writerGenerationFxState.actionEnabled = false;
  writerGenerationFxState.streamActivated = false;
  writerGenerationFxState.sourceText = "";
  writerGenerationFxState.pendingText = "";
  writerGenerationFxState.displayedText = "";
  writerGenerationFxState.renderedWallText = "";
  updateWriterGenerationFxStreamWall("");
  document.body.classList.remove("bamboo-writing-fx-lock");
}

function startWriterGenerationFx() {
  if (!isWriterGenerationEffectEnabled()) return;

  ensureWriterGenerationFx();
  clearWriterGenerationFxTimers();
  writerGenerationFxState.startAt = performance.now();
  writerGenerationFxState.actionEnabled = false;
  writerGenerationFxState.streamActivated = false;
  writerGenerationFxState.sourceText = "";
  writerGenerationFxState.pendingText = "";
  writerGenerationFxState.displayedText = "";
  setWriterGenerationFxCopy("“竹林”正在谱写", "风入青篁，文意渐起");
  updateWriterGenerationFxStreamWall("");
  showWriterGenerationFx();

  const { overlay, copy } = writerGenerationFxElements();
  if (!overlay) return;
  overlay.dataset.phase = "intro";
  overlay.dataset.stream = "idle";
  if (copy) {
    copy.classList.remove("is-flashing");
  }

  writerGenerationFxState.revealTimer = window.setTimeout(() => {
    overlay.dataset.phase = "running";
    writerGenerationFxState.actionEnabled = true;
    setWriterGenerationFxCopy("“竹林”正在谱写", "点“谱写”看字流铺开");
    pulseWriterGenerationFxCopy();
  }, WRITER_GENERATION_FX_TEXT_REVEAL_MS);
}

async function finishWriterGenerationFx(success) {
  const { overlay } = writerGenerationFxElements();
  if (!overlay || !overlay.classList.contains("flex")) return;

  const elapsed = performance.now() - writerGenerationFxState.startAt;
  if (elapsed < WRITER_GENERATION_FX_TEXT_REVEAL_MS) {
    await waitFor(WRITER_GENERATION_FX_TEXT_REVEAL_MS - elapsed);
  }

  if (overlay.dataset.phase === "intro") {
    overlay.dataset.phase = "running";
    pulseWriterGenerationFxCopy();
  }

  const currentElapsed = performance.now() - writerGenerationFxState.startAt;
  if (currentElapsed < WRITER_GENERATION_FX_MIN_DURATION_MS) {
    await waitFor(WRITER_GENERATION_FX_MIN_DURATION_MS - currentElapsed);
  }

  clearWriterGenerationFxTimers();
  overlay.dataset.phase = success ? "complete" : "error";
  writerGenerationFxState.actionEnabled = false;
  setWriterGenerationFxCopy(
    success ? "“竹林”谱写完成" : "“竹林”暂缓落笔",
    success ? "清风过叶，落笔成章" : "青叶未歇，稍后再试"
  );
  pulseWriterGenerationFxCopy();

  await waitFor(success ? WRITER_GENERATION_FX_SUCCESS_HOLD_MS : WRITER_GENERATION_FX_ERROR_HOLD_MS);
  await hideWriterGenerationFx();
}

function selectedBookId(books) {
  const params = new URLSearchParams(window.location.search);
  const bookId = Number(params.get("book"));
  if (bookId && books.some((book) => book.id === bookId)) return bookId;
  return books[0]?.id || null;
}

function selectedChapterId(chapters) {
  const params = new URLSearchParams(window.location.search);
  const chapterId = Number(params.get("chapter"));
  if (chapterId && chapters.some((chapter) => chapter.id === chapterId)) return chapterId;
  return chapters.find((chapter) => chapter.node_type === "chapter" || chapter.node_type === "scene")?.id || chapters[0]?.id || null;
}

function renderChapterTree(chapters, activeChapterId, bookId) {
  return chapters.length
    ? chapters
        .map(
          (chapter) => `
            <a class="block rounded-2xl px-4 py-3 text-left transition cursor-pointer ${
              chapter.id === activeChapterId ? "bg-bamboo-highlight text-bamboo-deep" : "bg-white/80 text-slate-600"
            }" data-navigate-chapter="/writer?book=${bookId}&chapter=${chapter.id}" style="margin-left:${chapter.depth * 12}px">
              <div class="font-medium">${escapeHtml(chapter.title)}</div>
              <div class="mt-1 text-xs uppercase tracking-[0.16em] text-slate-400">
                ${escapeHtml(chapterNodeLabel(chapter.node_type))} · ${escapeHtml(chapterStatusLabel(chapter.status))}
              </div>
            </a>
          `
        )
        .join("")
    : '<div class="rounded-2xl bg-white/80 px-4 py-4 text-sm text-slate-500">还没有章节。</div>';
}

function renderSnapshotList(bookId, snapshots) {
  return snapshots.length
    ? snapshots
        .map(
          (item) => `
            <a class="block rounded-[1.1rem] bg-white/82 p-4 shadow-cloud" href="/history?book=${bookId}&snapshot=${item.id}">
              <div class="flex items-center justify-between gap-3">
                <div class="font-medium text-bamboo-deep">${escapeHtml(item.label || item.kind)}</div>
                <span class="bamboo-chip">v${escapeHtml(item.chapter_version)}</span>
              </div>
              <p class="mt-3 text-sm leading-7 text-slate-500">${escapeHtml(truncate(item.diff_summary || item.summary, 88))}</p>
              <div class="mt-3 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(formatDateTime(item.created_at))}</div>
            </a>
          `
        )
        .join("")
    : '<div class="rounded-[1.1rem] bg-white/82 p-4 text-sm text-slate-500 shadow-cloud">当前章节暂无快照。</div>';
}

function renderAIConfigs(configs) {
  return configs.length
    ? configs
        .map(
          (config) => `
            <div class="rounded-3xl bg-white/80 p-4 shadow-cloud">
              <div class="flex items-center justify-between gap-3">
                <div class="font-medium text-bamboo-deep">${escapeHtml(aiModuleLabel(config.module))}</div>
                <span class="bamboo-chip">${escapeHtml(aiScopeLabel(config.scope))}</span>
              </div>
              <div class="mt-3 text-sm leading-7 text-slate-500">${escapeHtml(config.model_name || "未设置模型")}</div>
              <div class="text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(aiProviderLabel(config))}</div>
            </div>
          `
        )
        .join("")
    : '<div class="rounded-3xl bg-white/80 p-4 text-sm text-slate-500 shadow-cloud">当前没有可用的 AI 功能配置，请先去配置页设置接口和模型。</div>';
}

function renderMemory(book, chapter) {
  return [
    { title: "系统文风", text: book.global_style_prompt || "暂无固定文风要求" },
    { title: "长期摘要", text: book.long_term_summary || "暂无长期摘要" },
    { title: "当前章节", text: `版本 v${chapter.version} · ${formatCount(chapter.word_count)} 字` },
  ]
    .map(
      (item) => `
        <article class="bamboo-panel p-5">
          <div class="bamboo-mini-label">${escapeHtml(item.title)}</div>
          <p class="mt-3 text-sm leading-7 text-slate-500">${escapeHtml(item.text)}</p>
        </article>
      `
    )
    .join("");
}

function writerCharacterStatusLabel(value) {
  const text = String(value || "").trim();
  return WRITER_CHARACTER_STATUS_LABELS[text] || WRITER_CHARACTER_STATUS_LABELS[text.toLowerCase()] || text;
}

function writerCharacterStatuses(character) {
  const source = Array.isArray(character?.life_statuses)
    ? character.life_statuses
    : Array.isArray(character?.card_json?.life_statuses)
      ? character.card_json.life_statuses
      : [];
  const values = [];
  source.forEach((item) => {
    const label = writerCharacterStatusLabel(item);
    if (label && !values.includes(label)) {
      values.push(label);
    }
  });
  return values;
}

function writerCharacterTimelineEntries(character) {
  const source = Array.isArray(character?.timeline_entries)
    ? character.timeline_entries
    : Array.isArray(character?.card_json?.timeline_entries)
      ? character.card_json.timeline_entries
      : [];
  return source
    .filter((item) => item && typeof item === "object")
    .map((item) => ({
      chapter_id: Number(item.chapter_id || 0) || null,
      chapter_number: Number(item.chapter_number || 0) || null,
      chapter_label: String(item.chapter_label || item.chapter_title || "").trim(),
      event: String(item.event || item.notes || "").trim(),
      location: String(item.location || "").trim(),
      status: String(item.status || "").trim(),
    }))
    .filter((item) => item.chapter_id || item.chapter_number || item.event || item.location || item.status)
    .sort((left, right) => (left.chapter_number || 0) - (right.chapter_number || 0));
}

function parseWriterCharacterAliases(value) {
  return String(value || "")
    .split(/[\n,，、/|]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .filter((item, index, array) => array.indexOf(item) === index);
}

function writerCharacterCardValue(character, key) {
  const directValue = character?.[key];
  if (directValue !== undefined && directValue !== null && String(directValue).trim()) return String(directValue).trim();
  const cardValue = character?.card_json?.[key];
  if (cardValue !== undefined && cardValue !== null && String(cardValue).trim()) return String(cardValue).trim();
  return "";
}

function renderWriterCharacterRows(characters) {
  if (!characters.length) {
    return '<div class="rounded-[1.2rem] bg-white/82 p-5 text-sm text-slate-500 shadow-cloud">当前书籍还没有人物卡。</div>';
  }

  return characters
    .map(
      (character) => `
        <article class="rounded-[1.35rem] bg-white/88 p-5 shadow-cloud">
          <div class="text-lg font-semibold text-bamboo-deep">${escapeHtml(character.name || "未命名人物")}</div>
          <div class="mt-4">
            <button class="bamboo-btn-secondary" data-writer-edit-character="${character.id}" type="button">编辑</button>
          </div>
        </article>
      `
    )
    .join("");
}

function writerCharacterChapterOptions(selectedId = null) {
  const options = ['<option value="">选择章节</option>'];
  writerCharacterState.chapters.forEach((chapter) => {
    const selected = Number(selectedId || 0) === Number(chapter.id) ? "selected" : "";
    options.push(`<option value="${chapter.id}" ${selected}>${escapeHtml(chapter.title)}</option>`);
  });
  return options.join("");
}

function renderWriterCharacterStatusesEditor() {
  const container = document.getElementById("writer-character-statuses");
  if (!container) return;
  const values = writerCharacterState.lifeStatuses || [];
  const presetButtons = WRITER_CHARACTER_PRESET_STATUSES.filter((item) => !values.includes(item))
    .map(
      (status) =>
        `<button class="bamboo-btn-ghost px-3 py-2 text-sm" data-writer-character-preset-status="${escapeHtml(status)}" type="button">${escapeHtml(status)}</button>`
    )
    .join("");
  const selectedChips = values.length
    ? values
        .map(
          (status) => `
            <span class="inline-flex items-center gap-2 rounded-full bg-bamboo-highlight px-3 py-2 text-sm text-bamboo-deep">
              <span>${escapeHtml(status)}</span>
              <button class="text-slate-500 transition hover:text-rose-600" data-writer-character-remove-status="${escapeHtml(status)}" type="button">删除</button>
            </span>
          `
        )
        .join("")
    : '<div class="text-sm text-slate-500">还没有生命状态。</div>';
  container.innerHTML = `
    <div class="flex flex-wrap gap-2">${selectedChips}</div>
    ${presetButtons ? `<div class="mt-3 flex flex-wrap gap-2">${presetButtons}</div>` : ""}
  `;
}

function renderWriterCharacterTimelineEditor() {
  const container = document.getElementById("writer-character-timeline-list");
  if (!container) return;
  const entries = writerCharacterState.timelineDraft || [];
  if (!entries.length) {
    container.innerHTML =
      '<div class="rounded-[1rem] bg-bamboo-mist/55 p-4 text-sm text-slate-500">还没有章节时间节点，点上方按钮即可新增。</div>';
    return;
  }
  container.innerHTML = entries
    .map(
      (entry, index) => `
        <div class="rounded-[1rem] bg-white/84 p-4 shadow-cloud" data-writer-character-timeline-row="${index}">
          <div class="flex items-center justify-between gap-3">
            <select class="bamboo-field" data-writer-character-timeline-chapter="${index}">
              ${writerCharacterChapterOptions(entry.chapter_id)}
            </select>
            <button class="bamboo-btn-ghost text-rose-600" data-writer-character-remove-timeline="${index}" type="button">删除</button>
          </div>
          <textarea class="bamboo-field mt-3 min-h-[5rem] resize-y" data-writer-character-timeline-event="${index}" placeholder="这一章里他做了什么">${escapeHtml(entry.event || "")}</textarea>
          <div class="mt-3 grid gap-3 md:grid-cols-2">
            <input class="bamboo-field" data-writer-character-timeline-location="${index}" placeholder="所在位置" value="${escapeHtml(entry.location || "")}" />
            <input class="bamboo-field" data-writer-character-timeline-status="${index}" placeholder="当时状态" value="${escapeHtml(entry.status || "")}" />
          </div>
        </div>
      `
    )
    .join("");
}

function collectWriterCharacterTimelineEntries() {
  const rows = Array.from(document.querySelectorAll("[data-writer-character-timeline-row]"));
  return rows
    .map((row, index) => {
      const chapterId = Number(row.querySelector(`[data-writer-character-timeline-chapter="${index}"]`)?.value || 0);
      const chapter = writerCharacterState.chapters.find((item) => item.id === chapterId);
      const event = String(row.querySelector(`[data-writer-character-timeline-event="${index}"]`)?.value || "").trim();
      const location = String(row.querySelector(`[data-writer-character-timeline-location="${index}"]`)?.value || "").trim();
      const status = String(row.querySelector(`[data-writer-character-timeline-status="${index}"]`)?.value || "").trim();
      if (!chapterId || !chapter || (!event && !location && !status)) {
        return null;
      }
      return {
        chapter_id: chapterId,
        chapter_number: Number(chapter.sequence_number || 0) || index + 1,
        chapter_label: chapter.title || `第${index + 1}章`,
        chapter_title: chapter.title || "",
        event,
        location,
        status,
      };
    })
    .filter(Boolean);
}

function openWriterCharacterModal(character = null) {
  writerCharacterState.editingId = character?.id || null;
  writerCharacterState.lifeStatuses = writerCharacterStatuses(character);
  writerCharacterState.timelineDraft = writerCharacterTimelineEntries(character);

  document.getElementById("writer-character-modal-title").textContent = character ? `编辑人物：${character.name}` : "新增人物";
  document.getElementById("writer-character-name").value = character?.name || "";
  document.getElementById("writer-character-age").value = writerCharacterCardValue(character, "age");
  document.getElementById("writer-character-aliases").value = (character?.aliases || []).join("、");
  document.getElementById("writer-character-short-goal").value = writerCharacterCardValue(character, "short_term_goal");
  document.getElementById("writer-character-long-goal").value = writerCharacterCardValue(character, "long_term_goal");
  document.getElementById("writer-character-motivation").value = writerCharacterCardValue(character, "motivation");
  document.getElementById("writer-character-personality").value = writerCharacterCardValue(character, "personality");
  document.getElementById("writer-character-appearance").value = writerCharacterCardValue(character, "appearance");
  document.getElementById("writer-character-weakness").value = writerCharacterCardValue(character, "weakness");
  document.getElementById("writer-character-secrets").value = character?.secrets || "";
  document.getElementById("writer-character-biography").value = character?.biography || character?.description || "";
  document.getElementById("writer-character-status-input").value = "";
  document.getElementById("writer-character-delete").classList.toggle("hidden", !character);

  renderWriterCharacterStatusesEditor();
  renderWriterCharacterTimelineEditor();
  document.getElementById("writer-character-modal").classList.remove("hidden");
  document.getElementById("writer-character-modal").classList.add("flex");
}

function closeWriterCharacterModal() {
  document.getElementById("writer-character-modal").classList.add("hidden");
  document.getElementById("writer-character-modal").classList.remove("flex");
}

function addWriterCharacterStatus(value) {
  const text = writerCharacterStatusLabel(value);
  if (!text) return;
  if (!writerCharacterState.lifeStatuses.includes(text)) {
    writerCharacterState.lifeStatuses = [...writerCharacterState.lifeStatuses, text];
  }
  renderWriterCharacterStatusesEditor();
}

function writerCurrentGenerationPayload() {
  return {
    module: document.getElementById("writer-module").value,
    target_field: document.getElementById("writer-target-field").value,
    apply_mode: document.getElementById("writer-apply-mode").value,
    target_units: Number(document.getElementById("writer-target-units").value || 2000),
    previous_chapters: Number(document.getElementById("writer-previous-chapters").value || 1),
    chunk_size: Number(document.getElementById("writer-chunk-size").value || 900),
    user_prompt: document.getElementById("writer-prompt").value,
    system_prompt_override: getSystemPromptEditorValue().trim() || undefined,
    use_reasoner_planning: document.getElementById("writer-use-reasoner").checked,
    store_snapshot: true,
  };
}

function currentDraftEditorValue() {
  const editor = document.getElementById("writer-draft-editor");
  return editor ? editor.value : "";
}

function setDraftStatus(message, tone = "default") {
  const element = document.getElementById("writer-draft-status");
  if (!element) return;
  element.textContent = message;
  element.className = "rounded-2xl px-4 py-3 text-sm leading-7";
  if (tone === "error") {
    element.classList.add("bg-rose-50", "text-rose-700");
    return;
  }
  if (tone === "success") {
    element.classList.add("bg-emerald-50", "text-emerald-700");
    return;
  }
  if (tone === "loading") {
    element.classList.add("animate-pulse", "bg-bamboo-highlight", "text-bamboo-deep");
    return;
  }
  element.classList.add("bg-white/88", "text-slate-500");
}

function computeDraftAdjustmentMode(response, draftText) {
  const targetUnits = Number(response?.target_units || 0);
  if (!targetUnits) return "none";
  const actualUnits = draftText ? draftText.length : 0;
  void actualUnits;
  const draftUnits = estimateDraftUnits(draftText);
  const tolerance = Math.max(60, Math.floor(targetUnits * 0.12));
  if (Math.abs(draftUnits - targetUnits) <= tolerance) return "none";
  return draftUnits < targetUnits ? "expand" : "trim";
}

function estimateDraftUnits(text) {
  const value = String(text || "").trim();
  const cjk = (value.match(/[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3]/g) || []).length;
  const latin = (value.match(/[A-Za-z0-9_'-]+/g) || []).length;
  return cjk + latin;
}

function renderDraftMeta(response, draftText) {
  const targetUnits = Number(response?.target_units || 0);
  const draftUnits = estimateDraftUnits(draftText);
  const delta = targetUnits ? draftUnits - targetUnits : 0;
  const deltaText = !targetUnits || delta === 0 ? "已接近目标字数" : delta > 0 ? `超出 ${formatCount(delta)} 字` : `少了 ${formatCount(Math.abs(delta))} 字`;
  return `目标 ${formatCount(targetUnits || 0)} 字 · 当前 ${formatCount(draftUnits)} 字 · ${deltaText}`;
}

function updateDraftModal(response, { preserveStatus = false } = {}) {
  const editor = document.getElementById("writer-draft-editor");
  const meta = document.getElementById("writer-draft-meta");
  const hint = document.getElementById("writer-draft-hint");
  const refineButton = document.getElementById("writer-draft-refine");
  const applyButton = document.getElementById("writer-draft-apply");
  if (!editor || !meta || !hint || !refineButton || !applyButton) return;

  const draftText = response?.generated_text || "";
  editor.value = draftText;
  meta.textContent = renderDraftMeta(response, draftText);

  const adjustmentMode = computeDraftAdjustmentMode(response, draftText);
  writerDraftState.response = {
    ...response,
    refinement_suggestion: {
      ...(response?.refinement_suggestion || {}),
      mode: adjustmentMode,
    },
  };

  if (adjustmentMode === "expand") {
    refineButton.textContent = "二次扩写到目标字数";
    refineButton.classList.remove("hidden");
    hint.textContent = "当前草稿比目标偏短。你可以直接插入，也可以先做二次扩写。";
  } else if (adjustmentMode === "trim") {
    refineButton.textContent = "二次精简到目标字数";
    refineButton.classList.remove("hidden");
    hint.textContent = "当前草稿比目标偏长。你可以直接插入，也可以先做二次精简。";
  } else {
    refineButton.classList.add("hidden");
    hint.textContent = "当前草稿已接近目标字数。确认无误后可直接插入正文。";
  }

  applyButton.textContent =
    response?.target_field === "content"
      ? "直接插入正文"
      : response?.target_field === "outline"
        ? "直接写入大纲"
        : "直接写入摘要";

  if (!preserveStatus) {
    setDraftStatus("第一版草稿已生成。你可以直接插入，或先做二次扩写/精简。");
  }
}

function openDraftModal(bookId, chapterId, payload, response) {
  const modal = document.getElementById("writer-draft-modal");
  if (!modal) return;
  writerDraftState.isOpen = true;
  writerDraftState.bookId = bookId;
  writerDraftState.chapterId = chapterId;
  writerDraftState.basePayload = { ...payload };
  modal.classList.remove("hidden");
  modal.classList.add("flex");
  updateDraftModal(response);
}

function closeDraftModal() {
  const modal = document.getElementById("writer-draft-modal");
  if (!modal) return;
  writerDraftState.isOpen = false;
  modal.classList.add("hidden");
  modal.classList.remove("flex");
}

function renderWriterConfirmMeta(payload, previewResponse) {
  const preview = previewResponse?.context_preview || {};
  const relatedCharacters = Array.isArray(preview.related_characters) ? preview.related_characters.length : 0;
  const previousChapters = Array.isArray(preview.previous_chapters) ? preview.previous_chapters.length : 0;
  const summaryItems = [
    ["写作模式", aiModuleLabel(payload.module)],
    ["写入位置", payload.target_field === "content" ? "正文" : payload.target_field === "outline" ? "大纲" : "摘要"],
    ["写入方式", payload.apply_mode === "append" ? "接在后面" : "直接覆盖"],
    ["目标字数", `${formatCount(payload.target_units || 0)} 字`],
    ["单段字数", `${formatCount(payload.chunk_size || 0)} 字`],
    ["向前引用", `${previousChapters}/${formatCount(payload.previous_chapters || 0)} 章`],
    ["关联人物", `${relatedCharacters} 个`],
  ];
  return summaryItems
    .map(
      ([label, value]) => `
        <div class="rounded-2xl bg-bamboo-highlight/55 px-3 py-2">
          <div class="text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(label)}</div>
          <div class="mt-1 text-sm text-bamboo-deep">${escapeHtml(value)}</div>
        </div>
      `
    )
    .join("");
}

function openWriterConfirmModal(bookId, chapterId, payload, previewResponse) {
  const modal = document.getElementById("writer-confirm-modal");
  const systemField = document.getElementById("writer-confirm-system");
  const userField = document.getElementById("writer-confirm-user");
  const meta = document.getElementById("writer-confirm-meta");
  const status = document.getElementById("writer-confirm-status");
  if (!modal || !systemField || !userField || !meta || !status) return;

  writerConfirmState.isOpen = true;
  writerConfirmState.bookId = bookId;
  writerConfirmState.chapterId = chapterId;
  writerConfirmState.payload = { ...payload };
  writerConfirmState.previewResponse = previewResponse;

  systemField.value = previewResponse?.context_preview?.system_prompt || "";
  userField.value = previewResponse?.context_preview?.user_prompt || "";
  meta.innerHTML = renderWriterConfirmMeta(payload, previewResponse);
  status.textContent = "这里展示的是本次真正提交给 AI 的提示词。确认无误后，再开始谱写。";
  modal.classList.remove("hidden");
  modal.classList.add("flex");
}

function closeWriterConfirmModal() {
  const modal = document.getElementById("writer-confirm-modal");
  if (!modal) return;
  writerConfirmState.isOpen = false;
  writerConfirmState.bookId = null;
  writerConfirmState.chapterId = null;
  writerConfirmState.payload = null;
  writerConfirmState.previewResponse = null;
  modal.classList.add("hidden");
  modal.classList.remove("flex");
}

function normalizeSystemPromptValue(value) {
  return typeof value === "string" ? value.replace(/\r\n/g, "\n") : "";
}

function getSystemPromptEditorValue() {
  const editor = document.getElementById("writer-system-prompt-editor");
  if (editor) {
    return normalizeSystemPromptValue(editor.value);
  }
  return normalizeSystemPromptValue(writerPreviewState.systemPromptDraft);
}

function setSystemPromptDraft(value, { saved = false } = {}) {
  writerPreviewState.systemPromptDraft = normalizeSystemPromptValue(value);
  writerPreviewState.hasSavedSystemPrompt = saved;
}

function getEffectiveSystemPrompt(previewSystemPrompt = "") {
  const draft = getSystemPromptEditorValue();
  if (draft.trim()) {
    return draft;
  }
  return normalizeSystemPromptValue(previewSystemPrompt);
}

function renderSystemPromptEditor(systemPrompt) {
  return `
    <div>
      <div class="flex items-center justify-between gap-3">
        <div class="bamboo-mini-label">系统写作要求</div>
        <button class="bamboo-btn-secondary px-4 py-2 text-xs" id="writer-save-system-prompt" type="button">保存修改</button>
      </div>
      <textarea class="bamboo-field mt-3 min-h-[12rem] resize-y bg-white/88 text-sm leading-7" id="writer-system-prompt-editor" placeholder="这里会显示本次生成实际使用的系统写作要求，可直接修改。">${escapeHtml(systemPrompt)}</textarea>
      <p class="mt-2 text-xs leading-6 text-slate-500" id="writer-system-prompt-hint">
        这里可以直接修改本次生成要提交给 AI 的系统写作要求。点击“保存修改”后会在当前页面暂存；即使不点保存，直接点“开始生成”也会提交当前框内内容。
      </p>
      <div class="mt-2 text-xs ${writerPreviewState.hasSavedSystemPrompt ? "text-emerald-600" : "text-slate-400"}" id="writer-system-prompt-save-state">
        ${writerPreviewState.hasSavedSystemPrompt ? "已保存本页写作要求，后续生成会优先使用。" : "当前为预览内容，可按需修改后保存。"}
      </div>
    </div>
  `;
}

function bindSystemPromptEditor(defaultValue = "") {
  const editor = document.getElementById("writer-system-prompt-editor");
  const saveButton = document.getElementById("writer-save-system-prompt");
  const saveState = document.getElementById("writer-system-prompt-save-state");
  if (!editor || !saveButton || !saveState) return;

  if (!getSystemPromptEditorValue().trim() && normalizeSystemPromptValue(defaultValue).trim()) {
    setSystemPromptDraft(defaultValue, { saved: false });
    editor.value = normalizeSystemPromptValue(defaultValue);
  }

  editor.addEventListener("input", () => {
    setSystemPromptDraft(editor.value, { saved: false });
    saveState.textContent = "已修改，点击“保存修改”后可在本页暂存；直接开始生成也会使用当前内容。";
    saveState.className = "mt-2 text-xs text-amber-600";
  });

  saveButton.addEventListener("click", () => {
    setSystemPromptDraft(editor.value, { saved: true });
    saveState.textContent = "已保存本页写作要求，后续生成会优先使用。";
    saveState.className = "mt-2 text-xs text-emerald-600";
    setWriterStatus("系统写作要求已保存，本次生成会提交修改后的内容。", "success");
    showToast("系统写作要求已保存");
  });
}

function renderContextPreview(response) {
  const preview = response.context_preview || {};
  const systemPrompt = getEffectiveSystemPrompt(preview.system_prompt || "");
  return `
    <div class="space-y-4">
      ${renderSystemPromptEditor(systemPrompt)}
      <div>
        <div class="bamboo-mini-label">关联人物</div>
        <p class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml((preview.related_characters || []).map((item) => item.name).join("、") || "暂无")}</p>
      </div>
      <div>
        <div class="bamboo-mini-label">引用上文</div>
        <p class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(`${(preview.previous_chapters || []).length} 段历史内容已拼装`)}</p>
      </div>
    </div>
  `;
}

function renderGenerateSummary(response) {
  const preview = response.context_preview || {};
  const systemPrompt = getEffectiveSystemPrompt(preview.system_prompt || "");
  const metrics = [
    `本次写入 ${formatCount(response.generated_units || 0)} 字`,
    response.applied_units != null ? `章节当前共 ${formatCount(response.applied_units)} 字` : null,
    response.was_trimmed
      ? `已按目标长度截断，模型原始返回约 ${formatCount(response.generated_units_before_trim || response.generated_units || 0)} 字`
      : null,
  ].filter(Boolean);

  return `
    <div class="space-y-4">
      <div>
        <div class="bamboo-mini-label">本次结果</div>
        <p class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(metrics.join(" · "))}</p>
      </div>
      <div>
        <div class="bamboo-mini-label">生成摘要</div>
        <p class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(truncate(response.generated_text, 260))}</p>
      </div>
      <div>
        <div class="bamboo-mini-label">写前规划</div>
        <p class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(truncate(response.planning_text || "本次未启用写前规划。", 220))}</p>
      </div>
      ${renderSystemPromptEditor(systemPrompt)}
    </div>
  `;
}

function setWriterStatus(message, tone = "default") {
  const element = document.getElementById("writer-action-status");
  element.textContent = message;
  element.className = "rounded-2xl px-4 py-3 text-sm leading-7";
  if (tone === "error") {
    element.classList.add("bg-rose-50", "text-rose-700");
    return;
  }
  if (tone === "success") {
    element.classList.add("bg-emerald-50", "text-emerald-700");
    return;
  }
  if (tone === "loading") {
    element.classList.add("animate-pulse", "bg-bamboo-highlight", "text-bamboo-deep");
    return;
  }
  element.classList.add("bg-white/80", "text-slate-500");
}

function setButtonsBusy(ids, isBusy, loadingTextById = {}) {
  ids.forEach((id) => {
    const button = document.getElementById(id);
    if (!button) return;
    if (!button.dataset.defaultText) {
      button.dataset.defaultText = button.textContent;
    }
    button.disabled = isBusy;
    button.classList.toggle("opacity-60", isBusy);
    button.classList.toggle("pointer-events-none", isBusy);
    button.textContent = isBusy ? loadingTextById[id] || button.dataset.defaultText : button.dataset.defaultText;
  });
}

function setButtonsDisabled(ids, isDisabled) {
  ids.forEach((id) => {
    const button = document.getElementById(id);
    if (!button) return;
    button.disabled = isDisabled;
    button.classList.toggle("opacity-60", isDisabled);
    button.classList.toggle("pointer-events-none", isDisabled);
  });
}

function bindCreateChapterForm(bookId) {
  const form = document.getElementById("writer-create-chapter-form");
  const button = document.getElementById("writer-create-chapter");
  if (!form || !button || form.dataset.bound === "true") return;
  form.dataset.bound = "true";
  let submitting = false;

  async function handleCreateChapter(event) {
    if (event) {
      event.preventDefault();
    }
    if (submitting) return;

    const title = document.getElementById("writer-new-chapter").value.trim();
    if (!title) {
      showToast("请先输入章节标题", "error");
      setWriterStatus("请先输入章节标题。", "error");
      return;
    }

    submitting = true;
    setButtonsBusy(["writer-create-chapter"], true, { "writer-create-chapter": "创建中..." });
    setWriterStatus(`正在创建章节“${title}”...`, "loading");
    try {
      const chapter = await createChapter(bookId, { title });
      showToast("章节已创建");
      document.getElementById("writer-new-chapter").value = "";
      window.location.href = `/writer?book=${bookId}&chapter=${chapter.id}`;
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "创建章节失败";
      showToast(message, "error");
      setWriterStatus(message, "error");
      setButtonsBusy(["writer-create-chapter"], false, { "writer-create-chapter": "创建中..." });
    } finally {
      submitting = false;
    }
  }

  form.addEventListener("submit", handleCreateChapter);
}

export async function initWriterPage() {
  setSystemPromptDraft("", { saved: false });
  const books = (await listBooks()).items || [];
  const bookId = selectedBookId(books);
  const bookSelect = document.getElementById("writer-book-select");

  bookSelect.innerHTML = books.length
    ? books.map((book) => `<option value="${book.id}" ${book.id === bookId ? "selected" : ""}>${escapeHtml(book.title)}</option>`).join("")
    : '<option value="">暂无书籍</option>';

  if (!bookId) {
    setAssistantContext();
    document.getElementById("writer-empty").classList.remove("hidden");
    return;
  }

  bindCreateChapterForm(bookId);
  ensureWriterGenerationFx();

  const [book, chapterListResponse, configsResponse, charactersResponse] = await Promise.all([
    getBook(bookId),
    listChapters(bookId),
    listAIConfigs({ book_id: bookId }),
    listCharacters(bookId),
  ]);

  const chapters = chapterListResponse.items || [];
  writerCharacterState.chapters = chapters;
  writerCharacterState.items = (charactersResponse.items || []).sort((left, right) =>
    String(left.name || "").localeCompare(String(right.name || ""), "zh-CN")
  );
  const chapterId = selectedChapterId(chapters);
  const currentChapter = chapters.find((item) => item.id === chapterId) || null;
  setAssistantContext({ bookId, chapterId, chapterTitle: currentChapter?.title || null });
  const availableConfigs = (configsResponse.items || []).filter((item) => item.scope !== "book" || item.book_id === bookId);
  const enabledModules = new Set(
    availableConfigs.filter((item) => item.is_enabled).map((item) => item.module)
  );
  const hasAIConfigs = enabledModules.size > 0;

  document.getElementById("writer-book").textContent = book.title;
  document.getElementById("writer-book-select").addEventListener("change", () => {
    window.location.href = `/writer?book=${bookSelect.value}`;
  });
  document.getElementById("writer-tree").innerHTML = renderChapterTree(chapters, chapterId, bookId);
  document.getElementById("writer-ai-configs").innerHTML = renderAIConfigs(availableConfigs);
  const writerCharacterWorkspace = document.getElementById("writer-characters");
  if (writerCharacterWorkspace) {
    writerCharacterWorkspace.innerHTML = renderWriterCharacterRows(writerCharacterState.items);

    async function refreshCharacterWorkspace() {
      const response = await listCharacters(bookId);
      writerCharacterState.items = (response.items || []).sort((left, right) =>
        String(left.name || "").localeCompare(String(right.name || ""), "zh-CN")
      );
      writerCharacterWorkspace.innerHTML = renderWriterCharacterRows(writerCharacterState.items);
      bindWriterCharacterButtons();
    }

    async function refreshCharacterChapters() {
      const response = await listChapters(bookId);
      writerCharacterState.chapters = response.items || [];
    }

    async function handleWriterCharacterSubmit(event) {
      event.preventDefault();
      const current = writerCharacterState.items.find((item) => item.id === writerCharacterState.editingId) || null;
      writerCharacterState.timelineDraft = collectWriterCharacterTimelineEntries();
      const baseCard = current?.card_json && typeof current.card_json === "object" ? { ...current.card_json } : {};
      const fieldMap = {
        age: document.getElementById("writer-character-age").value.trim(),
        short_term_goal: document.getElementById("writer-character-short-goal").value.trim(),
        long_term_goal: document.getElementById("writer-character-long-goal").value.trim(),
        motivation: document.getElementById("writer-character-motivation").value.trim(),
        personality: document.getElementById("writer-character-personality").value.trim(),
        appearance: document.getElementById("writer-character-appearance").value.trim(),
        weakness: document.getElementById("writer-character-weakness").value.trim(),
      };
      Object.entries(fieldMap).forEach(([key, value]) => {
        if (value) {
          baseCard[key] = value;
        } else {
          delete baseCard[key];
        }
      });

      const payload = {
        name: document.getElementById("writer-character-name").value.trim(),
        aliases: parseWriterCharacterAliases(document.getElementById("writer-character-aliases").value),
        biography: document.getElementById("writer-character-biography").value.trim() || null,
        description: document.getElementById("writer-character-biography").value.trim() || null,
        secrets: document.getElementById("writer-character-secrets").value.trim() || null,
        life_statuses: writerCharacterState.lifeStatuses,
        timeline_entries: writerCharacterState.timelineDraft,
        card_json: {
          ...baseCard,
          life_statuses: writerCharacterState.lifeStatuses,
          timeline_entries: writerCharacterState.timelineDraft,
        },
      };

      try {
        if (writerCharacterState.editingId) {
          await updateCharacter(bookId, writerCharacterState.editingId, payload);
          showToast("人物卡已更新");
        } else {
          await createCharacter(bookId, payload);
          showToast("人物卡已创建");
        }
        closeWriterCharacterModal();
        await refreshCharacterWorkspace();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "保存人物卡失败";
        showToast(message, "error");
      }
    }

    async function handleWriterCharacterDelete() {
      if (!writerCharacterState.editingId) return;
      if (!window.confirm("确认删除这个人物及其关联关系吗？")) return;
      try {
        await deleteCharacter(bookId, writerCharacterState.editingId);
        showToast("人物卡已删除");
        closeWriterCharacterModal();
        await refreshCharacterWorkspace();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "删除人物卡失败";
        showToast(message, "error");
      }
    }

    function bindWriterCharacterButtons() {
      document.querySelectorAll("[data-writer-edit-character]").forEach((button) => {
        button.addEventListener("click", () => {
          const character = writerCharacterState.items.find((item) => item.id === Number(button.dataset.writerEditCharacter));
          if (!character) return;
          openWriterCharacterModal(character);
        });
      });
    }

    bindWriterCharacterButtons();

    document.getElementById("writer-character-create")?.addEventListener("click", () => {
      writerCharacterState.editingId = null;
      writerCharacterState.lifeStatuses = [];
      writerCharacterState.timelineDraft = [];
      openWriterCharacterModal(null);
    });

    document.getElementById("writer-character-delete-all")?.addEventListener("click", async () => {
      if (!writerCharacterState.items.length) {
        showToast("当前没有可删除的人物卡", "error");
        return;
      }
      if (!window.confirm("确认一键删除当前书籍的所有人物卡吗？关联关系也会一并清空。")) return;
      try {
        const result = await deleteAllCharacters(bookId);
        closeWriterCharacterModal();
        showToast(`已删除 ${result.deleted_character_count || 0} 张人物卡，并清理 ${result.deleted_relation_count || 0} 条关系`);
        await refreshCharacterWorkspace();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "批量删除人物卡失败";
        showToast(message, "error");
      }
    });

    document.getElementById("writer-character-form")?.addEventListener("submit", handleWriterCharacterSubmit);
    document.getElementById("writer-character-close")?.addEventListener("click", closeWriterCharacterModal);
    document.getElementById("writer-character-cancel")?.addEventListener("click", closeWriterCharacterModal);
    document.getElementById("writer-character-delete")?.addEventListener("click", handleWriterCharacterDelete);
    document.getElementById("writer-character-modal")?.addEventListener("click", (event) => {
      if (event.target?.id === "writer-character-modal") {
        closeWriterCharacterModal();
      }
    });
    document.getElementById("writer-character-status-add")?.addEventListener("click", () => {
      const input = document.getElementById("writer-character-status-input");
      const value = input.value.trim();
      if (!value) return;
      addWriterCharacterStatus(value);
      input.value = "";
    });
    document.getElementById("writer-character-status-input")?.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const input = document.getElementById("writer-character-status-input");
      const value = input.value.trim();
      if (!value) return;
      addWriterCharacterStatus(value);
      input.value = "";
    });
    document.getElementById("writer-character-statuses")?.addEventListener("click", (event) => {
      const presetButton = event.target instanceof HTMLElement ? event.target.closest("[data-writer-character-preset-status]") : null;
      if (presetButton instanceof HTMLButtonElement) {
        addWriterCharacterStatus(presetButton.dataset.writerCharacterPresetStatus || "");
        return;
      }
      const removeButton = event.target instanceof HTMLElement ? event.target.closest("[data-writer-character-remove-status]") : null;
      if (removeButton instanceof HTMLButtonElement) {
        const value = removeButton.dataset.writerCharacterRemoveStatus || "";
        writerCharacterState.lifeStatuses = writerCharacterState.lifeStatuses.filter((item) => item !== value);
        renderWriterCharacterStatusesEditor();
      }
    });
    document.getElementById("writer-character-timeline-add")?.addEventListener("click", async () => {
      await refreshCharacterChapters();
      writerCharacterState.timelineDraft = [...collectWriterCharacterTimelineEntries(), {}];
      renderWriterCharacterTimelineEditor();
    });
    document.getElementById("writer-character-timeline-list")?.addEventListener("click", (event) => {
      const removeButton = event.target instanceof HTMLElement ? event.target.closest("[data-writer-character-remove-timeline]") : null;
      if (!(removeButton instanceof HTMLButtonElement)) return;
      const index = Number(removeButton.dataset.writerCharacterRemoveTimeline || -1);
      writerCharacterState.timelineDraft = collectWriterCharacterTimelineEntries().filter((_item, itemIndex) => itemIndex !== index);
      renderWriterCharacterTimelineEditor();
    });
  }

  // 章节切换时检查未保存内容
  document.getElementById("writer-tree").addEventListener("click", (event) => {
    const link = event.target instanceof HTMLElement ? event.target.closest("[data-navigate-chapter]") : null;
    if (!link) return;
    const currentContent = document.getElementById("writer-editor")?.value || "";
    if (currentContent !== lastSavedContent) {
      event.preventDefault();
      if (window.confirm("当前章节有未保存的内容，确定要切换章节吗？未保存的内容将丢失。")) {
        window.location.href = link.dataset.navigateChapter;
      }
    } else {
      window.location.href = link.dataset.navigateChapter;
    }
  });

  if (!chapterId) {
    document.getElementById("writer-empty").classList.remove("hidden");
    setWriterStatus("当前书籍还没有章节。请在左侧输入标题后创建第一章。");
    return;
  }

  const [chapter, snapshotsResponse] = await Promise.all([
    getChapter(bookId, chapterId),
    listSnapshots(bookId, { chapter_id: chapterId, limit: 10 }),
  ]);

  document.getElementById("writer-empty").classList.add("hidden");
  document.getElementById("writer-editor-pane").classList.remove("hidden");
  setAssistantContext({ bookId, chapterId, chapterTitle: chapter.title || null });
  document.getElementById("writer-chapter").value = chapter.title || "";
  document.getElementById("writer-outline").value = chapter.outline || "";
  document.getElementById("writer-summary").value = chapter.summary || "";
  document.getElementById("writer-editor").value = chapter.content || "";
  document.getElementById("writer-meta").innerHTML = `
    <span class="rounded-full px-3 py-1 text-xs font-medium ${statusTone(chapter.status)}">${escapeHtml(chapterStatusLabel(chapter.status))}</span>
    <span class="bamboo-chip">v${escapeHtml(chapter.version)}</span>
    <span class="bamboo-chip">${formatCount(chapter.word_count)} 字</span>
    <span class="text-xs text-slate-400" id="writer-live-word-count"></span>
  `;
  document.getElementById("writer-memory").innerHTML = renderMemory(book, chapter);
  document.getElementById("writer-snapshots").innerHTML = renderSnapshotList(bookId, snapshotsResponse.items || []);

  async function refreshWriterAfterApply(response) {
    const refreshed = await getChapter(bookId, chapterId);
    document.getElementById("writer-editor").value = refreshed.content || "";
    document.getElementById("writer-outline").value = refreshed.outline || "";
    document.getElementById("writer-summary").value = refreshed.summary || "";
    setAssistantContext({ bookId, chapterId, chapterTitle: refreshed.title || null });
    document.getElementById("writer-meta").innerHTML = `
      <span class="rounded-full px-3 py-1 text-xs font-medium ${statusTone(refreshed.status)}">${escapeHtml(chapterStatusLabel(refreshed.status))}</span>
      <span class="bamboo-chip">v${escapeHtml(refreshed.version)}</span>
      <span class="bamboo-chip">${formatCount(refreshed.word_count)} 字</span>
    `;
    document.getElementById("writer-memory").innerHTML = renderMemory(book, refreshed);
    document.getElementById("writer-context-preview").innerHTML = renderGenerateSummary(response);
    bindSystemPromptEditor(response.context_preview?.system_prompt || "");

    const snapshots = await listSnapshots(bookId, { chapter_id: chapterId, limit: 10 });
    document.getElementById("writer-snapshots").innerHTML = renderSnapshotList(bookId, snapshots.items || []);

    const generatedUnits = formatCount(response.generated_units || 0);
    const appliedUnits = formatCount(response.applied_units ?? refreshed.word_count ?? 0);
    setWriterStatus(
      `草稿已确认写入，本次写入 ${generatedUnits} 字，章节当前共 ${appliedUnits} 字，并保存快照 #${response.snapshot_id || "-"}`,
      "success"
    );
    showToast(`已写入正文，本次写入 ${generatedUnits} 字`);
  }

  function bindDraftModalActions() {
    const modal = document.getElementById("writer-draft-modal");
    const closeButton = document.getElementById("writer-draft-close");
    const applyButton = document.getElementById("writer-draft-apply");
    const refineButton = document.getElementById("writer-draft-refine");
    const draftEditor = document.getElementById("writer-draft-editor");
    const meta = document.getElementById("writer-draft-meta");
    if (!modal || !closeButton || !applyButton || !refineButton || !draftEditor || !meta) return;
    if (modal.dataset.bound === "true") return;
    modal.dataset.bound = "true";

    closeButton.addEventListener("click", () => {
      closeDraftModal();
      setWriterStatus("草稿已生成但尚未插入正文。你可以重新生成，或稍后再操作。");
    });

    modal.addEventListener("click", (event) => {
      if (event.target === modal) {
        closeDraftModal();
      }
    });

    draftEditor.addEventListener("input", () => {
      if (!writerDraftState.response) return;
      meta.textContent = renderDraftMeta(writerDraftState.response, draftEditor.value);
      const mode = computeDraftAdjustmentMode(writerDraftState.response, draftEditor.value);
      writerDraftState.response = {
        ...writerDraftState.response,
        generated_text: draftEditor.value,
        generated_units: estimateDraftUnits(draftEditor.value),
        refinement_suggestion: {
          ...(writerDraftState.response.refinement_suggestion || {}),
          mode,
        },
      };
      if (mode === "expand") {
        refineButton.textContent = "二次扩写到目标字数";
        refineButton.classList.remove("hidden");
      } else if (mode === "trim") {
        refineButton.textContent = "二次精简到目标字数";
        refineButton.classList.remove("hidden");
      } else {
        refineButton.classList.add("hidden");
      }
    });

    refineButton.addEventListener("click", async () => {
      if (!writerDraftState.response || !writerDraftState.basePayload) return;
      const adjustmentMode = computeDraftAdjustmentMode(writerDraftState.response, draftEditor.value);
      if (adjustmentMode === "none") {
        setDraftStatus("当前草稿已接近目标字数，无需二次调整。", "success");
        refineButton.classList.add("hidden");
        return;
      }

      setButtonsBusy(["writer-draft-refine", "writer-draft-apply"], true, {
        "writer-draft-refine": adjustmentMode === "expand" ? "扩写中..." : "精简中...",
        "writer-draft-apply": "直接插入正文",
      });
      setDraftStatus(
        adjustmentMode === "expand" ? "正在让 AI 对草稿做二次扩写..." : "正在让 AI 对草稿做二次精简...",
        "loading"
      );
      try {
        const response = await refineAIDraft(bookId, chapterId, {
          ...writerDraftState.basePayload,
          draft_text: draftEditor.value,
          adjustment_mode: adjustmentMode,
          planning_text: writerDraftState.response.planning_text || null,
        });
        updateDraftModal(response, { preserveStatus: true });
        setDraftStatus(
          adjustmentMode === "expand" ? "二次扩写完成。请确认后再插入正文。" : "二次精简完成。请确认后再插入正文。",
          "success"
        );
        showToast(adjustmentMode === "expand" ? "二次扩写完成" : "二次精简完成");
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "二次调整失败";
        setDraftStatus(message, "error");
        showToast(message, "error");
      } finally {
        setButtonsBusy(["writer-draft-refine", "writer-draft-apply"], false, {
          "writer-draft-refine": refineButton.textContent,
          "writer-draft-apply": "直接插入正文",
        });
      }
    });

    applyButton.addEventListener("click", async () => {
      if (!writerDraftState.response || !writerDraftState.basePayload) return;
      setButtonsBusy(["writer-draft-refine", "writer-draft-apply"], true, {
        "writer-draft-refine": refineButton.textContent || "二次调整",
        "writer-draft-apply": "写入中...",
      });
      setDraftStatus("正在把确认后的草稿写入章节正文...", "loading");
      try {
        const response = await applyAIDraft(bookId, chapterId, {
          ...writerDraftState.basePayload,
          generated_text: draftEditor.value,
          planning_text: writerDraftState.response.planning_text || null,
          store_snapshot: true,
        });
        closeDraftModal();
        await refreshWriterAfterApply(response);
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "写入正文失败";
        setDraftStatus(message, "error");
        showToast(message, "error");
      } finally {
        setButtonsBusy(["writer-draft-refine", "writer-draft-apply"], false, {
          "writer-draft-refine": refineButton.textContent || "二次调整",
          "writer-draft-apply": "直接插入正文",
        });
      }
    });
  }

  bindDraftModalActions();

  function selectedModuleIsReady() {
    return enabledModules.has(document.getElementById("writer-module").value);
  }

  function syncAIAvailabilityStatus() {
    if (!hasAIConfigs) {
      setWriterStatus("当前没有可用的 AI 功能配置。请先到“配置”页设置接口地址、API Key 和模型。", "error");
      setButtonsDisabled(["writer-preview", "writer-generate"], true);
      return false;
    }

    if (!selectedModuleIsReady()) {
      setWriterStatus(`“${aiModuleLabel(document.getElementById("writer-module").value)}”还没有配置可用模型，请先到“配置”页补充。`, "error");
      setButtonsDisabled(["writer-preview", "writer-generate"], true);
      return false;
    }

    setButtonsDisabled(["writer-preview", "writer-generate"], false);
    setWriterStatus("填写补充要求后，可先预览上下文，再开始生成。生成时页面会显示实时状态。");
    return true;
  }

  syncAIAvailabilityStatus();
  document.getElementById("writer-module").addEventListener("change", () => {
    syncAIAvailabilityStatus();
  });

  async function fetchWriterContextPreview(payload) {
    const response = await previewAI(bookId, chapterId, payload);
    document.getElementById("writer-context-preview").innerHTML = renderContextPreview(response);
    bindSystemPromptEditor(response.context_preview?.system_prompt || "");
    return response;
  }

  async function runWriterGeneration(payload) {
    setButtonsBusy(
      ["writer-preview", "writer-generate", "writer-save"],
      true,
      {
        "writer-preview": "预览上下文",
        "writer-generate": "生成中...",
        "writer-save": "保存章节",
      }
    );
    setWriterStatus("正在生成第一版草稿。这个过程可能需要 10 到 60 秒，请不要重复点击。", "loading");
    startWriterGenerationFx();

    try {
      const response = await streamAIDraft(bookId, chapterId, payload, {
        onEvent: (event) => {
          if (!event || typeof event !== "object") {
            return;
          }
          if (event.type === "status") {
            updateWriterGenerationFxProgress(event.message || "");
            return;
          }
          if (event.type === "draft") {
            updateWriterGenerationFxStreamSource(event.text || "");
          }
        },
      });
      await settleWriterGenerationFxStream(response.generated_text || "");
      await finishWriterGenerationFx(true);
      openDraftModal(bookId, chapterId, payload, response);
      setWriterStatus("第一版草稿已生成。请在弹窗里确认是否直接插入，或先做二次扩写/精简。", "success");
      showToast("第一版草稿已生成");
    } catch (error) {
      await finishWriterGenerationFx(false);
      const message = error instanceof ApiError ? error.message : "AI 生成失败";
      setWriterStatus(message, "error");
      showToast(message, "error");
    } finally {
      if (hasAIConfigs) {
        setButtonsBusy(
          ["writer-preview", "writer-generate", "writer-save"],
          false,
          {
            "writer-preview": "预览上下文",
            "writer-generate": "开始生成",
            "writer-save": "保存章节",
          }
        );
      }
    }
  }

  document.getElementById("writer-save").addEventListener("click", async () => {
    setButtonsBusy(["writer-save"], true, { "writer-save": "保存中..." });
    setWriterStatus("正在保存章节内容...");
    try {
      const updated = await updateChapter(bookId, chapterId, {
        title: document.getElementById("writer-chapter").value.trim(),
        outline: document.getElementById("writer-outline").value,
        summary: document.getElementById("writer-summary").value,
        content: document.getElementById("writer-editor").value,
      });
      setWriterStatus(`章节已保存到 v${updated.version}`, "success");
      showToast(`章节已保存到 v${updated.version}`);
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "保存章节失败";
      setWriterStatus(message, "error");
      showToast(message, "error");
    } finally {
      setButtonsBusy(["writer-save"], false, { "writer-save": "保存中..." });
    }
  });

  // 自动保存（每 30 秒检测一次，内容有变化才保存）
  let lastSavedContent = document.getElementById("writer-editor").value;
  let lastSavedOutline = document.getElementById("writer-outline").value;
  let lastSavedSummary = document.getElementById("writer-summary").value;
  let autoSaveTimer = null;
  const AUTO_SAVE_INTERVAL_MS = 30_000;

  function updateLiveWordCount() {
    const editor = document.getElementById("writer-editor");
    const text = editor ? editor.value || "" : "";
    const count = text.replace(/\s/g, "").length;
    const wordCountEl = document.getElementById("writer-live-word-count");
    if (wordCountEl) {
      wordCountEl.textContent = `实时 ${formatCount(count)} 字`;
    }
  }

  const editorEl = document.getElementById("writer-editor");
  if (editorEl) {
    editorEl.addEventListener("input", updateLiveWordCount);
  }
  updateLiveWordCount();

  autoSaveTimer = window.setInterval(async () => {
    if (!bookId || !chapterId) return;
    const currentContent = document.getElementById("writer-editor").value;
    const currentOutline = document.getElementById("writer-outline").value;
    const currentSummary = document.getElementById("writer-summary").value;
    if (currentContent === lastSavedContent && currentOutline === lastSavedOutline && currentSummary === lastSavedSummary) return;
    try {
      const updated = await updateChapter(bookId, chapterId, {
        title: document.getElementById("writer-chapter").value.trim(),
        outline: currentOutline,
        summary: currentSummary,
        content: currentContent,
      });
      lastSavedContent = currentContent;
      lastSavedOutline = currentOutline;
      lastSavedSummary = currentSummary;
      document.getElementById("writer-meta").innerHTML = `
        <span class="rounded-full px-3 py-1 text-xs font-medium ${statusTone(updated.status)}">${escapeHtml(chapterStatusLabel(updated.status))}</span>
        <span class="bamboo-chip">v${escapeHtml(updated.version)}</span>
        <span class="bamboo-chip">${formatCount(updated.word_count)} 字</span>
        <span class="text-xs text-emerald-600" id="writer-live-word-count">已自动保存</span>
      `;
      window.setTimeout(() => {
        const hint = document.getElementById("writer-live-word-count");
        if (hint && hint.textContent === "已自动保存") {
          hint.textContent = `实时 ${formatCount(currentContent.replace(/\s/g, "").length)} 字`;
          hint.className = "text-xs text-slate-400";
        }
      }, 2000);
    } catch {
      // 静默失败，不干扰用户
    }
  }, AUTO_SAVE_INTERVAL_MS);

  function flushWriterDraftOnPageExit() {
    const currentContent = document.getElementById("writer-editor")?.value || "";
    const currentOutline = document.getElementById("writer-outline")?.value || "";
    const currentSummary = document.getElementById("writer-summary")?.value || "";
    if (
      currentContent !== lastSavedContent ||
      currentOutline !== lastSavedOutline ||
      currentSummary !== lastSavedSummary
    ) {
      void updateChapterKeepalive(bookId, chapterId, {
        title: document.getElementById("writer-chapter")?.value?.trim() || "",
        outline: currentOutline,
        summary: currentSummary,
        content: currentContent,
      });
    }
  }

  // 页面离开前尝试保存
  window.addEventListener("beforeunload", flushWriterDraftOnPageExit);
  window.addEventListener("pagehide", flushWriterDraftOnPageExit);

  document.getElementById("writer-delete").addEventListener("click", async () => {
    if (!window.confirm("确认删除当前章节吗？")) return;
    setButtonsBusy(["writer-delete"], true, { "writer-delete": "删除中..." });
    setWriterStatus("正在删除章节...");
    try {
      await deleteChapter(bookId, chapterId);
      showToast("章节已删除");
      window.location.href = `/writer?book=${bookId}`;
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "删除章节失败";
      setWriterStatus(message, "error");
      showToast(message, "error");
      setButtonsBusy(["writer-delete"], false, { "writer-delete": "删除中..." });
    }
  });

  async function runWriterPreview(payload) {
    setButtonsBusy(
      ["writer-preview", "writer-generate"],
      true,
      { "writer-preview": "预览中...", "writer-generate": "开始生成" }
    );
    setWriterStatus("正在拼装上下文，这通常只需要几秒钟...", "loading");
    try {
      await fetchWriterContextPreview(payload);
      setWriterStatus("上下文预览已刷新。可以确认后开始生成。", "success");
      showToast("上下文预览已刷新");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "预览上下文失败";
      setWriterStatus(message, "error");
      showToast(message, "error");
    } finally {
      setButtonsBusy(
        ["writer-preview", "writer-generate"],
        false,
        { "writer-preview": "预览中...", "writer-generate": "开始生成" }
      );
    }
  }

  async function openWriterConfirmFlow(payload) {
    setButtonsBusy(
      ["writer-preview", "writer-generate"],
      true,
      { "writer-preview": "预览上下文", "writer-generate": "准备中..." }
    );
    setWriterStatus("正在生成前确认提示词，请稍候...", "loading");
    try {
      const previewResponse = await fetchWriterContextPreview(payload);
      openWriterConfirmModal(bookId, chapterId, payload, previewResponse);
      setWriterStatus("已打开提交确认窗口。确认提示词后，再开始谱写。", "success");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "生成前预览失败";
      setWriterStatus(message, "error");
      showToast(message, "error");
    } finally {
      setButtonsBusy(
        ["writer-preview", "writer-generate"],
        false,
        { "writer-preview": "预览上下文", "writer-generate": "开始生成" }
      );
    }
  }

  const previewButton = document.getElementById("writer-preview");
  const generateButton = document.getElementById("writer-generate");
  const confirmCloseButton = document.getElementById("writer-confirm-close");
  const confirmBackButton = document.getElementById("writer-confirm-back");
  const confirmSubmitButton = document.getElementById("writer-confirm-submit");
  const confirmModal = document.getElementById("writer-confirm-modal");

  previewButton?.addEventListener("click", async () => {
    if (!syncAIAvailabilityStatus()) {
      return;
    }
    await runWriterPreview(writerCurrentGenerationPayload());
  });

  generateButton?.addEventListener("click", async () => {
    if (!syncAIAvailabilityStatus()) {
      return;
    }
    await openWriterConfirmFlow(writerCurrentGenerationPayload());
  });

  confirmCloseButton?.addEventListener("click", () => {
    closeWriterConfirmModal();
  });
  confirmBackButton?.addEventListener("click", () => {
    closeWriterConfirmModal();
  });
  confirmModal?.addEventListener("click", (event) => {
    if (event.target?.id === "writer-confirm-modal") {
      closeWriterConfirmModal();
    }
  });
  confirmSubmitButton?.addEventListener("click", async () => {
    if (!writerConfirmState.payload) return;
    const payload = { ...writerConfirmState.payload };
    closeWriterConfirmModal();
    await runWriterGeneration(payload);
  });

}
