import { clearToken, getHealth, isAdminRole, listBooks, listChapters, listCharacters, logout, streamAssistantMessage } from "./api.js";

const PAGE_META = {
  library: {
    title: "我的书库",
    kicker: "Story Library",
    subtitle: "管理书籍、章节、快照和创作入口。",
  },
  writer: {
    title: "核心写作空间",
    kicker: "Writing Studio",
    subtitle: "编辑正文，使用 AI 写作助手，并查看章节快照。",
  },
  characters: {
    title: "人物卡工作台",
    kicker: "Cast Board",
    subtitle: "单独维护人物卡，用豆腐块视图快速浏览和编辑。",
  },
  world: {
    title: "世界观与关系网",
    kicker: "World Bible",
    subtitle: "维护关系图谱与世界观事实。",
  },
  history: {
    title: "历史版本对比",
    kicker: "Snapshot Review",
    subtitle: "查看 AI 修改前的快照与当前章节版本。",
  },
  settings: {
    title: "全局设置、模型配置与 API 配置",
    kicker: "AI Setup",
    subtitle: "维护书籍写作记忆，并为不同用途配置 API、模型和调用策略。",
  },
  admin: {
    title: "管理员控制台",
    kicker: "Operations",
    subtitle: "分配账号、调整权限，并维护本地用户状态。",
  },
};

const NAV_ITEMS = [
  { key: "library", label: "书库", href: "/library", badge: "Books" },
  { key: "writer", label: "写作", href: "/writer", badge: "Draft" },
  { key: "characters", label: "人物卡", href: "/characters", badge: "Cast" },
  { key: "world", label: "世界", href: "/world", badge: "Lore" },
  { key: "history", label: "版本", href: "/history", badge: "Diff" },
  { key: "settings", label: "配置", href: "/settings", badge: "AI" },
  { key: "admin", label: "后台", href: "/admin", badge: "Ops", adminOnly: true },
];

const AI_MODULE_LABELS = {
  assistant: "AI 助手",
  co_writing: "正文续写",
  outline_expansion: "大纲扩写",
  summary: "摘要整理",
  setting_extraction: "设定提取",
  character_extraction: "人物提取",
  relation_extraction: "关系提取",
  reasoner: "写前规划",
};

const AI_SCOPE_LABELS = {
  system: "全局通用",
  user: "当前账号",
  book: "当前书籍",
};

const AI_TARGET_FIELD_LABELS = {
  content: "正文",
  outline: "大纲",
  summary: "摘要",
};

const AI_APPLY_MODE_LABELS = {
  append: "接在后面",
  replace: "直接覆盖",
};

const CHAPTER_NODE_LABELS = {
  volume: "卷",
  chapter: "章",
  scene: "场景",
  note: "笔记",
};

const CHAPTER_STATUS_LABELS = {
  draft: "草稿",
  review: "待审",
  final: "定稿",
  archived: "归档",
};

const USER_ROLE_LABELS = {
  super_admin: "超级管理员",
  admin: "管理员",
  author: "作者",
};

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function formatDateTime(value) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未记录";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatCount(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

export function truncate(value, max = 120) {
  const text = String(value || "").trim();
  if (!text) return "暂无内容";
  return text.length <= max ? text : `${text.slice(0, max).trimEnd()}...`;
}

export function aiModuleLabel(value) {
  return AI_MODULE_LABELS[value] || String(value || "未设置");
}

export function aiScopeLabel(value) {
  return AI_SCOPE_LABELS[value] || String(value || "未设置");
}

export function aiTargetFieldLabel(value) {
  return AI_TARGET_FIELD_LABELS[value] || String(value || "未设置");
}

export function aiApplyModeLabel(value) {
  return AI_APPLY_MODE_LABELS[value] || String(value || "未设置");
}

export function chapterNodeLabel(value) {
  return CHAPTER_NODE_LABELS[value] || String(value || "未设置");
}

export function chapterStatusLabel(value) {
  return CHAPTER_STATUS_LABELS[value] || String(value || "未设置");
}

export function userRoleLabel(value) {
  return USER_ROLE_LABELS[value] || String(value || "未设置");
}

export function aiProviderLabel(config) {
  const providerName = String(config?.provider_name || "").trim();
  if (providerName) return providerName;
  if (config?.api_format === "openai_v1") return "OpenAI 兼容接口";
  return String(config?.api_format || "未填写");
}

export function statusTone(status) {
  if (["active", "final"].includes(status)) return "bg-emerald-100 text-emerald-700";
  if (["review", "draft"].includes(status)) return "bg-amber-100 text-amber-700";
  if (["disabled", "locked", "archived"].includes(status)) return "bg-slate-100 text-slate-600";
  return "bg-slate-100 text-slate-600";
}

export function showToast(message, tone = "default") {
  const container = document.getElementById("app-toast-container") || document.body;
  const toast = document.createElement("div");
  toast.className = "bamboo-toast";
  if (tone === "error") {
    toast.classList.add("bg-rose-100", "text-rose-700");
  }
  toast.textContent = message;
  container.appendChild(toast);

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      toast.style.opacity = "1";
      toast.style.transform = "translateY(0)";
    });
  });

  window.setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(12px)";
    window.setTimeout(() => toast.remove(), 320);
  }, 2600);
}

export function renderStats(container, stats) {
  container.innerHTML = stats
    .map(
      (stat) => `
        <article class="bamboo-stat">
          <div class="bamboo-mini-label">${escapeHtml(stat.label)}</div>
          <div class="mt-3 flex items-end justify-between gap-4">
            <div class="text-3xl font-semibold text-bamboo-deep">${escapeHtml(stat.value)}</div>
            <div class="text-sm text-slate-500">${escapeHtml(stat.meta || "")}</div>
          </div>
        </article>
      `
    )
    .join("");
}

