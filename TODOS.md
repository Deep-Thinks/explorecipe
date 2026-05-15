# ExploreCipe · TODOS

> 设计评审 / 工程评审里识别出但未在本轮落地的工作。每条都有动机和上下文，3 个月后接手的人能秒懂为什么这条还没做。

---

## 设计评审 · 2026-05-15 衍生

### T1 · 后端 Loading 阶段信号（SSE 或 chunked json）

**What**：改造 `/api/start` 和 `/api/drill` 接口，让前端能实时感知三阶段进度——「审核中 → 识菜中 → 画图中」——而不是盲等。

**Why**：DESIGN.md § 3.4 / Pass 3 决策 5 「3 阶段 quip + 后端信号」是本轮重设计中消化 60-90s 等待的核心方案。没有后端信号，前端只能用启发式定时切换，可信度下降。

**Pros**：
- 用户实际看到 AI 进展、心理时间被实质性压缩
- 后端任何阶段卡住（审核服务 dn / Gemini 慢 / gpt-image-2 排队）都能精确指向哪一阶段
- 后续做 retry / cancel / "再等等" 都有精确的阶段定位

**Cons**：
- 服务从「一次 POST 等结果」改成「streaming response」，client / nginx / cloudflare 路径全程要兼容
- gpt-image-2 上游本身是 blocking 调用，阶段切换点只能在「本服务收到上游响应」时打——颗粒度有限

**Context**：当前 `server.py::call_dmfox_explosion` 已经走 3 次指数退避，上游耗时可以 logging 到。可选方案：
- (a) SSE：标准方案、cloudflare 兼容性好
- (b) chunked json：每行一个 progress event、最简单
- (c) WebSocket：过度工程，不推荐

**Depends on / blocked by**：无。可与 UI 重设计 PR 解耦，UI 先用启发式定时上线，后端做完替换为真实信号。

---

### T2 · 全站 trending 后端 + opt-in 发布流

**What**：
1. 数据库新增 `journeys.is_public` 字段（默认 false）+ `published_at` 时间戳
2. 新接口 `POST /api/journey/{id}/publish`（用户从完成态点「发到 trending」时触发）
3. 新接口 `GET /api/trending?limit=20`（首页加载时拉取）
4. 隐私兜底：trending 响应只暴露 `journey_id / dish_name_zh / Lv1 thumbnail_url / published_at`，**不暴露 IP / device / 上传者**

**Why**：决策 1 升级版（用户补充）——首页双卷轴：本机 localStorage「我的拆解夹」 + 全站 trending 「大家最近拆过」。lilibear-world 是默认公开（探索世界本身就是社交叙事），explorecipe 是私人食物内容（"我的午饭" 不该被默认公开），所以走 opt-in 模型。

**Pros**：
- 首访用户在首页就能看到产品产物，降低首次焦虑（Pass 3 时间维度 5 秒视觉感）
- 用户 opt-in 才进 trending，隐私优先
- 完成态新增「发到 trending」按钮 = 增加完成态情绪出口（lilibear 完成态 3 按钮模式延续）

**Cons**：
- 数据库迁移；现有 journeys 默认 is_public=false 是 safe
- 需要做基础 abuse 防护：是否要审核员审核？是否要"举报"按钮？至少先加 `flagged` 字段，前端有 hidden 报告链接
- trending 列表本身需要某种排序信号——按 `published_at` 倒序最简单，按"被分享次数"更准但需要更多数据点

**Context**：当前 `server_v2.py` journey 模型已有 `journey_id` 永久 URL 的基础。增加 publishing 是叠加而非重构。Lv1 thumbnail 已存在于 `logs/images/` 归档，需要给 nginx 加 public 路径访问。

**Depends on / blocked by**：UI 重设计上线（完成态新按钮 + 首页 trending 卷轴位置）。

---

### T3 · a11y 清单逐条实施

**What**：按 DESIGN.md § 6 清单全部实施：
- [ ] 所有 button 加 `aria-label`（含 icon-only）
- [ ] 关键 `<img>` 加具体 alt（食物名 + 层级，不是空串）
- [ ] focus-visible outline：所有 button 显示 `var(--accent)` 2px outline
- [ ] 触摸目标 ≥ 44px：list-row / leaf / crumb / icon-btn padding 调整
- [ ] `prefers-reduced-motion` 媒体查询：spinner 改 dot pulse；mascot 取消摇头；transition 时长归零
- [ ] 食材 / 工具：颜色 + 中文 + 形状（圆点 / 方块）三冗余（线框稿已实现）
- [ ] ESC 关闭抽屉、focus trap、关闭时焦点回到触发元素
- [ ] 错误 toast 用 `role="alert"` + `aria-live="assertive"`

**Why**：DESIGN.md § 0 第 5 条「设计是平静的、可恢复的」、§ 7 反模式都隐含 a11y 优先。视障 / 键盘 / 色盲 / 减少动效偏好用户也是用户。

**Pros**：起点高、未来不返工。Search engines、社团内推广（学生群体里有视障/色盲学生）友好

**Cons**：focus trap 在 vanilla JS 里要自己写、约 30 行代码。其他都是 1-2 行级别的小改动

**Context**：本轮 UI 重设计 PR 里可以顺手做掉一半（button aria-label / focus ring / 44px target / reduced-motion）；focus trap 和错误 role 单独一个小 PR 跟进。

**Depends on / blocked by**：UI 重设计 PR。
