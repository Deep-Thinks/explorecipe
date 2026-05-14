# ExploreCipe Phase 2 设计文档

> **核心命题**：让用户上传一张食物照片，能用红框无限钻入这张食物的 recipe（"它是怎么做出来的"）。每次钻入产出一张新爆炸图，移动端用面包屑栈导航，桌面端用思维导图展开。
>
> **本次重构的关键转向**：把"创作配方"的决策权从 Gemini 3 Flash 转移到 gpt-image-2。Flash 退化为纯 OCR / 提取器，只读不写。

---

## 1 · 重构动因

Phase 1 实测显示：让 Gemini Flash 列 ingredients + tools 配方，结果质量参差不齐——
- Lv5 偶尔列出"美拉德反应""脂肪""蛋白质"这类**抽象材料**（需要堆 prompt 兜底）
- 描述文字平淡（"是的灵魂的厚重基石"这类套话频繁出现）
- 容易循环（Lv2-5 全在"鹅肝"系列同义词里打转）

根本原因：Flash 模型的语言生成能力不足以承担"创作配方"这种需要世界知识 + 审美判断的工作。它擅长的是**轻量结构化任务**（识别、抽取、定位）。

**新分工：**

| 角色 | 现状（Phase 1）| 新方案（Phase 2）|
|---|---|---|
| Gemini 3 Flash | 列配方 + 写描述 + 定位 + 识别 | 只做 OCR / bbox 提取 / 简单识别 |
| gpt-image-2 | 按 Gemini 给的配方画图 | **自己决定**配方 + 描述 + 画图（端到端）|

Gemini 从"作者"降为"读者"，gpt-image-2 从"插画师"升为"作者 + 插画师"。

---

## 2 · 整体架构

```
┌───────────────────────────────────────────────────────────────────┐
│                          浏览器 (HTTPS)                            │
│  ┌─────────────────────┬─────────────────────────────────────┐    │
│  │  移动端（首要）       │  桌面端（>=1024px）                  │    │
│  │  面包屑栈 + 红框      │  思维导图 + 红框                     │    │
│  └─────────────────────┴─────────────────────────────────────┘    │
└──────────────────────────────┬────────────────────────────────────┘
                               │ /api/*
                               ▼
┌───────────────────────────────────────────────────────────────────┐
│                  Python 单进程 HTTP 服务（server.py）              │
│                                                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────────┐   │
│  │ /api/start  │  │ /api/drill  │  │ /api/journey/{id}, /j/   │   │
│  │ 上传 → Lv1  │  │ 红框 → Lv+1 │  │ 永久链接、分享卡          │   │
│  └─────────────┘  └─────────────┘  └──────────────────────────┘   │
│         │                │                                        │
│         └───────┬────────┘                                        │
└─────────────────│─────────────────────────────────────────────────┘
                  │
       ┌──────────┼─────────┐
       ▼          ▼         ▼
┌────────────┐ ┌────────┐ ┌───────────────────────┐
│ MiniCPM-V  │ │ Gemini │ │ gpt-image-2           │
│ 内容审核    │ │ 3 Flash│ │ (token-recyclebin)    │
│ is_food    │ │ OCR    │ │ 创作 + 渲染            │
└────────────┘ └────────┘ └───────────────────────┘
```

**调用频次预估（一次完整 5 层探索）：**

| 上游 | 调用次数 | 单次耗时 | 累计 |
|---|---|---|---|
| MiniCPM-V 审核 | 1 | 1-3s | ~2s |
| gpt-image-2 | 5 (每层一次) | 45-100s | ~5 min |
| Gemini Flash 提取 | 6 (每层 1 次：读图 OCR；初始 1 次：识别食物名) | 3-5s | ~25s |
| **合计** | | | **~5-6 min** |

Gemini Flash 用量下降：从 Phase 1 的 ~2-3 次 / 层 → 1 次 / 层。

---

## 3 · 模型分工细节

### 3.1 gpt-image-2 · 端到端"配方爆炸图"

**输入**：
- 原图（Lv1）或上一层带红框的图（Lv2+）
- 上下文文本：菜名（仅 Lv1）或被框物体名（Lv2+，由 Gemini OCR 提供）
- 模板 prompt

**输出**：一张 1024×1536 的爆炸图，内容包括：
- 4-6 个 **ingredients**（食材层）：物体 + 中文名 + ~30 字描述
- 2-3 个 **tools**（工具层）：物体本身，**不带任何中文标签**
- 木桌背景延续

**Prompt 模板（Lv2+ 示例，开放式让模型自己想配方）**：

