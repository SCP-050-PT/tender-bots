"""
mcp_server.py v4.1 (Logging Enhanced)
MCP-over-SSE сервер для Yandex AI Studio.
v4.1: Добавлено детальное логирование вызовов инструментов для отладки "мышления" агента.
"""

import os
import json
import uuid
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any
from loguru import logger
import time
from fastapi import FastAPI, Request, Query, Header, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

# Библиотеки для парсинга документов
try:
    from docx import Document as DocxDocument
    from openpyxl import load_workbook
    import fitz  # PyMuPDF
except ImportError as e:
    raise RuntimeError(
        f"Missing libs: {e}. Install: pip install python-docx openpyxl pymupdf"
    )

app = FastAPI(title="Tender-Bot MCP Server", version="4.1.0")
DOWNLOADS_DIR = Path("/opt/tender-bot/core/downloads")

# Хранилище активных сессий: session_id -> asyncio.Queue
sessions: Dict[str, asyncio.Queue] = {}

# Настройка логгера для MCP-сервера
mcp_logger = logger.bind(type="mcp_server")


class MCPCallRequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str
    params: Optional[Dict[str, Any]] = None
    id: Optional[Any] = None


# === Вспомогательные функции парсинга ===
def find_files_by_tender_id(tender_id: str, max_retries: int = 2) -> List[Path]:
    """
    Ищет файлы тендера.
    v4.2: Добавлен fallback по времени модификации для файлов без tender_id в имени.
    """
    for attempt in range(max_retries):
        if not DOWNLOADS_DIR.exists():
            if attempt < max_retries - 1:
                logger.warning(f" Папка {DOWNLOADS_DIR} еще не создана. Ждем 5с...")
                time.sleep(5)
                continue
            return []

        # 1. Поиск по ID в имени файла
        files = [
            f for f in DOWNLOADS_DIR.iterdir() if f.is_file() and tender_id in f.name
        ]

        if files:
            logger.info(
                f"✅ Найдено {len(files)} файлов для тендера {tender_id} (по ID)"
            )
            return files

        # 2. Fallback: Поиск свежих файлов (последние 10 минут)
        # Это решает проблему, когда файлы сохраняются как filename_timestamp.ext
        current_time = time.time()
        recent_files = [
            f
            for f in DOWNLOADS_DIR.iterdir()
            if f.is_file() and (current_time - f.stat().st_mtime) < 600
        ]

        if recent_files:
            logger.warning(
                f"⚠️ Файлы по ID {tender_id} не найдены. "
                f"Использую {len(recent_files)} свежих файлов (последние 10 мин)."
            )
            return recent_files

        # Если файлов нет и это не последняя попытка — ждем
        if attempt < max_retries - 1:
            logger.warning(f" Файлы для {tender_id} пока не найдены. Ждем 10с...")
            time.sleep(10)

    logger.error(
        f"❌ После {max_retries} попыток файлы для тендера {tender_id} так и не найдены"
    )
    return []


def extract_text_from_docx(file_path: Path) -> str:
    try:
        doc = DocxDocument(str(file_path))
        return "\n".join([p.text for p in doc.paragraphs])
    except Exception:
        return ""


def extract_text_from_pdf(file_path: Path) -> str:
    try:
        txt = ""
        with fitz.open(str(file_path)) as pdf:
            for page in pdf:
                txt += page.get_text() + "\n"
        return txt
    except Exception:
        return ""


def search_in_text(text: str, query: str, context_chars: int = 300) -> List[Dict]:
    results = []
    q_lower = query.lower()
    t_lower = text.lower()
    start = 0
    while True:
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


# === Описание инструментов MCP ===
TOOLS = [
    {
        "name": "list_tender_files",
        "description": "Получить список документов тендера по его ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"tender_id": {"type": "string"}},
            "required": ["tender_id"],
        },
    },
    {
        "name": "search_documents",
        "description": "Поиск текста в документах тендера с контекстом.",
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
        "description": "Прочитать Excel-файл тендера как JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tender_id": {"type": "string"},
                "sheet_name": {"type": "string"},
            },
            "required": ["tender_id"],
        },
    },
]


