import {
  ApiError,
  createUser,
  downloadDatabaseBackup,
  getAdminBookMemory,
  getDatabaseBackupSettings,
  isAdminRole,
  listBooks,
  listRecentSnapshots,
  listUsers,
  resetUserPassword,
  restoreDatabaseBackup,
  runDatabaseBackupNow,
  updateBook,
  updateDatabaseBackupSettings,
  updateUser,
} from "./api.js";
import { escapeHtml, formatCount, formatDateTime, renderStats, showToast, truncate } from "./shell.js";

let activeMemoryTab = "style";

function renderUserRow(user) {
  return `
    <tr>
      <td class="px-4 py-3">
        <div class="font-medium text-bamboo-deep">${escapeHtml(user.display_name || user.username)}</div>
        <div class="text-sm text-slate-500">${escapeHtml(user.username)}</div>
      </td>
      <td class="px-4 py-3 text-sm text-slate-500">${escapeHtml(user.email || "未设置")}</td>
      <td class="px-4 py-3">
        <select class="bamboo-field min-w-[8rem]" data-user-role="${user.id}">
          ${["super_admin", "admin", "author"]
            .map((role) => `<option value="${role}" ${role === user.role ? "selected" : ""}>${role}</option>`)
            .join("")}
        </select>
      </td>
      <td class="px-4 py-3">
        <select class="bamboo-field min-w-[8rem]" data-user-status="${user.id}">
          ${["active", "disabled", "locked"]
            .map((status) => `<option value="${status}" ${status === user.status ? "selected" : ""}>${status}</option>`)
            .join("")}
        </select>
      </td>
      <td class="px-4 py-3">
        <label class="inline-flex items-center gap-2 text-sm text-slate-600">
          <input ${user.is_active ? "checked" : ""} data-user-active="${user.id}" type="checkbox" />
          <span>启用</span>
        </label>
      </td>
      <td class="px-4 py-3 text-sm text-slate-500">${formatCount(user.book_count)}</td>
      <td class="px-4 py-3 text-sm text-slate-500">${escapeHtml(formatDateTime(user.last_login_at))}</td>
      <td class="px-4 py-3">
        <button class="bamboo-btn-secondary" data-user-save="${user.id}" type="button">保存</button>
      </td>
    </tr>
  `;
}

function renderBookRow(book, users) {
  return `
    <tr>
      <td class="px-4 py-3">
        <div class="font-medium text-bamboo-deep">${escapeHtml(book.title)}</div>
        <div class="text-sm text-slate-500">${escapeHtml(book.genre || "未分类")}</div>
      </td>
      <td class="px-4 py-3 text-sm text-slate-500">${formatCount(book.chapter_count)}</td>
      <td class="px-4 py-3 text-sm text-slate-500">${formatCount(book.word_count)}</td>
      <td class="px-4 py-3">
        <select class="bamboo-field min-w-[10rem]" data-book-owner="${book.id}">
          ${users
            .filter((user) => user.role === "author" || user.role === "admin" || user.role === "super_admin")
            .map(
              (user) =>
                `<option value="${user.id}" ${user.id === book.owner_id ? "selected" : ""}>${escapeHtml(user.display_name || user.username)}</option>`
            )
            .join("")}
        </select>
      </td>
      <td class="px-4 py-3 text-sm text-slate-500">${escapeHtml(formatDateTime(book.updated_at))}</td>
      <td class="px-4 py-3">
        <button class="bamboo-btn-secondary" data-book-save="${book.id}" type="button">分配</button>
      </td>
    </tr>
  `;
}

