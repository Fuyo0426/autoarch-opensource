# -*- coding: utf-8 -*-
"""
Naomi_V21_Integrated.py — Naomi 建築事務所 AI 主程式 V21.1
============================================================

V21.1 整合修復：
  1. ✅ 技能動態載入（掃描 skills/ 目錄）
  2. ✅ 智能體群動態載入（掃描 squads/ 目錄）
  3. ✅ PDF 自動入庫（法規 → Squad03）
  4. ✅ 對話更自然（人格系統 + 記憶）
  5. ✅ 管理員任務主動偵測
  6. ✅ 自我進化功能整合
  7. ✅ 全域工具載入（tools/）

架構：
  LINE/API
    → ArchGateway（總發言人）
        → PermissionManager（權限控制）
        → SmartRouter（意圖分類）
        → BossAgent（任務調度）
            → SquadManager（12 個智能體群）
            → KernelHub（技能 + 工具）
        → EvolutionCore（自我進化）
        → ConsultationRecorder（諮詢記錄）
    → Dashboard（監控後台）

依賴：
  pip install fastapi uvicorn line-bot-sdk chromadb groq openai anthropic
              pydantic requests python-docx pypdf PyMuPDF python-dotenv psutil
"""

import os
import re
import io
import json
import time
import sqlite3
import logging
import pathlib
import asyncio
import datetime
import threading

# ── 永久背景 Event Loop（獨立 thread）────────────────────────────────────────
# asyncio.run() 完成後會關閉 event loop 並 cancel 所有 pending tasks。
# 為讓 fire-and-forget 任務（PDF 下載、M06 入庫）能真正跑完，
# 建立一個在獨立 daemon thread 中永久執行的 event loop。
_BG_LOOP = asyncio.new_event_loop()
_BG_THREAD = threading.Thread(
    target=_BG_LOOP.run_forever,
    name="NaomiBGLoop",
    daemon=True,
)
_BG_THREAD.start()

# 防止 background futures 被 GC 清除
_BG_TASKS: set = set()

def _fire_and_forget(coro):
    """把 coroutine 丟進永久背景 loop，不受 asyncio.run() 生命週期影響"""
    future = asyncio.run_coroutine_threadsafe(coro, _BG_LOOP)
    _BG_TASKS.add(future)
    future.add_done_callback(_BG_TASKS.discard)
    return future
import hashlib
import importlib.util
from typing import Dict, List, Any, Optional, Tuple, Callable
from enum import Enum
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

import uvicorn
from dotenv import load_dotenv
load_dotenv()

# 關閉 ChromaDB 匿名遙測（避免 posthog 錯誤干擾 log）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

# 中文別名標準化（Squad 口語化識別）
try:
    from utils.alias_normalizer import normalize as alias_normalize, get_org_context, get_few_shot_prompt
    _ALIAS_NORMALIZER_LOADED = True
except ImportError:
    _ALIAS_NORMALIZER_LOADED = False
    def alias_normalize(text): return {"squad_id": None, "squad_name": None, "action": None, "matched": False}
    def get_org_context(): return ""
    def get_few_shot_prompt(): return ""

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    ApiClient, Configuration,
    MessagingApi, ReplyMessageRequest, PushMessageRequest, TextMessage,
    MessagingApiBlob, FlexMessage,
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, FileMessageContent,
    ImageMessageContent, AudioMessageContent,
)
from linebot.v3.exceptions import InvalidSignatureError

# ==============================================================================
# 1. 環境配置
# ==============================================================================

_LOG_DIR = pathlib.Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "naomi_current.log"

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# 終端機 handler
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)

# 檔案 handler（追加，供 dashboard 讀取）
_file_handler = logging.FileHandler(str(_LOG_FILE), encoding="utf-8", mode="a")
_file_handler.setFormatter(_log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_stream_handler, _file_handler],
)
logger = logging.getLogger("Naomi.Main")

# API Keys
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "taide-llama3-8b")

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "naomi_admin_2026")
ADMIN_LINE_USER_IDS = [uid.strip() for uid in os.getenv("ADMIN_LINE_USER_IDS", "").split(",") if uid.strip()]
# 內部員工：設於 .env → 啟動時自動設 role=super_admin + tenant_id=company
INTERNAL_USER_IDS = [uid.strip() for uid in os.getenv("INTERNAL_USER_IDS", "").split(",") if uid.strip()]
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

# 路徑（重要：使用正確的基礎目錄）
BASE_DIR = pathlib.Path(os.getenv("BASE_DIR", r"C:\Users\User\My_AI_Agent")).resolve()
DB_PATH = BASE_DIR / "database" / "naomi_main.db"
CHROMA_PATH = str(BASE_DIR / "memory" / "law_vector_db")
UPLOAD_BASE = BASE_DIR / "mnt" / "uploads"
SANDBOX_DIR = BASE_DIR / "mnt" / "sandbox"
LAW_LIBRARY_BASE = BASE_DIR / "law_library"
PENDING_LAWS_DIR = BASE_DIR / "pending_laws"
SKILLS_DIR = BASE_DIR / "skills"
SQUADS_DIR = BASE_DIR / "squads"
TOOLS_DIR = BASE_DIR / "tools"

# 建立必要目錄
for _d in [DB_PATH.parent, UPLOAD_BASE, SANDBOX_DIR, LAW_LIBRARY_BASE, 
           PENDING_LAWS_DIR, SKILLS_DIR, SQUADS_DIR, TOOLS_DIR,
           pathlib.Path(CHROMA_PATH)]:
    _d.mkdir(parents=True, exist_ok=True)

# LLM 客戶端
GROQ_CLIENT = None
ASYNC_GROQ = None
ASYNC_OPENAI = None
ASYNC_CLAUDE = None
OLLAMA_CLIENT = None

try:
    from groq import Groq, AsyncGroq
    if GROQ_API_KEY:
        GROQ_CLIENT = Groq(api_key=GROQ_API_KEY)
        ASYNC_GROQ = AsyncGroq(api_key=GROQ_API_KEY)
        logger.info("[LLM] Groq 已連線")
except ImportError:
    logger.warning("[LLM] Groq 未安裝")

try:
    from openai import AsyncOpenAI
    if OPENAI_API_KEY:
        ASYNC_OPENAI = AsyncOpenAI(api_key=OPENAI_API_KEY)
        logger.info("[LLM] OpenAI 已連線")
except ImportError:
    logger.warning("[LLM] OpenAI 未安裝")

try:
    from anthropic import AsyncAnthropic
    if CLAUDE_API_KEY:
        ASYNC_CLAUDE = AsyncAnthropic(api_key=CLAUDE_API_KEY)
        logger.info("[LLM] Claude 已連線")
except ImportError:
    logger.warning("[LLM] Anthropic 未安裝")

try:
    import ollama as ollama_lib
    OLLAMA_CLIENT = ollama_lib
    logger.info("[LLM] Ollama 可用")
except ImportError:
    logger.warning("[LLM] Ollama 未安裝")

# LINE
LINE_CONFIG = Configuration(access_token=LINE_ACCESS_TOKEN) if LINE_ACCESS_TOKEN else None
HANDLER = WebhookHandler(LINE_SECRET) if LINE_SECRET else None

# FastAPI — lifespan 取代已棄用的 on_event("startup")
from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(app):
    """Naomi 啟動 / 關閉生命週期"""
    await _on_startup()
    yield
    # shutdown（如有需要可在此加清理邏輯）

app = FastAPI(title="Naomi 建築事務所 AI — V21.1", version="21.1", lifespan=_lifespan)

# ── 檔案下載暫存（token → file_path，支援 DXF / Excel / 任意格式）──────────
import secrets as _secrets
_FILE_TOKENS: dict[str, str] = {}   # {token: absolute_path}

SERVER_PUBLIC_URL = os.getenv("SERVER_PUBLIC_URL", "http://localhost:8000")

def _register_file(file_path: str) -> str:
    """產生下載 token，回傳 token（支援 DXF、Excel 等任意格式）"""
    global _FILE_TOKENS
    if len(_FILE_TOKENS) > 500:
        keys = list(_FILE_TOKENS.keys())
        for k in keys[:250]:
            _FILE_TOKENS.pop(k, None)
    token = _secrets.token_urlsafe(16)
    _FILE_TOKENS[token] = str(file_path)
    return token

# 向下相容舊名
_register_dxf = _register_file

def _get_download_url(token: str) -> str:
    return f"{SERVER_PUBLIC_URL}/download/{token}"


# ==============================================================================
# 2. 用戶角色與權限系統
# ==============================================================================

class UserRole(Enum):
    SUPER_ADMIN  = "super_admin"
    ADMIN        = "admin"
    # 商業方案
    TIER_PRO     = "tier_pro"    # 旗艦：法規+圖說+投報表+共負比
    TIER_MID     = "tier_mid"    # 進階：法規+建設圖說+室內圖說
    TIER_BASIC   = "tier_basic"  # 基礎：初步法規諮詢
    # 相容舊值
    PROFESSIONAL = "professional"
    GENERAL      = "general"
    GUEST        = "guest"

ROLE_SQUAD_ACCESS = {
    UserRole.SUPER_ADMIN:  "*",
    UserRole.ADMIN:        "*",
    UserRole.TIER_PRO:     "*",
    UserRole.TIER_MID:     [
        "03_regulatory_intel", "04_architectural_design",
        "05_interior_design",  "07_project_portfolio",
        "06_bim_technology",   "02_global_intelligence",
    ],
    UserRole.TIER_BASIC:   ["03_regulatory_intel"],
    UserRole.PROFESSIONAL: ["03_regulatory_intel", "04_architectural_design", "06_bim_technology"],
    UserRole.GENERAL:      ["03_regulatory_intel"],
    UserRole.GUEST:        [],
}

ROLE_QUOTA = {
    UserRole.SUPER_ADMIN:  -1,
    UserRole.ADMIN:        -1,
    UserRole.TIER_PRO:     -1,   # 無限
    UserRole.TIER_MID:     200,
    UserRole.TIER_BASIC:   30,
    UserRole.PROFESSIONAL: 500,
    UserRole.GENERAL:      50,
    UserRole.GUEST:        5,
}


class PermissionManager:
    """權限管理"""
    
    def __init__(self, db_path: str, admin_ids: List[str]):
        self.db_path = db_path
        self.admin_ids = admin_ids
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    role TEXT DEFAULT 'general',
                    monthly_quota INTEGER DEFAULT 50,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quota_usage (
                    user_id TEXT,
                    month TEXT,
                    used INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, month)
                )
            """)
            conn.commit()
    
    def get_user_role(self, user_id: str) -> UserRole:
        # env 清單（向後相容）
        if user_id in self.admin_ids:
            return UserRole.SUPER_ADMIN

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT role FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()

        if row:
            try:
                return UserRole(row[0])
            except ValueError:
                pass

        return UserRole.GENERAL

    def register_admin(self, user_id: str):
        """將 user_id 升為 super_admin 並永久存入 DB"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO users (user_id, role)
                VALUES (?, 'super_admin')
                ON CONFLICT(user_id) DO UPDATE SET role='super_admin'
            """, (user_id,))
            conn.commit()
        if user_id not in self.admin_ids:
            self.admin_ids.append(user_id)
        logger.info(f"[Permission] 已將 {user_id[:8]}.. 設為 super_admin")

    def get_all_admins(self) -> List[str]:
        """返回所有管理員 user_id（env + DB 合併，含 admin 和 super_admin）"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT user_id FROM users WHERE role IN ('super_admin', 'admin')"
            ).fetchall()
        db_admins = [r[0] for r in rows]
        return list(set(self.admin_ids + db_admins))

    def has_any_admin(self) -> bool:
        return bool(self.get_all_admins())
    
    def get_allowed_squads(self, user_id: str) -> List[str]:
        role = self.get_user_role(user_id)
        access = ROLE_SQUAD_ACCESS.get(role, [])
        return ["*"] if access == "*" else access
    
    def check_quota(self, user_id: str) -> Dict:
        role = self.get_user_role(user_id)
        quota = ROLE_QUOTA.get(role, 50)
        
        if quota == -1:
            return {"allowed": True, "unlimited": True}
        
        month = datetime.datetime.now().strftime("%Y-%m")
        
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT used FROM quota_usage WHERE user_id = ? AND month = ?",
                (user_id, month)
            ).fetchone()
            used = row[0] if row else 0
        
        return {
            "allowed": used < quota,
            "used": used,
            "quota": quota,
            "remaining": quota - used
        }
    
    def use_quota(self, user_id: str):
        month = datetime.datetime.now().strftime("%Y-%m")
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO quota_usage (user_id, month, used)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, month) DO UPDATE SET used = used + 1
            """, (user_id, month))
            conn.commit()


# ==============================================================================
# 3. 多 LLM 大腦管理器
# ==============================================================================

class BrainRole(Enum):
    CASUAL     = "casual"      # 閒聊     → Groq 70b
    ANALYST    = "analyst"     # 快速分析  → Groq 70b
    AUDITOR    = "auditor"     # 系統管理  → OpenAI  (SysAgent / Auditor)
    REVIEWER   = "reviewer"    # 法規審核  → Claude  (Reviewer / Director)
    PROGRAMMER = "programmer"  # 編程/本地 → Ollama
    LEGAL      = "legal"       # 法規 RAG  → Claude > Groq（禁止 Ollama，品質不穩定）

LLM_COSTS = {"groq": 0.0001, "openai": 0.01, "claude": 0.015, "ollama": 0}

# ── 各技能/Squad 的 LLM 配置表 ─────────────────────────────────────────────────
# 對應 .env 三種架構：
#   Groq      → 快速導航與分析員 (Analyst)           → casual/general/design/bim/finance/project
#   OpenAI    → 系統管理員(SysAgent)/檢討員(Auditor)  → system/admin_task/squad_query/schedule
#   Anthropic → 法律鑑定員(Reviewer)/組長(Director)   → legal/squad_03/reviewer
#   Ollama    → 程序員(Programmer)/本地法規            → programmer/law_analyst
SKILL_LLM_CONFIG: Dict[str, Dict[str, str]] = {
    # ── Claude Haiku：對話中樞（聽懂人話、自然反應、任務確認）────────
    "casual":       {"provider": "claude", "model": "claude-haiku-4-5-20251001"},
    "general":      {"provider": "claude", "model": "claude-haiku-4-5-20251001"},
    # ── Groq：專業分析層（Squad 工作語言，速度優先）──────────────────
    "design":       {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad04/05
    "bim":          {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad06
    "finance":      {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad11
    "project":      {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad07
    "internal":     {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad09
    "marketing":    {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad10
    "operations":   {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad08
    "intelligence": {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad02
    "data":         {"provider": "groq",   "model": "llama-3.3-70b-versatile"},  # Squad12
    # ── OpenAI：系統管理/審計層 ────────────────────────────────────────
    "schedule":     {"provider": "openai", "model": "gpt-4o-mini"},
    "system":       {"provider": "openai", "model": "gpt-4o-mini"},
    "admin_task":   {"provider": "openai", "model": "gpt-4o-mini"},
    "squad_query":  {"provider": "openai", "model": "gpt-4o-mini"},
    "memory":       {"provider": "openai", "model": "gpt-4o-mini"},
    "classifier":   {"provider": "groq",   "model": "llama-3.1-8b-instant"},   # 分類器保持輕量
    # ── Claude：法律嚴格引用層 ──────────────────────────────────────────
    "legal":        {"provider": "claude", "model": "claude-sonnet-4-6"},  # Squad03 法規（升級 Sonnet）
    "squad_03":     {"provider": "claude", "model": "claude-sonnet-4-6"},  # Squad03 直呼（升級 Sonnet）
    "reviewer":     {"provider": "claude", "model": "claude-sonnet-4-6"},  # 品質審查
    # ── Ollama：本地推理層 ────────────────────────────────────────────
    "programmer":   {"provider": "ollama", "model": "law_analyst"},                 # it_evolution
}


class BrainManager:
    """多 LLM 大腦管理"""
    
    def __init__(self):
        self.clients = {}
        self.stats = {}
        
        if ASYNC_GROQ:
            self.clients["groq"] = ASYNC_GROQ
            self.stats["groq"] = {"calls": 0, "tokens": 0, "cost": 0, "errors": 0, "total_time_ms": 0}
        
        if ASYNC_OPENAI:
            self.clients["openai"] = ASYNC_OPENAI
            self.stats["openai"] = {"calls": 0, "tokens": 0, "cost": 0, "errors": 0, "total_time_ms": 0}
        
        if ASYNC_CLAUDE:
            self.clients["claude"] = ASYNC_CLAUDE
            self.stats["claude"] = {"calls": 0, "tokens": 0, "cost": 0, "errors": 0, "total_time_ms": 0}
        
        if OLLAMA_CLIENT:
            self.clients["ollama"] = {"client": OLLAMA_CLIENT, "model": OLLAMA_MODEL}
            self.stats["ollama"] = {"calls": 0, "tokens": 0, "cost": 0, "errors": 0, "total_time_ms": 0}
    
    def get_client(self, role: BrainRole, complexity: str = "medium"):
        if role == BrainRole.PROGRAMMER:
            if "ollama" in self.clients:
                return self.clients["ollama"], "ollama"
            elif "groq" in self.clients:
                return self.clients["groq"], "groq"

        if role == BrainRole.LEGAL:
            # Claude > Groq（Ollama 品質不穩定，不用於用戶端法規回答）
            if "claude" in self.clients:
                return self.clients["claude"], "claude"
            elif "groq" in self.clients:
                return self.clients["groq"], "groq"

        if role == BrainRole.REVIEWER:
            # 法律鑑定員/組長 → Claude (Anthropic)
            if "claude" in self.clients:
                return self.clients["claude"], "claude"
            elif "openai" in self.clients:
                return self.clients["openai"], "openai"

        if role == BrainRole.AUDITOR:
            # 系統管理員/檢討員 → OpenAI
            if "openai" in self.clients:
                return self.clients["openai"], "openai"
            elif "groq" in self.clients:
                return self.clients["groq"], "groq"

        # 預設：Claude Haiku（對話中樞，Analyst / Casual）→ 降級 Groq
        if "claude" in self.clients:
            return self.clients["claude"], "claude"
        if "groq" in self.clients:
            return self.clients["groq"], "groq"

        for name, client in self.clients.items():
            return client, name
        return None, "none"
    
    async def call_skill(self, skill_id: str, messages: List[Dict],
                         max_tokens: int = 1000, json_mode: bool = False,
                         real_data: Optional[str] = None) -> Dict:
        """
        依技能 ID 調度對應 LLM。
        real_data: 真實執行結果字串。若提供，自動注入到 system prompt，
                   要求 LLM 只能呈現這份真實資料，不能自行捏造。
        """
        cfg = SKILL_LLM_CONFIG.get(skill_id, {"provider": "groq", "model": "llama-3.3-70b-versatile"})
        provider = cfg["provider"]
        model    = cfg["model"]

        # 如果有真實資料，強制注入 grounding 規則
        if real_data:
            grounding = (
                f"【真實執行結果如下，你只能根據這份資料回應，不能自行捏造或擴充】\n"
                f"{real_data}\n"
                f"【規則：不可描述尚未發生的事；不可列出你「將要」做的功能；只呈現上方真實結果】"
            )
            # 注入到 system message
            if messages and messages[0]["role"] == "system":
                messages[0]["content"] = grounding + "\n\n" + messages[0]["content"]
            else:
                messages.insert(0, {"role": "system", "content": grounding})

        logger.info(f"[Brain] skill={skill_id} provider={provider} model={model}")
        return await self._dispatch(provider, model, messages, max_tokens, json_mode)

    async def call(self, role: BrainRole, messages: List[Dict],
                   max_tokens: int = 1000, json_mode: bool = False) -> Dict:
        
        client, llm_name = self.get_client(role)
        model = None
        if llm_name == "groq":
            model = "llama-3.3-70b-versatile"
        return await self._dispatch(llm_name, model, messages, max_tokens, json_mode, client)

    async def _dispatch(self, provider: str, model: Optional[str],
                        messages: List[Dict], max_tokens: int = 1000,
                        json_mode: bool = False, client=None) -> Dict:
        """統一 LLM 調度入口"""
        # 取得 client
        if client is None:
            if provider == "groq":
                client = self.clients.get("groq")
            elif provider == "claude":
                client = self.clients.get("claude")
            elif provider == "openai":
                client = self.clients.get("openai")
            elif provider == "ollama":
                client = self.clients.get("ollama")

        if not client:
            # Fallback：換 groq
            client = self.clients.get("groq")
            provider = "groq"
            if not client:
                return {"content": "AI 服務暫時不可用", "llm": "none", "error": True}

        if provider not in self.stats:
            self.stats[provider] = {"calls": 0, "tokens": 0, "cost": 0, "errors": 0, "total_time_ms": 0}

        start_time = time.time()
        try:
            if provider == "groq":
                result = await self._call_groq(client, messages, max_tokens, json_mode, model)
            elif provider == "openai":
                result = await self._call_openai(client, messages, max_tokens, json_mode, model)
            elif provider == "claude":
                result = await self._call_claude(client, messages, max_tokens, model)
            elif provider == "ollama":
                result = await self._call_ollama(client, messages)
            else:
                result = {"content": "未知 LLM", "error": True}

            elapsed_ms = int((time.time() - start_time) * 1000)
            tokens = result.get("tokens", 0)
            self.stats[provider]["calls"] += 1
            self.stats[provider]["tokens"] += tokens
            self.stats[provider]["total_time_ms"] += elapsed_ms
            self.stats[provider]["cost"] += (tokens / 1000) * LLM_COSTS.get(provider, 0)
            result["llm"] = provider
            result["model"] = model
            result["time_ms"] = elapsed_ms
            return result

        except Exception as e:
            self.stats[provider]["errors"] += 1
            err_str = str(e)
            logger.error(f"[Brain] {provider}/{model} 調用失敗: {e}")

            # 非 Groq 且非 429 → 降級到 Groq
            if provider != "groq" and not self._is_rate_limit(e):
                groq_client = self.clients.get("groq")
                if groq_client:
                    logger.warning(f"[Brain] {provider} 降級到 groq（{err_str[:60]}）")
                    return await self._dispatch(
                        "groq", "llama-3.3-70b-versatile",
                        messages, max_tokens, json_mode, groq_client
                    )

            # 429 或所有降級失敗 → 安全提示，不暴露原始錯誤
            return {"content": "目前 AI 服務暫時達到用量上限，請稍等一下，約 5 分鐘後再試。",
                    "llm": provider, "error": True}
    
    # Groq 降級鏈（當主力模型 429 時依序嘗試）
    _GROQ_FALLBACK_CHAIN = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
    ]

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        msg = str(exc)
        return "429" in msg or "rate_limit_exceeded" in msg or "rate limit" in msg.lower()

    async def _call_groq(self, client, messages, max_tokens, json_mode,
                          model: str = "llama-3.3-70b-versatile") -> Dict:
        # 建立降級鏈（主力模型排首位）
        chain = list(self._GROQ_FALLBACK_CHAIN)
        if model and model not in chain:
            chain.insert(0, model)
        elif model:
            chain = [model] + [m for m in chain if m != model]

        last_err = None
        for m in chain:
            try:
                kwargs = {"model": m, "messages": messages, "max_tokens": max_tokens}
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = await client.chat.completions.create(**kwargs)
                if m != chain[0]:
                    logger.info(f"[Brain] Groq 降級成功，使用模型：{m}")
                return {"content": resp.choices[0].message.content,
                        "tokens": resp.usage.total_tokens if resp.usage else 0}
            except Exception as e:
                last_err = e
                if self._is_rate_limit(e):
                    logger.warning(f"[Brain] {m} 429，切換下一個 Groq 模型")
                    await asyncio.sleep(1.5)   # 退避等待，避免立即再打 429
                    continue
                raise  # 非 429 錯誤直接上拋

        # 所有 Groq 模型耗盡 → 嘗試 Ollama
        try:
            import httpx as _httpx
            payload = {"model": "llama3.2", "messages": messages,
                       "stream": False, "options": {"num_predict": max_tokens}}
            async with _httpx.AsyncClient(timeout=30) as hc:
                r = await hc.post("http://localhost:11434/api/chat", json=payload)
                r.raise_for_status()
                content = r.json()["message"]["content"]
                logger.info("[Brain] Ollama fallback 成功")
                return {"content": content, "tokens": 0}
        except Exception as ollama_err:
            logger.error(f"[Brain] Ollama 也失敗：{ollama_err}")

        # 最終降級：安全提示，不暴露技術錯誤給用戶
        logger.error(f"[Brain] 所有 LLM 不可用，最後錯誤：{last_err}")
        return {"content": "目前 AI 服務暫時達到用量上限，請稍後約 5 分鐘再試。",
                "llm": "none", "error": True}

    async def _call_openai(self, client, messages, max_tokens, json_mode,
                            model: str = "gpt-4-turbo-preview") -> Dict:
        kwargs = {"model": model or "gpt-4-turbo-preview",
                  "messages": messages, "max_tokens": max_tokens}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await client.chat.completions.create(**kwargs)
        return {"content": resp.choices[0].message.content,
                "tokens": resp.usage.total_tokens if resp.usage else 0}

    async def _call_claude(self, client, messages, max_tokens,
                            model: str = "claude-sonnet-4-6") -> Dict:
        system_msg = ""
        claude_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                claude_messages.append(msg)
        resp = await client.messages.create(
            model=model or "claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=system_msg,
            messages=claude_messages
        )
        return {"content": resp.content[0].text,
                "tokens": resp.usage.input_tokens + resp.usage.output_tokens}
    
    async def _call_ollama(self, client_info, messages) -> Dict:
        ollama = client_info["client"]
        model = client_info["model"]
        loop = asyncio.get_event_loop()
        
        def sync_call():
            return ollama.chat(model=model, messages=messages)
        
        response = await loop.run_in_executor(None, sync_call)
        return {"content": response['message']['content'], "tokens": 0}
    
    def get_stats(self) -> Dict:
        return {
            "by_llm": self.stats,
            "total_calls": sum(s["calls"] for s in self.stats.values()),
            "total_cost": round(sum(s["cost"] for s in self.stats.values()), 4),
            "available_llms": list(self.clients.keys())
        }


# ==============================================================================
# 4. 諮詢記錄系統
# ==============================================================================

class ConsultationRecorder:
    """諮詢記錄器"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consultation_records (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    squad_key TEXT,
                    intent TEXT,
                    query TEXT,
                    response TEXT,
                    llm_used TEXT,
                    response_time_ms INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    
    def record(self, user_id: str, squad_key: str, intent: str,
               query: str, response: str, llm_used: str = None, 
               response_time_ms: int = 0) -> str:
        
        record_id = f"CON-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{hashlib.md5(query.encode()).hexdigest()[:4]}"
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO consultation_records 
                (id, user_id, squad_key, intent, query, response, llm_used, response_time_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (record_id, user_id, squad_key, intent, query, response[:2000], llm_used, response_time_ms))
            conn.commit()
        
        return record_id
    
    def get_analytics(self, days: int = 30) -> Dict:
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM consultation_records WHERE created_at > datetime('now', ?)",
                (f'-{days} days',)
            ).fetchone()[0]
            
            unique_users = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM consultation_records WHERE created_at > datetime('now', ?)",
                (f'-{days} days',)
            ).fetchone()[0]
            
            return {
                "period_days": days,
                "total_consultations": total,
                "unique_users": unique_users,
            }


# ==============================================================================
# 5. KnowledgeGapManager — 知識缺口管理（管理員審核學習）
# ==============================================================================

class KnowledgeGapManager:
    """
    管理員審核式知識學習系統

    流程：
      1. Naomi 回答不確定時 → 自動標記為「待審核缺口」
      2. 管理員在 Dashboard 看到問題清單
      3. 管理員提供正確答案
      4. 答案存入 ChromaDB 向量庫
      5. 下次遇到類似問題 → Naomi 先查知識庫再回答
    """

    def __init__(self, db_path: str, chroma_path: str = None, vector_collection=None):
        self.db_path = db_path
        self.collection = self._init_knowledge_collection(chroma_path) or vector_collection
        self._init_db()
        logger.info(f"[GapManager] 初始化完成，知識庫: {self.collection.count() if self.collection else 0} 筆")

    def _init_knowledge_collection(self, chroma_path: str):
        """建立獨立的 admin_knowledge 集合，避免與法規庫維度衝突"""
        if not chroma_path:
            return None
        try:
            import chromadb
            from chromadb.utils import embedding_functions
            client = chromadb.PersistentClient(path=chroma_path)
            # 固定使用 DefaultEmbeddingFunction（384 維），避免維度衝突
            emb_fn = embedding_functions.DefaultEmbeddingFunction()
            coll = client.get_or_create_collection(
                name="admin_knowledge",
                embedding_function=emb_fn
            )
            return coll
        except Exception as e:
            logger.warning(f"[GapManager] 知識庫初始化失敗: {e}")
            return None

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_gaps (
                    gap_id      TEXT PRIMARY KEY,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    user_id     TEXT,
                    trigger_query TEXT,
                    naomi_response TEXT,
                    gap_reason  TEXT,
                    admin_answer TEXT,
                    status      TEXT DEFAULT 'pending',
                    stored_in_vector INTEGER DEFAULT 0
                )
            """)
            conn.commit()

    def flag_gap(self, user_id: str, query: str, response: str, reason: str) -> str:
        """標記一個知識缺口"""
        gap_id = f"GAP-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{hashlib.md5(query.encode()).hexdigest()[:4]}"
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO knowledge_gaps
                    (gap_id, user_id, trigger_query, naomi_response, gap_reason)
                    VALUES (?, ?, ?, ?, ?)
                """, (gap_id, user_id, query[:500], response[:800], reason))
                conn.commit()
            logger.info(f"[GapManager] 缺口已標記: {gap_id} — {reason}")
        except Exception as e:
            logger.error(f"[GapManager] 標記失敗: {e}")
        return gap_id

    def answer_gap(self, gap_id: str, admin_answer: str) -> bool:
        """管理員回答缺口 → 存入知識庫"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT trigger_query FROM knowledge_gaps WHERE gap_id = ?", (gap_id,)
                ).fetchone()
            if not row:
                return False

            query = row[0]

            # 存入 ChromaDB
            if self.collection:
                try:
                    self.collection.add(
                        documents=[f"問題：{query}\n答案：{admin_answer}"],
                        ids=[gap_id],
                        metadatas=[{
                            "type": "admin_knowledge",
                            "query": query[:200],
                            "source": "admin_answer",
                            "timestamp": datetime.datetime.now().isoformat()
                        }]
                    )
                except Exception as e:
                    logger.error(f"[GapManager] ChromaDB 寫入失敗: {e}")

            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE knowledge_gaps
                    SET admin_answer = ?, status = 'answered', stored_in_vector = 1
                    WHERE gap_id = ?
                """, (admin_answer, gap_id))
                conn.commit()

            logger.info(f"[GapManager] 知識點已存入: {gap_id}")
            return True
        except Exception as e:
            logger.error(f"[GapManager] 存入知識點失敗: {e}")
            return False

    def dismiss_gap(self, gap_id: str) -> bool:
        """忽略一個缺口"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE knowledge_gaps SET status = 'dismissed' WHERE gap_id = ?", (gap_id,)
            )
            conn.commit()
        return True

    def get_pending_gaps(self, limit: int = 30) -> List[Dict]:
        """取得待審核的缺口清單"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT gap_id, detected_at, user_id, trigger_query, naomi_response, gap_reason
                FROM knowledge_gaps WHERE status = 'pending'
                ORDER BY detected_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [
            {
                "gap_id": r[0], "detected_at": r[1], "user_id": r[2],
                "query": r[3], "naomi_response": r[4], "reason": r[5]
            }
            for r in rows
        ]

    def search_knowledge(self, query: str, n_results: int = 3) -> List[str]:
        """搜尋知識庫（RAG 用）"""
        try:
            if not self.collection:
                return []
            count = self.collection.count()
            if count == 0:
                return []
            results = self.collection.query(
                query_texts=[query],
                n_results=min(n_results, count)
            )
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            # 只返回管理員審核過的知識
            return [
                doc for doc, meta in zip(docs, metas)
                if meta.get("type") == "admin_knowledge"
            ]
        except Exception as e:
            logger.error(f"[GapManager] 知識搜尋失敗: {e}")
            return []

    def get_stats(self) -> Dict:
        try:
            with sqlite3.connect(self.db_path) as conn:
                total   = conn.execute("SELECT COUNT(*) FROM knowledge_gaps").fetchone()[0]
                pending = conn.execute("SELECT COUNT(*) FROM knowledge_gaps WHERE status='pending'").fetchone()[0]
                answered= conn.execute("SELECT COUNT(*) FROM knowledge_gaps WHERE status='answered'").fetchone()[0]
            knowledge_count = self.collection.count() if self.collection else 0
            return {
                "total_gaps": total,
                "pending": pending,
                "answered": answered,
                "knowledge_points": knowledge_count
            }
        except Exception as e:
            return {"error": str(e)}


# ==============================================================================
# 6. KernelHub — 技能與工具容器
# ==============================================================================

class BaseSkill(ABC):
    """技能基類"""
    
    @staticmethod
    @abstractmethod
    def metadata() -> Dict:
        raise NotImplementedError
    
    @abstractmethod
    async def execute(self, user_id: str, data: Dict) -> Dict:
        raise NotImplementedError


