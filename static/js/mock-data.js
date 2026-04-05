export const libraryBooks = [
  {
    id: 1,
    title: "竹影入长安",
    genre: "东方幻想",
    progress: 68,
    chapters: 24,
    updatedAt: "2026-03-25 22:10",
    summary: "主线进入宫廷密约阶段，人物关系进入高密度博弈。",
    status: "连载中",
  },
  {
    id: 2,
    title: "群山回声",
    genre: "悬疑成长",
    progress: 34,
    chapters: 11,
    updatedAt: "2026-03-24 19:45",
    summary: "第二幕刚完成设伏，AI 摘要已抽出 14 条伏笔线索。",
    status: "大纲扩写",
  },
  {
    id: 3,
    title: "潮汐档案",
    genre: "近未来科幻",
    progress: 87,
    chapters: 37,
    updatedAt: "2026-03-22 08:30",
    summary: "终局章节进入收束，适合做版本冻结和差异审校。",
    status: "终稿校对",
  },
];

export const writingWorkspace = {
  activeBook: "竹影入长安",
  activeChapter: "第十二章 · 雨后石阶",
  chapterTree: [
    { title: "卷一 · 山门", status: "已定稿" },
    { title: "第十章 · 夜灯", status: "已定稿" },
    { title: "第十一章 · 白马渡口", status: "审阅中" },
    { title: "第十二章 · 雨后石阶", status: "伴写中", active: true },
    { title: "第十三章 · 假令", status: "待扩写" },
  ],
  voiceModes: ["克制叙述", "古典白描", "压迫氛围"],
  memoryBlocks: [
    { title: "系统文风", text: "冷静、内敛、保留古典留白，不使用过度解释。" },
    { title: "远期摘要", text: "主角此章不破局，只确认宦官集团与山门旧案有关。" },
    { title: "关联人物 JSON", text: "林晏、沈知微、裴掌院三人关系张力上升，信任度重新排序。" },
  ],
  suggestions: [
    "强化脚步声与潮湿石阶的听觉意象，承接上一章的压迫感。",
    "在段尾加入一条轻微误导，为第十三章的假令做铺垫。",
    "对话减少一轮，改用动作和视线处理权力差。",
  ],
  editorText:
    "雨是在四更时停的。\n\n山门石阶仍泛着冷白的湿光，像有人将一层薄霜铺在了青石上。林晏踩上去时，鞋底并不打滑，只发出极轻的一声涩响，仿佛什么旧事在暗处被人轻轻翻了一页。\n\n他没有立刻抬头。\n\n檐角的滴水还在继续，三息一落，稳得近乎刻意。这样的稳，比急更像警告。",
};

export const aiConfigs = [
  {
    module: "伴写",
    model: "deepseek-chat",
    baseUrl: "https://api.example.com/v1",
    priority: 10,
    mode: "OpenAI v1 兼容",
  },
  {
    module: "大纲扩写",
    model: "deepseek-reasoner",
    baseUrl: "https://api.example.com/v1",
    priority: 20,
    mode: "Reasoner 规划 + 分块生成",
  },
  {
    module: "设定提取",
    model: "gpt-4.1-mini",
    baseUrl: "https://api.example.com/v1",
    priority: 30,
    mode: "结构化 JSON 输出",
  },
];

export const snapshotHistory = [
  {
    id: 301,
    label: "AI 伴写前自动快照",
    chapter: "第十二章 · 雨后石阶",
    createdAt: "2026-03-25 21:43",
    author: "admin",
    changes: { add: 486, remove: 71, ratio: "14.2%" },
    before:
      "山门外一片寂静。林晏站在台阶下，觉得风有些冷。他想起昨夜那封信，不知道是否该继续往前走。",
    after:
      "雨是在四更时停的。山门石阶仍泛着冷白的湿光，像有人将一层薄霜铺在了青石上。林晏踩上去时，鞋底并不打滑，只发出极轻的一声涩响。",
    summary: "加强了环境压迫感，并把‘迟疑’改成了更克制的动作描写。",
  },
  {
    id: 302,
    label: "手动保存",
    chapter: "第十一章 · 白马渡口",
    createdAt: "2026-03-24 18:16",
    author: "editor-01",
    changes: { add: 132, remove: 18, ratio: "4.8%" },
    before:
      "渡口人声嘈杂，沈知微看见船夫正在和商贩争论价钱。她没有停留，径直走向停在最里面的黑篷船。",
    after:
      "渡口嘈杂得近乎刻意。沈知微只看了一眼争价的船夫，便压低伞沿，穿过人群，走向最里面那艘黑篷船。",
    summary: "节奏更紧，人物行动更明确。",
  },
];

export const characters = [
  { name: "林晏", role: "主角", trait: "克制、审慎、感知敏锐", goal: "确认山门旧案背后的朝堂势力" },
  { name: "沈知微", role: "盟友", trait: "外冷内热、判断迅速", goal: "保护线人，同时阻止消息外泄" },
  { name: "裴掌院", role: "关键阻力", trait: "沉稳、模糊、善于引导", goal: "维持学院与朝局间的脆弱平衡" },
  { name: "顾持灯", role: "潜在敌友", trait: "话少、执行力强", goal: "接近主角并判断其是否可控" },
];

export const relationGraph = [
  { from: "林晏", to: "沈知微", label: "互信上升" },
  { from: "林晏", to: "裴掌院", label: "试探" },
  { from: "沈知微", to: "顾持灯", label: "警惕" },
  { from: "裴掌院", to: "顾持灯", label: "默认协作" },
];

export const worldFacts = [
  "山门旧案距今十二年，牵连朝中三司。",
  "白马渡口是情报交换高频地点，夜间流量异常。",
  "宫中近期启用新印令，假令体系开始渗透地方。",
];

export const adminUsers = [
  { username: "admin", role: "super_admin", status: "active", lastLogin: "2026-03-26 09:12" },
  { username: "editor-01", role: "author", status: "active", lastLogin: "2026-03-25 21:40" },
  { username: "editor-02", role: "author", status: "locked", lastLogin: "2026-03-21 18:05" },
  { username: "ops", role: "admin", status: "active", lastLogin: "2026-03-24 10:23" },
];

export const adminAudit = [
  "2026-03-26 09:12 admin 重置了超级管理员密码策略说明",
  "2026-03-25 22:18 ops 为 editor-01 分配了《竹影入长安》写作权限",
  "2026-03-25 21:43 系统为第十二章创建 AI 快照 #301",
];
