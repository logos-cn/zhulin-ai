import { ApiError, createBook, deleteBook, listBooks, listRecentSnapshots } from "./api.js";
import { escapeHtml, formatCount, formatDateTime, renderStats, showToast, statusTone, truncate } from "./shell.js";

function renderBookCard(book) {
  return `
    <article class="bamboo-panel p-5">
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="bamboo-mini-label">${escapeHtml(book.genre || "未分类")}</div>
          <h3 class="bamboo-display mt-2 text-2xl font-semibold text-bamboo-deep">${escapeHtml(book.title)}</h3>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-medium ${statusTone(book.status)}">${escapeHtml(book.status)}</span>
      </div>
      <p class="mt-4 text-sm leading-7 text-slate-500">${escapeHtml(truncate(book.description, 130))}</p>
      <div class="mt-5 flex flex-wrap gap-2 text-xs uppercase tracking-[0.16em] text-slate-400">
        <span>${formatCount(book.chapter_count)} 章节</span>
        <span>${formatCount(book.word_count)} 字</span>
      </div>
      <div class="mt-5 flex flex-wrap gap-3">
        <a class="bamboo-btn-primary" href="/writer?book=${book.id}">进入写作</a>
        <a class="bamboo-btn-secondary" href="/settings?book=${book.id}">编辑配置</a>
        <a class="bamboo-btn-secondary" href="/history?book=${book.id}">查看版本</a>
        <button class="bamboo-btn-ghost text-rose-600" data-delete-book="${book.id}">删除</button>
      </div>
      <div class="mt-4 text-xs uppercase tracking-[0.16em] text-slate-400">更新于 ${escapeHtml(formatDateTime(book.updated_at))}</div>
    </article>
  `;
}

function renderSnapshotCard(item) {
  return `
    <a class="bamboo-panel-soft block p-4" href="/history?book=${item.book_id}&snapshot=${item.id}">
      <div class="flex items-center justify-between gap-3">
        <div class="font-medium text-bamboo-deep">${escapeHtml(item.chapter_title)}</div>
        <span class="bamboo-chip">${escapeHtml(item.label || item.kind)}</span>
      </div>
      <div class="mt-2 text-sm text-slate-500">${escapeHtml(item.book_title || "")}</div>
      <p class="mt-3 text-sm leading-7 text-slate-500">${escapeHtml(truncate(item.diff_summary || item.summary, 120))}</p>
      <div class="mt-3 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(formatDateTime(item.created_at))}</div>
    </a>
  `;
}

export async function initLibraryPage(user) {
  const [booksResponse, snapshotsResponse] = await Promise.all([listBooks(), listRecentSnapshots(8)]);
  const books = booksResponse.items || [];
  const snapshots = snapshotsResponse.items || [];

  renderStats(document.getElementById("library-stats"), [
    { label: "书籍总数", value: formatCount(books.length), meta: "当前工作台" },
    {
      label: "章节总数",
      value: formatCount(books.reduce((sum, book) => sum + (book.chapter_count || 0), 0)),
      meta: "已建立章节树",
    },
    {
      label: "累计字数",
      value: formatCount(books.reduce((sum, book) => sum + (book.word_count || 0), 0)),
      meta: "按书籍聚合",
    },
  ]);

  document.getElementById("library-current-user").textContent = user.display_name || user.username;
  document.getElementById("library-books").innerHTML = books.length
    ? books.map(renderBookCard).join("")
    : '<div class="bamboo-panel-soft p-5 text-sm text-slate-500">还没有书籍，先在右侧创建一本。</div>';
  document.getElementById("library-activity").innerHTML = snapshots.length
    ? snapshots.map(renderSnapshotCard).join("")
    : '<div class="bamboo-panel-soft p-4 text-sm text-slate-500">暂无 AI 快照。</div>';

  document.getElementById("book-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = document.getElementById("new-book-title").value.trim();
    if (!title) {
      showToast("请先填写书名。", "error");
      return;
    }

    const payload = {
      title,
      genre: document.getElementById("new-book-genre").value.trim() || null,
      description: document.getElementById("new-book-description").value.trim() || null,
    };

    try {
      const book = await createBook(payload);
      showToast("书籍已创建。");
      window.location.href = `/writer?book=${book.id}`;
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "创建书籍失败。";
      showToast(message, "error");
    }
  });

  document.querySelectorAll("[data-delete-book]").forEach((button) => {
    button.addEventListener("click", async () => {
      const bookId = Number(button.dataset.deleteBook);
      if (!window.confirm("确认删除这本书及其章节、快照吗？")) return;
      try {
        await deleteBook(bookId);
        showToast("书籍已删除。");
        window.location.reload();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "删除书籍失败。";
        showToast(message, "error");
      }
    });
  });
}