```
The INPUT image has a RED rectangle highlighting "{target_zh}" (also visible inside the box).
Ignore everything outside the red box.

Create a hyper-realistic VERTICAL exploded view that shows the RECIPE of "{target_zh}" —
i.e., what you need to make / produce / manufacture it.

YOU decide the recipe. List the most accurate / interesting items:
  • 4-6 INGREDIENTS — discrete physical objects that go INTO the product
    (e.g., flour, water, a piece of beef, iron ore, a glass pellet)
  • 2-3 TOOLS — discrete physical objects used during production but not consumed
    (e.g., frying pan, oven, hydraulic press, electric arc welder)

For each INGREDIENT layer (HORIZONTAL layout per item — important):
  - Render the object photo-realistically on ONE side (left or right) of the row
  - On the OPPOSITE side of the same row, render the Chinese label:
      Line 1: the Chinese name (bold, ~40pt)
      Line 2-3: an interesting ~30 Chinese character description
        (origin / fun fact / role — not generic praise)
  - Alternate sides between rows for visual rhythm:
      row 1: image LEFT, label RIGHT
      row 2: image RIGHT, label LEFT
      row 3: image LEFT, label RIGHT
      ...
  - DO NOT stack the label below the image; the row uses HORIZONTAL space, not vertical
  - Image side and label side together fill one row (~280-360px tall per row)

For each TOOL layer:
  - Center the object photo horizontally (no label to compete for space)
  - DO NOT render any text label or Chinese character

Strict rules (must follow):
  - All sub-items MUST be DISCRETE PHYSICAL OBJECTS, never abstract materials
    (forbidden: 脂肪 / 蛋白质 / 糖类 / 淀粉 / 油脂 / 美拉德反应 / 发酵 / 风味)
  - All sub-items MUST be more atomic / upstream than "{target_zh}"
  - Do NOT include "{target_zh}" itself as one of the sub-items

Aspect ratio: output image is 9:16 (e.g., 1024×1792). Rows stack vertically.
Layout order: ingredients on top, tools at the bottom. 60-100px gap between rows.
Background: preserve a soft ambient setting (wooden table / muted neutral), not solid studio.
```

关键点：**gpt-image-2 自己决定**列哪些 ingredient / tool、起什么中文名、写什么 30 字描述。它把"做配方"这事一并做了。**标签从"堆下方"改为"放左右"，让每行的视觉重心是物体本身而不是文字块**。

### 3.2 Gemini 3 Flash · 仅做三件轻量事

**(A) 启动时识别食物名（Lv1 唯一用途）**
```
Input: 原图
Output JSON: {"dish_name_zh": "...", "dish_name_en": "..."}
```
1 次调用，~3s，单一目的：知道这道菜叫什么，传给 gpt-image-2 做上下文。

**(B) 每层生成后，从图中提取 layers 元数据**
```
Input: gpt-image-2 刚生成的 Lv N 图
Output JSON: {
  "layers": [
    {
      "kind": "ingredient" | "tool",
      "name_zh": "面饼",      // OCR 读图里写的字
      "name_en": "Dough",     // 如果图里有英文则提取
      "desc_30": "...",       // OCR 读 30 字描述（ingredient 才有）
      "bbox": [x0, y0, x1, y1]  // 0-1 ratio，包裹物体本身
    },
    ...
  ]
}
```
1 次调用，~5s。**Gemini 只在"读"图里 gpt-image-2 渲染好的字**，不创作。准确率会比"自己想"高得多。

**(C) 用户 drill 时，对裁剪后的红框区域生成 ≤40 字简介**

每次 drill 都调用一次（详见 §3.3）。Flash 看 crop（不是整图），输出一句话告诉 gpt-image-2："你即将分解的这块物体是什么、有什么显著特征"，作为 gpt-image-2 创作 Lv N+1 的上下文 seed。

**(D) 备用：红框 OCR 兜底**

(B) 已把所有 bbox 都返回了，前端能直接判定红框命中哪个 layer，不需要再调一次 Gemini。仅当命中度极低（IoU < 0.2 且 (C) 也读不出）才走这条兜底。**默认不调。**

### 3.3 关键修订 · Drill 工作流（Lv 2+）

> ⚠️ Phase 1 实测的苦涩经验：把"整张 Lv N 图 + 红框"喂给 gpt-image-2 会触发它的 **image-edit 模式** —— 它会试图"在原图基础上修改"，导致上一层渲染的中文字段被当成背景元素一起卷入下一层的生成，**Lv5 字形累积漂移就是这么来的**。
>
> 修法：drill 时不再传整图。传**裁剪后的红框区**（slightly expanded）+ Flash 写的简介。gpt-image-2 进入**新生成模式**，无视觉污染。

**新 drill 流程**：

```
Lv N 图（1024×1792 PNG，含中文标签）
   │
   │ 用户在前端拉红框 → 得到 bbox (x0,y0,x1,y1)（0-1 比例）
   ▼
[Server] 裁剪 bbox 区域，外扩 padding=0.04（约 4% 安全边距）
   │ → crop.png（保留物体本体，但可能裁掉部分标签 → 反而是好事）
   │
   ├──→ [Gemini Flash · call C] 看 crop.png
   │      Prompt: "用 1 句话（≤40 字中文）描述这个物体，必须包含它的中文名。"
   │      Output: "一只白色羽毛、橘色喙的活鹅，常见家禽，体型较大。"
   │
   ▼
[Server] 组装新 prompt：
   • image input: crop.png  ← gpt-image-2 看到的"输入"是干净的物体局部图
   • text prompt: §3.1 同款 recipe prompt
                  + "This is: {Flash brief}"   ← 给模型一个名字 handle
                  + "Generate at 9:16 aspect ratio (1024×1792)"
                  + "Compose this as a FRESH image, not an edit of the input."
   ▼
[gpt-image-2] 生成 Lv N+1（1024×1792）  ← 干净中文，无历史污染
   │
   ▼
[Gemini Flash · call B] 提取 layers 元数据 → layer_(N+1).json
```