function renderMemorySummary(payload) {
  const summary = document.getElementById("admin-memory-summary");
  if (!summary) return;
  if (!payload) {
    summary.innerHTML = "请选择一本书查看记忆。";
    return;
  }
  const stats = payload.memory_stats || {};
  const styleSourceLabel =
    payload.style_anchor?.source === "derived_style_summary" ? "自动总结文风" : "暂无自动总结文风";
  summary.innerHTML = `
    <div class="space-y-2">
      <div class="font-medium text-bamboo-deep">${escapeHtml(payload.book?.title || "未命名书籍")}</div>
      <div>章节数：${escapeHtml(formatCount(stats.chapter_count || 0))}</div>
      <div>章节情景记忆：${escapeHtml(formatCount(stats.episodic_count || 0))}</div>
      <div>语义记忆：${escapeHtml(formatCount(stats.semantic_count || 0))}</div>
      <div>文风范本来源：${escapeHtml(styleSourceLabel)}</div>
    </div>
  `;
}

function renderStyleAnchor(payload) {
  const container = document.getElementById("admin-memory-style-anchor");
  if (!container) return;
  const content = String(payload?.style_anchor?.content || "").trim();
  container.textContent = content || "暂无自动总结文风。";
}

function renderSemanticMemories(payload) {
  const container = document.getElementById("admin-memory-semantic-list");
  if (!container) return;
  const items = Array.isArray(payload?.semantic_memories) ? payload.semantic_memories : [];
  if (!items.length) {
    container.innerHTML = '<div class="text-sm text-slate-500">这本书还没有语义记忆。</div>';
    return;
  }
  container.innerHTML = items
    .map(
      (item) => `
        <div class="rounded-[1rem] border border-slate-200/70 bg-white px-4 py-3">
          <div class="font-medium text-bamboo-deep">${escapeHtml(item.entity_name || "未命名实体")}</div>
          <div class="mt-2 text-sm leading-7 text-slate-600 whitespace-pre-wrap">${escapeHtml(item.core_fact || "")}</div>
          <div class="mt-2 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(formatDateTime(item.updated_at) || "未知时间")}</div>
        </div>
      `
    )
    .join("");
}

function renderEpisodicMemories(payload) {
  const container = document.getElementById("admin-memory-episodic-list");
  if (!container) return;
  const items = Array.isArray(payload?.episodic_memories) ? payload.episodic_memories : [];
  if (!items.length) {
    container.innerHTML = '<div class="text-sm text-slate-500">这本书还没有章节情景记忆。</div>';
    return;
  }
  container.innerHTML = items
    .map(
      (item) => `
        <div class="rounded-[1rem] border border-slate-200/70 bg-white px-4 py-3">
          <div class="flex flex-wrap items-center gap-2">
            <div class="font-medium text-bamboo-deep">${escapeHtml(item.chapter_title || "未命名章节")}</div>
            <div class="text-xs uppercase tracking-[0.16em] text-slate-400">#${escapeHtml(String(item.sequence_number || item.sort_order || item.chapter_id || "-"))}</div>
          </div>
          <div class="mt-2 text-sm leading-7 text-slate-600 whitespace-pre-wrap">${escapeHtml(item.summary || "")}</div>
          <div class="mt-2 text-sm text-slate-500">出场人物：${escapeHtml(item.involved_characters || "暂无")}</div>
          <div class="mt-2 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(formatDateTime(item.updated_at) || "未知时间")}</div>
        </div>
      `
    )
    .join("");
}

function applyBookMemory(payload) {
  renderMemorySummary(payload);
  renderStyleAnchor(payload);
  renderSemanticMemories(payload);
  renderEpisodicMemories(payload);
}

async function loadBookMemory(bookId) {
  if (!bookId) {
    applyBookMemory(null);
    return;
  }
  const payload = await getAdminBookMemory(bookId);
  applyBookMemory(payload);
}

function applyMemoryTabState(nextTab) {
  activeMemoryTab = nextTab || "style";
  document.querySelectorAll("[data-memory-tab]").forEach((button) => {
    const isActive = button.dataset.memoryTab === activeMemoryTab;
    button.classList.toggle("bamboo-btn-primary", isActive);
    button.classList.toggle("bamboo-btn-secondary", !isActive);
  });
  document.querySelectorAll("[data-memory-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.memoryPanel !== activeMemoryTab);
  });
}

