# ExploreCipe · LLM Prompt 与调用链路总览

> 本文盘点 `server_v2.py` 里所有上游 LLM 调用，覆盖 4 个模型、4 段 prompt、2 条业务链路。
> 出处文件：`server_v2.py`（v2 后端，端口 18083）。
> 阅读顺序建议：先看 §1 总览表 → §2 调用链路图 → §3 prompt 全文。

---

## 1. 上游模型总览

| 角色 | 模型 | base_url | 调用函数 | env key |
|---|---|---|---|---|
| 内容审核 | `MINICPM_23u6wt`（MiniCPM-V，多模态） | `MINICPM_BASE_URL`（任意 OpenAI 兼容反代） | `call_minicpm_food_check` | `MINICPM_API_KEY` |
| 文本理解 | `gemini-3-flash-preview` | google-genai SDK | `call_gemini_identify_dish` / `call_gemini_extract_layers` / `call_gemini_brief` | `GEMINI_API_KEY` |
| 图像生成 | `gpt-image-2`（image-edit 接口） | `IMAGE_UPSTREAM_URL`（任意 OpenAI 兼容反代） | `call_image_gen` | `IMAGE_API_KEY` |

> 注：所有 Gemini 调用都关闭了 thinking（`thinking_budget=0`），求快不求深。
> 注：gpt-image-2 走 image-edit 接口，但 Lv N+1 的 prompt 里强制声明「FRESH composition, NOT an edit of the input」——是 §3.5 苦涩经验之一。

---

## 2. 调用链路

### 2.1 链路 A · `POST /api/start`（用户上传食物图，生成 Lv1）

```
用户上传 image_bytes
   │
   ├─[1] MiniCPM 审核   call_minicpm_food_check
   │      ↓ JSON {is_food, category, has_person, confidence, reason}
   │      ↓ _decide_is_food() 联合判定（修 is_food 字段不稳）
   │      └ 不是食物 & 高置信 → 拒绝（REJECT_NOT_FOOD）
   │
   ├─[2] Gemini 识菜名  call_gemini_identify_dish
   │      ↓ JSON {dish_name_zh, dish_name_en}
   │
   ├─[3] gpt-image-2 出 Lv1 爆炸图   call_image_gen
   │      ref_image = 用户原图
   │      prompt    = LV1_PROMPT_TEMPLATE.format(dish_name=...)
   │      ↓ layer_1.png
   │
   └─[4] Gemini 提取 layers   call_gemini_extract_layers
          ↓ JSON {layers: [{kind, name_zh, name_en, desc_30, bbox}, ...]}
          ↓ 落盘 layer_1.json，前端渲染热区
```

总耗时：约 60–90s（瓶颈是 [3] gpt-image-2）。

### 2.2 链路 B · `POST /api/drill`（用户点击爆炸图里某个物体，钻入下一层）

```
入参：{journey_id, from_level, bbox}
   │
   ├─[1] 本地 Pillow 裁剪      crop_image_bbox（无 LLM）
   │      ↓ crop_bytes（保留 4% padding）
   │
   ├─[2] Gemini 写一句简介      call_gemini_brief
   │      ↓ "≤40 字中文" 一句话，包含物体中文名
   │      （兜底：失败则用父层 layer.name_zh）
   │
   ├─[3] gpt-image-2 fresh 出 Lv N+1   call_image_gen
   │      ref_image = crop_bytes（不传整图+红框，避免字形漂移）
   │      prompt    = LV_NEXT_PROMPT_TEMPLATE.format(brief=...)
   │      ↓ layer_{N+1}.png
   │
   └─[4] Gemini 提取 layers   call_gemini_extract_layers（与链路 A 相同）
          ↓ 新一层热区数据
```

> 关键设计（§3.5 苦涩经验）：drill **只传 crop+brief，不传整图+红框**。
> 早期版本传「原图 + 红框圈出位置 + edit 指令」，gpt-image-2 会进入 image-edit 模式、把上一层的中文标签「漂移残留」到新图里。

---

## 3. Prompt 全文

