# ExploreCipe · 一张照片，把食物拆给你看

> **上传一张食物照片 → AI 把它"爆炸"开来 → 点任意成分，递归无限分解。**

ExploreCipe 是一个用「视觉爆炸图 + LLM 视觉理解 + 中文科普卡」做食物可视化教育的开源项目。
姊妹项目是 [lilibear-world](https://github.com/Deep-Thinks/lilibear-world)，两者共享一套手工纸感视觉语言。

```
   一张鹅肝汉堡        AI 垂直分解            点击鹅肝层
        │                  │                     │
        ▼                  ▼                     ▼
   ┌─────────┐      ┌─────────────┐       ┌──────────────┐
   │  原图   │ ───▶ │  顶层面包    │ ───▶ │  鹅肝是什么？ │
   │         │      │  生菜       │       │  从哪里来？  │
   │         │      │  鹅肝       │       │  营养？      │
   │         │      │  牛肉饼     │       │  趣闻？      │
   │         │      │  底层面包    │       │              │
   └─────────┘      └─────────────┘       └──────────────┘
```

---

## ✨ 核心特性

- **垂直爆炸分解图**：用 `gpt-image-2` 把任意食物原图重新构图为「成分独立悬浮、垂直堆叠」的科普风格图
- **无限递归分解（v2）**：点击任意成分 → 生成它的下一层分解图（鹅肝汉堡 → 鹅肝 → 鹅肝的微观结构…），每层都是独立的 journey 节点
- **永久分享卡**：每个 journey 拿到 7 位 base32 短码（如 `/j/7Q4HK3M`），自带 Canvas 合成竖版分享卡 + 二维码
- **三阶段 loading 叙事**：60-90s 的上游生成不再是空白等待——审核中 → 识菜中 → 画图中，配合摇头吉祥物 + 轮播文案 + 上一层 peek
- **内容审核**：MiniCPM-V 1.3B 在调 `gpt-image-2` 前做 is_food 二分类，拦截人物/文档/建筑等非食物图，fail-closed
- **手工纸感视觉**：米黄纸底 + 墨色硬边 + 偏移硬阴影，拒绝 AI slop 的玻璃拟态
- **trending 卷轴**：首页双卷轴（本机「我的拆解夹」 + 全站 opt-in trending），探索社交叙事

---

## 🧠 架构

```
  浏览器 (HTTPS)
      │
      ▼
  [nginx :443/:80]   ← Let's Encrypt + certbot.timer 自动续期
      │ 反代
      ▼
  [server_v2.py :127.0.0.1:18082]   ← Python 标准库 HTTP server + LLM SDK
      │
      ├─ POST /api/start          → 上传食物图，生成 Lv1
      │      ├─ MiniCPM-V         · is_food 审核（fail-closed）
      │      ├─ Gemini 3 Flash    · 识菜名
      │      ├─ gpt-image-2       · 生成 Lv1 爆炸图
      │      └─ Gemini 3 Flash    · OCR 提取 layers 元数据 (bbox + label)
      │
      ├─ POST /api/drill          → {journey_id, from_level, bbox} 钻入下一层
      │      ├─ crop + brief      · 不带整图，只带局部 + 简介（避免字形漂移）
      │      ├─ gpt-image-2       · 生成 Lv N+1
      │      └─ Gemini 3 Flash    · 提取新层 layers
      │
      ├─ GET  /api/journey/{id}   → 整个 journey JSON
      ├─ GET  /j/{id}             → 永久分享页（SSR + OG meta）
      └─ GET  /journey/{id}/share-card.png  → Canvas 合成竖版分享卡
```

完整 LLM prompt 与调用链路：[LLM_PIPELINE.md](LLM_PIPELINE.md)
完整设计系统：[DESIGN.md](DESIGN.md)
v2「无限分解」详细设计：[PHASE2_DESIGN.md](PHASE2_DESIGN.md)
未完成的工作：[TODOS.md](TODOS.md)

---

## 🚀 本地跑起来

```bash
# 1. clone
git clone https://github.com/Deep-Thinks/explorecipe.git
cd explorecipe

# 2. 装依赖（建议用虚拟环境）
python3 -m pip install -r requirements.txt

# 3. 配置上游 API
cp .env.example .env
$EDITOR .env       # 填入 IMAGE_API_KEY / GEMINI_API_KEY / MINICPM_API_KEY

# 4. 跑 v2（默认 18083）
python3 server_v2.py
# 浏览器打开 http://localhost:18083
```

### 上游依赖一览

| 角色 | 模型 | 你需要准备的 |
|---|---|---|
| 图像生成 | `gpt-image-2` | 任意 OpenAI 兼容反代 + key（OpenAI 官方、Azure OpenAI、或第三方反代如 dm-fox/yescale/oneapi） |
| 视觉理解 | `gemini-3-flash-preview` | Google AI Studio 免费 key（https://aistudio.google.com/apikey） |
| 内容审核 | `MINICPM_23u6wt`（MiniCPM-V 1.3B） | 自部署 MiniCPM-V，或任意 OpenAI 兼容视觉小模型反代 |
| 科普卡（仅 v1） | `step-3.6`（StepFun） | StepFun 视觉模式 key |

> 不想接审核？设 `MODERATION_ENABLED=false`（生产仍建议保持开启，否则用户能传任何东西进 gpt-image-2，烧 token 又有合规风险）。

---

## 📦 v1 vs v2

仓库同时保留两份后端实现，对应产品迭代的两个阶段：

| | **v1** (`server.py` + `index.html`) | **v2** (`server_v2.py` + `index_v2.html`) |
|---|---|---|
| **能力** | 一次爆炸图 + 点击成分调 StepFun 拿科普卡 | 无限递归分解 + 永久分享 + 三阶段 loading + trending |
| **视觉理解** | StepFun step-3.6 | Gemini 3 Flash |
| **状态** | legacy，保留以备紧急回滚 | 当前主线 |
| **端口** | `PORT=18082` | `PORT_V2=18083`（生产复用 18082） |

新功能只往 v2 上加；v1 不再迭代但仍可独立运行。

---

## 🌐 部署

完整自动化部署脚本见 [`deploy/setup-remote.sh`](deploy/setup-remote.sh)，一行命令在裸服务器上拉起 nginx + systemd + post-merge auto-restart。

```bash
# 准备：DOMAIN/A 记录已生效、ssh 免密配置好
export DOMAIN=explorecipe.example.com
export REMOTE=user@your.server.ip

ssh "$REMOTE" 'bash -s' < deploy/setup-remote.sh
scp .env "$REMOTE":/opt/explorecipe/.env
ssh "$REMOTE" "certbot --nginx -d $DOMAIN --redirect --agree-tos -m you@example.com -n"
ssh "$REMOTE" "systemctl start explorecipe"
```

日常更新：本地 `git push` 后，远端 git hook 会自动 `systemctl restart explorecipe`，无需手动操作。

---

## 🗂 项目结构

```
explorecipe/
├── server.py              # v1 后端（legacy）
├── server_v2.py           # v2 后端（生产主线）
├── index.html             # v1 前端 SPA
├── index_v2.html          # v2 前端 SPA
├── static/                # 静态资源（吉祥物 logo 等）
├── deploy/                # 部署脚本
│   ├── setup-remote.sh    # 远端一键部署
│   ├── explorecipe.service  # systemd unit
│   ├── nginx-explorecipe.conf  # nginx vhost 模板
│   └── post-merge         # git hook：pull 后自动 restart
├── DESIGN.md              # 设计系统 single source of truth
├── PHASE2_DESIGN.md       # v2「无限分解」详细设计
├── LLM_PIPELINE.md        # LLM prompt 与调用链路总览
├── TODOS.md               # 待办与未尽事项
├── requirements.txt
├── .env.example           # 环境变量模板
└── LICENSE                # MIT
```

---

## 🧪 上游故障应急

`gpt-image-2` 反代偶发性故障是已知问题（任何第三方反代都不能假定 100% 可用），代码内置 3 次指数退避重试。如果上游持续不可用：

1. 切换 `IMAGE_UPSTREAM_URL` 到备用反代
2. 临时关闭审核：`MODERATION_ENABLED=false`
3. 重启服务：`systemctl restart explorecipe`

完整苦涩经验列表见 [PHASE2_DESIGN.md §3.5](PHASE2_DESIGN.md)。

---

## 🤝 贡献

欢迎 issue 和 PR。改 UI 前请先读 [DESIGN.md](DESIGN.md)（视觉 token 是 single source of truth），改 v2 业务逻辑前请先读 [PHASE2_DESIGN.md §3.5 苦涩经验](PHASE2_DESIGN.md)（很多看似多余的约束都有踩过的坑）。

---

## 📜 License

[MIT](LICENSE) · Copyright © 2026 [Deep-Thinks](https://github.com/Deep-Thinks)

---

## 🙏 致谢

- 视觉 DNA 来自姊妹项目 [lilibear-world](https://github.com/Deep-Thinks/lilibear-world)
- LLM 提供方：OpenAI（gpt-image-2）、Google（Gemini）、ModelBest（MiniCPM-V）、StepFun（step-3.6）
- 字体：[ZCOOL KuaiLe](https://fonts.google.com/specimen/ZCOOL+KuaiLe) · [Ma Shan Zheng](https://fonts.google.com/specimen/Ma+Shan+Zheng) · [Noto Sans SC](https://fonts.google.com/noto/specimen/Noto+Sans+SC) · [JetBrains Mono](https://www.jetbrains.com/lp/mono/)