**关键设计原则**（来自苦涩经验）：

1. **不要把 Lv N 整图带入 Lv N+1 生成**。只带 crop。
2. **Flash 简介只是"取个名"**，不是 recipe 作者。它告诉 gpt-image-2 "你看到的这块叫什么"，避免后者瞎猜。
3. **gpt-image-2 进的是"生成新图"模式**，不是"编辑旧图"模式。这两种模式对 token-recyclebin 这类反代来说调用方式相同，但 prompt 引导可以选择哪种行为占主导：现在的引导是 "Compose this as a FRESH image, not an edit."
4. **9:16（1024×1792）取代 2:3（1024×1536）**：移动端竖屏天然 9:16，整图更"沉浸"；Lv1 也同步使用 9:16。

### 3.4 内容审核 · 不动

MiniCPM-V 1.3B `is_food` 二分类不变，沿用 `server.py::call_minicpm_food_check`，逻辑保持。

### 3.5 苦涩经验清单 · Design Principles (Bitter Lessons)

充分相信 gpt-image-2 的能力，但务必注意"怎么喂它"。下面是 Phase 1 实测换来的硬约束：

| # | 现象 | 根因 | 设计对策 |
|---|---|---|---|
| L1 | Lv5 中文字符变形（"鹅肝脏肝脏"） | 整图+红框 → image-edit 模式 → 上一层文字污染 | drill 改为 crop+简介+新生成（§3.3）|
| L2 | 标签堆下方占 50% 视觉空间，物体反而像配角 | 默认 prompt 让 label 在 image 下方 | 标签改为左右排，与物体同一行（§3.1）|
| L3 | Gemini Flash 自创 recipe 容易抽象 / 重复 / 套话 | 任务超出 Flash 能力上限 | 把 "decide recipe" 移到 gpt-image-2 prompt，Flash 只 OCR / 简介 |
| L4 | Tools 偶尔被画上小标签 | prompt 不够强硬 | 用 "ABSOLUTELY NO Chinese characters anywhere on or near this tool" |
| L5 | "美拉德反应""脂肪"等抽象项混进来 | 子配方 prompt 没拦死 | 强制 "DISCRETE PHYSICAL OBJECTS"，列黑名单 |
| L6 | gpt-image-2 偶发 "无 data" 间歇失败 | token-recyclebin 上游不稳 | 3 次指数退避重试（沿用现 `call_dmfox_explosion`）|

**核心哲学**：信任 gpt-image-2 ≠ 撒手不管。约束写在 prompt 里、写在数据流里；不是把它的创作权剥离给文本模型。Phase 1 已经证明给它合适的 prompt + 合适的 input，它能稳定渲染含中文 30 字描述的爆炸图。

---

## 4 · 数据模型

### 4.1 服务端持久化（每个 journey 一个目录）

```
logs/journeys/
  {journey_id}/                        # 例如 7Q4HK3M
    meta.json                          # 总览
    layer_1.png                        # gpt-image-2 输出
    layer_1.json                       # Gemini 提取的 layers 元数据
    layer_1_boxed.png                  # （drill 时存档的红框版，可选）
    layer_2.png
    layer_2.json
    ...
```

### 4.2 `meta.json` 结构

```json
{
  "journey_id": "7Q4HK3M",
  "created_at": "2026-05-15T01:23:45",
  "dish_name_zh": "鹅肝牛肉汉堡",
  "dish_name_en": "Foie Gras Beef Burger",

  "current_level": 5,

  "path": [
    {
      "level": 1,
      "title_zh": "鹅肝牛肉汉堡",
      "title_en": "Foie Gras Beef Burger",
      "parent_level": null,
      "parent_picked": null,
      "image": "layer_1.png",
      "image_url": "/journey/7Q4HK3M/layer/1",
      "layers_meta_url": "/api/journey/7Q4HK3M/layer/1/meta",
      "generated_at": "2026-05-15T01:23:45",
      "gen_elapsed_sec": 53.4
    },
    {
      "level": 2,
      "title_zh": "鹅肝",
      "title_en": "Foie Gras",
      "parent_level": 1,
      "parent_picked": {                // 用户在 Lv1 画了什么红框
        "bbox": [0.29, 0.01, 0.68, 0.11],
        "name_zh": "鹅肝",
        "kind": "ingredient"
      },
      "image": "layer_2.png",
      "image_url": "/journey/7Q4HK3M/layer/2",
      ...
    }
  ]
}
```