### 3.1 MiniCPM 审核 · system prompt

> 出处：`server_v2.py::MODERATION_SYSTEM_PROMPT`（约 line 502）
> user 消息固定为「判断这张图。」+ 图片 base64

```text
你是图片内容分类器。判断输入图片：
1. is_food：图片主体是否为可食用的食物 / 菜品 / 饮品 / 食材
2. category：food / person / document / scene / animal / object / other
3. has_person：是否有人物面部
4. confidence：0-1 浮点

严格 JSON 输出：
{"is_food": true/false, "category": "...", "has_person": true/false, "confidence": 0.95, "reason": "≤30字中文"}
```

**后处理（`_decide_is_food`）**：MiniCPM 经常 `category="food"、reason="这是汉堡"` 同时 `is_food=false`，所以决策顺序是：
1. `is_food=true` → 通过
2. `category` 命中食物词（food/dish/drink/...） → 通过
3. `reason` 命中食物词（食物/菜/饭/汉堡/...） → 通过
4. 否则拒，但若 `confidence < MODERATION_FAIL_OPEN_BELOW`（默认 0.7）→ fail-open 放行

---

### 3.2 Gemini 识菜名

> 出处：`server_v2.py::IDENTIFY_DISH_PROMPT`（约 line 433）
> 入参：用户原始上传图

```text
识别这张食物照片，给出菜名（如果是非中国菜也用中文写）。

严格 JSON 输出，不要额外文字：
{
  "dish_name_zh": "...",
  "dish_name_en": "..."
}
```

---

### 3.3 Gemini 提取 layers（爆炸图 → 热区元数据）

> 出处：`server_v2.py::EXTRACT_LAYERS_PROMPT`（约 line 443）
> 入参：上一步 gpt-image-2 生成的爆炸图 PNG
> 链路 A 和链路 B 都会调用这一段

```text
这是一张爆炸分解图。它由多个垂直堆叠的"行"组成。
请提取每一行的元数据，按从上到下顺序输出。

每一行包含一个**物体**（左或右）+ 可选的**中文标签**（另一侧或在物体旁），或者只有一个**工具物体**（无标签）。

判断"行的类型"：
- 有中文标签 → kind="ingredient"（食材）
- 只有物体、无中文字符 → kind="tool"（工具）

对每一行输出：
  - kind: "ingredient" | "tool"
  - name_zh: 中文名（OCR 读图里写的字；tool 行没标签时你猜测一个常见名）
  - name_en: 英文名（OCR 读；没读到则空字符串）
  - desc_30: 30 字描述（OCR 读图里写的描述；tool 行留空）
  - bbox: [x0, y0, x1, y1]，0.0-1.0 比例，**紧紧包裹"物体本身"**（不要把中文标签框进去）

严格 JSON 输出：
{
  "layers": [
    {"kind": "ingredient", "name_zh": "...", "name_en": "...", "desc_30": "...", "bbox": [0.0, 0.0, 0.0, 0.0]},
    ...
  ]
}

要求：
- bbox 必须是 **0.0 到 1.0 之间的小数比例**（不是像素）
- 中文 OCR 如果读不清，name_zh 给 "?"（一个问号），desc_30 留空
- layers 按图中从上到下顺序排列
- 行数限制：通常 5-9 行
```

**后处理**：
- 兜底归一化：若 bbox 任一值 > 1.5，按 1024×1792 反推像素 → 比例
- 抽象禁词检测：`name_zh` 命中 `脂肪/蛋白质/反应/工艺...` → 标记 `abstract=True`，前端显示「探索受限」

---

### 3.4 Gemini brief（drill 时给 crop 写一句话）

> 出处：`server_v2.py::_brief_prompt`（约 line 475）
> 入参：从上一层裁出来的物体 crop

```text
用一句话（≤40 字中文）描述这张图里的主要物体。包含它的中文名。
示例：'一只白色羽毛、橘色喙的活鹅，常见家禽。'
只输出这一句话本身，不要 JSON、不要前后缀。
```

