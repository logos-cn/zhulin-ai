import {
  ApiError,
  createCharacter,
  deleteAllCharacters,
  deleteCharacter,
  getBook,
  listBooks,
  listCharacters,
  listChapters,
  updateCharacter,
} from "./api.js";
import { escapeHtml, formatDateTime, formatCount, setAssistantContext, showToast, truncate } from "./shell.js";

const STATUS_LABELS = {
  alive: "活着",
  dead: "死亡",
  serious_injury: "重伤",
  minor_injury: "轻伤",
  disabled: "残疾",
};

const PRESET_STATUSES = ["活着", "死亡", "重伤", "轻伤", "残疾", "失踪", "失忆"];

const state = {
  books: [],
  bookId: null,
  book: null,
  chapters: [],
  characters: [],
  editingId: null,
  lifeStatuses: [],
  timelineDraft: [],
};

function selectedBookId(books) {
  const params = new URLSearchParams(window.location.search);
  const bookId = Number(params.get("book"));
  if (bookId && books.some((book) => book.id === bookId)) return bookId;
  return books[0]?.id || null;
}

function statusLabel(value) {
  const text = String(value || "").trim();
  return STATUS_LABELS[text] || STATUS_LABELS[text.toLowerCase()] || text;
}

function characterStatuses(character) {
  const values = Array.isArray(character?.life_statuses)
    ? character.life_statuses
    : Array.isArray(character?.card_json?.life_statuses)
      ? character.card_json.life_statuses
      : [];
  return values
    .map(statusLabel)
    .filter(Boolean)
    .filter((item, index, array) => array.indexOf(item) === index);
}

