import {
  ApiError,
  cancelWorldExtractionJob,
  createCharacter,
  createFaction,
  createFactionMembership,
  createRelation,
  deleteAllCharacters,
  deleteAllRelations,
  deleteWorldExtractionJob,
  deleteCharacter,
  deleteFaction,
  deleteFactionMembership,
  getFaction,
  listRelationEvents,
  listFactions,
  listFactionMemberships,
  deleteRelation,
  estimateWorldExtractionDocument,
  extractWorld,
  getWorldExtractionJob,
  getBook,
  getRelation,
  listBooks,
  listCharacters,
  listChapters,
  listRelations,
  listWorldExtractionJobs,
  resumeWorldExtractionJob,
  resolveWorldExtractionConflict,
  startWorldExtractionJob,
  uploadWorldExtractionDocument,
  updateBook,
  updateCharacter,
  updateFaction,
  updateFactionMembership,
  updateRelation,
} from "./api.js";
import { escapeHtml, formatDateTime, setAssistantContext, showToast, truncate } from "./shell.js";

const WORLD_FACTS_PREVIEW_LIMIT = 24;
const GRAPH_SCALE_MIN = 0.55;
const GRAPH_SCALE_MAX = 2.6;
const GRAPH_SCALE_STEP = 0.18;
const GRAPH_NODE_WIDTH = 240;
const GRAPH_NODE_HEIGHT = 152;
const GRAPH_PADDING = 68;
const GRAPH_FORCE_ITERATIONS = 220;
const GRAPH_FORCE_ITERATIONS_MEDIUM = 148;
const GRAPH_FORCE_ITERATIONS_MEDIUM_STABLE = 88;
const GRAPH_FORCE_ITERATIONS_LARGE = 112;
const GRAPH_FORCE_ITERATIONS_LARGE_STABLE = 64;
const GRAPH_RELAYOUT_THRESHOLD = 80;
const GRAPH_SPATIAL_BUCKET_SIZE = 240;
const GRAPH_SPATIAL_BUCKET_RADIUS = 2;
let worldTimelineChapters = [];
const LIFE_STATUS_OPTIONS = [
  { value: "alive", label: "活着" },
  { value: "dead", label: "死亡" },
  { value: "serious_injury", label: "重伤" },
  { value: "minor_injury", label: "轻伤" },
  { value: "disabled", label: "残疾" },
];
const LIFE_STATUS_LABELS = LIFE_STATUS_OPTIONS.reduce((map, item) => {
  map[item.value] = item.label;
  return map;
}, {});
const RELATION_CATEGORY_DEFINITIONS = [
  {
    key: "kinship",
    label: "亲属类",
    color: "#4f7c6a",
    keywords: [],
  },
  {
    key: "affinity",
    label: "友好类",
    color: "#c06a7d",
    keywords: [],
  },
  {
    key: "hostility",
    label: "对立类",
    color: "#b45b4d",
    keywords: [],
  },
  {
    key: "authority",
    label: "权势类",
    color: "#5678a8",
    keywords: [],
  },
  {
    key: "other",
    label: "其他",
    color: "#7b8794",
    keywords: [],
  },
];
const RELATION_CATEGORY_BY_KEY = RELATION_CATEGORY_DEFINITIONS.reduce((map, item) => {
  map[item.key] = item;
  return map;
}, {});
const RELATION_IMPORTANCE_LABELS = {
  core: "核心",
  major: "主要",
  minor: "次要",
  background: "背景",
};

function getSelectedBookId(books) {
  const params = new URLSearchParams(window.location.search);
  const bookId = Number(params.get("book"));
  if (bookId && books.some((book) => book.id === bookId)) return bookId;
  return books[0]?.id || null;
}

function getSelectedChapterId(chapters) {
  const params = new URLSearchParams(window.location.search);
  const chapterId = Number(params.get("chapter"));
  if (chapterId && chapters.some((chapter) => chapter.id === chapterId)) return chapterId;
  return chapters.find((item) => item.node_type === "chapter" || item.node_type === "scene")?.id || chapters[0]?.id || null;
}

function buildRelationCountMap(relations) {
  return relations.reduce((map, relation) => {
    map.set(relation.source_character_id, (map.get(relation.source_character_id) || 0) + 1);
    map.set(relation.target_character_id, (map.get(relation.target_character_id) || 0) + 1);
    return map;
  }, new Map());
}

function canonicalRelationText(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[_\-]+/g, " ")
    .replace(/\s+/g, " ");
}

function getRelationTypeDisplay(relation) {
  return String(relation.label || relation.relation_type_label || relation.relation_type || "").trim() || "关系";
}

function classifyRelationCategory(relation) {
  return RELATION_CATEGORY_BY_KEY[relation.relation_type] || RELATION_CATEGORY_BY_KEY.other;
}

function annotateRelations(relations) {
  return relations
    .map((relation) => {
      const category = classifyRelationCategory(relation);
      const relationTypeDisplay = getRelationTypeDisplay(relation);
      return {
        ...relation,
        relation_type_display: relationTypeDisplay,
        relation_type_label: relation.relation_type_label || category.label || "其他",
        relation_type_key: canonicalRelationText(relationTypeDisplay) || `relation-${relation.id}`,
        relation_category_key: category.key,
        relation_category_label: category.label,
        relation_category_color: category.color,
        importance_label: RELATION_IMPORTANCE_LABELS[String(relation.importance_level || "").trim()] || "主要",
      };
    })
    .sort((left, right) => {
      const categoryDelta =
        RELATION_CATEGORY_DEFINITIONS.findIndex((item) => item.key === left.relation_category_key) -
        RELATION_CATEGORY_DEFINITIONS.findIndex((item) => item.key === right.relation_category_key);
      if (categoryDelta !== 0) return categoryDelta;
      return String(left.relation_type_display || "").localeCompare(String(right.relation_type_display || ""), "zh-CN");
    });
}

function buildRelationFilterMeta(relations) {
  const categories = new Map();

  relations.forEach((relation) => {
    const categoryKey = relation.relation_category_key || "other";
    const category = categories.get(categoryKey) || {
      key: categoryKey,
      label: relation.relation_category_label || RELATION_CATEGORY_BY_KEY[categoryKey]?.label || "其他",
      color: relation.relation_category_color || RELATION_CATEGORY_BY_KEY[categoryKey]?.color || "#7b8794",
      count: 0,
      types: new Map(),
    };
    category.count += 1;

    const typeKey = relation.relation_type_key || canonicalRelationText(relation.relation_type_display) || `relation-${relation.id}`;
    const type = category.types.get(typeKey) || {
      key: typeKey,
      label: relation.relation_type_display || "关系",
      count: 0,
      category_key: categoryKey,
      color: category.color,
    };
    type.count += 1;

    category.types.set(typeKey, type);
    categories.set(categoryKey, category);
  });

  return {
    relation_count: relations.length,
    categories: RELATION_CATEGORY_DEFINITIONS.map((definition) => {
      const category = categories.get(definition.key);
      if (!category) return null;
      return {
        key: definition.key,
        label: definition.label,
        color: definition.color,
        count: category.count,
        types: Array.from(category.types.values()).sort((left, right) => {
          if (right.count !== left.count) return right.count - left.count;
          return left.label.localeCompare(right.label, "zh-CN");
        }),
      };
    }).filter(Boolean),
  };
}

function syncRelationFilterState(currentState, meta) {
  const nextCategories = {};
  const nextTypes = {};

  meta.categories.forEach((category) => {
    nextCategories[category.key] = currentState.initialized ? currentState.categories[category.key] !== false : true;
    category.types.forEach((type) => {
      nextTypes[type.key] = currentState.initialized ? currentState.types[type.key] !== false : true;
    });
  });

  return {
    initialized: true,
    categories: nextCategories,
    types: nextTypes,
  };
}

function filterRelationsBySelection(relations, relationFilterState) {
  if (!relationFilterState?.initialized) return relations;
  return relations.filter((relation) => {
    const categoryVisible = relationFilterState.categories[relation.relation_category_key] !== false;
    const typeVisible = relationFilterState.types[relation.relation_type_key] !== false;
    return categoryVisible && typeVisible;
  });
}

function normalizeImportanceLevel(character) {
  const card = character.card_json || {};
  const rawValue = String(
    card.importance_level ||
      card.importance ||
      card.character_importance ||
      card.visibility_level ||
      ""
  )
    .trim()
    .toLowerCase();

  if (["major", "main", "primary", "important", "core"].includes(rawValue)) return "major";
  if (["minor", "background", "extra", "passerby", "npc"].includes(rawValue)) return "minor";
  return "";
}

function looksLikeMinorRole(character) {
  const name = String(character.name || "").trim();
  const role = String(character.role_label || "").trim();
  const text = `${name} ${role}`.toLowerCase();
  const minorKeywords = [
    "路人",
    "路人甲",
    "路人乙",
    "村民",
    "店员",
    "掌柜",
    "伙计",
    "小二",
    "护卫",
    "侍卫",
    "侍女",
    "客人",
    "行人",
    "摊主",
    "同学甲",
    "同学乙",
    "群众",
    "围观",
    "弟子甲",
    "弟子乙",
  ];
  return minorKeywords.some((keyword) => text.includes(keyword));
}

function isPrimaryCharacter(character, relationCountMap) {
  const relationCount = relationCountMap.get(character.id) || 0;
  const timelineCount = getCharacterTimelineEntries(character).length;
  const explicitLevel = normalizeImportanceLevel(character);
  if (explicitLevel === "major") return true;
  if (explicitLevel === "minor") {
    // Treat explicit minor as advisory, not absolute. Older extracted data often
    // tagged important supporting roles as minor, which hid most of the graph.
    if (looksLikeMinorRole(character) && relationCount <= 1 && timelineCount === 0) return false;
    if (relationCount >= 2) return true;
    if (timelineCount >= 1) return true;
    const roleText = `${character.role_label || ""} ${(character.card_json || {}).role_label || ""}`.toLowerCase();
    if (roleText.trim() && !looksLikeMinorRole(character)) return true;
    return false;
  }

  const roleText = `${character.role_label || ""} ${(character.card_json || {}).role_label || ""}`.toLowerCase();
  const majorKeywords = ["主角", "男主", "女主", "反派", "导师", "核心", "关键", "重要", "搭档", "队友"];
  if (majorKeywords.some((keyword) => roleText.includes(keyword))) return true;
  if (looksLikeMinorRole(character)) return false;
  if (roleText.trim()) return true;

  const descriptionLength = String(character.biography || character.description || "").trim().length;
  const secretsLength = String(character.secrets || "").trim().length;

  if (relationCount >= 1) return true;
  if (timelineCount >= 1 || secretsLength >= 4) return true;
  if (descriptionLength >= 36) return true;
  return false;
}

function annotateCharacterVisibility(characters, relationCountMap) {
  return characters
    .map((character) => {
      const timelineEntries = getCharacterTimelineEntries(character);
      const lifeStatuses = getCharacterLifeStatuses(character);
      const isPrimaryDisplay = isPrimaryCharacter(character, relationCountMap);
      const biographyText = String(character.biography || character.description || "").trim();
      const latestTimelineEntry = timelineEntries[timelineEntries.length - 1] || null;
      const latestTimelineSummary = summarizeTimelineEntry(latestTimelineEntry);
      return {
        ...character,
        life_statuses: lifeStatuses,
        timeline_entries: timelineEntries,
        latest_timeline_entry: latestTimelineEntry,
        latest_timeline_summary: latestTimelineSummary,
        biography_preview: truncate(biographyText, 180) || "未填写人物小传",
        graph_preview_text: truncate(latestTimelineSummary || biographyText, 72) || "未填写人物小传",
        is_deceased: lifeStatuses.includes("dead"),
        importance_level: normalizeImportanceLevel(character) || (isPrimaryDisplay ? "major" : "minor"),
        is_primary_display: isPrimaryDisplay,
        relation_count: relationCountMap.get(character.id) || 0,
      };
    })
    .sort((left, right) => {
      if (left.is_primary_display !== right.is_primary_display) {
        return left.is_primary_display ? -1 : 1;
      }
      return String(left.name || "").localeCompare(String(right.name || ""), "zh-CN");
    });
}

function buildVisibleWorldState(characters, relations, showMinorCharacters, relationFilterState = null) {
  const relationCountMap = buildRelationCountMap(relations);
  const annotatedCharacters = annotateCharacterVisibility(characters, relationCountMap);
  const visibleCharacters = showMinorCharacters
    ? annotatedCharacters
    : annotatedCharacters.filter((character) => character.is_primary_display);
  const visibleCharacterIds = new Set(visibleCharacters.map((character) => character.id));
  const relationCandidates = annotateRelations(
    relations.filter(
      (relation) =>
        visibleCharacterIds.has(relation.source_character_id) &&
        visibleCharacterIds.has(relation.target_character_id)
    )
  );
  const visibleRelations = filterRelationsBySelection(relationCandidates, relationFilterState);

  return {
    annotatedCharacters,
    visibleCharacters,
    relationCandidates,
    visibleRelations,
    relationFilterMeta: buildRelationFilterMeta(relationCandidates),
    hiddenMinorCount: annotatedCharacters.length - visibleCharacters.length,
    hiddenRelationCount: relationCandidates.length - visibleRelations.length,
  };
}

function relationFilterStats(meta, relationFilterState) {
  const selectedCategories = meta.categories.filter((category) => relationFilterState?.categories?.[category.key] !== false);
  const selectedTypeCount = selectedCategories.reduce(
    (total, category) => total + category.types.filter((type) => relationFilterState?.types?.[type.key] !== false).length,
    0
  );
  const totalTypeCount = meta.categories.reduce((total, category) => total + category.types.length, 0);

  return {
    selected_category_count: selectedCategories.length,
    total_category_count: meta.categories.length,
    selected_type_count: selectedTypeCount,
    total_type_count: totalTypeCount,
  };
}

function renderRelationFilterPanel(meta, relationFilterState) {
  if (!meta.categories.length) {
    return '<div class="bamboo-relation-filter-empty">当前人物范围内还没有可筛选的关系。</div>';
  }

  return `
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="text-xs uppercase tracking-[0.16em] text-slate-400">勾选后立即生效，箭头始终表示关系方向</div>
      <div class="bamboo-relation-filter-actions">
        <button class="bamboo-relation-filter-action" data-relation-filter-action="select-all" type="button">全部显示</button>
        <button class="bamboo-relation-filter-action" data-relation-filter-action="clear-all" type="button">全部隐藏</button>
      </div>
    </div>
    ${meta.categories
      .map((category) => {
        const categoryChecked = relationFilterState?.categories?.[category.key] !== false;
        return `
          <section class="bamboo-relation-filter-group">
            <div class="bamboo-relation-filter-head">
              <label class="bamboo-choice-chip bamboo-relation-filter-chip ${categoryChecked ? "is-selected" : ""}" style="--relation-color:${category.color}">
                <input data-relation-category-toggle="${category.key}" type="checkbox" ${categoryChecked ? "checked" : ""} />
                <span>${escapeHtml(category.label)}</span>
                <span class="text-xs text-slate-400">${category.count} 条</span>
              </label>
              <div class="text-xs uppercase tracking-[0.16em] text-slate-400">${category.types.length} 种关系词</div>
            </div>
            <div class="bamboo-relation-filter-types">
              ${category.types
                .map((type) => {
                  const typeChecked = relationFilterState?.types?.[type.key] !== false;
                  return `
                    <label class="bamboo-choice-chip bamboo-relation-filter-chip ${typeChecked ? "is-selected" : ""}" style="--relation-color:${category.color}">
                      <input
                        data-relation-type-toggle="${type.key}"
                        data-relation-category-key="${category.key}"
                        type="checkbox"
                        ${typeChecked ? "checked" : ""}
                      />
                      <span>${escapeHtml(type.label)}</span>
                      <span class="text-xs text-slate-400">${type.count}</span>
                    </label>
                  `;
                })
                .join("")}
            </div>
          </section>
        `;
      })
      .join("")}
  `;
}

function graphIterationBudget(characters, previousPositions = {}) {
  const count = characters.length;
  const seededCount = characters.reduce((total, character) => total + (previousPositions[character.id] ? 1 : 0), 0);
  const seededRatio = count ? seededCount / count : 0;

  if (count >= 36) {
    return seededRatio >= 0.85 ? GRAPH_FORCE_ITERATIONS_LARGE_STABLE : GRAPH_FORCE_ITERATIONS_LARGE;
  }
  if (count >= 20) {
    return seededRatio >= 0.85 ? GRAPH_FORCE_ITERATIONS_MEDIUM_STABLE : GRAPH_FORCE_ITERATIONS_MEDIUM;
  }
  return seededRatio >= 0.85 ? Math.round(GRAPH_FORCE_ITERATIONS * 0.58) : GRAPH_FORCE_ITERATIONS;
}

function buildGraphSpatialBuckets(characters, positions) {
  const buckets = new Map();
  characters.forEach((character) => {
    const point = positions[character.id];
    if (!point) return;
    const bucketX = Math.floor(point.x / GRAPH_SPATIAL_BUCKET_SIZE);
    const bucketY = Math.floor(point.y / GRAPH_SPATIAL_BUCKET_SIZE);
    const key = `${bucketX}:${bucketY}`;
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.push(character);
      return;
    }
    buckets.set(key, [character]);
  });
  return buckets;
}

function formatFileSize(size) {
  if (!Number.isFinite(size) || size <= 0) return "";
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(size < 128 * 1024 ? 0 : 1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTokenCount(value) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric) || numeric <= 0) return "0";
  if (numeric >= 10000) {
    return `${(numeric / 10000).toFixed(numeric >= 100000 ? 0 : 1)}万`;
  }
  return new Intl.NumberFormat("zh-CN").format(Math.round(numeric));
}

function validateImportFile(file) {
  if (!file) return "请先选择要导入的 TXT、DOCX 或 PDF 文件";
  const name = String(file.name || "").toLowerCase();
  const dotIndex = name.lastIndexOf(".");
  const extension = dotIndex >= 0 ? name.slice(dotIndex) : "";
  if (extension === ".doc") return "旧版 .doc 暂不支持，请先另存为 .docx 再导入";
  if (![".txt", ".docx", ".pdf"].includes(extension)) {
    return "目前只支持 TXT、DOCX 和 PDF 文件";
  }
  return "";
}

function clearImportEstimate() {
  const container = document.getElementById("world-import-estimate");
  if (!container) return;
  container.classList.add("hidden");
  container.innerHTML = "";
}