function formatFileSize(sizeBytes) {
  const value = Number(sizeBytes || 0);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const precision = size >= 100 || unitIndex === 0 ? 0 : 1;
  return `${size.toFixed(precision)} ${units[unitIndex]}`;
}

function renderBackupSummary(status) {
  const summary = document.getElementById("admin-backup-summary");
  if (!summary) return;
  if (!status) {
    summary.innerHTML = "未获取到备份状态。";
    return;
  }
  const statusLabel = status.supported
    ? status.enabled
      ? "已启用自动备份"
      : "自动备份未启用"
    : "当前数据库暂不支持自动备份";
  summary.innerHTML = `
    <div class="space-y-2">
      <div class="font-medium text-bamboo-deep">${escapeHtml(statusLabel)}</div>
      <div>备份目录：${escapeHtml(status.backup_directory || "未设置")}</div>
      <div>备份引擎：${escapeHtml(status.database_engine || "unknown")} · ${escapeHtml(status.detail || "")}</div>
      <div>最近备份：${escapeHtml(formatDateTime(status.last_backup_at) || "暂无")}</div>
      <div>下次计划：${escapeHtml(formatDateTime(status.next_backup_at) || "未计划")}</div>
      <div>最近产物：${escapeHtml(status.latest_backup?.filename || "暂无")}</div>
      ${
        status.last_error
          ? `<div class="text-rose-600">最近错误：${escapeHtml(status.last_error)}</div>`
          : ""
      }
    </div>
  `;
}

function renderBackupRecent(status) {
  const container = document.getElementById("admin-backup-recent");
  if (!container) return;
  const items = Array.isArray(status?.recent_backups) ? status.recent_backups : [];
  if (!items.length) {
    container.innerHTML =
      '<div class="rounded-[1rem] bg-white/70 px-4 py-3 text-sm text-slate-500">暂无备份文件。</div>';
    return;
  }
  container.innerHTML = items
    .map(
      (item) => `
        <div class="rounded-[1rem] bg-white/70 px-4 py-3 text-sm text-slate-600">
          <div class="font-medium text-bamboo-deep">${escapeHtml(item.filename || "未命名备份")}</div>
          <div class="mt-1 text-xs uppercase tracking-[0.16em] text-slate-400">
            ${escapeHtml(formatFileSize(item.size_bytes))} · ${escapeHtml(formatDateTime(item.created_at))}
          </div>
          <div class="mt-3 flex gap-2">
            <button class="bamboo-btn-secondary" data-backup-download="${escapeHtml(item.filename || "")}" type="button">下载</button>
            <button class="bamboo-btn-secondary" data-backup-restore="${escapeHtml(item.filename || "")}" type="button">恢复</button>
          </div>
        </div>
      `
    )
    .join("");
}

function fillBackupForm(status) {
  document.getElementById("admin-backup-enabled").checked = Boolean(status?.enabled);
  document.getElementById("admin-backup-interval-hours").value = String(status?.interval_hours || 24);
  document.getElementById("admin-backup-retention-days").value = String(status?.retention_days || 7);
  const runButton = document.getElementById("admin-backup-run-now");
  if (runButton) {
    runButton.disabled = !status?.supported;
  }
}

function applyBackupStatus(status) {
  fillBackupForm(status);
  renderBackupSummary(status);
  renderBackupRecent(status);
}

async function handleBackupDownload(filename) {
  const blob = await downloadDatabaseBackup(filename);
  const objectUrl = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 1000);
}

