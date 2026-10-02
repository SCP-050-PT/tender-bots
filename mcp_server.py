"""
mcp_server.py v4.3
MCP-over-SSE для tender-bot (Yandex / совместимые клиенты).

v4.3:
  - DOWNLOADS_DIR из env (Windows/VPS)
  - поиск файлов ТОЛЬКО по tender_id в имени (без recent-fallback)
  - read_excel: xlsx (openpyxl) + xls (xlrd), несколько файлов
  - search: docx, pdf, txt-подобные; подсказки query для адресов/ОПР
  - tool find_qty_hints — быстрый поиск количества ОПР/РМ/точек
"""

from __future__ import annotations

import os
import re
import json
import uuid
import asyncio
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from loguru import logger
from fastapi import FastAPI, Request, Query, Header, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

try:
    from docx import Document as DocxDocument
    from openpyxl import load_workbook
    import fitz  # PyMuPDF
except ImportError as e:
    raise RuntimeError(f"Missing libs: {e}. pip install python-docx openpyxl pymupdf")

try:
    import xlrd

    HAS_XLRD = True
except ImportError:
    HAS_XLRD = False

app = FastAPI(title="Tender-Bot MCP Server", version="4.3.0")

# VPS: /opt/tender-bot/core/downloads
# Локально: задайте TENDER_DOWNLOADS_DIR
DOWNLOADS_DIR = Path(
    os.getenv(
        "TENDER_DOWNLOADS_DIR",
        os.getenv("DOWNLOADS_DIR", "/opt/tender-bot/core/downloads"),
    )
)

sessions: Dict[str, asyncio.Queue] = {}
mcp_logger = logger.bind(type="mcp_server")


class MCPCallRequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str
    params: Optional[Dict[str, Any]] = None
    id: Optional[Any] = None


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def find_files_by_tender_id(tender_id: str, max_retries: int = 3) -> List[Path]:
    """Только файлы, в имени которых есть tender_id. Без fallback по mtime."""
    tid = (tender_id or "").strip()
    if not tid:
        return []

    for attempt in range(max_retries):
        if not DOWNLOADS_DIR.exists():
            if attempt < max_retries - 1:
                mcp_logger.warning(f"Нет папки {DOWNLOADS_DIR}, ждём 3с...")
                time.sleep(3)
                continue
            return []

        files = sorted(
            [f for f in DOWNLOADS_DIR.iterdir() if f.is_file() and tid in f.name],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if files:
            mcp_logger.info(f"Найдено {len(files)} файлов для {tid}")
            return files

        if attempt < max_retries - 1:
            mcp_logger.warning(f"Файлы {tid} не найдены, ждём 5с...")
            time.sleep(5)

    mcp_logger.error(f"Файлы для {tid} не найдены в {DOWNLOADS_DIR}")
    return []


def _suffix(p: Path) -> str:
    return p.suffix.lower()


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def extract_text_from_docx(file_path: Path) -> str:
    try:
        doc = DocxDocument(str(file_path))
        parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
        # таблицы — важно для адресов/количеств
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    except Exception as e:
        mcp_logger.debug(f"docx fail {file_path.name}: {e}")
        return ""


def extract_text_from_pdf(file_path: Path) -> str:
    try:
        txt = []
        with fitz.open(str(file_path)) as pdf:
            for page in pdf:
                t = page.get_text()
                if t:
                    txt.append(t)
        return "\n".join(txt)
    except Exception as e:
        mcp_logger.debug(f"pdf fail {file_path.name}: {e}")
        return ""


def extract_text_from_file(file_path: Path) -> str:
    s = _suffix(file_path)
    if s == ".docx":
        return extract_text_from_docx(file_path)
    if s == ".pdf":
        return extract_text_from_pdf(file_path)
    if s in (".txt", ".csv"):
        try:
            return file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    # .doc / прочее — пропускаем (на VPS часто только docx)
    return ""


def search_in_text(text: str, query: str, context_chars: int = 300) -> List[Dict]:
    results = []
    if not text or not query:
        return results
    q_lower = query.lower()
    t_lower = text.lower()
    start = 0
    while len(results) < 15:
        idx = t_lower.find(q_lower, start)
        if idx == -1:
            break
        ctx_s = max(0, idx - context_chars)
        ctx_e = min(len(text), idx + len(query) + context_chars)
        results.append(
            {
                "match": text[idx : idx + len(query)],
                "context": text[ctx_s:ctx_e].replace("\n", " ").strip(),
                "position": idx,
            }
        )
        start = idx + 1
    return results


def _read_xlsx_rows(
    path: Path, sheet_name: Optional[str]
) -> Tuple[str, List[List[str]]]:
    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        title = ws.title
        rows: List[List[str]] = []
        for row in ws.iter_rows(values_only=True):
            cleaned = [str(c).strip() if c is not None else "" for c in row]
            if any(c for c in cleaned):
                rows.append(cleaned)
        return title, rows
    finally:
        wb.close()


def _read_xls_rows(
    path: Path, sheet_name: Optional[str]
) -> Tuple[str, List[List[str]]]:
    if not HAS_XLRD:
        raise RuntimeError("xlrd not installed")
    wb = xlrd.open_workbook(str(path))
    if sheet_name:
        try:
            sheet = wb.sheet_by_name(sheet_name)
        except Exception:
            sheet = wb.sheet_by_index(0)
    else:
        sheet = wb.sheet_by_index(0)
    rows = []
    for r in range(sheet.nrows):
        cleaned = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
        if any(cleaned):
            rows.append(cleaned)
    return sheet.name, rows


def format_excel_preview(rows: List[List[str]], title: str, fname: str) -> str:
    summary_row = None
    for cleaned in rows:
        row_text = " ".join(cleaned).lower()
        if (
            any(k in row_text for k in ("итого", "всего", "total"))
            and len(row_text) < 80
        ):
            summary_row = cleaned

    total_rows = len(rows)
    output = f"Файл: {fname}, Лист: {title}, строк: {total_rows}\n"
    if summary_row:
        output += f"ИТОГО: {summary_row}\n"
    else:
        output += "Строка Итого не найдена — шапка + начало + хвост\n"

    context_rows = rows[:8]
    if summary_row:
        context_rows.append(["..."])
        context_rows.append(summary_row)
    elif total_rows > 10:
        context_rows.append(["..."])
        context_rows.extend(rows[-4:])

    output += "\n".join(str(r) for r in context_rows)
    return output


# ---------------------------------------------------------------------------
# Tools schema
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_tender_files",
        "description": (
            "Список файлов тендера по reg_number / tender_id. "
            "Вызывать ПЕРВЫМ перед search/read_excel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"tender_id": {"type": "string"}},
            "required": ["tender_id"],
        },
    },
    {
        "name": "search_documents",
        "description": (
            "Поиск подстроки в docx/pdf тендера. "
            "Для адресов: query='адрес' или 'место оказания' или 'филиал' или 'г.'. "
            "Для ОПР: 'должност', 'рабочих мест', 'позици', 'оценка профессиональных рисков'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tender_id": {"type": "string"},
                "query": {"type": "string"},
                "context_chars": {"type": "integer"},
            },
            "required": ["tender_id", "query"],
        },
    },
    {
        "name": "read_excel_table",
        "description": (
            "Прочитать xlsx/xls тендера: шапка, первые строки, ИТОГО. "
            "Для количества ОПР/РМ/точек смотри колонки кол-во и строку Итого."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tender_id": {"type": "string"},
                "sheet_name": {"type": "string"},
            },
            "required": ["tender_id"],
        },
    },
    {
        "name": "find_qty_hints",
        "description": (
            "Сводка по объёму: ищет в тексте и Excel числа рядом с "
            "РМ / должност / точек / слушател. Для ОПР и СОУТ."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tender_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "description": "opr | sout | plk | education | any",
                },
            },
            "required": ["tender_id"],
        },
    },
]


