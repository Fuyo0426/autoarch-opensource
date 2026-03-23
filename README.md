# 建築自動化　開源系統

> 讓建築師不再花時間翻法規、算面積、查造價——AI 幫你做，你專注設計。
>
> 由建築師、法規專員、結構工程師、BIM 技術者共同維護的開源 AI 協作平台。

![License](https://img.shields.io/badge/license-MIT-orange)
![自動索引更新](https://github.com/Fuyo0426/autoarch-opensource/actions/workflows/auto-index.yml/badge.svg)
![進度儀表板](https://github.com/Fuyo0426/autoarch-opensource/actions/workflows/update-progress.yml/badge.svg)

---

## 這是什麼

這個系統讓建築師透過 LINE，直接問：

- 「這塊地的容積率上限是多少？」
- 「這棟建築需要幾個無障礙停車位？」
- 「15 層 RC 造，大概要多少造價？」
- 「這個案子的投報率和 IRR？」

AI 根據最新法規、真實案例、建材物價，給出有依據的回答。

---

## Squad 架構

詳細說明 → [ARCHITECTURE.md](ARCHITECTURE.md)

```
建築自動化 開源系統
│
├── 專業設計層
│   ├── S03  法規智慧組（6人）── 建築法規、容積率、地籍解析
│   ├── S04  建築設計組（6人）── 體量策略、結構初估、立面設計
│   ├── S05  室內設計組（6人）── 風格定義、照明計算、軟裝選配
│   └── S06  BIM 技術組（6人）── Revit建模、碰撞檢測、IFC輸出
│
├── 專案管理層
│   ├── S07  案例管理組（4人）── 作品集、造價基準、競圖策略
│   ├── S08  永續節能組（3人）── EEWH綠建築、ESG、碳足跡
│   └── S09  行政整合組───────── 甘特圖、建照申請、文件追蹤
│
├── 商業支援層
│   ├── S10  行銷公關組（6人）── 提案書、品牌、社群文案
│   ├── S11  財務管理組（11人）─ ROI/NPV/IRR、造價估算、融資
│   └── S12  數據採集組（3人）── 建材物價、法規監測、實價登錄
│
└── 系統核心層
    └── IT   進化組（6人）───── 系統監控、部署、自我進化
```

---

## 如何加入協作

**不需要懂程式，也不需要懂 Git。**

👉 [查看協作指南 + 加入申請](https://autoarch-guide.vercel.app)

---

## 貢獻流程

1. Fork 這個 Repo
2. 在自己的 Squad 資料夾新增或修改內容
3. 開 Pull Request，說明你做了什麼
4. 等候審核合併

**Commit 格式**：`[S03] 新增都市計畫法第 85 條解讀`

---

## 自動化機制

每次 Pull Request 合併後，系統自動執行：

| 動作 | 說明 |
|------|------|
| 更新法規索引 | `docs/laws/` 有新文件時自動重新編目 |
| 更新技能清單 | `skills/` 有新模組時自動列入 |
| 更新進度儀表板 | README 進度區塊自動刷新 |
| 產生 CHANGELOG | 每次變更自動記錄版本歷史 |

---

<!-- PROGRESS_START -->
## 📊 系統執行進度

> 自動更新於：2026-03-23 14:14 UTC ｜ 總 Commits：64 ｜ 貢獻者：2

### Squad 文件覆蓋率　`░░░░░░░░░░ 0%`

| 功能 | 狀態 | 說明 |
|------|------|------|
| 📋 法規文件庫 | ✅ 1 份文件 | `docs/laws/` |
| 🧠 Skills 模組 | ⬜ 尚無模組 | `skills/` |
| 🤖 Squad 程式模組 | ⬜ 尚無模組 | `squads/` |
| 🔧 工具模組 | ⬜ 尚無工具 | `tools/` |

### GitHub Actions 狀態

![自動索引更新](https://github.com/Fuyo0426/autoarch-opensource/actions/workflows/auto-index.yml/badge.svg)
![進化追蹤](https://github.com/Fuyo0426/autoarch-opensource/actions/workflows/evolution-tracker.yml/badge.svg)
![進度儀表板](https://github.com/Fuyo0426/autoarch-opensource/actions/workflows/update-progress.yml/badge.svg)

<!-- PROGRESS_END -->

---

## 開發者部署

<details>
<summary>展開部署說明</summary>

**環境需求**

```bash
pip install fastapi uvicorn line-bot-sdk chromadb groq openai anthropic \
            pydantic requests python-docx pypdf PyMuPDF python-dotenv psutil
```

**啟動**

```bash
cp .env.example .env   # 填入 API keys
python Naomi_V21_Final.py
```

**資料夾結構**

```
autoarch_opensource/
├── Naomi_V21_Final.py        # 主程式
├── skills/                   # 技能模組（動態載入）
├── squads/                   # Squad 定義
├── tools/                    # 工具模組
├── docs/
│   ├── squads/               # Squad 組織文件
│   └── laws/                 # 法規對照表
└── .github/workflows/        # 自動化流程
```

</details>

---

© 2026 建築自動化開源系統　[MIT License](LICENSE)
