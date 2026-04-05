export const BOOK_STYLE_PRESET_SOURCE = "yukino_dionysia_0306";

export const BOOK_STYLE_PRESET_CATEGORIES = Object.freeze([
  { id: "foundation", name: "写作基础" },
  { id: "cn_literature", name: "中文文学" },
  { id: "east_asia", name: "东亚叙事" },
  { id: "world_literature", name: "世界文学" },
  { id: "genre", name: "类型题材" },
]);

export const BOOK_STYLE_PRESETS = Object.freeze([
  {
    id: "foundation_scene_transmutation",
    category: "foundation",
    name: "场景转译写法",
    summary: "把设定和摘要转成场景、动作、感官和人物反应，不直抄资料。",
    prompt: [
      "[写作基础·场景转译]",
      "- 不要把设定、人物卡、摘要原文直接搬进正文。",
      "- 先把信息转译为角色当下能感到的声音、气味、动作阻力、空间细节和对话分寸。",
      "- 设定通过物件、规矩、后果和人的反应显影，而不是资料说明。",
      "- 每段都尽量落到具体画面、动作链和情绪变化上。",
    ].join("\n"),
  },
  {
    id: "foundation_character_agency",
    category: "foundation",
    name: "角色欲望驱动",
    summary: "让人物带着自己的欲望、顾虑和立场行动，而不是当情节工具。",
    prompt: [
      "[写作基础·角色欲望驱动]",
      "- 人物必须带着明确欲望、顾虑、偏见或立场行动。",
      "- 不要把角色写成只为推动剧情出现的工具人。",
      "- 冲突优先来自人物选择、误判、克制与代价，而不是作者硬推。",
      "- 多人同场时保持群像流动，让每个人的反应都带着自己的位置感。",
    ].join("\n"),
  },
  {
    id: "foundation_relation_progression",
    category: "foundation",
    name: "关系递进",
    summary: "关系靠日常试探、互动累积和具体事件推进，不靠突然跳级。",
    prompt: [
      "[写作基础·关系递进]",
      "- 人物关系通过日常试探、共同经历、利益冲突和细小偏移慢慢推进。",
      "- 不要突然从陌生跳到生死相托，也不要硬把一次对话写成命运羁绊。",
      "- 亲密、敌意、信任、依赖都要有具体触发点和可见后果。",
      "- 关系变化尽量落在对白分寸、称呼、站位、照顾方式和回避动作上。",
    ].join("\n"),
  },
  {
    id: "foundation_environment_participation",
    category: "foundation",
    name: "环境参与叙事",
    summary: "环境不是背景板，要参与到情绪、动作和冲突里。",
    prompt: [
      "[写作基础·环境参与叙事]",
      "- 环境不是静态背景，要参与人物选择、节奏和气氛变化。",
      "- 光线、天气、噪音、空间拥挤度和物件位置都应对场景产生作用。",
      "- 尽量让场景中的门、桌、楼道、雨、气味、温度成为叙事力量的一部分。",
      "- 少写空泛氛围词，多写环境如何改变人的动作和判断。",
    ].join("\n"),
  },
  {
    id: "foundation_clean_output",
    category: "foundation",
    name: "克制输出",
    summary: "不写说明腔、格式腔和作者讲解，正文只给目标内容。",
    prompt: [
      "[写作基础·克制输出]",
      "- 正文不要出现解释自己在做什么的说明话术。",
      "- 不要写标题回顾、结构提示、总结升华、说教或旁白式催问。",
      "- 情绪优先通过动作、停顿、视线和环境噪点显影。",
      "- 避免浮夸副词堆叠、廉价深情和网络套路腔。",
    ].join("\n"),
  },
  {
    id: "cn_worldly_realism",
    category: "cn_literature",
    name: "古典世情写实",
    summary: "繁华肌理里藏消逝之意，写人情世态，闲笔里见要紧。",
    prompt: [
      "[中文文学·古典世情写实]",
      "- 叙事目标：极摹人情世态之歧，写普通人的心事如何一步步走到覆水难收。",
      "- 核心原则：以物色写人情，以闲笔藏要紧，不急着替角色下判断。",
      "- 语调：半文半白，雅俗相间，家长里短里要有来历和分寸。",
      "- 重点：居所、衣饰、器物和饭食都用来写人，不做空转装饰。",
    ].join("\n"),
  },
  {
    id: "cn_pastoral_lyricism",
    category: "cn_literature",
    name: "京派山水抒情",
    summary: "文字清透克制，日常与风土中见人情与余韵。",
    prompt: [
      "[中文文学·京派山水抒情]",
      "- 气质：清澈、含蓄、带人间烟火气，不故作宏大。",
      "- 多写地方风土、河流街巷、饮食器物与人物心性之间的暗合。",
      "- 句子宜干净舒展，给景物和停顿留白，不要把情绪喊出来。",
      "- 悲喜都收着写，让余味落在景、声和小动作里。",
    ].join("\n"),
  },
  {
    id: "cn_meticulous_urban",
    category: "cn_literature",
    name: "都市工笔写实",
    summary: "细密书写城市生活肌理，人物关系靠日常磨出来。",
    prompt: [
      "[中文文学·都市工笔写实]",
      "- 重点书写城市生活的层次、家庭空间、日常秩序和细碎拉扯。",
      "- 关系变化放在饭桌、走廊、闲谈、照顾和回避里慢慢显形。",
      "- 句子要稳，细节要密，但不要失去人物的呼吸和温度。",
      "- 不追求强戏剧起伏，更重视长期消耗与微妙转折。",
    ].join("\n"),
  },
  {
    id: "cn_wry_critical",
    category: "cn_literature",
    name: "冷面讽刺",
    summary: "用轻松外壳包裹锋利观察，嘲讽不靠喊口号。",
    prompt: [
      "[中文文学·冷面讽刺]",
      "- 语气机灵、冷静、带一点旁观者的清醒和幽默。",
      "- 嘲讽来自人物处境、语言反差和制度荒诞，不靠作者直接发言。",
      "- 保持句子利落，能一针见血就不要绕远。",
      "- 即便批判，也要让人物像活人而不是论点载体。",
    ].join("\n"),
  },
  {
    id: "east_asia_minimal_realism",
    category: "east_asia",
    name: "极简对白现实主义",
    summary: "对白短而准，靠空白和停顿制造张力。",
    prompt: [
      "[东亚叙事·极简对白现实主义]",
      "- 对话要短、准、含蓄，让真正重要的东西藏在没说出口的部分。",
      "- 少解释情绪，多写沉默、动作、打断和说话前后的微小犹豫。",
      "- 画面尽量朴素，避免过度修辞，让细节自己发力。",
      "- 冲突不必外放，压住时反而更有力量。",
    ].join("\n"),
  },
  {
    id: "east_asia_lyrical_sensory",
    category: "east_asia",
    name: "感官抒情",
    summary: "用季节、光线、触感和细碎物象包裹情绪流动。",
    prompt: [
      "[东亚叙事·感官抒情]",
      "- 情绪通过季节、温度、光影、气味和触感慢慢渗出。",
      "- 画面要细致柔软，但不能堆砌辞藻和空灵形容。",
      "- 重视片刻感受、微小失神和身体反应，不要强行升华。",
      "- 人物关系宜暧昧、克制、带留白。",
    ].join("\n"),
  },
  {
    id: "east_asia_confessional",
    category: "east_asia",
    name: "私小说自白",
    summary: "近距离书写羞耻、脆弱、迟疑和自我剖开。",
    prompt: [
      "[东亚叙事·私小说自白]",
      "- 允许人物暴露自己的自卑、摇摆、逃避和不体面。",
      "- 语气要真，不要把自我剖白写成漂亮标语。",
      "- 重点在心理缠绕与行为失措，而不是宏大事件。",
      "- 保留一点自嘲和无奈，让脆弱显得真实。",
    ].join("\n"),
  },
  {
    id: "east_asia_youth_ambiguity",
    category: "east_asia",
    name: "青春暧昧日常",
    summary: "轻微情绪震荡、日常互动和若即若离感更重要。",
    prompt: [
      "[东亚叙事·青春暧昧日常]",
      "- 重点写微妙心动、误读、试探和不肯说破的分寸。",
      "- 事件不必大，教室、回家路、便利店、消息框都可以成为主场景。",
      "- 对白自然、轻一点，保留年轻人的别扭和闪躲。",
      "- 不要硬煽情，让情绪停在半步未满的位置。",
    ].join("\n"),
  },
  {
    id: "east_asia_dry_comedy",
    category: "east_asia",
    name: "脱力日常喜剧",
    summary: "节奏轻、观察偏冷，笑点来自荒诞日常和人物反差。",
    prompt: [
      "[东亚叙事·脱力日常喜剧]",
      "- 喜感来自一本正经的荒诞、错位反应和不合时宜的认真。",
      "- 保持轻巧节奏，不要把笑点解释给读者听。",
      "- 人物越认真，场面越离谱，效果通常越好。",
      "- 允许留白和冷场，让笑意从停顿里出来。",
    ].join("\n"),
  },
  {
    id: "world_stream_of_consciousness",
    category: "world_literature",
    name: "意识流心理",
    summary: "重视感知流、记忆闪回和内在时间，而不是事件汇报。",
    prompt: [
      "[世界文学·意识流心理]",
      "- 重点写意识如何流动、跳转、回环，而不是把事件按提纲复述。",
      "- 时间可以沿着联想、触发物和情绪波动伸缩。",
      "- 句子允许更柔软、更回旋，但仍要保持可读性和清晰主体。",
      "- 内心与外界要相互牵引，不要变成纯独白堆积。",
    ].join("\n"),
  },
  {
    id: "world_gothic_suspense",
    category: "world_literature",
    name: "哥特悬疑",
    summary: "用空间阴影、异常细节和不安感推动悬念。",
    prompt: [
      "[世界文学·哥特悬疑]",
      "- 场景应带阴影、潮气、旧物感和逼仄压力，让空间本身制造不安。",
      "- 悬念来自异常细节、感知偏差、时间延迟和信息错位。",
      "- 语气可以冷、密、略带阴森，但不要故作惊悚辞藻。",
      "- 真相应一步步逼近，不要过早把谜底说穿。",
    ].join("\n"),
  },
  {
    id: "world_british_romcom",
    category: "world_literature",
    name: "英式浪漫轻喜剧",
    summary: "感情线轻快、机智、带尴尬感，靠节奏和对白取胜。",
    prompt: [
      "[世界文学·英式浪漫轻喜剧]",
      "- 语气轻盈、机智、略带笨拙和体面崩塌时的可爱感。",
      "- 感情推进靠误会、错拍、临场失控和嘴硬心软。",
      "- 对白讲究节奏和回弹，笑点来自关系互动而不是段子拼贴。",
      "- 保持温暖底色，不要把轻喜剧写成油滑贫嘴。",
    ].join("\n"),
  },
  {
    id: "genre_detective_grotesque",
    category: "genre",
    name: "变格推理",
    summary: "诡异感、线索控制和心理压迫并行，重证据与氛围。",
    prompt: [
      "[类型题材·变格推理]",
      "- 先稳住线索、动机、异常点和证据链，再去营造诡异气氛。",
      "- 异常必须可追踪、可回看，不要只靠惊吓和玄乎词。",
      "- 人物行为要服务谜面与真相，同时保留心理压迫感。",
      "- 悬疑感来自信息控制和视角限制，不来自故弄玄虚。",
    ].join("\n"),
  },
  {
    id: "genre_epic_fantasy",
    category: "genre",
    name: "权谋奇幻史诗",
    summary: "重世界秩序、阵营博弈、代价感和宏观局势压力。",
    prompt: [
      "[类型题材·权谋奇幻史诗]",
      "- 世界观要有秩序、阶层、资源约束和长期后果。",
      "- 权谋冲突来自立场、利益、盟约和背叛，不靠强行反转。",
      "- 战争、王权、宗教或家族压力要真实压到人物身上。",
      "- 即便视野宏大，也要把关键情绪落回个人抉择和代价。",
    ].join("\n"),
  },
  {
    id: "genre_wuxia_epic",
    category: "genre",
    name: "武侠史诗",
    summary: "江湖义理、门派格局和人物抉择并重，豪气里带人情。",
    prompt: [
      "[类型题材·武侠史诗]",
      "- 江湖不是空壳，要有门派规矩、恩怨来历、行走路径和名望秩序。",
      "- 人物既有侠义与担当，也要有私情、偏见和软肋。",
      "- 动作场面要清楚、利落，有招式逻辑和空间感。",
      "- 豪气之外要有人情与世情，不把武侠写成口号。",
    ].join("\n"),
  },
]);

