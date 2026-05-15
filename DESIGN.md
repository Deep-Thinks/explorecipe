# ExploreCipe · 设计系统

> 这份文档是 ExploreCipe UI 的 single source of truth。所有 token、组件 vocabulary、和它们的设计依据都在这里。改 UI 之前先读这页，改完之后同步这页。
>
> 视觉 DNA 继承自姊妹篇 [lilibear-world](../lilibear_world/index.html)，两者属于厦门大学美食协会同一系列。

---

## 0 · 设计哲学

1. **手工纸感 + 科普严谨**——米黄纸为底、墨色为边、偏移硬阴影为签名。爆炸图巨幅深色照片以"墨色画框挂在纸面上"的方式融入。
2. **每个昂贵动作都要先看到目标**——任何调用 60-90 秒上游生成的 click 都必须先进入 CONFIRM 状态（吉祥物 + 大字目标 + crop 预览 + ETA + 主/次按钮）。痛点根因消除。
3. **等待是讲故事的窗口**——60-90s loading 用三阶段进度 + 摇头吉祥物 + 轮播 quip + 计时器 + 上一层 peek 完整消化。
4. **删 emoji、用中文 + SVG**——`📷 🔧 🥬 ✏️ 🗺` 这些通用 emoji 在 explorecipe 调性里降级；只有 `~` `›` `→` `?` 等情境性符号保留。
5. **错误是平静的、可恢复的**——所有错误统一走顶部 toast banner，纸感卡 + accent 边框 + 重试按钮，不打断探索状态。

---

## 1 · 视觉 token

完整 CSS variables（src 见 `index_v2.html` `:root` 块。任何新模块必须只引用 token、不能硬编码）。

### 1.1 色板

```css
--paper:    #fbf3e4;   /* 米黄底色 · UI 主背景 */
--paper-2:  #f3e7cf;   /* 次级背景 · 卡片填充 */
--paper-3:  #ecdcb8;   /* 三级背景 · hover state */
--ink:      #4a342a;   /* 主文字 · 深褐 */
--ink-soft: #6e4d3d;   /* 次级文字 · 灰褐 (4.5:1 对 paper) */
--accent:   #d8593a;   /* 主强调 · 珊瑚红 · CTA / 关键状态 */
--accent-2: #b8451f;   /* 强调深 · hover / 大标题 */
--leaf:     #6f9b5a;   /* 食材标签绿 */
--tool:     #6a8aa3;   /* 工具标签灰蓝 (explorecipe 新增) */
--sky:      #d9ebe8;   /* 中性蓝绿 · info banner */
--shadow:   rgba(80, 50, 30, 0.18);  /* 偏移投影色 */

--canvas-bg:    #0d0d0f;     /* 爆炸图巨幅深色画布底 */
--canvas-frame: var(--ink);  /* 爆炸图边框 · 墨色画框感 */
```

**色盲冗余**：食材 / 工具区分必须用 **颜色 + 中文文字 + 形状（圆点 vs 方块）** 三冗余。

### 1.2 字体

```css
--font-display:     'ZCOOL KuaiLe', 'Ma Shan Zheng', sans-serif;  /* 大标题 / 按钮 */
--font-calligraphy: 'Ma Shan Zheng', 'ZCOOL KuaiLe', sans-serif;  /* 副标 / 角标 */
--font-body:        'Noto Sans SC', 'PingFang SC', sans-serif;     /* 正文 / 描述 */
--font-mono:        'JetBrains Mono', monospace;                   /* 计时 / 坐标 */
```

**`-apple-system` / `system-ui` / `BlinkMacSystemFont` 永久禁用作为主字体**。它们是 AI slop 黑名单 #11 的信号——"我放弃排版"。

字体由 Google Fonts 加载。国内访问没问题（lilibear-world 生产已验证）。

### 1.3 字号 / 间距 / 圆角 / 边框

