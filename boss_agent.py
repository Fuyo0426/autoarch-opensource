"""
boss_agent.py — BossAgent V3.1 (AutoArch)

協調六大專業 Squad，處理複雜建築任務。
此檔案為 Naomi_V21_Final.py 的獨立模組，需與主程式同目錄。

Handle 返回格式：
  {"answer": str, "_detected_city": str, "squad_used": str}
"""

from __future__ import annotations

import re
import logging
import asyncio
from typing import Dict, Any, Optional, List

logger = logging.getLogger("Naomi.BossAgent")

# Squad key → 中文名稱
SQUAD_NAMES: Dict[str, str] = {
    "s01": "法規資料組",
    "s02": "法規審查組",
    "s03": "法規查核組",
    "s04": "建築設計組",
    "s05": "室內設計組",
    "s06": "BIM 技術組",
    "s07": "綠建築組",
    "s08": "永續節能組",
    "s09": "行政整合組",
    "s10": "行銷公關組",
    "s11": "財務管理組",
    "s12": "數據採集組",
    "s13": "結構工程組",
    "s14": "機電工程組",
    "s15": "景觀設計組",
    "s16": "施工管理組",
    "s17": "地籍測量組",
    "s18": "環評地質組",
    "s19": "物業管理組",
    "s20": "智慧建築系統組",
    "s21": "不動產開發組",
    "s22": "都更整合組",
    "s23": "歷史建築修復組",
    "s24": "醫療建築組",
    "s25": "工業廠房組",
    "s26": "建照審查加速組",
}

# 城市偵測
TAIWAN_CITIES = [
    "台北", "臺北", "新北", "桃園", "台中", "臺中", "台南", "臺南",
    "高雄", "基隆", "新竹", "苗栗", "彰化", "南投", "雲林", "嘉義",
    "屏東", "宜蘭", "花蓮", "台東", "臺東", "澎湖", "金門", "連江",
]


def _detect_city(text: str) -> str:
    for city in TAIWAN_CITIES:
        if city in text:
            return city
    return ""


def _load_squad_system_prompt(squad_key: str) -> str:
    """從 docs/squads/ 載入 system_prompt.md，失敗時回傳預設提示。"""
    import pathlib
    squads_dir = pathlib.Path(__file__).parent / "docs" / "squads"
    for folder in squads_dir.iterdir():
        if not folder.is_dir():
            continue
        key_part = squad_key.lower().replace("_", "")
        folder_lower = folder.name.lower().replace("_", "")
        if key_part in folder_lower:
            prompt_file = folder / "system_prompt.md"
            if prompt_file.exists():
                return prompt_file.read_text(encoding="utf-8")
    return f"你是 AutoArch {SQUAD_NAMES.get(squad_key, squad_key)} 專家，請用繁體中文回答建築相關問題。"


async def _call_llm(
    system_prompt: str,
    user_message: str,
    async_groq=None,
    hub=None,
) -> str:
    """依可用的 LLM 客戶端發送請求，回傳回覆文字。"""

    # 1. Groq
    if async_groq:
        try:
            resp = await async_groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
                max_tokens=2048,
                temperature=0.3,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning(f"[BossAgent] Groq 失敗：{e}")

    # 2. Hub 上的其他 LLM 客戶端
    if hub:
        clients = getattr(hub, "clients", {})

        # OpenAI
        openai_client = clients.get("openai")
        if openai_client:
            try:
                resp = await openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_message},
                    ],
                    max_tokens=2048,
                    temperature=0.3,
                )
                return resp.choices[0].message.content
            except Exception as e:
                logger.warning(f"[BossAgent] OpenAI 失敗：{e}")

        # Claude
        claude_client = clients.get("claude")
        if claude_client:
            try:
                resp = await claude_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                return resp.content[0].text
            except Exception as e:
                logger.warning(f"[BossAgent] Claude 失敗：{e}")

    # 3. 無 LLM 可用 — 回傳說明訊息
    logger.warning("[BossAgent] 所有 LLM 客戶端不可用，回傳說明訊息")
    return (
        "系統目前無法連線到 AI 模型（Groq/OpenAI/Claude API Key 未設定）。\n\n"
        "請在 .env 檔案中設定至少一個 API Key：\n"
        "  GROQ_API_KEY=...\n"
        "  OPENAI_API_KEY=...\n"
        "  CLAUDE_API_KEY=...\n\n"
        "設定完成後重新啟動後端即可使用完整 AI 功能。"
    )


