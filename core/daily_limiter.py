"""
core/daily_limiter.py
Лимитер запусков + очистка файлов + кэш дубликатов.
v7.3.0
"""

import json
import time
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import Tuple, Dict, Any, Optional
from loguru import logger


class DailyLimiter:
    """Контролирует лимиты запусков, регулярную очистку файлов и кэш дубликатов."""

    VERSION = "v7.3.0"

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        self.data_dir = self.base_dir / "data"
        self.downloads_dir = self.base_dir / "core" / "downloads"
        
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.data_dir / "daily_limiter.json"
        self.cache_file = self.data_dir / "tender_cache_ids.json"

        # === ЛИМИТЫ ===
        self.MAX_PER_RUN = 10   # Максимум тендеров за запуск
        self.MAX_PER_DAY = 60   # Максимум тендеров в день
        self.MAX_PER_HOUR = 15  # Максимум тендеров в час

        # === ОЧИСТКА ===
        self.DOWNLOADS_MAX_AGE_DAYS = 1  # downloads/ — 1 день
        self.DATA_MAX_AGE_DAYS = 7       # data/ — 7 дней
        self.CACHE_TTL_DAYS = 3          # Кэш ID — 3 дня

        # Загрузка состояния и кэша
        self.state = self._load_state()
        self.cache = self._load_cache()

    # ================================================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ (Безопасная запись)
    # ================================================================

    def _atomic_save_json(self, filepath: Path, data: Any) -> None:
        """Безопасная запись в JSON через временный файл для защиты от повреждения данных."""
        tmp_file = filepath.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp_file.replace(filepath)
        except Exception as e:
            logger.error(f"[{self.VERSION}] [DailyLimiter] Ошибка атомарной записи {filepath.name}: {e}")
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)

    # ================================================================
    # СОСТОЯНИЕ (счётчики)
    # ================================================================

    def _load_state(self) -> Dict[str, Any]:
        """Загружает текущее состояние лимитов или сбрасывает его при смене дня/часа."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        current_hour = now.hour

        default = {
            "date": today,
            "hour": current_hour,
            "tenders_today": 0,
            "tenders_this_hour": 0,
            "last_run": None,
        }

        if not self.state_file.exists():
            return default

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)

            # Сброс при смене дня
            if state.get("date") != today:
                logger.info(
                    f"[{self.VERSION}] [DailyLimiter] Новый день {today}, сброс счетчика: "
                    f"{state.get('tenders_today', 0)} → 0"
                )
                state["date"] = today
                state["tenders_today"] = 0
                state["tenders_this_hour"] = 0
                state["hour"] = current_hour

            # Сброс при смене часа
            elif state.get("hour") != current_hour:
                state["hour"] = current_hour
                state["tenders_this_hour"] = 0

            return state
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"[{self.VERSION}] [DailyLimiter] Ошибка загрузки состояния ({e}), восстановление по умолчанию")
            return default

    def _save_state(self) -> None:
        self._atomic_save_json(self.state_file, self.state)

    # ================================================================
    # КЭШ ДУБЛИКАТОВ
    # ================================================================

    def _load_cache(self) -> Dict[str, str]:
        """Загружает кэш ID тендеров с автоматической очисткой устаревших записей."""
        if not self.cache_file.exists():
            return {}

        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)

            cutoff_dt = datetime.now() - timedelta(days=self.CACHE_TTL_DAYS)
            expired = []

            for tender_id, timestamp_str in cache.items():
                try:
                    ts = datetime.fromisoformat(timestamp_str)
                    if ts < cutoff_dt:
                        expired.append(tender_id)
                except Exception:
                    # Если формат даты некорректен, проверяем строковым сравнением
                    if timestamp_str < cutoff_dt.isoformat():
                        expired.append(tender_id)

            if expired:
                for k in expired:
                    del cache[k]
                logger.info(
                    f"[{self.VERSION}] [DailyLimiter] Кэш: удалено {len(expired)} устаревших ID "
                    f"(TTL {self.CACHE_TTL_DAYS} дн.), осталось {len(cache)}"
                )
                self._save_cache(cache)

            return cache
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"[{self.VERSION}] [DailyLimiter] Ошибка загрузки кэша ({e}), создается новый кэш")
            return {}

    def _save_cache(self, cache: Optional[Dict[str, str]] = None) -> None:
        self._atomic_save_json(self.cache_file, cache if cache is not None else self.cache)

    def is_cached(self, tender_id: str) -> bool:
        """Проверяет, обрабатывался ли тендер ранее."""
        return str(tender_id) in self.cache

    def add_to_cache(self, tender_id: str) -> None:
        """Добавляет тендер в кэш с текущей меткой времени."""
        self.cache[str(tender_id)] = datetime.now().isoformat()
        self._save_cache()

    def get_cache_size(self) -> int:
        return len(self.cache)

    # ================================================================
    # ПРОВЕРКА ЛИМИТОВ
    # ================================================================

    def can_run(self) -> Tuple[bool, str]:
        """Проверяет возможность запуска агента согласно лимитам."""
        self.state = self._load_state()

        if self.state["tenders_today"] >= self.MAX_PER_DAY:
            reason = f"Дневной лимит исчерпан: {self.state['tenders_today']}/{self.MAX_PER_DAY}"
            logger.warning(f"[{self.VERSION}] [DailyLimiter] 🚫 {reason}")
            return False, reason

        if self.state["tenders_this_hour"] >= self.MAX_PER_HOUR:
            reason = f"Часовой лимит исчерпан: {self.state['tenders_this_hour']}/{self.MAX_PER_HOUR}"
            logger.warning(f"[{self.VERSION}] [DailyLimiter] 🚫 {reason}")
            return False, reason

        return True, ""

    def record_tenders(self, count: int) -> None:
        """Регистрирует количество успешно обработанных тендеров."""
        if count <= 0:
            return

        self.state = self._load_state()
        self.state["tenders_today"] += count
        self.state["tenders_this_hour"] += count
        self.state["last_run"] = datetime.now().isoformat()
        self._save_state()

        logger.info(
            f"[{self.VERSION}] [DailyLimiter] 📊 Зафиксировано +{count} тендеров. "
            f"День: {self.state['tenders_today']}/{self.MAX_PER_DAY}, "
            f"Час: {self.state['tenders_this_hour']}/{self.MAX_PER_HOUR}, "
            f"В кэше: {self.get_cache_size()} ID"
        )

    def get_status(self) -> str:
        """Возвращает форматированную строку статуса лимитов."""
        self.state = self._load_state()
        return (
            f"📊 Лимиты ({self.VERSION}): "
            f"день {self.state['tenders_today']}/{self.MAX_PER_DAY}, "
            f"час {self.state['tenders_this_hour']}/{self.MAX_PER_HOUR}, "
            f"кэш {self.get_cache_size()} ID"
        )

    # ================================================================
    # ОЧИСТКА ФАЙЛОВ И ДИРЕКТОРИЙ
    # ================================================================

    def cleanup_downloads(self) -> None:
        """Очищает папку downloads/ (файлы и папки старше 1 дня)."""
        if not self.downloads_dir.exists():
            return

        cutoff = time.time() - (self.DOWNLOADS_MAX_AGE_DAYS * 86400)
        deleted = 0
        freed_bytes = 0

        for item in self.downloads_dir.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_file():
                        freed_bytes += item.stat().st_size
                        item.unlink()
                        deleted += 1
                    elif item.is_dir():
                        shutil.rmtree(item)
                        deleted += 1
            except Exception as e:
                logger.error(f"[{self.VERSION}] [DailyLimiter] Ошибка удаления {item.name}: {e}")

        if deleted > 0:
            logger.info(
                f"[{self.VERSION}] [DailyLimiter] 🧹 Downloads: удалено объектов: {deleted}, "
                f"освобождено {freed_bytes / 1024 / 1024:.1f} МБ"
            )

    def cleanup_data(self) -> None:
        """Очищает временные файлы в data/ старше 7 дней (исключая служебные базы)."""
        if not self.data_dir.exists():
            return

        cutoff = time.time() - (self.DATA_MAX_AGE_DAYS * 86400)
        deleted = 0
        freed_bytes = 0

        # Служебные файлы, которые нельзя удалять
        protected = {
            "daily_limiter.json",
            "tender_cache_ids.json",
            "tender_cache.db",
            "daily_limiter.tmp",
            "tender_cache_ids.tmp",
        }

        for item in self.data_dir.iterdir():
            if item.name in protected:
                continue

            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_file():
                        freed_bytes += item.stat().st_size
                        item.unlink()
                        deleted += 1
                    elif item.is_dir():
                        shutil.rmtree(item)
                        deleted += 1
            except Exception as e:
                logger.error(f"[{self.VERSION}] [DailyLimiter] Ошибка удаления {item.name}: {e}")

        if deleted > 0:
            logger.info(
                f"[{self.VERSION}] [DailyLimiter] 🧹 Data: удалено объектов: {deleted}, "
                f"освобождено {freed_bytes / 1024 / 1024:.1f} МБ"
            )

    def cleanup_all(self) -> None:
        """Полный цикл очистки перед запуском."""
        self.cleanup_downloads()
        self.cleanup_data()