> 这一段输出会喂给下一步 gpt-image-2 的 `LV_NEXT_PROMPT_TEMPLATE.{brief}`。
> 失败兜底：用父层 layer 的 `name_zh`。

---

### 3.5 gpt-image-2 · Lv1 prompt（链路 A）

> 出处：`server_v2.py::LV1_PROMPT_TEMPLATE`（约 line 361）
> 占位符：`{dish_name}` ← 来自 §3.2 的 `dish_name_zh`
> 参考图：用户上传的原图

```text
Create a hyper-realistic VERTICAL exploded-view of "{dish_name}" shown in the input image.

The exploded view must reveal the RECIPE of this food — what ingredients go INTO it and what TOOLS are used to make it.

YOU decide the recipe. List the most accurate / interesting items:
  - 4 to 6 INGREDIENTS: discrete physical objects that go into the final dish (e.g., a slab of beef, an egg, a tomato, a piece of cheese)
  - 2 to 3 TOOLS: discrete physical objects used during cooking but not consumed (e.g., a frying pan, an oven, a chef's knife)

For each INGREDIENT row (HORIZONTAL layout per row — critical):
  - Render the object photo-realistically on ONE side (left or right) of the row
  - On the OPPOSITE side of the SAME row, render a Chinese label:
      Line 1: the Chinese name in bold (around 42pt)
      Line 2-3: an interesting ~30-character Chinese description (origin / fun fact / role — never generic praise)
  - Alternate sides between rows for visual rhythm:
      row 1: object LEFT, label RIGHT
      row 2: object RIGHT, label LEFT
      row 3: object LEFT, label RIGHT
      ...
  - DO NOT stack the label below the image; each row uses HORIZONTAL space, not vertical
  - Each row is approximately 280-360 px tall

For each TOOL row:
  - Center the tool horizontally on its own row, NO label, NO Chinese characters anywhere on or near the tool
  - Just the photorealistic object on a clean row

Strict rules (must follow):
  - All sub-items MUST be DISCRETE PHYSICAL OBJECTS, never abstract materials.
    FORBIDDEN words/concepts: 脂肪 / 蛋白质 / 糖类 / 淀粉 / 油脂 / 美拉德反应 / 发酵 / 风味 / 口感 / 调味 / 烘焙
  - All sub-items MUST be more atomic / upstream than "{dish_name}" itself.
  - Do NOT include "{dish_name}" itself as one of the sub-items.
  - Background (CRITICAL): Preserve the ORIGINAL photo's background and ambient lighting exactly — the wooden table, plate edge, surrounding props, light direction, and any out-of-focus elements behind the food. The scene should look like the original food gracefully exploded apart into its component ingredients/tools, floating in its OWN original environment. Do NOT invent a new studio backdrop, do NOT replace with a solid color, do NOT switch to a different table surface.
  - Ingredients on top, tools at the bottom. 80-120 px vertical gap between rows.

Output aspect: vertical (9:16). Rows stack vertically along the entire height.
```

**关键设计点**：
- **横向行布局**：物体一侧、中文标签另一侧，奇偶交替——避免"标签压在图下"的廉价信息图风格
- **DISCRETE PHYSICAL OBJECTS**：硬性禁掉「脂肪/蛋白质/美拉德反应」这类抽象词，否则模型会画"一团黄色的脂肪"
- **背景保留**：要求继承原图桌面/光线/景深，让爆炸图像是"原食物在原环境里炸开"，不是抠到白底

---

### 3.6 gpt-image-2 · Lv N+1 prompt（链路 B）

> 出处：`server_v2.py::LV_NEXT_PROMPT_TEMPLATE`（约 line 398）
> 占位符：`{brief}` ← 来自 §3.4 Gemini brief
> 参考图：上一层的 crop（不是整图！）