function renderImportEstimate(estimate) {
  const container = document.getElementById("world-import-estimate");
  if (!container) return;
  const assumptions = Array.isArray(estimate?.assumptions) ? estimate.assumptions : [];
  container.innerHTML = `
    <div class="space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="font-medium text-bamboo-deep">导入预估</div>
        <div class="text-xs uppercase tracking-[0.16em] text-slate-400">按当前配置估算</div>
      </div>
      <div class="grid gap-3 md:grid-cols-2">
        <div class="rounded-[0.8rem] bg-white/80 p-3">
          <div class="text-xs uppercase tracking-[0.16em] text-slate-400">预计分段</div>
          <div class="mt-1 text-lg font-semibold text-bamboo-deep">${escapeHtml(String(estimate.estimated_segment_count || 0))}</div>
          <div class="mt-1 text-xs text-slate-500">原始碎段 ${escapeHtml(String(estimate.raw_block_count || 0))}，合并后块数 ${escapeHtml(
            String(estimate.coalesced_block_count || 0)
          )}</div>
        </div>
        <div class="rounded-[0.8rem] bg-white/80 p-3">
          <div class="text-xs uppercase tracking-[0.16em] text-slate-400">预计 Token</div>
          <div class="mt-1 text-lg font-semibold text-bamboo-deep">${escapeHtml(
            `${formatTokenCount(estimate.estimated_total_tokens_low)} - ${formatTokenCount(estimate.estimated_total_tokens_high)}`
          )}</div>
          <div class="mt-1 text-xs text-slate-500">模型调用约 ${escapeHtml(String(estimate.estimated_model_call_count || 0))} 次</div>
        </div>
      </div>
      <div class="grid gap-3 md:grid-cols-3">
        <div class="rounded-[0.8rem] bg-white/80 p-3">
          <div class="text-xs uppercase tracking-[0.16em] text-slate-400">文件大小</div>
          <div class="mt-1 font-medium text-bamboo-deep">${escapeHtml(formatFileSize(estimate.file_size_bytes) || "未知")}</div>
        </div>
        <div class="rounded-[0.8rem] bg-white/80 p-3">
          <div class="text-xs uppercase tracking-[0.16em] text-slate-400">正文计量</div>
          <div class="mt-1 font-medium text-bamboo-deep">${escapeHtml(formatTokenCount(estimate.estimated_text_units || 0))}</div>
        </div>
        <div class="rounded-[0.8rem] bg-white/80 p-3">
          <div class="text-xs uppercase tracking-[0.16em] text-slate-400">单段目标</div>
          <div class="mt-1 font-medium text-bamboo-deep">${escapeHtml(formatTokenCount(estimate.segment_unit_limit || 0))}</div>
        </div>
      </div>
      ${
        assumptions.length
          ? `<div class="rounded-[0.8rem] bg-white/70 p-3 text-xs leading-6 text-slate-500">${assumptions
              .map((item) => `- ${escapeHtml(String(item || ""))}`)
              .join("<br />")}</div>`
          : ""
      }
    </div>
  `;
  container.classList.remove("hidden");
}