```css
--fs-xs: 11px;  --fs-sm: 13px;  --fs-md: 15px;
--fs-lg: 19px;  --fs-xl: 28px;  --fs-xxl: 40px;

--sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px;  --sp-4: 16px;
--sp-5: 24px; --sp-6: 32px; --sp-7: 48px;

--r-sm: 6px;    /* 缩略图 / 小标签 */
--r-md: 10px;   /* 卡片 */
--r-lg: 16px;   /* 大卡片 / sheet 顶角 */
--r-pill: 28px; /* 胶囊按钮 */

--bw:      2.5px;  /* 实心粗边 */
--bw-thin: 1.5px;  /* 实心细边 / 虚线 */
```

### 1.4 投影（核心视觉签名）

```css
--shadow-stamp:      3px 3px 0 var(--ink);     /* 按钮 / 卡片 · 默认 */
--shadow-stamp-lg:   4px 4px 0 var(--ink);     /* hover 抬升 */
--shadow-stamp-sm:   2px 2px 0 var(--ink);     /* 小元件 */
--shadow-stamp-soft: 4px 4px 0 var(--shadow);  /* 巨幅爆炸图 · 柔化版 */
```

**永久禁止**：现代柔光阴影 `0 8px 24px ...`、`drop-shadow(0 4px 12px ...)`。所有阴影必须是偏移硬阴影（offset hard shadow），保留"印章贴纸面"的手工感。

吉祥物 SVG/PNG 用 `filter: drop-shadow(3px 3px 0 var(--ink)) drop-shadow(0 2px 4px var(--shadow));`——两层叠加：硬阴影做风格，柔影做体积。

### 1.5 动效

```css
--t-fast: 0.15s ease;
--t-mid:  0.25s ease;
--t-slow: 0.4s cubic-bezier(.4,1.4,.5,1);
```

吉祥物动效专用 keyframes：

```css
@keyframes wave       { 0%,100% { transform: rotate(-3deg); } 50% { transform: rotate(3deg); } }
@keyframes spin-bob   { 0%,100% { transform: translateY(0) rotate(-5deg); } 50% { transform: translateY(-6px) rotate(5deg); } }
@keyframes dotloop    { 0% { content: ''; } 25% { content: '.'; } 50% { content: '..'; } 75% { content: '...'; } }
```

`prefers-reduced-motion` 媒体查询下：所有 spin-bob/wave 减为静态；transition 时长归零；dot loop 改为静态 `...`。

---

## 2 · 组件 vocabulary

每个组件 = token 的固定组合。任何新页面只能用下列组件，不能临时写一次性样式。

### 2.1 `<Btn>`

| variant | 用途 |
|---|---|
| `.btn.primary` | 主行动。CTA、确认、提交。珊瑚红 + offset shadow |
| `.btn.ghost`   | 次级。取消、返回、辅助操作。透明 + ink 边 |
| `.btn.icon`    | 仅图标。36px 圆形、纸感、`aria-label` 必需 |
| `.btn.block`   | full-width 修饰类 |

hover：`translate(-1px, -1px) + shadow-stamp-lg`。active：`translate(2px, 2px) + shadow-stamp-sm`。

### 2.2 `<Pill>` / `<TagKind>`

`.pill` 用于 ETA / level / 次要标识，`--r-pill` 圆角 + dashed ink-soft 边 + paper-2 填。

`.tag-kind.food` / `.tag-kind.tool` 用于 layer 类型识别。

### 2.3 `<MascotBob>`

吉祥物图像（统一引用 `lilibear` 资源，见决策 4 of Pass 4）。三种状态：

- 静态 logo：`filter: drop-shadow(2px 2px 0 var(--ink))`
- `wave`：确认抽屉里挥手
- `spin-bob`：loading 状态摇头浮动

### 2.4 `<DrawerBottom>` / `<DrawerRight>`

`<DrawerBottom>`：移动端从底部上拉的 sheet。`border-top: var(--bw) solid var(--ink) + border-radius: var(--r-lg) var(--r-lg) 0 0 + 0 -8px 0 var(--ink) 投影`。带顶部把手 + ESC 关闭 + focus trap。

