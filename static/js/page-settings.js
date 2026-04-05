import {
  applyBookProjectArchiveImport,
  ApiError,
  changeOwnPassword,
  createAIConfig,
  deleteAIConfig,
  discoverAIModels,
  exportBookProjectArchive,
  getBook,
  isAdminRole,
  listAIConfigs,
  listBooks,
  previewBookProjectArchive,
  testAIConnection,
  updateAIConfig,
  updateBook,
} from "./api.js";
import {
  aiModuleLabel,
  aiProviderLabel,
  aiScopeLabel,
  escapeHtml,
  formatDateTime,
  setAssistantContext,
  showToast,
  truncate,
} from "./shell.js";
import {
  BOOK_STYLE_PRESET_SOURCE,
  composeBookStylePrompt,
  findBookStylePresetById,
  listBookStylePresetsByCategory,
  normalizeBookStylePresetIds,
} from "./style-presets.js";
import {
  isWriterGenerationEffectEnabled,
  setWriterGenerationEffectEnabled,
} from "./ui-preferences.js";

const MODULE_OPTIONS = [
  "assistant",
  "co_writing",
  "outline_expansion",
  "summary",
  "setting_extraction",
  "character_extraction",
  "relation_extraction",
  "reasoner",
];

const AUTO_DISCOVERY_DELAY_MS = 700;
const BOOK_STYLE_PRESET_DEFAULT_SUMMARY =
  "可多选，建议 1-3 项。上方负责管理预设组合，下方“最终写作要求”负责提交真正保存的文本。";
const PROJECT_IMPORT_DECISIONS = [
  { value: "smart_merge", label: "智能合并" },
  { value: "keep_existing", label: "保留系统内容" },
  { value: "replace_existing", label: "采用压缩包内容" },
];
const PROJECT_IMPORT_STATUS_LABELS = {
  new: "新增",
  conflict: "有冲突",
  same: "一致",
};

function projectImportDecisionLabel(value) {
  return PROJECT_IMPORT_DECISIONS.find((item) => item.value === value)?.label || value;
}

function projectImportStatusLabel(value) {
  return PROJECT_IMPORT_STATUS_LABELS[value] || value;
}

function renderProjectImportResult(result) {
  const report = result?.report;
  if (!report) {
    return "这里会显示本次导入导出的结果摘要。";
  }

  const modules = (report.modules_detected || []).length
    ? `识别模块：${(report.modules_detected || []).join("、")}`
    : "未识别到可导入模块";
  const lines = [
    modules,
    `AI 配置：新增 ${report.ai_configs?.created || 0}，更新 ${report.ai_configs?.updated || 0}，跳过 ${report.ai_configs?.skipped || 0}`,
    `章节：新增 ${report.chapters?.created || 0}，更新 ${report.chapters?.updated || 0}，跳过 ${report.chapters?.skipped || 0}`,
    `人物卡：新增 ${report.characters?.created || 0}，更新 ${report.characters?.updated || 0}，跳过 ${report.characters?.skipped || 0}`,
    `人物关系：新增 ${report.relations?.created || 0}，更新 ${report.relations?.updated || 0}，跳过 ${report.relations?.skipped || 0}`,
    report.book?.updated ? "书籍提示词/书籍信息：已更新" : "书籍提示词/书籍信息：未改动",
  ];
  if ((report.warnings || []).length) {
    lines.push(`警告：${report.warnings.slice(0, 5).join("；")}`);
  }
  return lines.join("\n");
}

function renderProjectImportPreviewSummary(preview) {
  if (!preview) {
    return "这里会显示导入预览结果。";
  }
  const modules = (preview.modules_detected || []).length
    ? `识别模块：${(preview.modules_detected || []).join("、")}`
    : "未识别到可导入模块";
  const counts = preview.counts || {};
  const lines = [
    modules,
    `共 ${counts.total || 0} 项：新增 ${counts.new || 0}，有冲突 ${counts.conflict || 0}，一致 ${counts.same || 0}`,
  ];
  if ((preview.warnings || []).length) {
    lines.push(`提示：${preview.warnings.slice(0, 5).join("；")}`);
  }
  return lines.join("\n");
}