const BOOK_STYLE_PRESET_MAP = new Map(BOOK_STYLE_PRESETS.map((item) => [item.id, item]));

export function findBookStylePresetById(id) {
  const normalizedId = String(id || "").trim();
  return normalizedId ? BOOK_STYLE_PRESET_MAP.get(normalizedId) || null : null;
}

export function normalizeBookStylePresetIds(ids) {
  if (!Array.isArray(ids)) return [];
  const seen = new Set();
  const normalized = [];
  for (const rawId of ids) {
    const id = String(rawId || "").trim();
    if (!id || seen.has(id) || !BOOK_STYLE_PRESET_MAP.has(id)) continue;
    seen.add(id);
    normalized.push(id);
  }
  return normalized;
}

export function listBookStylePresetsByCategory() {
  return BOOK_STYLE_PRESET_CATEGORIES.map((category) => ({
    ...category,
    items: BOOK_STYLE_PRESETS.filter((item) => item.category === category.id),
  })).filter((category) => category.items.length);
}

export function composeBookStylePrompt(selectedIds = [], customText = "") {
  const normalizedIds = normalizeBookStylePresetIds(selectedIds);
  const sections = [];

  if (normalizedIds.length) {
    const names = normalizedIds
      .map((id) => findBookStylePresetById(id))
      .filter(Boolean)
      .map((item) => item.name);
    if (names.length) {
      sections.push(`[已选文风模块]\n${names.join(" / ")}`);
    }
    for (const id of normalizedIds) {
      const preset = findBookStylePresetById(id);
      if (preset?.prompt) {
        sections.push(String(preset.prompt).trim());
      }
    }
  }

  const normalizedCustomText = String(customText || "").trim();
  if (normalizedCustomText) {
    sections.push(`[自定义写作要求]\n${normalizedCustomText}`);
  }

  return sections.join("\n\n").trim();
}