`<DrawerRight>`：桌面端从右侧滑入（复用 lilibear `#panel`）。

### 2.5 `<CanvasFrame>`

爆炸图容器。`--canvas-bg` 深色底 + `--bw` `--canvas-frame` 边 + `--r-sm` 圆角 + `--shadow-stamp-soft` 偏移投影。专门负责"深色照片在米色纸面上呈现"。

### 2.6 `<ToastBanner>`

错误统一形态。顶部 fixed、`--accent` 边、`--shadow-stamp`、含 message + 内联 retry 按钮。5s 自动消（hover 时暂停）。永久取代所有 `alert()`。

### 2.7 `<StageTrack>`

Loading 三段进度圈。`done` 状态为 `--leaf` 填，`active` 为 `--accent` 填，`pending` 为 `--paper-2` 填。三个圈用虚线连接，对应三个阶段（审核 → 识菜 → 画图）。

### 2.8 `<PeekCard>`

Loading 期间常驻底部的「上一层 peek」。纸感 dashed 卡 + 缩略图 + 文案。

---

## 3 · 屏幕级 patterns

### 3.1 首页 (UPLOAD)

- 顶部 `<Header>`：mascot + wordmark + 副标
- 中央 hero：大手写 `<h1>` + 副文案 + chip-row（示例探索路径）
- 主 CTA：`<Btn primary block>`「选张图开始拆」
- 双卷轴：`<HistoryBlock>` 个人 (localStorage) + `<HistoryBlock>` 全站 trending

### 3.2 EXPLORE (移动 / 桌面)

- 移动：顶部 mascot + 菜名 + 层级 + icon-btn × 2；面包屑藤蔓节点；`<CanvasFrame>` 爆炸图；底部 `<Btn ghost>` 查看清单 + `<Btn primary>` 框出一块
- 桌面：三列布局（mindmap 320px / canvas / layers 280px）；mindmap 中只展开当前层 trunk

### 3.3 CONFIRM（新状态、痛点 #2 解法）

- 触发：用户在画布上拖框完成 + 点「框出一块」按钮
- 形态：`<DrawerBottom>` 移动 / `<DrawerRight>` 桌面
- 内容：把手 + `<MascotBob wave>` + 大字「再拆一层 · <目标>？」+ crop 预览 + `<Pill ETA>` + 说明 + `<Btn primary>` 继续 + `<Btn ghost>` 取消

### 3.4 LOADING（痛点 #1 解法）

- `<MascotBob spin-bob>` + `<StageTrack>` + 主 quip + 副 quip + 计时器 + 底部 `<PeekCard>`
- 三阶段（含后端信号）：
  1. 「AI 在检查这是不是食物」`（审核中）`
  2. 「AI 在认这是啥菜」`（识菜中）`
  3. 「AI 正在画第 N 层爆炸图」`（生成中）`
- > 360s 时显示「再等等 / 取消」链接

### 3.5 错误 toast

- 触发：任何 fetch 失败、内容审核拒绝、文件超限
- 形态：`<ToastBanner>` 顶部固定
- 文案规范：
  - 拒人话：~~"HTTP 500 Internal Server Error"~~
  - 用人话：「AI 这一层没画出来呢，可能是上游堵车了。」+ `<Btn>` 重试

---

## 4 · 响应式断点

```
mobile          : < 768           默认
tablet          : 768 - 1023      mindmap 顶部水平条；面包屑隐藏
desktop         : 1024 - 1599     mindmap 左 320px
desktop-wide    : >= 1600         mindmap 320px / 画布 max-width 1200px
```

爆炸图自身比例 1024×1536，必须保持 portrait，且 `max-height: calc(100vh - 220px)` 防止溢出。

---

## 5 · 关键交互规则