class BossAgent:
    """
    BossAgent V3.1 — AutoArch 任務協調器

    負責：
    1. 從 context 或任務文字偵測目標 Squad
    2. 嘗試透過 SquadManager 調度（若 Squad 已實作 execute_async）
    3. 從 docs/squads/ 載入對應 system_prompt，呼叫 LLM
    4. 回傳統一格式 {"answer": str, "_detected_city": str, "squad_used": str}
    """

    def __init__(
        self,
        hub,
        squad_manager,
        db_path: str = None,
        async_groq=None,
        brain_manager=None,
    ):
        self.hub = hub
        self.squad_manager = squad_manager
        self.db_path = db_path
        self.async_groq = async_groq or getattr(hub, "async_groq", None)
        self.brain_manager = brain_manager
        logger.info("[BossAgent] V3.1 初始化完成")

    async def handle(
        self,
        user_id: str,
        task: str,
        context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        主要入口：協調 Squad 並回傳答案。

        Parameters
        ----------
        user_id : str
        task    : str  — 用戶訊息或任務描述
        context : dict — 包含 intent、history、squad 等路由線索

        Returns
        -------
        {"answer": str, "_detected_city": str, "squad_used": str}
        """
        ctx = context or {}
        detected_city = _detect_city(task)

        # ── 1. 決定 Squad ───────────────────────────────────────────────
        squad_key: Optional[str] = (
            ctx.get("intent")
            or ctx.get("squad")
            or self.squad_manager.get_squad_by_trigger(task)
        )

        # ── 2. 嘗試 SquadManager 調度（Squad 已有實作時走這條）────────────
        if squad_key and squad_key in getattr(self.squad_manager, "squads", {}):
            try:
                result = await self.squad_manager.dispatch(
                    squad_key=squad_key,
                    user_id=user_id,
                    task=task,
                    context=ctx,
                )
                answer = result.get("answer", "")
                if answer:
                    logger.info(f"[BossAgent] Squad {squad_key} 已回應")
                    return {
                        "answer": answer,
                        "_detected_city": detected_city,
                        "squad_used": squad_key,
                    }
            except Exception as e:
                logger.warning(f"[BossAgent] SquadManager dispatch 失敗：{e}")

        # ── 3. 從 system_prompt.md 載入知識，呼叫 LLM ────────────────────
        system_prompt = _load_squad_system_prompt(squad_key) if squad_key else (
            "你是 AutoArch 全域建築 AI 顧問，精通台灣建築法規、設計、工程、商業開發。"
            "請用繁體中文回答，提供具體、可執行的建議。"
        )

        # 附加對話歷史（最近 3 輪）
        history: List[Dict] = ctx.get("history", [])
        messages = []
        for h in history[-3:]:
            if isinstance(h, dict) and "role" in h and "content" in h:
                messages.append(h)
        messages.append({"role": "user", "content": task})

        # 組合 messages 成單一文字傳給 LLM（簡化版，不走 multi-turn API）
        full_task = task
        if history:
            history_text = "\n".join(
                f"{'用戶' if h.get('role') == 'user' else 'AI'}：{h.get('content', '')}"
                for h in history[-3:] if isinstance(h, dict)
            )
            full_task = f"[對話歷史]\n{history_text}\n\n[當前問題]\n{task}"

        answer = await _call_llm(
            system_prompt=system_prompt,
            user_message=full_task,
            async_groq=self.async_groq,
            hub=self.hub,
        )

        squad_name = SQUAD_NAMES.get(squad_key, squad_key or "全域")
        logger.info(f"[BossAgent] LLM 回應完成（{squad_name}）")

        return {
            "answer": answer,
            "_detected_city": detected_city,
            "squad_used": squad_key or "general",
        }