QTY_PATTERNS = {
    "opr": [
        re.compile(
            r"(\d{1,5})\s*(?:должност\w*|позици\w*|рабочих\s*мест|рм)\b",
            re.I,
        ),
        re.compile(
            r"(?:должност\w*|позици\w*|рабочих\s*мест)\s*[:\-]?\s*(\d{1,5})",
            re.I,
        ),
    ],
    "sout": [
        re.compile(r"(\d{1,5})\s*(?:рабочих\s*мест|рм)\b", re.I),
        re.compile(r"(?:рабочих\s*мест|рм)\s*[:\-]?\s*(\d{1,5})", re.I),
    ],
    "plk": [
        re.compile(r"(\d{1,5})\s*(?:точек|точки|замер\w*)\b", re.I),
        re.compile(r"(?:точек|замер\w*)\s*[:\-]?\s*(\d{1,5})", re.I),
    ],
    "education": [
        re.compile(r"(\d{1,5})\s*(?:слушател\w*|человек|обучающ\w*)\b", re.I),
    ],
}


def _qty_hints_from_text(text: str, kind: str) -> List[str]:
    kinds = [kind] if kind in QTY_PATTERNS else list(QTY_PATTERNS.keys())
    found = []
    for k in kinds:
        for pat in QTY_PATTERNS[k]:
            for m in pat.finditer(text or ""):
                n = m.group(1)
                try:
                    v = int(n)
                except ValueError:
                    continue
                if 1 <= v <= 20000:
                    found.append(f"{k}: {v} ← …{m.group(0)[:80]}…")
    # unique keep order
    seen = set()
    out = []
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[:20]


async def execute_tool(name: str, args: Dict) -> Any:
    mcp_logger.info(f"TOOL {name} args={json.dumps(args, ensure_ascii=False)}")
    tid = str(args.get("tender_id") or "").strip()

    if name == "list_tender_files":
        files = find_files_by_tender_id(tid)
        if not files:
            return f"Файлы для тендера {tid} не найдены в {DOWNLOADS_DIR}"
        lines = []
        for f in files:
            s = _suffix(f)
            ftype = (
                "excel"
                if s in (".xlsx", ".xls")
                else "docx" if s == ".docx" else "pdf" if s == ".pdf" else s or "file"
            )
            lines.append(
                f"- {f.name} ({ftype}, {round(f.stat().st_size / 1024, 1)} KB)"
            )
        return "\n".join(lines)

    if name == "search_documents":
        files = find_files_by_tender_id(tid)
        if not files:
            return f"Файлы для {tid} не найдены."
        query = str(args.get("query") or "")
        ctx = int(args.get("context_chars") or 300)
        blocks = []
        for fp in files:
            txt = extract_text_from_file(fp)
            if not txt:
                continue
            matches = search_in_text(txt, query, ctx)
            if not matches:
                continue
            chunk = f"\nФайл: {fp.name}\n"
            for m in matches[:5]:
                chunk += f'  "{m["match"]}" → …{m["context"][:400]}…\n'
            blocks.append(chunk)
        return "".join(blocks) if blocks else f'Запрос "{query}" не найден.'

    if name == "read_excel_table":
        files = find_files_by_tender_id(tid)
        excels = [f for f in files if _suffix(f) in (".xlsx", ".xls")]
        if not excels:
            return "Excel-файлы не найдены."
        sheet_name = args.get("sheet_name")
        parts = []
        for target in excels[:3]:
            try:
                if _suffix(target) == ".xlsx":
                    title, rows = _read_xlsx_rows(target, sheet_name)
                else:
                    title, rows = _read_xls_rows(target, sheet_name)
                parts.append(format_excel_preview(rows, title, target.name))
            except Exception as e:
                parts.append(f"Ошибка {target.name}: {e}")
        return "\n\n---\n\n".join(parts)

    if name == "find_qty_hints":
        files = find_files_by_tender_id(tid)
        if not files:
            return f"Файлы для {tid} не найдены."
        kind = str(args.get("kind") or "any").lower()
        hints: List[str] = []
        for fp in files:
            txt = extract_text_from_file(fp)
            if txt:
                for h in _qty_hints_from_text(txt, kind):
                    hints.append(f"[{fp.name}] {h}")
            if _suffix(fp) in (".xlsx", ".xls"):
                try:
                    if _suffix(fp) == ".xlsx":
                        _, rows = _read_xlsx_rows(fp, None)
                    else:
                        _, rows = _read_xls_rows(fp, None)
                    flat = "\n".join(" ".join(r) for r in rows[:50])
                    for h in _qty_hints_from_text(flat, kind):
                        hints.append(f"[{fp.name}:excel] {h}")
                except Exception as e:
                    hints.append(f"[{fp.name}] excel error: {e}")
        if not hints:
            return "Подсказок по количеству не найдено. Вызовите read_excel_table и search_documents."
        return "\n".join(hints[:30])

    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# HTTP / SSE
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "tender-mcp-server",
        "version": "4.3.0",
        "downloads": str(DOWNLOADS_DIR),
        "endpoints": {"sse": "/sse", "messages": "/messages", "health": "/health"},
    }