function renderProjectImportPreviewItems(preview, defaultDecision) {
  const items = preview?.items || [];
  if (!items.length) {
    return '<div class="rounded-[1.2rem] bg-white/80 p-5 text-sm text-slate-500 shadow-cloud">压缩包里没有可导入的项目。</div>';
  }

  return items
    .map((item) => {
      const decision = item.recommended_decision || defaultDecision || "smart_merge";
      const statusClass =
        item.status === "conflict"
          ? "bg-amber-100 text-amber-700"
          : item.status === "new"
            ? "bg-emerald-100 text-emerald-700"
            : "bg-slate-200 text-slate-600";
      const changedFields = (item.changed_fields || []).length
        ? `
          <div class="mt-4 space-y-2">
            ${(item.changed_fields || [])
              .map(
                (field) => `
                  <div class="rounded-2xl bg-bamboo-mist/60 p-3">
                    <div class="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">${escapeHtml(field.label || "字段")}</div>
                    <div class="mt-2 grid gap-2 md:grid-cols-2">
                      <div>
                        <div class="text-[11px] uppercase tracking-[0.18em] text-slate-400">系统内</div>
                        <div class="mt-1 text-sm leading-6 text-slate-600">${escapeHtml(field.current || "（空）")}</div>
                      </div>
                      <div>
                        <div class="text-[11px] uppercase tracking-[0.18em] text-slate-400">压缩包</div>
                        <div class="mt-1 text-sm leading-6 text-slate-600">${escapeHtml(field.incoming || "（空）")}</div>
                      </div>
                    </div>
                  </div>
                `
              )
              .join("")}
          </div>
        `
        : '<div class="mt-4 rounded-2xl bg-bamboo-mist/55 p-3 text-sm text-slate-500">这一项和系统内容一致，没有发现差异。</div>';

      return `
        <article class="rounded-[1.5rem] bg-white/92 p-5 shadow-cloud">
          <div class="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <span class="inline-flex rounded-full px-3 py-1 text-xs font-semibold ${statusClass}">${escapeHtml(projectImportStatusLabel(item.status))}</span>
                <span class="text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(item.module || "")}</span>
              </div>
              <h4 class="mt-3 text-lg font-semibold text-bamboo-deep">${escapeHtml(item.title || "导入项")}</h4>
              <p class="mt-2 text-sm leading-6 text-slate-500">${escapeHtml(item.description || "")}</p>
            </div>
            <label class="block min-w-[220px]">
              <span class="bamboo-mini-label">导入决策</span>
              <select class="bamboo-field mt-2" data-project-import-decision="${escapeHtml(item.conflict_id)}">
                ${PROJECT_IMPORT_DECISIONS.map(
                  (option) =>
                    `<option value="${escapeHtml(option.value)}" ${option.value === decision ? "selected" : ""}>${escapeHtml(option.label)}</option>`
                ).join("")}
              </select>
            </label>
          </div>
          ${changedFields}
        </article>
      `;
    })
    .join("");
}

function renderBookStylePresetOptions(selectedIds = []) {
  const selected = new Set(normalizeBookStylePresetIds(selectedIds));
  return listBookStylePresetsByCategory()
    .map(
      (category) => `
        <optgroup label="${escapeHtml(category.name)}">
          ${category.items
            .map(
              (item) =>
                `<option value="${escapeHtml(item.id)}" ${selected.has(item.id) ? "selected" : ""}>${escapeHtml(item.name)}</option>`
            )
            .join("")}
        </optgroup>
      `
    )
    .join("");
}

function readSelectedBookStylePresetIds() {
  const select = document.getElementById("settings-book-style-preset");
  if (!select) return [];
  return normalizeBookStylePresetIds(Array.from(select.selectedOptions || []).map((option) => option.value));
}

function setSelectedBookStylePresetIds(ids) {
  const selected = new Set(normalizeBookStylePresetIds(ids));
  const select = document.getElementById("settings-book-style-preset");
  if (!select) return;
  Array.from(select.options || []).forEach((option) => {
    option.selected = selected.has(option.value);
  });
}

function buildBookStyleExtraData(state, presetIds, customPrompt) {
  const extraData = { ...(state.bookExtraData || {}) };
  if (presetIds.length) {
    extraData.style_preset_ids = presetIds;
    extraData.style_preset_source = BOOK_STYLE_PRESET_SOURCE;
  } else {
    delete extraData.style_preset_ids;
    delete extraData.style_preset_source;
  }

  if (customPrompt) {
    extraData.style_custom_prompt = customPrompt;
  } else {
    delete extraData.style_custom_prompt;
  }

  return extraData;
}

function previewWasCustomized(state) {
  const preview = document.getElementById("settings-book-style");
  return String(preview?.value || "").trim() !== String(state.lastComposedStylePrompt || "").trim();
}

function updateBookStylePresetSummary(state) {
  const element = document.getElementById("settings-book-style-preset-summary");
  if (!element) return;

  const selectedIds = readSelectedBookStylePresetIds();
  const customPrompt = String(document.getElementById("settings-book-style-custom")?.value || "").trim();
  const finalPrompt = String(document.getElementById("settings-book-style")?.value || "").trim();
  const selectedLabels = selectedIds
    .map((id) => findBookStylePresetById(id))
    .filter(Boolean)
    .map((item) => item.name);

  if (!selectedLabels.length && !customPrompt && !finalPrompt) {
    element.textContent = BOOK_STYLE_PRESET_DEFAULT_SUMMARY;
    return;
  }

  const messages = [];
  if (selectedLabels.length) {
    messages.push(`已选 ${selectedLabels.length} 项：${selectedLabels.join("、")}。`);
  } else {
    messages.push("当前未选择预设，可只使用自定义要求。");
  }
  if (customPrompt) {
    messages.push("已填写自定义追加要求。");
  }
  messages.push(
    previewWasCustomized(state)
      ? "下方“最终写作要求”已手动修改；保存时将以该文本为准。"
      : "下方“最终写作要求”会跟随当前选择自动更新，也可以继续手动微调。"
  );
  element.textContent = messages.join(" ");
}

function composeCurrentBookStylePrompt() {
  return composeBookStylePrompt(
    readSelectedBookStylePresetIds(),
    document.getElementById("settings-book-style-custom")?.value || ""
  );
}