```text
The input image is a close-up crop showing: {brief}

Compose a FRESH hyper-realistic VERTICAL exploded-view that reveals the RECIPE of this object — i.e., what you would need to MAKE / PRODUCE / MANUFACTURE it.

This is a FRESH composition, NOT an edit of the input. Do not preserve any text or labels from the input. Treat the input only as a visual reference for what the target object looks like.

YOU decide the recipe. List the most accurate / interesting items:
  - 4 to 6 INGREDIENTS: discrete physical objects that go INTO the product (raw materials, parts, sub-components)
  - 2 to 3 TOOLS: discrete physical objects used during production but not consumed (machines, hand tools, implements)

For each INGREDIENT row (HORIZONTAL layout per row — critical):
  - Render the object photo-realistically on ONE side (left or right) of the row
  - On the OPPOSITE side of the SAME row, render a Chinese label:
      Line 1: the Chinese name in bold (around 42pt)
      Line 2-3: an interesting ~30-character Chinese description (origin / fun fact / role)
  - Alternate sides: row 1 object LEFT, row 2 object RIGHT, row 3 object LEFT, ...
  - DO NOT stack label below image. Use HORIZONTAL row space.

For each TOOL row:
  - Center the tool horizontally on its own row, NO label, NO Chinese characters anywhere on or near it.

Strict rules:
  - All sub-items MUST be DISCRETE PHYSICAL OBJECTS, never abstract materials.
    FORBIDDEN: 脂肪 / 蛋白质 / 糖类 / 淀粉 / 油脂 / 美拉德反应 / 发酵 / 风味 / 口感 / 调味 / 化学反应 / 物理变化
  - All sub-items MUST be more atomic / upstream than the input object.
  - Do NOT include the input object itself as one of the sub-items.
  - Background (CRITICAL): Inspect the input crop's surrounding environment — wooden table, kitchen counter, factory floor, workshop bench, soil, ocean, etc. — and PRESERVE that ambient setting in the output. The exploded scene should sit in the SAME world the input object was photographed in (same lighting direction, same surface, same atmospheric tone). Do NOT default to a studio backdrop. Do NOT replace the surface with a different material. If the crop background is ambiguous or tightly framed, infer a plausible setting from the object's natural habitat (e.g., a live goose belongs in a farm yard, not a kitchen counter) and render that consistently.
  - Ingredients on top, tools at the bottom. 80-120 px vertical gap between rows.

Output aspect: vertical (9:16). FRESH image, fresh Chinese typography — no remnants of any prior render.
```

**与 Lv1 的差异**：
- 把"recipe"扩展为「MAKE / PRODUCE / MANUFACTURE」——支持从食物钻入工业品/原材料
- 显式声明 **FRESH composition, NOT an edit**——抑制 image-edit 接口的字形/标签残留
- 背景指引扩展到「factory floor / workshop bench / soil / ocean」——支持非厨房场景
- 增加「ambiguous 时按物体自然栖息地推断」——例如活鹅放在农场院里、不是厨房台面

---

## 4. 容错与重试

| 调用 | 重试次数 | 退避 | 失败兜底 |
|---|---|---|---|
| MiniCPM | 3 | `0.8 × (attempt+1)` 秒线性 | 抛 RuntimeError，上层 500 |
| Gemini identify | 不重试 | — | 抛错，上层 500 |
| Gemini extract | 不重试 | — | 在 `start_journey` / `drill_journey` 内捕获，`layers=[]` 继续 |
| Gemini brief | 不重试 | — | 在 `drill_journey` 内捕获，用父层 `name_zh` 兜底 |
| gpt-image-2 | 3（`RETRY_TIMES`） | `2.0 × (attempt+1)` 秒线性退避 | 抛 RuntimeError，上层 500 |

> gpt-image-2 上游间歇性故障是已知坑（CLAUDE.md 暗坑 #1），所以重试是必须的。

---

## 5. 一句话总结

> **一张原图 + 一段菜名** → gpt-image-2 端到端创作爆炸图 → Gemini OCR 切热区。
> **drill 时只看局部 crop，不带历史上下文**——这是避免视觉残留的核心约束。
> 所有"画什么"的决定权交给 gpt-image-2，Gemini 只负责"读"（识名 / OCR / brief），不参与"想"。