export function renderShell(page, content, user) {
  const meta = PAGE_META[page] || PAGE_META.library;
  const visibleNav = NAV_ITEMS.filter((item) => !item.adminOnly || isAdminRole(user.role));
  const nav = visibleNav
    .map(
      (item) => `
        <a class="bamboo-nav-link" data-active="${item.key === page}" href="${item.href}">
          <span class="text-sm font-medium">${item.label}</span>
          <span class="text-[0.7rem] uppercase tracking-[0.18em] text-slate-400">${item.badge}</span>
        </a>
      `
    )
    .join("");

  return `
    <div class="hero-mist"></div>
    <div class="relative flex min-h-screen">
      <aside class="hidden w-72 shrink-0 flex-col gap-8 px-6 py-8 lg:flex">
        <div class="bamboo-panel p-5">
          <div class="bamboo-chip mb-4">Bamboo Sanctuary</div>
          <div class="bamboo-display text-3xl font-semibold text-bamboo-deep">竹林 AI</div>
          <p class="mt-3 text-sm leading-7 text-slate-500">本地化多用户 AI 小说辅助写作工作台。</p>
        </div>
        <nav class="bamboo-panel p-4">
          <div class="mb-3 px-2 text-[0.72rem] uppercase tracking-[0.22em] text-slate-400">Workspace</div>
          <div class="space-y-2">${nav}</div>
        </nav>
        <div class="bamboo-panel p-5">
          <div class="bamboo-mini-label">当前账号</div>
          <div class="mt-3 flex items-center justify-between gap-4">
            <div>
              <div class="font-medium text-bamboo-deep">${escapeHtml(user.display_name || user.username)}</div>
              <div class="mt-1 text-sm text-slate-500">${escapeHtml(userRoleLabel(user.role))}</div>
            </div>
            <span class="bamboo-chip">${escapeHtml(user.username)}</span>
          </div>
        </div>
      </aside>
      <div class="flex min-w-0 flex-1 flex-col">
        <header class="px-4 pt-4 lg:px-8 lg:pt-8">
          <div class="bamboo-panel flex flex-col gap-4 p-5 lg:flex-row lg:items-center lg:justify-between">
            <div class="flex items-center gap-4">
              <button class="bamboo-btn-secondary px-3 py-2 lg:hidden" id="mobile-menu-toggle" type="button" aria-label="打开菜单">
                <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16" stroke-linecap="round"/></svg>
              </button>
              <div>
                <div class="bamboo-mini-label">${meta.kicker}</div>
                <h1 class="bamboo-display mt-2 text-3xl font-semibold text-bamboo-deep">${meta.title}</h1>
                <p class="mt-2 max-w-3xl text-sm leading-7 text-slate-500">${meta.subtitle}</p>
              </div>
            </div>
            <div class="flex flex-wrap items-center gap-3">
              <div class="inline-flex items-center gap-3 rounded-full bg-white/75 px-4 py-3 text-sm text-slate-600 shadow-cloud">
                <span class="bamboo-status-dot" id="service-status-dot"></span>
                <span id="service-status-text">正在连接后端</span>
              </div>
              <button class="bamboo-btn-secondary" data-logout>退出登录</button>
            </div>
          </div>
          <div class="mt-4 hidden gap-3 overflow-x-auto lg:hidden" id="mobile-nav-bar">${visibleNav.map((item) => `<a class="bamboo-nav-link shrink-0 bg-white/75" data-active="${item.key === page}" href="${item.href}">${item.label}</a>`).join("")}</div>
        </header>
        <main class="flex-1 px-4 py-6 lg:px-8 lg:py-8">${content}</main>
      </div>
    </div>
    <div class="fixed inset-0 z-[100] hidden bg-black/40 lg:hidden" id="mobile-sidebar-overlay"></div>
    <aside class="fixed inset-y-0 left-0 z-[101] w-72 -translate-x-full transform bg-[var(--surface)] p-6 shadow-2xl transition-transform duration-300 ease-in-out lg:hidden" id="mobile-sidebar">
      <div class="mb-6 flex items-center justify-between">
        <div class="bamboo-display text-xl font-semibold text-bamboo-deep">竹林 AI</div>
        <button class="rounded-full px-3 py-2 text-slate-500 hover:bg-white/80" id="mobile-menu-close" type="button" aria-label="关闭菜单">
          <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M6 18L18 6M6 6l12 12" stroke-linecap="round"/></svg>
        </button>
      </div>
      <nav class="space-y-2">${visibleNav.map((item) => `<a class="bamboo-nav-link" data-active="${item.key === page}" href="${item.href}"><span class="text-sm font-medium">${item.label}</span><span class="text-[0.7rem] uppercase tracking-[0.18em] text-slate-400">${item.badge}</span></a>`).join("")}</nav>
    </aside>
    <div class="bamboo-assistant" id="bamboo-assistant">
      <button class="bamboo-assistant-toggle" id="bamboo-assistant-toggle" type="button" aria-label="打开 AI 助手">
        <span class="bamboo-assistant-glyph" aria-hidden="true">竹</span>
        <span class="bamboo-assistant-toggle-copy">
          <span class="bamboo-assistant-toggle-title">AI 助手</span>
          <span class="bamboo-assistant-toggle-subtitle">弹窗打开</span>
        </span>
      </button>

      <div class="bamboo-assistant-overlay hidden" id="bamboo-assistant-overlay">
        <section class="bamboo-assistant-panel" id="bamboo-assistant-panel" aria-modal="true" role="dialog">
          <div class="bamboo-assistant-layout">
            <div class="bamboo-assistant-main">
              <div class="bamboo-assistant-header">
                <div>
                  <div class="bamboo-mini-label">竹林智能助手</div>
                  <div class="mt-2 text-xl font-semibold text-bamboo-deep">全局小说助手</div>
                  <p class="mt-2 text-sm leading-7 text-slate-500">分析剧情、梳理人物、检查世界观矛盾，也可以快速查看最近章节走势。</p>
                </div>
                <button class="bamboo-assistant-close" id="bamboo-assistant-close" type="button" aria-label="关闭 AI 助手">
                  <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path d="M6 18L18 6M6 6l12 12" stroke-linecap="round"></path>
                  </svg>
                </button>
              </div>

              <div class="bamboo-assistant-toolbar">
                <label class="block">
                  <span class="bamboo-mini-label">当前书籍</span>
                  <select class="bamboo-field mt-2" id="bamboo-assistant-book"></select>
                </label>
                <div class="bamboo-assistant-context" id="bamboo-assistant-context">
                  这里会显示当前对话实际绑定的书籍、章节和页面范围。
                </div>
                <div class="bamboo-assistant-prompt-entry">
                  <button class="bamboo-btn-secondary w-full" id="bamboo-assistant-prompt-toggle" type="button">提示词</button>
                  <div class="bamboo-assistant-prompt-summary" id="bamboo-assistant-prompt-summary">未选择人物卡、章节或自定义提示词。</div>
                </div>
              </div>

              <div class="bamboo-assistant-messages" id="bamboo-assistant-messages"></div>

              <form class="bamboo-assistant-form" id="bamboo-assistant-form">
                <textarea
                  class="bamboo-field bamboo-assistant-input"
                  id="bamboo-assistant-input"
                  placeholder="例如：帮我分析最近三章节奏；检查主角动机是否连续；或批量保留软件内人物卡。"
                ></textarea>
                <div class="bamboo-assistant-actions">
                  <div class="bamboo-assistant-primary-actions">
                    <button class="bamboo-btn-primary" id="bamboo-assistant-send" type="submit">发送</button>
                    <button class="bamboo-btn-secondary" id="bamboo-assistant-insert-content" type="button">插入正文</button>
                    <button class="bamboo-btn-secondary" id="bamboo-assistant-insert-outline" type="button">插入大纲</button>
                  </div>
                  <div class="bamboo-assistant-secondary-actions">
                    <button class="bamboo-btn-ghost" id="bamboo-assistant-clear" type="button">清空当前对话</button>
                    <button class="bamboo-btn-ghost" id="bamboo-assistant-attach" type="button">附件</button>
                    <button class="bamboo-btn-ghost" id="bamboo-assistant-voice" type="button">语音</button>
                  </div>
                </div>
              </form>
            </div>

            <aside class="bamboo-assistant-sidebar">
              <div class="bamboo-assistant-sidebar-header">
                <div class="text-sm font-semibold text-bamboo-deep">会话记录</div>
                <div class="mt-1 text-[0.72rem] uppercase tracking-[0.18em] text-slate-400">历史对话</div>
              </div>
              <div class="bamboo-assistant-sessions" id="bamboo-assistant-sessions"></div>
              <div class="bamboo-assistant-sidebar-footer">
                <button class="bamboo-btn-secondary w-full" id="bamboo-assistant-new-session" type="button">开始新对话</button>
              </div>
            </aside>
          </div>

          <div class="bamboo-assistant-prompt-overlay hidden" id="bamboo-assistant-prompt-overlay">
            <div class="bamboo-assistant-prompt-panel">
              <div class="bamboo-assistant-prompt-header">
                <div>
                  <div class="bamboo-mini-label">提示词配置</div>
                  <div class="mt-2 text-lg font-semibold text-bamboo-deep">筛选本轮发给 AI 的上下文</div>
                </div>
                <button class="bamboo-assistant-close" id="bamboo-assistant-prompt-close" type="button" aria-label="关闭提示词">
                  <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path d="M6 18L18 6M6 6l12 12" stroke-linecap="round"></path>
                  </svg>
                </button>
              </div>
              <div class="bamboo-assistant-prompt-body">
                <section class="bamboo-assistant-prompt-section">
                  <div class="bamboo-mini-label">人物卡选择</div>
                  <div class="bamboo-assistant-prompt-grid" id="bamboo-assistant-character-options"></div>
                </section>
                <section class="bamboo-assistant-prompt-section">
                  <div class="bamboo-mini-label">章节选择</div>
                  <div class="bamboo-assistant-prompt-grid" id="bamboo-assistant-chapter-options"></div>
                </section>
                <section class="bamboo-assistant-prompt-section">
                  <div class="bamboo-mini-label">自定义提示词</div>
                  <textarea
                    class="bamboo-field min-h-[10rem] resize-y bg-white/90"
                    id="bamboo-assistant-custom-prompt"
                    placeholder="例如：只分析女主和师尊的关系推进；重点看第三章到第五章；不要重复复述原文，只给结论和依据。"
                  ></textarea>
                </section>
              </div>
              <div class="bamboo-assistant-prompt-actions">
                <button class="bamboo-btn-ghost" id="bamboo-assistant-prompt-reset" type="button">清空提示词</button>
                <button class="bamboo-btn-primary" id="bamboo-assistant-prompt-apply" type="button">确认提示词</button>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
    <div class="bamboo-toast-container" id="app-toast-container"></div>
  `;
}