### 4.3 单层 `layer_N.json`（Gemini 提取后落盘）

```json
{
  "level": 2,
  "layers": [
    {
      "kind": "ingredient",
      "name_zh": "整块鹅肝",
      "name_en": "Whole Foie Gras",
      "desc_30": "选取肥美丰腴的完整鹅肝，是整道菜品的核心主体。",
      "bbox": [0.27, 0.04, 0.73, 0.18]
    },
    {
      "kind": "ingredient",
      "name_zh": "海盐",
      "name_en": "Sea Salt",
      "desc_30": "细颗粒海盐，用于提升鹅肝的鲜甜度并丰富口味层次。",
      "bbox": [0.30, 0.25, 0.70, 0.36]
    },
    {
      "kind": "tool",
      "name_zh": "平底不粘锅",
      "name_en": "Non-stick Pan",
      "desc_30": "",
      "bbox": [0.20, 0.62, 0.80, 0.76]
    },
    ...
  ]
}
```

---

## 5 · 后端 API

### 5.1 `POST /api/start`

**触发**：用户在前端上传食物照片，第一次进入。

**Request**：`Content-Type: image/jpeg` 或 `image/png`，body 是原图字节。

**Server 处理**（同步，前端 loading）：
1. MiniCPM-V `is_food` 审核（fail-closed）
2. Gemini 识别 `dish_name_zh` / `dish_name_en`
3. 生成 `journey_id`（7 位 base32，沿用现 `_generate_share_id`）
4. 落盘 `logs/journeys/{id}/`
5. 用 prompt（带 `dish_name_zh`）调 gpt-image-2 → `layer_1.png`
6. 调 Gemini 提取 `layer_1.json`
7. 落盘 `meta.json` `current_level=1`

**Response（同步返回）**：
```json
{
  "ok": true,
  "journey_id": "7Q4HK3M",
  "share_url": "https://explorecipe.xmu-cuisine.club/j/7Q4HK3M",
  "current_level": 1,
  "path": [...],            // 同 meta.path 结构
  "layers_meta": {...}      // Lv1 的 layers JSON
}
```

**注意**：因为 gpt-image-2 单层耗时 45-100s，前端必须做好 loading UX。可以考虑 SSE / 长连接推进度，但 MVP 阶段同步 + 客户端 60s+ 等待提示就够。

### 5.2 `POST /api/drill`

**触发**：用户在 Lv N 图上画红框，确认"分解这一块"。

**Request**：
```json
{
  "journey_id": "7Q4HK3M",
  "from_level": 2,
  "bbox": [0.28, 0.04, 0.72, 0.18]   // 0-1 比例
}
```

**Server 处理（按 §3.3 新工作流）**：
1. 加载 `meta.json` 校验 `from_level == current_level`（防并发乱序）
2. 读 `layer_{from_level}.json`，找到与 `bbox` 重叠最大的 layer → 得到 `target_name_zh` 和 `kind`（仅用于落档案 + 前端面包屑标签，不直接传给 gpt-image-2）
3. **从 `layer_{from_level}.png` 裁剪 bbox（外扩 padding=0.04）→ `crop.png`**
   - 落盘存档为 `layer_{from_level}_crop.png`
4. **Gemini Flash 看 crop**（§3.2 call C）：返回 ≤40 字简介 `brief`
   - 同时存为 `layer_{from_level}_crop_brief.txt`
5. **gpt-image-2 新生成**（§3.3）：
   - input image: `crop.png`（不是整图、不带红框）
   - prompt: §3.1 recipe prompt + `"This object: {brief}"` + `"FRESH composition, not an edit"`
   - size: 1024×1792 (9:16)
   - 输出 → `layer_{from_level+1}.png`
6. Gemini 提取（§3.2 call B）→ `layer_{from_level+1}.json`
7. 更新 `meta.json`：append path、`current_level++`，记录这次 drill 的 crop + brief 引用

**为什么不传整图 + 红框？** 见 §3.3 / §3.5：会触发 image-edit 模式，Lv5 字形漂移就是这么来的。新流程把"上一层视觉"完全切断，只通过 crop 的物体本体 + Flash 一句话 brief 把语义带过去。

**Response**：同 `/api/start`（统一格式，前端只需一种解析逻辑）。

**深度软限制**：`current_level >= 8` 时返回 409 + 提示文案"已是最深探索层，继续即兴探索请新开一次"。硬上限避免恶意打满。

### 5.3 `GET /api/journey/{id}`

读出整个 journey 用于回放、桌面端思维导图全景渲染。

**Response**：
```json
{
  "ok": true,
  "journey_id": "7Q4HK3M",
  "meta": {...},          // meta.json
  "layers": {             // 所有层的 layers 元数据
    "1": {...},
    "2": {...},
    ...
  }
}
```

### 5.4 `GET /journey/{id}/layer/{n}` （图片）

返回 `layer_N.png` 字节流。带 1 天缓存头。

### 5.5 `GET /j/{id}` （SPA 入口 + OG meta）