function syncBookStylePreview(state, { force = false } = {}) {
  const textarea = document.getElementById("settings-book-style");
  if (!textarea) return;

  const composedPrompt = composeCurrentBookStylePrompt();
  const canOverwrite = force || !String(textarea.value || "").trim() || !previewWasCustomized(state);
  state.lastComposedStylePrompt = composedPrompt;

  if (canOverwrite) {
    textarea.value = composedPrompt;
  }
  updateBookStylePresetSummary(state);
}

function applyDerivedBookStyleState(book) {
  const textarea = document.getElementById("settings-book-style-derived");
  const meta = document.getElementById("settings-book-style-derived-meta");
  if (!textarea || !meta) return;

  const content = String(book?.derived_style_summary || "").trim();
  const updatedAt = String(book?.derived_style_summary_updated_at || "").trim();
  textarea.value = content;
  meta.textContent = content
    ? `自动总结文风仅供作者手动参考。最近更新时间：${formatDateTime(updatedAt) || "未知时间"}`
    : "暂无自动总结文风。";
}

function applyBookStyleState(state, book) {
  const extraData = book?.extra_data && typeof book.extra_data === "object" ? book.extra_data : {};
  state.bookExtraData = { ...extraData };

  let presetIds = normalizeBookStylePresetIds(extraData.style_preset_ids);
  let customPrompt = typeof extraData.style_custom_prompt === "string" ? extraData.style_custom_prompt : "";
  const savedStylePrompt = String(book?.global_style_prompt || "").trim();

  if (!presetIds.length && !customPrompt.trim() && savedStylePrompt) {
    customPrompt = savedStylePrompt;
  }

  document.getElementById("settings-book-style-preset").innerHTML = renderBookStylePresetOptions(presetIds);
  setSelectedBookStylePresetIds(presetIds);
  document.getElementById("settings-book-style-custom").value = customPrompt;

  const composedPrompt = composeBookStylePrompt(presetIds, customPrompt);
  state.lastComposedStylePrompt = composedPrompt;
  document.getElementById("settings-book-style").value = savedStylePrompt || composedPrompt;
  updateBookStylePresetSummary(state);
  applyDerivedBookStyleState(book);
}

function currentBookId(books) {
  const params = new URLSearchParams(window.location.search);
  const bookId = Number(params.get("book"));
  if (bookId && books.some((book) => book.id === bookId)) return bookId;
  return books[0]?.id || null;
}

function scopeText(config) {
  if (config.scope === "book") {
    return config.book_title ? `当前书籍：${config.book_title}` : "当前书籍";
  }
  return aiScopeLabel(config.scope);
}

function renderConfigCard(config, { canManage = true } = {}) {
  const apiKeyState = config.has_api_key ? "API Key 已配置" : "API Key 未配置";
  return `
    <div class="rounded-[1.25rem] bg-white/85 p-5 shadow-cloud">
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="font-medium text-bamboo-deep">${escapeHtml(config.name)}</div>
          <div class="mt-1 text-sm text-slate-500">${escapeHtml(aiModuleLabel(config.module))} · ${escapeHtml(scopeText(config))}</div>
        </div>
        <span class="bamboo-chip">优先级 ${escapeHtml(config.priority)}</span>
      </div>
      <div class="mt-4 grid gap-3 md:grid-cols-2">
        <div>
          <div class="bamboo-mini-label">模型名称</div>
          <div class="mt-2 text-sm text-slate-600">${escapeHtml(config.model_name || "未设置")}</div>
        </div>
        <div>
          <div class="bamboo-mini-label">API 接口地址</div>
          <div class="mt-2 text-sm text-slate-600">${escapeHtml(truncate(config.base_url || "未设置", 42))}</div>
        </div>
      </div>
      <div class="mt-3 flex flex-wrap gap-3 text-sm text-slate-500">
        <span>服务来源：${escapeHtml(aiProviderLabel(config))}</span>
        <span>${escapeHtml(apiKeyState)}</span>
      </div>
      <div class="mt-4 flex flex-wrap items-center gap-3 text-xs uppercase tracking-[0.16em] text-slate-400">
        <span>${config.is_enabled ? "已启用" : "已停用"}</span>
        <span>${config.is_default ? "默认配置" : "备用配置"}</span>
        <span>${escapeHtml(formatDateTime(config.updated_at))}</span>
      </div>
      ${
        canManage
          ? `
      <div class="mt-4 flex gap-3">
        <button class="bamboo-btn-secondary" data-edit-config="${config.id}" type="button">编辑</button>
        <button class="bamboo-btn-ghost text-rose-600" data-delete-config="${config.id}" type="button">删除</button>
      </div>
      `
          : `
      <div class="mt-4 text-xs uppercase tracking-[0.16em] text-slate-400">系统级配置仅管理员可编辑</div>
      `
      }
    </div>
  `;
}

function setHint(text, tone = "default") {
  const element = document.getElementById("config-model-hint");
  element.textContent = text;
  element.className = "text-xs leading-6";
  element.classList.add(tone === "error" ? "text-rose-600" : "text-slate-500");
}