async def execute_tool(name: str, args: Dict) -> Any:
    # ЛОГИРОВАНИЕ ВЫЗОВА ИНСТРУМЕНТА
    mcp_logger.info(f"🛠️ АГЕНТ ВЫЗВАЛ ИНСТРУМЕНТ: {name}")
    mcp_logger.info(f"   Аргументы: {json.dumps(args, ensure_ascii=False)}")

    result = None

    if name == "list_tender_files":
        files = find_files_by_tender_id(args.get("tender_id", ""))
        if not files:
            result = f"Файлы для тендера {args.get('tender_id')} не найдены."
        else:
            result_list = []
            for f in files:
                ext = f.suffix.lower()
                ftype = "unknown"
                if ".docx" in ext:
                    ftype = "docx"
                elif ".xlsx" in ext or ".xls" in ext:
                    ftype = "excel"
                elif ".pdf" in ext:
                    ftype = "pdf"
                result_list.append(
                    f"- {f.name} ({ftype}, {round(f.stat().st_size / 1024, 1)} KB)"
                )
            result = "\n".join(result_list)

    elif name == "search_documents":
        files = find_files_by_tender_id(args.get("tender_id", ""))
        if not files:
            result = f"Файлы для тендера {args.get('tender_id')} не найдены."
        else:
            query = args.get("query", "")
            ctx = args.get("context_chars", 300)
            all_results = []
            for fp in files:
                txt = ""
                if ".docx" in fp.suffix:
                    txt = extract_text_from_docx(fp)
                elif ".pdf" in fp.suffix:
                    txt = extract_text_from_pdf(fp)
                matches = search_in_text(txt, query, ctx)
                if matches:
                    file_res = f"\n Файл: {fp.name}\n"
                    for m in matches[:3]:
                        file_res += f"   Найдено: \"{m['match']}\"\n   Контекст: ...{m['context']}...\n"
                    all_results.append(file_res)
            if not all_results:
                result = f'Запрос "{query}" не найден в документах тендера {args.get("tender_id")}.'
            else:
                result = "".join(all_results)

    elif name == "read_excel_table":
        files = find_files_by_tender_id(args.get("tender_id", ""))
        excels = [f for f in files if ".xlsx" in f.suffix or ".xls" in f.suffix]

        if not excels:
            result = "Excel-файлы не найдены."
        else:
            target = excels[0]
            sheet_name = args.get("sheet_name")
            try:
                wb = load_workbook(str(target), read_only=True, data_only=True)
                ws = (
                    wb[sheet_name]
                    if sheet_name and sheet_name in wb.sheetnames
                    else wb.active
                )

                rows = []
                summary_row = None

                # Читаем все строки, но запоминаем только заголовки, начало и итог
                for row in ws.iter_rows(values_only=True):
                    cleaned = [str(c) if c is not None else "" for c in row]
                    if any(c.strip() for c in cleaned):
                        rows.append(cleaned)

                        # Ищем строку с итого/всего
                        row_text = " ".join(cleaned).lower()
                        if (
                            "итого" in row_text
                            or "всего" in row_text
                            or "total" in row_text
                        ) and len(row_text) < 50:
                            summary_row = cleaned

                wb.close()

                total_rows = len(rows)
                output = f"📊 Файл: {target.name}, Лист: {ws.title}, Всего строк: {total_rows}\n"

                if summary_row:
                    output += f"✅ НАЙДЕНА СТРОКА ИТОГО: {summary_row}\n"
                else:
                    output += "⚠️ Строка 'Итого' не найдена. Показываю первые и последние строки.\n"

                # Возвращаем контекст: заголовок + первые 5 строк + (если есть итог или последние 3)
                context_rows = rows[:6]
                if summary_row:
                    context_rows.append(["...", "...", "..."])
                    context_rows.append(summary_row)
                elif total_rows > 8:
                    context_rows.append(["...", "...", "..."])
                    context_rows.extend(rows[-3:])

                output += "\n".join([str(r) for r in context_rows])
                result = output

            except Exception as e:
                result = f"Ошибка чтения Excel: {e}"
    else:
        result = {"error": f"Unknown tool: {name}"}

    # ЛОГИРОВАНИЕ РЕЗУЛЬТАТА ИНСТРУМЕНТА
    result_preview = (
        str(result)[:300] + "..." if len(str(result)) > 300 else str(result)
    )
    mcp_logger.info(f"✅ РЕЗУЛЬТ ИНСТРУМЕНТА {name}: {result_preview}")

    return result


# === Эндпоинты MCP (HTTP with SSE) ===


@app.get("/")
async def root():
    """Health-check endpoint для клиента."""
    return {
        "status": "ok",
        "service": "tender-mcp-server",
        "version": "4.1.0",
        "protocol": "MCP-over-SSE",
        "endpoints": {"sse": "/sse", "messages": "/messages"},
    }


@app.get("/sse")
async def sse_endpoint(request: Request):
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[session_id] = queue

    async def event_generator():
        try:
            # 1. НЕМЕДЛЕННО отправляем endpoint (до начала цикла!)
            yield "event: endpoint\ndata: /messages\n\n"

            # 2. Основной цикл чтения из очереди
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
            "Transfer-Encoding": "chunked",
        },
    )


@app.post("/messages")
async def messages_endpoint(
    req: MCPCallRequest,
    sessionId: Optional[str] = Query(None),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
):
    """
    Принимает MCP-запросы от клиента.
    АВТОМАТИЧЕСКИ привязывает запрос к ПОСЛЕДНЕЙ АКТИВНОЙ сессии.
    """
    sid = x_session_id or sessionId

    if not sid and sessions:
        sid = next(reversed(sessions))

    if not sid or sid not in sessions:
        raise HTTPException(
            status_code=404,
            detail=f"Session not found: {sid}. Active sessions: {len(sessions)}",
        )

    queue = sessions[sid]
    method = req.method
    response_msg = {"jsonrpc": "2.0", "id": req.id}

    if method == "initialize":
        response_msg["result"] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tender-mcp", "version": "4.1.0"},
        }
    elif method == "tools/list":
        response_msg["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        params = req.params or {}
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            result = await execute_tool(tool_name, args)
            response_msg["result"] = {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                ]
            }
        except Exception as e:
            mcp_logger.error(f"❌ ОШИБКА ВЫПОЛНЕНИЯ ИНСТРУМЕНТА {tool_name}: {e}")
            response_msg["result"] = {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True,
            }
    else:
        response_msg["error"] = {"code": -32601, "message": "Method not found"}

    await queue.put(response_msg)
    return JSONResponse(status_code=202, content={"status": "accepted"})


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    # Добавляем логирование в файл для MCP-сервера
    logger.add(
        "/opt/tender-bot/logs/mcp_server_{time:YYYYMMDD}.log",
        level="INFO",
        rotation="10 MB",
        retention="7 days",
        format="{time:HH:mm:ss} | {level} | {message}",
    )
    uvicorn.run(app, host="0.0.0.0", port=8000)