@app.get("/sse")
async def sse_endpoint(request: Request):
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[session_id] = queue

    async def event_generator():
        try:
            yield "event: endpoint\ndata: /messages\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=10.0)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sessions.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/messages")
async def messages_endpoint(
    req: MCPCallRequest,
    sessionId: Optional[str] = Query(None),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
):
    sid = x_session_id or sessionId
    if not sid and sessions:
        sid = next(reversed(sessions))
    if not sid or sid not in sessions:
        raise HTTPException(
            status_code=404,
            detail=f"Session not found: {sid}. Active: {len(sessions)}",
        )

    queue = sessions[sid]
    method = req.method
    response_msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": req.id}

    if method == "initialize":
        response_msg["result"] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tender-mcp", "version": "4.3.0"},
        }
    elif method == "tools/list":
        response_msg["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        params = req.params or {}
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        try:
            result = await execute_tool(tool_name, arguments)
            response_msg["result"] = {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            result
                            if isinstance(result, str)
                            else json.dumps(result, ensure_ascii=False)
                        ),
                    }
                ]
            }
        except Exception as e:
            mcp_logger.error(f"TOOL ERROR {tool_name}: {e}")
            response_msg["result"] = {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            }
    else:
        response_msg["error"] = {"code": -32601, "message": "Method not found"}

    await queue.put(response_msg)
    return JSONResponse(status_code=202, content={"status": "accepted"})


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "4.3.0",
        "downloads_exists": DOWNLOADS_DIR.exists(),
        "downloads": str(DOWNLOADS_DIR),
    }
@app.post("/call")
async def direct_tool_call(request: Request):
    """Прямой вызов tool без SSE (для FORCE_MCP_DOCS из Python)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    name = (body.get("name") or "").strip()
    arguments = body.get("arguments") or {}
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    try:
        result = await execute_tool(name, arguments)
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        return {"ok": True, "name": name, "result": text}
    except Exception as e:
        mcp_logger.error(f"/call ERROR {name}: {e}")
        return {"ok": False, "name": name, "error": str(e)}

if __name__ == "__main__":
    import uvicorn

    log_path = os.getenv(
        "MCP_LOG_PATH",
        "/opt/tender-bot/logs/mcp_server_{time:YYYYMMDD}.log",
    )
    try:
        logger.add(
            log_path,
            level="INFO",
            rotation="10 MB",
            retention="7 days",
            format="{time:HH:mm:ss} | {level} | {message}",
        )
    except Exception:
        pass

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("MCP_PORT", "8000")))