function setManualModelInput({ visible, value = "" }) {
  const input = document.getElementById("config-model-name-manual");
  if (!input) return;
  input.classList.toggle("hidden", !visible);
  if (visible) {
    input.value = String(value || "").trim();
    return;
  }
  input.value = "";
}

function setApiKeyVisibility(visible) {
  const input = document.getElementById("config-api-key");
  const button = document.getElementById("config-toggle-api-key");
  if (!input || !button) return;
  input.type = visible ? "text" : "password";
  button.textContent = visible ? "隐藏" : "显示";
  button.dataset.visible = visible ? "true" : "false";
}

function syncApiKeyHelpText({ editing = false, hasSavedKey = false } = {}) {
  const input = document.getElementById("config-api-key");
  const note = document.getElementById("config-api-key-note");
  if (!input || !note) return;

  if (editing) {
    input.placeholder = hasSavedKey
      ? "已有 API Key 已保存；留空表示不修改，填写新值会覆盖"
      : "当前配置还没有 API Key，请在这里补充";
    note.textContent = hasSavedKey
      ? "当前配置已经保存过 API Key。留空不会覆盖，勾选下方“清空当前已保存的 API Key”会删除它。"
      : "当前配置还没有保存 API Key。填写后会加密存储。";
    return;
  }

  input.placeholder = "请输入当前服务商 API Key";
  note.textContent = "API Key 会加密保存，新建模型配置时建议直接填写。";
}

function setModelOptions(
  items,
  selectedValue = "",
  placeholder = "请先填写接口地址",
  { manualInputVisible = items.length === 0 } = {}
) {
  const select = document.getElementById("config-model-name");
  const normalizedSelected = String(selectedValue || "").trim();
  let hasSelected = false;

  const options = [`<option value="">${escapeHtml(placeholder)}</option>`];
  for (const item of items) {
    const modelId = String(item.id || "").trim();
    if (!modelId) continue;
    const selected = modelId === normalizedSelected;
    if (selected) hasSelected = true;
    options.push(
      `<option value="${escapeHtml(modelId)}" ${selected ? "selected" : ""}>${escapeHtml(item.label || modelId)}</option>`
    );
  }

  if (normalizedSelected && !hasSelected) {
    options.push(
      `<option value="${escapeHtml(normalizedSelected)}" selected>当前值：${escapeHtml(normalizedSelected)}</option>`
    );
  }

  select.innerHTML = options.join("");
  select.dataset.currentValue = normalizedSelected;
  select.disabled = items.length === 0;
  setManualModelInput({
    visible: manualInputVisible,
    value: normalizedSelected,
  });
}

function setButtonLoading(buttonId, isLoading, loadingText, defaultText) {
  const button = document.getElementById(buttonId);
  if (!button) return;
  button.disabled = isLoading;
  button.textContent = isLoading ? loadingText : defaultText;
}

function readConnectionPayload(state) {
  return {
    config_id: state.editingConfigId || null,
    base_url: document.getElementById("config-base-url").value.trim() || null,
    api_key: document.getElementById("config-api-key").value.trim() || null,
    timeout_seconds: Number(document.getElementById("config-timeout").value || 30),
  };
}

function hasUsableBaseUrl(state) {
  const baseUrl = document.getElementById("config-base-url").value.trim();
  return Boolean(baseUrl) || Boolean(state.editingConfigId);
}

function shouldAutoDiscover(state) {
  const baseUrl = document.getElementById("config-base-url").value.trim();
  return state.editingConfigId || /^https?:\/\//i.test(baseUrl);
}

function readConfiguredModelName() {
  const manualValue = document.getElementById("config-model-name-manual")?.value.trim() || "";
  const selectedValue = document.getElementById("config-model-name")?.value.trim() || "";
  const cachedValue = document.getElementById("config-model-name")?.dataset.currentValue?.trim() || "";
  return manualValue || selectedValue || cachedValue;
}