注入 `window.__JOURNEY_ID__` 让前端直接载入对应 journey。

### 5.6 `POST /api/journey/{id}/share-card`

**触发**：用户点"生成分享卡"。

**Server**：Canvas / Pillow 把整条 path 的 5 张缩略图合成一张 9:16 竖版分享卡，加 logo + path 文字 + QR 码。沿用 Phase 1 `/g/{id}` 卡片合成思路扩展。

**Response**：分享卡 PNG bytes。

---

## 6 · 前端

### 6.1 路由

```
/                 — 上传页（拍照 / 选图）
/loading          — 全屏 loading（生成 Lv1 / drill 中）
/j/{id}           — 探索视图（响应式：mobile breadcrumb / desktop mindmap）
/j/{id}/share     — 静态分享卡查看页（OG meta 注入）
```

### 6.2 移动端 · 面包屑栈

```
┌──────────────────────────────────────────┐
│ ← ExploreCipe                  [⋯ 分享]  │
├──────────────────────────────────────────┤
│ 🍔─→─🦆─→─🥩─→─🦢─→─🥚 (current)        │ ← 横向滚动 breadcrumb
│ Lv1  Lv2  Lv3  Lv4   Lv5                │   每个缩略图 + 名字
├──────────────────────────────────────────┤
│                                          │
│                                          │
│       [当前层级大爆炸图]                  │
│       1024×1536，scaled to fit           │
│                                          │
│                                          │
│                                          │
├──────────────────────────────────────────┤
│  ┌──────────────────────────────────┐    │
│  │  ✏️ 框选 → 继续分解              │    │ ← floating bottom CTA
│  └──────────────────────────────────┘    │
└──────────────────────────────────────────┘
```

**面包屑交互**：
- 当前层有高亮边框
- 点其他缩略图 → 跳到那一层（layers 数据本地有，不需后端往返）
- 横向滚动支持多层（理论上无限，硬上限 8）

**框选模式**：
- 点 "✏️ 框选" → 整图蒙半透明黑遮罩
- 用户用单指拖出红框（PointerEvent，支持 touch + mouse）
- 实时回显 bbox 在屏幕坐标 + 比例坐标
- 提示文案："一次只框一样东西效果最好"
- 底部出现 "✓ 分解这一块" + "✕ 取消"
- 点确认 → 屏幕进入 loading → POST `/api/drill` → 收到响应后 push 新层到 breadcrumb → 切换到新层

**框选辅助**（基于 `layers_meta.bbox`）：
- 用户开始拖时，如果起点落在某个 layer 的 bbox 内，**自动吸附**到这个 layer 的 bbox（即"点击即选中整个 ingredient"）
- 双击物体 = 一键选中 = 不用手动拖框（最常用路径）
- 拖动 = 高阶模式，允许跨多个对象框选

### 6.3 桌面端 · 思维导图

```
┌──────────────────────────────────────────────────────────────┐
│ ExploreCipe                                       [⋯ 分享]   │
├──────────────────────────┬───────────────────────────────────┤
│  探索地图                 │  当前查看：Lv3 整块鹅肝          │
│                          │  ↑ 从 Lv2 鹅肝钻入                │
│  🍔                       │                                   │
│   ├─ 🦆 鹅肝              │   ┌─────────────────────────┐    │
│   │   ├─ 🥩 整块鹅肝 ●    │   │                         │    │
│   │   │   ├─ 🦢 活鹅      │   │  [当前层大爆炸图]        │    │
│   │   │   ├─ 盐           │   │                         │    │
│   │   │   └─ 黑胡椒        │   │  鼠标拖动 = 红框        │    │
│   │   └─ 平底锅           │   │  双击物体 = 一键选中     │    │
│   ├─ 牛肉饼               │   │                         │    │
│   └─ 烤箱                 │   └─────────────────────────┘    │
│                          │                                   │
│  [+ 框选 → 继续分解]      │   配方清单（layers meta）         │
│                          │   • 整块鹅肝                       │
│                          │   • 海盐                           │
│                          │   • 黑胡椒粉                       │
│                          │   🔧 平底不粘锅                    │
│                          │   🔧 不锈钢煎铲                    │
│                          │   🔧 厨房纸巾                      │
└──────────────────────────┴───────────────────────────────────┘
```

**思维导图特性**：
- 树形布局：每个用户访问过的层都是一个节点
- 节点显示 emoji + 名字（缩略图悬浮显示）
- 当前节点 `●` 高亮
- 点击节点 → 右侧切换到那一层
- 未访问的分支（用户没钻过去的 ingredient/tool）以**虚线节点**显示，标记 "可探索"
- 点击虚线节点 → 等于在父层框选这个 item → 触发 drill

**响应式断点**：
- `< 1024px` → 移动端面包屑
- `>= 1024px` → 桌面端思维导图

### 6.4 加载状态（移动端）

gpt-image-2 单层 45-100s。这段时间必须有事干，否则用户跑路。