function bindBackupFileActions() {
  document.querySelectorAll("[data-backup-download]").forEach((button) => {
    button.addEventListener("click", async () => {
      const filename = button.dataset.backupDownload;
      if (!filename) return;
      try {
        await handleBackupDownload(filename);
        showToast("备份文件下载已开始。");
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "下载备份失败。";
        showToast(message, "error");
      }
    });
  });

  document.querySelectorAll("[data-backup-restore]").forEach((button) => {
    button.addEventListener("click", async () => {
      const filename = button.dataset.backupRestore;
      if (!filename) return;
      const confirmed = window.confirm(
        `将使用备份 ${filename} 覆盖当前数据库。系统会先自动创建一份恢复前备份，是否继续？`
      );
      if (!confirmed) return;
      try {
        const response = await restoreDatabaseBackup({
          filename,
          create_safety_backup: true,
        });
        applyBackupStatus(response.settings || null);
        bindBackupFileActions();
        showToast(response.message || "数据库已恢复，页面即将刷新。");
        window.setTimeout(() => window.location.reload(), 1200);
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "恢复备份失败。";
        showToast(message, "error");
      }
    });
  });
}

export async function initAdminPage(user) {
  if (!isAdminRole(user.role)) {
    window.location.href = "/library";
    return;
  }

  const [usersResponse, booksResponse, snapshotsResponse, backupStatus] = await Promise.all([
    listUsers(),
    listBooks(),
    listRecentSnapshots(8),
    getDatabaseBackupSettings(),
  ]);
  const users = usersResponse.items || [];
  const books = booksResponse.items || [];
  const snapshots = snapshotsResponse.items || [];
  applyBackupStatus(backupStatus);
  bindBackupFileActions();

  renderStats(document.getElementById("admin-stats"), [
    { label: "用户总数", value: formatCount(users.length), meta: "本地账号池" },
    { label: "作者数量", value: formatCount(users.filter((item) => item.role === "author").length), meta: "Author" },
    { label: "启用账号", value: formatCount(users.filter((item) => item.is_active).length), meta: "Active" },
    { label: "书籍总数", value: formatCount(books.length), meta: "Books" },
  ]);

  document.getElementById("admin-users").innerHTML = `
    <table class="bamboo-table">
      <thead>
        <tr>
          <th>用户</th>
          <th>Email</th>
          <th>角色</th>
          <th>状态</th>
          <th>启用</th>
          <th>书籍</th>
          <th>最近登录</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>${users.map(renderUserRow).join("")}</tbody>
    </table>
  `;

  document.getElementById("admin-books").innerHTML = books.length
    ? `
      <table class="bamboo-table">
        <thead>
          <tr>
            <th>书籍</th>
            <th>章节</th>
            <th>字数</th>
            <th>当前归属</th>
            <th>更新时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>${books.map((book) => renderBookRow(book, users)).join("")}</tbody>
      </table>
    `
    : '<div class="text-sm text-slate-500">暂无书籍。</div>';

  const memoryBookSelect = document.getElementById("admin-memory-book-select");
  if (memoryBookSelect) {
    memoryBookSelect.innerHTML = books.length
      ? `
        <option value="">请选择书籍</option>
        ${books
          .map(
            (book) =>
              `<option value="${book.id}">${escapeHtml(book.title)}${book.owner_display_name ? ` · ${escapeHtml(book.owner_display_name)}` : ""}</option>`
          )
          .join("")}
      `
      : '<option value="">暂无书籍</option>';
  }
  applyMemoryTabState(activeMemoryTab);

  document.getElementById("admin-audit").innerHTML = snapshots.length
    ? snapshots
        .map(
          (item) => `
            <li class="rounded-2xl bg-white/82 px-4 py-3 text-sm leading-7 text-slate-600 shadow-cloud">
              ${escapeHtml(item.created_by_username || "system")} · ${escapeHtml(item.book_title || "")} / ${escapeHtml(item.chapter_title)}
              <div class="mt-1 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(truncate(item.label || item.kind, 30))} · ${escapeHtml(formatDateTime(item.created_at))}</div>
            </li>
          `
        )
        .join("")
    : '<li class="rounded-2xl bg-white/82 px-4 py-3 text-sm text-slate-500 shadow-cloud">暂无近期写作记录。</li>';

  document.querySelectorAll("[data-user-save]").forEach((button) => {
    button.addEventListener("click", async () => {
      const userId = Number(button.dataset.userSave);
      try {
        await updateUser(userId, {
          role: document.querySelector(`[data-user-role="${userId}"]`).value,
          status: document.querySelector(`[data-user-status="${userId}"]`).value,
          is_active: document.querySelector(`[data-user-active="${userId}"]`).checked,
        });
        showToast("用户状态已更新。");
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "更新用户失败。";
        showToast(message, "error");
      }
    });
  });

  document.querySelectorAll("[data-book-save]").forEach((button) => {
    button.addEventListener("click", async () => {
      const bookId = Number(button.dataset.bookSave);
      const ownerId = Number(document.querySelector(`[data-book-owner="${bookId}"]`).value);
      try {
        await updateBook(bookId, { owner_id: ownerId });
        showToast("书籍归属已更新。");
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "更新书籍归属失败。";
        showToast(message, "error");
      }
    });
  });

  if (memoryBookSelect) {
    document.querySelectorAll("[data-memory-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        applyMemoryTabState(button.dataset.memoryTab || "style");
      });
    });
    memoryBookSelect.addEventListener("change", async () => {
      const bookId = Number(memoryBookSelect.value);
      if (!bookId) {
        applyBookMemory(null);
        return;
      }
      try {
        await loadBookMemory(bookId);
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "加载书籍记忆失败。";
        showToast(message, "error");
      }
    });
    if (books.length) {
      memoryBookSelect.value = String(books[0].id);
      try {
        await loadBookMemory(books[0].id);
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "加载书籍记忆失败。";
        showToast(message, "error");
      }
    } else {
      applyBookMemory(null);
    }
  }

  document.getElementById("admin-create-user-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await createUser({
        username: document.getElementById("admin-new-username").value.trim(),
        password: document.getElementById("admin-new-password").value,
        display_name: document.getElementById("admin-new-display-name").value.trim() || null,
        email: document.getElementById("admin-new-email").value.trim() || null,
        role: document.getElementById("admin-new-role").value,
      });
      showToast("用户已创建。");
      window.location.reload();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "创建用户失败。";
      showToast(message, "error");
    }
  });

  document.getElementById("admin-reset-password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const userId = Number(document.getElementById("admin-reset-user-id").value);
    if (!userId) {
      showToast("请输入用户 ID。", "error");
      return;
    }
    try {
      await resetUserPassword(userId, {
        password: document.getElementById("admin-reset-password").value,
      });
      showToast("密码已重置。");
      event.target.reset();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "重置密码失败。";
      showToast(message, "error");
    }
  });

  document.getElementById("admin-backup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const intervalHours = Number(document.getElementById("admin-backup-interval-hours").value);
    const retentionDays = Number(document.getElementById("admin-backup-retention-days").value);
    if (!Number.isInteger(intervalHours) || intervalHours < 1) {
      showToast("备份间隔必须是不小于 1 的整数小时。", "error");
      return;
    }
    if (!Number.isInteger(retentionDays) || retentionDays < 1) {
      showToast("备份保留天数必须是不小于 1 的整数。", "error");
      return;
    }
    try {
      const nextStatus = await updateDatabaseBackupSettings({
        enabled: document.getElementById("admin-backup-enabled").checked,
        interval_hours: intervalHours,
        retention_days: retentionDays,
      });
      applyBackupStatus(nextStatus);
      bindBackupFileActions();
      showToast("数据库备份策略已保存。");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "保存备份策略失败。";
      showToast(message, "error");
    }
  });

  document.getElementById("admin-backup-run-now").addEventListener("click", async () => {
    try {
      const response = await runDatabaseBackupNow();
      applyBackupStatus(response.settings || null);
      bindBackupFileActions();
      showToast(response.message || "数据库备份已完成。");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "立即备份失败。";
      showToast(message, "error");
    }
  });
}