export async function updateServiceStatus() {
  const dot = document.getElementById("service-status-dot");
  const text = document.getElementById("service-status-text");
  const loginBadge = document.getElementById("login-health");

  try {
    const health = await getHealth();
    if (dot) dot.dataset.state = "ok";
    if (text) text.textContent = `${health.app_name} / ${health.environment}`;
    if (loginBadge) loginBadge.textContent = `服务在线 · ${health.environment}`;
  } catch {
    if (dot) dot.dataset.state = "error";
    if (text) text.textContent = "后端不可用";
    if (loginBadge) loginBadge.textContent = "服务离线";
  }
}

export function bindCommonActions() {
  document.querySelectorAll("[data-logout]").forEach((button) => {
    button.addEventListener("click", () => logout());
  });
  document.querySelectorAll("[data-soon]").forEach((button) => {
    button.addEventListener("click", () => showToast("这部分将在下一步接入。"));
  });

  const sidebar = document.getElementById("mobile-sidebar");
  const overlay = document.getElementById("mobile-sidebar-overlay");
  const openBtn = document.getElementById("mobile-menu-toggle");
  const closeBtn = document.getElementById("mobile-menu-close");

  function openMobileSidebar() {
    if (sidebar) sidebar.classList.remove("-translate-x-full");
    if (sidebar) sidebar.classList.add("translate-x-0");
    if (overlay) overlay.classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }
  function closeMobileSidebar() {
    if (sidebar) sidebar.classList.add("-translate-x-full");
    if (sidebar) sidebar.classList.remove("translate-x-0");
    if (overlay) overlay.classList.add("hidden");
    document.body.style.overflow = "";
  }

  openBtn?.addEventListener("click", openMobileSidebar);
  closeBtn?.addEventListener("click", closeMobileSidebar);
  overlay?.addEventListener("click", closeMobileSidebar);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeMobileSidebar();
  });
}