function canonicalWorldFact(value) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, " ")
    .replace(/^[\s\-*•—]+/, "")
    .replace(/[\s,.;:!?、。，；：！？]+$/g, "")
    .replace(/[\s,.;:!?、。，；：！？'"“”‘’（）()【】\[\]\-_*]+/g, "")
    .toLowerCase();
}

function normalizeWorldFacts(worldBible) {
  const seen = new Set();
  return String(worldBible || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => {
      const key = canonicalWorldFact(line);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function removeWorldFactFromBible(worldBible, factToRemove) {
  const targetKey = canonicalWorldFact(factToRemove);
  if (!targetKey) return String(worldBible || "");

  return String(worldBible || "")
    .split(/\r?\n/)
    .filter((line) => canonicalWorldFact(line) !== targetKey)
    .join("\n")
    .trim();
}

function normalizeLifeStatuses(rawValue) {
  const source = Array.isArray(rawValue)
    ? rawValue
    : typeof rawValue === "string"
      ? rawValue.split(/[\s、，,/\n|]+/)
      : [];
  const aliases = {
    "活着": "alive",
    alive: "alive",
    "生还": "alive",
    "存活": "alive",
    "死亡": "dead",
    dead: "dead",
    deceased: "dead",
    "已死": "dead",
    "重伤": "serious_injury",
    serious_injury: "serious_injury",
    seriously_injured: "serious_injury",
    "轻伤": "minor_injury",
    minor_injury: "minor_injury",
    lightly_injured: "minor_injury",
    "残疾": "disabled",
    disabled: "disabled",
  };
  const normalized = [];
  const customStatuses = [];
  source.forEach((item) => {
    const text = String(item || "").trim();
    if (!text) return;
    const key = text.toLowerCase();
    const value = aliases[key] || aliases[text] || "";
    if (value) {
      if (!normalized.includes(value)) {
        normalized.push(value);
      }
      return;
    }
    if (!customStatuses.includes(text)) {
      customStatuses.push(text);
    }
  });
  if (normalized.includes("dead")) {
    return normalized.filter((item) => item !== "alive").concat(customStatuses);
  }
  return normalized.concat(customStatuses);
}

function getCharacterLifeStatuses(character) {
  const card = character?.card_json || {};
  return normalizeLifeStatuses(
    character?.life_statuses || card.life_statuses || card.life_status || card.life_state || card.status_tags || []
  );
}

function getCharacterTimelineEntries(character) {
  const rawEntries =
    character?.timeline_entries ||
    character?.card_json?.timeline_entries ||
    character?.card_json?.chapter_timeline ||
    [];
  if (!Array.isArray(rawEntries)) return [];

  return rawEntries
    .map((item) => {
      if (!item || typeof item !== "object") return null;
      const chapterNumber = Number(item.chapter_number || item.chapter || item.sequence_number || 0);
      const event = String(item.event || item.summary || item.action || "").trim();
      const location = String(item.location || item.place || "").trim();
      const status = String(item.status || item.state || "").trim();
      const notes = String(item.notes || item.task || "").trim();
      if (!Number.isFinite(chapterNumber) || chapterNumber <= 0) return null;
      if (!event && !location && !status && !notes) return null;
      return {
        chapter_id: Number(item.chapter_id || 0) || undefined,
        chapter_number: chapterNumber,
        chapter_label: String(item.chapter_label || item.chapter_title || `第${chapterNumber}章`).trim(),
        event,
        location,
        status,
        notes,
      };
    })
    .filter(Boolean)
    .sort((left, right) => left.chapter_number - right.chapter_number);
}

function characterIsDead(character) {
  return getCharacterLifeStatuses(character).includes("dead");
}

function renderLifeStatusChips(statuses, { compact = false } = {}) {
  if (!statuses.length) {
    return `<span class="bamboo-status-chip ${compact ? "compact" : ""}" data-tone="default">未标注</span>`;
  }

  return statuses
    .map((status) => {
      const tone = status === "dead" ? "muted" : status === "serious_injury" ? "danger" : "default";
      return `<span class="bamboo-status-chip ${compact ? "compact" : ""}" data-tone="${tone}">${escapeHtml(
        LIFE_STATUS_LABELS[status] || status
      )}</span>`;
    })
    .join("");
}

function graphNodeWeight(character, relationCountMap) {
  const relationCount = relationCountMap.get(character.id) || 0;
  const primaryBonus = character.is_primary_display ? 1.2 : 0.6;
  const deathPenalty = characterIsDead(character) ? -0.25 : 0;
  return relationCount + primaryBonus + deathPenalty;
}

function summarizeTimelineEntry(entry) {
  if (!entry) return "";
  const meta = [entry.location, entry.status].filter(Boolean).join(" · ");
  return `${entry.chapter_label || `第${entry.chapter_number}章`}：${entry.event || entry.notes || "未记录"}${
    meta ? ` · ${meta}` : ""
  }`;
}

function renderTimelineItems(entries, { limit = 4, emptyText = "未记录章节时间节点" } = {}) {
  const visibleEntries = (entries || []).slice(-limit);
  if (!visibleEntries.length) {
    return `<div class="text-sm text-slate-400">${escapeHtml(emptyText)}</div>`;
  }

  return `
    <ul class="space-y-2">
      ${visibleEntries
        .map(
          (entry) => `
            <li class="rounded-2xl bg-bamboo-highlight/45 px-3 py-3 text-sm leading-6 text-slate-600">
              <div class="font-medium text-bamboo-deep">${escapeHtml(entry.chapter_label || `第${entry.chapter_number}章`)}</div>
              <div class="mt-1">${escapeHtml(entry.event || entry.notes || "未记录事件")}</div>
              ${
                entry.location || entry.status
                  ? `<div class="mt-1 text-xs uppercase tracking-[0.14em] text-slate-400">${escapeHtml(
                      [entry.location, entry.status].filter(Boolean).join(" · ")
                    )}</div>`
                  : ""
              }
            </li>
          `
        )
        .join("")}
    </ul>
  `;
}

function renderTimelinePreview(entries = []) {
  const container = document.getElementById("character-timeline-preview");
  if (!container) return;
  const count = Array.isArray(entries) ? entries.length : 0;
  if (!count) {
    container.innerHTML =
      '<div class="rounded-2xl bg-white/72 px-4 py-3 text-sm text-slate-500">还没有时间节点。点击“管理时间节点”后可在二级弹窗中维护。</div>';
    return;
  }

  container.innerHTML = `
    <div class="rounded-2xl bg-white/78 px-4 py-3 shadow-cloud">
      <div class="text-sm text-slate-600">当前已记录 ${count} 条时间节点</div>
      <div class="mt-3">${renderTimelineItems(entries, { limit: 3, emptyText: "还没有可预览的时间节点" })}</div>
    </div>
  `;
}

function measureGraphBoundingBox(characters, positions) {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;

  characters.forEach((character) => {
    const point = positions[character.id];
    if (!point) return;
    minX = Math.min(minX, point.x - GRAPH_NODE_WIDTH / 2);
    maxX = Math.max(maxX, point.x + GRAPH_NODE_WIDTH / 2);
    minY = Math.min(minY, point.y - GRAPH_NODE_HEIGHT / 2);
    maxY = Math.max(maxY, point.y + GRAPH_NODE_HEIGHT / 2);
  });

  if (!Number.isFinite(minX) || !Number.isFinite(maxX) || !Number.isFinite(minY) || !Number.isFinite(maxY)) {
    return null;
  }

  return {
    minX,
    maxX,
    minY,
    maxY,
    width: Math.max(1, maxX - minX),
    height: Math.max(1, maxY - minY),
  };
}

function resolveGraphBounds(graphElement, characters) {
  const rect = graphElement?.getBoundingClientRect?.() || { width: 0, height: 0 };
  const viewportWidth = Math.max(Math.round(rect.width || 0), 860);
  const viewportHeight = Math.max(Math.round(rect.height || 0), 520);
  const stageWidth = Math.max(
    Math.round(viewportWidth * 1.65),
    Math.min(2600, 980 + Math.max(0, characters.length - 1) * 150)
  );
  const stageHeight = Math.max(
    Math.round(viewportHeight * 1.55),
    Math.min(1900, 760 + Math.max(0, characters.length - 1) * 110)
  );

  return {
    viewportWidth,
    viewportHeight,
    stageWidth,
    stageHeight,
    minX: GRAPH_NODE_WIDTH / 2 + GRAPH_PADDING,
    maxX: stageWidth - GRAPH_NODE_WIDTH / 2 - GRAPH_PADDING,
    minY: GRAPH_NODE_HEIGHT / 2 + GRAPH_PADDING,
    maxY: stageHeight - GRAPH_NODE_HEIGHT / 2 - GRAPH_PADDING,
    centerX: stageWidth / 2,
    centerY: stageHeight / 2,
  };
}

function clampGraphPoint(point, bounds) {
  return {
    x: Math.min(bounds.maxX, Math.max(bounds.minX, point.x)),
    y: Math.min(bounds.maxY, Math.max(bounds.minY, point.y)),
  };
}

function buildGraphLayoutSignature(characters, relations) {
  return [
    characters.map((item) => item.id).join(","),
    relations.map((item) => `${item.id}:${item.source_character_id}-${item.target_character_id}`).join(","),
  ].join("|");
}

function buildInitialGraphPositions(characters, relations, bounds, previousPositions = {}) {
  const relationCountMap = buildRelationCountMap(relations);
  const positions = {};
  const sortedCharacters = [...characters].sort((left, right) => {
    const weightDelta = graphNodeWeight(right, relationCountMap) - graphNodeWeight(left, relationCountMap);
    if (Math.abs(weightDelta) > 0.001) return weightDelta;
    return String(left.name || "").localeCompare(String(right.name || ""), "zh-CN");
  });

  sortedCharacters.forEach((character, index) => {
    const previous = previousPositions[character.id];
    if (previous) {
      positions[character.id] = clampGraphPoint(previous, bounds);
      return;
    }

    const angle = index * 2.399963229728653;
    const radius = 110 + Math.sqrt(index + 1) * 110;
    const ellipseX = Math.min(bounds.stageWidth * 0.34, 520);
    const ellipseY = Math.min(bounds.stageHeight * 0.3, 380);
    positions[character.id] = clampGraphPoint(
      {
        x: bounds.centerX + Math.cos(angle) * Math.min(radius, ellipseX),
        y: bounds.centerY + Math.sin(angle) * Math.min(radius * 0.84, ellipseY),
      },
      bounds
    );
  });

  return positions;
}

function buildGraphNodeRadiusMap(characters, relations) {
  const degreeMap = buildRelationCountMap(relations);
  return new Map(
    characters.map((character) => [
      character.id,
      (character.is_primary_display ? 74 : 64) + Math.min(16, (degreeMap.get(character.id) || 0) * 3),
    ])
  );
}

function buildRelationCurveMeta(relations) {
  const buckets = new Map();

  relations.forEach((relation) => {
    const sourceId = Number(relation.source_character_id || 0);
    const targetId = Number(relation.target_character_id || 0);
    const pairKey = sourceId <= targetId ? `${sourceId}:${targetId}` : `${targetId}:${sourceId}`;
    const bucket = buckets.get(pairKey) || [];
    bucket.push(relation.id);
    buckets.set(pairKey, bucket);
  });

  const meta = new Map();
  buckets.forEach((ids) => {
    const center = (ids.length - 1) / 2;
    ids.forEach((id, index) => {
      meta.set(id, {
        pair_offset_index: index - center,
        pair_count: ids.length,
      });
    });
  });
  return meta;
}

function describeRelationDirection(relation) {
  return relation.is_bidirectional ? "双向" : "单向";
}

function quadraticPoint(start, control, end, t) {
  const mt = 1 - t;
  return {
    x: mt * mt * start.x + 2 * mt * t * control.x + t * t * end.x,
    y: mt * mt * start.y + 2 * mt * t * control.y + t * t * end.y,
  };
}

function quadraticTangent(start, control, end, t) {
  return {
    x: 2 * (1 - t) * (control.x - start.x) + 2 * t * (end.x - control.x),
    y: 2 * (1 - t) * (control.y - start.y) + 2 * t * (end.y - control.y),
  };
}

function relationLabelWidth(label) {
  const charWeight = Array.from(String(label || "")).reduce((total, char) => total + (/[\u4e00-\u9fff]/.test(char) ? 1.7 : 1), 0);
  return Math.max(72, Math.min(188, 28 + charWeight * 8.4));
}

function resolveRelationGeometry(relation, positions, nodeRadiusById, curveMeta = {}) {
  const from = positions[relation.source_character_id];
  const to = positions[relation.target_character_id];
  if (!from || !to) return null;

  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const distance = Math.hypot(dx, dy) || 0.001;
  const nx = dx / distance;
  const ny = dy / distance;
  const normalX = -ny;
  const normalY = nx;
  const sourceInset = Math.min((nodeRadiusById.get(relation.source_character_id) || 72) + 8, distance * 0.3);
  const targetInset = Math.min((nodeRadiusById.get(relation.target_character_id) || 72) + 18, distance * 0.34);
  const start = {
    x: from.x + nx * sourceInset,
    y: from.y + ny * sourceInset,
  };
  const end = {
    x: to.x - nx * targetInset,
    y: to.y - ny * targetInset,
  };

  const pairOffset = Number(curveMeta.pair_offset_index || 0);
  const curvatureBase = Math.min(132, Math.max(34, distance * 0.18));
  const curvature = pairOffset * 46 + (pairOffset === 0 ? 22 : 0);
  const control = {
    x: (start.x + end.x) / 2 + normalX * Math.min(curvatureBase, Math.abs(curvature)) * Math.sign(curvature || 1),
    y: (start.y + end.y) / 2 + normalY * Math.min(curvatureBase, Math.abs(curvature)) * Math.sign(curvature || 1),
  };
  const curvePoint = quadraticPoint(start, control, end, 0.5);
  const tangent = quadraticTangent(start, control, end, 0.5);
  const tangentDistance = Math.hypot(tangent.x, tangent.y) || 1;
  const labelNormalX = -tangent.y / tangentDistance;
  const labelNormalY = tangent.x / tangentDistance;
  const labelLift = 26 + Math.min(28, Math.abs(pairOffset) * 10);
  const labelCenter = {
    x: curvePoint.x + labelNormalX * labelLift,
    y: curvePoint.y + labelNormalY * labelLift,
  };
  const labelPrimary = relation.relation_type_display || "关系";
  const labelSecondary = relation.relation_category_label || describeRelationDirection(relation);
  const labelWidth = relationLabelWidth(labelPrimary);
  const labelHeight = labelSecondary ? 34 : 24;

  return {
    path: `M ${start.x.toFixed(2)} ${start.y.toFixed(2)} Q ${control.x.toFixed(2)} ${control.y.toFixed(2)} ${end.x.toFixed(2)} ${end.y.toFixed(2)}`,
    label_center: labelCenter,
    label_width: labelWidth,
    label_height: labelHeight,
    label_primary: labelPrimary,
    label_secondary: labelSecondary,
  };
}

function computeGraphLayout(characters, relations, graphElement, previousPositions = {}) {
  const bounds = resolveGraphBounds(graphElement, characters);
  if (!characters.length) {
    return { bounds, positions: {} };
  }

  if (characters.length === 1) {
    return {
      bounds,
      positions: {
        [characters[0].id]: { x: bounds.centerX, y: bounds.centerY },
      },
    };
  }

  const positions = buildInitialGraphPositions(characters, relations, bounds, previousPositions);
  const nodeRadiusById = buildGraphNodeRadiusMap(characters, relations);
  const iterationBudget = graphIterationBudget(characters, previousPositions);

  for (let iteration = 0; iteration < iterationBudget; iteration += 1) {
    const cooling = 1 - iteration / iterationBudget;
    const delta = new Map(characters.map((character) => [character.id, { x: 0, y: 0 }]));
    const buckets = buildGraphSpatialBuckets(characters, positions);

    for (let index = 0; index < characters.length; index += 1) {
      const left = characters[index];
      const leftPoint = positions[left.id];
      const bucketX = Math.floor(leftPoint.x / GRAPH_SPATIAL_BUCKET_SIZE);
      const bucketY = Math.floor(leftPoint.y / GRAPH_SPATIAL_BUCKET_SIZE);

      for (let offsetX = -GRAPH_SPATIAL_BUCKET_RADIUS; offsetX <= GRAPH_SPATIAL_BUCKET_RADIUS; offsetX += 1) {
        for (let offsetY = -GRAPH_SPATIAL_BUCKET_RADIUS; offsetY <= GRAPH_SPATIAL_BUCKET_RADIUS; offsetY += 1) {
          const bucket = buckets.get(`${bucketX + offsetX}:${bucketY + offsetY}`);
          if (!bucket) continue;

          for (let candidateIndex = 0; candidateIndex < bucket.length; candidateIndex += 1) {
            const right = bucket[candidateIndex];
            if (right.id <= left.id) continue;
            const rightPoint = positions[right.id];
            const dx = rightPoint.x - leftPoint.x;
            const dy = rightPoint.y - leftPoint.y;
            const distance = Math.hypot(dx, dy) || 0.001;
            const nx = dx / distance;
            const ny = dy / distance;
            const minimumDistance = (nodeRadiusById.get(left.id) || 72) + (nodeRadiusById.get(right.id) || 72) + 34;
            const repulsion = Math.max(0, minimumDistance * 2.1 - distance) * 0.06;
            const overlap = Math.max(0, minimumDistance - distance) * 0.42;
            const push = repulsion + overlap;

            if (push <= 0) continue;

            const leftDelta = delta.get(left.id);
            const rightDelta = delta.get(right.id);
            leftDelta.x -= nx * push;
            leftDelta.y -= ny * push;
            rightDelta.x += nx * push;
            rightDelta.y += ny * push;
          }
        }
      }
    }

    relations.forEach((relation) => {
      const from = positions[relation.source_character_id];
      const to = positions[relation.target_character_id];
      if (!from || !to) return;

      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const distance = Math.hypot(dx, dy) || 0.001;
      const nx = dx / distance;
      const ny = dy / distance;
      const strength = Number.isFinite(relation.strength) ? relation.strength : 0.5;
      const targetDistance = 220 + (1 - Math.max(0, Math.min(1, strength))) * 80;
      const pull = (distance - targetDistance) * 0.013;

      const sourceDelta = delta.get(relation.source_character_id);
      const targetDelta = delta.get(relation.target_character_id);
      sourceDelta.x += nx * pull;
      sourceDelta.y += ny * pull;
      targetDelta.x -= nx * pull;
      targetDelta.y -= ny * pull;
    });

    characters.forEach((character) => {
      const point = positions[character.id];
      const drift = delta.get(character.id);
      const centerPull = character.is_primary_display ? 0.006 : 0.004;
      drift.x += (bounds.centerX - point.x) * centerPull;
      drift.y += (bounds.centerY - point.y) * centerPull;

      positions[character.id] = clampGraphPoint(
        {
          x: point.x + drift.x * (0.8 + cooling * 0.9),
          y: point.y + drift.y * (0.8 + cooling * 0.9),
        },
        bounds
      );
    });
  }

  return { bounds, positions };
}

function renderGraph(characters, relations, graphLayout, viewport) {
  if (!characters.length) {
    return '<div class="bamboo-graph-empty text-sm">当前筛选下没有显示人物，点上方按钮可切换显示次要人物。</div>';
  }

  const { positions, bounds } = graphLayout;
  const nodeRadiusById = buildGraphNodeRadiusMap(characters, relations);
  const relationCurveMeta = buildRelationCurveMeta(relations);
  const relationMarkers = RELATION_CATEGORY_DEFINITIONS.map(
    (category) => `
      <marker
        id="bamboo-relation-arrow-${category.key}"
        markerWidth="12"
        markerHeight="12"
        refX="10"
        refY="6"
        orient="auto-start-reverse"
        markerUnits="strokeWidth"
      >
        <path d="M 0 1 L 10 6 L 0 11 z" fill="${category.color}"></path>
      </marker>
    `
  ).join("");
  const relationEdges = relations
    .map((relation) => {
      const geometry = resolveRelationGeometry(relation, positions, nodeRadiusById, relationCurveMeta.get(relation.id));
      if (!geometry) return "";
      const strokeWidth = 2 + Math.max(0, Math.min(1, Number(relation.strength) || 0)) * 1.4;
      const tooltip = `${relation.source_character_name || "人物"} ${
        relation.is_bidirectional ? "⇄" : "→"
      } ${relation.target_character_name || "人物"} · ${relation.relation_type_display || "关系"}${
        relation.description ? ` · ${relation.description}` : ""
      }`;
      return `
        <g
          class="bamboo-relation-edge"
          data-graph-relation-edge="${relation.id}"
          data-graph-relation-line="${relation.id}"
          data-graph-relation-source="${relation.source_character_id}"
          data-graph-relation-target="${relation.target_character_id}"
          style="--relation-color:${relation.relation_category_color || "#7b8794"}"
        >
          <path
            class="bamboo-relation-line"
            d="${geometry.path}"
            stroke-width="${strokeWidth.toFixed(2)}"
            marker-end="url(#bamboo-relation-arrow-${relation.relation_category_key || "other"})"
            ${relation.is_bidirectional ? `marker-start="url(#bamboo-relation-arrow-${relation.relation_category_key || "other"})"` : ""}
          >
            <title>${escapeHtml(tooltip)}</title>
          </path>
          <g
            class="bamboo-relation-label-group"
            data-graph-relation-label="${relation.id}"
            transform="translate(${geometry.label_center.x.toFixed(2)} ${geometry.label_center.y.toFixed(2)})"
          >
            <rect
              class="bamboo-relation-label-box"
              x="${(-geometry.label_width / 2).toFixed(2)}"
              y="${(-geometry.label_height / 2).toFixed(2)}"
              width="${geometry.label_width.toFixed(2)}"
              height="${geometry.label_height.toFixed(2)}"
              rx="999"
              ry="999"
            ></rect>
            ${
              geometry.label_secondary
                ? `<text class="bamboo-relation-label-category" x="0" y="-6">${escapeHtml(geometry.label_secondary)}</text>`
                : ""
            }
            <text class="bamboo-relation-label-text" x="0" y="${geometry.label_secondary ? "7" : "0"}">${escapeHtml(
              geometry.label_primary
            )}</text>
          </g>
        </g>
      `;
    })
    .join("");

  const nodes = characters
    .map((character) => {
      const position = positions[character.id];
      const lifeStatuses = character.life_statuses || [];
      const dead = Boolean(character.is_deceased);
      return `
        <div
          class="bamboo-node ${dead ? "is-dead" : ""}"
          data-graph-node="${character.id}"
          style="left:${position.x}px;top:${position.y}px;"
          title="按住可拖动人物节点"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="font-medium text-bamboo-deep">${escapeHtml(character.name)}</div>
              <div class="mt-1 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(character.role_label || "未设角色")}</div>
            </div>
            <div class="bamboo-node-meta">${character.is_primary_display ? "主要" : "次要"}</div>
          </div>
          <div class="bamboo-node-statuses">${renderLifeStatusChips(lifeStatuses, { compact: true })}</div>
          <div class="mt-3 text-sm text-slate-500">${escapeHtml(character.graph_preview_text || "未填写人物小传")}</div>
        </div>
      `;
    })
    .join("");

  return `
    <div
      data-graph-canvas
      class="bamboo-graph-stage"
      style="width:${bounds.stageWidth}px;height:${bounds.stageHeight}px;transform: translate(${viewport.pan.x}px, ${viewport.pan.y}px) scale(${viewport.scale});"
    >
      <svg class="bamboo-graph-edges" viewBox="0 0 ${bounds.stageWidth} ${bounds.stageHeight}" preserveAspectRatio="none">
        <defs>${relationMarkers}</defs>
        ${relationEdges}
      </svg>
      <div class="bamboo-graph-node-layer">${nodes}</div>
    </div>
    <div class="bamboo-graph-hint">悬停可高亮关系 · 拖人物可重排 · 拖空白处平移 · 滚轮缩放</div>
  `;
}

function renderCharacterCard(character) {
  const lifeStatuses = character.life_statuses || [];
  const dead = Boolean(character.is_deceased);
  const timelineEntries = character.timeline_entries || [];
  return `
    <article class="bamboo-panel p-5 ${dead ? "bamboo-character-card-dead" : ""}">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h3 class="font-medium text-bamboo-deep">${escapeHtml(character.name)}</h3>
          <div class="mt-1 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(character.role_label || "未设角色")}</div>
        </div>
        <span class="bamboo-chip">${character.is_primary_display ? "主要人物" : "次要人物"}</span>
      </div>
      <div class="mt-3 flex flex-wrap gap-2">${renderLifeStatusChips(lifeStatuses)}</div>
      <div class="mt-4">
        <div class="text-xs uppercase tracking-[0.16em] text-slate-400">人物小传</div>
        <p class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(character.biography_preview || "未填写人物小传")}</p>
      </div>
      <div class="mt-4">
        <div class="text-xs uppercase tracking-[0.16em] text-slate-400">章节时间节点</div>
        <div class="mt-2">${renderTimelineItems(timelineEntries, { limit: 3 })}</div>
      </div>
      <div class="mt-4 flex gap-3">
        <button class="bamboo-btn-secondary" data-edit-character="${character.id}" type="button">编辑</button>
        <button class="bamboo-btn-ghost text-rose-600" data-delete-character="${character.id}" type="button">删除</button>
      </div>
    </article>
  `;
}

function renderRelationCard(relation) {
  return `
    <div class="rounded-[1.1rem] bg-white/82 p-4 shadow-cloud">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="font-medium text-bamboo-deep">${escapeHtml(relation.source_character_name)} ${relation.is_bidirectional ? "⇄" : "→"} ${escapeHtml(relation.target_character_name)}</div>
        <div class="flex flex-wrap gap-2">
          <span class="bamboo-status-chip" style="background:${relation.relation_category_color || "rgba(220, 229, 218, 0.92)"}20;color:${relation.relation_category_color || "#516160"}">${escapeHtml(relation.relation_category_label || "其他")}</span>
          <span class="bamboo-status-chip">${escapeHtml(relation.importance_label || "主要")}</span>
        </div>
      </div>
      <div class="mt-2 text-sm text-slate-500">${escapeHtml(relation.relation_type_display || relation.label || relation.relation_type || "关系")}</div>
      ${
        relation.description
          ? `<div class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(truncate(relation.description, 88))}</div>`
          : ""
      }
      ${
        relation.latest_event_summary
          ? `<div class="mt-2 text-xs leading-6 text-slate-400">最近变化：${escapeHtml(truncate(relation.latest_event_summary, 72))}</div>`
          : ""
      }
      <div class="mt-2 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(formatDateTime(relation.updated_at))}</div>
      <div class="mt-3 flex gap-3">
        <button class="bamboo-btn-secondary" data-view-relation="${relation.id}" type="button">查看时间线</button>
        <button class="bamboo-btn-secondary" data-edit-relation="${relation.id}" type="button">编辑</button>
        <button class="bamboo-btn-ghost text-rose-600" data-delete-relation="${relation.id}" type="button">删除</button>
      </div>
    </div>
  `;
}

function renderChapterExtractionResult(result) {
  return `
    <div class="space-y-3">
      <div class="text-sm text-slate-600">人物：新增 ${result.created_character_count}，更新 ${result.updated_character_count}</div>
      <div class="text-sm text-slate-600">关系：新增 ${result.created_relation_count}，更新 ${result.updated_relation_count}</div>
      <div class="text-sm text-slate-600">阵营：新增 ${result.created_faction_count || 0}，更新 ${result.updated_faction_count || 0}</div>
      <div class="text-sm text-slate-600">事实：抽取 ${result.world_facts.length} 条，追加 ${result.world_facts_appended.length} 条</div>
    </div>
  `;
}

function renderFactionCard(faction, memberships) {
  const relatedMemberships = memberships.filter((item) => item.faction_id === faction.id);
  return `
    <div class="rounded-[1.1rem] bg-white/82 p-4 shadow-cloud">
      <div class="flex items-center justify-between gap-3">
        <div class="font-medium text-bamboo-deep">${escapeHtml(faction.name || "未命名阵营")}</div>
        <span class="bamboo-status-chip" style="background:${escapeHtml(faction.color || "#6b8d52")}20;color:${escapeHtml(faction.color || "#6b8d52")}">成员 ${relatedMemberships.length}</span>
      </div>
      ${faction.description ? `<div class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(truncate(faction.description, 88))}</div>` : ""}
      ${
        relatedMemberships.length
          ? `<div class="mt-3 flex flex-wrap gap-2">${relatedMemberships
              .map(
                (item) =>
                  `<span class="bamboo-status-chip">${escapeHtml(item.character_name || "未知人物")}${item.role_label ? ` · ${escapeHtml(item.role_label)}` : ""}</span>`
              )
              .join("")}</div>`
          : '<div class="mt-3 text-xs uppercase tracking-[0.16em] text-slate-400">暂无成员</div>'
      }
      <div class="mt-3 flex gap-3">
        <button class="bamboo-btn-secondary" data-view-faction="${faction.id}" type="button">查看详情</button>
        <button class="bamboo-btn-secondary" data-edit-faction="${faction.id}" type="button">编辑</button>
        <button class="bamboo-btn-ghost text-rose-600" data-delete-faction="${faction.id}" type="button">删除</button>
      </div>
    </div>
  `;
}

function renderBatchExtractionResult(result) {
  const chapterScopeLabel = result.chapter_scope === "all" ? "全书所有章节" : "仅处理有内容的章节";
  if (!result.selected_chapter_count) {
    return `
      <div class="space-y-3">
        <div class="text-sm text-slate-600">当前筛选下没有可提取章节。</div>
        <div class="text-xs uppercase tracking-[0.16em] text-slate-400">范围：${escapeHtml(chapterScopeLabel)}</div>
      </div>
    `;
  }

  const errors = (result.errors || [])
    .slice(0, 5)
    .map(
      (item) => `
        <li class="rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">
          <div class="font-medium">${escapeHtml(item.chapter_title || `章节 #${item.chapter_id}`)}</div>
          <div class="mt-1 text-xs leading-6">${escapeHtml(item.detail || "处理失败")}</div>
        </li>
      `
    )
    .join("");

  return `
    <div class="space-y-4">
      <div class="grid gap-3 md:grid-cols-2">
        <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">章节：处理 ${result.processed_chapter_count}，失败 ${result.failed_chapter_count}，跳过 ${result.skipped_chapter_count}</div>
        <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">人物：新增 ${result.totals.created_character_count}，更新 ${result.totals.updated_character_count}</div>
        <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">关系：新增 ${result.totals.created_relation_count}，更新 ${result.totals.updated_relation_count}</div>
        <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">阵营：新增 ${result.totals.created_faction_count || 0}，更新 ${result.totals.updated_faction_count || 0}</div>
        <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">事实：抽取 ${result.totals.world_facts_count} 条，追加 ${result.totals.world_facts_appended_count} 条</div>
      </div>
      ${
        errors
          ? `<div>
              <div class="mb-2 text-xs uppercase tracking-[0.16em] text-rose-500">失败章节</div>
              <ul class="space-y-2">${errors}</ul>
            </div>`
          : '<div class="text-sm text-emerald-600">批量提取已全部完成，没有失败章节。</div>'
      }
    </div>
  `;
}

function worldJobStatusLabel(job) {
  if (job?.is_terminated) return "已终止";
  if (job?.cancel_requested) return "终止中";
  if (job?.status === "pending") return "排队中";
  if (job?.status === "running") return "进行中";
  if (job?.status === "completed") return "已完成";
  if (job?.status === "failed") return "失败";
  return job?.status || "未知";
}

function conflictStrategyLabel(value) {
  if (value === "manual_review") return "逐条确认";
  if (value === "keep_existing") return "保留软件内";
  if (value === "prefer_imported") return "原著覆盖";
  return "智能合并";
}

function sourceTypeLabel(value) {
  if (value === "imported_document") return "外部原著";
  return "软件内全书";
}

function hasRunningJobs(jobs) {
  return jobs.some((job) => job.status === "pending" || job.status === "running");
}

function renderJobPostprocessSummary(postprocess = {}) {
  const status = postprocess.status || "not_started";
  const characters = Number(postprocess.characters_summarized || 0);
  const relations = Number(postprocess.relations_summarized || 0);
  const worldFacts = Number(postprocess.world_facts_summarized || 0);

  if (status === "completed") {
    return `
      <div class="rounded-2xl bg-emerald-50/80 px-4 py-3 text-sm text-emerald-700">
        整理结果：人物 ${characters} 条，关系 ${relations} 条，世界观事实 ${worldFacts} 条
      </div>
    `;
  }

  if (status === "failed") {
    return `
      <div class="rounded-2xl bg-amber-50/80 px-4 py-3 text-sm text-amber-700">
        结果整理未完成，本次仅保留原始提取结果。
      </div>
    `;
  }

  if (status === "skipped") {
    return `
      <div class="rounded-2xl bg-slate-100/80 px-4 py-3 text-sm text-slate-500">
        本次没有可整理的新增结果，已跳过后处理总结。
      </div>
    `;
  }

  return "";
}

function renderJobCompletionSummary(job) {
  const totals = job.result_payload?.totals || {};
  const postprocess = job.result_payload?.postprocess || {};
  const failedSegments = job.failed_segment_count || totals.failed_segment_count || (job.result_payload?.errors || []).length || 0;
  const pendingConflicts = job.pending_conflicts_count || totals.pending_conflict_count || 0;

  return `
    <div class="mt-3 grid gap-3 md:grid-cols-2">
      <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">
        处理结果：片段 ${job.total_segments || 0} 个，失败 ${failedSegments} 个，待确认冲突 ${pendingConflicts} 条
      </div>
      <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">
        提取结果：人物 ${totals.created_character_count || 0} 新增 / ${totals.updated_character_count || 0} 更新，关系 ${totals.created_relation_count || 0} 新增 / ${totals.updated_relation_count || 0} 更新
      </div>
      <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">
        世界观事实：抽取 ${totals.world_facts_count || 0} 条，写入 ${totals.world_facts_appended_count || 0} 条
      </div>
      <div class="rounded-2xl bg-white/85 px-4 py-3 text-sm text-slate-600 shadow-cloud">
        冲突统计：人物 ${totals.character_conflict_count || 0} 条，关系 ${totals.relation_conflict_count || 0} 条
      </div>
    </div>
    ${renderJobPostprocessSummary(postprocess)}
  `;
}

function renderJobList(jobs) {
  if (!jobs.length) {
    return '<div class="text-sm text-slate-500">还没有后台提取任务。</div>';
  }

  return jobs
    .map((job) => {
      const percent = Math.max(0, Math.min(100, Math.round((job.progress_ratio || 0) * 100)));
      const progressText = job.total_segments
        ? `${job.processed_segments}/${job.total_segments} 段`
        : "等待分析分段";
      const totals = job.result_payload?.totals || {};
      const pendingConflicts = job.pending_conflicts_count || totals.pending_conflict_count || 0;
      const incrementalText =
        job.source_type === "internal_book"
          ? [
              job.skip_unchanged_chapters ? "仅扫描新增/改动章节" : "全书重扫",
              job.skipped_unchanged_chapter_count ? `跳过旧章节 ${job.skipped_unchanged_chapter_count} 章` : "",
              job.planned_chapter_count ? `本次处理 ${job.planned_chapter_count} 章` : "",
            ]
              .filter(Boolean)
              .join(" 路 ")
          : "";
      const performanceText = [
        job.worker_count ? `${job.worker_count} 线程` : "",
        job.detected_context_window ? `${Math.round(job.detected_context_window / 1000)}k 上下文` : "",
      ]
        .filter(Boolean)
        .join(" · ");

      return `
        <article class="rounded-[1rem] bg-white/82 p-4 shadow-cloud">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="font-medium text-bamboo-deep">${escapeHtml(job.source_name || sourceTypeLabel(job.source_type))}</div>
            <span class="bamboo-chip">${escapeHtml(worldJobStatusLabel(job))}</span>
          </div>
          <div class="mt-2 text-xs uppercase tracking-[0.16em] text-slate-400">
            ${escapeHtml(sourceTypeLabel(job.source_type))} · ${escapeHtml(conflictStrategyLabel(job.conflict_strategy))}
          </div>
          <div class="mt-3 h-2 overflow-hidden rounded-full bg-slate-100">
            <div class="h-full rounded-full bg-bamboo-deep/70 transition-all" style="width:${percent}%"></div>
          </div>
          <div class="mt-2 text-sm text-slate-600">${escapeHtml(progressText)} · ${percent}%</div>
          ${performanceText ? `<div class="mt-2 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(performanceText)}</div>` : ""}
          ${incrementalText ? `<div class="mt-2 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(incrementalText)}</div>` : ""}
          <div class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(job.message || job.error_message || "等待开始")}</div>
          ${
            job.cancel_requested && ["pending", "running"].includes(job.status) && !job.is_terminated
              ? '<div class="mt-3 text-sm text-amber-700">已收到终止请求，系统会在当前片段处理完后停止。</div>'
              : ""
          }
          ${
            job.status === "completed"
              ? renderJobCompletionSummary(job)
              : ""
          }
          ${
            ["pending", "running"].includes(job.status) && !job.cancel_requested
              ? `<div class="mt-3 flex flex-wrap items-center gap-3">
                  <button class="bamboo-btn-ghost text-rose-600" data-cancel-job="${job.id}" type="button">终止任务</button>
                </div>`
              : ""
          }
          ${
            job.status === "completed" && pendingConflicts > 0
              ? `<div class="mt-3 flex flex-wrap items-center gap-3">
                  <span class="text-sm text-amber-700">还有 ${pendingConflicts} 条冲突待确认</span>
                  <button class="bamboo-btn-secondary" data-open-conflicts="${job.id}" type="button">处理冲突</button>
                </div>`
              : ""
          }
          ${
            (job.status === "failed" || job.is_terminated) && job.error_message
              ? `<div class="mt-3 text-sm text-rose-600">${escapeHtml(job.error_message)}</div>`
              : ""
          }
          ${
            job.status === "failed"
              ? `<div class="mt-3 flex flex-wrap items-center gap-3">
                  ${
                    job.resume_available
                      ? `<button class="bamboo-btn-secondary" data-resume-job="${job.id}" type="button">继续提取</button>`
                      : ""
                  }
                  ${
                    job.retry_failed_available
                      ? `<button class="bamboo-btn-secondary" data-retry-failed-job="${job.id}" type="button">仅重试失败片段</button>`
                      : ""
                  }
                  <button class="bamboo-btn-ghost text-slate-500" data-delete-job="${job.id}" type="button">删除记录</button>
                </div>`
              : job.status === "completed" || job.is_terminated
                ? `<div class="mt-3 flex flex-wrap items-center gap-3">
                    ${
                      job.retry_failed_available
                        ? `<button class="bamboo-btn-secondary" data-retry-failed-job="${job.id}" type="button">仅重试失败片段</button>`
                        : ""
                    }
                    <button class="bamboo-btn-ghost text-slate-500" data-delete-job="${job.id}" type="button">删除记录</button>
                  </div>`
                : ""
          }
        </article>
      `;
    })
    .join("");
}

function renderQueuedJob(job) {
  return `
    <div class="space-y-3">
      <div class="text-sm text-slate-600">后台任务 #${job.id} 已创建，来源：${escapeHtml(sourceTypeLabel(job.source_type))}</div>
      <div class="text-sm text-slate-600">冲突处理：${escapeHtml(conflictStrategyLabel(job.conflict_strategy))}</div>
      <div class="text-sm text-slate-600">分段设置：默认按约 ${job.segment_unit_limit || 36000} 字/段处理。</div>
      <div class="text-sm text-slate-600">系统会自动分段处理，可在下方“后台提取任务”查看进度。</div>
    </div>
  `;
}

function renderConflictField(label, value) {
  const display =
    Array.isArray(value)
      ? value.join("、")
      : value && typeof value === "object"
        ? JSON.stringify(value, null, 2)
        : value;
  return `
    <div class="rounded-2xl bg-white/75 px-4 py-3">
      <div class="text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(label)}</div>
      <div class="mt-2 whitespace-pre-wrap break-words text-sm leading-7 text-slate-600">${escapeHtml(display || "未提供")}</div>
    </div>
  `;
}

function renderCharacterConflict(conflict) {
  const existing = conflict.existing || {};
  const incoming = conflict.incoming || {};
  const existingFields = [
    ["人物名", existing.name],
    ["角色标签", existing.role_label],
    ["人物小传", existing.biography || existing.description],
    ["章节时间节点", getCharacterTimelineEntries(existing).map(summarizeTimelineEntry)],
    ["备注", existing.notes],
  ];
  const incomingFields = [
    ["人物名", incoming.name],
    ["角色标签", incoming.role_label],
    ["人物小传", incoming.biography || incoming.description],
    ["章节时间节点", getCharacterTimelineEntries(incoming).map(summarizeTimelineEntry)],
    ["备注", incoming.notes],
  ];

  return `
    <article class="rounded-[1.3rem] bg-[#f3efe4] p-5 shadow-cloud">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div class="font-medium text-bamboo-deep">${escapeHtml(conflict.title || existing.name || "人物冲突")}</div>
          <div class="mt-1 text-xs uppercase tracking-[0.16em] text-slate-400">人物卡冲突 · 来源 ${escapeHtml((conflict.source_labels || []).join(" / ") || "未知片段")}</div>
        </div>
        ${
          conflict.status === "resolved"
            ? `<span class="bamboo-chip">${escapeHtml(conflict.resolution === "prefer_imported" ? "已采用导入内容" : "已保留软件内")}</span>`
            : ""
        }
      </div>
      <div class="mt-4 grid gap-4 lg:grid-cols-2">
        <div class="space-y-3">
          <div class="text-sm font-medium text-bamboo-deep">软件内现有人物卡</div>
          ${existingFields.map(([label, value]) => renderConflictField(label, value)).join("")}
        </div>
        <div class="space-y-3">
          <div class="text-sm font-medium text-bamboo-deep">导入原著提取结果</div>
          ${incomingFields.map(([label, value]) => renderConflictField(label, value)).join("")}
        </div>
      </div>
      ${
        conflict.status !== "resolved"
          ? `<div class="mt-4 flex flex-wrap gap-3">
              <button class="bamboo-btn-secondary" data-resolve-conflict="${escapeHtml(conflict.id)}" data-resolution="keep_existing" type="button">保留软件内人物卡</button>
              <button class="bamboo-btn-primary" data-resolve-conflict="${escapeHtml(conflict.id)}" data-resolution="prefer_imported" type="button">采用导入人物卡</button>
            </div>`
          : ""
      }
    </article>
  `;
}

function renderRelationConflict(conflict) {
  const existing = conflict.existing || {};
  const incoming = conflict.incoming || {};
  const existingFields = [
    ["关系", `${existing.source_character_name || "?"} → ${existing.target_character_name || "?"}`],
    ["关系类型", existing.relation_type_label || existing.relation_type || "关系"],
    ["关系标题", existing.label || "未提供"],
    ["关系描述", existing.description],
    ["强度", existing.strength],
    ["重要度", existing.importance_label || existing.importance_level || "主要"],
  ];
  const incomingFields = [
    ["关系", `${incoming.source_name || "?"} → ${incoming.target_name || "?"}`],
    ["关系类型", incoming.relation_type_label || incoming.relation_type || "关系"],
    ["关系标题", incoming.label || "未提供"],
    ["关系描述", incoming.description],
    ["强度", incoming.strength],
    ["重要度", incoming.importance_label || incoming.importance_level || "主要"],
  ];

  return `
    <article class="rounded-[1.3rem] bg-[#f3efe4] p-5 shadow-cloud">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div class="font-medium text-bamboo-deep">${escapeHtml(conflict.title || "关系冲突")}</div>
          <div class="mt-1 text-xs uppercase tracking-[0.16em] text-slate-400">关系冲突 · 来源 ${escapeHtml((conflict.source_labels || []).join(" / ") || "未知片段")}</div>
        </div>
        ${
          conflict.status === "resolved"
            ? `<span class="bamboo-chip">${escapeHtml(conflict.resolution === "prefer_imported" ? "已采用导入内容" : "已保留软件内")}</span>`
            : ""
        }
      </div>
      <div class="mt-4 grid gap-4 lg:grid-cols-2">
        <div class="space-y-3">
          <div class="text-sm font-medium text-bamboo-deep">软件内现有关系</div>
          ${existingFields.map(([label, value]) => renderConflictField(label, value)).join("")}
        </div>
        <div class="space-y-3">
          <div class="text-sm font-medium text-bamboo-deep">导入原著提取结果</div>
          ${incomingFields.map(([label, value]) => renderConflictField(label, value)).join("")}
        </div>
      </div>
      ${
        conflict.status !== "resolved"
          ? `<div class="mt-4 flex flex-wrap gap-3">
              <button class="bamboo-btn-secondary" data-resolve-conflict="${escapeHtml(conflict.id)}" data-resolution="keep_existing" type="button">保留软件内关系</button>
              <button class="bamboo-btn-primary" data-resolve-conflict="${escapeHtml(conflict.id)}" data-resolution="prefer_imported" type="button">采用导入关系</button>
            </div>`
          : ""
      }
    </article>
  `;
}

function renderConflictModal(job) {
  const conflicts = (job?.result_payload?.conflicts || []).filter((item) => item.status !== "resolved");
  if (!conflicts.length) {
    return `
      <div class="rounded-[1.2rem] bg-white/82 p-5 text-sm leading-7 text-slate-500 shadow-cloud">
        当前任务没有待处理冲突，或已经全部确认完成。
      </div>
    `;
  }

  return `
    <div class="space-y-4">
      <div class="rounded-[1.1rem] bg-white/82 px-4 py-3 text-sm text-slate-600 shadow-cloud">
        本任务还有 ${conflicts.length} 条冲突待确认。你可以逐条选择保留软件内数据，或采用导入内容。
      </div>
      ${conflicts
        .map((conflict) => (conflict.conflict_type === "relation" ? renderRelationConflict(conflict) : renderCharacterConflict(conflict)))
        .join("")}
    </div>
  `;
}

function setExtractionBusy(isBusy) {
  ["world-extract", "world-extract-batch", "world-import-extract", "world-import-file"].forEach((id) => {
    const button = document.getElementById(id);
    if (!button) return;
    if ("disabled" in button) {
      button.disabled = isBusy;
    }
    button.classList.toggle("opacity-60", isBusy);
    button.classList.toggle("pointer-events-none", isBusy);
  });
}

function resetImportFileUi(message = "未选择文件。旧版 .doc 需要先另存为 .docx。") {
  const note = document.getElementById("world-import-file-note");
  if (note) note.textContent = message;
  clearImportEstimate();
}

async function requestImportEstimate(file) {
  const requestId = state.importEstimateRequestId + 1;
  state.importEstimateRequestId = requestId;
  const note = document.getElementById("world-import-file-note");
  if (note) {
    const sizeLabel = formatFileSize(file?.size);
    note.textContent = `已选择：${file?.name || "未命名文件"}${sizeLabel ? ` · ${sizeLabel}` : ""}，正在估算…`;
  }
  clearImportEstimate();
  try {
    const estimate = await estimateWorldExtractionDocument(bookId, file, {
      update_world_bible: document.getElementById("world-update-bible").checked,
      segment_unit_limit: 36000,
    });
    if (requestId !== state.importEstimateRequestId) return;
    if (note) {
      const sizeLabel = formatFileSize(file?.size);
      note.textContent = `已选择：${file?.name || "未命名文件"}${sizeLabel ? ` · ${sizeLabel}` : ""}`;
    }
    renderImportEstimate(estimate);
  } catch (error) {
    if (requestId !== state.importEstimateRequestId) return;
    const message = error instanceof ApiError ? error.message : "导入预估失败";
    if (note) note.textContent = message;
    clearImportEstimate();
  }
}

function setCharacterOptions(characters) {
  const options = characters
    .map(
      (item) =>
        `<option value="${item.id}">${escapeHtml(item.name)}${item.is_primary_display ? "" : " · 次要人物"}</option>`
    )
    .join("");
  document.getElementById("relation-source").innerHTML = options;
  document.getElementById("relation-target").innerHTML = options;
  const factionMemberSelect = document.getElementById("faction-membership-character");
  if (factionMemberSelect) {
    factionMemberSelect.innerHTML = `<option value="">选择人物</option>${options}`;
  }
}

function renderChapterOptions(selectedChapterId = "") {
  const chapters = listTimelineChapters(worldTimelineChapters);
  if (!chapters.length) return '<option value="">暂无章节可选</option>';
  return chapters
    .map((chapter) => {
      const chapterNumber = timelineChapterNumber(chapter);
      const chapterLabel = chapter.title || `第${chapterNumber}章`;
      return `<option value="${chapter.id}" ${Number(selectedChapterId) === Number(chapter.id) ? "selected" : ""}>第${chapterNumber}章 · ${escapeHtml(
        chapterLabel
      )}</option>`;
    })
    .join("");
}

function setFactionMembershipChapterOptions(startChapterId = "", endChapterId = "") {
  const startSelect = document.getElementById("faction-membership-start");
  const endSelect = document.getElementById("faction-membership-end");
  const chapterOptions = renderChapterOptions();
  if (startSelect) {
    startSelect.innerHTML = `<option value="">加入章节未记录</option>${chapterOptions}`;
    startSelect.value = startChapterId ? String(startChapterId) : "";
  }
  if (endSelect) {
    endSelect.innerHTML = `<option value="">退出章节未记录</option>${chapterOptions}`;
    endSelect.value = endChapterId ? String(endChapterId) : "";
  }
}

function readCharacterLifeStatuses() {
  return Array.from(document.querySelectorAll("[data-life-status]:checked"))
    .map((input) => input.dataset.lifeStatus || "")
    .filter(Boolean);
}

function syncCharacterLifeStatusChoiceState() {
  document.querySelectorAll("#character-life-statuses .bamboo-choice-chip").forEach((label) => {
    const input = label.querySelector("input[data-life-status]");
    label.classList.toggle("is-selected", Boolean(input?.checked));
  });
}

function setCharacterLifeStatuses(statuses) {
  const selected = new Set(normalizeLifeStatuses(statuses));
  document.querySelectorAll("[data-life-status]").forEach((input) => {
    input.checked = selected.has(input.dataset.lifeStatus || "");
  });
  syncCharacterLifeStatusChoiceState();
}

function mergeCharacterCardJson(character, lifeStatuses, importanceLevel = "") {
  const current = character?.card_json && typeof character.card_json === "object" ? { ...character.card_json } : {};
  const normalizedLifeStatuses = normalizeLifeStatuses(lifeStatuses);
  if (normalizedLifeStatuses.length) {
    current.life_statuses = normalizedLifeStatuses;
  } else {
    delete current.life_statuses;
  }
  const normalizedImportance = normalizeImportanceLevel({ card_json: { importance_level: importanceLevel } }) || "";
  if (normalizedImportance) {
    current.importance_level = normalizedImportance;
  } else {
    delete current.importance_level;
  }
  return current;
}

function listTimelineChapters(chapters) {
  return (chapters || [])
    .filter((chapter) => chapter.node_type === "chapter" || chapter.node_type === "scene")
    .slice()
    .sort((left, right) => {
      const leftNumber = Number(left.sequence_number || left.sort_order || left.id || 0);
      const rightNumber = Number(right.sequence_number || right.sort_order || right.id || 0);
      if (leftNumber !== rightNumber) return leftNumber - rightNumber;
      return Number(left.id) - Number(right.id);
    });
}

function timelineChapterNumber(chapter) {
  return Number(chapter?.sequence_number || chapter?.sort_order || chapter?.id || 0);
}

function renderTimelineEditor(entries = [], containerId = "character-timeline-list") {
  const container = document.getElementById(containerId);
  if (!container) return;
  const chapters = listTimelineChapters(worldTimelineChapters);
  const normalizedEntries = Array.isArray(entries) && entries.length ? entries : [];

  if (!normalizedEntries.length) {
    container.innerHTML =
      '<div class="rounded-2xl bg-white/72 px-4 py-3 text-sm text-slate-500">还没有时间节点。建议至少记录人物最近一次出现时在第几章、在哪里、正在做什么。</div>';
    return;
  }

  container.innerHTML = normalizedEntries
    .map((entry, index) => {
      const selectedChapterId =
        Number(entry.chapter_id) ||
        chapters.find((chapter) => timelineChapterNumber(chapter) === Number(entry.chapter_number))?.id ||
        "";
      const options = chapters.length
        ? chapters
            .map((chapter) => {
              const chapterNumber = timelineChapterNumber(chapter);
              const chapterLabel = chapter.title || `第${chapterNumber}章`;
              return `<option value="${chapter.id}" ${Number(selectedChapterId) === Number(chapter.id) ? "selected" : ""}>第${chapterNumber}章 · ${escapeHtml(
                chapterLabel
              )}</option>`;
            })
            .join("")
        : '<option value="">暂无章节可选</option>';

      return `
        <div class="rounded-[1rem] bg-white/80 p-4 shadow-cloud" data-timeline-entry="${index}">
          <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
            <select class="bamboo-field" data-timeline-chapter>
              <option value="">选择章节</option>
              ${options}
            </select>
            <button class="bamboo-btn-ghost text-rose-600" data-remove-timeline-entry="${index}" type="button">删除</button>
          </div>
          <textarea class="bamboo-field mt-3 min-h-[5rem] resize-y" data-timeline-event placeholder="这一章里他做了什么">${escapeHtml(
            entry.event || entry.notes || ""
          )}</textarea>
          <div class="mt-3 grid gap-3 md:grid-cols-2">
            <input class="bamboo-field" data-timeline-location placeholder="所在位置，如东市 / 山门 / 客栈" value="${escapeHtml(
              entry.location || ""
            )}" />
            <input class="bamboo-field" data-timeline-status placeholder="当前状态，如潜伏调查 / 重伤修养" value="${escapeHtml(
              entry.status || ""
            )}" />
          </div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll("[data-remove-timeline-entry]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextEntries = collectTimelineEditorEntries(containerId).filter((_, index) => index !== Number(button.dataset.removeTimelineEntry));
      renderTimelineEditor(nextEntries, containerId);
    });
  });
}