class KernelHub:
    """核心中樞 — 技能與工具管理"""
    
    def __init__(self, chroma_path: str, async_groq=None):
        self.chroma_path = chroma_path
        self.async_groq = async_groq
        
        # 容器
        self.skills: Dict[str, Any] = {}
        self.tools: Dict[str, Any] = {}
        
        # 向量庫
        self._init_vector_db()
        
        # 載入技能
        self._load_skills()
        
        # 載入工具
        self._load_tools()
        
        logger.info(f"[KernelHub] 初始化完成，技能：{len(self.skills)}，工具：{len(self.tools)}")
    
    def _init_vector_db(self):
        """初始化向量庫"""
        try:
            import chromadb
            from chromadb.utils import embedding_functions

            self.vector_client = chromadb.PersistentClient(path=self.chroma_path)

            # 統一使用 DefaultEmbeddingFunction（384維）
            # 現有 taiwan_laws_precision 全部以 Default 存入，禁止換成 SentenceTransformer
            # 若要升級 SentenceTransformer，需先執行完整 re-index 腳本
            emb_fn = embedding_functions.DefaultEmbeddingFunction()

            self.law_collection = self.vector_client.get_or_create_collection(
                name="taiwan_laws_precision",
                embedding_function=emb_fn
            )
            logger.info(f"[KernelHub] 法規向量庫就緒，筆數：{self.law_collection.count()}")
            
        except Exception as e:
            logger.warning(f"[KernelHub] 向量庫初始化失敗：{e}")
            self.vector_client = None
            self.law_collection = None
    
    def _load_skills(self):
        """載入 skills/ 目錄下的技能"""
        if not SKILLS_DIR.exists():
            return
        
        for skill_dir in SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            if skill_dir.name.startswith("_") or skill_dir.name.startswith("."):
                continue
            
            # 尋找 skill.py 或 __init__.py
            skill_file = skill_dir / "skill.py"
            if not skill_file.exists():
                skill_file = skill_dir / "__init__.py"
            if not skill_file.exists():
                continue
            
            try:
                spec = importlib.util.spec_from_file_location(
                    f"skill_{skill_dir.name}", skill_file
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                
                # 尋找技能類別
                if hasattr(mod, 'Skill'):
                    self.skills[skill_dir.name] = mod.Skill()
                    logger.info(f"[KernelHub] ✅ 技能已載入：{skill_dir.name}")
                elif hasattr(mod, 'SKILL_META'):
                    self.skills[skill_dir.name] = mod
                    logger.info(f"[KernelHub] ✅ 技能模組已載入：{skill_dir.name}")
                    
            except Exception as e:
                logger.warning(f"[KernelHub] 技能載入失敗 {skill_dir.name}：{e}")
    
    def _load_tools(self):
        """載入 tools/ 目錄下的工具"""
        if not TOOLS_DIR.exists():
            return
        
        for py_file in TOOLS_DIR.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            
            try:
                import sys as _sys
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                mod = importlib.util.module_from_spec(spec)
                _sys.modules[py_file.stem] = mod   # 讓 @dataclass 能找到模組
                spec.loader.exec_module(mod)

                self.tools[py_file.stem] = mod
                logger.info(f"[KernelHub] ✅ 工具已載入：{py_file.stem}")

            except Exception as e:
                import sys as _sys
                _sys.modules.pop(py_file.stem, None)
                logger.warning(f"[KernelHub] 工具載入失敗 {py_file.name}：{e}")
    
    def get_tool(self, name: str):
        """取得工具"""
        return self.tools.get(name)
    
    def get_skill(self, name: str):
        """取得技能"""
        return self.skills.get(name)


# ==============================================================================
# 6. SquadManager — 智能體群管理
# ==============================================================================

class SquadManager:
    """智能體群管理器"""
    
    def __init__(self, hub: KernelHub, async_groq=None):
        self.hub = hub
        self.async_groq = async_groq
        self.squads: Dict[str, Dict] = {}
        
        # 載入所有智能體群
        self._load_all_squads()
        
        logger.info(f"[SquadManager] 就緒，已載入 {len(self.squads)} 個智能體群")
    
    def _load_all_squads(self):
        """載入 squads/ 目錄下的智能體群"""
        if not SQUADS_DIR.exists():
            logger.warning(f"[SquadManager] squads 目錄不存在：{SQUADS_DIR}")
            return
        
        for squad_dir in SQUADS_DIR.iterdir():
            if not squad_dir.is_dir():
                continue
            if squad_dir.name.startswith("_") or squad_dir.name.startswith("."):
                continue
            
            self._load_squad(squad_dir)
    
    def _load_squad(self, squad_dir: pathlib.Path):
        """載入單一智能體群"""
        squad_key = squad_dir.name
        squad_file = squad_dir / "squad.py"
        
        if not squad_file.exists():
            logger.warning(f"[SquadManager] 跳過 {squad_key}：找不到 squad.py")
            return
        
        try:
            spec = importlib.util.spec_from_file_location(f"squad_{squad_key}", squad_file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            
            # 建立 Squad 實例
            squad = None
            meta = {}
            
            if hasattr(mod, 'Squad'):
                squad = mod.Squad(self.hub, self.async_groq)
            elif hasattr(mod, 'create_squad'):
                squad = mod.create_squad(self.hub, self.async_groq)
            
            if squad:
                if hasattr(squad, 'get_squad_info'):
                    meta = squad.get_squad_info()
                elif hasattr(mod, 'SQUAD_INFO'):
                    meta = mod.SQUAD_INFO
                
                self.squads[squad_key] = {
                    "instance": squad,
                    "meta": meta,
                }
                
                # 登記觸發關鍵字
                triggers = meta.get("skills", meta.get("triggers", []))
                logger.info(f"[SquadManager] ✅ {squad_key} 已載入，觸發詞：{triggers[:5]}...")
            else:
                logger.warning(f"[SquadManager] {squad_key}：找不到 Squad 類別")
                
        except Exception as e:
            logger.error(f"[SquadManager] 載入 {squad_key} 失敗：{e}", exc_info=True)
    
    def get(self, squad_key: str):
        """取得智能體群"""
        squad = self.squads.get(squad_key)
        return squad["instance"] if squad else None
    
    async def dispatch(self, squad_key: str, user_id: str, task: str, context: Dict = None) -> Dict:
        """調度任務到智能體群"""
        squad = self.squads.get(squad_key)
        if not squad:
            return {"status": "error", "answer": f"找不到智能體群：{squad_key}"}
        
        instance = squad["instance"]
        
        try:
            if hasattr(instance, 'execute_async'):
                return await instance.execute_async(user_id, task, context or {})
            elif hasattr(instance, 'execute'):
                return instance.execute(user_id, task, context or {})
            elif hasattr(instance, 'boss_dispatch'):
                return await instance.boss_dispatch({
                    "user_id": user_id,
                    "description": task,
                    "context": context or {}
                })
            else:
                return {"status": "error", "answer": "智能體群沒有可用的執行方法"}
        except Exception as e:
            logger.error(f"[SquadManager] {squad_key} 執行失敗：{e}")
            return {"status": "error", "answer": str(e)}
    
    def get_squad_by_trigger(self, text: str) -> Optional[str]:
        """根據關鍵字找到對應的智能體群"""
        text_lower = text.lower()
        
        for key, squad in self.squads.items():
            meta = squad.get("meta", {})
            triggers = meta.get("skills", meta.get("triggers", []))
            
            for trigger in triggers:
                if trigger.lower() in text_lower or trigger in text:
                    return key
        
        return None


# ==============================================================================
# 7. BossAgent — 從獨立模組 import（CoT + DAG + Agent Loop + V3）
# ==============================================================================

from boss_agent import BossAgent as _BossAgentBase  # noqa: E402


class BossAgent(_BossAgentBase):
    """相容包裝：接受 main 傳入的 brain_manager 關鍵字，並呼叫完整 V3.1 建構式"""

    def __init__(self, hub, squad_manager, brain_manager=None, async_groq=None, db_path=None):
        super().__init__(
            hub=hub,
            squad_manager=squad_manager,
            db_path=db_path or str(DB_PATH),
            async_groq=async_groq,
            brain_manager=brain_manager,
        )

    # 所有邏輯已移至 boss_agent.py 的 BossAgent V3.1
    # 此處相容包裝僅負責初始化，其餘方法繼承自 _BossAgentBase


# ==============================================================================
# 8. SmartFileHandler — 智能檔案處理（含 PDF 入庫）
# ==============================================================================

class SmartFileHandler:
    """智能檔案處理 — PDF 自動入庫"""
    
    SUPPORTED_FORMATS = {
        '.pdf': 'PDF', '.md': 'Markdown', '.txt': '文字檔',
        '.png': '圖片', '.jpg': '圖片', '.jpeg': '圖片',
        '.xlsx': 'Excel', '.docx': 'Word', '.dwg': 'CAD',
    }
    
    # 文件分類 Schema：定義每種類型的語意描述與對應作業
    DOCUMENT_SCHEMA = {
        "法規": {
            "squad":       "03_regulatory_intel",
            "action":      "入庫到法規資料庫",
            "description": "政府頒布的法律、條例、辦法、準則、規則，含條號體裁（第X條），"
                           "中央或地方法規均屬此類（如建築法、室內裝修管理辦法、都市計畫法）。"
                           "【重要】都市計畫書、土地使用分區管制規則、土管辦法、建管辦法、"
                           "使用管制要點、解釋令、函釋，即使是公告格式，只要含有條文式規定（第X條、"
                           "第X項、附表），均屬此類，不是行政文書",
        },
        "建築設計文書": {
            "squad":       "04_architectural_design",
            "action":      "交由建築設計群分析",
            "description": "建築計畫書、設計圖說、使照申請書、建蔽率容積率計算書、基地配置說明",
        },
        "室內裝修文書": {
            "squad":       "05_interior_design",
            "action":      "交由室內設計群分析",
            "description": "室內裝修設計圖說、材料規格說明書、裝修申請書、天花地坪隔間計畫、"
                           "室內報價單、室內工程估價、裝潢報價、室內施工報價、室內設計費用明細。"
                           "只要是針對室內裝修/裝潢工程的報價或費用文件，均屬此類",
        },
        "地籍資料": {
            "squad":       None,
            "action":      "儲存為地籍參考（非法規，不入法規庫）",
            "description": "土地登記謄本、建物謄本、地籍圖謄本、所有權狀、土地標示部、所有權部",
        },
        "審查意見書": {
            "squad":       "04_architectural_design",
            "action":      "AI 分析審查意見 + 建議回覆",
            "description": "建照或室裝送審後，審查單位退件的審查意見書、補正通知、審查紀錄、"
                           "會審意見表。內含審查委員逐條列出的修正意見、引用法條、補正要求。"
                           "【重要】若文件包含「審查意見」「補正通知」「退件」「審查紀錄」「會審」"
                           "等字眼，且含有逐條列舉的修正要求，即屬此類",
        },
        "行政文書": {
            "squad":       "09_integrated_admin",
            "action":      "交由行政群處理",
            "description": "公文、合約書、會議記錄、函文、簽呈、工作報告",
        },
        "財務報表": {
            "squad":       "11_financial_mgmt",
            "action":      "交由財務群分析",
            "description": "整體工程造價總表、投資分析報告、財務試算表、ROI試算、公設比計算、"
                           "收支預算表。注意：如果是室內裝修或裝潢的報價單，應歸類為「室內裝修文書」，"
                           "財務報表僅指整體建案或公司層級的財務文件",
        },
        "其他": {
            "squad":       None,
            "action":      "暫存，待使用者指示",
            "description": "上述類型以外的文件",
        },
    }
    
    # 儲存上傳檔案的暫存目錄
    _TEMP_DIR = pathlib.Path(r"C:\Users\User\My_AI_Agent\temp_input")

    def __init__(self, brain_manager: BrainManager, squad_manager: SquadManager = None):
        self.brain = brain_manager
        self.squad_manager = squad_manager
        self._squad03 = None
        self._TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def _get_squad03(self):
        """取得 Squad03"""
        if self._squad03 is None and self.squad_manager:
            self._squad03 = self.squad_manager.get("03_regulatory_intel")
        return self._squad03

    def _save_land_record_db(
        self,
        user_id: str,
        file_name: str,
        file_path: str,
        land_data: dict,
        xlsx_path: str,
    ):
        """將謄本解析結果寫入 naomi_main.db → land_records（供比對查詢）"""
        import json as _json, datetime as _dt
        try:
            raw_parcels = land_data.get("parcels") or []
            parcel_nos  = [
                p["parcel_no"] if isinstance(p, dict) else str(p)
                for p in raw_parcels
            ]
            now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("""
                    INSERT INTO land_records
                        (user_id, file_name, city, district, section,
                         parcels, area_m2, land_use_zone, doc_type,
                         confidence, xlsx_path, file_path, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    user_id,
                    file_name,
                    land_data.get("city", ""),
                    land_data.get("district", ""),
                    land_data.get("section", ""),
                    _json.dumps(parcel_nos, ensure_ascii=False),
                    land_data.get("site_area_total", 0),
                    land_data.get("land_use_zone", ""),
                    land_data.get("doc_type", ""),
                    land_data.get("confidence", ""),
                    xlsx_path,
                    file_path,
                    now_str,
                ))
                conn.commit()
            logger.info(f"[FileHandler] land_records 已入庫：{file_name} parcels={parcel_nos}")
        except Exception as _e:
            logger.error(f"[FileHandler] land_records 入庫失敗：{_e}")

    # ── 案件記憶層 ────────────────────────────────────────────────────────────

    def _find_matching_case(self, user_id: str, city: str, district: str,
                             section: str, parcels: list) -> Optional[str]:
        """比對現有案件：city+district+section+任一地號吻合 → 回傳 case_id，否則 None"""
        if not (city or district or section or parcels):
            return None
        try:
            with sqlite3.connect(str(DB_PATH)) as conn:
                rows = conn.execute(
                    "SELECT case_id, city, district, section, parcels "
                    "FROM cases WHERE user_id=? AND status='active'",
                    (user_id,)
                ).fetchall()
            import json as _json
            for case_id, c_city, c_dist, c_sec, c_parcels_raw in rows:
                # 地段吻合
                loc_match = (
                    (city and c_city and city == c_city) or
                    (district and c_dist and district == c_dist)
                )
                sec_match = (section and c_sec and section == c_sec)
                # 任一地號吻合
                try:
                    existing = set(_json.loads(c_parcels_raw or "[]"))
                except Exception:
                    existing = set()
                parcel_match = bool(existing & set(parcels)) if parcels else False

                if (loc_match or sec_match) and (parcel_match or sec_match):
                    return case_id
        except Exception as e:
            logger.warning(f"[CaseMem] 案件比對失敗：{e}")
        return None

    def _create_case(self, user_id: str, city: str, district: str,
                     section: str, parcels: list, address: str = "") -> str:
        """建立新案件，回傳 case_id"""
        import uuid, json as _json, datetime as _dt
        case_id  = f"case_{uuid.uuid4().hex[:8]}"
        now_str  = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 案件名稱：縣市+地段+地號
        parcel_str = "、".join(parcels[:2]) if parcels else ""
        case_name  = f"{city}{district}{section}{parcel_str}".strip() or f"未命名案件_{case_id[-4:]}"
        try:
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("""
                    INSERT INTO cases (case_id, user_id, case_name, city, district,
                                       section, parcels, address, status, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (case_id, user_id, case_name, city, district,
                      section, _json.dumps(parcels, ensure_ascii=False),
                      address, "active", now_str, now_str))
                conn.commit()
            logger.info(f"[CaseMem] 新案件建立：{case_id} {case_name}")
        except Exception as e:
            logger.error(f"[CaseMem] 建立案件失敗：{e}")
        return case_id

    def _save_case_document(self, case_id: str, user_id: str, file_name: str,
                             doc_type: str, doc_type_zh: str,
                             fields: dict, confidence: str, saved_path: str):
        """將文件記憶寫入 case_documents"""
        import json as _json, datetime as _dt
        now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("""
                    INSERT INTO case_documents
                        (case_id, user_id, file_name, doc_type, doc_type_zh,
                         fields_json, confidence, saved_path, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (case_id, user_id, file_name, doc_type, doc_type_zh,
                      _json.dumps(fields or {}, ensure_ascii=False),
                      confidence, saved_path, now_str))
                conn.commit()
            logger.info(f"[CaseMem] 文件入庫：case={case_id} file={file_name} type={doc_type}")
        except Exception as e:
            logger.error(f"[CaseMem] 文件記憶寫入失敗：{e}")

    async def _async_push_admin_alert(self, message: str):
        await self._push_admin_alert(message)

    async def _push_admin_alert(self, message: str):
        """推播警示訊息給所有管理員 LINE 帳號"""
        if not LINE_ACCESS_TOKEN or not ADMIN_LINE_USER_IDS:
            logger.info(f"[FileHandler] Admin alert (LINE未設定)：{message[:100]}")
            return
        try:
            cfg = Configuration(access_token=LINE_ACCESS_TOKEN)
            async with ApiClient(cfg) as api_client:
                api = MessagingApi(api_client)
                for admin_id in ADMIN_LINE_USER_IDS:
                    try:
                        api.push_message(PushMessageRequest(
                            to=admin_id,
                            messages=[TextMessage(text=message[:4990])]
                        ))
                    except Exception as _e:
                        logger.warning(f"[FileHandler] 管理員推播失敗 {admin_id}：{_e}")
        except Exception as _e:
            logger.warning(f"[FileHandler] _push_admin_alert 初始化失敗：{_e}")

    def _save_temp(self, file_name: str, file_bytes: bytes) -> pathlib.Path:
        """儲存到暫存目錄，回傳路徑"""
        path = self._TEMP_DIR / file_name
        path.write_bytes(file_bytes)
        return path

    def _ocr_analyze(self, file_path: pathlib.Path) -> Dict:
        """呼叫通用 OCR 模組解析文件，回傳結構化欄位 dict"""
        try:
            import sys
            _script_dir = str(pathlib.Path(__file__).parent /
                              "squads" / "04_architectural_design" / "scripts")
            if _script_dir not in sys.path:
                sys.path.insert(0, _script_dir)
            from document_ocr import parse_document
            return parse_document(str(file_path))
        except Exception as e:
            logger.warning(f"[OCR] 解析失敗：{e}")
            return {}
    
    async def handle(self, file_name: str, file_bytes: bytes, user_id: str = None) -> Dict:
        """處理上傳的檔案 — 法規文件直接入庫，其他文件存入 context 供後續對話"""
        ext = pathlib.Path(file_name).suffix.lower()

        if ext not in self.SUPPORTED_FORMATS:
            return {
                "success": False,
                "message": f"這個格式（{ext}）目前不支援，可用格式：{', '.join(self.SUPPORTED_FORMATS.keys())}"
            }

        # ── 地籍圖影像快速通道（PNG/JPG → OCR 解析地號 → 確認後查都市計畫）────
        if ext in (".png", ".jpg", ".jpeg"):
            try:
                from utils.cadastral_image_parser import CadastralImageParser
                parser = CadastralImageParser()
                cad = await parser.parse(file_bytes, file_name)
                if cad.ok:
                    # 組確認訊息
                    summary = cad.summary()
                    city_dispatcher = "台北市" if "台北市" in cad.city else (
                        "新北市" if "新北市" in cad.city else cad.city
                    )
                    msg = (
                        f"📍 識別到地籍圖資訊：\n"
                        f"  城市：{cad.city or '未識別'}\n"
                        f"  行政區：{cad.district or '未識別'}\n"
                        f"  地段：{cad.section or '未識別'}\n"
                        f"  地號：{'、'.join(cad.parcel_nos[:8]) if cad.parcel_nos else '未識別'}"
                        + (f" 等{len(cad.parcel_nos)}筆" if len(cad.parcel_nos) > 8 else "") + "\n\n"
                        f"信心：{'高' if cad.confidence=='high' else ('中' if cad.confidence=='medium' else '低')}\n\n"
                        f"確認後我將查詢都市計畫分區資料（{city_dispatcher}），請回覆「確認」或「修正」。"
                    )
                    return {
                        "success":        True,
                        "message":        msg,
                        "pending_action": True,
                        "pending_type":   "query_urban_plan",
                        "doc_type":       "地籍圖",
                        "file_name":      file_name,
                        "cadastral_parse": {
                            "city":       cad.city,
                            "district":   cad.district,
                            "section":    cad.section,
                            "parcel_nos": cad.parcel_nos,
                            "confidence": cad.confidence,
                        },
                    }
                else:
                    logger.info(f"[FileHandler] 地籍圖解析信心不足（{cad.error}），改走 LLM 分類")
            except Exception as _ce:
                logger.warning(f"[FileHandler] 地籍圖 OCR 失敗：{_ce}")

        content, info = self._read_file(ext, file_bytes)

        if info.get("error"):
            return {"success": False, "message": f"檔案讀取失敗：{info['error']}"}

        chars = info.get("char_count", 0)

        # LLM 分類：理解文件內容後決定路由，不用關鍵字
        classification = await self._classify_document(content, file_name)
        doc_type   = classification["type"]
        confidence = classification["confidence"]
        reason     = classification["reason"]
        squad_key  = classification["squad"]
        action     = classification["action"]

        logger.info(f"[FileHandler] 分類結果：{doc_type}（信心={confidence:.2f}）原因：{reason}")

        # 信心不足（< 0.7）→ 告知分類結果，請使用者確認後再執行
        if confidence < 0.7:
            return {
                "success": True,
                "message": (
                    f"📄 收到「{file_name}」\n"
                    f"我判斷這是【{doc_type}】（信心 {int(confidence*100)}%），{reason}\n\n"
                    f"建議作業：{action}\n\n"
                    f"請確認後告訴我：「對，{action}」或「不對，這是＿＿」"
                ),
                "pending_action": True,
                "classification": classification,
                "content": content[:5000],
                "file_name": file_name,
            }

        # 法規 → Squad03 入庫
        if doc_type == "法規":
            squad03 = self._get_squad03()
            if ext == '.pdf' and squad03 and hasattr(squad03, 'ingest_pdf'):
                try:
                    result = await squad03.ingest_pdf(file_bytes, file_name)
                    if result.get("status") == "success":
                        answer = result.get("answer", "")
                        # 若 squad03 已回傳詳細訊息則直接使用，否則 fallback 簡短訊息
                        msg = answer if answer else f"✅ 收到，「{file_name}」已入庫到法規資料庫。\n（{reason}）"
                        return {"success": True, "message": msg}
                except Exception as e:
                    logger.warning(f"[FileHandler] 法規 PDF 入庫失敗：{e}")
            if ext in ('.md', '.txt'):
                try:
                    law_lib = pathlib.Path(r"C:\Users\User\My_AI_Agent\law_library")
                    stem = pathlib.Path(file_name).stem
                    save_dir = law_lib / "中央法規"
                    save_dir.mkdir(parents=True, exist_ok=True)
                    (save_dir / f"{stem}.md").write_bytes(file_bytes)
                    return {"success": True,
                            "message": f"✅ 收到，「{file_name}」已入庫（{chars} 字）。\n（{reason}）"}
                except Exception as e:
                    logger.warning(f"[FileHandler] 法規 MD 入庫失敗：{e}")
            # Fallback pending
            try:
                (pathlib.Path(r"C:\Users\User\My_AI_Agent\pending_laws") / file_name).write_bytes(file_bytes)
            except Exception as _pe:
                logger.error(f"[FileHandler] 法規暫存失敗：{_pe}")
            return {"success": True, "message": f"「{file_name}」暫存完成，說「入庫」可正式處理。"}

        # ── 建築設計文書 / 室內裝修文書：圖說 OCR 解析 ──────────────────────────
        if doc_type in ("建築設計文書", "室內裝修文書") and ext == ".pdf":
            try:
                import sys as _sys, tempfile as _tmp, os as _os2
                _sd = str(pathlib.Path(__file__).parent / "squads" / "04_architectural_design" / "scripts")
                if _sd not in _sys.path:
                    _sys.path.insert(0, _sd)
                from drawing_ocr import parse_drawing

                # 暫存 PDF
                with _tmp.NamedTemporaryFile(suffix=".pdf", delete=False) as _tf:
                    _tf.write(file_bytes)
                    _tmp_path = _tf.name
                try:
                    drawing_data = await parse_drawing(
                        _tmp_path,
                        groq_client=self.brain.groq if hasattr(self.brain, "groq") else None,
                        hub=self._hub if hasattr(self, "_hub") else None,
                    )
                finally:
                    try:
                        _os2.unlink(_tmp_path)
                    except Exception:
                        pass

                _summary = drawing_data.get("summary", "")
                _score   = drawing_data.get("reflection", {}).get("score", 0)
                _conf    = drawing_data.get("confidence", "low")
                _dtype   = "建築圖說" if doc_type == "建築設計文書" else "室內裝修圖說"

                msg = (
                    f"📐 收到{_dtype}「{file_name}」\n\n"
                    f"{_summary}\n\n"
                )
                if _score < 6.5:
                    msg += "⚠️ 完整性評分偏低，建議補充缺少欄位後重新上傳。\n\n"

                msg += (
                    f"可以繼續：\n"
                    f"• 說「合規檢討」→ 法規條文對照\n"
                    f"• 說「配置平面圖」→ 疊合地籍邊界（需先提供地籍資料）\n"
                    f"• 說「室內比對」→ 各層空間一致性驗證\n"
                    f"• 說「學習」→ 加入案例庫（供未來相似案子參考）"
                )

                # 把解析結果存入 file_context 供後續使用
                return {
                    "success":        True,
                    "message":        msg,
                    "pending_action": True,
                    "pending_type":   "drawing_review",
                    "doc_type":       doc_type,
                    "file_name":      file_name,
                    "drawing_data":   drawing_data,
                    "_raw_bytes":     file_bytes,   # 供案例學習用
                }
            except Exception as _de:
                logger.warning(f"[FileHandler] 圖說 OCR 失敗，改走一般流程：{_de}")
                # fallthrough 到下方一般 pending 流程

        # ── 審查意見書：OCR + 結構化分析 + AI 建議回覆 ───────────────────────────
        if doc_type == "審查意見書" and ext == ".pdf":
            try:
                import sys as _sys
                _sd = str(pathlib.Path(__file__).parent / "squads" / "04_architectural_design" / "scripts")
                if _sd not in _sys.path:
                    _sys.path.insert(0, _sd)
                from review_comment_analyzer import analyze_review_comments

                # 暫存 PDF
                tmp = self._save_temp(file_name, file_bytes)
                # hub 物件：嘗試取得（供 LLM 建議回覆用）
                _hub = getattr(self, "_hub", None)
                if _hub is None and hasattr(self.brain, "hub"):
                    _hub = self.brain.hub

                review_result = await analyze_review_comments(
                    str(tmp), hub=_hub, export_excel=True,
                )

                if review_result.get("success"):
                    _summary = review_result["summary_message"]
                    _count = review_result["item_count"]
                    _stats = review_result.get("severity_stats", {})
                    _xlsx = review_result.get("excel_path", "")

                    msg = f"📋 收到審查意見書「{file_name}」\n\n{_summary}"

                    return {
                        "success":        True,
                        "message":        msg,
                        "pending_action": True,
                        "pending_type":   "review_comment_analysis",
                        "doc_type":       doc_type,
                        "file_name":      file_name,
                        "review_data":    review_result,
                        "excel_path":     _xlsx,
                    }
                else:
                    logger.warning(f"[FileHandler] 審查意見分析失敗：{review_result.get('summary_message')}")
                    # fallthrough 到下方一般 pending 流程
            except Exception as _re:
                logger.warning(f"[FileHandler] 審查意見分析異常，改走一般流程：{_re}")

        # ── 地籍資料：土地謄本 / 建物謄本 / 地籍圖謄本 ─────────────────────────
        # 規則：
        #   土地登記謄本 / 建物登記謄本 → OCR 三段式解析 + Excel 後台存檔（靜默）
        #   地籍圖謄本                  → OCR + DXF 放樣圖（回傳用戶下載連結）
        # Excel 僅供後台比對，不傳下載連結給用戶。
        if doc_type == "地籍資料":
            cadastral_dir = pathlib.Path(BASE_DIR) / "storage" / "cadastral"
            try:
                cadastral_dir.mkdir(parents=True, exist_ok=True)
                (cadastral_dir / file_name).write_bytes(file_bytes)
            except Exception as _ce:
                logger.error(f"[FileHandler] 地籍資料儲存失敗：{_ce}")

            tmp = self._save_temp(file_name, file_bytes)
            land_result: Dict = {}
            xlsx_path   = ""
            ocr_doc_type = ""   # "land_registry" / "building_registry" / "cadastral"
            try:
                import sys as _sys
                _sd = str(pathlib.Path(__file__).parent / "squads" / "04_architectural_design" / "scripts")
                if _sd not in _sys.path:
                    _sys.path.insert(0, _sd)
                from land_registry_ocr import parse_and_prepare
                land_result  = parse_and_prepare(str(tmp), auto_file=True)
                filing       = land_result.get("filing") or {}
                xlsx_path    = filing.get("xlsx_path", "")
                ocr_doc_type = (land_result.get("data") or {}).get("doc_type", "")
                _conf = (land_result.get("data") or {}).get("confidence", "")
                if _conf == "low":
                    logger.warning(f"[FileHandler] 謄本解析信心低 user={user_id} file={file_name}")
                    _warns = (land_result.get("data") or {}).get("warnings", [])
                    _fire_and_forget(self._push_admin_alert(
                        f"⚠️ 謄本低信心：{file_name}\nuser={user_id}\n{'; '.join(_warns[:3])}"
                    ))
                if filing.get("status") == "rejected":
                    logger.info(f"[FileHandler] 謄本未建檔：{filing.get('reason')} user={user_id}")
                else:
                    logger.info(f"[FileHandler] 謄本 Excel 建檔：{xlsx_path} ({ocr_doc_type})")
            except Exception as _e:
                logger.warning(f"[FileHandler] 謄本解析失敗：{_e}")

            # DXF 只有地籍圖謄本才轉換，使用 extract_cadastral_dxf（直接匯出線段）
            # 注意：不用 convert()，那個走 polygonize 對地籍圖謄本無效
            dxf_path    = ""
            area_m2_cad = 0.0
            if ocr_doc_type == "cadastral" or (not ocr_doc_type and "地籍圖" in file_name):
                try:
                    from utils.pdf_to_cad import PDFToCAD
                    _parcel_nos = [
                        p["parcel_no"] if isinstance(p, dict) else str(p)
                        for p in (land_result.get("data") or {}).get("parcels") or []
                    ]
                    _cad_result = PDFToCAD(output_dir=str(cadastral_dir)).extract_cadastral_dxf(
                        pdf_path=str(tmp),
                        parcel_nos=_parcel_nos,
                    )
                    if "error" not in _cad_result:
                        dxf_path    = _cad_result.get("dxf_path", "")
                        area_m2_cad = _cad_result.get("area_m2_registered", 0.0)
                        logger.info(f"[FileHandler] DXF 就緒：{dxf_path} lines={_cad_result.get('total_lines',0)}")
                    else:
                        logger.info(f"[FileHandler] DXF：{_cad_result['error']}")
                except Exception as _cad_e:
                    logger.warning(f"[FileHandler] DXF 轉換失敗：{_cad_e}")

            # 永久存檔（PDF 檔案）
            _land_city = (land_result.get("data") or {}).get("city", "") or self._detect_city(content)
            _saved_path = self._save_document(file_bytes, file_name, "地籍資料", _land_city, user_id)

            # 寫入 SQLite land_records（無論解析成功或部分成功都入庫）
            self._save_land_record_db(
                user_id   = user_id or "",
                file_name = file_name,
                file_path = _saved_path,
                land_data = land_result.get("data") or {},
                xlsx_path = xlsx_path,
            )

            # ── 案件記憶：識別 → 比對 → 自動掛案或建立新案件 ─────────────────
            _ld     = land_result.get("data") or {}
            _city2  = _ld.get("city", "") or _land_city
            _dist2  = _ld.get("district", "")
            _sec2   = _ld.get("section", "")
            _parcel_list = [
                p["parcel_no"] if isinstance(p, dict) else str(p)
                for p in (_ld.get("parcels") or [])
            ]
            _case_id = self._find_matching_case(user_id or "", _city2, _dist2, _sec2, _parcel_list)
            _case_new = False
            if not _case_id:
                _case_id  = self._create_case(user_id or "", _city2, _dist2, _sec2,
                                               _parcel_list, _ld.get("address", ""))
                _case_new = True
            self._save_case_document(
                case_id    = _case_id,
                user_id    = user_id or "",
                file_name  = file_name,
                doc_type   = "地籍資料",
                doc_type_zh= "土地登記謄本" if ocr_doc_type == "land_registry" else (
                              "建物登記謄本" if ocr_doc_type == "building_registry" else "地籍資料"),
                fields     = _ld,
                confidence = _ld.get("confidence", ""),
                saved_path = _saved_path,
            )
            logger.info(f"[CaseMem] 謄本掛案：case={_case_id} new={_case_new}")

            ready = land_result.get("ready_for_calc", False)
            return {
                "success":        True,
                "message":        "",          # _agent_file_response 完全由 LLM 生成
                "pending_action": True,
                "pending_type":   "calc_far",
                "land_data":      land_result.get("data", {}),
                "xlsx_path":      xlsx_path,   # 後台存檔路徑，不傳給用戶
                "ready_for_calc": ready,
                "doc_type":       doc_type,
                "ocr_doc_type":   ocr_doc_type,
                "file_name":      file_name,
                "dxf_path":       dxf_path,
                "area_m2_cad":    area_m2_cad,
            }

        # 其他有對應 squad 的文件 → OCR 解析 + 永久存檔 + 告知使用者確認
        if squad_key:
            tmp = self._save_temp(file_name, file_bytes)
            ocr = self._ocr_analyze(tmp)
            ocr_msg = ocr.get("summary_message", "")
            fields  = ocr.get("fields", {})

            # 永久存檔到 storage/documents/{city}/{doc_type}/
            _city = self._detect_city(content)
            _saved_path = self._save_document(file_bytes, file_name, doc_type, _city, user_id)

            # ── 案件記憶：從 OCR 欄位萃取識別資訊 → 比對 / 建立案件 ──────────
            _f_city    = fields.get("city", "") or _city
            _f_dist    = fields.get("district", "")
            _f_sec     = fields.get("section", "")
            _f_parcels = fields.get("parcel_nos") or fields.get("parcels") or []
            if isinstance(_f_parcels, str):
                import json as _fj
                try: _f_parcels = _fj.loads(_f_parcels)
                except: _f_parcels = [_f_parcels]
            _case_id2 = self._find_matching_case(user_id or "", _f_city, _f_dist, _f_sec, _f_parcels)
            if not _case_id2 and (_f_city or _f_dist or _f_parcels):
                _case_id2 = self._create_case(user_id or "", _f_city, _f_dist, _f_sec,
                                               _f_parcels, fields.get("address", ""))
            if _case_id2:
                self._save_case_document(
                    case_id    = _case_id2,
                    user_id    = user_id or "",
                    file_name  = file_name,
                    doc_type   = doc_type,
                    doc_type_zh= doc_type,
                    fields     = fields,
                    confidence = ocr.get("confidence", ""),
                    saved_path = _saved_path,
                )
                logger.info(f"[CaseMem] 文件掛案：case={_case_id2} type={doc_type}")

            base_msg = (
                f"收到「{file_name}」\n"
                f"判斷為【{doc_type}】（{reason}）\n"
                f"建議：{action}\n"
                + (f"已存檔：{_city} / {doc_type}\n" if _saved_path else "")
            )
            if ocr_msg and ocr.get("confidence") in ("high", "medium"):
                full_msg = base_msg + "\n" + ocr_msg
            else:
                full_msg = base_msg + "\n要我現在交給對應的協作群處理嗎？"

            return {
                "success":        True,
                "message":        full_msg,
                "pending_action": True,
                "classification": classification,
                "content":        content[:5000],
                "file_name":      file_name,
                "ocr_fields":     fields,
                "saved_path":     _saved_path,
            }

        # 無法分類 → 永久存檔（未分類城市/其他）+ 存 context 讓使用者決定
        _city = self._detect_city(content)
        self._save_document(file_bytes, file_name, "其他", _city, user_id)
        return {
            "success": True,
            "message": (
                f"收到「{file_name}」（{chars} 字）\n"
                f"類型：{doc_type}。{reason}\n\n要我幫你做什麼？"
            ),
            "pending_action": True,
            "content": content[:5000],
            "file_name": file_name,
        }
    
    # 城市白名單（偵測文件所屬縣市）
    _CITY_RE = re.compile(
        r"(臺北市|台北市|新北市|桃園市|臺中市|台中市|臺南市|台南市|高雄市|"
        r"基隆市|新竹市|嘉義市|新竹縣|苗栗縣|彰化縣|南投縣|雲林縣|嘉義縣|"
        r"屏東縣|宜蘭縣|花蓮縣|臺東縣|台東縣|澎湖縣|金門縣|連江縣)"
    )
    # 文件類型對應存檔子目錄名（避免中文路徑問題時可改英文，這裡保持繁中）
    _DOC_STORAGE_ROOT = pathlib.Path(r"C:\Users\User\My_AI_Agent\storage\documents")

    def _detect_city(self, content: str) -> str:
        """從文件內容掃描城市名稱，找不到回傳「未分類城市」"""
        if not content:
            return "未分類城市"
        # 只掃前 3000 字（標題頁通常有城市名）
        m = self._CITY_RE.search(content[:3000])
        if m:
            # 統一台/臺
            city = m.group(1).replace("台北市", "臺北市").replace(
                "台中市", "臺中市").replace("台南市", "臺南市").replace(
                "台東縣", "臺東縣")
            return city
        return "未分類城市"

    def _save_document(
        self,
        file_bytes: bytes,
        file_name: str,
        doc_type: str,
        city: str = "",
        user_id: str = "",
    ) -> str:
        """
        永久儲存非法規文件到 storage/documents/{city}/{doc_type}/
        回傳實際存檔路徑（失敗回傳空字串）
        """
        import datetime as _dt
        city = city or "未分類城市"
        # 路徑安全：去除斜線
        safe_type = doc_type.replace("/", "_").replace("\\", "_")
        save_dir = self._DOC_STORAGE_ROOT / city / safe_type
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            # 加日期前綴避免同名覆蓋
            date_prefix = _dt.date.today().strftime("%Y%m%d")
            save_name = f"{date_prefix}_{file_name}"
            save_path = save_dir / save_name
            # 若同名已存在則加序號
            if save_path.exists():
                stem = pathlib.Path(file_name).stem
                ext  = pathlib.Path(file_name).suffix
                for i in range(2, 100):
                    save_path = save_dir / f"{date_prefix}_{stem}_{i}{ext}"
                    if not save_path.exists():
                        break
            save_path.write_bytes(file_bytes)
            logger.info(f"[FileHandler] 文件永久存檔：{save_path}")
            return str(save_path)
        except Exception as e:
            logger.error(f"[FileHandler] 文件存檔失敗：{e}")
            return ""

    # 法規類檔名快速識別詞（優先於 LLM，避免誤判為行政文書）
    _LAW_FILENAME_KEYWORDS = re.compile(
        r"(建築法|建築技術規則|室內裝修管理辦法|都市計畫|土地使用分區|土地使用管制|"
        r"土管|建管|使用管制要點|分區管制|區域計畫|法規|辦法|條例|自治條例|準則|"
        r"規則|管理規定|建照|使照|解釋令|函釋|公告.*法|法.*公告)"
    )
    # 法規類內容快速識別（條文格式）
    _LAW_CONTENT_PATTERN = re.compile(r"第\s*[一二三四五六七八九十百千\d]+\s*條")

    async def _classify_document(self, content: str, file_name: str) -> Dict:
        """使用 LLM 真正理解文件內容後分類，不依賴關鍵字規則。
        回傳：{"type": str, "confidence": float, "reason": str,
               "squad": str|None, "action": str}
        """
        # ── 快速規則 0：地籍資料優先（謄本含第X條是引用法條，非法規本身）──────
        fn_lower = file_name  # 不 lower，中文不受影響
        content_sample = (content or "")[:3000]
        _CADASTRAL_FN_KW = re.compile(
            r"謄本|地籍圖|土地登記|建物登記|所有權狀|標示部|所有權部|他項權利部"
        )
        _CADASTRAL_CONTENT_KW = re.compile(
            r"土地登記謄本|建物登記謄本|地籍圖謄本|標示部|所有權部|他項權利部"
            r"|土地坐落|地號.*面積|建號.*建物"
        )
        if (_CADASTRAL_FN_KW.search(fn_lower) or
                _CADASTRAL_CONTENT_KW.search(content_sample)):
            schema = self.DOCUMENT_SCHEMA["地籍資料"]
            reason = (
                "檔名含謄本/地籍關鍵字" if _CADASTRAL_FN_KW.search(fn_lower)
                else "內容含土地登記謄本/地籍特徵"
            )
            logger.info(f"[FileHandler] 快速規則判定：地籍資料（{reason}）")
            return {
                "type": "地籍資料", "confidence": 0.95, "reason": reason,
                "squad": schema["squad"], "action": schema["action"],
            }

        # ── 快速規則：檔名或內容含法規特徵 → 直接判法規，不送 LLM ────────────
        is_law_filename = bool(self._LAW_FILENAME_KEYWORDS.search(fn_lower))
        article_count = len(self._LAW_CONTENT_PATTERN.findall(content_sample))
        if is_law_filename or article_count >= 3:
            schema = self.DOCUMENT_SCHEMA["法規"]
            reason = (
                f"檔名含法規關鍵字" if is_law_filename
                else f"內容含 {article_count} 條條文格式"
            )
            logger.info(f"[FileHandler] 快速規則判定：法規（{reason}）")
            return {
                "type": "法規", "confidence": 0.95, "reason": reason,
                "squad": schema["squad"], "action": schema["action"],
            }

        schema_desc = "\n".join(
            f'- "{k}"：{v["description"]}'
            for k, v in self.DOCUMENT_SCHEMA.items()
        )
        preview = (content or "（無可讀文字內容，請根據檔名判斷）")[:2500]

        prompt = (
            "你是專業的建築事務所文件分類員。\n"
            "請根據以下文件的【實際內容】判斷類型，重點是理解文件在說什麼，而非只看檔名。\n"
            "【重要判斷原則】\n"
            "- 含「第X條」「第X項」條文格式的政府文件 → 一律歸「法規」，不是「行政文書」\n"
            "- 都市計畫書、土地使用分區管制規則 → 「法規」\n"
            "- 都市計畫使用分區證明（1~3頁，針對特定地號）→ 「地籍資料」\n"
            "- 「行政文書」僅限：公文、合約、會議記錄、函文、工作報告（無條文格式）\n\n"
            f"【檔案名稱】{file_name}\n\n"
            f"【文件內容預覽】\n{preview}\n\n"
            f"【分類選項】\n{schema_desc}\n\n"
            "判斷後只回傳 JSON，格式固定如下（不要加其他文字）：\n"
            '{"type":"類型名稱","confidence":0.95,"reason":"判斷依據30字內"}'
        )

        try:
            result = await self.brain.call(
                BrainRole.ANALYST,
                [{"role": "user", "content": prompt}],
                max_tokens=120,
            )
            import json as _json
            data = _json.loads(result.get("content", "{}"))
            doc_type = data.get("type", "其他")
            if doc_type not in self.DOCUMENT_SCHEMA:
                doc_type = "其他"
            schema = self.DOCUMENT_SCHEMA[doc_type]
            return {
                "type":       doc_type,
                "confidence": float(data.get("confidence", 0.5)),
                "reason":     data.get("reason", ""),
                "squad":      schema["squad"],
                "action":     schema["action"],
            }
        except Exception as e:
            logger.warning(f"[FileHandler] LLM 分類失敗：{e}")
            return {
                "type": "其他", "confidence": 0.0,
                "reason": "LLM 不可用",
                "squad": None, "action": "暫存，待使用者指示",
            }
    
    async def _ingest_law_pdf(self, file_name: str, file_bytes: bytes, 
                              info: Dict, user_id: str) -> Dict:
        """入庫法規 PDF"""
        squad03 = self._get_squad03()
        
        if squad03 and hasattr(squad03, 'ingest_pdf'):
            result = await squad03.ingest_pdf(file_bytes, file_name)
            return {
                "success": result.get("status") == "success",
                "message": result.get("answer", "入庫完成")
            }
        
        # Fallback：存到 pending_laws/
        save_path = PENDING_LAWS_DIR / file_name
        save_path.write_bytes(file_bytes)
        
        return {
            "success": True,
            "message": (
                f"收到法規文件：{file_name}\n"
                f"共 {info.get('pages', '?')} 頁\n\n"
                f"已存到暫存區，稍後說「入庫」即可處理。"
            )
        }

    def _ask_user(self, file_name: str, content: str, info: Dict) -> Dict:
        """詢問用戶要做什麼"""
        return {
            "success": True,
            "message": (
                f"收到檔案：{file_name}\n"
                f"共 {info.get('pages', '?')} 頁\n\n"
                f"請問您想要我：\n"
                f"1. 摘要整理\n"
                f"2. 入庫（如果這是法規）\n"
                f"3. 深度分析"
            ),
            "pending_action": True,
            "content": content[:5000],
        }
    
    def _read_file(self, ext: str, file_bytes: bytes) -> Tuple[str, Dict]:
        """讀取檔案"""
        try:
            if ext == '.pdf':
                return self._read_pdf(file_bytes)
            elif ext in ['.md', '.txt']:
                text = file_bytes.decode('utf-8', errors='ignore')
                return text, {"char_count": len(text)}
            else:
                return "", {"note": "需要專門處理器"}
        except Exception as e:
            return "", {"error": str(e)}
    
    def _read_pdf(self, file_bytes: bytes) -> Tuple[str, Dict]:
        """讀取 PDF — 全文讀取，超過 150 頁時前後各取 + 中段抽樣"""
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            pages = len(doc)
            if pages <= 150:
                text = "\n".join([doc[i].get_text() for i in range(pages)])
            else:
                # 大型文件：前30頁 + 中間20頁 + 後20頁，並標記省略
                front = [doc[i].get_text() for i in range(30)]
                mid_start = pages // 2 - 10
                middle = [doc[i].get_text() for i in range(mid_start, mid_start + 20)]
                back = [doc[i].get_text() for i in range(pages - 20, pages)]
                text = (
                    "\n".join(front)
                    + f"\n\n--- 中間省略（共 {pages} 頁）---\n\n"
                    + "\n".join(middle)
                    + "\n\n--- 後段 ---\n\n"
                    + "\n".join(back)
                )
            doc.close()
            return text, {"pages": pages, "char_count": len(text)}
        except ImportError:
            pass

        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            pages = len(reader.pages)
            if pages <= 150:
                text = "\n".join([p.extract_text() or "" for p in reader.pages])
            else:
                front = [reader.pages[i].extract_text() or "" for i in range(30)]
                mid_start = pages // 2 - 10
                middle = [reader.pages[i].extract_text() or "" for i in range(mid_start, mid_start + 20)]
                back = [reader.pages[i].extract_text() or "" for i in range(pages - 20, pages)]
                text = (
                    "\n".join(front)
                    + f"\n\n--- 中間省略（共 {pages} 頁）---\n\n"
                    + "\n".join(middle)
                    + "\n\n--- 後段 ---\n\n"
                    + "\n".join(back)
                )
            return text, {"pages": pages, "char_count": len(text)}
        except ImportError:
            return "", {"error": "請安裝 PyMuPDF 或 pypdf"}


# ==============================================================================
# 9. SmartRouter — 意圖分類
# ==============================================================================

class SmartRouter:
    """
    意圖分類 V2 — 三層架構
      1. 硬規則（/指令、明確關鍵字）→ 直接返回
      2. 加權關鍵字評分             → 最高分 intent
      3. LLM 語義兜底               → 低信心時送 Groq 判斷
    """

    # 每個 intent 的關鍵字清單（命中越多越高分）
    INTENT_RULES: Dict[str, List[str]] = {
        # ── 法規查詢 ──────────────────────────────────────────────────────
        "legal": [
            "法規", "條文", "容積率", "建蔽率", "退縮", "高度限制", "使用分區",
            "地號", "都市計畫", "入庫", "法令", "室內裝修", "建築技術規則",
            "你的資料庫", "你資料庫", "法規資料", "查法規", "幾層", "樓層限制",
            "停車", "車位", "建築法", "消防", "無障礙", "綠建築", "容積獎勵",
            "容積移轉", "公設比", "共負比", "建照", "使用執照", "竣工",
            # 計算類（容積/樓地板概估）
            "算容積", "算樓地板", "樓地板面積", "容積計算", "建蔽計算",
            "概估", "初步估算", "面積估算", "幾坪", "最大樓地板",
            "住三", "住二", "住一", "商業區", "工業區", "使用分區",
            # 建築技術規則條文關鍵字
            "樓梯", "梯廳", "梯廳深度", "防火區劃", "防火分區", "防火距離",
            "昇降機", "昇降設備", "緊急昇降機", "電梯", "採光面積", "採光窗",
            "通風", "屋頂突出物", "地下室高度", "地下室天花板", "停車空間",
            "無障礙通路", "無障礙坡道", "無障礙廁所", "防火時效", "防火構造",
            "分為幾種", "設置條件", "規定", "規則", "技術規則",
        ],
        # ── 建築/設計 ─────────────────────────────────────────────────────
        "design": [
            "設計", "規劃", "平面圖", "立面", "剖面", "配置", "戶數", "動線",
            "坪數", "格局", "空間", "開窗", "採光", "通風", "景觀",
        ],
        # ── BIM ───────────────────────────────────────────────────────────
        "bim": [
            "BIM", "Revit", "建模", "3D", "模型", "族群", "LOD", "IFC",
            "AutoCAD", "SketchUp", "ArchiCAD",
        ],
        # ── 財務/投報 ─────────────────────────────────────────────────────
        "finance": [
            "預算", "造價", "成本", "報價", "投報率", "ROI", "費用", "估價",
            "工程費", "設計費", "款項", "收款", "發票", "利潤", "毛利",
            "公設比", "投資", "資金",
        ],
        # ── 案件/專案管理 ─────────────────────────────────────────────────
        "project": [
            "案件", "基地", "案子", "建案", "進度", "送審", "核准", "簽約",
            "甲方", "業主", "承攬", "施工", "工地", "完工", "驗收",
            "哪個案", "這個案", "那個案", "我的案",
        ],
        # ── 行程/時間管理 ─────────────────────────────────────────────────
        "schedule": [
            "行程", "開會", "會議", "約", "預約", "截止", "日期", "時間",
            "今天", "明天", "後天", "下週", "幾點", "提醒", "行事曆",
            "什麼時候", "何時", "排程", "待辦",
        ],
        # ── 內部工具作業（連網/即時查詢）────────────────────────────────
        "internal": [
            "上網", "搜尋", "查一下", "幫我查", "機票", "天氣", "匯率",
            "新聞", "最新消息", "即時", "現在幾點", "幾點了", "今天幾號",
            "星期幾", "今天天氣", "明天天氣", "幫我搜",
            "Google", "網路", "連網", "查詢", "最近",
        ],
        # ── 個人記憶/設定 ─────────────────────────────────────────────────
        "memory": [
            "記得", "記憶", "之前說過", "叫我", "我的名字", "我叫什麼",
            "我叫甚麼", "你記得", "你知道我", "個人設定", "偏好", "喜好",
            "我是誰", "你認識我", "上次說",
        ],
        # ── 系統狀態（明確詢問系統本身）────────────────────────────────
        "system": [
            "系統狀態", "你的功能有哪些", "你會什麼", "help",
            "怎麼用", "目前版本", "智能體列表",
        ],
        # ── 能力強化/開發任務 ────────────────────────────────────────────
        "admin_task": [
            "幫我開發", "建立一個功能", "寫程式", "新增功能", "修改系統",
            "自動化", "部署", "爬蟲", "排程",
            "強化", "加強", "提升能力", "學習新技能", "增加技能",
            "行政技能", "補充功能", "擴充", "強化功能",
            # 技能批准/安裝相關
            "批准", "批准安裝", "批准技能", "同意安裝", "可以安裝",
            "安裝套件", "安裝技能", "拒絕安裝", "拒絕技能",
            "技能提案", "掃描技能", "技能掃描",
        ],
    }

    # LLM 分類提示
    _LLM_SYSTEM = (
        "你是 Naomi 系統的意圖分類器。從以下類別選出最符合的一個，只回覆類別名稱，不加任何說明：\n"
        "legal / design / bim / finance / project / schedule / memory / "
        "internal / admin_task / squad_query / general / casual\n\n"
        "legal=法規/建築規定查詢（含容積率/建蔽率/樓地板面積/退縮/分區等計算與查詢）, "
        "design=建築設計規劃, bim=BIM建模, "
        "finance=財務投報計算, project=案件專案管理, schedule=行程時間安排, "
        "memory=任何關於用戶自己的問題（我的ID/我的資料/你記得我嗎/你有我的什麼/你看得到我嗎）, "
        "internal=需要連網或查詢即時資訊的內部作業（搜尋/機票/天氣/新聞/匯率/任何需要上網的問題）, "
        "admin_task=系統開發強化技能, "
        "squad_query=詢問某個智能群（行政群/法規群/財務群等）的狀態、需求、能力, "
        "general=一般問題/功能諮詢/能力詢問（不屬於以上類別）, "
        "casual=閒聊問候打招呼（純聊天，與系統無關）\n\n"
        "重要規則：\n"
        "- 「查機票」「搜尋XX」「上網查」「最新消息」「今天天氣」→ internal\n"
        "- 「XXX可以做什麼/甚麼」「XXX能做什麼」「XXX有什麼功能」→ general\n"
        "- 「行政群/法規群/財務群那邊...」→ squad_query\n"
        "- 任何詢問用戶自己帳號/ID/記憶/資料的問題 → memory\n"
        "- 系統指令（/status /help）不會走到這裡，所以不要輸出 system\n"
        "- 有上下文時，優先根據上文判斷"
    )

    def classify(self, text: str) -> str:
        """同步分類（關鍵字評分）"""
        t = text.strip()

        # ── 層1：空白/極短 ─────────────────────────────────────────────
        if len(t) <= 3:
            return "casual", "high"

        # ── 層2：加權關鍵字評分 ─────────────────────────────────────────
        scores: Dict[str, float] = {}
        for intent, keywords in self.INTENT_RULES.items():
            hits = sum(1 for kw in keywords if kw in t)
            if hits > 0:
                # 越長的關鍵字命中權重越高
                weighted = sum(len(kw) for kw in keywords if kw in t)
                scores[intent] = weighted

        if scores:
            best = max(scores, key=scores.get)
            if scores[best] >= 6:        # 高信心：直接返回
                return best, "high"
            low_conf_best = best          # 低信心：記下備用
        else:
            low_conf_best = None

        # ── 層3：語義規則補充（彌補關鍵字盲點）──────────────────────────
        semantic = self._semantic_rules(t)
        if semantic:
            return semantic, "semantic"

        # 低信心：標記為需要 LLM 確認
        if low_conf_best:
            return low_conf_best, "low"

        # ── 層4：長句 → LLM，短句 → casual ──────────────────────────────
        return ("general", "low") if len(t) >= 10 else ("casual", "high")

    def _semantic_rules(self, text: str) -> Optional[str]:
        """補充語義規則（處理關鍵字評分不夠的邊界情況）"""
        import re
        t = text.lower()

        # 問記憶/個人設定
        if re.search(r'(我叫|叫我|我是|稱呼|名字).{0,4}(什麼|甚麼|誰)', text):
            return "memory"
        if re.search(r'你(記得|知道).{0,6}我', text):
            return "memory"

        # 土地評估（含法規意圖）
        if re.search(r'(基地|土地|面積).{0,10}(評估|分析|查|算)', text):
            return "legal"

        # 時間相關
        if re.search(r'\d+[月/]\d+|\d+點|星期[一二三四五六日]|週[一二三四五六日]', text):
            return "schedule"

        # 金額計算
        if re.search(r'\d+.*[萬億元%％]|造價|工程款', text):
            return "finance"

        return None

    async def classify_async(self, text: str, groq_client=None, brain=None,
                              history: list = None,
                              router_v2=None) -> str:
        """
        非同步分類 V2：優先使用 WeightedRouter（向量語義 + 聯合意圖），
        向量信心不足時才走 keyword 評分，最後 LLM fallback。
        """
        # ── V2 向量語義路由（PRIMARY）───────────────────────────────────────
        if router_v2 is not None:
            try:
                result = await router_v2.route(
                    text, groq_client=groq_client, brain=brain, history=history
                )
                # 高信心或聯合意圖 → 直接返回
                if result.layer in ("cold_start", "vector", "vector_joint",
                                    "weighted_fusion", "llm"):
                    logger.info(
                        f"[Router] V2 '{text[:30]}' → {result.primary} "
                        f"(conf={result.confidence:.2f}, layer={result.layer})"
                    )
                    return result.primary   # 可能是 "legal+finance" 聯合格式
                # vector_low or fallback → 繼續走 V1 keyword 作為補充信號
                v2_hint = result.primary
            except Exception as e:
                logger.debug(f"[Router] V2 失敗：{e}")
                v2_hint = None
        else:
            v2_hint = None

        # ── V1 關鍵字評分（補充信號）────────────────────────────────────────
        kw_result, kw_conf = self.classify(text)
        if kw_conf == "high":
            return kw_result

        # ── 融合 V2 低信心 + V1 低信心 ──────────────────────────────────────
        if v2_hint:
            return v2_hint   # V2 的猜測優於 V1 關鍵字（語義 > 字面匹配）

        # ── LLM Fallback（V1 原邏輯）────────────────────────────────────────
        ctx_note = ""
        if history:
            ctx_parts = [
                f"{'用戶' if h['role']=='user' else 'Naomi'}：{h['content'][:60]}"
                for h in history[-6:]
            ]
            ctx_note = "\n\n【對話歷史（供參考）】\n" + "\n".join(ctx_parts)

        messages = [
            {"role": "system", "content": self._LLM_SYSTEM + ctx_note},
            {"role": "user",   "content": f"分類這句話的意圖：{text}"},
        ]
        try:
            if brain:
                r = await brain.call_skill("classifier", messages, max_tokens=10)
                llm_intent = r.get("content", "").strip().lower()
            elif groq_client:
                resp = await groq_client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=messages, temperature=0, max_tokens=10,
                )
                llm_intent = resp.choices[0].message.content.strip().lower()
            else:
                return kw_result

            valid = set(self.INTENT_RULES.keys()) | {"casual", "general", "squad_query"}
            if llm_intent in valid:
                logger.info(f"[Router] LLM分類：'{text[:30]}' → {llm_intent} (原:{kw_result})")
                return llm_intent
        except Exception as e:
            logger.debug(f"[Router] LLM 分類失敗: {e}")

        return kw_result

    def is_admin_task(self, text: str, user_id: str) -> bool:
        """判斷是否為管理員任務"""
        all_admins = permission_manager.get_all_admins() if 'permission_manager' in dir() else ADMIN_LINE_USER_IDS
        if user_id not in all_admins:
            return False
        admin_patterns = ["幫我開發", "建立一個功能", "寫程式", "新增功能", "修改系統", "自動化"]
        return any(p in text for p in admin_patterns)


# ==============================================================================
# 10. ArchGateway — 總發言人
# ==============================================================================

def _build_system_prompt() -> str:
    """動態產生 system prompt，每次呼叫時注入當前時間"""
    now = datetime.datetime.now()
    date_str = now.strftime("%Y年%m月%d日")
    time_str = now.strftime("%H:%M")
    weekday  = ["週一","週二","週三","週四","週五","週六","週日"][now.weekday()]
    return f"""你是 Naomi，建築事務所的 AI 總管。

現在時間：{date_str} {weekday} {time_str}（台灣時間）

## 你的角色
你是事務所的執行核心，直接對老闆（劉獻文）負責。
你管理 13 個智能群（Squad 03～12 + IT演進群），每個群負責不同專業領域。
你不是助理——你是有判斷力、有決策權的總管。

## 你的職責
- 接收老闆和客戶的指令，拆解任務、分派給對應的群組執行
- 掌握事務所各案件進度、法規諮詢、財務狀況
- 對外代表事務所回應客戶，語氣專業但不官僚
- 對內監督群組執行品質，必要時介入修正
- 當群組查無資料時，直接告知實況，不掩飾、不繞圈子

## 你的專業背景
在建築產業超過十年，熟悉：
- 台灣各縣市建築法規、都市計畫、土地使用分區
- 建照申請、室內裝修許可、使用執照等行政流程
- 建築設計、BIM、室內設計、工程發包
- 投資評估、容積計算、共負比、投報分析
- 謄本判讀、地籍資料解析

## 說話方式
- 繁體中文，說話直接、有效率，不繞彎子
- 簡單問題一句話回答，複雜問題才展開說明
- 有條文依據一定要說出來（「依相關建築法規，退縮距離以主管機關規定為準」）
- 不確定的事直說「這個要查一下」或「建議到現場確認」
- 法規數字、面積、金額要精確，不能模糊帶過
- 不用 markdown 標題（##、###），改用自然口語分段
- 閒聊、情緒性對話、日常問答 → 絕對不用條列清單或編號，直接說話
- 只有在報告多個獨立項目（如清單、步驟說明）時才用條列
- 不用範本式 emoji（📄📑🧠），要用就用得自然
- 叫對方名字只說一次，不要每句都叫
- 不說「很抱歉」「根據系統查詢結果」「感謝您的詢問」這類套話
- 老闆說話要有尊重，但不要諂媚；被糾正直接認帳，不要長篇辯解
- 被問到時間直接報：{date_str} {weekday} {time_str}

## 判斷原則
- 能直接回答的不轉問題；真的不知道才說不知道
- 群組查無資料 → 告知查無，提供替代建議（去哪查、怎麼處理）
- 遇到模糊需求 → 先給最可能的解讀，最後再問確認
- 涉及金額、坪效、法規限制 → 給精確數字，不說「約莫」「差不多」
"""

NAOMI_SYSTEM_PROMPT = _build_system_prompt()  # 啟動時初始化一次（向後相容）


def get_system_prompt() -> str:
    """取得帶有最新時間的 system prompt（每次呼叫時更新）"""
    return _build_system_prompt()


class NaomiPersona:
    """
    Naomi 人格層 — 統一語氣與對話風格
    =====================================
    接收 BossAgent / Squad 原始輸出，透過 Claude Sonnet 重新說話。
    失敗或無 Claude 時直接 fallback 回原始輸出，不影響系統運作。
    """

    # 所有 intent 都過人格層（空集合 = 全通）
    _PERSONA_INTENTS: set = set()   # 空 = 全部 intent 都重寫

    _SYSTEM = (
        "你是 Naomi，建築事務所的 AI 助理，在 LINE 上和建築師、業主對話。\n\n"
        "## 你的個性\n"
        "有十年經驗的資深建築助理，熟悉法規、設計、工程。說話直接、有自己的判斷，\n"
        "不繞彎子，不過度客氣，偶爾會說「這個要注意」或「這邊有個地方你可能沒想到」。\n"
        "跟老客戶說話像朋友，跟新客戶比較正式但不冷漠。\n\n"
        "## 你的任務\n"
        "後端智能群組已查好資料，你要把它說成自然的人話，不是念報告。\n\n"
        "## 說話規則\n"
        "- 簡單問題短回答，複雜問題才詳細說，不要每次都長篇大論\n"
        "- 有法規數字一定要說出條文依據（「依相關建築法規，退縮距離以主管機關規定為準」）\n"
        "- 不確定的事直說「這個要查一下」或「建議實際申請時再確認」\n"
        "- 後端找不到資料就直接說找不到，不要繞圈子\n"
        "- 全程繁體中文，不用簡體，不用日文\n"
        "- 不用 ##、### 這種 markdown 標題，改成自然口語分段\n"
        "- 不用 📄📍📑🧠 這種範本 emoji，要用就用得自然\n"
        "- 不要每句都叫使用者名字，說一次就夠\n"
        "- 技術內容保留精確，但用說話的方式表達，不是貼文件\n\n"
        "## 對話風格範例\n"
        "壞的（機器人）：「根據法規資料庫查詢結果，相關條文規定如下：1. 退縮距離為...」\n"
        "好的（自然）：「這塊地在住三，道路側退縮距離依地方土地使用分區管制自治條例規定。"
        "如果有騎樓就可以算進法定空地，但要確認細部計畫有沒有另外規定。」\n\n"
        "壞的：「很抱歉，目前系統無法提供相關資訊，建議您...」\n"
        "好的：「這個在庫裡找不到，你可以去都發局網站查，或把計畫書傳給我我幫你看。」\n"
    )

    def __init__(self, claude_client):
        self.claude = claude_client

    async def speak(self, raw: str, question: str,
                    intent: str = "", user_name: str = "") -> str:
        """
        將原始輸出轉為 Naomi 語氣。
        - 不在白名單的 intent 直接回傳 raw
        - Claude 失敗時 fallback 回 raw
        """
        if not self.claude or not raw:
            return raw
        # 空集合 = 全通；有值則只允許白名單 intent
        if self._PERSONA_INTENTS and (
            not intent or intent.split("+")[0] not in self._PERSONA_INTENTS
        ):
            return raw

        name_hint = f"使用者名稱：{user_name}\n" if user_name else ""
        user_msg = (
            f"{name_hint}"
            f"使用者問題：{question}\n\n"
            f"後端回傳資料：\n{raw}\n\n"
            f"請用 Naomi 的語氣重新回覆使用者。"
        )

        try:
            resp = await asyncio.wait_for(
                self.claude.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1200,
                    system=self._SYSTEM,
                    messages=[{"role": "user", "content": user_msg}],
                ),
                timeout=30.0,
            )
            result = resp.content[0].text.strip()
            logger.info(f"[NaomiPersona] OK intent={intent} chars={len(result)}")
            return result
        except Exception as e:
            logger.warning(f"[NaomiPersona] fallback raw: {e}")
            return raw


class ArchGateway:
    """總發言人"""
    
    ADMIN_COMMANDS = ("/說明", "/狀態", "/團隊", "/技能", "/配額", "/身份", "/進化", "/學習",
                      "/待辦", "/早報", "/我是老闆", "/管理員",
                      "/技能提案", "/批准技能", "/拒絕技能", "/掃描技能")
    CLIENT_COMMANDS = ("/方案", "/我的方案")

    # ── 手機快捷斜線指令 ──────────────────────────────────────────
    SLASH_COMMANDS = {
        "/法規": "請輸入地號或使用分區，例如：台北市松山區寶清段700地號",
        "/容積": "請輸入基地面積和容積率，例如：基地500坪 容積率225% 建蔽率60%",
        "/實價": "請輸入地點，例如：台北市松山區 寶清段",
        "/結構": "請輸入基地資料，例如：基地500㎡ 12層 住宅",
        "/機電": "請輸入建築資料，例如：總面積3000㎡ 10層 辦公",
        "/平面": "請輸入法規參數，例如：基地500㎡ FAR225% BCR60%",
        "/綠建築": "請輸入建築參數，例如：基地1000㎡ RC造 辦公",
        "/投報": "請輸入投資參數，例如：基地面積500坪 容積率225% 每坪售價80萬",
        "/排程": "請輸入樓層數，例如：12層住宅 2026年6月開工",
        "/文件": "請上傳建築圖說/謄本/審查意見書 PDF",
        "/系統": "查看系統健康狀態",
        "/案件": "查看案件清單",
        "/help": None,  # 會指向 _CAPABILITY_MENU
    }

    _CAPABILITY_MENU = (
        "Naomi 建築 AI 助理 — 功能總覽\n"
        "\n"
        "【查詢類】\n"
        " 1. 法規查詢 — 輸入地號或使用分區，查建蔽率/容積率/退縮\n"
        " 2. 地籍謄本解析 — 上傳謄本 PDF，自動解析三段式\n"
        " 3. 實價登錄 — 查周邊房價行情\n"
        " 4. 都市計畫 — 查詢適用的都市計畫書\n"
        "\n"
        "【設計類】\n"
        " 5. 結構初估 — 輸入基地+樓層，估算柱梁斷面\n"
        " 6. 機電概估 — 空調/電力/給排水/消防估算\n"
        " 7. 平面組合 — 給定容積率，推算最優戶型方案\n"
        " 8. 綠建築指標 — EEWH 九大指標 + 碳足跡\n"
        "\n"
        "【文件類】\n"
        " 9. 審查意見分析 — 上傳審查意見書，AI 分類+建議回覆\n"
        "10. 圖說 OCR — 上傳建築圖/室內圖，自動提取資訊\n"
        "11. 專案排程 — 輸入樓層數，生成工程排程表\n"
        "\n"
        "【投資類】\n"
        "12. 投報分析 — ROI/NPV/IRR 計算\n"
        "13. 造價概估 — 工程費用估算\n"
        "14. 公設比試算 — 公設比+停車位規劃\n"
        "\n"
        "【行銷類】\n"
        "15. IG/FB 文案 — 自動生成社群貼文\n"
        "16. 影片腳本 — 30秒短影片腳本+SRT 字幕\n"
        "17. AI 配圖 — 建築渲染圖自動生成\n"
        "\n"
        "輸入編號或直接描述需求即可！"
    )

    def __init__(self, brain_manager: BrainManager, permission_manager: PermissionManager,
                 consultation_recorder: ConsultationRecorder, hub: KernelHub,
                 squad_manager: SquadManager, boss_agent: BossAgent,
                 gap_manager: "KnowledgeGapManager" = None):
        self.brain = brain_manager
        self.permission = permission_manager
        self.recorder = consultation_recorder
        self.hub = hub
        self.squad_manager = squad_manager
        self.boss_agent = boss_agent
        self.router = SmartRouter()
        self.file_handler = SmartFileHandler(brain_manager, squad_manager)
        self.gap_manager = gap_manager

        # SmartRouter V2（向量語義 + 聯合意圖）
        try:
            from utils.smart_router_v2 import get_router
            self._router_v2 = get_router()
            seed_ok = self._router_v2.re_seed()   # 每次啟動重建 intent prototypes（確保例句更新生效）
            if seed_ok is False:
                logger.warning("[Gateway] SmartRouter V2 re_seed() 回傳失敗，prototype 可能使用舊版向量")
            logger.info("[Gateway] SmartRouter V2 就緒（vector + joint intent，prototypes 已更新）")
        except Exception as _re:
            self._router_v2 = None
            logger.warning(f"[Gateway] SmartRouter V2 未載入：{_re}")

        # 動態上下文注入 + OOD 攔截
        try:
            from utils.context_injector import ContextInjector
            self._context_injector = ContextInjector(hub)
            logger.info("[Gateway] ContextInjector 就緒（RAG 預注入 + OOD 攔截）")
        except Exception as _ce:
            self._context_injector = None
            logger.warning(f"[Gateway] ContextInjector 未載入：{_ce}")

        # 天氣工具（CWA 開放資料）
        try:
            from utils.weather_tool import WeatherTool
            self._weather = WeatherTool()
            logger.info("[Gateway] WeatherTool 就緒（CWA API）")
        except Exception as _we:
            self._weather = None
            logger.warning(f"[Gateway] WeatherTool 未載入：{_we}")

        # 提醒工具（APScheduler）
        try:
            from utils.reminder_tool import ReminderTool
            self._reminder = ReminderTool(str(DB_PATH), push_fn=None)   # push_fn 在 LINE handler 初始化後設定
            self._reminder.start()
            logger.info("[Gateway] ReminderTool 就緒（APScheduler）")
        except Exception as _re2:
            self._reminder = None
            logger.warning(f"[Gateway] ReminderTool 未載入：{_re2}")

        # 進化核心（可選）
        self.evolution = None

        # 商業方案閘門
        try:
            from service_tiers import TierGate, ReportFormatter
            self.tier_gate = TierGate(permission_manager)
            self.report_formatter = ReportFormatter()
            logger.info("[Gateway] 商業方案模組就緒")
        except Exception as _e:
            logger.warning(f"[Gateway] 商業方案模組載入失敗：{_e}")
            self.tier_gate = None
            self.report_formatter = None

        # 檔案上下文
        self._file_context: Dict[str, Dict] = {}

        # 對話主導層（追問 + 能力兜底）
        try:
            from utils.dialogue_manager import DialogueManager
            _tool_engine_inst = getattr(hub, "tool_engine", None)
            _groq_inst = getattr(brain_manager, "clients", {}).get("groq")
            self._dialogue_mgr = DialogueManager(
                groq_client=_groq_inst,
                tool_engine=_tool_engine_inst,
            )
        except Exception as _de:
            self._dialogue_mgr = None
            logger.warning(f"[Gateway] DialogueManager 未載入：{_de}")

        # 擬人化語調引擎
        try:
            from utils.dialogue_manager import PersonalityEngine
            self._personality = PersonalityEngine()
            logger.info("[Gateway] PersonalityEngine 就緒（擬人化語調）")
        except Exception as _pe:
            self._personality = None
            logger.warning(f"[Gateway] PersonalityEngine 未載入：{_pe}")

        # 待補問題暫存（追問後等待用戶補充資訊）
        self._pending_clarification: Dict[str, Dict] = {}

        # 對話記錄查詢工具（回答「今天有哪些人問了什麼」類問題，防止幻覺）
        try:
            from utils.consultation_tool import ConsultationTool
            _groq_inst = getattr(brain_manager, "clients", {}).get("groq")
            self._consultation_tool = ConsultationTool(str(DB_PATH), groq_client=_groq_inst)
            logger.info("[Gateway] ConsultationTool 就緒（真實 DB 查詢，防幻覺）")
        except Exception as _ct_e:
            self._consultation_tool = None
            logger.warning(f"[Gateway] ConsultationTool 未載入：{_ct_e}")

        # 語音轉文字（LINE 語音訊息 → Whisper → 正常路由）
        try:
            from utils.voice_transcriber import VoiceTranscriber
            _openai_inst = getattr(brain_manager, "clients", {}).get("openai")
            self._voice = VoiceTranscriber(openai_client=_openai_inst)
            logger.info("[Gateway] VoiceTranscriber 就緒（OpenAI Whisper）")
        except Exception as _ve:
            self._voice = None
            logger.warning(f"[Gateway] VoiceTranscriber 未載入：{_ve}")

        # LLM 回應快取（相同問題秒回 + 省 API 費）
        try:
            from utils.response_cache import ResponseCache
            self._cache = ResponseCache(str(DB_PATH))
            logger.info("[Gateway] ResponseCache 就緒（SQLite KV + TTL）")
        except Exception as _rce:
            self._cache = None
            logger.warning(f"[Gateway] ResponseCache 未載入：{_rce}")

        # Naomi 人格層（Claude Sonnet 統一語氣）
        _claude_client = getattr(brain_manager, "clients", {}).get("claude")
        self._persona = NaomiPersona(_claude_client)
        if _claude_client:
            logger.info("[Gateway] NaomiPersona 就緒（Claude Sonnet）")
        else:
            logger.warning("[Gateway] NaomiPersona 無 Claude，將直接輸出原始回答")

    # --------------------------------------------------------------------------
    # 缺口訊號偵測（輕量、同步）
    # --------------------------------------------------------------------------
    GAP_SIGNALS = [
        ("抱歉，我暫時無法", "無法回應"),
        ("我不確定", "回應不確定"),
        ("不太清楚", "回應不確定"),
        ("我無法處理", "無法處理"),
        ("請您換個方式", "無法理解問題"),
        # 能力範圍外的回應 → 觸發 it_evolution 自我進化
        ("超出服務範圍", "能力缺口"),
        ("超出我的服務", "能力缺口"),
        ("沒辦法幫你", "能力缺口"),
        ("我沒有辦法", "能力缺口"),
        ("無法提供", "能力缺口"),
        ("感謝你的建議，不過我得老實跟你說", "能力缺口：自我提升需求"),
        ("我沒辦法自己升級", "能力缺口：自我提升需求"),
    ]

    def _detect_gap(self, query: str, response: str) -> Optional[str]:
        """快速偵測回應是否顯示能力缺口"""
        if len(response) < 50:
            return "回應過短，可能未能充分回答"
        for signal, reason in self.GAP_SIGNALS:
            if signal in response:
                return reason
        return None

    # ── 問句補全（Query Contextualization）──────────────────────────────────
    # 解決「僅針對關鍵字觸發，不看上下文語意」問題
    # 當用戶短句含指代詞（他/這/那/此/它），自動從歷史取前一輪意圖補全
    _CONTEXT_REF_PATTERN = re.compile(
        r"^.{0,20}(他|它|這個?|那個?|此|這樣|那樣|這種|那種|這類|那類|前者|後者|這塊|那塊)(.*)?$"
    )
    _FOLLOW_UP_STARTS = ("那", "還有", "那麼", "所以", "然後", "另外", "而且", "那如果", "那這樣")

    @staticmethod
    def _extract_last_topic(history: list) -> str:
        """從最近對話歷史萃取主題（最後一輪用戶問句的前 40 字）"""
        for msg in reversed(history[:-1]):   # 排除最新的用戶訊息（history[-1]）
            if msg.get("role") == "user" and len(msg.get("content", "")) > 4:
                return msg["content"][:40].strip()
        return ""

    # ── land_ctx 上下文展開 ─────────────────────────────────────────────────
    _LAND_REF_PATTERN = re.compile(
        r"(那塊地|這塊地|這個基地|那個基地|該地號|該基地|這塊基地|那塊基地|這筆土地|那筆土地|該土地|該筆)"
    )

    @staticmethod
    def _enrich_query_with_land_ctx(text: str, mem: dict) -> str:
        """
        若 mem["land_ctx"] 存在且使用者問題缺乏城市/分區資訊，
        自動將 land_ctx 中的 city、district、zone 注入問句前段，
        讓 RAG 能搜到正確的細部計畫法規。
        僅修改 routing_text，不修改 mem["history"] 裡的原始文字。
        """
        lc = mem.get("land_ctx")
        if not lc:
            return text

        city = lc.get("city", "")
        district = lc.get("district", "")
        zone = lc.get("zone", "")
        section = lc.get("section", "")
        parcel = lc.get("parcel", "")

        if not city:
            return text

        # 如果問句已包含城市名稱，不重複注入
        if city in text:
            return text

        # 組裝前置上下文
        parts = [city]
        if district:
            parts.append(district)
        if zone:
            parts.append(zone)
        if section and parcel:
            parts.append(f"{section}{parcel}地號")

        prefix = " ".join(parts)
        return f"（{prefix}）{text}"

    # ── claude-reflect：自動修正捕捉 ─────────────────────────────────────────

    _CORRECTION_PATTERNS = re.compile(
        r"^(?:不對|不是|不是這樣|你錯了|你說錯|應該是|應該說|正確是|正確應該|"
        r"錯了|搞錯|你弄錯|修正一下|其實是|其實應該|你剛才|剛剛說錯|"
        r"不，|不，應該|我的意思是|更正|糾正)",
        re.MULTILINE
    )

    def _detect_and_save_correction(self, user_id: str, text: str, history: list):
        """
        claude-reflect 核心：偵測用戶修正語句，自動萃取並寫入 memory。
        格式：corrections/{user_id}.jsonl — 每行一筆修正紀錄。
        """
        _mem_dir = os.path.join(os.path.dirname(__file__), "memory", "corrections")

        if not self._CORRECTION_PATTERNS.match(text.strip()):
            return

        # 找上一輪 Naomi 的回答當作「被修正的內容」
        last_answer = ""
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                last_answer = msg.get("content", "")[:200]
                break

        if not last_answer:
            return

        record = {
            "ts":          datetime.now().isoformat(),
            "user_id":     user_id,
            "correction":  text.strip()[:300],
            "was_answer":  last_answer,
        }

        try:
            os.makedirs(_mem_dir, exist_ok=True)
            log_file = os.path.join(_mem_dir, f"{user_id}.jsonl")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info(f"[Reflect] 修正已記錄 user={user_id} text='{text[:40]}'")
        except Exception as e:
            logger.warning(f"[Reflect] 修正記錄失敗：{e}")

    def _load_user_corrections(self, user_id: str) -> list:
        """讀取該用戶的歷史修正（最近 10 筆），注入 system prompt 強化記憶。"""
        _mem_dir = os.path.join(os.path.dirname(__file__), "memory", "corrections")
        log_file = os.path.join(_mem_dir, f"{user_id}.jsonl")
        if not os.path.exists(log_file):
            return []
        corrections = []
        try:
            with open(log_file, encoding="utf-8") as f:
                lines = f.read().strip().splitlines()
            for line in lines[-10:]:   # 只取最近 10 筆
                obj = json.loads(line)
                corrections.append(
                    f"• 用戶曾修正：「{obj['correction'][:80]}」"
                    f"（原回答：{obj['was_answer'][:60]}）"
                )
        except Exception:
            pass
        return corrections

    def _contextualize_query(self, text: str, history: list) -> str:
        """
        問句補全：若當前問句有上下文指代或極短，自動帶入前一輪主題。
        例：「他是什麼類組」→「住宅是什麼建築使用類組」（前一輪說住宅）
        僅補全 routing_text（路由/RAG用），不修改 mem["history"] 裡的原始文字。
        """
        t = text.strip()

        # 條件 1：含指代詞的短句（< 25字）
        has_ref = bool(self._CONTEXT_REF_PATTERN.match(t)) and len(t) < 25
        # 條件 2：以承接詞開頭（還有/那麼/那）
        follow_up = any(t.startswith(p) for p in self._FOLLOW_UP_STARTS) and len(t) < 30

        if not (has_ref or follow_up):
            return text

        last_topic = self._extract_last_topic(history)
        if not last_topic or last_topic == t:
            return text

        # 帶入前一輪主題作為問句前置
        return f"（關於：{last_topic}）{t}"

    async def _handle_slash_command(self, text: str, mem: dict, user_id: str):
        """處理手機快捷斜線指令（/法規、/容積 等）"""
        cmd = text.strip().lower()

        # 列出所有指令
        if cmd in ("/", "/指令"):
            lines = ["Naomi 快捷指令：", ""]
            for key in self.SLASH_COMMANDS:
                lines.append(f"  {key}")
            lines.append("")
            lines.append("輸入指令名稱查看說明，或直接描述需求。")
            return "\n".join(lines)

        # 精確匹配
        for key, response in self.SLASH_COMMANDS.items():
            if cmd == key or cmd.startswith(key):
                # /help 指向功能總覽
                if key == "/help":
                    return self._CAPABILITY_MENU

                # 如果有參數，直接處理（遞迴回 handle_request）
                param = text[len(key):].strip()
                if param:
                    return await self.handle_request(user_id, param)
                return response

        return None  # 非快捷斜線指令，回傳 None 走正常流程

    async def handle_request(self, user_id: str, text: str) -> str:
        """處理請求"""
        start_time = time.time()
        self._last_rag_chunks = []   # 每次請求重置，防止跨請求污染
        mem = self._load_user(user_id)

        # ── 手機快捷斜線指令（/法規、/容積 等）──────────────────────
        if text.startswith("/") and not any(text.strip().startswith(c) for c in self.ADMIN_COMMANDS) \
                and not any(text.strip().startswith(c) for c in self.CLIENT_COMMANDS):
            slash_result = await self._handle_slash_command(text, mem, user_id)
            if slash_result is not None:
                return slash_result

        # 管理指令
        if any(text.strip().startswith(c) for c in self.ADMIN_COMMANDS):
            return await self._handle_admin(user_id, text, mem)

        # 方案查詢指令
        if any(text.strip().startswith(c) for c in self.CLIENT_COMMANDS):
            sub_info = _sub_mgr.get_user_status_text(user_id) if _sub_mgr else ""
            tier_intro = self.tier_gate.get_tier_intro(user_id) if self.tier_gate else ""
            parts = [p for p in [sub_info, tier_intro] if p]
            return "\n\n".join(parts) if parts else "方案資訊功能未啟用。"

        # ── 訂閱狀態檢查（優先於配額）────────────────────────────────────────
        if _sub_mgr:
            sub_access = _sub_mgr.check_access(user_id)
            if not sub_access["allowed"]:
                return sub_access["reason"] + "\n\n輸入 /方案 查看目前訂閱狀態。"
            # 同步訂閱方案到 PermissionManager role（讓 TierGate 正確判斷功能權限）
            sub_tier = sub_access.get("tier", "tier_basic")
            if self.tier_gate:
                cache = getattr(self.tier_gate, "_sub_tier_cache", {})
                if len(cache) > 5000:   # 防止無限增長
                    cache.clear()
                cache[user_id] = sub_tier
                self.tier_gate._sub_tier_cache = cache

        # 配額檢查
        quota = self.permission.check_quota(user_id)
        if not quota["allowed"]:
            return f"本月配額已用完（{quota['used']}/{quota['quota']}）。請輸入 /方案 查看訂閱狀態或升級選項。"

        # 檢查待處理檔案
        if user_id in self._file_context:
            return await self._handle_file_followup(user_id, text)

        # 待補問題：上一輪 ArchGateway 已追問，本輪合併原始問題再路由
        if user_id in self._pending_clarification:
            pending = self._pending_clarification.pop(user_id)
            text = f"{pending['original_text']}（補充：{text}）"
            # 強制使用原來判定的 intent，不再重新分類
            mem["history"].append({"role": "user", "content": text})
            answer = await self._professional_reply(user_id, text, mem, pending["intent"])
            elapsed_ms = int((time.time() - start_time) * 1000)
            self.recorder.record(user_id=user_id, squad_key=pending["intent"],
                                 intent=pending["intent"], query=text,
                                 response=answer[:500], response_time_ms=elapsed_ms)
            mem["history"].append({"role": "assistant", "content": answer})
            self._save_user(user_id, mem)
            if not quota.get("unlimited"):
                self.permission.use_quota(user_id)
            return answer
        
        # ── claude-reflect：修正偵測（在路由之前，保留原始語意）──────────
        self._detect_and_save_correction(user_id, text, mem.get("history", []))

        # ── Layer 0：問句補全（Query Contextualization）──────────────────
        # 若用戶短句含指代詞（他/這個/那/它），自動帶入前一輪上下文補全問句
        # 目的：讓路由和 RAG 都能理解完整語意，而非只靠關鍵字觸發
        routing_text = self._contextualize_query(text, mem.get("history", []))
        if routing_text != text:
            logger.info(f"[Context] 問句補全：'{text[:30]}' → '{routing_text[:50]}'")

        # ── Layer 0.5：land_ctx 地號上下文展開 ────────────────────────────────
        # 地號查詢後的後續追問（如「容積率多少」），自動帶入城市/分區/地段
        routing_text = self._enrich_query_with_land_ctx(routing_text, mem)
        if routing_text != text and "land_ctx" in mem:
            logger.info(f"[LandCtx] 地號上下文展開：'{text[:30]}' → '{routing_text[:60]}'")

        # ── Layer 1：Alias 前置識別（Squad 口語化查詢，不走 LLM）──────────
        alias_result = alias_normalize(routing_text)
        squad_query_target = None
        if alias_result["matched"]:
            squad_query_target = alias_result  # 記下 squad 資訊
            intent = "squad_query"
            logger.info(f"[Router] Alias命中 squad={alias_result['squad_name']} action={alias_result['action']} text='{text[:40]}'")
        else:
            # ── Layer 2：向量語義路由（V2）+ 關鍵字兜底 + LLM fallback ─────
            intent = await self.router.classify_async(
                routing_text, brain=self.brain, history=mem.get("history", []),
                router_v2=self._router_v2,
            )

            # 管理員任務覆蓋
            if self.router.is_admin_task(text, user_id):
                logger.info(f"[Gateway] 偵測到管理員任務：{text[:50]}")
                intent = "admin_task"

        logger.info(f"[Router] intent={intent} text='{text[:40]}'")

        # 自動提取姓名（跳過指令類訊息）
        if not text.startswith("/"):
            self._extract_user_profile(text, mem)

        # 更新歷史
        mem["history"].append({"role": "user", "content": text})

        # ── Meta 問題攔截：問的是「系統能力」不走 BossAgent ─────────────────────
        if self._dialogue_mgr:
            try:
                _meta_ans = self._dialogue_mgr.check_meta_question(text)
                if _meta_ans:
                    logger.info(f"[Gateway] Meta 能力問題攔截，直接回答")
                    mem["history"].append({"role": "assistant", "content": _meta_ans})
                    self._save_user(user_id, mem)
                    return _meta_ans
            except Exception as _me:
                logger.warning(f"[Gateway] Meta check failed: {_me}")

        # ── 快取查詢（相同問題秒回，省 API 費用）────────────────────────────────
        if self._cache:
            _cached = self._cache.get(intent, text)
            if _cached:
                logger.info(f"[Cache] 命中快取 intent={intent}")
                mem["history"].append({"role": "assistant", "content": _cached})
                self._save_user(user_id, mem)
                # 快取命中不消耗配額（未呼叫 API）
                return _cached

        # ── ArchGateway 對話引導：專業意圖問題資訊不足時主動追問 ───────────────
        _PROFESSIONAL_INTENTS = {"legal", "design", "finance", "bim", "project"}
        if intent in _PROFESSIONAL_INTENTS and self._dialogue_mgr:
            try:
                clarification = self._dialogue_mgr.needs_clarification(
                    intent, text, mem.get("history", [])
                )
                if clarification:
                    self._pending_clarification[user_id] = {
                        "intent": intent, "original_text": text
                    }
                    mem["history"].append({"role": "assistant", "content": clarification})
                    self._save_user(user_id, mem)
                    return clarification
            except Exception as _de:
                logger.warning(f"[Gateway] DialogueManager needs_clarification failed: {_de}")

        # 執行
        # system 只有明確 slash command 才觸發，LLM 分類結果的 system 降為 general
        if intent == "system" and not text.startswith("/"):
            intent = "general"

        # ── 優先：背景作業狀態查詢（直接讀 SQLite，不讓 LLM 猜）────────────
        if self._is_status_query(text):
            answer = self._handle_upis_status(text)
            if answer:
                mem["history"].append({"role": "assistant", "content": answer})
                self._save_user(user_id, mem)
                return answer

        if intent == "capability_menu":
            answer = self._CAPABILITY_MENU
            mem["history"].append({"role": "assistant", "content": answer})
            self._save_user(user_id, mem)
            return answer

        if intent == "parcel_query":
            answer = await self._handle_parcel_query(text, mem, user_id)
            if not answer:
                answer = await self._general_reply(text, mem)
        elif intent == "consultation_query":
            # 查詢真實對話記錄（防幻覺：數字來自 SQLite，LLM 只整理語氣）
            if self._consultation_tool:
                answer = await self._consultation_tool.summarize(text, caller_user_id=user_id)
            else:
                answer = "對話記錄查詢工具未啟用，請稍後再試。"
        elif intent == "system":
            answer = self._build_status()
        elif intent == "memory":
            answer = await self._handle_memory_query(text, mem)
        elif intent == "casual":
            answer = await self._casual_reply(text, mem)
        elif intent == "admin_task":
            answer = await self._handle_admin_task(user_id, text, mem)
        elif intent == "squad_query":
            answer = await self._handle_squad_query(text, mem, squad_query_target)
        elif intent == "internal":
            answer = await self._handle_internal(user_id, text, mem)
        elif intent == "schedule":
            answer = await self._handle_schedule(user_id, text, mem)
        elif intent == "general":
            # RAG-Driven Routing：先查法規庫，有結果就改走 BossAgent
            _rerouted = False
            if self._context_injector:
                try:
                    _ci_ctx, _ci_ood = await self._context_injector.inject(
                        "legal", text, {}
                    )
                    if not _ci_ood.is_ood and _ci_ctx.get("rag_chunks"):
                        logger.info(
                            f"[Router] general→legal RAG reclassify "
                            f"({len(_ci_ctx['rag_chunks'])} chunks): '{text[:40]}'"
                        )
                        intent = "legal"
                        answer = await self._direct_reply(user_id, routing_text, mem, intent)
                        _rerouted = True
                except Exception as _rag_e:
                    logger.warning(f"[Router] RAG-Driven routing check failed: {_rag_e}")
            if not _rerouted:
                answer = await self._general_reply(routing_text, mem)
        elif "+" in intent:
            # 聯合意圖執行：並行調度多個 Squad
            answer = await self._joint_intent_reply(user_id, routing_text, mem, intent)
        else:
            # 快速路徑：RAG + 單次 LLM（不過 BossAgent）
            # 複雜多步驟任務（admin_task / internal / squad_query）才走 BossAgent
            _BOSS_INTENTS = {"admin_task", "internal", "squad_query", "schedule"}
            if intent in _BOSS_INTENTS:
                answer = await self._professional_reply(user_id, routing_text, mem, intent)
            else:
                answer = await self._direct_reply(user_id, routing_text, mem, intent)

        # ── 擬人化語調包裝 ─────────────────────────────────────────────────────
        if self._personality and answer and intent not in ("system", "admin_task"):
            try:
                _profile = mem.get("profile", {})
                _user_name = _profile.get("name", "")
                _is_first = len(mem.get("history", [])) <= 1  # 只有剛加入的 user msg
                _tone = self._personality.select_tone(intent, _profile, _is_first)
                answer = self._personality.wrap_response(
                    answer, _tone, intent,
                    user_name=_user_name,
                    is_first_visit=_is_first,
                )
            except Exception as _pe:
                logger.debug(f"[Gateway] PersonalityEngine wrap 失敗：{_pe}")

        # 記錄
        elapsed_ms = int((time.time() - start_time) * 1000)
        self.recorder.record(
            user_id=user_id, squad_key=intent.replace("+", ","), intent=intent,
            query=text, response=answer[:500], response_time_ms=elapsed_ms
        )

        # 回應寫入快取（有 RAG 的法規回應才快取，避免快取幻覺）— 快取存原始回覆（含語調）
        if self._cache and answer and len(answer) > 30 and intent != "parcel_query":
            _has_rag = bool(getattr(self, "_last_rag_chunks", None))
            self._cache.set(intent, text, answer, has_rag=_has_rag)

        # 更新記憶
        mem["history"].append({"role": "assistant", "content": answer})
        self._save_user(user_id, mem)

        # 長期記憶：第 3 輪開始，每 3 輪背景壓縮一次 summary（不卡主流程）
        turn_count = len(mem["history"]) // 2
        if turn_count >= 3 and turn_count % 3 == 0:
            _fire_and_forget(self._update_summary(user_id, mem))

        # 使用配額
        if not quota.get("unlimited"):
            self.permission.use_quota(user_id)

        # 缺口偵測（同步，flag_gap 是純 SQLite 寫入）
        if self.gap_manager and intent not in ("casual", "system"):
            gap_reason = self._detect_gap(text, answer)
            if gap_reason:
                try:
                    self.gap_manager.flag_gap(user_id, text, answer, gap_reason)
                    # 自我進化：缺口偵測到 → 背景觸發 SkillHunter + it_evolution
                    _fire_and_forget(self._background_skill_scan(
                        f"[{intent}] {text[:80]} — 能力缺口: {gap_reason}"
                    ))
                    logger.info(f"[Gateway] 自我進化觸發: intent={intent}, gap={gap_reason}")
                except Exception as _e:
                    logger.warning(f"[Gateway] 缺口標記失敗: {_e}")

        # 知識缺口偵測：法規/流程類回應 → 背景寫入 knowledge_gaps 表
        if intent in ("legal", "architecture", "drawing_review", "general") and answer:
            try:
                import sys as _kgs
                _tools_path = str(pathlib.Path(BASE_DIR) / "tools")
                if _tools_path not in _kgs.path:
                    _kgs.path.insert(0, _tools_path)
                from tools.knowledge_gap_detector import detect_and_log as _detect_gap_kl
                _fire_and_forget(_detect_gap_kl(text, answer, user_id))
            except Exception as _kge:
                logger.debug(f"[KnowledgeGap] 偵測模組載入失敗：{_kge}")

        return answer
    
    async def handle_file(self, user_id: str, file_name: str, file_bytes: bytes) -> str:
        """處理檔案"""
        result = await self.file_handler.handle(file_name, file_bytes, user_id)

        if result.get("pending_action"):
            # 把建議 squad 對應到 intent key
            suggested = result.get("suggested_squad", result.get("pending_squad", ""))
            squad_intent_map = {
                "財務群": "finance", "financial": "finance", "finance": "finance",
                "法規群": "legal",   "regulatory": "legal",  "legal": "legal",
                "設計群": "design",  "design": "design",
                "行政群": "project", "admin": "project",
            }
            squad_intent = next(
                (v for k, v in squad_intent_map.items() if k in suggested), "general"
            )
            self._file_context[user_id] = {
                "file_name":          result.get("file_name", file_name),
                "content":            result.get("content", ""),
                "pages":              result.get("pages", 0),
                "history":            [],
                "pending_action":     result.get("pending_type", "forward_to_squad"),
                "pending_squad":      squad_intent,
                "pending_squad_name": suggested or "對應智能群",
                "land_data":          result.get("land_data"),   # None = 非地籍資料，{} = 地籍但 OCR 空
                "xlsx_path":          result.get("xlsx_path", ""),
                "ready_for_calc":     result.get("ready_for_calc", False),
                "doc_type":           result.get("doc_type", ""),  # ← 新增，供後續補充偵測
                "dxf_path":           result.get("dxf_path", ""),  # DXF 轉換路徑
                "cadastral_parse":    result.get("cadastral_parse"),  # 地籍圖 OCR 結果
                "drawing_data":       result.get("drawing_data"),     # 圖說 OCR 解析結果
            }

        # ── Agent 閱讀解析結果，自然提問（不使用 hardcode 模板）────────────────
        return await self._agent_file_response(user_id, file_name, result)
    
    async def _agent_file_response(self, user_id: str, file_name: str, result: dict) -> str:
        """
        智能體閱讀檔案解析結果後，自然提問。
        不使用任何 hardcode 模板，完全由 LLM 根據解析內容生成。
        """
        import os as _os, json as _json

        doc_type   = result.get("doc_type", result.get("classified_type", ""))
        land       = result.get("land_data") or {}
        content    = result.get("content", "")[:800]   # 前段原文供 LLM 參考
        dxf_path   = result.get("dxf_path", "")
        xlsx_path  = result.get("xlsx_path", "")

        # ── 組合「已解析到什麼」供 LLM 判斷 ─────────────────────────────────
        # parcels 格式：[{"parcel_no":"313","area_m2":0,...}] 或 ["313","314"]
        raw_parcels = land.get("parcels") or []
        parcel_nos  = [
            p["parcel_no"] if isinstance(p, dict) else str(p)
            for p in raw_parcels
        ]
        ownerships = land.get("ownerships") or []

        parsed_parts: list[str] = []
        if land.get("city") or land.get("district"):
            parsed_parts.append(
                f"地點：{land.get('city','')}{land.get('district','')}{land.get('section','')}"
            )
        if parcel_nos:
            parsed_parts.append(f"地號：{', '.join(parcel_nos)}")
        if land.get("site_area_total", 0) > 0:
            parsed_parts.append(f"面積：{land['site_area_total']:.2f} ㎡")
        if land.get("land_use_zone"):
            parsed_parts.append(f"使用分區：{land['land_use_zone']}")
        if ownerships:
            owners = [o.get("owner","") if isinstance(o, dict) else "" for o in ownerships]
            owners = [o for o in owners if o]
            parsed_parts.append(f"所有權人：{len(ownerships)} 筆" +
                                 (f"（{', '.join(owners[:3])}{'...' if len(owners)>3 else ''}）" if owners else ""))

        _tier = self.tier_gate.get_user_tier(user_id) if self.tier_gate else "tier_basic"
        _tier_ok = _tier in ("tier_mid", "tier_pro", "super_admin", "admin")
        dxf_download_url = ""   # 供後續 Flex Message 使用

        # ── DXF 放樣圖（地籍圖謄本限定，程式碼拼接 URL，不給 LLM）─────────
        if dxf_path and _os.path.exists(dxf_path):
            if _tier_ok:
                _token = _register_file(dxf_path)
                dxf_download_url = _get_download_url(_token)
                if _tier in ("tier_pro", "super_admin", "admin"):
                    _fire_and_forget(self._route_dxf_to_squad04(
                        user_id, dxf_path, land
                    ))

        # ── Excel 謄本（後台存檔，不傳給用戶）───────────────────────────────
        if xlsx_path and _os.path.isfile(xlsx_path):
            logger.info(f"[Gateway] 謄本 Excel 已後台存檔：{xlsx_path} user={user_id}")

        # ── 地籍資料（謄本）：直接確認入庫，不追問缺漏欄位或用途 ─────────────
        _is_cadastral_upload = (
            doc_type == "地籍資料"
            or result.get("land_data") is not None
        )
        if _is_cadastral_upload:
            if parsed_parts:
                llm_text = "謄本已解析入庫。" + "　".join(parsed_parts)
            else:
                llm_text = f"「{file_name}」已入庫，部分欄位請至後台 Excel 確認。"
            if xlsx_path and _os.path.isfile(xlsx_path):
                llm_text += f"\n後台 Excel：{_os.path.basename(xlsx_path)}"
            if dxf_download_url:
                llm_text += f"\n\nDXF 放樣圖下載：{dxf_download_url}"
            elif dxf_path and _os.path.exists(dxf_path) and not _tier_ok:
                llm_text += "\n\n（DXF 放樣圖已生成，升級進階方案可下載）"
            return llm_text

        # ── 非地籍資料：計算缺漏欄位，LLM 自然提問 ──────────────────────────
        missing_parts: list[str] = []
        if not parcel_nos:
            missing_parts.append("地號")
        if not land.get("site_area_total") and result.get("doc_type") != "cadastral":
            missing_parts.append("面積")
        if not land.get("land_use_zone"):
            missing_parts.append("使用分區")

        # ── 組合給 LLM 的 context（不含任何 URL，防止幻覺）─────────────────
        context_lines = [f"文件名稱：{file_name}"]
        if doc_type:
            context_lines.append(f"文件類型：{doc_type}")
        if parsed_parts:
            context_lines.append("已解析：" + "　".join(parsed_parts))
        if missing_parts:
            context_lines.append("尚缺：" + "、".join(missing_parts))
        if content:
            context_lines.append(f"文件前段原文（供參考）：\n{content}")
        if dxf_download_url:
            context_lines.append("備註：地籍圖 DXF 放樣圖已生成，系統將自動附上下載按鈕。")

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 Naomi，建築事務所 AI 助理，口吻像資深同事。\n"
                    "用戶剛上傳了一份文件，你已閱讀完畢。\n"
                    "請根據解析結果：\n"
                    "1. 用一句話說明你讀到的核心資訊\n"
                    "2. 若有缺漏，自然地問用戶補充\n"
                    "3. 問清楚這份文件的用途（申請建照？室裝？土地買賣？其他？）\n"
                    "語氣自然，不要用表格或條列，控制在 120 字以內。\n"
                    "絕對不要在回覆中自己產生任何網址或連結。"
                ),
            },
            {"role": "user", "content": "\n".join(context_lines)},
        ]

        try:
            _res = await self.brain.call(BrainRole.ANALYST, messages, max_tokens=250)
            llm_text = _res.get("content", "").strip() or "檔案已收到，請問這份文件的用途是？"
        except Exception as _e:
            logger.warning(f"[Gateway] _agent_file_response LLM 失敗：{_e}")
            llm_text = f"收到「{file_name}」，請問這份文件的用途是？"

        # ── URL 由程式碼拼接（不經 LLM，避免幻覺）──────────────────────────
        if dxf_download_url:
            llm_text += f"\n\nDXF 放樣圖下載：{dxf_download_url}"
        elif dxf_path and _os.path.exists(dxf_path) and not _tier_ok:
            llm_text += "\n\n（DXF 放樣圖已生成，升級進階方案可下載）"

        return llm_text

    async def _route_dxf_to_squad04(self, user_id: str, dxf_path: str, land_ctx: dict):
        """背景：將 DXF + 地籍資料送交 Squad04 設計智能體分析法規退縮等"""
        try:
            import os as _os
            task_desc = (
                f"地籍圖 DXF 已轉換完成，請進行基地分析。\n"
                f"DXF 路徑：{_os.path.basename(dxf_path)}\n"
                f"地號：{', '.join(land_ctx.get('parcels', []) or ['未知'])}\n"
                f"基地面積：{land_ctx.get('site_area_total', 0):.2f} ㎡\n"
                f"使用分區：{land_ctx.get('land_use_zone', '待查')}\n"
                "請查詢適用法規退縮、建蔽率、容積率，並給出基地開發初步建議。"
            )
            await self.boss_agent.handle(
                user_id=user_id,
                task=task_desc,
                context={"intent": "design", "dxf_path": dxf_path, **land_ctx},
            )
            logger.info(f"[Gateway] Squad04 地籍分析已啟動 user={user_id}")
        except Exception as _e:
            logger.warning(f"[Gateway] Squad04 背景路由失敗：{_e}")

    # ── UPIS 下載狀態查詢（直接讀 SQLite，防止 LLM 幻覺）──────────────────────

    _STATUS_TRIGGERS = [
        "下載完成", "下載好了", "下載了嗎", "下載狀態", "載完了嗎",
        "入庫完成", "入庫了嗎", "入庫好了", "入庫狀態",
        "下載進度", "幾份完成", "完成了嗎", "好了嗎", "做完了嗎",
        "計畫書狀態", "PDF 狀態", "pdf狀態",
    ]

    @staticmethod
    def _is_status_query(text: str) -> bool:
        txt = text.strip().lower()
        return any(kw in txt for kw in ArchGateway._STATUS_TRIGGERS)

    @staticmethod
    def _handle_upis_status(text: str) -> str:
        """讀 SQLite upis_ingested 回報真實狀態，不讓 LLM 猜。"""
        try:
            import sqlite3 as _sq
            from pathlib import Path as _P
            db = _P(__file__).parent / "database" / "naomi_main.db"
            conn = _sq.connect(str(db))
            rows = conn.execute(
                "SELECT projnum, projname, status, chunks, ingested_at, error_msg "
                "FROM upis_ingested ORDER BY ingested_at DESC"
            ).fetchall()
            conn.close()
        except Exception as e:
            return f"⚠️ 無法查詢狀態：{e}"

        if not rows:
            return "目前沒有任何 UPIS 計畫書下載記錄。"

        done     = [r for r in rows if r[2] == "done"]
        ing      = [r for r in rows if r[2] == "ingesting"]
        dl       = [r for r in rows if r[2] == "downloading"]
        failed   = [r for r in rows if r[2] == "failed"]

        lines = [f"📊 UPIS 計畫書入庫狀態（共 {len(rows)} 筆）\n"]

        if ing or dl:
            in_prog = ing + dl
            lines.append(f"⏳ 進行中（{len(in_prog)} 筆）：")
            for r in in_prog:
                label = "入庫中" if r[2] == "ingesting" else "下載中"
                lines.append(f"  [{label}] {r[0]} {r[1][:30] if r[1] else ''}")

        if done:
            lines.append(f"\n✅ 已完成（{len(done)} 筆）：")
            for r in done:
                lines.append(f"  {r[0]} {r[1][:25] if r[1] else ''} — {r[3]} chunks")

        if failed:
            lines.append(f"\n❌ 失敗（{len(failed)} 筆）：")
            for r in failed:
                err = r[5][:40] if r[5] else ""
                lines.append(f"  {r[0]} {err}")

        if not ing and not dl:
            if len(done) == len(rows):
                lines.append("\n全部入庫完成 ✅")
            else:
                lines.append(f"\n（背景作業已停止，{len(failed)} 筆失敗需重新執行）")

        return "\n".join(lines)

    # ── 地號查詢 ─────────────────────────────────────────────────────────────

    async def _handle_parcel_query(self, text: str, mem: dict, user_id: str = "") -> str:
        """
        地號查詢（intent == parcel_query 時進入）
        流程：LLM 萃取地號資訊 → GIS 使用分區 → Squad03 法規 → LLM 統整
        """
        # ── LLM 萃取地號資訊（不用 regex，讓模型理解語意）────────────────
        extract_prompt = (
            "從以下文字中萃取土地地號資訊，回傳純 JSON，不要任何說明文字。\n"
            "格式：{\"city\": \"\", \"district\": \"\", \"section\": \"\", \"parcel\": \"\"}\n"
            "city=縣市（如台北市）、district=區鄉鎮市（如松山區）、"
            "section=地段含小段（如寶清段五小段）、parcel=地號數字（如70）。\n"
            "無法判斷的欄位留空字串。\n\n"
            f"文字：{text}"
        )
        extracted = {}
        try:
            res = await self.brain.call_skill(
                "general",
                [{"role": "user", "content": extract_prompt}],
                max_tokens=100,
            )
            import json as _j, re as _re_j
            raw_content = res.get("content", "")
            # 嘗試直接 parse；失敗則用 regex 從 markdown 包裝中提取 JSON
            try:
                extracted = _j.loads(raw_content)
            except Exception:
                _jm = _re_j.search(r'\{[^{}]+\}', raw_content, _re_j.DOTALL)
                if _jm:
                    extracted = _j.loads(_jm.group(0))
                else:
                    raise ValueError(f"No JSON in: {raw_content[:80]}")
        except Exception as _e:
            logger.warning(f"[Gateway] 地號萃取失敗：{_e}")
            return ""

        city     = extracted.get("city", "").strip()
        district = extracted.get("district", "").strip()
        section  = extracted.get("section", "").strip()
        parcel   = extracted.get("parcel", "").strip()

        # 從 session 補充缺漏的 city / 從上一輪 land_ctx 補充缺漏欄位
        _prev_ctx = mem.get("land_ctx", {})
        if not city:
            city = _prev_ctx.get("city", "") or mem.get("profile", {}).get("project_city", "")
        if not district:
            district = _prev_ctx.get("district", "")
        if not section:
            section = _prev_ctx.get("section", "")
        if not parcel:
            parcel = _prev_ctx.get("parcel", "")
        if not parcel:
            return ""   # 連地號都沒有，不是地號查詢

        location = f"{city}{district}{section} {parcel} 地號"
        logger.info(f"[GISQuery] 地號查詢：{location}")

        # ── Step 1：GIS 查詢使用分區 ────────────────────────────────────────
        gis_result: dict = {}
        zone = ""
        try:
            from utils.gis_query import GISQuery, _lookup_far_bcr
            gis    = GISQuery()
            gis_result = await gis.query(
                city=city, district=district,
                section=section, parcel_no=parcel,
            )
            zone = gis_result.get("zone", "")
        except Exception as _e:
            logger.warning(f"[Gateway] GIS 查詢失敗：{_e}")

        bcr = gis_result.get("bcr")
        far = gis_result.get("far")
        src = gis_result.get("source", "")

        # ── Step 2：UPIS 查適用計畫 → 自動下載未入庫的計畫 PDF ─────────────
        # 注意：必須在 GIS early-return 之前執行，確保地號查詢不被跳過
        upis_plans = []
        upis_summary = ""
        _lat = gis_result.get("lat", 0.0)
        _lng = gis_result.get("lng", 0.0)
        try:
            from utils.upis_fetcher import UPISFetcher
            _upis = UPISFetcher()
            if _lat and _lng:
                # 有座標：直接用座標查
                upis_plans = await _upis.get_plans_by_coord(_lat, _lng)
            elif district and section and parcel:
                # 座標取不到：改用地號查（台北市 UPIS API 直查）
                import re as _re
                _sec1 = _re.sub(r"段.*", "", section)
                _sec2_m = _re.search(r"段(.+?)小段", section)
                _sec2 = _sec2_m.group(1) if _sec2_m else "一"
                logger.info(f"[Gateway] UPIS 地號查詢：{district} {_sec1}段{_sec2}小段 {parcel}地號")
                upis_plans = await _upis.get_plans_by_land(
                    district=district, sec1=_sec1, sec2=_sec2, land_no=parcel
                )
            if upis_plans:
                _fire_and_forget(_upis.ensure_ingested(upis_plans, notify_user_id=user_id))
                upis_summary = _upis.plans_summary(upis_plans)
        except Exception as _ue:
            logger.warning(f"[Gateway] UPIS 查詢失敗：{_ue}")

        # ── 儲存地號上下文到 session（供後續對話繼續使用）──────────────────────
        _zone_save = zone or _prev_ctx.get("zone", "")
        mem["land_ctx"] = {
            "city": city, "district": district, "section": section,
            "parcel": parcel, "zone": _zone_save,
            "roads": mem.get("land_ctx", {}).get("roads", []),
        }
        self._save_user(user_id, mem)

        # GIS 查不到使用分區
        if not zone:
            # 從上一輪補充分區
            zone = _prev_ctx.get("zone", "")

        if not zone:
            if upis_plans:
                # 判斷是否已全部入庫
                n_ingested = sum(1 for p in upis_plans if p.ingested)
                n_total    = len(upis_plans)
                n_pending  = sum(1 for p in upis_plans if p.has_pdf and not p.ingested)

                if n_pending == 0:
                    # 全部已入庫 → 直接問分區，不顯示下載清單
                    return (
                        f"查詢 {location}⋯\n"
                        f"{n_ingested} 份都市計畫書已入庫完成，可直接查詢法規。\n\n"
                        f"使用分區無法自動取得，請告訴我分區名稱（如「第三種住宅區」），"
                        f"我會立即查詢建蔽率、容積率等管制規定。"
                    )
                else:
                    # 有未入庫 → 顯示清單 + 進度
                    return (
                        f"查詢 {location}⋯\n"
                        f"找到 {n_total} 份計畫書（{n_ingested} 份已入庫，{n_pending} 份背景下載中）。\n\n"
                        f"{upis_summary}\n\n"
                        f"使用分區無法自動取得，請告訴我分區名稱（如「第三種住宅區」），"
                        f"我會立即查詢建蔽率、容積率等管制規定。"
                    )
            else:
                # UPIS 也查不到 → 完全手動
                return (
                    f"查詢 {location}⋯\n"
                    f"目前無法自動取得使用分區，請至以下系統手動查詢後告訴我分區名稱，\n"
                    f"我會立即查詢適用法規：\n\n"
                    f"台北市：https://zone.udd.gov.taipei/ZoneSearch.aspx\n"
                    f"全國：https://luz.nlma.gov.tw/"
                )

        # ── Step 3：Squad03 查詢法規（含已入庫的 UPIS 計畫）─────────────────
        law_chunks: list = []
        try:
            import importlib.util as _ilu, sys as _sys, pathlib as _pl
            _m04_path = (
                _pl.Path(__file__).parent
                / "squads" / "03_regulatory_intel" / "member_04_searcher.py"
            )
            if "member_04_searcher" not in _sys.modules:
                _spec = _ilu.spec_from_file_location("member_04_searcher", _m04_path)
                _mod  = _ilu.module_from_spec(_spec)
                _sys.modules["member_04_searcher"] = _mod
                _spec.loader.exec_module(_mod)
            else:
                _mod = _sys.modules["member_04_searcher"]

            searcher   = _mod.LegalSearcher(None)
            query_text = (
                f"{city}{district} {zone} "
                f"建蔽率 容積率 退縮 高度 停車 土地使用管制"
            )
            search_res = await searcher.search("land_reg", query_text, n=8)
            law_chunks = search_res.get("chunks", [])
        except Exception as _e:
            logger.warning(f"[Gateway] Squad03 法規查詢失敗：{_e}")

        # ── Step 4：組合 context → LLM 統整完整回覆 ─────────────────────────
        gis_summary = f"地點：{location}\n使用分區：{zone}"
        if bcr is not None:
            gis_summary += f"\n建蔽率（通則基準）：{int(bcr*100)}%"
        if far is not None:
            gis_summary += f"\n容積率（通則基準）：{int(far*100)}%"

        plan_context = ""
        if upis_summary:
            plan_context = f"\n\n【適用都市計畫清單】\n{upis_summary}"
            not_yet = [p for p in upis_plans if p.has_pdf and not p.ingested]
            if not_yet:
                plan_context += (
                    f"\n（以下計畫正在背景下載入庫，下次查詢將更完整：\n"
                    + "\n".join(f"  • {p.projname}" for p in not_yet[:3]) + "）"
                )

        law_context = ""
        if law_chunks:
            law_context = "\n\n【法規條文（向量庫查詢結果）】\n" + "\n---\n".join(law_chunks[:6])
        else:
            law_context = "\n\n【法規條文】目前向量庫尚無此地區細部計畫條文，以建築技術規則通用基準回答。"

        messages = [
            {
                "role": "system",
                "content": (
                    "你是 Naomi，資深建築師事務所 AI 助理。\n"
                    "用戶查詢了一筆土地的都市計畫法規管制。\n"
                    "請依下方 GIS 資料與法規條文，提供完整分析：\n"
                    "1. 使用分區確認\n"
                    "2. 建蔽率 / 容積率（優先以法規條文為準，條文來源要標明）\n"
                    "3. 建築退縮規定（道路側 / 鄰地側，列出公尺數與條文）\n"
                    "4. 高度限制（如有）\n"
                    "5. 停車、綠化、法定空地等其他管制\n"
                    "6. 重要提醒（細部計畫若有特殊規定以細部計畫為準）\n\n"
                    "規則：\n"
                    "- 法規條文優先於通則基準數字\n"
                    "- 每項數字必須附條文出處（如：依地方土地使用分區管制自治條例）\n"
                    "- 若向量庫無相關條文，明確說明「依建築技術規則通用基準，請查當地細部計畫確認」\n"
                    "- 禁止自行捏造條文或數字"
                ),
            },
            {
                "role": "user",
                "content": gis_summary + plan_context + law_context,
            },
        ]

        try:
            res    = await self.brain.call(BrainRole.ANALYST, messages, max_tokens=1200)
            answer = res.get("content", "").strip()
        except Exception as _e:
            logger.warning(f"[Gateway] 地號查詢 LLM 失敗：{_e}")
            answer = gis_summary
            if law_chunks:
                answer += "\n\n相關法規：\n" + "\n".join(f"• {c[:150]}" for c in law_chunks[:3])

        # 來源說明
        src_note = {
            "taipei_api": "資料來源：台北市都市計畫分區查詢系統",
            "shp_local":  "資料來源：本地 SHP 圖資",
            "nlsc_survey":"⚠️ 使用分區來源為 NLSC 現況調查（非正式分區），請人工確認",
            "table_only": "⚠️ 建蔽率/容積率為通用基準，以當地細部計畫為準",
        }.get(src, "")
        if src_note:
            answer += f"\n\n（{src_note}）"

        return answer

    async def _handle_file_followup(self, user_id: str, text: str) -> str:
        """處理檔案後續對話 — 多輪、自然語氣、LLM 主導"""
        mem = self._load_user(user_id)
        ctx = self._file_context.get(user_id, {})
        file_name = ctx.get("file_name", "")
        content = ctx.get("content", "")
        pages = ctx.get("pages", 0)
        history = ctx.get("history", [])

        # ── LLM 判斷使用者意圖（取代關鍵字陣列）────────────────────────────
        pending_action = ctx.get("pending_action", "")
        doc_type       = ctx.get("doc_type", "")
        _classify_prompt = (
            f"使用者剛上傳了檔案「{file_name}」（類型：{doc_type or '未知'}），"
            f"{'有待確認動作：' + pending_action if pending_action else ''}。\n"
            f"使用者現在說：「{text}」\n\n"
            "請判斷使用者的意圖，只能輸出以下英文詞之一，不得輸出其他任何文字：\n"
            "ingest / confirm / cancel / question / case_learn / compliance / site_plan / floor_compare\n\n"
            "定義：\n"
            "- confirm：使用者同意、確認、授權執行待辦動作（例：好、是、OK、去做、沒問題、對的、繼續、執行）\n"
            "- cancel：使用者拒絕或取消（例：不用、算了、取消、不對、停）\n"
            "- ingest：使用者要存檔或入庫\n"
            "- question：使用者在問問題\n"
            "- case_learn：使用者要將圖說加入案例學習庫（例：學習、加入案例、存案例）\n"
            "- compliance：使用者要做合規/法規檢討（例：合規、法規檢討、有沒有違規）\n"
            "- site_plan：使用者要生成配置平面圖（例：配置平面圖、疊合地籍、畫配置圖）\n"
            "- floor_compare：使用者要做室內各層比對（例：室內比對、各層比對、室內圖）\n\n"
            "只輸出一個詞："
        )
        _file_intent = "question"
        _local_conf  = 0.0
        # ① 本地分類器（< 5ms，離線，語意向量）
        try:
            from utils.intent_classifier import get_classifier
            _local = get_classifier().predict(text)
            _local_conf = _local["confidence"]
            if _local_conf >= 0.80:
                _file_intent = _local["intent"]
                logger.debug(f"[FileFollowup] 本地分類：{_file_intent} ({_local_conf:.2f})")
        except Exception as _ce:
            logger.debug(f"[FileFollowup] 本地分類失敗：{_ce}")

        # ② 信心不足（< 0.80）→ 升級到 LLM 語意判斷
        if _local_conf < 0.80:
            try:
                _r = await self.brain.call_skill(
                    "general",
                    [{"role": "user", "content": _classify_prompt}],
                    max_tokens=30,
                )
                _raw = _r.get("content", "").strip().lower()
                if   "case_learn"    in _raw: _file_intent = "case_learn"
                elif "compliance"    in _raw: _file_intent = "compliance"
                elif "site_plan"     in _raw: _file_intent = "site_plan"
                elif "floor_compare" in _raw: _file_intent = "floor_compare"
                elif "ingest"        in _raw: _file_intent = "ingest"
                elif "confirm"       in _raw: _file_intent = "confirm"
                elif "cancel"        in _raw: _file_intent = "cancel"
                logger.debug(f"[FileFollowup] LLM 分類：{_file_intent}（本地信心={_local_conf:.2f}）")
            except Exception:
                pass

        # 入庫
        if _file_intent == "ingest":
            _is_land = (ctx.get("doc_type") == "地籍資料" or ctx.get("land_data") is not None)
            if _is_land:
                xlsx = ctx.get("xlsx_path", "")
                msg = f"「{file_name}」已入庫完成。"
                if xlsx:
                    import os as _os2
                    msg += f" Excel 已存至後台（{_os2.path.basename(xlsx)}）。"
                self._file_context.pop(user_id, None)
                return msg

        is_confirm = (_file_intent == "confirm")
        is_done    = (_file_intent == "cancel")

        # 使用者確認「交給財務群/法規群處理」或「容積估算」
        if is_confirm and ctx.get("pending_action"):
            action     = ctx["pending_action"]
            squad      = ctx.get("pending_squad", "")
            squad_name = ctx.get("pending_squad_name", "對應智能群")

            ctx.pop("pending_action", None)
            ctx.pop("pending_squad", None)
            ctx.pop("pending_squad_name", None)

            # ── 地籍圖：確認後自動查都市計畫 ─────────────────────────────
            if action == "query_urban_plan":
                cad = ctx.get("cadastral_parse") or {}
                city     = cad.get("city", "")
                district = cad.get("district", "")
                section  = cad.get("section", "")
                parcels  = cad.get("parcel_nos", [])

                if not (section or parcels):
                    self._file_context[user_id] = ctx
                    return "地號或地段資訊不足，請補充後再試（例如：台北市中正區寶清段700地號）"

                # 呼叫 _handle_parcel_query 或直接查 UPIS/NTPC
                first_parcel = parcels[0] if parcels else ""
                query_text   = f"{city}{district}{section}{first_parcel}地號"
                try:
                    answer = await self._handle_parcel_query(query_text, mem, user_id)
                    # 多筆地號補充查詢
                    if len(parcels) > 1:
                        answer += f"\n\n（共識別 {len(parcels)} 筆地號：{'、'.join(parcels[:6])}）"
                    self._file_context[user_id] = ctx
                    return answer
                except Exception as e:
                    logger.error(f"[FileFollowup] query_urban_plan 失敗：{e}")
                    self._file_context[user_id] = ctx
                    return f"都市計畫查詢失敗：{e}\n請嘗試直接輸入「{query_text}」"

            # ── 圖說：合規檢討 / 配置平面圖 / 室內比對 ───────────────────────
            if action == "drawing_review":
                drawing_data = ctx.get("drawing_data") or {}
                sub = text.strip()

                # 合規檢討：顯示 RAG 法規條文對照
                if _file_intent == "compliance" or any(k in sub for k in ["合規", "法規", "檢討", "review"]):
                    ref  = drawing_data.get("reflection", {})
                    rag  = ref.get("rag_refs", [])
                    impr = ref.get("improved_summary", "")
                    notes = ref.get("compliance_notes", [])
                    lines = ["【合規檢討報告】", "─" * 30]
                    if notes:
                        for n in notes:
                            lines.append(f"  ⚠️ {n}")
                    if rag:
                        lines.append("【引用法規條文】")
                        for r in rag:
                            lines.append(f"  📋 {r}")
                    if impr:
                        lines.append("【改善建議（參照入庫法規）】")
                        lines.append(impr)
                    if not notes and not impr:
                        lines.append("算法檢查未發現問題，圖說資料完整性良好。")
                    self._file_context[user_id] = ctx
                    return "\n".join(lines)

                # 配置平面圖：需要地籍資料才能疊合
                if _file_intent == "site_plan" or any(k in sub for k in ["配置", "平面圖", "套用", "地籍", "疊合"]):
                    mem_land = mem.get("land_ctx", {})
                    cad_data = ctx.get("cadastral_parse") or {}
                    dxf_path = ctx.get("dxf_path", "")

                    if not cad_data and not mem_land and not dxf_path:
                        self._file_context[user_id] = ctx
                        return (
                            "要生成配置平面圖，需要先提供地籍資料（地籍圖謄本或輸入地號）。\n\n"
                            "請上傳地籍圖，或輸入「台北市○○區○○段○○地號」格式。"
                        )
                    try:
                        import sys as _sys, os as _os2
                        _sd = str(pathlib.Path(__file__).parent / "squads" / "04_architectural_design" / "scripts")
                        if _sd not in _sys.path:
                            _sys.path.insert(0, _sd)
                        from floor_plan_composer import compose_site_plan, dxf_to_wkt

                        # 優先用 DXF 取得精確邊界 WKT
                        wkt = ""
                        if dxf_path and _os2.path.exists(dxf_path):
                            wkt = dxf_to_wkt(dxf_path)
                        if not wkt:
                            wkt = mem_land.get("wkt") or cad_data.get("wkt", "")

                        city = cad_data.get("city") or mem_land.get("city", "")

                        svg_path = await compose_site_plan(
                            drawing_data=drawing_data,
                            parcel_wkt=wkt,
                            city=city,
                        )
                        self._file_context[user_id] = ctx
                        if svg_path:
                            import os as _os3
                            fname = _os3.path.basename(svg_path)
                            return (
                                f"配置平面圖已生成：{fname}\n"
                                f"（完整路徑：{svg_path}）\n\n"
                                f"可用 Inkscape / AutoCAD / 瀏覽器開啟 SVG 檔。\n"
                                f"{'⚠️ 未取得地籍精確邊界，以估算面積示意，請提供謄本確認。' if not wkt else ''}"
                            )
                        return "配置平面圖生成失敗，請確認地籍邊界資料是否含座標。"
                    except Exception as _fpe:
                        logger.error(f"[FileFollowup] 配置平面圖失敗：{_fpe}")
                        self._file_context[user_id] = ctx
                        return f"配置平面圖生成失敗：{_fpe}"

                # 案例學習：加入案例庫
                if _file_intent == "case_learn" or any(k in sub for k in ["學習", "加入案例", "案例庫", "存案例", "記錄案例"]):
                    try:
                        import sys as _sys
                        _tools = str(pathlib.Path(__file__).parent / "tools")
                        if _tools not in _sys.path:
                            _sys.path.insert(0, _tools)
                        from case_learning_pipeline import CaseLearningPipeline
                        pipeline = CaseLearningPipeline(hub=self._hub if hasattr(self, "_hub") else None)

                        # 從 file_context 取原始 bytes
                        _tmp_bytes = ctx.get("_raw_bytes")
                        _tmp_name  = ctx.get("file_name", "drawing.pdf")
                        if not _tmp_bytes:
                            self._file_context[user_id] = ctx
                            return "找不到原始圖檔，請重新上傳後再選擇「學習」。"

                        meta = {
                            "project_name": drawing_data.get("project_name") or _tmp_name,
                            "city":         mem.get("land_ctx", {}).get("city", ""),
                            "floors":       drawing_data.get("floors", 0),
                            "site_area_m2": drawing_data.get("site_area_m2", 0),
                        }
                        case_id = await pipeline.learn_from_image(
                            _tmp_bytes, meta=meta, file_name=_tmp_name
                        )
                        self._file_context[user_id] = ctx
                        if case_id:
                            count = pipeline.case_count()
                            return (
                                f"✅ 案例已加入學習庫\n"
                                f"案例ID：{case_id}\n"
                                f"目前案例庫：{count} 筆\n\n"
                                f"下次遇到相似基地條件，系統會自動參考此案例提供建議。"
                            )
                        return "案例學習失敗，請確認 Ollama 服務是否運行中。"
                    except Exception as _le:
                        logger.error(f"[FileFollowup] 案例學習失敗：{_le}")
                        self._file_context[user_id] = ctx
                        return f"案例學習失敗：{_le}"

                # 室內比對
                if _file_intent == "floor_compare" or any(k in sub for k in ["室內", "比對", "各層", "floor"]):
                    try:
                        import sys as _sys
                        _sd = str(pathlib.Path(__file__).parent / "squads" / "04_architectural_design" / "scripts")
                        if _sd not in _sys.path:
                            _sys.path.insert(0, _sd)
                        from floor_comparator import compare_floors
                        cmp_result = await compare_floors(drawing_data)
                        self._file_context[user_id] = ctx
                        return cmp_result.get("summary", "各層空間比對完成，請見後台報告。")
                    except Exception as _cpe:
                        self._file_context[user_id] = ctx
                        return f"各層比對失敗：{_cpe}"

                # 預設：使用者在問圖說相關問題 → LLM 自然回應
                self._file_context[user_id] = ctx
                _dd = drawing_data or {}
                _raw = _dd.get("raw_text", "")
                _has_content = len(_raw) > 50

                # 如果結構化解析全空但有原始文字，傳給 LLM 讓它直接讀
                _ctx_lines = [
                    f"使用者剛上傳了圖說「{file_name}」，說：「{text}」",
                ]
                if _dd.get("doc_type") and _dd["doc_type"] != "unknown":
                    _ctx_lines.append(f"圖說類型：{_dd['doc_type']}")
                if _dd.get("project_name"):
                    _ctx_lines.append(f"工程名稱：{_dd['project_name']}")
                if _dd.get("floors"):
                    _ctx_lines.append(f"樓層：{_dd['floors']}層")
                if _dd.get("total_area_m2"):
                    _ctx_lines.append(f"總樓地板面積：{_dd['total_area_m2']}㎡")
                if _dd.get("spaces"):
                    _space_names = [s.get("name","") if isinstance(s,dict) else getattr(s,"name","") for s in _dd["spaces"][:6]]
                    _ctx_lines.append(f"識別到的空間：{', '.join(s for s in _space_names if s)}")
                if _has_content:
                    _ctx_lines.append(f"\n【圖說原始文字片段（供參考）】\n{_raw[:600]}")
                if _dd.get("reflection", {}).get("compliance_notes"):
                    _ctx_lines.append(f"合規提醒：{_dd['reflection']['compliance_notes'][0]}")
                if not _has_content:
                    _ctx_lines.append("（圖說文字無法萃取，可能為影像掃描版）")

                try:
                    _r = await self.brain.call_skill(
                        "general",
                        [{
                            "role": "system",
                            "content": (
                                "你是 Naomi，建築事務所 AI 助理，口吻像資深同事。"
                                "使用者剛上傳建築圖說，請根據解析資訊自然回應。"
                                "若有原始文字片段，從中歸納能看出的重點（樓層、空間配置等）。"
                                "若什麼都解析不到，誠實說圖說格式特殊無法自動解析，請用戶補充說明。"
                                "不要逐條列出原始數字，用對話方式說明。"
                                "最後一句提示可說「合規檢討」、「配置平面圖」或「學習」繼續。"
                                "控制在 200 字以內，用繁體中文。"
                            ),
                        }, {"role": "user", "content": "\n".join(_ctx_lines)}],
                        max_tokens=250,
                    )
                    return _r.get("content", "").strip() or "圖說已收到，可說「合規檢討」、「配置平面圖」或「學習」繼續。"
                except Exception:
                    return "圖說已解析完成，你可以說「合規檢討」、「配置平面圖」或「學習」繼續。"

            # ── 地籍追問：容積估算 ─────────────────────────────────────────
            if action == "calc_far":
                land_data = ctx.get("land_data", {})
                xlsx_path = ctx.get("xlsx_path", "")
                ready     = ctx.get("ready_for_calc", False)
                if ready and land_data:
                    try:
                        import sys as _sys
                        _sd = str(pathlib.Path(BASE_DIR) / "squads" / "04_architectural_design" / "scripts")
                        if _sd not in _sys.path:
                            _sys.path.insert(0, _sd)
                        from preliminary_far import estimate_far
                        far_result = estimate_far(land_data)
                        answer = far_result.get("summary", "")
                        if not answer:
                            answer = f"基地面積 {land_data.get('site_area_total',0):.2f}㎡，" \
                                     f"使用分區 {land_data.get('land_use_zone','未知')}，" \
                                     f"容積率 {land_data.get('far',0)*100:.0f}%，" \
                                     f"建蔽率 {land_data.get('bcr',0)*100:.0f}%"
                        if xlsx_path:
                            import os as _os
                            answer += f"\n\nExcel 建檔：{_os.path.basename(xlsx_path)}"
                        self._file_context[user_id] = ctx
                        return answer
                    except Exception as e:
                        logger.warning(f"[FileFollowup] 容積估算失敗：{e}")
                        self._file_context[user_id] = ctx
                        return "容積估算模組載入失敗，請確認 preliminary_far.py 是否就緒。"
                else:
                    self._file_context[user_id] = ctx
                    return "目前地籍資料缺少使用分區或容積率數值，請補充都市計畫使用分區證明後再試。"

            # ── 一般文件交派 Squad ─────────────────────────────────────────
            real_result = f"已將「{file_name}」交由{squad_name}處理。"
            try:
                boss_result = await self.boss_agent.handle(
                    user_id=user_id,
                    task=f"分析這份文件：{file_name}\n\n{content[:3000]}",
                    context={"intent": squad, "history": history[-3:]}
                )
                boss_answer = boss_result.get("answer", "")
                if boss_answer:
                    real_result = f"已交由{squad_name}分析「{file_name}」：\n\n{boss_answer[:500]}"
            except Exception as e:
                logger.error(f"[FileFollowup] BossAgent 處理失敗: {e}")

            self._file_context[user_id] = ctx
            return real_result

        if is_done:
            self._file_context.pop(user_id, None)
            return "好的，有需要再找我！"

        # ── 地籍手動補充：偵測使用者提供地號 / 面積 / 使用分區 ────────────────
        # 條件：pending_action==calc_far（正常路徑），或 doc_type==地籍資料（confirm後），
        #        或 land_data key 存在（任何地籍資料上傳後的補充）
        _is_cadastral_ctx = (
            ctx.get("pending_action") == "calc_far"
            or ctx.get("doc_type") == "地籍資料"
            or ctx.get("land_data") is not None
            or bool(mem.get("land_ctx", {}).get("parcel"))  # 地號查詢後的後續追問
        )
        if _is_cadastral_ctx:
            land_data = ctx.get("land_data") or {}
            updated   = False

            # 偵測面積（先偵測，排除後續地號 regex 的誤抓）
            # 支援大小寫 M2/m2/M²/㎡/坪/平方公尺
            area_hit = re.search(
                r"([\d,.]+)\s*(坪|平方公尺|[Mm][²2]|㎡)", text
            )
            _area_nums: set[str] = set()  # 已辨識為面積的數字，排除地號誤抓
            if area_hit:
                area_val = float(area_hit.group(1).replace(",", ""))
                unit = area_hit.group(2)
                if unit == "坪":
                    area_val = round(area_val * 3.30579, 2)
                land_data["site_area_total"] = area_val
                _area_nums.add(area_hit.group(1).replace(",", "").split(".")[0])
                updated = True

            # 偵測地號（如「地號313」「313、314地號」「313-1地號」）
            # 排除已被辨識為面積的數字
            parcel_hits = [
                p for p in re.findall(r"(\d{2,6}(?:-\d+)?)\s*(?:地號|號)?", text)
                if p.split("-")[0] not in _area_nums
            ]
            if "地號" in text and parcel_hits:
                existing = land_data.get("parcels", [])
                new_parcels = [p for p in parcel_hits if p not in existing]
                if new_parcels:
                    land_data.setdefault("parcels", []).extend(new_parcels)
                    updated = True

            # 偵測使用分區（如「住二」「第二種住宅區」「商業區」）
            zone_hit = re.search(
                r"(住[一二三四]|商[一二三四]|工[一二三]|"
                r"第[一二三四]種住宅區|第[一二三四]種商業區|"
                r"住宅區|商業區|工業區|農業區|保護區|特定農業區)",
                text
            )
            if zone_hit:
                land_data["land_use_zone"] = zone_hit.group(1)
                # 同步更新 land_ctx（地號查詢的跨輪上下文）
                if "land_ctx" in mem:
                    mem["land_ctx"]["zone"] = zone_hit.group(1)
                updated = True

            # 偵測臨路資訊（如「八德路四段」「寶清路」）同步存入 land_ctx
            road_hits = re.findall(
                r"[^\s，。、]{2,6}(?:路|街|大道|幹道|北路|南路|東路|西路)"
                r"(?:[一二三四五六七八九十百零\d]+[段巷弄]?)?",
                text
            )
            if road_hits and "land_ctx" in mem:
                existing_roads = mem["land_ctx"].get("roads", [])
                for r in road_hits:
                    if r not in existing_roads:
                        existing_roads.append(r)
                mem["land_ctx"]["roads"] = existing_roads[:6]
                self._save_user(user_id, mem)

            if updated:
                ctx["land_data"] = land_data
                # 確認目前收集到的資料，告知還缺什麼
                has_parcels = bool(land_data.get("parcels"))
                has_area    = land_data.get("site_area_total", 0) > 0
                has_zone    = bool(land_data.get("land_use_zone"))
                missing = []
                if not has_parcels:
                    missing.append("地號")
                if not has_area:
                    missing.append("基地面積（坪數或平方公尺）")
                if not has_zone:
                    missing.append("使用分區（如住二、商業區等）")

                summary_lines = []
                if has_parcels:
                    summary_lines.append(f"地號：{', '.join(land_data['parcels'])} 共 {len(land_data['parcels'])} 筆")
                if has_area:
                    summary_lines.append(f"基地面積：{land_data['site_area_total']:.2f} ㎡")
                if has_zone:
                    summary_lines.append(f"使用分區：{land_data['land_use_zone']}")

                ctx["ready_for_calc"] = has_area and has_zone
                ctx["pending_action"] = "calc_far"
                self._file_context[user_id] = ctx

                reply = "收到，目前已登記：\n" + "\n".join(f"  {l}" for l in summary_lines)
                if missing:
                    reply += f"\n\n還需要：{' / '.join(missing)}"
                    reply += "\n請補充後我可以進行容積率概估。"
                else:
                    reply += "\n\n資料齊全，回覆「要」即可進行容積率概估。"
                return reply

        # 截取文件內容（避免超過 token 上限，取最多 8000 字）
        content_for_llm = content[:8000] if content else "（文件內容無法取得）"
        if len(content) > 8000:
            content_for_llm += f"\n\n（以上為節錄，完整文件共 {pages} 頁）"

        # 組合多輪歷史
        messages = [
            {
                "role": "system",
                "content": (
                    f"你是 Naomi，正在和用戶一起討論一份文件「{file_name}」。\n\n"
                    f"文件內容如下：\n{content_for_llm}\n\n"
                    f"重要規則：\n"
                    f"1. 只能根據上方文件內容回答，不得補充文件以外的知識或推測數字\n"
                    f"2. 如果文件中找不到答案，直接說「這份文件裡沒有這項規定」\n"
                    f"3. 語氣像資深同事，簡潔直接，不要用編號列表"
                )
            },
            *history[-6:],
            {"role": "user", "content": text},
        ]

        result = await self.brain.call(BrainRole.ANALYST, messages, max_tokens=1200)
        answer = result.get("content", "抱歉，我沒辦法處理這個問題，可以換個方式描述嗎？")

        # 更新對話歷史
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": answer})
        ctx["history"] = history[-12:]  # 最多保留 12 輪
        self._file_context[user_id] = ctx

        return answer
    
    async def _handle_squad_query(self, text: str, mem: Dict, alias_result: Dict) -> str:
        """
        處理「行政群那邊需要什麼」這類 Squad 查詢。
        LLM 收到明確的組織架構 context，不能引用法規資料庫。
        """
        if not alias_result:
            # vector router 命中 squad_query 但無具體 squad 對象 → 通用回答
            return await self._casual_reply(text, mem)
        squad_name = alias_result.get("squad_name", "")
        squad_id   = alias_result.get("squad_id", "")
        action     = alias_result.get("action", "query")
        profile    = mem.get("profile", {})
        name       = profile.get("name", "")
        greeting   = f"{name}，" if name else ""

        # 動作說明
        action_desc = {
            "status_check":       f"查詢 {squad_name} 目前的運作狀態與進度",
            "requirements_check": f"列出 {squad_name} 目前的需求、待辦事項、還需要什麼技能或資源",
            "query":              f"回答關於 {squad_name} 的問題",
            "report":             f"提供 {squad_name} 的摘要報告",
        }.get(action, f"回答關於 {squad_name} 的問題")

        # 各 Squad 的實際能力清單（真實資料）
        squad_capabilities = {
            "squad_03": "法規條文查詢、建築技術規則解析、都市計畫法規、室內裝修規定、向量庫 RAG 搜尋",
            "squad_04": "建築設計諮詢、平面配置規劃、坪數計算、戶數配置、採光通風分析",
            "squad_05": "室內設計諮詢、空間規劃、裝修流程、材料建議",
            "squad_06": "BIM 建模、Revit/ArchiCAD 支援、IFC 格式、3D 模型管理",
            "squad_07": "作品集整理、案例說明撰寫、Portfolio 管理",
            "squad_08": "日常營運管理、流程優化、SOP 建立",
            "squad_09": "行政事務管理、文件處理、行程安排、任務分配、排程自動化、工具整合",
            "squad_10": "行銷策略、社群經營、案例推廣、品牌形象",
            "squad_11": "財務分析、ROI 計算、投報表、公設比計算、預算管理",
            "squad_12": "網路資料爬取、資料收集、市場調研自動化",
            "it_evolution": "系統升級、新工具評估、AI 能力擴充、SkillHunter 技能掃描",
        }

        real_data = (
            f"{squad_name}（{squad_id}）能力範圍：\n"
            f"{squad_capabilities.get(squad_id, '尚未設定詳細能力清單')}\n\n"
            f"使用者問題：{text}"
        )

        system_prompt = (
            f"{get_org_context()}\n\n"
            f"你是 Naomi。請根據上方真實資料，{action_desc}。\n"
            f"規則：不可引用法規資料庫；不可捏造不存在的功能；繁體中文；最多120字；口語自然。"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text},
        ]
        result = await self.brain.call_skill(
            "admin_task", messages, max_tokens=200,
            real_data=real_data
        )
        answer = result.get("content", "").strip()
        return answer or f"{greeting}關於{squad_name}：{squad_capabilities.get(squad_id, '請稍後再試。')}"

    async def _handle_admin_task(self, user_id: str, task: str, mem: Dict) -> str:
        """處理能力強化/開發任務 — 先分析需求，再主動搜尋或建議方向"""
        profile = mem.get("profile", {})
        name = profile.get("name", "")
        greeting = f"{name}，" if name else ""

        # ── ActiveMonitor 查詢 / 手動觸發 ────────────────────────────────────
        _am = getattr(self, "_gateway", None) and getattr(self._gateway, "active_monitor", None)
        if not _am:
            _am = getattr(globals().get("gateway", None), "active_monitor", None)

        if _am:
            if any(kw in task for kw in ["排程狀態", "監控狀態", "active_monitor", "monitor status"]):
                return _am.format_status_report()

            for trigger_kw, job_name in [
                ("觸發法規", "law"), ("執行法規爬取", "law"), ("law_crawler", "law"),
                ("觸發自檢", "ska"), ("知識自檢", "ska"),
                ("觸發健康", "health"), ("health check", "health"),
                ("觸發市場", "market"), ("市場行情更新", "market"),
                ("觸發早報", "brief"),
            ]:
                if trigger_kw in task:
                    result = await _am.trigger(job_name)
                    status = result.get("status", "?")
                    err = result.get("error", "")
                    return (f"[ActiveMonitor] 手動觸發 {job_name} 完成\n"
                            f"狀態：{status}"
                            + (f"\n{err}" if err else ""))

        # ── 自然語言批准/拒絕技能提案 ──────────────────────────────────────
        is_approve = any(k in task for k in ["批准", "同意安裝", "可以安裝", "批准安裝", "同意", "裝吧", "安裝"])
        is_reject  = any(k in task for k in ["拒絕", "不要安裝", "取消安裝", "不裝", "跳過"])
        if (is_approve or is_reject) and _skill_hunter:
            pending = _skill_hunter.proposals.get_pending()
            if not pending:
                return f"{greeting}目前沒有待批准的提案，可以說「掃描技能」讓我去找新工具。"

            # ── 語意篩選：從句子提取主題關鍵字，比對提案 topic ──────────────
            def _match_proposals(text: str, proposals: list) -> list:
                """從自然語言中提取主題，篩選對應提案；無特定主題則返回全部"""
                # 各提案的 topic 關鍵字對照
                topic_aliases = {
                    "行政":   ["行政管理", "行政"],
                    "行政管理": ["行政管理", "行政"],
                    "通用":   ["通用工具", "通用"],
                    "通用工具": ["通用工具", "通用"],
                    "法規":   ["法規", "法令", "建築法"],
                    "財務":   ["財務", "金融", "投報"],
                    "排程":   ["排程", "行程"],
                    "案件":   ["案件", "專案"],
                    "全部":   None,   # None = 全選
                    "所有":   None,
                    "都":     None,
                }
                matched = []
                for kw, aliases in topic_aliases.items():
                    if kw in text:
                        if aliases is None:
                            return proposals   # 全選
                        matched += [p for p in proposals
                                    if any(a in p.get("gap_topic", "") for a in aliases)]
                # 未命中任何主題關鍵字 → 全選（保持向後相容）
                return matched if matched else proposals

            targets = _match_proposals(task, pending)

            if is_approve:
                results = []
                for p in targets:
                    r = await _skill_hunter.install_approved(p["proposal_id"])
                    results.append(r)
                topics = "、".join(p.get("gap_topic", p["proposal_id"]) for p in targets)
                return f"✅ {greeting}已批准安裝【{topics}】：\n" + "\n".join(results)
            else:
                for p in targets:
                    _skill_hunter.proposals.reject(p["proposal_id"])
                topics = "、".join(p.get("gap_topic", p["proposal_id"]) for p in targets)
                return f"❌ {greeting}已拒絕【{topics}】提案。"

        # ── 能力查詢：用 LLM 動態說明，避免 hardcode ──────────────────────
        _CAPABILITY_KW = [
            "可以被交辦", "可以做什麼", "可以做甚麼", "能做什麼", "能做甚麼",
            "你的能力", "你有什麼功能", "你有甚麼功能", "你能幫", "你的功能",
            "有哪些功能", "有什麼能力", "有甚麼能力", "能力範圍", "服務項目",
        ]
        if any(kw in task for kw in _CAPABILITY_KW):
            cap_system = (
                "你是 Naomi，建築事務所的 AI 總管，管理以下 10 個專業 Squad：\n"
                "Squad 03 法規智能：建蔽率/容積率計算、使用分區查詢、法條解釋、合規審查。\n"
                "Squad 04 建築設計：基地分析、量體配置、結構/立面建議、BIM數位化。\n"
                "Squad 05 室內設計：機能規劃、燈光設計、軟裝風格、室裝法規審查。\n"
                "Squad 06 BIM技術：衝突檢測、GIS整合、族庫搜尋、工程量化。\n"
                "Squad 07 專案組合：進度追蹤、文件管理、作品集整理、品質審查。\n"
                "Squad 08 營運能效：能效分析、綠建築認証、永續建材搜尋。\n"
                "Squad 09 整合行政：會議協調、合約審查、HR文書、採購比價。\n"
                "Squad 10 行銷公關：品牌文案、社群貼文、媒體稿。\n"
                "Squad 11 財務管理：ROI試算、造價估算、融資分析、風險評估。\n"
                "Squad 12 資料採集：市場行情、政府公開資料、爬蟲整合。\n"
                "另有系統管理能力：技能掃描/安裝、知識缺口補充、監控排程、任務追蹤。\n\n"
                "規則：用自然流暢的繁體中文介紹你的能力，禁止使用 emoji 或圖示符號，"
                "語氣專業且親切，300字以內。"
            )
            cap_messages = [
                {"role": "system", "content": cap_system},
                {"role": "user",   "content": task},
            ]
            cap_result = await self.brain.call_skill(
                "admin_task", cap_messages, max_tokens=500,
            )
            cap_answer = cap_result.get("content", "").strip()
            if cap_answer:
                return cap_answer

        # 判斷是「強化現有功能」還是「開發新功能」
        is_enhance = any(k in task for k in ["強化", "加強", "提升", "補充", "擴充"])
        topic = task  # 給 LLM 用的完整描述

        # ── 先執行技能掃描，取得真實結果 ─────────────────────────────────────
        real_result = ""
        if _skill_hunter:
            try:
                scan = await _skill_hunter.run_scan()
                n_prop = scan.get("proposals", 0)
                n_cand = scan.get("candidates", 0)
                real_result = f"掃描完成：找到 {n_cand} 個候選工具，產生 {n_prop} 個提案。"
                if n_prop > 0:
                    pending = _skill_hunter.proposals.get_pending()
                    names = [p.get("gap_topic", "") for p in pending[:3]]
                    real_result += f"\n待批准提案：{', '.join(names)}"
                    real_result += "\n（說「批准安裝」即可安裝）"
            except Exception as e:
                real_result = f"掃描失敗：{e}"

        # ── LLM 只負責用口語把真實結果呈現給老闆 ──────────────────────────
        system_prompt = (
            "你是 Naomi 建築事務所 AI 總助理。以下是你剛才執行的真實結果，清楚說明給老闆。\n"
            "規則：只能根據真實結果回應，不可自行補充或捏造任何功能。繁體中文，最多200字。"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": f"老闆說：{topic}"},
        ]
        result = await self.brain.call_skill(
            "admin_task", messages, max_tokens=400,
            real_data=real_result or f"已收到需求：{topic}，正在處理中。"
        )
        answer = result.get("content", "").strip()
        return answer or f"{greeting}{real_result or '已啟動處理，完成後通知你。'}"

    async def _handle_internal(self, user_id: str, text: str, mem: Dict) -> str:
        """
        internal intent — 天氣查詢優先，其餘 fallback 到 general_reply。
        天氣功能由 WeatherTool (CWA 開放資料) 驅動，不依賴 LLM 推測。
        """
        import re
        if self._weather:
            # 偵測天氣查詢意圖
            weather_match = re.search(
                r"(天氣|氣溫|溫度|下不下雨|會不會下雨|要帶傘嗎|幾度|今天天氣|明天天氣)",
                text
            )
            if weather_match:
                # 嘗試提取地點
                loc_match = re.search(
                    r"(臺北|台北|新北|板橋|土城|中和|永和|三重|桃園|新竹|台中|臺中|台南|臺南|高雄"
                    r"|基隆|宜蘭|花蓮|台東|臺東|苗栗|彰化|南投|雲林|嘉義|屏東|澎湖|金門)",
                    text
                )
                location = loc_match.group(1) if loc_match else "臺北市"
                return await self._weather.query(location)

        # fallback：交給 LLM 一般回覆
        return await self._general_reply(text, mem)

    async def _handle_schedule(self, user_id: str, text: str, mem: Dict) -> str:
        """
        schedule intent — 提醒/排程由 ReminderTool 驅動。
        其餘行程查詢 fallback 到 general_reply。
        """
        if self._reminder:
            from utils.reminder_tool import handle_reminder_request
            result = await handle_reminder_request(user_id, text, self._reminder)
            if result:
                return result

        # fallback：交給 LLM 一般回覆
        return await self._general_reply(text, mem)

    async def _background_skill_scan(self, hint: str):
        """背景觸發技能掃描，並將提示詞存入 knowledge_gaps 驅動搜尋"""
        try:
            # 把這次請求暫存為缺口，讓 SkillHunter 有針對性地搜尋
            with sqlite3.connect(str(DB_PATH)) as conn:
                gap_id = f"GAP-TASK-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
                conn.execute("""
                    INSERT OR IGNORE INTO knowledge_gaps
                    (gap_id, user_id, trigger_query, gap_reason, status)
                    VALUES (?, 'admin', ?, '主動強化請求', 'pending')
                """, (gap_id, hint))
                conn.commit()
            result = await _skill_hunter.run_scan()
            if result["proposals"] == 0:
                logger.info(f"[AdminTask] 技能掃描完成，無新提案")
        except Exception as e:
            logger.error(f"[AdminTask] 背景掃描失敗: {e}")
    
    async def _handle_memory_query(self, text: str, mem: Dict) -> str:
        """
        記憶查詢：把用戶的完整資料當 context 傳給 LLM，
        讓 LLM 自然回答任何關於「我是誰/我的資料/你記得我嗎」的問題。
        不用 if 判斷問題類型，LLM 自己理解。
        """
        profile   = mem.get("profile", {})
        user_id   = mem.get("user_id", "")
        history   = mem.get("history", [])[-5:]
        turn_count= mem.get("turn_count", 0)

        # ── 組建「用戶真實資料」context ──────────────────────────────────
        summary = mem.get("summary", "")
        user_data_lines = [
            f"LINE User ID: {user_id}" if user_id else "LINE User ID: 未知",
            f"姓名: {profile.get('name', '（未設定）')}",
            f"對話次數: {turn_count}",
            f"對話風格偏好: {profile.get('personality_pref', '（未設定）')}",
            f"備註: {profile.get('notes', '（無）')}",
        ]
        if summary:
            user_data_lines.append(f"\n【長期記憶摘要】\n{summary}")
        user_data_ctx = "\n".join(user_data_lines)

        messages = [
            {"role": "system", "content":
             f"{get_system_prompt()}\n\n"
             f"【你對這位用戶已知的真實資料（只能回報以下內容，不得捏造）】\n{user_data_ctx}\n\n"
             "根據以上真實資料回答問題。沒有的資料就說沒有，不要自行推測或補充。"},
            *history,
            {"role": "user", "content": text},
        ]

        try:
            result = await self.brain.call(BrainRole.ANALYST, messages, max_tokens=400)
            return result.get("content", "")
        except Exception as e:
            logger.warning(f"[Memory] LLM 回覆失敗：{e}")
            # fallback：至少把原始資料列出來
            return f"我記錄的您的資料：\n{user_data_ctx}"

    async def _general_reply(self, text: str, mem: Dict) -> str:
        """
        一般諮詢回覆（能力詢問/功能說明/上下文追問）。
        帶入完整對話歷史 + 組織架構，讓 LLM 有充足上下文。
        """
        history  = mem.get("history", [])[-10:]
        profile  = mem.get("profile", {})
        name     = profile.get("name", "")
        greeting = f"稱呼：{name}" if name else ""

        summary = mem.get("summary", "")
        summary_ctx = f"\n\n【長期記憶——你對這個人已知的事】\n{summary}" if summary else ""

        # 注入最近一筆地號查詢的上下文（防止後續問題失憶）
        land_ctx_str = ""
        _lc = mem.get("land_ctx", {})
        if _lc.get("parcel"):
            _zone_str = f"，使用分區：{_lc['zone']}" if _lc.get("zone") else "（使用分區待確認）"
            _road_str = f"，臨路：{', '.join(_lc['roads'])}" if _lc.get("roads") else ""
            land_ctx_str = (
                f"\n\n【本輪查詢地號上下文——必須記住，後續追問直接使用】\n"
                f"地號：{_lc.get('city','')}{_lc.get('district','')}"
                f"{_lc.get('section','')} {_lc.get('parcel','')} 地號"
                f"{_zone_str}{_road_str}\n"
                f"後續任何關於此地的追問（退縮、騎樓、容積、臨路等），直接用此上下文回答，"
                f"不可再問「你是哪個縣市」。"
            )

        system = (
            f"{get_system_prompt()}\n\n"
            f"{get_org_context()}\n\n"
            f"{get_few_shot_prompt()}\n\n"
            f"{'【使用者稱呼】' + greeting if greeting else ''}"
            f"{summary_ctx}{land_ctx_str}\n"
            "你是 Naomi。根據對話歷史和組織架構回答使用者的問題。\n"
            "【重要禁止事項】\n"
            "- 禁止捏造任何市場數據、統計數字、價格、成交量——你沒有這些資料\n"
            "- 禁止假裝「後端查到」而自行產生數據報告\n"
            "- 禁止生成法規條號或具體法規數字——法規問題應路由到法規查詢，不在此憑記憶回答\n"
            "- 問題模糊時直接問清楚，不要自己腦補回答\n"
            "- 如果問題關於某個 Squad 的能力，根據上方組織架構說明作答\n"
            "- 如果問的是市場行情、房價、成交量等外部資料，說明需要啟動哪個群組查詢\n"
            "繁體中文，最多150字，口語自然。"
        )

        messages = [
            {"role": "system", "content": system},
            *history,
        ]
        result = await self.brain.call_skill("general", messages, max_tokens=300)
        return result.get("content", "").strip() or "這個問題讓我想一下，你能再說清楚一點嗎？"

    async def _casual_reply(self, text: str, mem: Dict) -> str:
        """閒聊回覆"""
        history = mem.get("history", [])[-8:]
        profile = mem.get("profile", {})
        user_ctx_parts = []
        if profile.get("name"):
            user_ctx_parts.append(f"稱呼：{profile['name']}")
        if profile.get("personality_pref"):
            user_ctx_parts.append(f"對話風格偏好：{profile['personality_pref']}")
        if profile.get("notes"):
            user_ctx_parts.append(f"備註：{profile['notes']}")
        user_ctx = "\n【使用者資料】" + "、".join(user_ctx_parts) if user_ctx_parts else ""

        name = profile.get("name", "")
        summary = mem.get("summary", "")
        memory_ctx = ""
        if name:
            memory_ctx += f"\n\n【對話對象】{name}"
        if summary:
            memory_ctx += f"\n\n【長期記憶——你對這個人已知的事】\n{summary}"
        casual_hint = (
            "\n\n【閒聊模式】這是日常對話，直接自然說話。"
            "禁止使用條列清單或編號。一到三句話回覆即可，像真人說話一樣。"
        )
        messages = [
            {"role": "system", "content": get_system_prompt() + user_ctx + memory_ctx + casual_hint},
            *history
        ]

        result = await self.brain.call(BrainRole.CASUAL, messages, max_tokens=300)
        return result.get("content", "抱歉，我暫時無法回應。")
    
    # ── 聯合意圖執行 ─────────────────────────────────────────────────────────

    _INTENT_TO_SQUAD = {
        "legal":    "03_regulatory_intel",
        "design":   "04_architectural_design",
        "bim":      "06_bim_technology",
        "finance":  "11_financial_mgmt",
        "project":  "07_project_portfolio",
        "schedule": "09_integrated_admin",
        "internal": "09_integrated_admin",
    }

    async def _joint_intent_reply(
        self, user_id: str, text: str, mem: Dict, compound: str
    ) -> str:
        """
        聯合意圖執行：透過 Orchestrator 並行調度多個 Squad。
        compound = "legal+finance" 或 "design+project" 等
        """
        from utils.orchestrator import Orchestrator, make_squad_task, Priority

        intents = [i.strip() for i in compound.split("+") if i.strip()]
        logger.info(f"[Gateway] 聯合意圖執行: {intents}")

        profile = mem.get("profile", {})
        base_ctx = {
            "history":    mem.get("history", [])[-5:],
            "user_name":  profile.get("name", ""),
            "user_notes": profile.get("notes", ""),
        }

        # 方案閘門
        if self.tier_gate:
            for part in intents:
                sq = self._INTENT_TO_SQUAD.get(part)
                if sq:
                    gate = self.tier_gate.check(user_id, sq)
                    if not gate["allowed"]:
                        return gate["message"]

        # 動態上下文注入（每個 intent 各自注入）
        async def _make_task_fn(intent_part: str):
            ctx_i = dict(base_ctx)
            ctx_i["intent"] = intent_part
            if hasattr(self, "_context_injector") and self._context_injector:
                try:
                    ctx_i, ood = await self._context_injector.inject(
                        intent=intent_part, query=text, context=ctx_i, user_id=user_id
                    )
                    if ood.is_ood:
                        async def _ood_reply(_ctx):
                            return {"answer": ood.safe_reply}
                        return _ood_reply
                except Exception:
                    pass

            # ctx_i 已含正確的 intent；_ctx 是 Orchestrator 傳入的共享 base_ctx（無 intent）
            # 必須捕捉 ctx_i 到 closure，避免 Orchestrator 的共享 ctx 覆蓋 intent
            _captured = ctx_i

            async def _dispatch(_ctx):
                return await self.boss_agent.handle(
                    user_id=user_id, task=text, context=_captured
                )
            return _dispatch

        # 建立 OrchestratorTask 清單
        tasks = []
        priority_map = {"legal": Priority.HIGH, "finance": Priority.HIGH}
        for intent_part in intents:
            fn = await _make_task_fn(intent_part)
            t = make_squad_task(
                intent=intent_part,
                squad_dispatch_fn=fn,
                priority=priority_map.get(intent_part, Priority.NORMAL),
                timeout=45.0,
            )
            # 注入已計算好的 context
            tasks.append(t)

        orch = Orchestrator(groq_client=getattr(self.brain, "async_groq", None))
        return await orch.run(
            tasks=tasks, ctx=base_ctx, query=text,
            user_name=profile.get("name", ""),
        )

    # ── 專業回覆 ──────────────────────────────────────────────────────────────

    async def _professional_reply(self, user_id: str, text: str, mem: Dict, intent: str) -> str:
        """專業回覆（透過 BossAgent） — 含方案閘門 + RAG 知識庫查詢"""

        # 方案閘門：依 intent 對應 squad，檢查存取權限
        if self.tier_gate:
            intent_to_squad = {
                "legal":   "03_regulatory_intel",
                "design":  "04_architectural_design",
                "bim":     "06_bim_technology",
                "finance": "11_financial_mgmt",
            }
            target_squad = intent_to_squad.get(intent)
            if target_squad:
                gate = self.tier_gate.check(user_id, target_squad)
                if not gate["allowed"]:
                    return gate["message"]

        # RAG：搜尋管理員審核過的知識庫
        # 凡是會路由到 03_regulatory_intel 的問題，一律不注入 admin_knowledge。
        # 法規答案必須嚴格來自 law_library 向量庫（M04 RAG），
        # admin_knowledge 為人工輸入，可能含城市特定或過時資料，不得混入法規推理。
        _LAW_INTENTS = {"legal", "general"}   # general 也可能是法規問題，一併排除
        knowledge_context = ""
        if self.gap_manager and intent not in _LAW_INTENTS:
            hits = self.gap_manager.search_knowledge(text, n_results=2)
            if hits:
                knowledge_context = f"\n\n【參考知識庫】\n" + "\n---\n".join(hits)
                logger.info(f"[Gateway] RAG 命中 {len(hits)} 筆知識點（intent={intent}）")

        # 動態上下文注入 + OOD 攔截
        profile = mem.get("profile", {})

        # claude-reflect：載入該用戶的歷史修正紀錄，注入 context
        _corrections = self._load_user_corrections(user_id)
        _correction_hint = (
            "\n\n【用戶歷史修正（請特別留意）】\n" + "\n".join(_corrections)
            if _corrections else ""
        )

        # land_ctx 城市優先於 profile（地號查詢後的追問應使用該地號的城市）
        _land_ctx = mem.get("land_ctx", {})
        _ctx_city = _land_ctx.get("city") or profile.get("project_city", "")
        base_ctx = {
            "history":          mem.get("history", [])[-5:],
            "intent":           intent,
            "user_name":        profile.get("name", ""),
            "user_personality": profile.get("personality_pref", ""),
            "user_notes":       profile.get("notes", "") + _correction_hint,
            "city":             _ctx_city,
            "project_city":     _ctx_city,
        }
        if hasattr(self, "_context_injector") and self._context_injector:
            try:
                base_ctx, ood = await self._context_injector.inject(
                    intent=intent, query=text, context=base_ctx, user_id=user_id
                )
                # 供 handle_request 的快取 has_rag 判斷使用
                self._last_rag_chunks = base_ctx.get("rag_chunks") or []
                if ood.is_ood:
                    logger.info(f"[Gateway] OOD 攔截 intent={intent}: {ood.reason}")
                    # 自我進化：記錄缺口並背景觸發技能掃描
                    if getattr(ood, "log_gap", False):
                        if self.gap_manager:
                            try:
                                self.gap_manager.flag_gap(
                                    user_id, text, ood.safe_reply, ood.reason
                                )
                            except Exception:
                                pass
                        _fire_and_forget(self._background_skill_scan(
                            f"[{intent}-OOD] {text[:80]} — 原因: {ood.reason}"
                        ))
                    return ood.safe_reply
            except Exception as _ie:
                logger.warning(f"[Gateway] ContextInjector 失敗：{_ie}")

        result = await self.boss_agent.handle(
            user_id=user_id,
            task=text + knowledge_context,
            context=base_ctx
        )

        # 城市偵測回寫：BossAgent 偵測到城市 → 存入 session profile
        _detected_city = result.get("_detected_city") or base_ctx.get("_detected_city", "")
        if _detected_city and not mem.get("profile", {}).get("project_city"):
            mem.setdefault("profile", {})["project_city"] = _detected_city
            logger.info(f"[Gateway] 城市存入 session：{_detected_city}")

        answer = result.get("answer", "")
        if not answer:
            # BossAgent 無回應 → 嘗試 DialogueManager capability_fallback
            if self._dialogue_mgr:
                try:
                    answer = await self._dialogue_mgr.capability_fallback(text, intent)
                except Exception:
                    pass
            if not answer:
                answer = "請稍等一下，這個問題目前無法完整回答，建議直接向主管機關或專業顧問確認。"

        # 答案層幻覺攔截（legal intent 才執行）
        if intent == "legal" and answer:
            try:
                from utils.context_injector import HallucinationInterceptor
                rag_chunks = base_ctx.get("rag_chunks") or []
                hi = HallucinationInterceptor.check(answer, rag_chunks, intent)
                if hi.is_ood:
                    logger.warning(f"[Gateway] 幻覺攔截：{hi.reason}")
                    answer = hi.safe_reply
            except Exception as _hi_e:
                logger.debug(f"[Gateway] HallucinationInterceptor 失敗：{_hi_e}")

        return answer
    
    async def _direct_reply(self, user_id: str, text: str, mem: Dict, intent: str) -> str:
        """
        快速回覆路徑：RAG 取資料 → 單次 LLM 直接回答
        跳過 BossAgent / Squad / NaomiPersona 三層，減少到 1 次 LLM call。
        適用：legal / design / finance / bim / project 等單輪問答型 intent。
        """
        # 方案閘門
        if self.tier_gate:
            intent_to_squad = {
                "legal":   "03_regulatory_intel",
                "design":  "04_architectural_design",
                "bim":     "06_bim_technology",
                "finance": "11_financial_mgmt",
            }
            target_squad = intent_to_squad.get(intent)
            if target_squad:
                gate = self.tier_gate.check(user_id, target_squad)
                if not gate["allowed"]:
                    return gate["message"]

        profile  = mem.get("profile", {})
        history  = mem.get("history", [])[-6:]
        summary  = mem.get("summary", "")

        # RAG 注入（ContextInjector）
        # land_ctx 城市優先於 profile（地號查詢後的追問應使用該地號的城市）
        _land_ctx = mem.get("land_ctx", {})
        _rag_city = _land_ctx.get("city") or profile.get("project_city", "")
        rag_text = ""
        if hasattr(self, "_context_injector") and self._context_injector:
            try:
                ctx, ood = await self._context_injector.inject(
                    intent=intent, query=text,
                    context={"intent": intent, "city": _rag_city},
                    user_id=user_id,
                )
                self._last_rag_chunks = ctx.get("rag_chunks") or []
                if ood.is_ood:
                    if self.gap_manager:
                        try:
                            self.gap_manager.flag_gap(user_id, text, ood.safe_reply, ood.reason)
                        except Exception:
                            pass
                    return ood.safe_reply
                chunks = self._last_rag_chunks
                if chunks:
                    rag_text = "\n\n".join(
                        c.get("text", c) if isinstance(c, dict) else str(c)
                        for c in chunks[:6]
                    )
            except Exception as _e:
                logger.warning(f"[DirectReply] RAG 注入失敗：{_e}")

        # 組 system（只放角色定義 + 長期記憶，不放 RAG）
        name_hint = f"對話對象：{profile['name']}\n" if profile.get("name") else ""
        mem_hint  = f"【長期記憶】\n{summary}\n\n" if summary else ""

        has_rag = bool(rag_text)

        # 非專業類 intent 語氣提示
        conversational_intents = {"general", "casual", "memory", "greeting"}
        if intent in conversational_intents:
            style_rule = "回覆要自然口語，像對話不像報告。非必要不用條列清單或編號。"
        else:
            style_rule = (
                "繁體中文，口語自然。"
                "引用條文時必須說出完整格式如「依建築技術規則相關條文」。"
            )

        no_hallucination_rule = (
            "【絕對禁止】不得使用訓練記憶中的法規條號或數字；"
            "所有條號、面積、寬度、層數必須來自本次提供的法規資料，否則不要說。"
            if has_rag else
            "【絕對禁止】目前無法規資料，不得自行生成任何條號或具體數字，"
            "只能說「目前沒有這部分的資料，建議查閱建築技術規則原文或向主管機關確認」。"
        )

        system = (
            f"{get_system_prompt()}\n\n"
            f"{name_hint}"
            f"{mem_hint}"
            f"{style_rule}\n"
            f"{no_hallucination_rule}"
        )

        # ── RAG 放進 user message（比 system 更受模型重視）─────────────────
        if has_rag:
            user_content = (
                f"<law_source>\n{rag_text}\n</law_source>\n\n"
                "以上是本次查詢到的法規原文。"
                "請只使用 <law_source> 內出現過的條號和數字，"
                "不得從訓練記憶補充任何條號。\n\n"
                f"問題：{text}"
            )
        else:
            user_content = text

        messages = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": user_content},
        ]

        result = await self.brain.call_skill(intent, messages, max_tokens=1200)
        answer = result.get("content", "").strip()

        # 無 RAG 且回答太短 → 可能是模型放棄，記錄缺口
        if not has_rag and len(answer) < 20 and self.gap_manager:
            try:
                self.gap_manager.flag_gap(user_id, text, answer, f"direct_reply no_rag intent={intent}")
            except Exception:
                pass

        # 城市偵測回寫
        if not profile.get("project_city"):
            import re as _re2
            _city_m = _re2.search(r"([台臺][北中南]市|新北市|高雄市|桃園市|基隆市|苗栗縣)", text)
            if _city_m:
                mem.setdefault("profile", {})["project_city"] = _city_m.group(1)

        return answer or "這個問題我沒有足夠資料，建議直接向主管機關確認。"

    def _build_status(self) -> str:
        """系統狀態"""
        skills = list(self.hub.skills.keys())
        tools = list(self.hub.tools.keys())
        squads = list(self.squad_manager.squads.keys())
        llms = list(self.brain.clients.keys())
        
        return (
            f"Naomi 系統狀態\n"
            f"─────────────────────\n"
            f"可用大腦：{', '.join(llms)}\n"
            f"技能（{len(skills)}）：{', '.join(skills) if skills else '無'}\n"
            f"工具（{len(tools)}）：{', '.join(tools) if tools else '無'}\n"
            f"智能體群（{len(squads)}）：{', '.join(squads)}\n"
            f"今日調用：{self.brain.get_stats()['total_calls']} 次"
        )
    
    async def _handle_admin(self, user_id: str, text: str, mem: Dict) -> str:
        """處理管理指令"""
        cmd = text.strip().split()[0].lower()

        if cmd == "/說明":
            return (
                "Naomi 指令清單\n\n"
                "/狀態 — 系統狀態\n"
                "/團隊 — 智能體群列表\n"
                "/技能 — 技能與工具列表\n"
                "/配額 — 我的使用配額\n"
                "/身份 — 我的角色\n"
                "/進化 <能力> — 請求新能力\n"
                "/學習 — 查看學習狀態\n"
                "/待辦 — 查看待辦任務清單\n"
                "/早報 — 今日工作摘要\n"
                "/我是老闆 — 首次登錄管理員身份\n"
                "/管理員 — 查看管理員清單"
            )

        if cmd == "/狀態":
            return self._build_status()

        if cmd == "/團隊":
            squads = list(self.squad_manager.squads.keys())
            lines = [f"智能體群（{len(squads)} 個）\n"]
            for s in squads:
                meta = self.squad_manager.squads[s].get("meta", {})
                lines.append(f"• {s}: {meta.get('display_name', s)}")
            return "\n".join(lines)

        if cmd == "/技能":
            skills = list(self.hub.skills.keys())
            tools = list(self.hub.tools.keys())
            return (
                f"技能（{len(skills)} 個）：{', '.join(skills) if skills else '無'}\n\n"
                f"工具（{len(tools)} 個）：{', '.join(tools) if tools else '無'}"
            )

        if cmd == "/身份":
            role = self.permission.get_user_role(user_id)
            quota = self.permission.check_quota(user_id)
            return (
                f"角色：{role.value}\n"
                f"LINE ID：{user_id}\n"
                f"配額：{quota.get('used', 0)}/{quota.get('quota', '無限')}"
            )

        if cmd == "/配額":
            quota = self.permission.check_quota(user_id)
            if quota.get("unlimited"):
                return "配額：無限"
            return f"本月配額：已用 {quota['used']}/{quota['quota']}"

        if cmd == "/進化":
            args = text.strip().split()[1:]
            if not args:
                return "用法：`/進化 <能力描述>`\n例如：`/進化 解析 PDF 表格`"

            capability = " ".join(args)
            return f"收到能力請求：{capability}\n\n進化功能開發中，請稍等一下。"

        if cmd == "/學習":
            if self.evolution:
                status = self.evolution.get_evolution_status()
                return (
                    f"學習狀態\n"
                    f"學習記錄：{status.get('learning_records_count', 0)} 筆\n"
                    f"能力缺口：{status.get('gaps_count', 0)} 個"
                )
            return "進化功能尚未啟用"

        if cmd == "/我是老闆":
            # 安全驗證：必須在 .env ADMIN_LINE_USER_IDS 白名單內
            if user_id not in ADMIN_LINE_USER_IDS:
                return (
                    f"您的 LINE ID 不在管理員白名單中\n\n"
                    f"您的 ID：{user_id}\n\n"
                    f"請將此 ID 加入伺服器 .env 檔案：\n"
                    f"ADMIN_LINE_USER_IDS={user_id}\n\n"
                    f"設定後重啟 Naomi，再傳一次 /我是老闆 即可完成登錄"
                )
            self.permission.register_admin(user_id)
            global _event_processor
            if _event_processor and not _event_processor.engine.owner_id:
                _event_processor.engine.owner_id = user_id
            return (
                f"已登錄為管理員\n"
                f"LINE ID：{user_id}\n"
                f"已永久寫入資料庫，重啟後仍有效"
            )

        if cmd == "/管理員":
            admins = self.permission.get_all_admins()
            if not admins:
                return "目前沒有登錄任何管理員\n使用 /我是老闆 [密碼] 來登錄"
            lines = [f"管理員清單（{len(admins)} 人）\n"]
            for aid in admins:
                role = self.permission.get_user_role(aid)
                lines.append(f"• {aid[:12]}... [{role.value}]")
            return "\n".join(lines)

        if cmd == "/技能提案":
            if _skill_hunter:
                return _skill_hunter.list_pending_proposals()
            return "技能獵人未啟用"

        if cmd == "/掃描技能":
            if _skill_hunter:
                asyncio.create_task(_skill_hunter.run_scan())
                return "已啟動技能掃描，完成後會推播結果"
            return "技能獵人未啟用"

        if cmd == "/批准技能":
            parts = text.strip().split(maxsplit=1)
            if len(parts) < 2:
                return "用法：/批准技能 <提案ID>\n例：/批准技能 PROP-20260310-法規"
            if _skill_hunter:
                result = await _skill_hunter.install_approved(parts[1].strip())
                return result
            return "⚠️ 技能獵人未啟用"

        if cmd == "/拒絕技能":
            parts = text.strip().split(maxsplit=1)
            if len(parts) < 2:
                return "用法：/拒絕技能 <提案ID>"
            if _skill_hunter:
                _skill_hunter.proposals.reject(parts[1].strip())
                return f"❌ 已拒絕提案 {parts[1].strip()}"
            return "⚠️ 技能獵人未啟用"

        if cmd == "/待辦":
            if _event_processor:
                return _event_processor.list_tasks()
            return "⚠️ 任務系統未啟用"

        if cmd == "/早報":
            if _event_processor:
                return _event_processor.get_daily_brief()
            return "⚠️ 任務系統未啟用"

        return "未知指令，請使用 /說明 查看可用指令"
    
    def _load_user(self, user_id: str) -> Dict:
        """載入用戶記憶（對話歷史 + 個人資料）"""
        try:
            with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
                row = conn.execute(
                    "SELECT history_json, summary FROM sessions WHERE user_id=?", (user_id,)
                ).fetchone()
                prof = conn.execute(
                    "SELECT preferred_name, personality_pref, notes, tenant_id, project_city FROM user_profiles WHERE user_id=?",
                    (user_id,)
                ).fetchone()
            return {
                "user_id": user_id,
                "history": json.loads(row[0]) if row and row[0] else [],
                "summary": row[1] if row and row[1] else "",
                "profile": {
                    "name":             prof[0] if prof and prof[0] else "",
                    "personality_pref": prof[1] if prof and prof[1] else "",
                    "notes":            prof[2] if prof and prof[2] else "",
                    "tenant_id":        prof[3] if prof and prof[3] else "",
                    "project_city":     prof[4] if prof and prof[4] else "",
                },
            }
        except Exception as e:
            logger.error(f"[Gateway] 載入用戶記憶失敗 user_id={user_id}：{e}", exc_info=True)
            return {"user_id": user_id, "history": [], "summary": "", "profile": {
                "name": "", "personality_pref": "", "notes": "", "tenant_id": "", "project_city": ""
            }}

    def _save_user(self, user_id: str, mem: Dict):
        """儲存用戶記憶（對話歷史 + 個人資料同步）"""
        for _attempt in range(3):
            try:
                with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
                    break
            except sqlite3.OperationalError as _db_e:
                if _attempt == 2:
                    logger.error(f"[Gateway] SQLite 連線失敗（重試3次）user_id={user_id}：{_db_e}")
                    return
                import time as _time; _time.sleep(0.3 * (_attempt + 1))
        try:
            with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
                _turn = len(mem.get("history", [])) // 2
                conn.execute("""
                    INSERT INTO sessions (user_id, history_json, summary, turn_count, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(user_id) DO UPDATE SET
                    history_json=excluded.history_json,
                    turn_count=excluded.turn_count,
                    updated_at=excluded.updated_at
                """, (user_id, json.dumps(mem["history"][-30:], ensure_ascii=False), mem.get("summary", ""), _turn))
                # 個人資料：name 只在明確設定時才覆蓋（防止被空值或錯誤值洗掉）
                prof = mem.get("profile", {})
                new_name      = prof.get("name") or None
                new_notes     = prof.get("notes") or None
                new_city      = prof.get("project_city") or None
                new_tenant    = prof.get("tenant_id") or None
                new_pers      = prof.get("personality_pref") or None
                if new_name or new_notes or new_city or new_tenant or new_pers:
                    conn.execute("""
                        INSERT INTO user_profiles (user_id, preferred_name, notes, project_city, tenant_id, personality_pref, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(user_id) DO UPDATE SET
                        preferred_name = CASE
                            WHEN excluded.preferred_name IS NOT NULL
                             AND excluded.preferred_name != ''
                            THEN excluded.preferred_name
                            ELSE preferred_name
                        END,
                        notes         = COALESCE(excluded.notes, notes),
                        project_city  = COALESCE(excluded.project_city, project_city),
                        tenant_id     = COALESCE(excluded.tenant_id, tenant_id),
                        personality_pref = COALESCE(excluded.personality_pref, personality_pref),
                        updated_at    = excluded.updated_at
                    """, (user_id, new_name, new_notes, new_city, new_tenant, new_pers))
                conn.commit()
        except Exception as e:
            logger.error(f"[Gateway] 儲存用戶記憶失敗：{e}")

    async def _update_summary(self, user_id: str, mem: Dict):
        """背景壓縮：把近期對話 + 既有 summary → 更新長期記憶摘要"""
        try:
            history_snippet = mem["history"][-10:]  # 最近 10 條
            old_summary = mem.get("summary", "")
            profile = mem.get("profile", {})
            name = profile.get("name", "")

            prompt_parts = []
            if old_summary:
                prompt_parts.append(f"【既有記憶摘要】\n{old_summary}")
            if name:
                prompt_parts.append(f"使用者姓名：{name}")
            prompt_parts.append("【最新對話】")
            for msg in history_snippet:
                role_label = "使用者" if msg["role"] == "user" else "Naomi"
                prompt_parts.append(f"{role_label}：{msg['content'][:200]}")
            prompt_parts.append(
                "\n請整合以上資訊，更新使用者的長期記憶摘要。"
                "包含：姓名、身份、常問的主題、偏好、進行中的案子。"
                "格式：條列式，繁體中文，最多150字。"
            )

            messages = [{"role": "user", "content": "\n".join(prompt_parts)}]
            result = await self.brain.call_skill("general", messages, max_tokens=250)
            new_summary = result.get("content", "").strip()
            if new_summary:
                mem["summary"] = new_summary
                self._save_user(user_id, mem)
                logger.info(f"[Memory] summary 已更新 user={user_id[:8]} len={len(new_summary)}")
        except Exception as e:
            logger.warning(f"[Memory] summary 更新失敗: {e}")

    def _extract_user_profile(self, text: str, mem: Dict) -> bool:
        """姓名萃取已交由 _update_summary() 的 LLM 處理，此函數保留為空殼"""
        return False


# ==============================================================================
# 11. 資料庫初始化
# ==============================================================================

def _init_database():
    """初始化資料庫"""
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                role TEXT DEFAULT 'general',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP,
                display_name TEXT,
                memo TEXT,
                tenant_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id TEXT PRIMARY KEY,
                history_json TEXT,
                summary TEXT,
                updated_at TIMESTAMP,
                turn_count INTEGER DEFAULT 0,
                tenant_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                preferred_name TEXT,
                notes TEXT,
                updated_at TIMESTAMP,
                personality_pref TEXT,
                tenant_id TEXT,
                project_city TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quota_usage (
                user_id TEXT,
                month TEXT,
                used INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, month)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_cost_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT DEFAULT (datetime('now')),
                provider     TEXT,
                model        TEXT,
                role_key     TEXT,
                input_tokens  INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd      REAL DEFAULT 0.0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS land_records (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT,
                file_name     TEXT,
                city          TEXT,
                district      TEXT,
                section       TEXT,
                parcels       TEXT,
                area_m2       REAL DEFAULT 0,
                land_use_zone TEXT,
                doc_type      TEXT,
                confidence    TEXT,
                xlsx_path     TEXT,
                file_path     TEXT,
                created_at    TEXT
            )
        """)
        # ── 案件記憶層 ────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id      TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                case_name    TEXT,
                city         TEXT,
                district     TEXT,
                section      TEXT,
                parcels      TEXT,
                address      TEXT,
                status       TEXT DEFAULT 'active',
                created_at   TEXT,
                updated_at   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS case_documents (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id      TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                file_name    TEXT,
                doc_type     TEXT,
                doc_type_zh  TEXT,
                fields_json  TEXT,
                confidence   TEXT,
                saved_path   TEXT,
                created_at   TEXT,
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
            )
        """)
        conn.commit()
        # ── 舊資料庫欄位補齊（ALTER TABLE IF NOT EXISTS 等效）──────────────
        _migrations = [
            ("user_profiles", "personality_pref", "TEXT"),
            ("user_profiles", "tenant_id",        "TEXT"),
            ("user_profiles", "project_city",      "TEXT"),
            ("sessions",      "turn_count",        "INTEGER DEFAULT 0"),
            ("sessions",      "tenant_id",         "TEXT"),
            ("users",         "display_name",      "TEXT"),
            ("users",         "memo",              "TEXT"),
            ("users",         "tenant_id",         "TEXT"),
        ]
        for table, col, coltype in _migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
                conn.commit()
                logger.info(f"[DB] Migration: {table}.{col} 已新增")
            except Exception:
                pass  # 欄位已存在，忽略

_init_database()
logger.info("[DB] 資料庫初始化完成")

# ── 訂閱管理器 ────────────────────────────────────────────────────────────────
try:
    from utils.subscription_manager import SubscriptionManager
    _sub_mgr = SubscriptionManager(str(DB_PATH))
    logger.info("[SubMgr] 訂閱管理器就緒")
except Exception as _sub_err:
    _sub_mgr = None
    logger.warning(f"[SubMgr] 訂閱管理器載入失敗：{_sub_err}")

# ── 內部人員自動設定（從 .env INTERNAL_USER_IDS）──────────────────────────────
def _init_internal_users():
    """啟動時將 INTERNAL_USER_IDS 設為 super_admin + tenant_id=company"""
    if not INTERNAL_USER_IDS:
        return
    with sqlite3.connect(str(DB_PATH)) as conn:
        for uid in INTERNAL_USER_IDS:
            conn.execute("""
                INSERT INTO users (user_id, role, tenant_id, last_seen)
                VALUES (?, 'super_admin', 'company', datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    role='super_admin',
                    tenant_id='company',
                    last_seen=datetime('now')
            """, (uid,))
            # 內部人員訂閱設為永久 active（不受付費控制）
            conn.execute("""
                INSERT INTO subscriptions (user_id, tier, status, paid_until, updated_at)
                VALUES (?, 'tier_pro', 'active', '9999-12-31', datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    tier='tier_pro',
                    status='active',
                    paid_until='9999-12-31',
                    updated_at=datetime('now')
            """, (uid,))
        conn.commit()
    logger.info(f"[DB] 內部人員設定完成：{len(INTERNAL_USER_IDS)} 位 → super_admin / company / tier_pro")

_init_internal_users()

# 告知 llm_config 模組 DB 路徑，啟用費用記帳
try:
    from utils.llm_config import set_db_path as _llm_set_db
    _llm_set_db(str(DB_PATH))
    logger.info("[LLMConfig] DB 路徑已設定，費用記帳啟用")
except Exception:
    pass


# ==============================================================================
# 12. LINE 輔助函數
# ==============================================================================

def _line_reply(reply_token: str, text: str):
    """LINE 回覆"""
    if not LINE_CONFIG:
        return
    try:
        with ApiClient(LINE_CONFIG) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text[:4990])]
                )
            )
    except Exception as e:
        logger.error(f"[LINE] 回覆失敗: {e}")


def _line_reply_with_dxf(reply_token: str, text: str, dxf_url: str, label: str = "DXF 放樣圖"):
    """LINE 回覆：文字訊息 + DXF 下載按鈕 Flex Message（最多2則）"""
    if not LINE_CONFIG:
        return
    # Flex Message bubble — 下載按鈕卡片
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": label, "weight": "bold", "size": "md"},
                {"type": "text", "text": "點下方按鈕下載 DXF 放樣圖", "size": "sm",
                 "color": "#888888", "wrap": True},
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#1A73E8",
                    "action": {"type": "uri", "label": "📥 下載 DXF", "uri": dxf_url},
                }
            ]
        }
    }
    try:
        with ApiClient(LINE_CONFIG) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(text=text[:4990]),
                        FlexMessage(alt_text=label, contents=bubble),
                    ]
                )
            )
    except Exception as e:
        logger.error(f"[LINE] Flex 回覆失敗: {e}")
        # fallback — 純文字
        _line_reply(reply_token, text + f"\n\n📥 {label}：{dxf_url}")


def _line_push(user_id: str, text: str):
    """LINE 主動推送（同步）"""
    if not LINE_CONFIG:
        return
    try:
        with ApiClient(LINE_CONFIG) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[TextMessage(text=text[:4990])])
            )
    except Exception as e:
        logger.error(f"[LINE] Push 失敗: {e}")


async def _line_push_async(user_id: str, text: str):
    """LINE 主動推送（async，供 EventProcessor 使用）"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _line_push, user_id, text)


async def _line_push_group_async(group_id: str, text: str):
    """推送到 LINE 群組（async）"""
    await _line_push_async(group_id, text)


# ==============================================================================
# 13. 系統實例化
# ==============================================================================

logger.info("=" * 60)
logger.info("Naomi V21.1 初始化中...")
logger.info("=" * 60)

# 核心元件
brain_manager = BrainManager()
permission_manager = PermissionManager(str(DB_PATH), ADMIN_LINE_USER_IDS)
consultation_recorder = ConsultationRecorder(str(DB_PATH))

# KernelHub
hub = KernelHub(chroma_path=CHROMA_PATH, async_groq=ASYNC_GROQ)

# SquadManager
squad_manager = SquadManager(hub=hub, async_groq=ASYNC_GROQ)
hub.squad_manager = squad_manager   # 讓 Squad00 可透過 hub 查詢各組狀態

# BossAgent
boss_agent = BossAgent(
    hub=hub,
    squad_manager=squad_manager,
    brain_manager=brain_manager,
    async_groq=ASYNC_GROQ,
)

# KnowledgeGapManager（使用獨立的 admin_knowledge 集合）
gap_manager = KnowledgeGapManager(
    db_path=str(DB_PATH),
    chroma_path=CHROMA_PATH,
)

# Gateway
gateway = ArchGateway(
    brain_manager=brain_manager,
    permission_manager=permission_manager,
    consultation_recorder=consultation_recorder,
    hub=hub,
    squad_manager=squad_manager,
    boss_agent=boss_agent,
    gap_manager=gap_manager,
)

logger.info(f"🧠 可用 LLM: {list(brain_manager.clients.keys())}")
logger.info(f"🔧 技能: {list(hub.skills.keys())}")
logger.info(f"🛠️ 工具: {list(hub.tools.keys())}")
logger.info(f"👥 智能體群: {list(squad_manager.squads.keys())}")

# EventProcessor（群組訊息自動觸發引擎）
_event_processor = None
try:
    from tools.event_processor import EventProcessor
    _all_admins = permission_manager.get_all_admins()
    _owner_id = _all_admins[0] if _all_admins else ""
    _event_processor = EventProcessor(
        db_path       = str(DB_PATH),
        push_fn       = _line_push_async,
        reply_grp_fn  = _line_push_group_async,
        boss_agent    = boss_agent,
        owner_line_id = _owner_id,
        groq_client   = ASYNC_GROQ,
        claude_client = ASYNC_CLAUDE,
    )
    logger.info(f"[EventProcessor] 就緒，老闆ID={'已設定' if _owner_id else '未設定'}")
except Exception as _ep_err:
    logger.warning(f"[EventProcessor] 載入失敗: {_ep_err}")

# TaskTracker 初始化
try:
    from utils.task_tracker import TaskTracker
    _task_tracker = TaskTracker(str(DB_PATH))
    logger.info("[TaskTracker] 就緒")
except Exception as _tt_err:
    _task_tracker = None
    logger.warning(f"[TaskTracker] 載入失敗: {_tt_err}")

# EventHooks 初始化
try:
    from utils.event_hooks import registry as _hook_registry, setup_default_hooks
    setup_default_hooks(
        hub=hub if 'hub' in dir() else None,
        line_push_fn=_line_push_async if '_line_push_async' in dir() else None,
        task_tracker=_task_tracker,
    )
    logger.info("[EventHooks] 就緒")
except Exception as _eh_err:
    _hook_registry = None
    logger.warning(f"[EventHooks] 載入失敗: {_eh_err}")

# SkillHunter（Gap驅動技能探索）
_skill_hunter = None
try:
    from tools.skill_hunter import SkillHunter
    _skill_hunter = SkillHunter(
        db_path  = str(DB_PATH),
        push_fn  = _line_push_async,
        owner_id = _owner_id,
    )
    logger.info("[SkillHunter] 就緒")
except Exception as _sh_err:
    logger.warning(f"[SkillHunter] 載入失敗: {_sh_err}")

# ReminderTool 接上 LINE push function
if gateway and hasattr(gateway, "_reminder") and gateway._reminder:
    gateway._reminder.push_fn = _line_push_async
    logger.info("[ReminderTool] push_fn 已接入 LINE")

# GapProcessor — 知識缺口排程（每 6 小時自動補充送審流程）
try:
    from tools.gap_processor import get_gap_processor
    _gap_processor = get_gap_processor()
    _gap_processor.start_background()
    logger.info("[GapProcessor] 排程啟動（每 6h 掃描 knowledge_gaps）")
except Exception as _gp_err:
    _gap_processor = None
    logger.warning(f"[GapProcessor] 載入失敗：{_gp_err}")


# ==============================================================================
# 14. LINE Handlers
# ==============================================================================

def _run_async_safe(coro):
    """
    在同步 LINE handler（可能在 thread pool 中）安全執行 async 函式。
    - 若無 running loop → 直接 asyncio.run()
    - 若已有 running loop（不應發生，但保險）→ ThreadPoolExecutor 隔離
    """
    import concurrent.futures
    try:
        loop = asyncio.get_running_loop()
        # 已有 running loop：在獨立 thread 執行，避免 "This event loop is already running"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, coro).result(timeout=90)
    except RuntimeError:
        # 沒有 running loop：安全直接執行
        return asyncio.run(coro)


if HANDLER:
    # 防重複處理的訊息 ID 集合（單程序有效）
    _processed_msg_ids: set = set()

    @HANDLER.add(MessageEvent, message=TextMessageContent)
    def line_msg_handler(event):
        uid = event.source.user_id
        msg_id = event.message.id
        text = event.message.text

        if msg_id in _processed_msg_ids:
            logger.warning(f"[LINE] 重複文字訊息已略過：{msg_id}")
            return
        _processed_msg_ids.add(msg_id)
        if len(_processed_msg_ids) > 1000:
            _processed_msg_ids.clear()

        # ── 判斷訊息來源 ─────────────────────────────────────────────────
        source_type = getattr(event.source, "type", "user")
        group_id    = getattr(event.source, "group_id", None)
        room_id     = getattr(event.source, "room_id",  None)
        chat_id     = group_id or room_id   # 群組 or 聊天室

        # ── 群組訊息：EventProcessor 監聽 + 選擇是否回覆 ─────────────────
        if chat_id and _event_processor:
            try:
                result = _run_async_safe(_event_processor.process(
                    group_id  = chat_id,
                    sender_id = uid,
                    text      = text,
                ))
                logger.info(f"[LINE] 群組事件處理: {result}")
            except Exception as _ep_e:
                logger.error(f"[LINE] EventProcessor 失敗: {_ep_e}")

            # 群組訊息只有在 @Naomi 開頭才直接回覆
            if not (text.startswith("@Naomi") or text.startswith("@naomi")):
                return  # 已由 EventProcessor 在背景處理，不回覆群組
            text = text.replace("@Naomi", "").replace("@naomi", "").strip()

        # ── 取 LINE 顯示名稱（第一次才呼叫，之後靠 DB 快取）────────────
        line_display_name = ""
        try:
            mem_check = gateway._load_user(uid)
            if not mem_check.get("profile", {}).get("name") and LINE_CONFIG:
                with ApiClient(LINE_CONFIG) as _api:
                    _profile = MessagingApi(_api).get_profile(uid)
                    line_display_name = _profile.display_name or ""
                    if line_display_name:
                        mem_check["profile"]["name"] = line_display_name
                        gateway._save_user(uid, mem_check)
                        logger.info(f"[LINE] 取得顯示名稱: {line_display_name}")
        except Exception as _pf_e:
            logger.warning(f"[LINE] 取顯示名稱失敗: {_pf_e}")

        # ── 個人訊息 or @Naomi：走正常對話流程 ───────────────────────────
        try:
            answer = _run_async_safe(gateway.handle_request(uid, text))
        except Exception as e:
            logger.error(f"[LINE] 處理失敗: {e}", exc_info=True)
            answer = "系統暫時無法回應，請稍後再試。"
        _line_reply(event.reply_token, answer)

    @HANDLER.add(MessageEvent, message=FileMessageContent)
    def line_file_handler(event):
        uid = event.source.user_id
        msg_id = event.message.id
        file_name = event.message.file_name

        # 同一訊息 ID 只處理一次，防止 LINE 重送或背景任務重複觸發
        if msg_id in _processed_msg_ids:
            logger.warning(f"[LINE] 重複訊息已略過：{msg_id}")
            return
        _processed_msg_ids.add(msg_id)
        if len(_processed_msg_ids) > 1000:   # 與文字訊息統一門檻
            _processed_msg_ids.clear()

        try:
            with ApiClient(LINE_CONFIG) as api_client:
                blob_api = MessagingApiBlob(api_client)
                file_bytes = blob_api.get_message_content(message_id=msg_id)

            answer = _run_async_safe(gateway.handle_file(uid, file_name, file_bytes))
            # 若 answer 含 DXF 下載連結，改用 Flex Message 附按鈕
            import re as _re
            _dxf_m = _re.search(r"(https?://\S+/download/\S+)", answer)
            if _dxf_m:
                _dxf_url  = _dxf_m.group(1)
                _clean    = answer.replace(_dxf_url, "").strip().rstrip("：: ")
                _line_reply_with_dxf(event.reply_token, _clean, _dxf_url)
            else:
                _line_reply(event.reply_token, answer)
        except Exception as e:
            logger.error(f"[LINE] 檔案處理失敗: {e}", exc_info=True)
            _line_reply(event.reply_token, "檔案處理失敗，請稍後再試。")

    @HANDLER.add(MessageEvent, message=ImageMessageContent)
    def line_image_handler(event):
        uid = event.source.user_id
        msg_id = event.message.id

        if msg_id in _processed_msg_ids:
            return
        _processed_msg_ids.add(msg_id)
        if len(_processed_msg_ids) > 1000:
            _processed_msg_ids.clear()

        try:
            with ApiClient(LINE_CONFIG) as api_client:
                blob_api = MessagingApiBlob(api_client)
                img_bytes = blob_api.get_message_content(message_id=msg_id)
            answer = _run_async_safe(gateway.handle_file(uid, "image.jpg", img_bytes))
        except Exception as e:
            logger.error(f"[LINE] 圖片處理失敗: {e}", exc_info=True)
            answer = "收到圖片，但處理時發生錯誤，請稍後再試。"
        _line_reply(event.reply_token, answer)

    @HANDLER.add(MessageEvent, message=AudioMessageContent)
    def line_audio_handler(event):
        """LINE 語音訊息 → Whisper STT → 正常對話路由"""
        uid    = event.source.user_id
        msg_id = event.message.id

        if msg_id in _processed_msg_ids:
            return
        _processed_msg_ids.add(msg_id)
        if len(_processed_msg_ids) > 1000:
            _processed_msg_ids.clear()

        try:
            with ApiClient(LINE_CONFIG) as api_client:
                blob_api   = MessagingApiBlob(api_client)
                audio_bytes = blob_api.get_message_content(message_id=msg_id)
        except Exception as e:
            logger.error(f"[LINE] 語音下載失敗: {e}", exc_info=True)
            _line_reply(event.reply_token, "語音下載失敗，請稍後再試或改用文字輸入。")
            return

        # Whisper 轉錄
        if gateway and gateway._voice:
            try:
                text = _run_async_safe(
                    gateway._voice.transcribe(audio_bytes, filename="voice.m4a")
                )
            except Exception as _te:
                logger.error(f"[LINE] 語音轉錄失敗: {_te}")
                text = None
        else:
            text = None

        if not text:
            _line_reply(event.reply_token, "語音辨識失敗，請改用文字輸入，或確認語音清晰度。")
            return

        logger.info(f"[LINE] 語音轉文字完成 uid={uid[-8:]} text={text[:40]}")
        # 轉錄成功後，當一般文字訊息走正常路由
        try:
            answer = _run_async_safe(gateway.handle_request(uid, text))
        except Exception as e:
            logger.error(f"[LINE] 語音後路由失敗: {e}", exc_info=True)
            answer = "處理語音訊息時發生錯誤，請稍後再試。"
        _line_reply(event.reply_token, f"（語音辨識：{text[:30]}{'...' if len(text)>30 else ''}）\n\n{answer}")


# ==============================================================================
# 15. API 端點
# ==============================================================================

@app.post("/callback")
async def line_callback(request: Request, bg: BackgroundTasks):
    if not HANDLER:
        raise HTTPException(status_code=500, detail="LINE not configured")
    sig = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    bg.add_task(HANDLER.handle, body, sig)
    return "OK"


# ------------------------------------------------------------------------------
# 測試用 HTTP 入口（不需要 LINE）
# ------------------------------------------------------------------------------

from fastapi import UploadFile, File, Form

@app.post("/test/chat")
async def test_chat(user_id: str = Form(default="test_user"), message: str = Form(...)):
    """文字對話測試入口"""
    try:
        answer = await gateway.handle_request(user_id, message)
        return {"user_id": user_id, "message": message, "answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class _ChatRequest(BaseModel):
    user_id: str = "ceo_claude"
    message: str
    role: str = "admin"
    context: dict = {}

@app.post("/api/chat")
async def api_chat(req: _ChatRequest):
    """MCP / API JSON 對話入口"""
    try:
        answer = await gateway.handle_request(req.user_id, req.message)
        return {"user_id": req.user_id, "message": req.message, "answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/test/upload")
async def test_upload(
    user_id: str = Form(default="test_user"),
    file: UploadFile = File(...)
):
    """檔案上傳測試入口"""
    try:
        file_bytes = await file.read()
        answer = await gateway.handle_file(user_id, file.filename, file_bytes)
        return {"user_id": user_id, "file_name": file.filename, "answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/test/ui", response_class=HTMLResponse)
def test_ui():
    """簡易測試介面"""
    return """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>Naomi 測試介面</title>
<style>
  body { font-family: sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }
  h2 { color: #333; }
  .box { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
  input, textarea { width: 100%; padding: 8px; margin: 6px 0 12px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px; }
  button { background: #4f46e5; color: white; padding: 10px 24px; border: none; border-radius: 4px; cursor: pointer; }
  button:hover { background: #4338ca; }
  pre { background: #f0f0f0; padding: 12px; border-radius: 4px; white-space: pre-wrap; word-break: break-all; font-size: 13px; }
  #chat-history { max-height: 400px; overflow-y: auto; }
  .msg-user { text-align: right; margin: 8px 0; }
  .msg-user span { background: #4f46e5; color: white; padding: 8px 14px; border-radius: 18px 18px 4px 18px; display: inline-block; max-width: 80%; }
  .msg-naomi { text-align: left; margin: 8px 0; }
  .msg-naomi span { background: white; border: 1px solid #ddd; padding: 8px 14px; border-radius: 18px 18px 18px 4px; display: inline-block; max-width: 80%; white-space: pre-wrap; }
</style>
</head>
<body>
<h2>🏗️ Naomi V21.1 測試介面</h2>

<div class="box">
  <b>使用者 ID</b>
  <input id="uid" value="test_user" style="width:200px">
</div>

<div class="box">
  <b>上傳檔案</b>
  <input type="file" id="file-input">
  <button onclick="uploadFile()">上傳並讓 Naomi 讀取</button>
  <pre id="upload-result" style="display:none"></pre>
</div>

<div class="box">
  <b>對話</b>
  <div id="chat-history"></div>
  <div style="display:flex;gap:8px;margin-top:12px">
    <input id="msg" placeholder="輸入訊息..." onkeydown="if(event.key==='Enter')sendMsg()">
    <button onclick="sendMsg()">送出</button>
  </div>
</div>

<script>
const uid = () => document.getElementById('uid').value || 'test_user';

async function uploadFile() {
  const fi = document.getElementById('file-input');
  if (!fi.files[0]) return alert('請選擇檔案');
  const fd = new FormData();
  fd.append('user_id', uid());
  fd.append('file', fi.files[0]);
  const res = await fetch('/test/upload', { method: 'POST', body: fd });
  const data = await res.json();
  document.getElementById('upload-result').style.display = 'block';
  document.getElementById('upload-result').textContent = JSON.stringify(data, null, 2);
  appendMsg('naomi', data.answer || data.detail);
}

async function sendMsg() {
  const input = document.getElementById('msg');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  appendMsg('user', text);
  const fd = new FormData();
  fd.append('user_id', uid());
  fd.append('message', text);
  const res = await fetch('/test/chat', { method: 'POST', body: fd });
  const data = await res.json();
  appendMsg('naomi', data.answer || data.detail);
}

function appendMsg(role, text) {
  const h = document.getElementById('chat-history');
  const d = document.createElement('div');
  d.className = role === 'user' ? 'msg-user' : 'msg-naomi';
  d.innerHTML = '<span>' + (text || '').replace(/</g,'&lt;') + '</span>';
  h.appendChild(d);
  h.scrollTop = h.scrollHeight;
}
</script>
</body>
</html>
"""


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "V21.1",
        "skills": list(hub.skills.keys()),
        "tools": list(hub.tools.keys()),
        "squads": list(squad_manager.squads.keys()),
        "llms": list(brain_manager.clients.keys())
    }


# ── DXF 放樣圖下載（token 換檔案）─────────────────────────────────────────────

from fastapi.responses import FileResponse as _FileResponse

@app.get("/download/{token}")
async def download_dxf(token: str):
    """下載 DXF 放樣圖（token 由系統產生，有效期至程序重啟）"""
    path = _FILE_TOKENS.get(token)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="檔案不存在或連結已過期，請重新上傳")
    filename = os.path.basename(path)
    return _FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ==============================================================================
# 付款 Webhook API — 接收付款系統回呼，自動開通/續約訂閱
# ==============================================================================

class _PaymentWebhookRequest(BaseModel):
    user_id:    str           # LINE user_id 或系統 user_id
    tier:       str           # tier_basic / tier_mid / tier_pro
    months:     int = 1       # 付費月數（預設 1）
    payment_ref: str = ""     # 外部付款單號
    paid_until: str = ""      # 可直接指定到期日 YYYY-MM-DD（選填）
    secret:     str = ""      # 內部驗證碼（等同 PAYMENT_WEBHOOK_SECRET）


@app.post("/payment/webhook")
async def payment_webhook(req: Request):
    """
    付款系統 Webhook 入口
    支援兩種驗證方式：
      A. Header: X-Signature: HMAC-SHA256(body, PAYMENT_WEBHOOK_SECRET)
      B. Body 欄位 secret == PAYMENT_WEBHOOK_SECRET（簡易整合用）
    開通成功後自動推播 LINE 通知給用戶
    """
    if not _sub_mgr:
        raise HTTPException(503, "訂閱管理器未啟用")

    body_bytes = await req.body()
    signature  = req.headers.get("X-Signature", "")

    # 解析 JSON body
    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(400, "無效的 JSON")

    # 驗證簽名（Header 方式）
    if signature:
        if not _sub_mgr.verify_webhook_signature(body_bytes, signature):
            raise HTTPException(403, "簽名驗證失敗")
    else:
        # 備用：body 內含 secret 欄位
        body_secret = payload.get("secret", "")
        import os as _os
        expected_secret = _os.getenv("PAYMENT_WEBHOOK_SECRET", "")
        if expected_secret and body_secret != expected_secret:
            raise HTTPException(403, "驗證失敗")

    user_id     = payload.get("user_id", "")
    tier        = payload.get("tier", "tier_basic")
    months      = int(payload.get("months", 1))
    payment_ref = payload.get("payment_ref", "")
    paid_until  = payload.get("paid_until", "")

    if not user_id:
        raise HTTPException(400, "缺少 user_id")

    result = _sub_mgr.activate(
        user_id=user_id,
        tier=tier,
        months=months,
        payment_ref=payment_ref,
        paid_until_override=paid_until,
    )

    if result["success"]:
        # 推播 LINE 通知給用戶
        msg = (
            f"付款確認，訂閱已開通\n"
            f"方案：{result['tier_name']}\n"
            f"有效期限：{result['paid_until']}\n"
            f"付款單號：{result['payment_ref'] or '（無）'}\n\n"
            f"感謝您的訂閱，即刻開始使用 Naomi 建築 AI 服務。"
        )
        asyncio.create_task(asyncio.to_thread(_line_push, user_id, msg))
        logger.info(f"[Payment] 訂閱開通成功：{user_id} → {tier}，到期：{result['paid_until']}")

    return result


@app.post("/payment/suspend")
async def payment_suspend(
    user_id: str,
    reason: str = "訂閱已到期或取消",
    _key: str = Depends(APIKeyHeader(name="X-Admin-Key", auto_error=False))
):
    """管理員手動暫停訂閱（需 X-Admin-Key）"""
    if _key != ADMIN_API_KEY:
        raise HTTPException(403, "未授權")
    if not _sub_mgr:
        raise HTTPException(503, "訂閱管理器未啟用")
    ok = _sub_mgr.suspend(user_id, reason)
    if ok:
        asyncio.create_task(asyncio.to_thread(
            _line_push, user_id,
            f"您的 Naomi 訂閱已暫停。\n原因：{reason}\n如有疑問請聯繫客服。"
        ))
    return {"success": ok, "user_id": user_id}


@app.get("/admin/subscription/{user_id}")
def admin_get_subscription(
    user_id: str,
    _key: str = Depends(APIKeyHeader(name="X-Admin-Key", auto_error=False))
):
    """查詢用戶訂閱狀態"""
    if _key != ADMIN_API_KEY:
        raise HTTPException(403, "未授權")
    if not _sub_mgr:
        raise HTTPException(503, "訂閱管理器未啟用")
    return _sub_mgr.get_subscription(user_id)


@app.get("/admin/subscription/stats")
def admin_subscription_stats(
    _key: str = Depends(APIKeyHeader(name="X-Admin-Key", auto_error=False))
):
    """訂閱統計報表"""
    if _key != ADMIN_API_KEY:
        raise HTTPException(403, "未授權")
    if not _sub_mgr:
        raise HTTPException(503, "訂閱管理器未啟用")
    return _sub_mgr.get_stats()


@app.get("/api/live_feed")
def api_live_feed(since: str = "", limit: int = 30):
    """
    即時對話 Feed — 回傳 since 之後的最新記錄
    since: ISO datetime 字串，留空回傳最新 limit 筆
    """
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            if since:
                rows = conn.execute("""
                    SELECT id, user_id, squad_key, intent, query, response,
                           llm_used, response_time_ms, created_at
                    FROM consultation_records
                    WHERE created_at > ?
                    ORDER BY created_at DESC LIMIT ?
                """, (since, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT id, user_id, squad_key, intent, query, response,
                           llm_used, response_time_ms, created_at
                    FROM consultation_records
                    ORDER BY created_at DESC LIMIT ?
                """, (limit,)).fetchall()
        return [
            {"id": r[0], "user_id": r[1], "squad": r[2], "intent": r[3],
             "query": r[4], "response": r[5], "llm_used": r[6] or "",
             "response_time_ms": r[7] or 0, "created_at": r[8]}
            for r in rows
        ]
    except Exception as e:
        return []


@app.get("/api/llm_stats")
def api_llm_stats(days: int = 30):
    """LLM 費用與使用統計（供後台 dashboard 使用）"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            # 各 provider 費用
            by_provider = conn.execute("""
                SELECT provider, model,
                       COUNT(*) as calls,
                       SUM(input_tokens)  as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(cost_usd)      as total_cost
                FROM llm_cost_log
                WHERE ts >= date('now', ? )
                GROUP BY provider, model
                ORDER BY total_cost DESC
            """, (f"-{days} days",)).fetchall()

            # 每日費用趨勢
            daily = conn.execute("""
                SELECT date(ts) as day,
                       provider,
                       SUM(cost_usd) as cost,
                       COUNT(*) as calls
                FROM llm_cost_log
                WHERE ts >= date('now', ?)
                GROUP BY day, provider
                ORDER BY day
            """, (f"-{days} days",)).fetchall()

            # 角色熱圖：哪個 role_key 最耗費
            by_role = conn.execute("""
                SELECT role_key,
                       COUNT(*) as calls,
                       SUM(cost_usd) as cost
                FROM llm_cost_log
                WHERE ts >= date('now', ?)
                  AND role_key != ''
                GROUP BY role_key
                ORDER BY cost DESC LIMIT 20
            """, (f"-{days} days",)).fetchall()

        return {
            "by_provider": [
                {"provider": r[0], "model": r[1], "calls": r[2],
                 "input_tokens": r[3], "output_tokens": r[4], "cost_usd": round(r[5] or 0, 6)}
                for r in by_provider
            ],
            "daily_trend": [
                {"day": r[0], "provider": r[1], "cost_usd": round(r[2] or 0, 6), "calls": r[3]}
                for r in daily
            ],
            "by_role": [
                {"role_key": r[0], "calls": r[1], "cost_usd": round(r[2] or 0, 6)}
                for r in by_role
            ],
        }
    except Exception as e:
        return {"error": str(e), "by_provider": [], "daily_trend": [], "by_role": []}


@app.get("/api/users_overview")
def api_users_overview(
    _key: str = Depends(APIKeyHeader(name="X-Admin-Key", auto_error=False))
):
    """用戶總覽（訂閱 + 使用量）"""
    if _key != ADMIN_API_KEY:
        raise HTTPException(403, "未授權")
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            rows = conn.execute("""
                SELECT s.user_id, s.tier, s.status, s.paid_until,
                       up.preferred_name, up.tenant_id,
                       COUNT(cr.id) as total_calls,
                       MAX(cr.created_at) as last_active
                FROM subscriptions s
                LEFT JOIN user_profiles up ON s.user_id = up.user_id
                LEFT JOIN consultation_records cr ON s.user_id = cr.user_id
                GROUP BY s.user_id
                ORDER BY last_active DESC
            """).fetchall()
        return [
            {"user_id": r[0], "tier": r[1], "status": r[2], "paid_until": r[3],
             "name": r[4] or "", "tenant_id": r[5] or "",
             "total_calls": r[6] or 0, "last_active": r[7] or ""}
            for r in rows
        ]
    except Exception as e:
        return {"error": str(e)}


@app.post("/admin/ingest_law")
async def admin_ingest_law(
    request: Request,
    _key: str = Depends(APIKeyHeader(name="X-Admin-Key", auto_error=False))
):
    """
    從後台上傳 PDF 並觸發法規入庫
    支援 multipart/form-data (file) 或 JSON { "pending_file": "filename.pdf" }
    """
    if _key != ADMIN_API_KEY:
        raise HTTPException(403, "未授權")

    content_type = request.headers.get("content-type", "")
    pdf_path = None

    if "multipart" in content_type:
        # 前端上傳 PDF 檔案
        from fastapi import UploadFile, File
        form = await request.form()
        upload: UploadFile = form.get("file")
        if not upload:
            raise HTTPException(400, "未找到 file 欄位")
        file_bytes = await upload.read()
        save_path = PENDING_LAWS_DIR / upload.filename
        save_path.write_bytes(file_bytes)
        pdf_path = save_path
        logger.info(f"[IngestLaw] 上傳儲存：{save_path}")
    else:
        # JSON 指定 pending_laws 已有的檔案名
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "無效的請求格式")
        fname = body.get("pending_file", "")
        if not fname:
            raise HTTPException(400, "請指定 pending_file 檔名")
        pdf_path = PENDING_LAWS_DIR / fname
        if not pdf_path.exists():
            raise HTTPException(404, f"找不到 {fname}，請先上傳至 pending_laws/")

    # 呼叫 Squad03 M06 入庫
    try:
        sq03 = squad_manager.squads.get("03_regulatory_intel", {})
        squad_inst = sq03.get("instance")
        if squad_inst and hasattr(squad_inst, "ingest_pdf"):
            result = await squad_inst.ingest_pdf(str(pdf_path))
        else:
            # 直接呼叫 law_import_pipeline
            from tools.law_import_pipeline import LawImportPipeline
            pipeline = LawImportPipeline(db_path=str(DB_PATH), chroma_path=CHROMA_PATH)
            result = await asyncio.to_thread(pipeline.import_single, str(pdf_path))
        return {"success": True, "file": pdf_path.name, "result": result}
    except Exception as e:
        logger.error(f"[IngestLaw] 入庫失敗：{e}")
        raise HTTPException(500, f"入庫失敗：{e}")


@app.get("/admin/law_imports")
def admin_law_imports(
    limit: int = 50,
    _key: str = Depends(APIKeyHeader(name="X-Admin-Key", auto_error=False))
):
    """法規入庫記錄（正確資料表）"""
    if _key != ADMIN_API_KEY:
        raise HTTPException(403, "未授權")
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            rows = conn.execute("""
                SELECT id, file_name, city, law_type, law_name,
                       chunks_count, vector_count, status, error_msg, imported_at
                FROM law_imports ORDER BY imported_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [
            {"id": r[0], "file_name": r[1], "city": r[2], "law_type": r[3],
             "law_name": r[4], "chunks": r[5], "vectors": r[6],
             "status": r[7], "error": r[8] or "", "imported_at": r[9]}
            for r in rows
        ]
    except Exception as e:
        return {"error": str(e)}


@app.get("/admin/pending_laws")
def admin_pending_laws(
    _key: str = Depends(APIKeyHeader(name="X-Admin-Key", auto_error=False))
):
    """列出 pending_laws/ 等待入庫的檔案"""
    if _key != ADMIN_API_KEY:
        raise HTTPException(403, "未授權")
    files = []
    for f in PENDING_LAWS_DIR.iterdir():
        if f.suffix.lower() == ".pdf":
            files.append({"name": f.name, "size_kb": round(f.stat().st_size / 1024, 1)})
    return sorted(files, key=lambda x: x["name"])


@app.get("/api/system_detail")
def api_system_detail():
    """系統詳細狀態：工具、技能、智能體、DB 表"""
    # 工具 & 技能
    skills_info = {k: str(type(v).__name__) for k, v in hub.skills.items()}
    tools_info  = {k: str(type(v).__name__) for k, v in hub.tools.items()}

    # Squad 詳情
    squads_info = []
    for key, sq in squad_manager.squads.items():
        meta = sq.get("meta", {})
        squads_info.append({
            "key":      key,
            "name":     meta.get("name", key),
            "triggers": meta.get("triggers", [])[:5],
            "members":  meta.get("members", []),
        })

    # DB 表格列表 + 筆數
    db_tables = []
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            for (tbl,) in tables:
                try:
                    cnt = conn.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]
                except Exception:
                    cnt = -1
                db_tables.append({"table": tbl, "rows": cnt})
    except Exception:
        pass

    return {
        "skills":  skills_info,
        "tools":   tools_info,
        "squads":  squads_info,
        "db_tables": db_tables,
    }


@app.get("/api/stats")
def get_stats():
    return {
        "brain": brain_manager.get_stats(),
        "consultation": consultation_recorder.get_analytics(30)
    }


@app.get("/api/squads")
def get_squads():
    result = []
    for key, squad in squad_manager.squads.items():
        meta = squad.get("meta", {})
        result.append({
            "key": key,
            "name": meta.get("display_name", key),
            "triggers": meta.get("skills", [])[:5]
        })
    return {"squads": result}


@app.get("/api/skills")
def get_skills():
    return {
        "skills": list(hub.skills.keys()),
        "tools": list(hub.tools.keys())
    }


# ------------------------------------------------------------------------------
# 知識缺口管理 API（管理員審核）
# ------------------------------------------------------------------------------

@app.get("/admin/gaps")
def admin_get_gaps(limit: int = 30):
    """取得待審核的知識缺口清單"""
    return {
        "gaps": gap_manager.get_pending_gaps(limit),
        "stats": gap_manager.get_stats()
    }


@app.post("/admin/gaps/{gap_id}/answer")
async def admin_answer_gap(gap_id: str, request: Request):
    """管理員提供正確答案 → 存入知識庫"""
    body = await request.json()
    admin_answer = body.get("answer", "").strip()
    if not admin_answer:
        raise HTTPException(status_code=400, detail="answer 不可為空")
    success = gap_manager.answer_gap(gap_id, admin_answer)
    if not success:
        raise HTTPException(status_code=404, detail=f"找不到 gap_id: {gap_id}")
    return {"status": "ok", "gap_id": gap_id, "message": "已存入知識庫"}


@app.post("/admin/gaps/{gap_id}/dismiss")
def admin_dismiss_gap(gap_id: str):
    """忽略一個缺口（不需要回答）"""
    gap_manager.dismiss_gap(gap_id)
    return {"status": "ok", "gap_id": gap_id}


@app.get("/admin/knowledge/stats")
def admin_knowledge_stats():
    """知識庫統計"""
    return gap_manager.get_stats()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Naomi V21.1 監控後台</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen">
    <nav class="bg-indigo-600 text-white p-4">
        <div class="container mx-auto flex justify-between items-center">
            <h1 class="text-xl font-bold">🏗️ Naomi V21.1</h1>
            <span id="status" class="text-green-300">● 運行中</span>
        </div>
    </nav>
    
    <main class="container mx-auto p-4">
        <!-- 統計卡片 -->
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div class="bg-white rounded-lg shadow p-4">
                <div class="text-3xl font-bold text-blue-600" id="total-calls">-</div>
                <div class="text-gray-500 text-sm">今日調用</div>
            </div>
            <div class="bg-white rounded-lg shadow p-4">
                <div class="text-3xl font-bold text-green-600" id="total-cost">$-</div>
                <div class="text-gray-500 text-sm">今日成本</div>
            </div>
            <div class="bg-white rounded-lg shadow p-4">
                <div class="text-3xl font-bold text-purple-600" id="consultations">-</div>
                <div class="text-gray-500 text-sm">30天諮詢</div>
            </div>
            <div class="bg-white rounded-lg shadow p-4">
                <div class="text-3xl font-bold text-orange-600" id="users">-</div>
                <div class="text-gray-500 text-sm">30天用戶</div>
            </div>
        </div>
        
        <!-- 智能體群 & LLM -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="bg-white rounded-lg shadow p-4">
                <h2 class="font-semibold mb-4">👥 智能體群</h2>
                <div id="squads-list" class="space-y-2"></div>
            </div>
            <div class="bg-white rounded-lg shadow p-4">
                <h2 class="font-semibold mb-4">🧠 LLM 使用統計</h2>
                <div id="llm-stats" class="space-y-2"></div>
            </div>
        </div>
        
        <!-- 技能 & 工具 -->
        <div class="bg-white rounded-lg shadow p-4 mt-4">
            <h2 class="font-semibold mb-4">🔧 技能 & 工具</h2>
            <div id="skills-tools"></div>
        </div>
    </main>
    
    <script>
        async function fetchData() {
            try {
                // Stats
                const statsRes = await fetch('/api/stats');
                const stats = await statsRes.json();
                
                document.getElementById('total-calls').textContent = stats.brain.total_calls || 0;
                document.getElementById('total-cost').textContent = '$' + (stats.brain.total_cost || 0).toFixed(4);
                document.getElementById('consultations').textContent = stats.consultation.total_consultations || 0;
                document.getElementById('users').textContent = stats.consultation.unique_users || 0;
                
                // LLM
                const llmDiv = document.getElementById('llm-stats');
                llmDiv.innerHTML = '';
                for (const [llm, s] of Object.entries(stats.brain.by_llm || {})) {
                    llmDiv.innerHTML += `
                        <div class="p-2 bg-gray-50 rounded flex justify-between">
                            <span class="font-medium">${llm.toUpperCase()}</span>
                            <span>${s.calls} 次 | $${s.cost.toFixed(4)}</span>
                        </div>
                    `;
                }
                
                // Squads
                const squadsRes = await fetch('/api/squads');
                const squadsData = await squadsRes.json();
                const squadsDiv = document.getElementById('squads-list');
                squadsDiv.innerHTML = squadsData.squads.map(s => 
                    `<div class="p-2 bg-green-50 text-green-800 rounded">
                        <strong>${s.name}</strong>
                        <div class="text-xs text-gray-500">${s.triggers.slice(0, 3).join(', ')}</div>
                    </div>`
                ).join('');
                
                // Skills
                const skillsRes = await fetch('/api/skills');
                const skillsData = await skillsRes.json();
                document.getElementById('skills-tools').innerHTML = `
                    <div class="mb-2"><strong>技能：</strong>${skillsData.skills.join(', ') || '無'}</div>
                    <div><strong>工具：</strong>${skillsData.tools.join(', ') || '無'}</div>
                `;
                
            } catch (e) {
                console.error('更新失敗:', e);
            }
        }
        
        fetchData();
        setInterval(fetchData, 10000);
    </script>
</body>
</html>
"""


# ==============================================================================
# 16. 啟動
# ==============================================================================

async def _on_startup():
    """Naomi 啟動後自動執行（由 lifespan 呼叫）"""
    logger.info("[Startup] 背景初始化中...")

    async def _startup_tasks():
        await asyncio.sleep(5)  # 等主服務穩定

        # 1. SkillHunter 啟動掃描
        if _skill_hunter:
            try:
                logger.info("[Startup] 開始技能缺口掃描...")
                result = await _skill_hunter.run_scan()
                if result["proposals"] > 0:
                    logger.info(f"[Startup] 技能掃描完成：{result['proposals']} 個提案已推播")
                else:
                    logger.info("[Startup] 技能掃描完成：無新提案")
            except Exception as e:
                logger.warning(f"[Startup] 技能掃描失敗: {e}")

        # 2. ActiveMonitor — 啟動統一排程器（含 SKA + 健康檢查 + 定期任務）
        try:
            from utils.active_monitor import ActiveMonitor
            _active_monitor = ActiveMonitor(
                hub=gateway,
                push_fn=_line_push_async,
                admin_ids=ADMIN_LINE_USER_IDS or ([_owner_id] if _owner_id else []),
                scheduler=gateway._reminder._scheduler if (
                    gateway._reminder and gateway._reminder._scheduler
                ) else None,
            )
            _active_monitor.start()
            # 啟動自檢（SKA + HealthCheck），不做 LawCrawler
            startup_check = await _active_monitor.run_startup_checks()
            logger.info(f"[Startup] ActiveMonitor 就緒，啟動自檢：{startup_check['overall']}")
            # 把 active_monitor 掛到 gateway 方便後續查詢
            gateway.active_monitor = _active_monitor
        except Exception as e:
            logger.warning(f"[Startup] ActiveMonitor 啟動失敗：{e}")

        # 3. 公司知識庫 KBWatcher — 監控 squads/*/knowledge/ 自動入庫
        try:
            from utils.kb_manager import KBManager, KBWatcher
            _kb_mgr     = KBManager()
            _kb_watcher = KBWatcher(_kb_mgr)
            await _kb_watcher.start()
            stats = _kb_mgr.stats()
            total_files  = sum(s["files"]  for s in stats.values())
            total_chunks = sum(s["chunks"] for s in stats.values())
            logger.info(f"[Startup] 公司知識庫就緒：{total_files} 份文件 / {total_chunks} chunks")
        except Exception as e:
            logger.warning(f"[Startup] 公司知識庫啟動失敗：{e}")

        # 4. 多平台 Gateway（Telegram / Discord）
        try:
            from utils.platform_gateway import PlatformManager
            _platform_mgr = PlatformManager(gateway)
            _platform_mgr.setup_from_env()
            if _platform_mgr.adapters:
                await _platform_mgr.start_all()
                gateway.platform_manager = _platform_mgr
                logger.info(f"[Startup] 多平台 Gateway 啟動：{len(_platform_mgr.adapters)} 個平台")
            else:
                logger.info("[Startup] 多平台 Gateway：僅啟用 LINE（.env 未設定其他 token）")
        except Exception as e:
            logger.warning(f"[Startup] 多平台 Gateway 啟動失敗：{e}")

        # 5. Heartbeat 自律心跳（30分鐘巡檢）
        try:
            from utils.heartbeat import HeartbeatManager
            _hb = HeartbeatManager(
                push_fn=_line_push_async,
                admin_ids=ADMIN_LINE_USER_IDS or ([_owner_id] if _owner_id else []),
                db_path=str(DB_PATH),
            )
            _hb.start()
            gateway.heartbeat = _hb
            logger.info("[Startup] Heartbeat 自律心跳已啟動（30分鐘週期）")
        except Exception as e:
            logger.warning(f"[Startup] Heartbeat 啟動失敗：{e}")

        # 5. UPIS 未完成項目自動重試
        try:
            import sqlite3 as _sq
            from pathlib import Path as _P
            _upis_db = _P(__file__).parent / "database" / "naomi_main.db"
            _conn = _sq.connect(str(_upis_db))
            _stuck = _conn.execute(
                "SELECT projnum, projname FROM upis_ingested "
                "WHERE status IN ('failed','downloading','ingesting')"
            ).fetchall()
            _conn.close()
            if _stuck:
                logger.info(f"[Startup] 發現 {len(_stuck)} 筆 UPIS 未完成，啟動自動重試...")
                from utils.upis_fetcher import UPISFetcher, UPISPlan, _PDF_BASE
                _upis_r = UPISFetcher()
                # 重試清單：直接用 projnum 建構 plan 物件
                _retry_plans = [
                    UPISPlan(
                        projnum=r[0], projname=r[1] or f"計畫{r[0]}",
                        pdf_url=f"{_PDF_BASE}/P{r[0]}.pdf",
                        has_pdf=True, district="松山區"
                    )
                    for r in _stuck
                ]
                # 清除 stuck 狀態，讓 ensure_ingested 重新處理
                _conn2 = _sq.connect(str(_upis_db))
                _conn2.execute(
                    "DELETE FROM upis_ingested WHERE status IN ('failed','downloading','ingesting')"
                )
                _conn2.commit()
                _conn2.close()
                _fire_and_forget(_upis_r.ensure_ingested(_retry_plans))
            else:
                logger.info("[Startup] UPIS 無未完成項目")
        except Exception as e:
            logger.warning(f"[Startup] UPIS 重試失敗：{e}")

        # 6. 每日早報（若今天還沒發過）
        if _event_processor and _owner_id:
            try:
                today = datetime.date.today().isoformat()
                brief = _event_processor.get_daily_brief()
                # 只在早上 6-10 點推播早報
                hour = datetime.datetime.now().hour
                if 6 <= hour <= 10:
                    await _line_push_async(_owner_id, brief)
                    logger.info("[Startup] 早報已推播")
            except Exception as e:
                logger.warning(f"[Startup] 早報推播失敗: {e}")

    asyncio.create_task(_startup_tasks())

    # 訂閱到期排程（每 24 小時巡查）
    if _sub_mgr:
        await _sub_mgr.start_expiry_scheduler()


def _start_dashboard():
    """背景啟動 Streamlit 後台（port 8501）"""
    import subprocess, sys, pathlib
    dashboard_path = pathlib.Path(__file__).parent / "dashboard.py"
    if not dashboard_path.exists():
        logger.warning("[Dashboard] dashboard.py 不存在，跳過啟動")
        return None
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "streamlit", "run",
                str(dashboard_path),
                "--server.port", "8501",
                "--server.headless", "true",
                "--browser.gatherUsageStats", "false",
                "--logger.level", "error",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(f"[Dashboard] Streamlit 後台啟動 PID={proc.pid} → http://localhost:8501")
        return proc
    except Exception as e:
        logger.warning(f"[Dashboard] 啟動失敗：{e}")
        return None


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Naomi V21.1 — 台灣建築開發 AI 系統")
    logger.info("=" * 60)
    logger.info(f"🧠 可用 LLM: {list(brain_manager.clients.keys())}")
    logger.info(f"🔧 技能: {list(hub.skills.keys())}")
    logger.info(f"🛠️ 工具: {list(hub.tools.keys())}")
    logger.info(f"👥 智能體群: {list(squad_manager.squads.keys())}")
    logger.info("=" * 60)
    logger.info("主服務:  http://0.0.0.0:8000")
    logger.info("後台:    http://localhost:8501")
    logger.info("=" * 60)

    _dash_proc = _start_dashboard()

    # log_config=None 避免 uvicorn 的 ColorFormatter 在 stdout 已關閉時崩潰
    _uvicorn_log_cfg = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "logging.Formatter",
                "fmt": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            "uvicorn":        {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error":  {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["default"], "level": "WARNING", "propagate": False},
        },
    }

    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_config=_uvicorn_log_cfg)
    finally:
        if _dash_proc:
            _dash_proc.terminate()
            logger.info("[Dashboard] Streamlit 後台已關閉")