export function setAssistantContext({ bookId = null, chapterId = null, chapterTitle = null } = {}) {
  const body = document.body;
  if (!body) return;

  const normalizedBookId = Number(bookId);
  const normalizedChapterId = Number(chapterId);

  if (Number.isFinite(normalizedBookId) && normalizedBookId > 0) {
    body.dataset.assistantBookId = String(normalizedBookId);
  } else {
    delete body.dataset.assistantBookId;
  }

  if (Number.isFinite(normalizedChapterId) && normalizedChapterId > 0) {
    body.dataset.assistantChapterId = String(normalizedChapterId);
  } else {
    delete body.dataset.assistantChapterId;
  }

  const normalizedChapterTitle = String(chapterTitle || "").trim();
  if (normalizedChapterTitle) {
    body.dataset.assistantChapterTitle = normalizedChapterTitle;
  } else {
    delete body.dataset.assistantChapterTitle;
  }
}

const ASSISTANT_SESSION_STORAGE_KEY = "bamboo_assistant_sessions_v2";
const ASSISTANT_MAX_SESSIONS = 12;
const ASSISTANT_PAGE_LABELS = {
  library: "书库",
  writer: "写作页",
  world: "世界页",
  history: "历史页",
  settings: "配置页",
  admin: "后台页",
  characters: "人物页",
};