function collectTimelineEditorEntries(containerId = "character-timeline-list") {
  const chapters = listTimelineChapters(worldTimelineChapters);
  const container = document.getElementById(containerId);
  if (!container) return [];
  return Array.from(container.querySelectorAll("[data-timeline-entry]"))
    .map((row) => {
      const chapterId = Number(row.querySelector("[data-timeline-chapter]")?.value || 0);
      const chapter = chapters.find((item) => item.id === chapterId) || null;
      const event = String(row.querySelector("[data-timeline-event]")?.value || "").trim();
      const location = String(row.querySelector("[data-timeline-location]")?.value || "").trim();
      const status = String(row.querySelector("[data-timeline-status]")?.value || "").trim();
      if (!chapter || !event) return null;
      const chapterNumber = timelineChapterNumber(chapter);
      return {
        chapter_id: chapter.id,
        chapter_number: chapterNumber,
        chapter_label: `第${chapterNumber}章`,
        chapter_title: chapter.title || `第${chapterNumber}章`,
        event,
        location: location || undefined,
        status: status || undefined,
      };
    })
    .filter(Boolean);
}

function focusWorldEditor(panelId, primaryFieldId) {
  const panel = document.getElementById(panelId);
  const field = document.getElementById(primaryFieldId);
  if (!panel) return;

  panel.scrollIntoView({ behavior: "smooth", block: "center" });
  panel.classList.remove("bamboo-panel-edit-focus");
  void panel.offsetWidth;
  panel.classList.add("bamboo-panel-edit-focus");

  window.setTimeout(() => {
    panel.classList.remove("bamboo-panel-edit-focus");
  }, 1800);

  if (field) {
    window.setTimeout(() => field.focus(), 220);
  }
}