function characterTimelineEntries(character) {
  const values = Array.isArray(character?.timeline_entries)
    ? character.timeline_entries
    : Array.isArray(character?.card_json?.timeline_entries)
      ? character.card_json.timeline_entries
      : [];
  return values
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

function cardField(character, key) {
  const directValue = character?.[key];
  if (directValue !== undefined && directValue !== null && String(directValue).trim()) {
    return String(directValue).trim();
  }
  const cardValue = character?.card_json?.[key];
  if (cardValue !== undefined && cardValue !== null && String(cardValue).trim()) {
    return String(cardValue).trim();
  }
  return "";
}

function parseAliases(value) {
  return String(value || "")
    .split(/[\n,，、/|]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .filter((item, index, array) => array.indexOf(item) === index);
}

function chapterOptions(selectedId = null) {
  const options = ['<option value="">选择章节</option>'];
  state.chapters.forEach((chapter) => {
    const selected = Number(selectedId || 0) === Number(chapter.id) ? "selected" : "";
    options.push(`<option value="${chapter.id}" ${selected}>${escapeHtml(chapter.title)}</option>`);
  });
  return options.join("");
}

function filteredCharacters() {
  const keyword = String(document.getElementById("characters-search")?.value || "").trim().toLowerCase();
  const statusFilter = String(document.getElementById("characters-status-filter")?.value || "").trim();
  const sortBy = String(document.getElementById("characters-sort")?.value || "name").trim();

  const items = state.characters.filter((character) => {
    const aliases = (character.aliases || []).join(" ");
    const summary = `${character.name || ""} ${aliases} ${character.biography || character.description || ""}`.toLowerCase();
    if (keyword && !summary.includes(keyword)) return false;
    if (statusFilter && !characterStatuses(character).includes(statusFilter)) return false;
    return true;
  });

  items.sort((left, right) => {
    if (sortBy === "updated_at") {
      return new Date(right.updated_at || 0).getTime() - new Date(left.updated_at || 0).getTime();
    }
    return String(left.name || "").localeCompare(String(right.name || ""), "zh-CN");
  });
  return items;
}

function renderStats() {
  const container = document.getElementById("characters-stats");
  if (!container) return;
  const aliveCount = state.characters.filter((item) => characterStatuses(item).includes("活着")).length;
  const customStatusCount = new Set(
    state.characters.flatMap((item) => characterStatuses(item)).filter((item) => !PRESET_STATUSES.includes(item))
  ).size;
  const timelineCount = state.characters.reduce((total, item) => total + characterTimelineEntries(item).length, 0);
  const stats = [
    { label: "人物总数", value: formatCount(state.characters.length), meta: "当前书籍" },
    { label: "存活人物", value: formatCount(aliveCount), meta: "基于生命状态" },
    { label: "时间节点", value: formatCount(timelineCount), meta: "全书累计" },
    { label: "自定义状态", value: formatCount(customStatusCount), meta: "去重后" },
  ];
  container.innerHTML = stats
    .map(
      (item) => `
        <article class="bamboo-stat">
          <div class="bamboo-mini-label">${escapeHtml(item.label)}</div>
          <div class="mt-3 flex items-end justify-between gap-4">
            <div class="text-3xl font-semibold text-bamboo-deep">${escapeHtml(item.value)}</div>
            <div class="text-sm text-slate-500">${escapeHtml(item.meta)}</div>
          </div>
        </article>
      `
    )
    .join("");
}

function renderStatusFilterOptions() {
  const select = document.getElementById("characters-status-filter");
  if (!select) return;
  const statuses = Array.from(new Set(state.characters.flatMap((item) => characterStatuses(item))));
  const current = select.value;
  select.innerHTML = ['<option value="">全部状态</option>']
    .concat(statuses.sort((left, right) => left.localeCompare(right, "zh-CN")).map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`))
    .join("");
  select.value = statuses.includes(current) ? current : "";
}

function renderSummary(items) {
  const container = document.getElementById("characters-summary");
  if (!container) return;
  container.textContent = `当前显示 ${items.length} / ${state.characters.length} 张人物卡`;
}

function renderCharacterGrid() {
  const container = document.getElementById("characters-grid");
  if (!container) return;
  const items = filteredCharacters();
  renderSummary(items);
  if (!items.length) {
    container.innerHTML =
      '<div class="col-span-full rounded-[1.4rem] bg-white/80 p-6 text-sm text-slate-500 shadow-cloud">当前筛选下没有人物卡。</div>';
    return;
  }

  container.innerHTML = items
    .map((character) => {
      const aliases = (character.aliases || []).join("、") || "未填写";
      const age = cardField(character, "age") || "未填写";
      const biography = character.biography || character.description || "未填写人物小传";
      return `
        <article class="rounded-[1.7rem] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.96),rgba(247,242,232,0.88))] p-5 shadow-cloud">
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0">
              <h3 class="truncate text-xl font-semibold text-bamboo-deep">${escapeHtml(character.name || "未命名人物")}</h3>
              <div class="mt-2 flex flex-wrap gap-2">
                ${characterStatuses(character)
                  .slice(0, 3)
                  .map(
                    (status) =>
                      `<span class="rounded-full bg-bamboo-highlight px-3 py-1 text-xs font-medium text-bamboo-deep">${escapeHtml(status)}</span>`
                  )
                  .join("") || '<span class="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-500">未标注状态</span>'}
              </div>
            </div>
            <div class="rounded-full bg-white/90 px-3 py-1 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(formatDateTime(character.updated_at))}</div>
          </div>

          <div class="mt-5 grid gap-3 rounded-[1.25rem] bg-bamboo-mist/48 p-4">
            <div class="grid grid-cols-[5rem_minmax(0,1fr)] gap-2 text-sm">
              <div class="font-medium text-slate-400">年龄</div>
              <div class="text-slate-700">${escapeHtml(age)}</div>
            </div>
            <div class="grid grid-cols-[5rem_minmax(0,1fr)] gap-2 text-sm">
              <div class="font-medium text-slate-400">别名</div>
              <div class="text-slate-700">${escapeHtml(aliases)}</div>
            </div>
            <div class="grid grid-cols-[5rem_minmax(0,1fr)] gap-2 text-sm">
              <div class="font-medium text-slate-400">小传</div>
              <div class="text-slate-700">${escapeHtml(truncate(biography, 110))}</div>
            </div>
          </div>

          <div class="mt-5 flex justify-end">
            <button class="bamboo-btn-secondary" data-edit-character-card="${character.id}" type="button">编辑</button>
          </div>
        </article>
      `;
    })
    .join("");

  document.querySelectorAll("[data-edit-character-card]").forEach((button) => {
    button.addEventListener("click", () => {
      const character = state.characters.find((item) => item.id === Number(button.dataset.editCharacterCard));
      if (!character) return;
      openModal(character);
    });
  });
}

function renderStatusesEditor() {
  const container = document.getElementById("characters-statuses");
  if (!container) return;
  const selected = state.lifeStatuses || [];
  container.innerHTML = `
    <div class="flex flex-wrap gap-2">
      ${
        selected.length
          ? selected
              .map(
                (status) => `
                  <span class="inline-flex items-center gap-2 rounded-full bg-bamboo-highlight px-3 py-2 text-sm text-bamboo-deep">
                    <span>${escapeHtml(status)}</span>
                    <button class="text-slate-500 transition hover:text-rose-600" data-remove-character-status="${escapeHtml(status)}" type="button">删除</button>
                  </span>
                `
              )
              .join("")
          : '<div class="text-sm text-slate-500">还没有生命状态。</div>'
      }
    </div>
    <div class="mt-3 flex flex-wrap gap-2">
      ${PRESET_STATUSES.filter((status) => !selected.includes(status))
        .map(
          (status) =>
            `<button class="bamboo-btn-ghost px-3 py-2 text-sm" data-preset-character-status="${escapeHtml(status)}" type="button">${escapeHtml(status)}</button>`
        )
        .join("")}
    </div>
  `;
}

function renderTimelineEditor() {
  const container = document.getElementById("characters-timeline-list");
  if (!container) return;
  const entries = state.timelineDraft || [];
  if (!entries.length) {
    container.innerHTML =
      '<div class="rounded-[1rem] bg-bamboo-mist/55 p-4 text-sm text-slate-500">还没有章节时间节点，点上方按钮即可新增。</div>';
    return;
  }
  container.innerHTML = entries
    .map(
      (entry, index) => `
        <div class="rounded-[1rem] bg-white/84 p-4 shadow-cloud" data-character-timeline-row="${index}">
          <div class="flex items-center justify-between gap-3">
            <select class="bamboo-field" data-character-timeline-chapter="${index}">
              ${chapterOptions(entry.chapter_id)}
            </select>
            <button class="bamboo-btn-ghost text-rose-600" data-remove-character-timeline="${index}" type="button">删除</button>
          </div>
          <textarea class="bamboo-field mt-3 min-h-[5rem] resize-y" data-character-timeline-event="${index}" placeholder="这一章里他做了什么">${escapeHtml(entry.event || "")}</textarea>
          <div class="mt-3 grid gap-3 md:grid-cols-2">
            <input class="bamboo-field" data-character-timeline-location="${index}" placeholder="所在位置" value="${escapeHtml(entry.location || "")}" />
            <input class="bamboo-field" data-character-timeline-status="${index}" placeholder="当时状态" value="${escapeHtml(entry.status || "")}" />
          </div>
        </div>
      `
    )
    .join("");
}

function collectTimelineEntries() {
  return Array.from(document.querySelectorAll("[data-character-timeline-row]"))
    .map((row, index) => {
      const chapterId = Number(row.querySelector(`[data-character-timeline-chapter="${index}"]`)?.value || 0);
      const chapter = state.chapters.find((item) => item.id === chapterId);
      const event = String(row.querySelector(`[data-character-timeline-event="${index}"]`)?.value || "").trim();
      const location = String(row.querySelector(`[data-character-timeline-location="${index}"]`)?.value || "").trim();
      const status = String(row.querySelector(`[data-character-timeline-status="${index}"]`)?.value || "").trim();
      if (!chapterId || !chapter || (!event && !location && !status)) return null;
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

function openModal(character = null) {
  state.editingId = character?.id || null;
  state.lifeStatuses = characterStatuses(character);
  state.timelineDraft = characterTimelineEntries(character);

  document.getElementById("characters-modal-title").textContent = character ? `编辑人物：${character.name}` : "新增人物";
  document.getElementById("characters-name").value = character?.name || "";
  document.getElementById("characters-age").value = cardField(character, "age");
  document.getElementById("characters-aliases").value = (character?.aliases || []).join("、");
  document.getElementById("characters-short-goal").value = cardField(character, "short_term_goal");
  document.getElementById("characters-long-goal").value = cardField(character, "long_term_goal");
  document.getElementById("characters-motivation").value = cardField(character, "motivation");
  document.getElementById("characters-personality").value = cardField(character, "personality");
  document.getElementById("characters-appearance").value = cardField(character, "appearance");
  document.getElementById("characters-weakness").value = cardField(character, "weakness");
  document.getElementById("characters-secrets").value = character?.secrets || "";
  document.getElementById("characters-biography").value = character?.biography || character?.description || "";
  document.getElementById("characters-status-input").value = "";
  document.getElementById("characters-delete").classList.toggle("hidden", !character);
  renderStatusesEditor();
  renderTimelineEditor();
  document.getElementById("characters-modal").classList.remove("hidden");
  document.getElementById("characters-modal").classList.add("flex");
}

function closeModal() {
  document.getElementById("characters-modal").classList.add("hidden");
  document.getElementById("characters-modal").classList.remove("flex");
}

function addStatus(value) {
  const label = statusLabel(value);
  if (!label) return;
  if (!state.lifeStatuses.includes(label)) {
    state.lifeStatuses = [...state.lifeStatuses, label];
  }
  renderStatusesEditor();
}

async function refreshData() {
  if (!state.bookId) return;
  const [book, chaptersResponse, charactersResponse] = await Promise.all([
    getBook(state.bookId),
    listChapters(state.bookId),
    listCharacters(state.bookId),
  ]);
  state.book = book;
  state.chapters = chaptersResponse.items || [];
  state.characters = charactersResponse.items || [];
  renderStats();
  renderStatusFilterOptions();
  renderCharacterGrid();
}

async function submitCharacterForm(event) {
  event.preventDefault();
  const current = state.characters.find((item) => item.id === state.editingId) || null;
  state.timelineDraft = collectTimelineEntries();
  const baseCard = current?.card_json && typeof current.card_json === "object" ? { ...current.card_json } : {};
  const extraFields = {
    age: document.getElementById("characters-age").value.trim(),
    short_term_goal: document.getElementById("characters-short-goal").value.trim(),
    long_term_goal: document.getElementById("characters-long-goal").value.trim(),
    motivation: document.getElementById("characters-motivation").value.trim(),
    personality: document.getElementById("characters-personality").value.trim(),
    appearance: document.getElementById("characters-appearance").value.trim(),
    weakness: document.getElementById("characters-weakness").value.trim(),
  };
  Object.entries(extraFields).forEach(([key, value]) => {
    if (value) {
      baseCard[key] = value;
    } else {
      delete baseCard[key];
    }
  });

  const payload = {
    name: document.getElementById("characters-name").value.trim(),
    aliases: parseAliases(document.getElementById("characters-aliases").value),
    biography: document.getElementById("characters-biography").value.trim() || null,
    description: document.getElementById("characters-biography").value.trim() || null,
    secrets: document.getElementById("characters-secrets").value.trim() || null,
    life_statuses: state.lifeStatuses,
    timeline_entries: state.timelineDraft,
    card_json: {
      ...baseCard,
      life_statuses: state.lifeStatuses,
      timeline_entries: state.timelineDraft,
    },
  };

  try {
    if (state.editingId) {
      await updateCharacter(state.bookId, state.editingId, payload);
      showToast("人物卡已更新");
    } else {
      await createCharacter(state.bookId, payload);
      showToast("人物卡已创建");
    }
    closeModal();
    await refreshData();
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "保存人物卡失败";
    showToast(message, "error");
  }
}

async function deleteCurrentCharacter() {
  if (!state.editingId) return;
  if (!window.confirm("确认删除这个人物及其关联关系吗？")) return;
  try {
    await deleteCharacter(state.bookId, state.editingId);
    closeModal();
    showToast("人物卡已删除");
    await refreshData();
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "删除人物卡失败";
    showToast(message, "error");
  }
}

function bindEvents() {
  document.getElementById("characters-book-select").addEventListener("change", (event) => {
    window.location.href = `/characters?book=${event.currentTarget.value}`;
  });
  document.getElementById("characters-search").addEventListener("input", renderCharacterGrid);
  document.getElementById("characters-status-filter").addEventListener("change", renderCharacterGrid);
  document.getElementById("characters-sort").addEventListener("change", renderCharacterGrid);
  document.getElementById("characters-create").addEventListener("click", () => openModal(null));
  document.getElementById("characters-delete-all").addEventListener("click", async () => {
    if (!state.characters.length) {
      showToast("当前没有可删除的人物卡", "error");
      return;
    }
    if (!window.confirm("确认一键删除当前书籍的所有人物卡吗？关联关系也会一并清空。")) return;
    try {
      const result = await deleteAllCharacters(state.bookId);
      closeModal();
      showToast(`已删除 ${result.deleted_character_count || 0} 张人物卡，并清理 ${result.deleted_relation_count || 0} 条关系`);
      await refreshData();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "批量删除人物卡失败";
      showToast(message, "error");
    }
  });
  document.getElementById("characters-close").addEventListener("click", closeModal);
  document.getElementById("characters-cancel").addEventListener("click", closeModal);
  document.getElementById("characters-delete").addEventListener("click", deleteCurrentCharacter);
  document.getElementById("characters-form").addEventListener("submit", submitCharacterForm);
  document.getElementById("characters-modal").addEventListener("click", (event) => {
    if (event.target?.id === "characters-modal") {
      closeModal();
    }
  });
  document.getElementById("characters-status-add").addEventListener("click", () => {
    const input = document.getElementById("characters-status-input");
    const value = input.value.trim();
    if (!value) return;
    addStatus(value);
    input.value = "";
  });
  document.getElementById("characters-status-input").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const input = document.getElementById("characters-status-input");
    const value = input.value.trim();
    if (!value) return;
    addStatus(value);
    input.value = "";
  });
  document.getElementById("characters-statuses").addEventListener("click", (event) => {
    const presetButton = event.target instanceof HTMLElement ? event.target.closest("[data-preset-character-status]") : null;
    if (presetButton instanceof HTMLButtonElement) {
      addStatus(presetButton.dataset.presetCharacterStatus || "");
      return;
    }
    const removeButton = event.target instanceof HTMLElement ? event.target.closest("[data-remove-character-status]") : null;
    if (removeButton instanceof HTMLButtonElement) {
      const value = removeButton.dataset.removeCharacterStatus || "";
      state.lifeStatuses = state.lifeStatuses.filter((item) => item !== value);
      renderStatusesEditor();
    }
  });
  document.getElementById("characters-timeline-add").addEventListener("click", () => {
    state.timelineDraft = [...collectTimelineEntries(), {}];
    renderTimelineEditor();
  });
  document.getElementById("characters-timeline-list").addEventListener("click", (event) => {
    const removeButton = event.target instanceof HTMLElement ? event.target.closest("[data-remove-character-timeline]") : null;
    if (!(removeButton instanceof HTMLButtonElement)) return;
    const index = Number(removeButton.dataset.removeCharacterTimeline || -1);
    state.timelineDraft = collectTimelineEntries().filter((_item, itemIndex) => itemIndex !== index);
    renderTimelineEditor();
  });
}

export async function initCharactersPage() {
  state.books = (await listBooks()).items || [];
  state.bookId = selectedBookId(state.books);
  setAssistantContext({ bookId: state.bookId });

  const select = document.getElementById("characters-book-select");
  select.innerHTML = state.books.length
    ? state.books.map((book) => `<option value="${book.id}" ${book.id === state.bookId ? "selected" : ""}>${escapeHtml(book.title)}</option>`).join("")
    : '<option value="">暂无书籍</option>';

  bindEvents();

  if (!state.bookId) {
    document.getElementById("characters-summary").textContent = "当前还没有书籍，无法管理人物卡。";
    document.getElementById("characters-grid").innerHTML =
      '<div class="col-span-full rounded-[1.4rem] bg-white/80 p-6 text-sm text-slate-500 shadow-cloud">请先创建书籍。</div>';
    return;
  }

  await refreshData();
}
