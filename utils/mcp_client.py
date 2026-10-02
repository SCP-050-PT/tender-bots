"""
utils/mcp_client.py
Прямые вызовы MCP tools (FORCE_MCP_DOCS).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

MCP_BASE = (os.getenv("MCP_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = float(os.getenv("MCP_CALL_TIMEOUT", "60"))


def call_tool(name: str, arguments: Dict[str, Any]) -> str:
    url = f"{MCP_BASE}/call"
    try:
        r = requests.post(
            url,
            json={"name": name, "arguments": arguments},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            err = data.get("error") or "unknown"
            logger.warning(f"[MCP] tool={name} error={err}")
            return f"ERROR: {err}"
        result = data.get("result") or ""
        logger.info(f"[MCP] tool={name} ok, len={len(result)}")
        return result if isinstance(result, str) else str(result)
    except Exception as e:
        logger.error(f"[MCP] call_tool {name}: {e}")
        return f"ERROR: {e}"


def gather_tender_mcp_context(tender_id: str, tender_type: str) -> str:
    """Все tools по тендеру → один текстовый блок для промпта."""
    tid = (tender_id or "").strip()
    if not tid:
        return "MCP: tender_id пуст"

    qty_q = {
        "sout": "рабочих мест",
        "opr": "должност",
        "plk": "точек",
        "education": "слушател",
    }.get(tender_type, "количество")

    parts: List[str] = [f"=== MCP tool_results tender_id={tid} type={tender_type} ==="]

    files = call_tool("list_tender_files", {"tender_id": tid})
    parts.append(f"\n--- list_tender_files ---\n{files}")

    for q in ("адрес", "место оказания", qty_q):
        res = call_tool(
            "search_documents",
            {"tender_id": tid, "query": q, "context_chars": 350},
        )
        parts.append(f"\n--- search_documents query={q!r} ---\n{res}")

    hints = call_tool(
        "find_qty_hints",
        {"tender_id": tid, "kind": tender_type or "any"},
    )
    parts.append(f"\n--- find_qty_hints ---\n{hints}")

    # Excel, если в списке файлов есть xls
    low = (files or "").lower()
    if ".xls" in low or "excel" in low:
        xl = call_tool("read_excel_table", {"tender_id": tid})
        parts.append(f"\n--- read_excel_table ---\n{xl}")

    text = "\n".join(parts)
    logger.info(f"[MCP] gather done tender_id={tid} total_chars={len(text)}")
    return text
