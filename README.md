# 建築自動化　開源系統

> Naomi V21 建築事務所 AI 平台。LINE Bot + FastAPI + 多智能體架構。

---

## 快速開始

```bash
pip install fastapi uvicorn line-bot-sdk chromadb groq openai anthropic \
            pydantic requests python-docx pypdf PyMuPDF python-dotenv psutil
cp .env.example .env   # 填入你的 API keys
python Naomi_V21_Final.py
```

---

## 資料夾結構

```
autoarch_opensource/
├── Naomi_V21_Final.py        # 主程式
├── skills/                   # 技能模組（動態載入）
├── squads/                   # Squad 定義（動態載入）
├── tools/                    # 工具模組
├── docs/
│   ├── squads/               # Squad 組織架構文件
│   └── laws/                 # 法規對照表
└── .github/workflows/        # 自動化流程
```

---

## Squad 架構

| Squad | 名稱 | 人數 |
|-------|------|------|
| 03 | 法規智慧組 | 6 |
| 04 | 建築設計組 | 6 |
| 05 | 室內設計組 | 6 |
| 06 | BIM 技術組 | 6 |
| 07 | 案例管理組 | 4 |
| 08 | 永續節能組 | 3 |
| 09 | 行政整合組 | - |
| 10 | 行銷公關組 | 6 |
| 11 | 財務管理組 | 11 |
| 12 | 數據採集組 | 3 |
| IT | 進化組 | 6 |

詳見 [docs/squads/Naomi_Squad組織架構.md](docs/squads/Naomi_Squad組織架構.md)

---

## 貢獻規則（自由協作）

- **直接 push `main`**，不需 PR 審核
- 每個人負責自己的 Squad 資料夾
- commit message 格式：`[Squad號] 說明` → 例：`[S03] 新增建蔽率條文`
- 法規文件放 `docs/laws/`，架構文件放 `docs/squads/`
- 程式模組放對應的 `skills/` 或 `squads/` 資料夾

---

## 自動化機制

每次 push 後，GitHub Actions 自動執行：

| 觸發條件 | 動作 |
|---------|------|
| 任何 push | 掃描所有 docs/ 更新 CHANGELOG |
| `docs/laws/` 有新增 | 自動更新法規索引 |
| `skills/` 有新增 | 自動更新技能清單 |
| `squads/` 有修改 | 自動更新 Squad 版本號 |

---

*自動生成索引由 GitHub Actions 維護，無需手動更新。*

<!-- PROGRESS_START -->
<!-- 此區塊由 GitHub Actions 自動更新，請勿手動編輯 -->
<!-- PROGRESS_END -->