```
┌──────────────────────────────────────────┐
│ 🍔─→─🦆─→─🥩─→─🦢─→─⏳                   │ ← 新层占位
├──────────────────────────────────────────┤
│                                          │
│   ┌─────────────────────┐                │
│   │  [crop 缩略图]      │ ← 用户框选的物体，已裁剪
│   │      鹅肝            │ 让用户确认 AI 看的是这块
│   └─────────────────────┘                │
│                                          │
│   AI 正在拆解「鹅肝」…                    │
│   ⠋ 大约 60 秒                            │
│                                          │
│   💡 「鹅肝」是…                          │
│   一只白色羽毛、橘色喙的活鹅经过           │ ← Flash 的 brief 直接展示
│   填饲增肥后的肝脏，法国传统食材。        │   （§3.3 drill 流程的副产物，零额外成本）
│                                          │
└──────────────────────────────────────────┘
```

**Loading 页的文字内容**：直接复用 §3.3 中 Flash 已生成的 `brief` —— 这本来就是 drill 工作流必走的一步，不需要额外接口、不需要额外 Gemini 调用。Codex P2 提到 fun-fact 是 scope creep，正确 —— 我们**删掉独立 `/api/fun-fact` 接口**，等待页文字直接用 drill response 里附带的 brief 字段。

### 6.5 错误处理

| 失败场景 | 用户看到 | 服务端动作 |
|---|---|---|
| `is_food=false` | 友好提示"请上传食物图" | 不调 gpt-image-2，省钱 |
| gpt-image-2 三次重试失败 | "AI 累了，30 秒后重试" + 按钮 | 写 event log |
| Gemini 提取 layers 失败 | 仍然返回图，layers_meta 为空 | 前端进入"无吸附辅助"模式，仍可手画框 |
| `current_level >= 8` | "已是最深，开新探索吧" | 不允许继续 drill |
| 网络中断 | "网络好像断了" + 重试按钮 | 前端缓存 bbox，恢复后自动重发 |

---

## 7 · 关键技术风险与缓解

### R1 · gpt-image-2 开放式 prompt 输出质量不稳

**风险**：让 gpt-image-2 自己想配方，可能列出奇怪的项（比如 Lv5 列"二氧化碳"）。

**缓解**：
- prompt 里 hardcode 抽象禁词清单（§3.5 L5）
- prompt 里给"好例子 / 坏例子"对照
- Gemini 提取后做 post-validation：含黑名单词 → 标记 layer 为"探索受限"（不允许 drill）
- §3.3 的 crop+brief 模式提供了 Flash 一次"reality check"机会：如果 Flash 看 crop 后认为这是个无法继续分解的物体（如"一粒盐"），可以提前告诉 gpt-image-2 "this is at atomic level"，让它返回一张"已无法继续分解"的占位图

**为什么不采纳 Codex 的"文本模型出 JSON，图像模型纯渲染"建议**：

外部审阅（Codex consult）建议把 recipe 创作权从 gpt-image-2 收回，交给一个更强的文本模型，gpt-image-2 只按 JSON 渲染。我们考虑后**拒绝**这个方向，原因：

1. **视觉接地（visual grounding）能力丢失**。gpt-image-2 是多模态模型，它"看着输入图想 recipe"和"看着 JSON 描述画 recipe"是两件事。前者能感知到原图的细节（比如这块鹅肝的纹理是煎过的而不是生的，所以子部件应含"煎制后的"修饰）；后者完全靠 JSON 字符串。Phase 1 实测里 gpt-image-2 自己列的"鹅肝 / 海盐 / 黑胡椒"配方比 Flash 列出来的"美拉德反应 / 油脂"准得多 —— 因为它看着图想。
2. **跨域跳跃（食物 → 工业）的语义连续性**。从"鹅肝牛肉汉堡"到"龙门吊"，文本模型很难凭空想到"焦糖洋葱→平底锅→冲压机→铣床→龙门吊"这种 5 层串得起来的链路；gpt-image-2 配合视觉锚点能。
3. **Codex 担心的 "OCR 反推 schema 脆弱" 由 §3.3 解决**。OCR 不再是跨层传递的事实来源 —— 上一层文字不再喂给下一层 gpt-image-2，因此即使 OCR 偶尔读错某层的标签，**错误也不会传染下一层**。每一层都是独立 fresh generation。
4. **"信任 + 怎么喂"的哲学**（§3.5）。gpt-image-2 是项目能拍板的最强模型，问题从来不是它能不能，而是 prompt 和输入构造对不对。把它降级到"纯画工"等于自废武功。

回退方案：如果开放式 prompt 真的塌方，调整 prompt（加更多约束 / 例子）而不是换架构。

### R2 · Gemini OCR 中文字段不准

**风险**：gpt-image-2 渲染的中文偶尔字形变形（Phase 1 Lv5+ 频发），Gemini OCR 读错。

