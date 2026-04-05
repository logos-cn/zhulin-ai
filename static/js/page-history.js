import { ApiError, getSnapshot, listBooks, listSnapshots } from "./api.js";
import {
  aiTargetFieldLabel,
  escapeHtml,
  formatCount,
  formatDateTime,
  renderStats,
  setAssistantContext,
  showToast,
  truncate,
} from "./shell.js";

function getSelectedBookId(books) {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = Number(params.get("book"));
  if (fromQuery && books.some((book) => book.id === fromQuery)) return fromQuery;
  return books[0]?.id || null;
}

export async function initHistoryPage() {
  const books = (await listBooks()).items || [];
  const bookId = getSelectedBookId(books);
  const bookSelect = document.getElementById("history-book-select");

  bookSelect.innerHTML = books
    .map((book) => `<option value="${book.id}" ${book.id === bookId ? "selected" : ""}>${escapeHtml(book.title)}</option>`)
    .join("");

  if (!bookId) {
    setAssistantContext();
    document.getElementById("history-list").innerHTML = '<div class="bamboo-panel p-4 text-sm text-slate-500">暂无书籍。</div>';
    return;
  }

  bookSelect.addEventListener("change", () => {
    window.location.href = `/history?book=${bookSelect.value}`;
  });

  const snapshots = (await listSnapshots(bookId, { limit: 50 })).items || [];
  const params = new URLSearchParams(window.location.search);
  const snapshotId = Number(params.get("snapshot"));
  const active = snapshots.find((item) => item.id === snapshotId) || snapshots[0];
  setAssistantContext({
    bookId,
    chapterId: active?.chapter_id || null,
    chapterTitle: active?.chapter_title || null,
  });

  document.getElementById("history-list").innerHTML = snapshots.length
    ? snapshots
        .map(
          (item) => `
            <a class="block rounded-[1.2rem] p-4 ${item.id === active?.id ? "bg-bamboo-highlight" : "bg-white/84 shadow-cloud"}" href="/history?book=${bookId}&snapshot=${item.id}">
              <div class="font-medium text-bamboo-deep">${escapeHtml(item.label || item.kind)}</div>
              <div class="mt-2 text-sm text-slate-500">${escapeHtml(item.chapter_title)}</div>
              <div class="mt-3 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(formatDateTime(item.created_at))}</div>
            </a>
          `
        )
        .join("")
    : '<div class="bamboo-panel p-4 text-sm text-slate-500">这个项目还没有快照。</div>';

  if (!active) {
    return;
  }

  try {
    const detail = await getSnapshot(bookId, active.id);
    const targetField = detail.target_field || "content";
    const fieldLabel = aiTargetFieldLabel(targetField);
    const beforeValue = detail.before_value || "";
    const afterValue = detail.after_value || "";

    document.getElementById("history-header").innerHTML = `
      <div class="flex flex-wrap gap-3">
        <div class="bamboo-chip">${escapeHtml(detail.source_model_name || "snapshot")}</div>
        <div class="bamboo-chip">${escapeHtml(fieldLabel)}</div>
      </div>
      <h2 class="bamboo-display mt-4 text-3xl font-semibold text-bamboo-deep">${escapeHtml(detail.chapter_title)}</h2>
      <p class="mt-3 text-sm leading-7 text-slate-500">${escapeHtml(truncate(detail.diff_summary || detail.summary, 160))}</p>
    `;

    renderStats(document.getElementById("history-metrics"), [
      { label: "快照版本", value: formatCount(detail.chapter_version), meta: "修改前版本" },
      { label: `快照${fieldLabel}`, value: formatCount(detail.metrics.before_units), meta: "Before" },
      { label: `当前${fieldLabel}`, value: formatCount(detail.metrics.after_units), meta: "After" },
    ]);

    document.getElementById("history-before").innerHTML = `
      <div class="bamboo-mini-label">快照${escapeHtml(fieldLabel)}</div>
      <div class="bamboo-diff-block bamboo-diff-before mt-4 text-sm leading-7 text-slate-600">${escapeHtml(beforeValue || "暂无内容")}</div>
    `;
    document.getElementById("history-after").innerHTML = `
      <div class="bamboo-mini-label">当前${escapeHtml(fieldLabel)}</div>
      <div class="bamboo-diff-block bamboo-diff-after mt-4 text-sm leading-7 text-slate-600">${escapeHtml(afterValue || "暂无内容")}</div>
    `;
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "加载快照详情失败。";
    showToast(message, "error");
  }
}