export async function initSettingsPage(user) {
  const books = (await listBooks()).items || [];
  const bookId = currentBookId(books);
  setAssistantContext({ bookId });
  const bookSelect = document.getElementById("settings-book-select");
  const configScope = document.getElementById("config-scope");
  const configModule = document.getElementById("config-module");
  const canAdmin = isAdminRole(user.role);
  const state = {
    editingConfigId: null,
    configs: [],
    discoveryToken: 0,
    autoDiscoveryTimer: null,
    bookExtraData: {},
    lastComposedStylePrompt: "",
    projectImportPreview: null,
    projectImportSessionId: null,
  };

  bookSelect.innerHTML = books.length
    ? books
        .map((book) => `<option value="${book.id}" ${book.id === bookId ? "selected" : ""}>${escapeHtml(book.title)}</option>`)
        .join("")
    : '<option value="">暂无书籍</option>';

  configScope.innerHTML = [
    `<option value="user">${aiScopeLabel("user")}</option>`,
    `<option value="book">${aiScopeLabel("book")}</option>`,
    canAdmin ? `<option value="system">${aiScopeLabel("system")}</option>` : "",
  ].join("");

  configModule.innerHTML = MODULE_OPTIONS.map((item) => `<option value="${item}">${escapeHtml(aiModuleLabel(item))}</option>`).join("");
  document.getElementById("settings-book-style-preset").innerHTML = renderBookStylePresetOptions();
  updateBookStylePresetSummary(state);

  setModelOptions([], "", "请先填写接口地址，系统会自动读取模型列表", { manualInputVisible: false });
  setHint("填写接口地址后，系统会自动读取模型列表。若服务商不提供模型列表，也可以直接手填模型名称。");
  setApiKeyVisibility(false);
  syncApiKeyHelpText();
  document.getElementById("settings-writer-generation-effect").checked = isWriterGenerationEffectEnabled();

  if (books.length && bookId) {
    const book = await getBook(bookId);
    document.getElementById("settings-book-title").value = book.title || "";
    document.getElementById("settings-book-genre").value = book.genre || "";
    document.getElementById("settings-book-description").value = book.description || "";
    applyBookStyleState(state, book);
    document.getElementById("settings-book-summary").value = book.long_term_summary || "";
    document.getElementById("settings-book-world").value = book.world_bible || "";
  } else {
    applyBookStyleState(
      state,
      {
        global_style_prompt: "",
        extra_data: {},
      }
    );
  }

  document.getElementById("settings-project-import-file")?.addEventListener("change", (event) => {
    const file = event.currentTarget.files?.[0];
    document.getElementById("settings-project-import-note").textContent = file
      ? `已选择：${file.name}`
      : "未选择 ZIP 压缩包。";
  });

  function closeProjectImportModal() {
    const modal = document.getElementById("settings-project-import-modal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.classList.remove("flex");
  }

  function openProjectImportModal() {
    const modal = document.getElementById("settings-project-import-modal");
    if (!modal) return;
    modal.classList.remove("hidden");
    modal.classList.add("flex");
  }

  function renderProjectImportModal() {
    const summary = document.getElementById("settings-project-import-modal-summary");
    const list = document.getElementById("settings-project-import-modal-list");
    if (summary) {
      summary.textContent = renderProjectImportPreviewSummary(state.projectImportPreview);
    }
    if (list) {
      list.innerHTML = renderProjectImportPreviewItems(
        state.projectImportPreview,
        document.getElementById("settings-project-import-strategy")?.value || "smart_merge"
      );
    }
  }

  function readProjectImportDecisions() {
    return Object.fromEntries(
      Array.from(document.querySelectorAll("[data-project-import-decision]"))
        .map((select) => [select.dataset.projectImportDecision, select.value])
        .filter(([key]) => Boolean(key))
    );
  }

  function visibleConfigs(configs) {
    return configs.filter((config) => config.scope !== "book" || !bookId || config.book_id === bookId);
  }

  async function refreshModelOptions({ silent = false } = {}) {
    const token = ++state.discoveryToken;
    const select = document.getElementById("config-model-name");
    const currentValue = select.value || select.dataset.currentValue || "";

    if (!hasUsableBaseUrl(state)) {
      setModelOptions([], currentValue, "请先填写接口地址，系统会自动读取模型列表", {
        manualInputVisible: false,
      });
      setHint("填写接口地址后，系统会自动读取模型列表。若服务商不提供模型列表，也可以直接手填模型名称。");
      return;
    }

    setButtonLoading("config-refresh-models", true, "读取中...", "获取模型列表");
    setHint("正在从服务商读取模型列表...");

    try {
      const response = await discoverAIModels(readConnectionPayload(state));
      if (token !== state.discoveryToken) return;

      const items = response.items || [];
      setModelOptions(items, currentValue, items.length ? "请选择模型" : "服务商未返回可用模型", {
        manualInputVisible: items.length === 0,
      });
      setHint(
        items.length
          ? `已获取 ${response.count} 个模型`
          : "接口已连通，但服务商没有返回可用模型。你可以直接手填模型名称。"
      );
      if (!silent) showToast("模型列表已刷新");
    } catch (error) {
      if (token !== state.discoveryToken) return;

      const message = error instanceof ApiError ? error.message : "获取模型列表失败";
      setModelOptions([], currentValue, currentValue ? "保留当前模型" : "获取失败，请检查接口地址或 API Key", {
        manualInputVisible: true,
      });
      setHint(`${message}。如果服务商本身不提供模型列表，也可以直接手填模型名称。`, "error");
      if (!silent) showToast(message, "error");
    } finally {
      if (token === state.discoveryToken) {
        setButtonLoading("config-refresh-models", false, "读取中...", "获取模型列表");
      }
    }
  }

  async function runConnectionTest() {
    if (!hasUsableBaseUrl(state)) {
      showToast("请先填写接口地址", "error");
      return;
    }

    setButtonLoading("config-test-connection", true, "测试中...", "测试连接");
    setHint("正在测试接口连接...");

    try {
      const result = await testAIConnection(readConnectionPayload(state));
      const sample = (result.sample_models || []).slice(0, 3).join("、");
      setHint(
        result.model_count
          ? `连接成功，发现 ${result.model_count} 个模型${sample ? `，例如：${sample}` : ""}`
          : "连接成功，但服务商没有返回可用模型。你可以直接手填模型名称。"
      );
      showToast("连接测试成功");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "连接测试失败";
      setHint(message, "error");
      showToast(message, "error");
    } finally {
      setButtonLoading("config-test-connection", false, "测试中...", "测试连接");
    }
  }

  function queueAutoModelDiscovery() {
    window.clearTimeout(state.autoDiscoveryTimer);
    if (!shouldAutoDiscover(state)) {
      if (!hasUsableBaseUrl(state)) {
        setHint("填写接口地址后，系统会自动读取模型列表。若服务商不提供模型列表，也可以直接手填模型名称。");
      }
      return;
    }

    setHint("检测到配置变化，稍后会自动读取模型列表...");
    state.autoDiscoveryTimer = window.setTimeout(() => {
      refreshModelOptions({ silent: true });
    }, AUTO_DISCOVERY_DELAY_MS);
  }

  function resetModelDiscoveryUI() {
    state.discoveryToken += 1;
    window.clearTimeout(state.autoDiscoveryTimer);
    setButtonLoading("config-refresh-models", false, "读取中...", "获取模型列表");
    setButtonLoading("config-test-connection", false, "测试中...", "测试连接");
    setModelOptions([], "", "请先填写接口地址，系统会自动读取模型列表", { manualInputVisible: false });
    setHint("填写接口地址后，系统会自动读取模型列表。若服务商不提供模型列表，也可以直接手填模型名称。");
  }

  async function refreshConfigs() {
    const response = await listAIConfigs(bookId ? { book_id: bookId } : {});
    state.configs = response.items || [];
    document.getElementById("settings-ai-configs").innerHTML = visibleConfigs(state.configs).length
      ? visibleConfigs(state.configs)
          .map((config) =>
            renderConfigCard(config, {
              canManage: canAdmin || config.scope !== "system",
            })
          )
          .join("")
      : '<div class="rounded-[1.2rem] bg-white/80 p-5 text-sm text-slate-500 shadow-cloud">暂无模型配置。</div>';

    document.querySelectorAll("[data-edit-config]").forEach((button) => {
      button.addEventListener("click", async () => {
        const config = state.configs.find((item) => item.id === Number(button.dataset.editConfig));
        if (!config) return;

        state.editingConfigId = config.id;
        document.getElementById("config-form-title").textContent = `编辑模型配置 #${config.id}`;
        document.getElementById("config-name").value = config.name || "";
        document.getElementById("config-scope").value = config.scope;
        document.getElementById("config-module").value = config.module;
        document.getElementById("config-provider").value = config.provider_name || "";
        document.getElementById("config-base-url").value = config.base_url || "";
        document.getElementById("config-api-key").value = "";
        setApiKeyVisibility(false);
        syncApiKeyHelpText({ editing: true, hasSavedKey: Boolean(config.has_api_key) });
        document.getElementById("config-reasoning").value = config.reasoning_effort || "";
        document.getElementById("config-timeout").value = config.timeout_seconds || 120;
        document.getElementById("config-priority").value = config.priority || 100;
        document.getElementById("config-enabled").checked = Boolean(config.is_enabled);
        document.getElementById("config-default").checked = Boolean(config.is_default);
        document.getElementById("config-system-prompt").value = config.system_prompt_template || "";
        document.getElementById("config-notes").value = config.notes || "";
        document.getElementById("config-clear-api-key").checked = false;
        setModelOptions([], config.model_name || "", config.model_name ? "当前已保存模型" : "正在读取模型列表");
        setHint(config.has_api_key ? "将优先使用已保存的 API Key 自动读取模型列表。" : "正在自动读取模型列表。");
        await refreshModelOptions({ silent: true });
      });
    });

    document.querySelectorAll("[data-delete-config]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!window.confirm("确认删除这条模型配置吗？")) return;
        try {
          await deleteAIConfig(Number(button.dataset.deleteConfig));
          showToast("模型配置已删除");
          await refreshConfigs();
        } catch (error) {
          const message = error instanceof ApiError ? error.message : "删除模型配置失败";
          showToast(message, "error");
        }
      });
    });
  }

  await refreshConfigs();

  async function refreshCurrentBookPanel() {
    if (!bookId) return;
    const book = await getBook(bookId);
    document.getElementById("settings-book-title").value = book.title || "";
    document.getElementById("settings-book-genre").value = book.genre || "";
    document.getElementById("settings-book-description").value = book.description || "";
    applyBookStyleState(state, book);
    document.getElementById("settings-book-summary").value = book.long_term_summary || "";
    document.getElementById("settings-book-world").value = book.world_bible || "";
    const selectedOption = Array.from(bookSelect.options || []).find((option) => Number(option.value) === bookId);
    if (selectedOption) {
      selectedOption.textContent = book.title || selectedOption.textContent;
    }
  }

  bookSelect.addEventListener("change", () => {
    window.location.href = `/settings?book=${bookSelect.value}`;
  });

  document.getElementById("settings-book-style-preset").addEventListener("change", () => {
    syncBookStylePreview(state);
  });

  document.getElementById("settings-book-style-custom").addEventListener("input", () => {
    syncBookStylePreview(state);
  });

  document.getElementById("settings-book-style").addEventListener("input", () => {
    updateBookStylePresetSummary(state);
  });

  document.getElementById("settings-book-style-rebuild").addEventListener("click", () => {
    const shouldOverwrite =
      !previewWasCustomized(state) ||
      window.confirm("这会按当前预设与自定义重建下方“最终写作要求”，覆盖你手动改过的内容。是否继续？");
    if (!shouldOverwrite) return;
    syncBookStylePreview(state, { force: true });
  });

  document.getElementById("settings-book-style-clear").addEventListener("click", () => {
    if (!window.confirm("确认清空当前文风预设和自定义要求吗？")) return;
    setSelectedBookStylePresetIds([]);
    document.getElementById("settings-book-style-custom").value = "";
    state.lastComposedStylePrompt = "";
    document.getElementById("settings-book-style").value = "";
    updateBookStylePresetSummary(state);
  });

  document.getElementById("settings-book-style-derived-copy").addEventListener("click", async () => {
    const derivedText = String(document.getElementById("settings-book-style-derived").value || "").trim();
    if (!derivedText) {
      showToast("当前还没有可复制的自动总结文风。", "error");
      return;
    }
    const target = document.getElementById("settings-book-style");
    target.value = derivedText;
    updateBookStylePresetSummary(state);
    try {
      await navigator.clipboard.writeText(derivedText);
      showToast("已复制到最终写作要求，并同步写入剪贴板。");
    } catch {
      showToast("已复制到最终写作要求。");
    }
  });

  document.getElementById("settings-writer-generation-effect").addEventListener("change", (event) => {
    const enabled = Boolean(event.currentTarget.checked);
    setWriterGenerationEffectEnabled(enabled);
    showToast(enabled ? "已开启全屏竹叶特效" : "已关闭全屏竹叶特效");
  });

  document.getElementById("config-model-name").addEventListener("change", (event) => {
    event.currentTarget.dataset.currentValue = event.currentTarget.value;
  });

  document.getElementById("config-model-name-manual").addEventListener("input", (event) => {
    const value = event.currentTarget.value.trim();
    const select = document.getElementById("config-model-name");
    if (select) {
      select.dataset.currentValue = value;
    }
  });

  document.getElementById("config-toggle-api-key").addEventListener("click", () => {
    const button = document.getElementById("config-toggle-api-key");
    const visible = button?.dataset.visible === "true";
    setApiKeyVisibility(!visible);
  });

  document.getElementById("config-refresh-models").addEventListener("click", async () => {
    await refreshModelOptions();
  });

  document.getElementById("config-test-connection").addEventListener("click", async () => {
    await runConnectionTest();
  });

  ["config-base-url", "config-api-key", "config-timeout"].forEach((id) => {
    document.getElementById(id).addEventListener("input", queueAutoModelDiscovery);
  });

  document.getElementById("settings-book-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!bookId) {
      showToast("请先创建书籍", "error");
      return;
    }

    try {
      const presetIds = readSelectedBookStylePresetIds();
      const customPrompt = document.getElementById("settings-book-style-custom").value.trim();
      const updatedBook = await updateBook(bookId, {
        title: document.getElementById("settings-book-title").value.trim(),
        genre: document.getElementById("settings-book-genre").value.trim() || null,
        description: document.getElementById("settings-book-description").value.trim() || null,
        global_style_prompt: document.getElementById("settings-book-style").value,
        long_term_summary: document.getElementById("settings-book-summary").value,
        world_bible: document.getElementById("settings-book-world").value,
        extra_data: buildBookStyleExtraData(state, presetIds, customPrompt),
      });
      state.bookExtraData =
        updatedBook?.extra_data && typeof updatedBook.extra_data === "object" ? updatedBook.extra_data : {};
      state.lastComposedStylePrompt = composeBookStylePrompt(presetIds, customPrompt);
      updateBookStylePresetSummary(state);
      const selectedOption = Array.from(bookSelect.options || []).find((option) => Number(option.value) === bookId);
      if (selectedOption) {
        selectedOption.textContent = updatedBook.title || selectedOption.textContent;
      }
      window.dispatchEvent(
        new CustomEvent("bamboo:book-renamed", {
          detail: {
            bookId,
            title: updatedBook.title || document.getElementById("settings-book-title").value.trim(),
          },
        })
      );
      showToast("书籍写作记忆已保存");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "保存书籍写作记忆失败";
      showToast(message, "error");
    }
  });

  document.getElementById("settings-project-export")?.addEventListener("click", async () => {
    if (!bookId) {
      showToast("请先选择书籍", "error");
      return;
    }
    try {
      const blob = await exportBookProjectArchive(bookId);
      const currentTitle = bookSelect.options[bookSelect.selectedIndex]?.textContent?.trim() || `book-${bookId}`;
      const link = document.createElement("a");
      const objectUrl = window.URL.createObjectURL(blob);
      link.href = objectUrl;
      link.download = `${currentTitle}-project-0.2.0.zip`;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      window.setTimeout(() => {
        window.URL.revokeObjectURL(objectUrl);
        link.remove();
      }, 1000);
      document.getElementById("settings-project-import-result").textContent = "当前书籍工程包已导出。";
      showToast("工程包 ZIP 已导出");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "导出工程包失败";
      showToast(message, "error");
      document.getElementById("settings-project-import-result").textContent = message;
    }
  });

  document.getElementById("settings-project-import")?.addEventListener("click", async () => {
    if (!bookId) {
      showToast("请先选择书籍", "error");
      return;
    }
    const input = document.getElementById("settings-project-import-file");
    const file = input?.files?.[0];
    if (!file) {
      showToast("请先选择 ZIP 压缩包", "error");
      return;
    }
    try {
      const previewResponse = await previewBookProjectArchive(bookId, file);
      state.projectImportPreview = previewResponse.preview || null;
      state.projectImportSessionId = previewResponse.session_id || null;
      document.getElementById("settings-project-import-result").textContent = renderProjectImportPreviewSummary(
        state.projectImportPreview
      );
      renderProjectImportModal();
      openProjectImportModal();
      showToast("请先确认每一项的导入策略");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "导入工程包失败";
      showToast(message, "error");
      document.getElementById("settings-project-import-result").textContent = message;
    }
  });

  document.getElementById("settings-project-import-modal-close")?.addEventListener("click", () => {
    closeProjectImportModal();
  });

  document.getElementById("settings-project-import-modal-cancel")?.addEventListener("click", () => {
    closeProjectImportModal();
  });

  document.getElementById("settings-project-import-modal-apply")?.addEventListener("click", async () => {
    if (!bookId || !state.projectImportSessionId) {
      showToast("当前没有可提交的导入会话", "error");
      return;
    }
    const applyButton = document.getElementById("settings-project-import-modal-apply");
    applyButton.disabled = true;
    try {
      const result = await applyBookProjectArchiveImport(bookId, {
        session_id: state.projectImportSessionId,
        merge_strategy: document.getElementById("settings-project-import-strategy").value,
        decisions: readProjectImportDecisions(),
      });
      document.getElementById("settings-project-import-result").textContent = renderProjectImportResult(result);
      state.projectImportPreview = null;
      state.projectImportSessionId = null;
      closeProjectImportModal();
      showToast("工程包导入完成");
      await refreshCurrentBookPanel();
      await refreshConfigs();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "应用导入结果失败";
      showToast(message, "error");
      document.getElementById("settings-project-import-result").textContent = message;
    } finally {
      applyButton.disabled = false;
    }
  });

  document.getElementById("settings-project-import-modal")?.addEventListener("click", (event) => {
    if (event.target?.id === "settings-project-import-modal") {
      closeProjectImportModal();
    }
  });

  document.getElementById("config-reset").addEventListener("click", () => {
    state.editingConfigId = null;
    document.getElementById("config-form-title").textContent = "新增模型配置";
    document.getElementById("config-form").reset();
    document.getElementById("config-scope").value = "user";
    document.getElementById("config-timeout").value = 120;
    document.getElementById("config-priority").value = 100;
    document.getElementById("config-enabled").checked = true;
    setApiKeyVisibility(false);
    syncApiKeyHelpText();
    resetModelDiscoveryUI();
  });

  document.getElementById("config-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const scope = document.getElementById("config-scope").value;
    const modelName = readConfiguredModelName();

    if (!modelName) {
      showToast("请先选择模型，或在下方直接手填模型名称", "error");
      return;
    }

    const payload = {
      name: document.getElementById("config-name").value.trim(),
      scope,
      module: document.getElementById("config-module").value,
      provider_name: document.getElementById("config-provider").value.trim() || null,
      base_url: document.getElementById("config-base-url").value.trim() || null,
      api_key: document.getElementById("config-api-key").value.trim() || undefined,
      model_name: modelName,
      reasoning_effort: document.getElementById("config-reasoning").value.trim() || null,
      timeout_seconds: Number(document.getElementById("config-timeout").value || 120),
      priority: Number(document.getElementById("config-priority").value || 100),
      is_enabled: document.getElementById("config-enabled").checked,
      is_default: document.getElementById("config-default").checked,
      system_prompt_template: document.getElementById("config-system-prompt").value.trim() || null,
      notes: document.getElementById("config-notes").value.trim() || null,
    };

    if (scope === "book") {
      if (!bookId) {
        showToast("书籍级配置必须先选中一本书", "error");
        return;
      }
      payload.book_id = bookId;
    }

    try {
      if (state.editingConfigId) {
        payload.clear_api_key = document.getElementById("config-clear-api-key").checked;
        await updateAIConfig(state.editingConfigId, payload);
        showToast("模型配置已更新");
      } else {
        await createAIConfig(payload);
        showToast("模型配置已创建");
      }
      document.getElementById("config-reset").click();
      await refreshConfigs();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "保存模型配置失败";
      showToast(message, "error");
    }
  });

  // 修改密码
  const changePasswordForm = document.getElementById("change-password-form");
  if (changePasswordForm) {
    changePasswordForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const oldPassword = document.getElementById("change-old-password").value;
      const newPassword = document.getElementById("change-new-password").value;
      const confirmPassword = document.getElementById("change-new-password-confirm").value;
      if (newPassword !== confirmPassword) {
        showToast("两次输入的新密码不一致。", "error");
        return;
      }
      try {
        await changeOwnPassword({ old_password: oldPassword, new_password: newPassword });
        showToast("密码修改成功，下次登录时生效。");
        changePasswordForm.reset();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "修改密码失败";
        showToast(message, "error");
      }
    });
  }
}