**缓解**：
- §3.3 crop+fresh-generate 模式从根上**切断了"上一层文字漂移到下一层"的传染链** —— 每一层都是 fresh 输出，不背前面层级的字形包袱。这是 Phase 1 Lv5 字形变形的根因修复，预期 Lv5+ 中文质量与 Lv1 同档。
- §3.1 标签从"图片下方"改成"图片左右"，每个标签的视觉权重更高，gpt-image-2 渲染时给它更多 attention 也是可能的（待 PR-0 smoke 验证）
- gpt-image-2 prompt 强化"按字符精确渲染，不要重复、不要替换"
- Gemini 提取时同时返回置信度，低置信度 layer 标记为"模糊"，前端 UI 显示问号
- 前端允许用户手动**纠正名字**（每个 layer 都有个 edit icon），纠正结果写回 `layer_N.json`

### R3 · bbox 精度（用户框选准确性）

**风险**：用户在小屏幕上拖框拖得歪，命中多个 layer。

**缓解**：
- "双击 = 一键选中" 优先体验（覆盖 90% 场景）
- 拖框时实时显示当前 bbox 命中哪些 layer 的名字，用户可视确认
- "拖到某个 layer 内 → 吸附"（IoU > 0.5 时自动吸附）
- 真不准就让 Gemini 兜底 OCR

### R4 · 单层生成 100s 用户流失

**风险**：移动端用户耐心 30s 是上限，5 层 5 分钟会大幅流失。

**缓解**：
- Lv1 必须有强引导（loading 页的趣闻 + 上传时显示"AI 正在为你画一张前所未见的爆炸图，大约 1 分钟"）
- Lv2+ 让用户感觉"我在与 AI 协作"（红框确认动作 → 用户主动）减少被动等待焦躁感
- 推送通知（如果用户授权）：长任务完成时叫醒（PWA Web Push 可做）
- 不要做"生成进度条"假动效，要做真的有信息密度的等待页

### R5 · 配额 & 成本

**风险**：免费 Gemini Flash 20 req/day。gpt-image-2 ~$0.05/次。

**缓解**：
- 升级 Gemini 到付费 tier（input $0.075/M、output $0.30/M），按当前用量月成本 < $1
- gpt-image-2 是主要成本，开启分享后单次探索成本 $0.25-0.40，可由社区资助 / 限频
- 加 IP 限流：每 IP 每天 3 次完整 5 层探索（在 `events.jsonl` 上累计）

### R6 · 移动端图片下载量

**风险**：1024×1536 PNG 单张 2MB，5 层 = 10MB。4G 网下载慢。

**缓解**：
- 服务端生成时同步出 webp 缩略图（150KB）+ 原图，前端先加载缩略图占位再换大图
- Service Worker 缓存已访问的层（用户跳回浏览历史时秒开）
- 思维导图节点用 200×300 webp，原图只在选中时加载

---

## 8 · 工程拆解（按 PR 拆分）

### PR-1 · 后端骨架
- `server.py` 删除 stepfun / explain-region / explain-all 等老路径
- 新增 `/api/start`、`/api/drill`、`/api/journey/{id}`、`/journey/{id}/layer/{n}`
- 新建 `gemini.py` 封装 Flash 调用（extract / locate-fallback / fun-fact）
- 新建 `imagegen.py` 封装 gpt-image-2 调用 + 重试
- 新建 `journey.py` 封装 meta.json 读写、journey_id 生成、目录布局
- 复用：MiniCPM-V `is_food`、share_id 字符集
- 写一个端到端 pytest 用 fixture 图片跑通 Lv1+Lv2

### PR-2 · 前端基础（移动端先）
- 拆掉旧 index.html 所有 stepfun 抽屉相关代码
- 新建一个 SPA 框架（保持单文件 vanilla JS 风格，与现有体例一致）
- 路由：`/` 上传、`/loading`、`/j/{id}`
- 上传 → POST `/api/start` → 跳 loading → 跳 `/j/{id}`
- `/j/{id}` 移动端面包屑视图 + 大图 + "✏️ 框选" CTA
- 框选 PointerEvent + 半透明遮罩 + 红框
- 双击 = 一键选中
- POST `/api/drill` → loading → 切到新层

### PR-3 · 加载页 & 趣闻
- `/api/fun-fact?name=X` 后端实现
- 前端 loading 页显示 boxed 缩略图 + 趣闻

### PR-4 · 桌面端思维导图
- 媒体查询 `>= 1024px` 切换 view
- 思维导图组件（简单 SVG 树形布局，每节点 emoji + 名字）
- 点击节点跳层
- 虚线 "未探索" 节点

### PR-5 · 分享卡 & SEO
- `/api/journey/{id}/share-card` Pillow 合成
- `/j/{id}` SSR 注入 OG meta
- 测试微信分享面板预览

### PR-6 · 部署 & 限频
- 部署 deploy/setup-remote.sh 更新（新文件清单）
- IP 限频中间件
- 监控：events.jsonl 增加每层 elapsed / 用户 path / 失败次数

---

## 9 · 验收标准