export async function initWorldPage() {
  const books = (await listBooks()).items || [];
  const bookId = getSelectedBookId(books);
  const bookSelect = document.getElementById("world-book-select");
  const safeGraphControls = document.querySelector("#world-graph-panel .flex.flex-wrap.gap-3");
  const safeResetGraphButton = document.getElementById("world-reset-graph");
  if (safeGraphControls && safeResetGraphButton && !document.getElementById("world-zoom-in")) {
    [
      { id: "world-relayout-graph", label: "重新排布" },
      { id: "world-zoom-out", label: "缩小" },
      { id: "world-zoom-in", label: "放大" },
      { id: "world-graph-fullscreen", label: "全屏查看" },
      { id: "world-graph-exit-fullscreen", label: "退出全屏", hidden: true },
    ].forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.id = item.id;
      button.textContent = item.label;
      button.className = `bamboo-btn-secondary px-4 py-2 text-sm${item.hidden ? " hidden" : ""}`;
      safeGraphControls.append(button);
    });
  }

  bookSelect.innerHTML = books.length
    ? books.map((book) => `<option value="${book.id}" ${book.id === bookId ? "selected" : ""}>${escapeHtml(book.title)}</option>`).join("")
    : '<option value="">暂无书籍</option>';

  if (!bookId) {
    setAssistantContext();
    document.getElementById("world-graph").innerHTML = '<div class="p-6 text-sm text-slate-500">暂无书籍可维护。</div>';
    return;
  }

  const state = {
    editingCharacterId: null,
    editingRelationId: null,
    editingFactionId: null,
    characterTimelineDraft: [],
    timelineModalSnapshot: null,
    characters: [],
    relations: [],
    factions: [],
    factionMemberships: [],
    chapters: [],
    jobs: [],
    jobPollTimer: null,
    pollingJobs: false,
    activeConflictJob: null,
    showMinorCharacters: false,
    showAllFacts: false,
    showRelationFilters: false,
    worldFacts: [],
    worldBible: "",
    relationFilters: {
      initialized: false,
      categories: {},
      types: {},
    },
    graphPan: { x: 0, y: 0 },
    graphScale: 1,
    graphStats: null,
    graphLayout: {
      signature: "",
      positions: {},
      bounds: null,
    },
    graphScene: null,
    graphViewportInitialized: false,
    graphStorageKey: `bamboo_world_graph_layout_${bookId}`,
    activeFactionDetailId: null,
    importEstimateRequestId: 0,
  };

  try {
    const storedPositions = JSON.parse(window.localStorage.getItem(state.graphStorageKey) || "{}");
    if (storedPositions && typeof storedPositions === "object") {
      state.graphLayout.positions = Object.fromEntries(
        Object.entries(storedPositions)
          .map(([key, value]) => {
            const id = Number(key);
            const x = Number(value?.x);
            const y = Number(value?.y);
            if (!id || !Number.isFinite(x) || !Number.isFinite(y)) return null;
            return [id, { x, y }];
          })
          .filter(Boolean)
      );
    }
  } catch {
    state.graphLayout.positions = {};
  }

  function persistGraphLayout() {
    try {
      window.localStorage.setItem(state.graphStorageKey, JSON.stringify(state.graphLayout.positions || {}));
    } catch {
      // ignore storage failures
    }
  }

  function closeConflictModal() {
    state.activeConflictJob = null;
    const modal = document.getElementById("world-conflict-modal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.classList.remove("flex");
    const body = document.getElementById("world-conflict-body");
    if (body) body.innerHTML = "";
  }

  function closeWorldDetailModal() {
    state.activeFactionDetailId = null;
    const modal = document.getElementById("world-detail-modal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.classList.remove("flex");
    const title = document.getElementById("world-detail-title");
    const subtitle = document.getElementById("world-detail-subtitle");
    const body = document.getElementById("world-detail-body");
    if (title) title.textContent = "详情";
    if (subtitle) subtitle.textContent = "";
    if (body) body.innerHTML = "";
  }

function renderRelationEventTimeline(events) {
  if (!events.length) {
    return '<div class="rounded-[1.2rem] bg-white/82 p-5 text-sm text-slate-500 shadow-cloud">当前还没有关系事件记录。</div>';
  }
  const sortedEvents = [...events].sort((left, right) => {
    const leftChapter = Number(left.chapter_id || 0);
    const rightChapter = Number(right.chapter_id || 0);
    if (leftChapter !== rightChapter) return leftChapter - rightChapter;
    return Number(new Date(left.created_at || 0)) - Number(new Date(right.created_at || 0));
  });
  return `
      <div class="relative space-y-4 pl-8 before:absolute before:bottom-4 before:left-[0.92rem] before:top-4 before:w-px before:bg-slate-200">
        ${sortedEvents
          .map(
            (event) => `
              <div class="relative rounded-[1.2rem] bg-white/82 p-5 shadow-cloud">
                <div class="absolute left-[-1.72rem] top-6 h-3.5 w-3.5 rounded-full border-2 border-[#f8f6ef] bg-bamboo-deep shadow-sm"></div>
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div class="text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(event.chapter_id ? `章节 ${event.chapter_id}` : "未标注章节")}</div>
                    <div class="mt-1 font-medium text-bamboo-deep">${escapeHtml(event.chapter_title || event.segment_label || "未标注章节")}</div>
                  </div>
                  <div class="flex flex-wrap gap-2">
                    <span class="bamboo-status-chip">${escapeHtml(event.relation_type_label || "关系")}</span>
                    <span class="bamboo-status-chip">${escapeHtml(event.importance_label || "主要")}</span>
                  </div>
                </div>
                <div class="mt-3 text-sm text-slate-500">${escapeHtml(event.label || event.relation_type_label || "关系")}</div>
                ${
                  event.description
                    ? `<div class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(event.description)}</div>`
                    : ""
                }
                ${
                  event.event_summary
                    ? `<div class="mt-2 text-xs leading-6 text-slate-400">变化说明：${escapeHtml(event.event_summary)}</div>`
                    : ""
                }
                <div class="mt-3 text-xs uppercase tracking-[0.16em] text-slate-400">${escapeHtml(formatDateTime(event.created_at))}</div>
              </div>
            `
          )
          .join("")}
      </div>
    `;
  }

function renderFactionDetail(faction) {
  const memberships = Array.isArray(faction.memberships) ? faction.memberships : [];
  return `
    <div class="space-y-4">
        <div class="rounded-[1.2rem] bg-white/82 p-5 shadow-cloud">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="font-medium text-bamboo-deep">${escapeHtml(faction.name || "未命名阵营")}</div>
            <span class="bamboo-status-chip" style="background:${escapeHtml(faction.color || "#6b8d52")}20;color:${escapeHtml(faction.color || "#6b8d52")}">成员 ${memberships.length}</span>
          </div>
          ${
            faction.description
              ? `<div class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(faction.description)}</div>`
              : '<div class="mt-2 text-sm text-slate-500">暂无阵营说明。</div>'
          }
        </div>
        ${
          memberships.length
            ? memberships
                .map(
                  (item) => `
                    <div class="rounded-[1.2rem] bg-white/82 p-5 shadow-cloud">
                      <div class="flex flex-wrap items-center justify-between gap-3">
                        <div class="font-medium text-bamboo-deep">${escapeHtml(item.character_name || "未知人物")}</div>
                        <div class="flex flex-wrap gap-2">
                          <span class="bamboo-status-chip">${escapeHtml(item.status_label || item.status || "活跃")}</span>
                          ${item.role_label ? `<span class="bamboo-status-chip">${escapeHtml(item.role_label)}</span>` : ""}
                        </div>
                      </div>
                      ${
                        item.notes
                          ? `<div class="mt-2 text-sm leading-7 text-slate-500">${escapeHtml(item.notes)}</div>`
                          : ""
                      }
                      <div class="mt-2 text-xs leading-6 text-slate-400">
                        ${
                          item.start_chapter_title || item.end_chapter_title
                            ? `章节区间：${escapeHtml(item.start_chapter_title || "起始未记录")} 至 ${escapeHtml(item.end_chapter_title || "当前仍在阵营")}`
                            : "章节区间未记录"
                        }
                        ${Number.isFinite(item.loyalty) ? ` · 忠诚度 ${item.loyalty}` : ""}
                      </div>
                      <div class="mt-3 flex gap-3">
                        <button class="bamboo-btn-secondary" data-edit-faction-membership="${item.id}" type="button">编辑成员</button>
                        <button class="bamboo-btn-ghost text-rose-600" data-delete-faction-membership="${item.id}" type="button">移出阵营</button>
                      </div>
                    </div>
                  `
                )
                .join("")
            : '<div class="rounded-[1.2rem] bg-white/82 p-5 text-sm text-slate-500 shadow-cloud">当前还没有阵营成员记录。</div>'
        }
        <div class="rounded-[1.2rem] bg-white/82 p-5 shadow-cloud">
          <div class="bamboo-mini-label" id="faction-membership-form-title">新增成员</div>
          <div class="mt-4 space-y-4">
            <input id="faction-membership-id" type="hidden" value="" />
            <select class="bamboo-field" id="faction-membership-character">
              <option value="">选择人物</option>
            </select>
            <input class="bamboo-field" id="faction-membership-role" placeholder="阵营身份，如党魁 / 骨干 / 幕僚" />
            <input class="bamboo-field" id="faction-membership-loyalty" max="1" min="0" placeholder="忠诚度 0-1" step="0.1" type="number" />
            <select class="bamboo-field" id="faction-membership-status">
              <option value="active">现成员</option>
              <option value="former">已退出</option>
            </select>
            <select class="bamboo-field" id="faction-membership-start">
              <option value="">加入章节未记录</option>
            </select>
            <select class="bamboo-field" id="faction-membership-end">
              <option value="">退出章节未记录</option>
            </select>
            <textarea class="bamboo-field min-h-[5rem] resize-y" id="faction-membership-notes" placeholder="备注"></textarea>
            <div class="flex gap-3">
              <button class="bamboo-btn-primary flex-1" id="faction-membership-save" type="button">保存成员</button>
              <button class="bamboo-btn-secondary" id="faction-membership-reset" type="button">清空</button>
            </div>
          </div>
        </div>
      </div>
    `;
}

  async function openRelationDetailModal(relationId) {
    const modal = document.getElementById("world-detail-modal");
    const title = document.getElementById("world-detail-title");
    const subtitle = document.getElementById("world-detail-subtitle");
    const body = document.getElementById("world-detail-body");
    if (!modal || !body) return;
    modal.classList.remove("hidden");
    modal.classList.add("flex");
    if (title) title.textContent = `关系 #${relationId}`;
    if (subtitle) subtitle.textContent = "正在加载关系时间线...";
    body.innerHTML = '<div class="rounded-[1.2rem] bg-white/82 p-5 text-sm text-slate-500 shadow-cloud">正在加载关系详情...</div>';
    try {
      const [relation, eventsResponse] = await Promise.all([
        getRelation(bookId, relationId),
        listRelationEvents(bookId, relationId),
      ]);
      if (title) title.textContent = `${relation.source_character_name || "人物"} ${relation.is_bidirectional ? "⇄" : "→"} ${relation.target_character_name || "人物"}`;
      if (subtitle) subtitle.textContent = `${relation.relation_type_label || "关系"} · ${relation.label || relation.relation_type_display || "未命名关系"}`;
      body.innerHTML = renderRelationEventTimeline(eventsResponse.items || relation.events || []);
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "加载关系详情失败";
      if (subtitle) subtitle.textContent = "";
      body.innerHTML = `<div class="rounded-[1.2rem] bg-rose-50 p-5 text-sm text-rose-700 shadow-cloud">${escapeHtml(message)}</div>`;
    }
  }

  async function openFactionDetailModal(factionId) {
    const modal = document.getElementById("world-detail-modal");
    const title = document.getElementById("world-detail-title");
    const subtitle = document.getElementById("world-detail-subtitle");
    const body = document.getElementById("world-detail-body");
    if (!modal || !body) return;
    modal.classList.remove("hidden");
    modal.classList.add("flex");
    if (title) title.textContent = `阵营 #${factionId}`;
    if (subtitle) subtitle.textContent = "正在加载成员详情...";
    body.innerHTML = '<div class="rounded-[1.2rem] bg-white/82 p-5 text-sm text-slate-500 shadow-cloud">正在加载阵营详情...</div>';
    try {
      const faction = await getFaction(bookId, factionId);
      state.activeFactionDetailId = factionId;
      if (title) title.textContent = faction.name || `阵营 #${factionId}`;
      if (subtitle) subtitle.textContent = `${(faction.memberships || []).length} 名成员`;
      body.innerHTML = renderFactionDetail(faction);
      setCharacterOptions(state.graphScene?.annotatedCharacters || state.characters || []);
      setFactionMembershipChapterOptions();
      bindFactionDetailButtons(faction);
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "加载阵营详情失败";
      if (subtitle) subtitle.textContent = "";
      body.innerHTML = `<div class="rounded-[1.2rem] bg-rose-50 p-5 text-sm text-rose-700 shadow-cloud">${escapeHtml(message)}</div>`;
    }
  }

  function resetFactionMembershipEditor() {
    const title = document.getElementById("faction-membership-form-title");
    const idInput = document.getElementById("faction-membership-id");
    const characterSelect = document.getElementById("faction-membership-character");
    const roleInput = document.getElementById("faction-membership-role");
    const loyaltyInput = document.getElementById("faction-membership-loyalty");
    const statusSelect = document.getElementById("faction-membership-status");
    const startChapterSelect = document.getElementById("faction-membership-start");
    const endChapterSelect = document.getElementById("faction-membership-end");
    const notesInput = document.getElementById("faction-membership-notes");
    if (title) title.textContent = "新增成员";
    if (idInput) idInput.value = "";
    if (characterSelect) characterSelect.value = "";
    if (roleInput) roleInput.value = "";
    if (loyaltyInput) loyaltyInput.value = "";
    if (statusSelect) statusSelect.value = "active";
    if (startChapterSelect) startChapterSelect.value = "";
    if (endChapterSelect) endChapterSelect.value = "";
    if (notesInput) notesInput.value = "";
  }

  function bindFactionDetailButtons(faction) {
    document.querySelectorAll("[data-edit-faction-membership]").forEach((button) => {
      button.addEventListener("click", () => {
        const membershipId = Number(button.dataset.editFactionMembership);
        const membership = (faction.memberships || []).find((item) => item.id === membershipId);
        if (!membership) return;
        const title = document.getElementById("faction-membership-form-title");
        if (title) title.textContent = `编辑成员 #${membership.id}`;
        document.getElementById("faction-membership-id").value = String(membership.id);
        document.getElementById("faction-membership-character").value = String(membership.character_id || "");
        document.getElementById("faction-membership-role").value = membership.role_label || "";
        document.getElementById("faction-membership-loyalty").value = membership.loyalty ?? "";
        document.getElementById("faction-membership-status").value = membership.status || "active";
        document.getElementById("faction-membership-start").value = membership.start_chapter_id ? String(membership.start_chapter_id) : "";
        document.getElementById("faction-membership-end").value = membership.end_chapter_id ? String(membership.end_chapter_id) : "";
        document.getElementById("faction-membership-notes").value = membership.notes || "";
      });
    });

    document.querySelectorAll("[data-delete-faction-membership]").forEach((button) => {
      button.addEventListener("click", async () => {
        const membershipId = Number(button.dataset.deleteFactionMembership);
        if (!membershipId) return;
        if (!window.confirm("确认将这名成员移出阵营吗？")) return;
        button.disabled = true;
        try {
          await deleteFactionMembership(bookId, membershipId);
          showToast("成员已移出阵营");
          await openFactionDetailModal(faction.id);
          await refresh();
        } catch (error) {
          const message = error instanceof ApiError ? error.message : "移出阵营失败";
          showToast(message, "error");
          button.disabled = false;
        }
      });
    });

    document.getElementById("faction-membership-reset")?.addEventListener("click", resetFactionMembershipEditor);
    document.getElementById("faction-membership-save")?.addEventListener("click", async () => {
      const membershipId = Number(document.getElementById("faction-membership-id").value || 0);
      const characterId = Number(document.getElementById("faction-membership-character").value || 0);
      if (!characterId) {
        showToast("请先选择人物", "error");
        return;
      }
      const startChapterId = Number(document.getElementById("faction-membership-start").value || 0) || null;
      const endChapterId = Number(document.getElementById("faction-membership-end").value || 0) || null;
      const chapters = listTimelineChapters(worldTimelineChapters);
      const startChapter = chapters.find((item) => Number(item.id) === Number(startChapterId)) || null;
      const endChapter = chapters.find((item) => Number(item.id) === Number(endChapterId)) || null;
      if (startChapter && endChapter && timelineChapterNumber(endChapter) < timelineChapterNumber(startChapter)) {
        showToast("退出章节不能早于加入章节", "error");
        return;
      }
      const payload = {
        faction_id: faction.id,
        character_id: characterId,
        role_label: document.getElementById("faction-membership-role").value.trim() || null,
        loyalty:
          document.getElementById("faction-membership-loyalty").value === ""
            ? null
            : Number(document.getElementById("faction-membership-loyalty").value),
        status: document.getElementById("faction-membership-status").value || "active",
        start_chapter_id: startChapterId,
        end_chapter_id: endChapterId,
        notes: document.getElementById("faction-membership-notes").value.trim() || null,
      };
      const saveButton = document.getElementById("faction-membership-save");
      if (saveButton) saveButton.disabled = true;
      try {
        if (membershipId) {
          await updateFactionMembership(bookId, membershipId, payload);
          showToast("阵营成员已更新");
        } else {
          await createFactionMembership(bookId, payload);
          showToast("阵营成员已添加");
        }
        resetFactionMembershipEditor();
        await openFactionDetailModal(faction.id);
        await refresh();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "保存阵营成员失败";
        showToast(message, "error");
      } finally {
        if (saveButton) saveButton.disabled = false;
      }
    });
  }

  function openTimelineModal() {
    const modal = document.getElementById("character-timeline-modal");
    if (!modal) return;
    renderTimelineEditor(state.characterTimelineDraft, "character-timeline-list");
    state.timelineModalSnapshot = serializeTimelineModalState();
    modal.classList.remove("hidden");
    modal.classList.add("flex");
  }

  function closeTimelineModal() {
    const modal = document.getElementById("character-timeline-modal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.classList.remove("flex");
    state.timelineModalSnapshot = null;
  }

  function serializeTimelineModalState() {
    const container = document.getElementById("character-timeline-list");
    if (!container) return "[]";
    const rows = Array.from(container.querySelectorAll("[data-timeline-entry]")).map((row) => ({
      chapterId: String(row.querySelector("[data-timeline-chapter]")?.value || ""),
      event: String(row.querySelector("[data-timeline-event]")?.value || "").trim(),
      location: String(row.querySelector("[data-timeline-location]")?.value || "").trim(),
      status: String(row.querySelector("[data-timeline-status]")?.value || "").trim(),
    }));
    return JSON.stringify(rows);
  }

  function timelineModalHasUnsavedChanges() {
    const modal = document.getElementById("character-timeline-modal");
    if (!modal || modal.classList.contains("hidden")) return false;
    return serializeTimelineModalState() !== (state.timelineModalSnapshot || "[]");
  }

  function requestCloseTimelineModal() {
    if (timelineModalHasUnsavedChanges()) {
      const shouldDiscard = window.confirm("时间节点有未保存修改，确认直接关闭并丢弃这些改动吗？");
      if (!shouldDiscard) return;
    }
    closeTimelineModal();
  }

  function resetCharacterEditor() {
    state.editingCharacterId = null;
    document.getElementById("character-form-title").textContent = "新增人物";
    document.getElementById("character-form").reset();
    document.getElementById("character-importance").value = "";
    setCharacterLifeStatuses([]);
    state.characterTimelineDraft = [];
    renderTimelinePreview([]);
    closeTimelineModal();
  }

  function resetRelationEditor() {
    state.editingRelationId = null;
    document.getElementById("relation-form-title").textContent = "新增关系";
    document.getElementById("relation-form").reset();
    document.getElementById("relation-importance").value = "major";
  }

  function resetFactionEditor() {
    state.editingFactionId = null;
    document.getElementById("faction-form-title").textContent = "新增阵营";
    document.getElementById("faction-form").reset();
    document.getElementById("faction-color").value = "#6b8d52";
  }

  function syncRelationFilterChipState() {
    document.querySelectorAll("#world-relation-filters .bamboo-choice-chip").forEach((label) => {
      const input = label.querySelector("input[data-relation-category-toggle], input[data-relation-type-toggle]");
      label.classList.toggle("is-selected", Boolean(input?.checked));
    });
  }

  function renderRelationFilters(meta, hiddenRelationCount) {
    const container = document.getElementById("world-relation-filters");
    const summary = document.getElementById("world-relation-filter-summary");
    const toggleButton = document.getElementById("world-toggle-relation-filters");
    if (!container || !summary) return;

    container.innerHTML = renderRelationFilterPanel(meta, state.relationFilters);
    syncRelationFilterChipState();
    container.classList.toggle("hidden", !state.showRelationFilters);
    if (toggleButton) {
      toggleButton.textContent = state.showRelationFilters ? "收起筛选" : "展开筛选";
    }

    const filterStats = relationFilterStats(meta, state.relationFilters);
    summary.textContent = `当前人物范围 ${meta.relation_count} 条关系，已显示 ${meta.relation_count - hiddenRelationCount} 条；已勾选 ${filterStats.selected_type_count}/${filterStats.total_type_count} 种关系词。箭头从关系发起方指向承受方。`;
  }

  async function openConflictModal(jobId) {
    const modal = document.getElementById("world-conflict-modal");
    if (!modal) {
      showToast("当前页面缺少冲突弹窗容器，请刷新后重试", "error");
      return;
    }
    modal.classList.remove("hidden");
    modal.classList.add("flex");
    const title = document.getElementById("world-conflict-title");
    const body = document.getElementById("world-conflict-body");
    if (title) title.textContent = `任务 #${jobId} 冲突确认`;
    if (body) {
      body.innerHTML =
        '<div class="rounded-[1.2rem] bg-white/82 p-5 text-sm text-slate-500 shadow-cloud">正在加载冲突详情...</div>';
    }

    try {
      const job = await getWorldExtractionJob(bookId, jobId);
      state.activeConflictJob = job;
      if (title) title.textContent = `任务 #${job.id} 冲突确认`;
      if (body) body.innerHTML = renderConflictModal(job);
      bindConflictResolveButtons();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "加载冲突详情失败";
      if (body) {
        body.innerHTML =
          `<div class="rounded-[1.2rem] bg-rose-50 p-5 text-sm text-rose-700 shadow-cloud">${escapeHtml(message)}</div>`;
      }
    }
  }

  function bindConflictResolveButtons() {
    document.querySelectorAll("[data-resolve-conflict]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!state.activeConflictJob) return;
        const decision = button.dataset.resolution;
        const conflictId = button.dataset.resolveConflict;
        if (!decision || !conflictId) return;

        button.disabled = true;
        try {
          const updatedJob = await resolveWorldExtractionConflict(bookId, state.activeConflictJob.id, {
            conflict_id: conflictId,
            decision,
          });
          state.activeConflictJob = updatedJob;
          document.getElementById("world-conflict-body").innerHTML = renderConflictModal(updatedJob);
          bindConflictResolveButtons();
          await refresh();
          showToast(decision === "prefer_imported" ? "已采用导入内容" : "已保留软件内内容");
        } catch (error) {
          const message = error instanceof ApiError ? error.message : "处理冲突失败";
          showToast(message, "error");
          button.disabled = false;
        }
      });
    });
  }

  function currentConflictStrategy() {
    return document.getElementById("world-conflict-strategy").value;
  }

  function updateVisibilityControls(hiddenMinorCount) {
    const toggleButton = document.getElementById("world-toggle-minor");
    const note = document.getElementById("world-visibility-note");
    toggleButton.textContent = state.showMinorCharacters ? "隐藏次要人物" : "显示次要人物";
    if (state.showMinorCharacters) {
      note.textContent = hiddenMinorCount
        ? `当前已显示全部人物，其中 ${hiddenMinorCount} 名原本默认隐藏。`
        : "当前人物都属于主要人物。";
      return;
    }
    note.textContent = hiddenMinorCount
      ? `默认只显示主要人物，已隐藏 ${hiddenMinorCount} 名路人或次要人物。`
      : "当前人物都属于主要人物。";
  }

  function renderWorldFactsSection() {
    const factsElement = document.getElementById("world-facts");
    const metaElement = document.getElementById("world-facts-meta");
    const toggleButton = document.getElementById("world-toggle-facts");
    const deleteAllButton = document.getElementById("world-delete-all-facts");
    if (!factsElement || !metaElement || !toggleButton || !deleteAllButton) return;

    const facts = state.worldFacts;
    if (!facts.length) {
      factsElement.innerHTML =
        '<li class="rounded-2xl bg-white/82 px-4 py-3 text-sm text-slate-500 shadow-cloud">请先在配置页填写世界观手册，或使用 AI 提取追加事实。</li>';
      metaElement.textContent = "暂无已沉淀的世界观事实";
      toggleButton.classList.add("hidden");
      deleteAllButton.classList.add("hidden");
      return;
    }

    const visibleFacts = state.showAllFacts ? facts : facts.slice(0, WORLD_FACTS_PREVIEW_LIMIT);
    const hiddenCount = Math.max(0, facts.length - visibleFacts.length);
    factsElement.innerHTML = visibleFacts
      .map(
        (fact) => `
          <li class="rounded-2xl bg-white/82 px-4 py-3 shadow-cloud">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0 flex-1 text-sm leading-7 text-slate-600">${escapeHtml(fact)}</div>
              <button
                class="bamboo-btn-ghost shrink-0 px-3 py-1.5 text-xs text-rose-600"
                data-delete-world-fact="${escapeHtml(fact)}"
                type="button"
              >
                删除
              </button>
            </div>
          </li>
        `
      )
      .join("");

    metaElement.textContent = state.showAllFacts
      ? `共 ${facts.length} 条世界观事实，当前已全部展开`
      : hiddenCount
        ? `共 ${facts.length} 条世界观事实，当前先展示 ${visibleFacts.length} 条`
        : `共 ${facts.length} 条世界观事实`;
    deleteAllButton.classList.remove("hidden");
    toggleButton.classList.toggle("hidden", facts.length <= WORLD_FACTS_PREVIEW_LIMIT);
    toggleButton.textContent = state.showAllFacts ? "收起列表" : `展开剩余 ${hiddenCount} 条`;
  }

  function clampGraphScale(value) {
    return Math.min(GRAPH_SCALE_MAX, Math.max(GRAPH_SCALE_MIN, value));
  }

  function currentGraphScalePercent() {
    return Math.round(state.graphScale * 100);
  }

  function renderGraphSummary() {
    const graphSummary = document.getElementById("world-graph-summary");
    if (!graphSummary) return;
    const stats = state.graphStats;
    if (!stats) {
      graphSummary.textContent = `等待载入人物关系图 · 缩放 ${currentGraphScalePercent()}%`;
      return;
    }

    const fullscreenElement = document.fullscreenElement || document.webkitFullscreenElement;
    const fullscreenActive = fullscreenElement?.id === "world-graph-panel";
    graphSummary.textContent =
      `当前显示 ${stats.visibleCharacters}/${stats.totalCharacters} 名人物 · ` +
      `${stats.visibleRelations}/${stats.totalRelations} 条关系` +
      (stats.relationCandidates !== stats.totalRelations ? `（当前人物范围 ${stats.relationCandidates} 条）` : "") +
      ` · 已勾选 ${stats.selectedRelationTypes}/${stats.totalRelationTypes} 种关系词 · ` +
      `缩放 ${currentGraphScalePercent()}%` +
      (fullscreenActive ? " · 全屏中" : "");
  }

  function ensureGraphLayout(visibleCharacters, visibleRelations, { force = false } = {}) {
    const graph = document.getElementById("world-graph");
    const nextSignature = buildGraphLayoutSignature(visibleCharacters, visibleRelations);
    const nextBounds = resolveGraphBounds(graph, visibleCharacters);
    const previousBounds = state.graphLayout.bounds;
    const previousPositions = force ? {} : { ...(state.graphLayout.positions || {}) };
    const sameSet =
      !force &&
      nextSignature === state.graphLayout.signature &&
      visibleCharacters.every((character) => previousPositions[character.id]);
    const boundsChanged =
      previousBounds &&
      (Math.abs(previousBounds.viewportWidth - nextBounds.viewportWidth) >= GRAPH_RELAYOUT_THRESHOLD ||
        Math.abs(previousBounds.viewportHeight - nextBounds.viewportHeight) >= GRAPH_RELAYOUT_THRESHOLD);

    if (sameSet && !boundsChanged) {
      return state.graphLayout;
    }

    const layout = computeGraphLayout(visibleCharacters, visibleRelations, graph, previousPositions);
    state.graphLayout = {
      signature: nextSignature,
      positions: {
        ...state.graphLayout.positions,
        ...layout.positions,
      },
      bounds: layout.bounds,
    };
    persistGraphLayout();
    return state.graphLayout;
  }

  function applyGraphViewport() {
    const stage = document.querySelector("#world-graph .bamboo-graph-stage");
    if (!stage) return;
    stage.style.transform = `translate(${state.graphPan.x}px, ${state.graphPan.y}px) scale(${state.graphScale})`;
    renderGraphSummary();
  }

  function updateGraphDomPositions(characters, relations) {
    const nodeRadiusById = buildGraphNodeRadiusMap(characters, relations);
    const relationCurveMeta = buildRelationCurveMeta(relations);

    characters.forEach((character) => {
      const point = state.graphLayout.positions[character.id];
      const node = document.querySelector(`#world-graph [data-graph-node="${character.id}"]`);
      if (!point || !(node instanceof HTMLElement)) return;
      node.style.left = `${point.x}px`;
      node.style.top = `${point.y}px`;
    });

    relations.forEach((relation) => {
      const geometry = resolveRelationGeometry(
        relation,
        state.graphLayout.positions,
        nodeRadiusById,
        relationCurveMeta.get(relation.id)
      );
      const line = document.querySelector(`#world-graph [data-graph-relation-line="${relation.id}"] .bamboo-relation-line`);
      const label = document.querySelector(`#world-graph [data-graph-relation-label="${relation.id}"]`);
      const box = label?.querySelector?.(".bamboo-relation-label-box");
      if (geometry && line instanceof SVGPathElement) {
        line.setAttribute("d", geometry.path);
      }
      if (geometry && label instanceof SVGGElement) {
        label.setAttribute("transform", `translate(${geometry.label_center.x.toFixed(2)} ${geometry.label_center.y.toFixed(2)})`);
      }
      if (geometry && box instanceof SVGRectElement) {
        box.setAttribute("x", String((-geometry.label_width / 2).toFixed(2)));
        box.setAttribute("y", String((-geometry.label_height / 2).toFixed(2)));
        box.setAttribute("width", String(geometry.label_width.toFixed(2)));
        box.setAttribute("height", String(geometry.label_height.toFixed(2)));
      }
    });
  }

  function fitGraphViewport(characters, graphLayout) {
    const graph = document.getElementById("world-graph");
    if (!graph || !characters.length) return;

    const rect = graph.getBoundingClientRect();
    const box = measureGraphBoundingBox(characters, graphLayout.positions);
    if (!rect.width || !rect.height || !box) return;

    const padding = 56;
    const scale = clampGraphScale(
      Math.min(
        (rect.width - padding * 2) / box.width,
        (rect.height - padding * 2) / box.height,
        1.12
      )
    );
    const centerX = box.minX + box.width / 2;
    const centerY = box.minY + box.height / 2;

    state.graphScale = scale;
    state.graphPan = {
      x: rect.width / 2 - centerX * scale,
      y: rect.height / 2 - centerY * scale,
    };
    state.graphViewportInitialized = true;
  }

  function zoomGraphTo(nextScale, { clientX, clientY } = {}) {
    const graph = document.getElementById("world-graph");
    if (!graph) return;

    const previousScale = state.graphScale;
    const scale = clampGraphScale(nextScale);
    if (Math.abs(scale - previousScale) < 0.001) return;

    const rect = graph.getBoundingClientRect();
    const localX = (clientX ?? rect.left + rect.width / 2) - rect.left;
    const localY = (clientY ?? rect.top + rect.height / 2) - rect.top;
    const worldX = (localX - state.graphPan.x) / previousScale;
    const worldY = (localY - state.graphPan.y) / previousScale;

    state.graphPan.x = localX - worldX * scale;
    state.graphPan.y = localY - worldY * scale;
    state.graphScale = scale;
    state.graphViewportInitialized = true;
    applyGraphViewport();
  }

  function updateGraphFullscreenButtons() {
    const openButton = document.getElementById("world-graph-fullscreen");
    const exitButton = document.getElementById("world-graph-exit-fullscreen");
    const fullscreenElement = document.fullscreenElement || document.webkitFullscreenElement;
    const isFullscreen = fullscreenElement?.id === "world-graph-panel";
    openButton?.classList.toggle("hidden", isFullscreen);
    exitButton?.classList.toggle("hidden", !isFullscreen);
    if (isFullscreen && state.showRelationFilters) {
      state.showRelationFilters = false;
    }
    if (state.graphScene?.visibleCharacters?.length) {
      state.graphViewportInitialized = false;
      renderWorldContent();
      bindEntityActionButtons();
    }
    renderGraphSummary();
  }

  function clearGraphHighlight() {
    const graph = document.getElementById("world-graph");
    if (!graph) return;
    graph.classList.remove("has-highlight");
    graph.querySelectorAll(".is-highlighted").forEach((element) => element.classList.remove("is-highlighted"));
  }

  function applyGraphHighlight({ relationId = null, nodeId = null } = {}) {
    const graph = document.getElementById("world-graph");
    if (!graph || !state.graphScene) return;

    const highlightNodeIds = new Set();
    const highlightRelationIds = new Set();
    const relations = state.graphScene.visibleRelations || [];

    if (relationId != null) {
      const relation = relations.find((item) => item.id === relationId);
      if (!relation) {
        clearGraphHighlight();
        return;
      }
      highlightRelationIds.add(relation.id);
      highlightNodeIds.add(relation.source_character_id);
      highlightNodeIds.add(relation.target_character_id);
    } else if (nodeId != null) {
      highlightNodeIds.add(nodeId);
      relations.forEach((relation) => {
        if (relation.source_character_id === nodeId || relation.target_character_id === nodeId) {
          highlightRelationIds.add(relation.id);
          highlightNodeIds.add(relation.source_character_id);
          highlightNodeIds.add(relation.target_character_id);
        }
      });
    } else {
      clearGraphHighlight();
      return;
    }

    graph.classList.add("has-highlight");
    graph.querySelectorAll("[data-graph-node]").forEach((element) => {
      const currentNodeId = Number(element.getAttribute("data-graph-node"));
      element.classList.toggle("is-highlighted", highlightNodeIds.has(currentNodeId));
    });
    graph.querySelectorAll("[data-graph-relation-edge]").forEach((element) => {
      const currentRelationId = Number(element.getAttribute("data-graph-relation-edge"));
      element.classList.toggle("is-highlighted", highlightRelationIds.has(currentRelationId));
    });
  }

  function bindGraphHoverInteractions() {
    const graph = document.getElementById("world-graph");
    if (!graph || graph.dataset.hoverBound === "true") return;
    graph.dataset.hoverBound = "true";

    let currentKey = "";

    graph.addEventListener("mousemove", (event) => {
      const target = event.target instanceof Element ? event.target : null;
      const relationEdge = target?.closest?.("[data-graph-relation-edge]");
      const node = target?.closest?.("[data-graph-node]");

      if (relationEdge instanceof Element) {
        const relationId = Number(relationEdge.getAttribute("data-graph-relation-edge"));
        const nextKey = `relation:${relationId}`;
        if (nextKey === currentKey) return;
        currentKey = nextKey;
        applyGraphHighlight({ relationId });
        return;
      }

      if (node instanceof Element) {
        const nodeId = Number(node.getAttribute("data-graph-node"));
        const nextKey = `node:${nodeId}`;
        if (nextKey === currentKey) return;
        currentKey = nextKey;
        applyGraphHighlight({ nodeId });
        return;
      }

      if (currentKey) {
        currentKey = "";
        clearGraphHighlight();
      }
    });

    graph.addEventListener("mouseleave", () => {
      currentKey = "";
      clearGraphHighlight();
    });
  }

  function bindGraphDragging() {
    const graph = document.getElementById("world-graph");
    if (!graph || graph.dataset.dragBound === "true") return;
    graph.dataset.dragBound = "true";

    let dragState = null;

    function pointToGraphCoordinates(clientX, clientY) {
      const rect = graph.getBoundingClientRect();
      return {
        x: (clientX - rect.left - state.graphPan.x) / state.graphScale,
        y: (clientY - rect.top - state.graphPan.y) / state.graphScale,
      };
    }

    graph.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      const node = event.target instanceof HTMLElement ? event.target.closest("[data-graph-node]") : null;
      if (node instanceof HTMLElement) {
        const nodeId = Number(node.dataset.graphNode);
        const point = pointToGraphCoordinates(event.clientX, event.clientY);
        const currentPoint = state.graphLayout.positions[nodeId];
        if (!currentPoint) return;
        event.preventDefault();
        dragState = {
          type: "node",
          nodeId,
          pointerId: event.pointerId,
          offsetX: point.x - currentPoint.x,
          offsetY: point.y - currentPoint.y,
        };
        graph.dataset.dragging = "node";
      } else {
        event.preventDefault();
        dragState = { type: "pan", x: event.clientX, y: event.clientY, pointerId: event.pointerId };
        graph.dataset.dragging = "pan";
      }
      graph.setPointerCapture?.(event.pointerId);
    });

    graph.addEventListener("pointermove", (event) => {
      if (!dragState) return;
      event.preventDefault();
      if (dragState.type === "node") {
        const point = pointToGraphCoordinates(event.clientX, event.clientY);
        state.graphLayout.positions[dragState.nodeId] = clampGraphPoint(
          {
            x: point.x - dragState.offsetX,
            y: point.y - dragState.offsetY,
          },
          state.graphLayout.bounds || resolveGraphBounds(graph, state.graphScene?.visibleCharacters || [])
        );
        if (state.graphScene) {
          updateGraphDomPositions(state.graphScene.visibleCharacters, state.graphScene.visibleRelations);
          applyGraphViewport();
        }
        return;
      }

      state.graphPan.x += event.clientX - dragState.x;
      state.graphPan.y += event.clientY - dragState.y;
      state.graphViewportInitialized = true;
      dragState = { ...dragState, x: event.clientX, y: event.clientY };
      applyGraphViewport();
    });

    graph.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const factor = Math.exp(-event.deltaY * 0.0015);
        zoomGraphTo(state.graphScale * factor, { clientX: event.clientX, clientY: event.clientY });
      },
      { passive: false }
    );

    const stopDragging = (event) => {
      if (!dragState) return;
      if (event?.pointerId != null) {
        graph.releasePointerCapture?.(event.pointerId);
      }
      if (dragState.type === "node") {
        persistGraphLayout();
      }
      state.graphViewportInitialized = true;
      dragState = null;
      graph.dataset.dragging = "false";
    };

    graph.addEventListener("pointerup", stopDragging);
    graph.addEventListener("pointercancel", stopDragging);
    graph.addEventListener("lostpointercapture", stopDragging);
  }

  function bindEntityActionButtons() {
    document.querySelectorAll("[data-edit-character]").forEach((button) => {
      button.addEventListener("click", () => {
        const character = state.characters.find((item) => item.id === Number(button.dataset.editCharacter));
        if (!character) return;
        state.editingCharacterId = character.id;
        document.getElementById("character-form-title").textContent = `编辑人物 #${character.id}`;
        document.getElementById("character-name").value = character.name || "";
        document.getElementById("character-role").value = character.role_label || "";
        document.getElementById("character-importance").value = normalizeImportanceLevel(character) || "";
        document.getElementById("character-biography").value = character.biography || character.description || "";
        setCharacterLifeStatuses(getCharacterLifeStatuses(character));
        state.characterTimelineDraft = getCharacterTimelineEntries(character);
        renderTimelinePreview(state.characterTimelineDraft);
        focusWorldEditor("character-form", "character-name");
      });
    });

    document.querySelectorAll("[data-delete-character]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!window.confirm("确认删除这个人物及其相关关系吗？")) return;
        const characterId = Number(button.dataset.deleteCharacter);
        if (!characterId) return;
        button.disabled = true;
        try {
          await deleteCharacter(bookId, characterId);
          if (state.editingCharacterId === characterId) {
            resetCharacterEditor();
          }
          showToast("人物已删除");
          try {
            await refresh();
          } catch {
            showToast("人物卡已删除，但列表刷新失败，请手动刷新页面", "error");
          }
        } catch (error) {
          const message = error instanceof ApiError ? error.message : "删除人物失败";
          showToast(message, "error");
        } finally {
          button.disabled = false;
        }
      });
    });

    document.querySelectorAll("[data-edit-relation]").forEach((button) => {
      button.addEventListener("click", async () => {
        const relationId = Number(button.dataset.editRelation);
        if (!relationId) return;
        const originalText = button.textContent;
        button.disabled = true;
        button.textContent = "加载中...";
        try {
          const relation = await getRelation(bookId, relationId);
          state.editingRelationId = relation.id;
          document.getElementById("relation-form-title").textContent = `编辑关系 #${relation.id}`;
          document.getElementById("relation-source").value = String(relation.source_character_id);
          document.getElementById("relation-target").value = String(relation.target_character_id);
          document.getElementById("relation-type").value = relation.relation_type || "";
          document.getElementById("relation-label").value = relation.label || "";
          document.getElementById("relation-description").value = relation.description || "";
          document.getElementById("relation-strength").value = relation.strength ?? "";
          document.getElementById("relation-importance").value = relation.importance_level || "major";
          document.getElementById("relation-bidirectional").checked = Boolean(relation.is_bidirectional);
          focusWorldEditor("relation-form", "relation-type");
        } catch (error) {
          const message = error instanceof ApiError ? error.message : "加载关系详情失败";
          showToast(message, "error");
        } finally {
          button.disabled = false;
          button.textContent = originalText || "编辑";
        }
      });
    });

    document.querySelectorAll("[data-view-relation]").forEach((button) => {
      button.addEventListener("click", async () => {
        const relationId = Number(button.dataset.viewRelation);
        if (!relationId) return;
        const originalText = button.textContent;
        button.disabled = true;
        button.textContent = "加载中...";
        try {
          await openRelationDetailModal(relationId);
        } finally {
          button.disabled = false;
          button.textContent = originalText || "查看时间线";
        }
      });
    });

    document.querySelectorAll("[data-delete-relation]").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!window.confirm("确认删除这条关系吗？")) return;
        const relationId = Number(button.dataset.deleteRelation);
        if (!relationId) return;
        button.disabled = true;
        try {
          await deleteRelation(bookId, relationId);
          if (state.editingRelationId === relationId) {
            resetRelationEditor();
          }
          showToast("关系已删除");
          try {
            await refresh();
          } catch {
            showToast("关系已删除，但列表刷新失败，请手动刷新页面", "error");
          }
        } catch (error) {
          const message = error instanceof ApiError ? error.message : "删除关系失败";
          showToast(message, "error");
        } finally {
          button.disabled = false;
        }
      });
    });

    document.querySelectorAll("[data-view-faction]").forEach((button) => {
      button.addEventListener("click", async () => {
        const factionId = Number(button.dataset.viewFaction);
        if (!factionId) return;
        const originalText = button.textContent;
        button.disabled = true;
        button.textContent = "加载中...";
        try {
          await openFactionDetailModal(factionId);
        } finally {
          button.disabled = false;
          button.textContent = originalText || "查看详情";
        }
      });
    });

    document.querySelectorAll("[data-edit-faction]").forEach((button) => {
      button.addEventListener("click", () => {
        const faction = state.factions.find((item) => item.id === Number(button.dataset.editFaction));
        if (!faction) return;
        state.editingFactionId = faction.id;
        document.getElementById("faction-form-title").textContent = `编辑阵营 #${faction.id}`;
        document.getElementById("faction-name").value = faction.name || "";
        document.getElementById("faction-description").value = faction.description || "";
        document.getElementById("faction-color").value = faction.color || "#6b8d52";
        focusWorldEditor("faction-form", "faction-name");
      });
    });

    document.querySelectorAll("[data-delete-faction]").forEach((button) => {
      button.addEventListener("click", async () => {
        const factionId = Number(button.dataset.deleteFaction);
        if (!factionId) return;
        if (!window.confirm("确认删除这个阵营及其成员归属吗？")) return;
        button.disabled = true;
        try {
          await deleteFaction(bookId, factionId);
          if (state.editingFactionId === factionId) {
            resetFactionEditor();
          }
          if (state.activeFactionDetailId === factionId) {
            closeWorldDetailModal();
          }
          showToast("阵营已删除");
          await refresh();
        } catch (error) {
          const message = error instanceof ApiError ? error.message : "删除阵营失败";
          showToast(message, "error");
          button.disabled = false;
        }
      });
    });
  }

  function renderWorldContent({ forceGraphRelayout = false } = {}) {
    const previousVisibleSignature = state.graphScene?.visibleCharacters?.map((item) => item.id).join(",") || "";
    const unfilteredState = buildVisibleWorldState(state.characters, state.relations, state.showMinorCharacters);
    state.relationFilters = syncRelationFilterState(state.relationFilters, unfilteredState.relationFilterMeta);
    const {
      annotatedCharacters,
      visibleCharacters,
      relationCandidates,
      visibleRelations,
      relationFilterMeta,
      hiddenMinorCount,
      hiddenRelationCount,
    } = buildVisibleWorldState(state.characters, state.relations, state.showMinorCharacters, state.relationFilters);
    const filterStats = relationFilterStats(relationFilterMeta, state.relationFilters);
    state.graphScene = {
      annotatedCharacters,
      visibleCharacters,
      relationCandidates,
      visibleRelations,
      hiddenMinorCount,
      hiddenRelationCount,
      relationFilterMeta,
    };
    const graphLayout = ensureGraphLayout(visibleCharacters, visibleRelations, { force: forceGraphRelayout });
    const nextVisibleSignature = visibleCharacters.map((item) => item.id).join(",");
    const shouldRefitViewport =
      forceGraphRelayout || !state.graphViewportInitialized || previousVisibleSignature !== nextVisibleSignature;

    document.getElementById("world-graph").innerHTML = renderGraph(
      visibleCharacters,
      visibleRelations,
      graphLayout,
      { pan: state.graphPan, scale: state.graphScale }
    );
    if (shouldRefitViewport) {
      fitGraphViewport(visibleCharacters, graphLayout);
    }
    applyGraphViewport();
    updateVisibilityControls(hiddenMinorCount);
    renderRelationFilters(relationFilterMeta, hiddenRelationCount);
    state.graphStats = {
      visibleCharacters: visibleCharacters.length,
      totalCharacters: annotatedCharacters.length,
      visibleRelations: visibleRelations.length,
      totalRelations: state.relations.length,
      relationCandidates: relationCandidates.length,
      selectedRelationTypes: filterStats.selected_type_count,
      totalRelationTypes: filterStats.total_type_count,
    };
    renderGraphSummary();

    document.getElementById("world-characters").innerHTML = visibleCharacters.length
      ? visibleCharacters.map(renderCharacterCard).join("")
      : '<div class="bamboo-panel-soft p-5 text-sm text-slate-500">当前筛选下暂无人物卡。</div>';
    document.getElementById("world-relations").innerHTML = visibleRelations.length
      ? visibleRelations.map(renderRelationCard).join("")
      : `<div class="rounded-[1.1rem] bg-white/82 p-4 text-sm text-slate-500 shadow-cloud">${
          relationCandidates.length ? "当前关系筛选下暂无关系记录。" : "当前筛选下暂无关系记录。"
        }</div>`;
    const factionContainer = document.getElementById("world-factions");
    if (factionContainer) {
      factionContainer.innerHTML = state.factions.length
        ? state.factions.map((faction) => renderFactionCard(faction, state.factionMemberships)).join("")
        : '<div class="rounded-[1.1rem] bg-white/82 p-4 text-sm text-slate-500 shadow-cloud">当前没有阵营记录。</div>';
    }

    setCharacterOptions(annotatedCharacters);
  }

  function setJobPolling(enabled) {
    if (enabled && !state.jobPollTimer) {
      state.jobPollTimer = window.setInterval(async () => {
        if (state.pollingJobs) return;
        state.pollingJobs = true;
        try {
          const hadRunning = hasRunningJobs(state.jobs);
          const jobsResponse = await listWorldExtractionJobs(bookId, { limit: 8 });
          state.jobs = jobsResponse.items || [];
          document.getElementById("world-jobs").innerHTML = renderJobList(state.jobs);
          const stillRunning = hasRunningJobs(state.jobs);
          if (!stillRunning) {
            setJobPolling(false);
          }
          if (hadRunning && !stillRunning) {
            await refresh();
          }
        } catch (error) {
          // keep the last known UI state; the next poll can recover
        } finally {
          state.pollingJobs = false;
        }
      }, 5000);
      return;
    }

    if (!enabled && state.jobPollTimer) {
      window.clearInterval(state.jobPollTimer);
      state.jobPollTimer = null;
    }
  }

  async function refresh() {
    const [book, chaptersResponse, charactersResponse, relationsResponse, factionsResponse, membershipsResponse, jobsResponse] = await Promise.all([
      getBook(bookId),
      listChapters(bookId),
      listCharacters(bookId),
      listRelations(bookId),
      listFactions(bookId),
      listFactionMemberships(bookId),
      listWorldExtractionJobs(bookId, { limit: 8 }),
    ]);

    state.chapters = chaptersResponse.items || [];
    worldTimelineChapters = state.chapters;
    state.characters = charactersResponse.items || [];
    state.relations = relationsResponse.items || [];
    state.factions = factionsResponse.items || [];
    state.factionMemberships = membershipsResponse.items || [];
    state.jobs = jobsResponse.items || [];
    state.worldBible = String(book.world_bible || "");
    state.worldFacts = normalizeWorldFacts(book.world_bible);
    if (state.worldFacts.length <= WORLD_FACTS_PREVIEW_LIMIT) {
      state.showAllFacts = false;
    }

    const chapterId = getSelectedChapterId(state.chapters);
    const currentChapter = state.chapters.find((item) => item.id === chapterId) || null;
    setAssistantContext({ bookId, chapterId, chapterTitle: currentChapter?.title || null });
    const chapterSelect = document.getElementById("world-chapter-select");
    chapterSelect.innerHTML = state.chapters.length
      ? state.chapters
          .map(
            (chapter) =>
              `<option value="${chapter.id}" ${chapter.id === chapterId ? "selected" : ""}>${escapeHtml(chapter.title)}</option>`
          )
          .join("")
      : '<option value="">暂无章节</option>';

    renderTimelinePreview(state.characterTimelineDraft);
    renderWorldContent();
    renderWorldFactsSection();
    document.getElementById("world-jobs").innerHTML = renderJobList(state.jobs);
    setJobPolling(hasRunningJobs(state.jobs));
    bindEntityActionButtons();
  }

  await refresh();
  bindGraphDragging();
  bindGraphHoverInteractions();

  document.getElementById("world-jobs").addEventListener("click", async (event) => {
    const target = event.target instanceof HTMLElement ? event.target.closest("button") : null;
    if (!(target instanceof HTMLButtonElement)) {
      return;
    }

    if (target.dataset.openConflicts) {
      const jobId = Number(target.dataset.openConflicts);
      if (!jobId) return;
      target.disabled = true;
      try {
        await openConflictModal(jobId);
      } finally {
        target.disabled = false;
      }
      return;
    }

    if (target.dataset.cancelJob) {
      const jobId = Number(target.dataset.cancelJob);
      if (!jobId) return;
      if (!window.confirm("确认终止这个后台提取任务吗？当前片段可能还会继续几秒后才停止。")) return;
      target.disabled = true;
      try {
        const updatedJob = await cancelWorldExtractionJob(bookId, jobId);
        state.jobs = state.jobs.map((job) => (job.id === updatedJob.id ? updatedJob : job));
        document.getElementById("world-jobs").innerHTML = renderJobList(state.jobs);
        showToast(updatedJob.is_terminated ? "任务已终止" : "已发送终止请求");
        if (updatedJob.status === "pending" || updatedJob.status === "running" || updatedJob.cancel_requested) {
          setJobPolling(true);
        }
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "终止任务失败";
        showToast(message, "error");
        target.disabled = false;
      }
      return;
    }

    if (target.dataset.resumeJob) {
      const jobId = Number(target.dataset.resumeJob);
      if (!jobId) return;
      const deletePrevious = window.confirm("继续提取时，是否同时删除当前这条旧记录？\n选择“确定”会删除旧记录，选择“取消”会保留旧记录。");
      target.disabled = true;
      try {
        const resumedJob = await resumeWorldExtractionJob(bookId, jobId, {
          delete_previous: deletePrevious,
        });
        showToast(`已创建继续提取任务 #${resumedJob.id}`);
        await refresh();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "继续提取失败";
        showToast(message, "error");
        target.disabled = false;
      }
      return;
    }

    if (target.dataset.retryFailedJob) {
      const jobId = Number(target.dataset.retryFailedJob);
      if (!jobId) return;
      const deletePrevious = window.confirm("重试失败片段时，是否同时删除当前这条旧记录？\n选择“确定”会删除旧记录，选择“取消”会保留旧记录。");
      target.disabled = true;
      try {
        const resumedJob = await resumeWorldExtractionJob(bookId, jobId, {
          delete_previous: deletePrevious,
          failed_only: true,
        });
        showToast(`已创建失败片段重试任务 #${resumedJob.id}`);
        await refresh();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "重试失败片段失败";
        showToast(message, "error");
        target.disabled = false;
      }
      return;
    }

    if (target.dataset.deleteJob) {
      const jobId = Number(target.dataset.deleteJob);
      if (!jobId) return;
      if (!window.confirm("确认删除这条后台提取记录吗？")) return;
      target.disabled = true;
      try {
        await deleteWorldExtractionJob(bookId, jobId);
        showToast("后台提取记录已删除");
        await refresh();
      } catch (error) {
        const message = error instanceof ApiError ? error.message : "删除记录失败";
        showToast(message, "error");
        target.disabled = false;
      }
    }
  });

  document.getElementById("world-toggle-minor").addEventListener("click", () => {
    state.showMinorCharacters = !state.showMinorCharacters;
    renderWorldContent();
    bindEntityActionButtons();
  });

  document.getElementById("world-toggle-relation-filters")?.addEventListener("click", () => {
    state.showRelationFilters = !state.showRelationFilters;
    renderWorldContent();
    bindEntityActionButtons();
  });

  document.getElementById("world-relation-filters")?.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;

    if (target.dataset.relationCategoryToggle) {
      state.relationFilters.categories[target.dataset.relationCategoryToggle] = target.checked;
      renderWorldContent();
      bindEntityActionButtons();
      return;
    }

    if (target.dataset.relationTypeToggle) {
      state.relationFilters.types[target.dataset.relationTypeToggle] = target.checked;
      if (target.checked && target.dataset.relationCategoryKey) {
        state.relationFilters.categories[target.dataset.relationCategoryKey] = true;
      }
      renderWorldContent();
      bindEntityActionButtons();
    }
  });

  document.getElementById("world-relation-filters")?.addEventListener("click", (event) => {
    const target = event.target instanceof HTMLElement ? event.target.closest("[data-relation-filter-action]") : null;
    if (!(target instanceof HTMLButtonElement)) return;

    const action = target.dataset.relationFilterAction;
    if (!state.graphScene?.relationFilterMeta) return;

    state.graphScene.relationFilterMeta.categories.forEach((category) => {
      const checked = action === "select-all";
      state.relationFilters.categories[category.key] = checked;
      category.types.forEach((type) => {
        state.relationFilters.types[type.key] = checked;
      });
    });

    renderWorldContent();
    bindEntityActionButtons();
  });

  document.getElementById("world-delete-all-characters").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (!state.characters.length) {
      showToast("当前没有可删除的人物卡", "error");
      return;
    }
    if (!window.confirm("确认一键删除当前书籍的所有人物卡吗？关联关系也会一并清空。")) {
      return;
    }
    button.disabled = true;
    try {
      const result = await deleteAllCharacters(bookId);
      const deletedCharacterCount = result.deleted_character_count || 0;
      const deletedRelationCount = result.deleted_relation_count || 0;
      resetCharacterEditor();
      resetRelationEditor();
      showToast(`已删除 ${deletedCharacterCount} 张人物卡，并清理 ${deletedRelationCount} 条关系`);
      await refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "批量删除人物卡失败";
      showToast(message, "error");
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("world-delete-all-relations").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (!state.relations.length) {
      showToast("当前没有可删除的关系", "error");
      return;
    }
    if (!window.confirm("确认一键删除当前书籍的所有关系吗？")) {
      return;
    }
    button.disabled = true;
    try {
      const result = await deleteAllRelations(bookId);
      const deletedCount = result.deleted_count || 0;
      resetRelationEditor();
      showToast(`已删除 ${deletedCount} 条关系`);
      await refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "批量删除关系失败";
      showToast(message, "error");
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("world-toggle-facts").addEventListener("click", () => {
    state.showAllFacts = !state.showAllFacts;
    renderWorldFactsSection();
  });

  document.getElementById("world-delete-all-facts").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (!state.worldFacts.length) {
      showToast("当前没有可删除的世界观事实", "error");
      return;
    }
    if (!window.confirm("确认一键删除当前书籍的所有世界观事实吗？")) {
      return;
    }

    button.disabled = true;
    try {
      const updatedBook = await updateBook(bookId, { world_bible: null });
      state.worldBible = String(updatedBook.world_bible || "");
      state.worldFacts = [];
      state.showAllFacts = false;
      renderWorldFactsSection();
      showToast("已删除全部世界观事实");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "批量删除世界观事实失败";
      showToast(message, "error");
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("world-facts").addEventListener("click", async (event) => {
    const button = event.target instanceof HTMLElement ? event.target.closest("[data-delete-world-fact]") : null;
    if (!(button instanceof HTMLButtonElement)) return;

    const fact = String(button.dataset.deleteWorldFact || "").trim();
    if (!fact) return;
    if (!window.confirm("确认删除这条世界观事实吗？")) return;

    button.disabled = true;
    try {
      const nextWorldBible = removeWorldFactFromBible(state.worldBible, fact);
      const updatedBook = await updateBook(bookId, { world_bible: nextWorldBible || null });
      state.worldBible = String(updatedBook.world_bible || "");
      state.worldFacts = normalizeWorldFacts(state.worldBible);
      if (state.worldFacts.length <= WORLD_FACTS_PREVIEW_LIMIT) {
        state.showAllFacts = false;
      }
      renderWorldFactsSection();
      showToast("世界观事实已删除");
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "删除世界观事实失败";
      showToast(message, "error");
      button.disabled = false;
    }
  });

  document.getElementById("world-reset-graph").addEventListener("click", () => {
    if (state.graphScene?.visibleCharacters?.length) {
      fitGraphViewport(state.graphScene.visibleCharacters, state.graphLayout);
    }
    applyGraphViewport();
  });

  document.getElementById("world-relayout-graph")?.addEventListener("click", () => {
    renderWorldContent({ forceGraphRelayout: true });
    bindEntityActionButtons();
  });

  document.getElementById("world-zoom-in")?.addEventListener("click", () => {
    zoomGraphTo(state.graphScale * (1 + GRAPH_SCALE_STEP));
  });

  document.getElementById("world-zoom-out")?.addEventListener("click", () => {
    zoomGraphTo(state.graphScale / (1 + GRAPH_SCALE_STEP));
  });

  document.getElementById("world-graph-fullscreen")?.addEventListener("click", async () => {
    const panel = document.getElementById("world-graph-panel");
    if (panel?.requestFullscreen) {
      await panel.requestFullscreen();
      return;
    }
    panel?.webkitRequestFullscreen?.();
  });

  document.getElementById("world-graph-exit-fullscreen")?.addEventListener("click", async () => {
    if (!document.fullscreenElement && !document.webkitFullscreenElement) return;
    if (document.exitFullscreen) {
      await document.exitFullscreen();
      return;
    }
    document.webkitExitFullscreen?.();
  });

  document.addEventListener("fullscreenchange", updateGraphFullscreenButtons);
  document.addEventListener("webkitfullscreenchange", updateGraphFullscreenButtons);
  let graphResizeTimer = null;
  window.addEventListener("resize", () => {
    if (!state.graphScene?.visibleCharacters?.length) return;
    window.clearTimeout(graphResizeTimer);
    graphResizeTimer = window.setTimeout(() => {
      renderWorldContent();
      bindEntityActionButtons();
    }, 250);
  });
  updateGraphFullscreenButtons();

  document.getElementById("world-import-file").addEventListener("change", (event) => {
    const file = event.currentTarget.files?.[0];
    state.importEstimateRequestId += 1;
    if (!file) {
      resetImportFileUi();
      return;
    }
    const validationMessage = validateImportFile(file);
    if (validationMessage) {
      resetImportFileUi(validationMessage);
      return;
    }
    requestImportEstimate(file);
  });

  document.getElementById("world-update-bible")?.addEventListener("change", () => {
    const file = document.getElementById("world-import-file")?.files?.[0];
    if (!file || validateImportFile(file)) return;
    requestImportEstimate(file);
  });

  const conflictCloseButton = document.getElementById("world-conflict-close");
  const conflictModal = document.getElementById("world-conflict-modal");
  const detailCloseButton = document.getElementById("world-detail-close");
  const detailModal = document.getElementById("world-detail-modal");
  if (conflictCloseButton) {
    conflictCloseButton.addEventListener("click", () => {
      closeConflictModal();
    });
  }
  if (conflictModal) {
    conflictModal.addEventListener("click", (event) => {
      if (event.target.id === "world-conflict-modal") {
        closeConflictModal();
      }
    });
  }
  if (detailCloseButton) {
    detailCloseButton.addEventListener("click", () => {
      closeWorldDetailModal();
    });
  }
  if (detailModal) {
    detailModal.addEventListener("click", (event) => {
      if (event.target?.id === "world-detail-modal") {
        closeWorldDetailModal();
      }
    });
  }

  document.getElementById("character-life-statuses")?.addEventListener("change", () => {
    syncCharacterLifeStatusChoiceState();
  });
  syncCharacterLifeStatusChoiceState();

  bookSelect.addEventListener("change", () => {
    const nextBookId = bookSelect.value;
    window.location.href = `/world?book=${nextBookId}`;
  });

  document.getElementById("world-chapter-select").addEventListener("change", () => {
    const params = new URLSearchParams(window.location.search);
    params.set("book", String(bookId));
    params.set("chapter", document.getElementById("world-chapter-select").value);
    window.location.href = `/world?${params.toString()}`;
  });

  document.getElementById("world-extract").addEventListener("click", async () => {
    const chapterId = Number(document.getElementById("world-chapter-select").value);
    if (!chapterId) {
      showToast("请先选择章节", "error");
      return;
    }

    setExtractionBusy(true);
    try {
      const result = await extractWorld(bookId, chapterId, {
        dry_run: false,
        update_world_bible: document.getElementById("world-update-bible").checked,
      });
      document.getElementById("world-extract-result").innerHTML = renderChapterExtractionResult(result);
      showToast("AI 世界观提取已完成");
      await refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "AI 提取失败";
      showToast(message, "error");
    } finally {
      setExtractionBusy(false);
    }
  });

  document.getElementById("world-extract-batch").addEventListener("click", async () => {
    setExtractionBusy(true);
    try {
      const job = await startWorldExtractionJob(bookId, {
        update_world_bible: document.getElementById("world-update-bible").checked,
        chapter_scope: document.getElementById("world-batch-content-only").checked ? "with_content" : "all",
        conflict_strategy: currentConflictStrategy(),
        segment_unit_limit: 36000,
        skip_unchanged_chapters: true,
      });
      document.getElementById("world-extract-result").innerHTML = renderQueuedJob(job);
      showToast(`后台任务 #${job.id} 已创建，正在提取软件内全书`);
      await refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "AI 提取失败";
      showToast(message, "error");
    } finally {
      setExtractionBusy(false);
    }
  });

  document.getElementById("world-import-extract").addEventListener("click", async () => {
    const fileInput = document.getElementById("world-import-file");
    const file = fileInput.files?.[0];
    const validationMessage = validateImportFile(file);
    if (validationMessage) {
      showToast(validationMessage, "error");
      return;
    }

    setExtractionBusy(true);
    document.getElementById("world-extract-result").innerHTML = `
      <div class="space-y-3">
        <div class="text-sm text-slate-600">正在上传并排队处理：${escapeHtml(file.name)}</div>
        <div class="text-sm text-slate-600">系统会自动按段切分，导入完成后可在下方查看后台任务进度。</div>
      </div>
    `;
    try {
      const job = await uploadWorldExtractionDocument(bookId, file, {
        update_world_bible: document.getElementById("world-update-bible").checked,
        conflict_strategy: currentConflictStrategy(),
        segment_unit_limit: 36000,
      });
      document.getElementById("world-extract-result").innerHTML = renderQueuedJob(job);
      fileInput.value = "";
      state.importEstimateRequestId += 1;
      resetImportFileUi();
      showToast(`后台任务 #${job.id} 已创建，正在导入原著并提取`);
      await refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "导入原著提取失败";
      document.getElementById("world-extract-result").innerHTML = `
        <div class="rounded-2xl bg-rose-50 px-4 py-3 text-sm leading-7 text-rose-700">${escapeHtml(message)}</div>
      `;
      showToast(message, "error");
    } finally {
      setExtractionBusy(false);
    }
  });

  document.getElementById("character-reset").addEventListener("click", resetCharacterEditor);

  document.getElementById("character-timeline-manage")?.addEventListener("click", openTimelineModal);
  document.getElementById("character-timeline-add")?.addEventListener("click", () => {
    const nextEntries = collectTimelineEditorEntries("character-timeline-list");
    nextEntries.push({});
    renderTimelineEditor(nextEntries, "character-timeline-list");
  });
  document.getElementById("character-timeline-close")?.addEventListener("click", requestCloseTimelineModal);
  document.getElementById("character-timeline-cancel")?.addEventListener("click", requestCloseTimelineModal);
  document.getElementById("character-timeline-save")?.addEventListener("click", () => {
    state.characterTimelineDraft = collectTimelineEditorEntries("character-timeline-list");
    renderTimelinePreview(state.characterTimelineDraft);
    state.timelineModalSnapshot = serializeTimelineModalState();
    closeTimelineModal();
  });
  document.getElementById("character-timeline-modal")?.addEventListener("click", (event) => {
    if (event.target?.id === "character-timeline-modal") {
      requestCloseTimelineModal();
    }
  });

  document.getElementById("relation-reset").addEventListener("click", resetRelationEditor);
  document.getElementById("faction-reset").addEventListener("click", resetFactionEditor);

  document.getElementById("character-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const editingCharacter = state.characters.find((item) => item.id === state.editingCharacterId) || null;
    const lifeStatuses = readCharacterLifeStatuses();
    const timelineEntries = state.characterTimelineDraft;
    const importanceLevel = document.getElementById("character-importance").value.trim();
    const payload = {
      name: document.getElementById("character-name").value.trim(),
      role_label: document.getElementById("character-role").value.trim() || null,
      biography: document.getElementById("character-biography").value.trim() || null,
      life_statuses: lifeStatuses,
      timeline_entries: timelineEntries,
      card_json: mergeCharacterCardJson(editingCharacter, lifeStatuses, importanceLevel),
    };

    try {
      if (state.editingCharacterId) {
        await updateCharacter(bookId, state.editingCharacterId, payload);
        showToast("人物已更新");
      } else {
        await createCharacter(bookId, payload);
        showToast("人物已创建");
      }
      document.getElementById("character-reset").click();
      await refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "保存人物失败";
      showToast(message, "error");
    }
  });

  document.getElementById("relation-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      source_character_id: Number(document.getElementById("relation-source").value),
      target_character_id: Number(document.getElementById("relation-target").value),
      relation_type: document.getElementById("relation-type").value.trim(),
      label: document.getElementById("relation-label").value.trim() || null,
      description: document.getElementById("relation-description").value.trim() || null,
      strength:
        document.getElementById("relation-strength").value === ""
          ? null
          : Number(document.getElementById("relation-strength").value),
      importance_level: document.getElementById("relation-importance").value || "major",
      is_bidirectional: document.getElementById("relation-bidirectional").checked,
    };

    try {
      if (state.editingRelationId) {
        await updateRelation(bookId, state.editingRelationId, payload);
        showToast("关系已更新");
      } else {
        await createRelation(bookId, payload);
        showToast("关系已创建");
      }
      document.getElementById("relation-reset").click();
      await refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "保存关系失败";
      showToast(message, "error");
    }
  });

  document.getElementById("faction-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      name: document.getElementById("faction-name").value.trim(),
      description: document.getElementById("faction-description").value.trim() || null,
      color: document.getElementById("faction-color").value || "#6b8d52",
    };

    try {
      if (state.editingFactionId) {
        await updateFaction(bookId, state.editingFactionId, payload);
        showToast("阵营已更新");
      } else {
        await createFaction(bookId, payload);
        showToast("阵营已创建");
      }
      resetFactionEditor();
      await refresh();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "保存阵营失败";
      showToast(message, "error");
    }
  });
}