function assistantSessionId() {
  return `assistant_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function assistantNormalizeMessages(messages) {
  return Array.isArray(messages)
    ? messages
        .filter((item) => item && (item.role === "assistant" || item.role === "user"))
        .map((item) => ({
          role: item.role,
          content: String(item.content || "").trim(),
        }))
        .filter((item) => item.content)
    : [];
}

function assistantDefaultMessage(bookTitle = "") {
  return {
    role: "assistant",
    content: bookTitle
      ? `这里是《${bookTitle}》的 AI 助手弹窗。你可以直接让我分析剧情走势、梳理人物关系、检查设定冲突，或总结最近几章的问题。`
      : "这里是竹林 AI 助手弹窗。先选一本书，我会结合这本书的上下文来回答。",
  };
}

function assistantSessionTitle(messages) {
  const firstUserMessage = assistantNormalizeMessages(messages).find((item) => item.role === "user");
  return firstUserMessage ? truncate(firstUserMessage.content, 18) : "新的助手对话";
}

function assistantSessionPreview(messages) {
  const normalized = assistantNormalizeMessages(messages);
  const lastMessage = normalized[normalized.length - 1];
  return lastMessage ? truncate(lastMessage.content, 32) : "还没有聊天内容";
}

function loadAssistantSessions() {
  try {
    const raw = window.localStorage.getItem(ASSISTANT_SESSION_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((item) => ({
        id: String(item?.id || "").trim() || assistantSessionId(),
        title: String(item?.title || "").trim() || "新的助手对话",
        updated_at: String(item?.updated_at || "").trim() || new Date().toISOString(),
        book_id: Number(item?.book_id || 0) || null,
        messages: assistantSessionMessages(item?.messages),
        prompt_filters: {
          character_ids: Array.isArray(item?.prompt_filters?.character_ids)
            ? item.prompt_filters.character_ids.map((value) => Number(value || 0)).filter((value) => value > 0)
            : [],
          chapter_ids: Array.isArray(item?.prompt_filters?.chapter_ids)
            ? item.prompt_filters.chapter_ids.map((value) => Number(value || 0)).filter((value) => value > 0)
            : [],
          custom_prompt: String(item?.prompt_filters?.custom_prompt || "").trim(),
        },
      }))
      .slice(0, ASSISTANT_MAX_SESSIONS);
  } catch {
    return [];
  }
}

function saveAssistantSessions(sessions) {
  try {
    window.localStorage.setItem(
      ASSISTANT_SESSION_STORAGE_KEY,
      JSON.stringify((sessions || []).slice(0, ASSISTANT_MAX_SESSIONS))
    );
  } catch {
    // Ignore persistence failures and keep the in-memory assistant usable.
  }
}

function assistantPromptFilters() {
  return {
    character_ids: [],
    chapter_ids: [],
    custom_prompt: "",
  };
}

function assistantSessionMessages(messages) {
  return Array.isArray(messages)
    ? messages
        .filter((item) => item && (item.role === "assistant" || item.role === "user"))
        .map((item) => ({
          role: item.role,
          content: String(item.content || "").trim(),
          editProposal:
            item.role === "assistant" && item.editProposal && typeof item.editProposal === "object"
              ? {
                  target_field: String(item.editProposal.target_field || "").trim(),
                  title: String(item.editProposal.title || "").trim(),
                  content: String(item.editProposal.content || ""),
                }
              : null,
        }))
        .filter((item) => item.content || item.editProposal)
    : [];
}

function assistantRenderMessage(message, index) {
  const roleLabel = message.role === "assistant" ? "竹林助手" : "你";
  const roleClass = message.role === "assistant" ? "assistant" : "user";
  return `
    <article class="bamboo-assistant-message-row" data-role="${roleClass}">
      ${
        message.role === "assistant"
          ? '<div class="bamboo-assistant-avatar" aria-hidden="true">竹</div>'
          : '<div class="bamboo-assistant-avatar-spacer" aria-hidden="true"></div>'
      }
      <div class="bamboo-assistant-message" data-role="${roleClass}">
        <div class="bamboo-mini-label">${escapeHtml(roleLabel)}</div>
        <div class="mt-2 whitespace-pre-wrap break-words text-sm leading-7">${escapeHtml(message.content || "")}</div>
        ${
          message.role === "assistant" && message.editProposal
            ? `
              <div class="bamboo-assistant-message-actions">
                <div class="text-xs leading-6 text-slate-500">助手已生成可直接写入${escapeHtml(
                  message.editProposal.target_field === "content"
                    ? "正文"
                    : message.editProposal.target_field === "outline"
                      ? "大纲"
                      : "摘要"
                )}的修改稿，确认后才会写入。</div>
                <button
                  class="bamboo-btn-primary"
                  data-assistant-accept-edit="true"
                  data-assistant-message-index="${index}"
                  type="button"
                >
                  接受修改
                </button>
              </div>
            `
            : ""
        }
      </div>
    </article>
  `;
}

function assistantDetectBookId(books) {
  const params = new URLSearchParams(window.location.search);
  const queryBookId = Number(params.get("book"));
  if (queryBookId && books.some((book) => book.id === queryBookId)) return queryBookId;

  const contextBookId = Number(document.body?.dataset.assistantBookId);
  if (contextBookId && books.some((book) => book.id === contextBookId)) return contextBookId;

  const candidateIds = [
    "writer-book-select",
    "world-book-select",
  ];
  for (const id of candidateIds) {
    const element = document.getElementById(id);
    const value = Number(element?.value);
    if (value && books.some((book) => book.id === value)) return value;
  }

  const stored = Number(window.localStorage.getItem("bamboo_assistant_book_id"));
  if (stored && books.some((book) => book.id === stored)) return stored;
  return books[0]?.id || null;
}

function assistantDetectChapterId() {
  const params = new URLSearchParams(window.location.search);
  const queryChapterId = Number(params.get("chapter"));
  if (queryChapterId) return queryChapterId;

  const contextChapterId = Number(document.body?.dataset.assistantChapterId);
  if (contextChapterId) return contextChapterId;

  const candidateIds = [
    "world-chapter-select",
  ];
  for (const id of candidateIds) {
    const element = document.getElementById(id);
    const value = Number(element?.value);
    if (value) return value;
  }
  return null;
}

function assistantDetectChapterTitle() {
  const contextChapterTitle = String(document.body?.dataset.assistantChapterTitle || "").trim();
  if (contextChapterTitle) return contextChapterTitle;

  const writerTitle = String(document.getElementById("writer-chapter")?.value || "").trim();
  if (writerTitle) return writerTitle;

  const worldSelect = document.getElementById("world-chapter-select");
  const worldTitle = String(worldSelect?.selectedOptions?.[0]?.textContent || "").trim();
  if (worldTitle) return worldTitle;

  return "";
}

export async function initGlobalAssistant(page) {
  const root = document.getElementById("bamboo-assistant");
  const overlay = document.getElementById("bamboo-assistant-overlay");
  const panel = document.getElementById("bamboo-assistant-panel");
  const toggle = document.getElementById("bamboo-assistant-toggle");
  const closeButton = document.getElementById("bamboo-assistant-close");
  const form = document.getElementById("bamboo-assistant-form");
  const input = document.getElementById("bamboo-assistant-input");
  const messages = document.getElementById("bamboo-assistant-messages");
  const bookSelect = document.getElementById("bamboo-assistant-book");
  const clearButton = document.getElementById("bamboo-assistant-clear");
  const sendButton = document.getElementById("bamboo-assistant-send");
  const context = document.getElementById("bamboo-assistant-context");
  const insertContentButton = document.getElementById("bamboo-assistant-insert-content");
  const insertOutlineButton = document.getElementById("bamboo-assistant-insert-outline");
  const sessionList = document.getElementById("bamboo-assistant-sessions");
  const newSessionButton = document.getElementById("bamboo-assistant-new-session");
  const attachButton = document.getElementById("bamboo-assistant-attach");
  const voiceButton = document.getElementById("bamboo-assistant-voice");
  const promptToggleButton = document.getElementById("bamboo-assistant-prompt-toggle");
  const promptSummary = document.getElementById("bamboo-assistant-prompt-summary");
  const promptOverlay = document.getElementById("bamboo-assistant-prompt-overlay");
  const promptCloseButton = document.getElementById("bamboo-assistant-prompt-close");
  const promptApplyButton = document.getElementById("bamboo-assistant-prompt-apply");
  const promptResetButton = document.getElementById("bamboo-assistant-prompt-reset");
  const characterOptions = document.getElementById("bamboo-assistant-character-options");
  const chapterOptions = document.getElementById("bamboo-assistant-chapter-options");
  const customPromptInput = document.getElementById("bamboo-assistant-custom-prompt");

  if (
    !root ||
    !overlay ||
    !panel ||
    !toggle ||
    !form ||
    !input ||
    !messages ||
    !bookSelect ||
    !clearButton ||
    !sendButton ||
    !context ||
    !insertContentButton ||
    !insertOutlineButton ||
    !sessionList ||
    !newSessionButton ||
    !attachButton ||
    !voiceButton ||
    !promptToggleButton ||
    !promptSummary ||
    !promptOverlay ||
    !promptCloseButton ||
    !promptApplyButton ||
    !promptResetButton ||
    !characterOptions ||
    !chapterOptions ||
    !customPromptInput
  ) {
    return;
  }

  const state = {
    books: [],
    bookId: null,
    chapters: [],
    characters: [],
    sessions: loadAssistantSessions(),
    activeSessionId: null,
    history: [],
    loading: false,
    promptFiltersDraft: assistantPromptFilters(),
  };

  const selectedBook = () => state.books.find((item) => item.id === state.bookId) || null;
  const latestAssistantReply = () =>
    [...state.history].reverse().find((item) => item.role === "assistant" && String(item.content || "").trim());

  function buildDefaultHistory(bookTitle = "") {
    return [assistantDefaultMessage(bookTitle)];
  }

  function createSession({ title = "", bookId = state.bookId, messages = buildDefaultHistory(selectedBook()?.title || "") } = {}) {
    const normalizedMessages = assistantNormalizeMessages(messages);
    return {
      id: assistantSessionId(),
      title: title || assistantSessionTitle(normalizedMessages),
      updated_at: new Date().toISOString(),
      book_id: Number(bookId || 0) || null,
      messages: assistantSessionMessages(normalizedMessages.length ? normalizedMessages : buildDefaultHistory()),
      prompt_filters: assistantPromptFilters(),
    };
  }

  function persistSessions() {
    saveAssistantSessions(state.sessions);
  }

  function renderSessions() {
    const items = [...state.sessions].sort(
      (left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime()
    );
    sessionList.innerHTML = items.length
      ? items
          .map((session) => {
            const active = session.id === state.activeSessionId;
            const book = state.books.find((item) => item.id === session.book_id);
            return `
              <button class="bamboo-assistant-session" data-session-id="${escapeHtml(session.id)}" data-active="${active}" type="button">
                <div class="bamboo-assistant-session-title">${escapeHtml(session.title || "新的助手对话")}</div>
                <div class="mt-2 text-xs leading-6 text-slate-500">${escapeHtml(assistantSessionPreview(session.messages))}</div>
                <div class="mt-3 flex items-center justify-between gap-3 text-[0.68rem] uppercase tracking-[0.14em] text-slate-400">
                  <span>${escapeHtml(book?.title || "未绑定书籍")}</span>
                  <span>${escapeHtml(formatDateTime(session.updated_at))}</span>
                </div>
              </button>
            `;
          })
          .join("")
      : '<div class="rounded-[1.2rem] bg-white/80 p-4 text-sm leading-7 text-slate-500 shadow-cloud">这里还没有历史对话。点击下方按钮即可开始新的助手会话。</div>';
  }

  function upsertActiveSession() {
    const normalizedMessages = assistantNormalizeMessages(state.history);
    const title = assistantSessionTitle(normalizedMessages);
    const updatedSession = {
      id: state.activeSessionId || assistantSessionId(),
      title,
      updated_at: new Date().toISOString(),
      book_id: state.bookId,
      messages: assistantSessionMessages(state.history),
      prompt_filters: {
        character_ids: [...(state.promptFiltersDraft.character_ids || [])],
        chapter_ids: [...(state.promptFiltersDraft.chapter_ids || [])],
        custom_prompt: String(state.promptFiltersDraft.custom_prompt || "").trim(),
      },
    };
    state.activeSessionId = updatedSession.id;
    state.sessions = [updatedSession, ...state.sessions.filter((item) => item.id !== updatedSession.id)].slice(
      0,
      ASSISTANT_MAX_SESSIONS
    );
    persistSessions();
    renderSessions();
  }

  function activateSession(session, { syncBook = true } = {}) {
    if (!session) return;
    state.activeSessionId = session.id;
    state.history = assistantSessionMessages(session.messages);
    state.promptFiltersDraft = {
      ...assistantPromptFilters(),
      ...(session.prompt_filters || {}),
      character_ids: Array.isArray(session.prompt_filters?.character_ids) ? [...session.prompt_filters.character_ids] : [],
      chapter_ids: Array.isArray(session.prompt_filters?.chapter_ids) ? [...session.prompt_filters.chapter_ids] : [],
      custom_prompt: String(session.prompt_filters?.custom_prompt || "").trim(),
    };
    if (!state.history.length) {
      state.history = buildDefaultHistory(selectedBook()?.title || "");
    }
    if (syncBook && session.book_id && state.books.some((book) => book.id === session.book_id)) {
      state.bookId = session.book_id;
      bookSelect.value = String(session.book_id);
    }
    renderMessages();
    syncContextNote();
    refreshInsertButtons();
    renderPromptSelectionSummary();
    renderPromptOptions();
    renderSessions();
  }

  function createAndActivateNewSession() {
    const session = createSession({
      bookId: state.bookId,
      messages: buildDefaultHistory(selectedBook()?.title || ""),
    });
    state.sessions = [session, ...state.sessions.filter((item) => item.id !== session.id)].slice(0, ASSISTANT_MAX_SESSIONS);
    persistSessions();
    activateSession(session, { syncBook: false });
  }

  async function refreshPromptData() {
    if (!state.bookId) {
      state.chapters = [];
      state.characters = [];
      renderPromptOptions();
      renderPromptSelectionSummary();
      return;
    }
    try {
      const [chaptersResponse, charactersResponse] = await Promise.all([
        listChapters(state.bookId),
        listCharacters(state.bookId),
      ]);
      state.chapters = chaptersResponse.items || [];
      state.characters = charactersResponse.items || [];
    } catch {
      state.chapters = [];
      state.characters = [];
    }
    const validCharacterIds = new Set(state.characters.map((item) => Number(item.id)));
    const validChapterIds = new Set(state.chapters.map((item) => Number(item.id)));
    state.promptFiltersDraft.character_ids = (state.promptFiltersDraft.character_ids || []).filter((id) =>
      validCharacterIds.has(Number(id))
    );
    state.promptFiltersDraft.chapter_ids = (state.promptFiltersDraft.chapter_ids || []).filter((id) =>
      validChapterIds.has(Number(id))
    );
    renderPromptOptions();
    renderPromptSelectionSummary();
  }

  function renderPromptSelectionSummary() {
    const characterCount = (state.promptFiltersDraft.character_ids || []).length;
    const chapterCount = (state.promptFiltersDraft.chapter_ids || []).length;
    const customPrompt = String(state.promptFiltersDraft.custom_prompt || "").trim();
    const summaryParts = [];
    if (characterCount) summaryParts.push(`已选 ${characterCount} 个人物卡`);
    if (chapterCount) summaryParts.push(`已选 ${chapterCount} 个章节`);
    if (customPrompt) summaryParts.push(`已填写自定义提示词`);
    promptSummary.textContent = summaryParts.length ? summaryParts.join("，") : "未选择人物卡、章节或自定义提示词。";
  }

  function renderPromptOptions() {
    const characterSelected = new Set((state.promptFiltersDraft.character_ids || []).map((item) => Number(item)));
    const chapterSelected = new Set((state.promptFiltersDraft.chapter_ids || []).map((item) => Number(item)));

    characterOptions.innerHTML = state.characters.length
      ? state.characters
          .map(
            (item) => `
              <button
                class="bamboo-assistant-filter-chip"
                data-filter-group="character"
                data-filter-id="${item.id}"
                data-active="${characterSelected.has(Number(item.id))}"
                type="button"
              >
                ${escapeHtml(item.name || `人物 #${item.id}`)}
              </button>
            `
          )
          .join("")
      : '<div class="text-sm leading-7 text-slate-500">当前书籍还没有人物卡。</div>';

    chapterOptions.innerHTML = state.chapters.length
      ? state.chapters
          .map(
            (item) => `
              <button
                class="bamboo-assistant-filter-chip"
                data-filter-group="chapter"
                data-filter-id="${item.id}"
                data-active="${chapterSelected.has(Number(item.id))}"
                type="button"
              >
                ${escapeHtml(item.title || `章节 #${item.id}`)}
              </button>
            `
          )
          .join("")
      : '<div class="text-sm leading-7 text-slate-500">当前书籍还没有章节。</div>';

    customPromptInput.value = String(state.promptFiltersDraft.custom_prompt || "");
  }

  function setPromptOverlay(open) {
    promptOverlay.classList.toggle("hidden", !open);
  }

  function setOpen(open) {
    overlay.classList.toggle("hidden", !open);
    toggle.dataset.open = open ? "true" : "false";
    document.body.style.overflow = open ? "hidden" : "";
  }

  function renderMessages() {
    messages.innerHTML = state.history.map((message, index) => assistantRenderMessage(message, index)).join("");
    messages.scrollTop = messages.scrollHeight;
  }

  function syncContextNote() {
    const book = selectedBook();
    const chapterId = assistantDetectChapterId();
    const chapterTitle = assistantDetectChapterTitle();
    const chapter = state.chapters.find((item) => Number(item.id) === Number(chapterId)) || null;
    const chapterNumber = Number(chapter?.sequence_number || chapter?.sort_order || 0);
    const chapterLabel = chapterTitle || chapter?.title || (chapterNumber ? `第${chapterNumber}章` : "");
    context.textContent = book
      ? `当前作用范围：${book.title}${chapterLabel ? ` / ${chapterLabel}` : chapterId ? ` / 章节 ${chapterId}` : ""} / ${ASSISTANT_PAGE_LABELS[page] || page || "当前页面"}`
      : "请先选择一本书，然后再和助手对话。";
  }

  function refreshInsertButtons() {
    const canInsert = page === "writer" && Boolean(document.getElementById("writer-editor"));
    const hasReply = Boolean(latestAssistantReply());
    insertContentButton.disabled = !canInsert || !hasReply || state.loading;
    insertOutlineButton.disabled = !canInsert || !hasReply || state.loading;
  }

  function setLoading(loading) {
    state.loading = loading;
    sendButton.disabled = loading;
    clearButton.disabled = loading;
    input.disabled = loading;
    sendButton.textContent = loading ? "发送中..." : "发送";
    refreshInsertButtons();
  }

  try {
    const response = await listBooks();
    state.books = response.items || [];
    state.bookId = assistantDetectBookId(state.books);
    bookSelect.innerHTML = state.books.length
      ? state.books
          .map(
            (book) =>
              `<option value="${book.id}" ${book.id === state.bookId ? "selected" : ""}>${escapeHtml(book.title)}</option>`
          )
          .join("")
      : '<option value="">暂无书籍</option>';
    if (state.bookId) {
      window.localStorage.setItem("bamboo_assistant_book_id", String(state.bookId));
    }
  } catch (error) {
    state.history = [
      {
        role: "assistant",
        content: "书籍列表加载失败，暂时无法使用全局 AI 助手。",
      },
    ];
  }

  window.addEventListener("bamboo:book-renamed", (event) => {
    const detail = event.detail && typeof event.detail === "object" ? event.detail : null;
    const bookId = Number(detail?.bookId);
    const title = String(detail?.title || "").trim();
    if (!bookId || !title) return;

    state.books = state.books.map((book) => (book.id === bookId ? { ...book, title } : book));
    const option = Array.from(bookSelect.options || []).find((item) => Number(item.value) === bookId);
    if (option) {
      option.textContent = title;
    }
    syncContextNote();
    renderSessions();
  });

  if (state.sessions.length) {
    const initialSession =
      state.sessions.find((item) => item.book_id === state.bookId) ||
      [...state.sessions].sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime())[0];
    activateSession(initialSession, { syncBook: !state.bookId });
  } else if (!state.history.length) {
    createAndActivateNewSession();
  } else {
    upsertActiveSession();
    activateSession(state.sessions[0], { syncBook: false });
  }

  await refreshPromptData();

  renderMessages();
  syncContextNote();
  renderSessions();
  refreshInsertButtons();

  toggle.addEventListener("click", () => {
    const isOpen = toggle.dataset.open === "true";
    setOpen(!isOpen);
    if (!isOpen) {
      window.setTimeout(() => input.focus(), 120);
    }
  });
  closeButton?.addEventListener("click", () => setOpen(false));
  overlay.addEventListener("click", (event) => {
    if (event.target?.id === "bamboo-assistant-overlay") {
      setOpen(false);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && toggle.dataset.open === "true") {
      setOpen(false);
    }
  });

  bookSelect.addEventListener("change", async () => {
    state.bookId = Number(bookSelect.value) || null;
    if (state.bookId) {
      window.localStorage.setItem("bamboo_assistant_book_id", String(state.bookId));
    }
    if (state.history.length === 1 && state.history[0]?.role === "assistant") {
      state.history = buildDefaultHistory(selectedBook()?.title || "");
      renderMessages();
    }
    syncContextNote();
    upsertActiveSession();
    await refreshPromptData();
  });

  clearButton.addEventListener("click", () => {
    state.history = buildDefaultHistory(selectedBook()?.title || "");
    renderMessages();
    upsertActiveSession();
    refreshInsertButtons();
  });

  sessionList.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-session-id]");
    if (!button) return;
    const session = state.sessions.find((item) => item.id === button.dataset.sessionId);
    activateSession(session);
    await refreshPromptData();
  });

  newSessionButton.addEventListener("click", async () => {
    createAndActivateNewSession();
    await refreshPromptData();
    window.setTimeout(() => input.focus(), 80);
  });

  const insertReply = (targetId, label) => {
    const target = document.getElementById(targetId);
    const reply = latestAssistantReply();
    if (!target || !reply) {
      showToast(`当前没有可插入到${label}的助手回复。`, "error");
      return;
    }
    const currentValue = String(target.value || "").trim();
    target.value = currentValue ? `${currentValue}\n\n${reply.content}` : reply.content;
    target.dispatchEvent(new Event("input", { bubbles: true }));
    showToast(`已插入到${label}`);
  };

  const applyAssistantEditProposal = (proposal) => {
    if (!proposal || typeof proposal !== "object") {
      showToast("当前没有可应用的改写提案。", "error");
      return;
    }
    const targetId =
      proposal.target_field === "content"
        ? "writer-editor"
        : proposal.target_field === "outline"
          ? "writer-outline"
          : proposal.target_field === "summary"
            ? "writer-summary"
            : "";
    const targetLabel =
      proposal.target_field === "content"
        ? "正文"
        : proposal.target_field === "outline"
          ? "大纲"
          : proposal.target_field === "summary"
            ? "摘要"
            : "";
    const target = targetId ? document.getElementById(targetId) : null;
    if (!target) {
      showToast("当前页面没有可写入的目标文本框。", "error");
      return;
    }
    target.value = String(proposal.content || "");
    target.dispatchEvent(new Event("input", { bubbles: true }));
    showToast(`已接受修改，并写入${targetLabel}`);
  };

  const currentWriterDraftPayload = () => {
    if (page !== "writer") return {};
    return {
      current_chapter_title: document.getElementById("writer-chapter")?.value?.trim() || null,
      current_chapter_summary: document.getElementById("writer-summary")?.value ?? null,
      current_chapter_outline: document.getElementById("writer-outline")?.value ?? null,
      current_chapter_content: document.getElementById("writer-editor")?.value ?? null,
    };
  };

  insertContentButton.addEventListener("click", () => insertReply("writer-editor", "正文"));
  insertOutlineButton.addEventListener("click", () => insertReply("writer-outline", "大纲"));
  attachButton.addEventListener("click", () => showToast("附件能力还没有接入。"));
  voiceButton.addEventListener("click", () => showToast("语音能力还没有接入。"));
  promptToggleButton.addEventListener("click", async () => {
    await refreshPromptData();
    setPromptOverlay(true);
  });
  promptCloseButton.addEventListener("click", () => setPromptOverlay(false));
  promptOverlay.addEventListener("click", (event) => {
    if (event.target?.id === "bamboo-assistant-prompt-overlay") {
      setPromptOverlay(false);
    }
  });
  promptApplyButton.addEventListener("click", () => {
    state.promptFiltersDraft.custom_prompt = customPromptInput.value.trim();
    renderPromptSelectionSummary();
    upsertActiveSession();
    setPromptOverlay(false);
  });
  promptResetButton.addEventListener("click", () => {
    state.promptFiltersDraft = assistantPromptFilters();
    renderPromptOptions();
    renderPromptSelectionSummary();
    upsertActiveSession();
  });
  customPromptInput.addEventListener("input", () => {
    state.promptFiltersDraft.custom_prompt = customPromptInput.value;
    renderPromptSelectionSummary();
  });
  characterOptions.addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter-group='character'][data-filter-id]");
    if (!button) return;
    const id = Number(button.dataset.filterId);
    const selected = new Set((state.promptFiltersDraft.character_ids || []).map((item) => Number(item)));
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    state.promptFiltersDraft.character_ids = [...selected];
    renderPromptOptions();
    renderPromptSelectionSummary();
  });
  chapterOptions.addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter-group='chapter'][data-filter-id]");
    if (!button) return;
    const id = Number(button.dataset.filterId);
    const selected = new Set((state.promptFiltersDraft.chapter_ids || []).map((item) => Number(item)));
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    state.promptFiltersDraft.chapter_ids = [...selected];
    renderPromptOptions();
    renderPromptSelectionSummary();
  });
  messages.addEventListener("click", (event) => {
    const button = event.target.closest("[data-assistant-accept-edit='true'][data-assistant-message-index]");
    if (!button) return;
    const index = Number(button.dataset.assistantMessageIndex);
    const messageItem = Number.isInteger(index) ? state.history[index] : null;
    if (!messageItem?.editProposal) {
      showToast("当前找不到这条改写提案。", "error");
      return;
    }
    applyAssistantEditProposal(messageItem.editProposal);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || state.loading) return;
    if (!state.bookId) {
      showToast("请先选择一本书", "error");
      return;
    }

    const historyForApi = state.history.slice(-8).map((item) => ({
      role: item.role,
      content: item.content,
    }));
    state.history.push({ role: "user", content: message });
    state.history.push({ role: "assistant", content: "" });
    renderMessages();
    upsertActiveSession();
    input.value = "";
    setLoading(true);

    try {
      const assistantIndex = state.history.length - 1;
      const response = await streamAssistantMessage({
        book_id: state.bookId,
        chapter_id: assistantDetectChapterId(),
        page,
        message,
        history: historyForApi,
        selected_character_ids: state.promptFiltersDraft.character_ids || [],
        selected_chapter_ids: state.promptFiltersDraft.chapter_ids || [],
        custom_prompt: String(state.promptFiltersDraft.custom_prompt || "").trim() || null,
        ...currentWriterDraftPayload(),
      }, {
        onEvent(eventPayload) {
          if (eventPayload?.type === "delta") {
            state.history[assistantIndex] = {
              ...state.history[assistantIndex],
              role: "assistant",
              content: String(eventPayload.text || eventPayload.delta || ""),
            };
            renderMessages();
          }
        },
      });
      state.history[assistantIndex] = {
        role: "assistant",
        content: response.reply || "这次没有拿到可展示的回复。",
        editProposal: response.edit_proposal || null,
      };
      renderMessages();
      syncContextNote();
      upsertActiveSession();
      refreshInsertButtons();

      if (response.action?.type === "world_conflict_resolution" && response.action?.resolved_count >= 0) {
        showToast(
          response.action.resolved_count
            ? `已处理 ${response.action.resolved_count} 条冲突`
            : "当前没有待处理冲突"
        );
      }

      if (response.action?.should_reload && page === "world") {
        window.setTimeout(() => window.location.reload(), 900);
      }
    } catch (error) {
      const messageText =
        error && typeof error === "object" && "message" in error
          ? error.message
          : "全局 AI 助手暂时不可用";
      if (state.history[state.history.length - 1]?.role === "assistant" && !state.history[state.history.length - 1]?.content) {
        state.history[state.history.length - 1] = { role: "assistant", content: String(messageText) };
      } else {
        state.history.push({ role: "assistant", content: String(messageText) });
      }
      renderMessages();
      upsertActiveSession();
      refreshInsertButtons();
      showToast(String(messageText), "error");
    } finally {
      setLoading(false);
    }
  });
}

export function redirectToLogin() {
  clearToken();
  window.location.href = "/login";
}