| 行为 | 触发路径 | 是否走 CONFIRM？ |
|---|---|---|
| 上传新图 → Lv1 生成 | input file change | 否（用户主动上传即视为已确认）|
| **画布拖框/双击吸附 → drill** | 拖框 + 点「框出一块」按钮 | **是** |
| mindmap leaf 点击 | leaf click | **永不触发分解**——只在画布上高亮 bbox，浮出气泡「在图上框这块再继续分解」 |
| layers list 行点击 | 同上 | 同上 |
| 已钻过的 leaf 点击 | leaf click | switchLevel 到该层（纯导航）|
| 切换 path 中已有的层 | breadcrumb click | switchLevel（纯导航）|

**核心规则**：drill 触发只允许一条路径——画布拖框 + 点按钮。这保证用户始终亲眼看着图框选、不会被 AI bbox 不准误导。

---

## 6 · A11y 清单（实施时必检）

- [ ] 所有 button 有可读 label（含 `aria-label` for icon-only）
- [ ] 关键 `<img>` 有具体 alt（食物名 + 层级）
- [ ] 焦点 ring：所有 button focus-visible 显示 `var(--accent)` 2px outline
- [ ] 触摸目标 ≥ 44px：所有 list-row / leaf / crumb 增加 padding 至 44px 高
- [ ] `--ink-soft` 提亮到 `#6e4d3d` 满足 4.5:1
- [ ] `prefers-reduced-motion`：spinner 改 dot pulse；mascot 取消摇头；transition 时长 0
- [ ] 食材 / 工具：颜色 + 中文 + 形状（圆点 / 方块）三冗余
- [ ] ESC 关闭抽屉、focus trap、关闭时焦点回到触发元素
- [ ] 错误 toast 用 `role="alert"` + `aria-live="assertive"`

---

## 7 · 反模式（永远不要做）

1. **不要用 emoji 当装饰**——`📷` `🔧` `🥬` `✏️` `🗺` `⏳`。改用中文 + SVG icon + 形状冗余。
2. **不要用 system-ui / default font stack 当主字体**——这是「我放弃排版」的 AI slop 信号。
3. **不要用现代柔光阴影**——`0 8px 24px ...` 一律改成 `--shadow-stamp` 偏移硬阴影。
4. **不要用 `alert()`**——一律走 `<ToastBanner>`。
5. **不要让 mindmap / layer-row click 触发昂贵生成**——drill 只能从画布拖框。
6. **不要让 60-90s loading 是静态 spinner**——必须含阶段进度 + quip + 计时器 + peek。
7. **不要在确认前提交昂贵操作**——任何调用上游 60-90s 生成的 click 都必须先进 CONFIRM 抽屉。
8. **不要堆 3 列 feature grid / hero**——explorecipe 是 APP UI 不是 landing。
9. **不要让画布是孤立的纯黑块**——必须用 `--canvas-frame` 边 + `--shadow-stamp-soft` 投影呈现"墨色画框挂在纸面上"。

---

## 8 · 实施顺序建议

1. **token + 字体** → 把 `:root` 替换、import Google Fonts
2. **首页 + 上传按钮 + 我的拆解夹** → 视觉 DNA 先落地
3. **EXPLORE 主视图 + 画框 + 藤蔓面包屑** → 重构核心展示
4. **CONFIRM 抽屉** → 消除痛点 #2
5. **LOADING 三阶段 + peek + quip** → 消除痛点 #1
6. **toast banner + 杀 alert()** → 错误统一
7. **桌面 mindmap 折叠规则** → 信息密度收敛
8. **全站 trending 后端接口 + 完成态 opt-in 发布按钮**
9. **a11y 清单逐条对照实施**

每一步独立可验证、独立可回滚。建议按顺序提 PR、每个 PR 单独跑一次 UI 截图对比。

---

## 9 · 引用 & 来源

- 视觉 DNA：`/niuniu869_dev/lilibear_world/index.html`（已生产）
- 评审记录：本仓库 `.gstack/projects/Deep-Thinks-explorecipe/branch-reviews.jsonl`
- 评审 wireframe：`~/.gstack/projects/Deep-Thinks-explorecipe/designs/explore-confirm-20260515/wireframe.html`
- 决策日期：2026-05-15
- 评审人：plan-design-review (gstack)