- [ ] 上传一张披萨原图 → Lv1 自动生成，含中文标签 + 30 字描述 + 工具区无标签
- [ ] 移动端单指拖红框、双击一键选中、面包屑滚动跳转，都流畅
- [ ] 桌面端思维导图能展示 5 层探索全景，未探索节点是虚线，点击虚线节点等同于 drill
- [ ] /j/{id} 链接复制给别人能完整复现你的探索路径
- [ ] 微信内打开 /j/{id}，分享卡预览正确显示 path 缩略图 + 菜名
- [ ] Lv5 不会出现"美拉德反应"这类抽象 sub-component
- [ ] 单层 P95 < 100s，端到端 Lv1 < 80s（含审核）
- [ ] 整站 Lighthouse mobile 性能 > 80（不算 gpt-image-2 等待）
- [ ] gpt-image-2 / Gemini 任一上游挂掉，错误页有重试按钮不会白屏

---

## 10 · 不在本期范围内

- 用户账号 / 登录 / 收藏夹
- 同一 journey 多设备同步
- 跨 journey 全局思维导图（只在单 journey 内可视化）
- 视频生成 / 语音解说
- 多语言（仅中文）
- 评论 / 点赞 / 排行榜
- 反向搜索（"我看过哪些菜含烤箱"）

这些是 Phase 3 候选。

---

## 11 · 与 Phase 1 实测的对照

Phase 1 5 层 stress 测试（http://43.160.251.210:20027/explorecipe-phase1/）已证明：
- ✅ Recipe paradigm（ingredients + tools 混排，tools 无标签）gpt-image-2 严格遵守
- ✅ Gemini Flash OCR/locate bbox 100% 命中（验证了 Flash 做"读"的工作可靠）
- ✅ 单层 P50 ~60s 可接受
- ⚠️ Lv5 中文字偶尔变形 —— **Phase 2 §3.3 crop+brief+fresh-generate 工作流是这个 bug 的根因修复**
- ⚠️ 标签堆下方占用大量纵向空间 —— **Phase 2 §3.1 左右排版修复**

Phase 2 还没在 Phase 1 验证过的关键假设：

| 假设 | 风险 | 验证方法（PR-0 smoke test）|
|---|---|---|
| gpt-image-2 自己想 recipe 质量优于 Flash 列大纲 | recipe 抽象 / 跑题 / 倒退 | 同一菜原图，两种 prompt 模式各跑 3 次，对比抽象项率、视觉一致性、用户主观偏好 |
| crop+brief 切断字形漂移 | Lv5 是否仍变形？crop 边界处理是否会丢失字符？ | 跑完整 5 层，统计每层 OCR 置信度与字符正确率 |
| 标签左右排版 gpt-image-2 能否稳定遵守 | 模型可能仍习惯把标签放下方 | 5 张菜各跑 1 次，看默认行为 vs prompt 强制后的 compliance |
| 9:16 (1024×1792) 是否被 token-recyclebin 支持 | 上游可能拒绝非标准尺寸 | 一次最小调用确认 |

**PR-0 必须在写 PR-1 后端骨架前完成**。任何上面假设崩塌 → 回到这份文档调整 §3，而不是带着错误假设继续写后端。

### 关于 Codex 审阅（2026-05-15）

Codex 提了 15 条 finding。我们的处置：

| 类别 | 项 | 处置 |
|---|---|---|
| **采纳** | P1 同步 45-100s API → 改 async job 模型 | 待 PR-1 改写 §5 API 时落实 |
| **采纳** | P1 并发控制（atomic write + 幂等键）| 待 PR-1 |
| **采纳** | P1 安全章节缺失 | 待补 §7.7（上传校验 / ID 枚举 / prompt injection）|
| **采纳** | P2 线性 path → 节点/边模型（思维导图需要）| 待 PR-1 重写 §4 |
| **采纳** | P2 移除 `/api/fun-fact` scope creep | 已落实于 §6.4（复用 §3.3 的 brief）|
| **采纳** | P2 验收标准量化（IoU/字段准确率/抽象率）| 待 PR-0 smoke test 时定阈值 |
| **采纳** | P2 PR 拆分太大 → 加 PR-0 | 已在 §8 加 PR-0 |
| **采纳** | P3 tools 保留 canonical name 服务端 | 已落实于 §3.1 + §4.3 |
| **采纳** | P3 存储 retention 策略 | 待补 §7.8 |
| **拒绝** | **P1 模型角色拆分（"图像模型纯渲染" 反对意见）** | 见 §7 R1 详细论证：保留 gpt-image-2 作为作者 + 渲染器。Codex 没考虑视觉接地的语义优势，也没考虑 §3.3 crop 模式已经从根上解决了它担心的 OCR 反推脆弱性 |
| **拒绝** | P1 Phase 1 Gemini OCR 反推数据模型脆弱 | §3.3 已解决：OCR 不再跨层传递，每层独立 |
| **拒绝** | P2 桌面端思维导图推到 Phase 2.5 | 保留，但承认是 stretch goal，必要时降级到桌面同布局放大版 |

---

*文档结束*
