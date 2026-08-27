"""
core/health_server.py
Мини HTTP-сервер для health-check мониторинга.
Отвечает на GET /health → JSON со статусом бота.
Запускается в отдельном потоке, не мешает основной работе.
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path
from loguru import logger


class HealthState:
    """Хранит состояние бота для health-check."""

    def __init__(self, data_dir: Path = None):
        self.data_dir = data_dir or Path(__file__).resolve().parent.parent / "data"
        self.state_file = self.data_dir / "health_state.json"
        self._lock = threading.Lock()

    def update(self, tenders_processed: int = 0, status: str = "idle", error: str = ""):
        """Обновляет состояние после запуска."""
        with self._lock:
            state = self._load()
            state["last_run"] = datetime.now().isoformat()
            state["last_status"] = status
            state["tenders_last_run"] = tenders_processed
            state["total_runs"] = state.get("total_runs", 0) + 1
            if error:
                state["last_error"] = error
                state["error_count"] = state.get("error_count", 0) + 1
            self._save(state)

    def get_state(self) -> dict:
        """Возвращает текущее состояние."""
        with self._lock:
            state = self._load()
            state["uptime_check"] = datetime.now().isoformat()
            return state

    def _load(self) -> dict:
        default = {
            "bot_name": "tender-bot",
            "version": "7.2.2",
            "started_at": datetime.now().isoformat(),
            "last_run": None,
            "last_status": "never_run",
            "tenders_last_run": 0,
            "total_runs": 0,
            "error_count": 0,
            "last_error": "",
        }
        if not self.state_file.exists():
            return default
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            default.update(saved)
            return default
        except Exception:
            return default

    def _save(self, state: dict):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[HealthServer] Ошибка сохранения: {e}")


# Глобальный экземпляр
_health_state = None


def get_health_state() -> HealthState:
    global _health_state
    if _health_state is None:
        _health_state = HealthState()
    return _health_state


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP-обработчик для /health."""

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            state = get_health_state().get_state()

            # Определяем общий статус
            last_status = state.get("last_status", "never_run")
            error_count = state.get("error_count", 0)

            if last_status == "never_run":
                overall = "warning"  # Ещё не запускался
            elif error_count > 3:
                overall = "critical"  # Много ошибок
            elif last_status == "error":
                overall = "warning"
            else:
                overall = "healthy"

            response = {
                "status": overall,
                "bot": state.get("bot_name", "tender-bot"),
                "version": state.get("version", "?"),
                "started_at": state.get("started_at"),
                "last_run": state.get("last_run"),
                "last_status": last_status,
                "tenders_last_run": state.get("tenders_last_run", 0),
                "total_runs": state.get("total_runs", 0),
                "error_count": error_count,
                "last_error": state.get("last_error", ""),
                "checked_at": state.get("uptime_check"),
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(
                json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8")
            )
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        """Подавляем стандартный лог HTTP-запросов."""
        pass


def start_health_server(port: int = 8080):
    """Запускает health-check сервер в отдельном потоке."""
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"[HealthServer]  Health-check запущен на порту {port}")
        logger.info(f"[HealthServer] URL: http://localhost:{port}/health")
        return server
    except OSError as e:
        logger.warning(f"[HealthServer] Не удалось запустить на порту {port}: {e}")
        return None
