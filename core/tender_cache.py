"""
Кэш тендеров на SQLite для TENDER-BOT.

Особенности:
- SQLite с WAL-режимом (потокобезопасность на чтение)
- Индексы по reg_number, checked_at, last_check
- TTL + автоочистка старых записей
- Отложенное сохранение (batch insert)
- Потокобезопасность через threading.RLock
"""

import sqlite3
import hashlib
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List, Any, Set, Tuple
from dataclasses import dataclass, asdict, field
from contextlib import contextmanager
from loguru import logger

# ============================================================================
# DATACLASSES
# ============================================================================


@dataclass
class PurchaseState:
    """Состояние закупки для кэширования."""

    reg_number: str
    last_update_date: str
    protocol_count: int = 0
    last_protocol_date: str = ""
    status: str = ""
    protocols_hash: str = ""
    checked_at: str = ""
    has_evaders: bool = False
    is_empty: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PurchaseState":
        # Убираем лишние поля
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)

    @property
    def checked_datetime(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.checked_at)
        except (ValueError, TypeError):
            return None


@dataclass
class TenderResult:
    """Найденный тендер (аналог EvaderRecord)."""

    reg_number: str
    protocol_date: str  # ДД.ММ.ГГГГ
    purchase_url: str
    first_found_date: str = ""  # ДД.ММ.ГГГГ
    total_count: int = 1
    last_check: str = ""  # ISO format

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SearchSession:
    """Сессия поиска для аналитики."""

    id: Optional[int] = None
    search_params_hash: str = ""
    okpd2_ids: str = ""
    fz_types: str = ""
    price_from: int = 0
    publish_date_from: str = ""
    total_found: int = 0
    relevant_count: int = 0
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# TENDER CACHE
# ============================================================================


class TenderCache:
    """
    Единый SQLite-кэш для TENDER-BOT.

    Потокобезопасный, с WAL-режимом, индексами и автоочисткой.
    """

    DEFAULT_TTL_DAYS = 90
    DEFAULT_MAX_ENTRIES = 50000
    SAVE_BATCH_SIZE = 50
    SAVE_DELAY_SECONDS = 5

    def __init__(
        self,
        db_path: Path,
        ttl_days: int = DEFAULT_TTL_DAYS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.ttl_days = ttl_days
        self.max_entries = max_entries

        self._lock = threading.RLock()
        self._pending_tenders: List[Tuple[str, str, str]] = []  # (reg, date, url)
        self._pending_save = False
        self._last_save_time = 0

        self._init_db()
        self._cleanup_old_entries()
    # ------------------------------------------------------------------------
    # DATABASE LAYER
    # ------------------------------------------------------------------------

    def _init_db(self):
        """Создаёт таблицы и индексы."""
        with self._connection() as conn:
            conn.executescript("""
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;
                PRAGMA temp_store = MEMORY;
                PRAGMA mmap_size = 268435456;

                CREATE TABLE IF NOT EXISTS purchase_states (
                    reg_number TEXT PRIMARY KEY,
                    last_update_date TEXT,
                    protocol_count INTEGER DEFAULT 0,
                    last_protocol_date TEXT,
                    status TEXT,
                    protocols_hash TEXT,
                    checked_at TEXT,
                    has_evaders INTEGER DEFAULT 0,
                    is_empty INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS tender_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reg_number TEXT NOT NULL,
                    protocol_date TEXT NOT NULL,
                    purchase_url TEXT,
                    first_found_date TEXT,
                    total_count INTEGER DEFAULT 1,
                    last_check TEXT,
                    UNIQUE(reg_number, protocol_date)
                );

                CREATE TABLE IF NOT EXISTS search_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_params_hash TEXT,
                    okpd2_ids TEXT,
                    fz_types TEXT,
                    price_from INTEGER,
                    publish_date_from TEXT,
                    total_found INTEGER,
                    relevant_count INTEGER,
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_ps_checked_at
                    ON purchase_states(checked_at);
                CREATE INDEX IF NOT EXISTS idx_ps_updated
                    ON purchase_states(updated_at);
                CREATE INDEX IF NOT EXISTS idx_tr_reg_number
                    ON tender_results(reg_number);
                CREATE INDEX IF NOT EXISTS idx_tr_last_check
                    ON tender_results(last_check);
                CREATE INDEX IF NOT EXISTS idx_tr_protocol_date
                    ON tender_results(protocol_date);
                CREATE INDEX IF NOT EXISTS idx_ss_created
                    ON search_sessions(created_at);
            """)

    @contextmanager
    def _connection(self):
        """Контекстный менеджер для соединения с БД."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    # ------------------------------------------------------------------------
    # PURCHASE STATES (из purchase_cache.py)
    # ------------------------------------------------------------------------

    def get_purchase_state(self, reg_number: str) -> Optional[PurchaseState]:
        """Получает состояние закупки."""
        with self._lock:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT * FROM purchase_states WHERE reg_number = ?", (reg_number,)
                ).fetchone()

                if row:
                    return PurchaseState(
                        reg_number=row["reg_number"],
                        last_update_date=row["last_update_date"] or "",
                        protocol_count=row["protocol_count"] or 0,
                        last_protocol_date=row["last_protocol_date"] or "",
                        status=row["status"] or "",
                        protocols_hash=row["protocols_hash"] or "",
                        checked_at=row["checked_at"] or "",
                        has_evaders=bool(row["has_evaders"]),
                        is_empty=bool(row["is_empty"]),
                    )
                return None

    def set_purchase_state(self, state: PurchaseState):
        """Сохраняет/обновляет состояние закупки."""
        now = datetime.now().isoformat()

        with self._lock:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO purchase_states (
                        reg_number, last_update_date, protocol_count,
                        last_protocol_date, status, protocols_hash,
                        checked_at, has_evaders, is_empty,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(reg_number) DO UPDATE SET
                        last_update_date = excluded.last_update_date,
                        protocol_count = excluded.protocol_count,
                        last_protocol_date = excluded.last_protocol_date,
                        status = excluded.status,
                        protocols_hash = excluded.protocols_hash,
                        checked_at = excluded.checked_at,
                        has_evaders = excluded.has_evaders,
                        is_empty = excluded.is_empty,
                        updated_at = excluded.updated_at
                """,
                    (
                        state.reg_number,
                        state.last_update_date,
                        state.protocol_count,
                        state.last_protocol_date,
                        state.status,
                        state.protocols_hash,
                        state.checked_at or now,
                        int(state.has_evaders),
                        int(state.is_empty),
                        now,
                        now,
                    ),
                )

            self._enforce_max_entries()
            logger.debug(f"💾 Состояние сохранено: {state.reg_number}")

    def needs_update(
        self,
        reg_number: str,
        current_update_date: str,
        current_protocol_count: int = 0,
        current_protocols_hash: Optional[str] = None,
    ) -> bool:
        """
        Проверяет, нужно ли обновлять закупку.

        Логика:
        1. Нет в кэше → обновлять
        2. Изменилась дата обновления → обновлять
        3. Изменилось кол-во протоколов → обновлять
        4. Изменился хеш протоколов → обновлять
        5. is_empty=True и ничего не изменилось → НЕ обновлять
        """
        cached = self.get_purchase_state(reg_number)

        if not cached:
            logger.info(f"  🆕 Новая закупка: {reg_number}")
            return True

        if cached.last_update_date != current_update_date:
            logger.info(
                f"  🔄 Изменилась дата: {cached.last_update_date} → {current_update_date}"
            )
            return True

        if cached.protocol_count != current_protocol_count:
            logger.info(
                f"  🔄 Протоколов: {cached.protocol_count} → {current_protocol_count}"
            )
            return True

        if current_protocols_hash and cached.protocols_hash != current_protocols_hash:
            logger.info(f"  🔄 Изменился хеш протоколов")
            return True

        if cached.is_empty and cached.protocol_count == current_protocol_count:
            logger.info(f"  ⏭ Пустая закупка, пропускаем: {reg_number}")
            return False

        logger.info(f"  ⏭ Без изменений: {reg_number}")
        return False

    def mark_empty(self, reg_number: str):
        """Помечает закупку как проверенную без результатов."""
        now = datetime.now().isoformat()
        with self._lock:
            with self._connection() as conn:
                conn.execute(
                    """
                    UPDATE purchase_states
                    SET is_empty = 1, checked_at = ?, updated_at = ?
                    WHERE reg_number = ?
                """,
                    (now, now, reg_number),
                )

    def mark_has_evaders(self, reg_number: str):
        """Помечает закупку как содержащую результаты."""
        now = datetime.now().isoformat()
        with self._lock:
            with self._connection() as conn:
                conn.execute(
                    """
                    UPDATE purchase_states
                    SET has_evaders = 1, checked_at = ?, updated_at = ?
                    WHERE reg_number = ?
                """,
                    (now, now, reg_number),
                )

    @staticmethod
    def get_protocols_hash(protocols: Optional[List[Dict]]) -> str:
        """Создаёт MD5-хеш списка протоколов."""
        if not protocols:
            return hashlib.md5(b"").hexdigest()[:16]

        try:
            names = [str(p.get("name", "")).strip() for p in protocols if p]
            names_str = "|".join(sorted(filter(None, names)))
            return hashlib.md5(names_str.encode("utf-8")).hexdigest()[:16]
        except Exception as e:
            logger.debug(f"⚠️ Ошибка хеша протоколов: {e}")
            return hashlib.md5(b"error").hexdigest()[:16]

    # ------------------------------------------------------------------------
    # TENDER RESULTS (из evaders_cache.py)
    # ------------------------------------------------------------------------

    def is_tender_processed(self, reg_number: str, protocol_date: str = None) -> bool:
        """Проверяет, обрабатывался ли тендер."""
        with self._lock:
            with self._connection() as conn:
                if protocol_date:
                    row = conn.execute(
                        """
                        SELECT 1 FROM tender_results
                        WHERE reg_number = ? AND protocol_date = ?
                    """,
                        (reg_number, protocol_date),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT 1 FROM tender_results WHERE reg_number = ?
                    """,
                        (reg_number,),
                    ).fetchone()
                return row is not None

    def add_tender(
        self,
        reg_number: str,
        protocol_date: str,
        purchase_url: str,
        batch: bool = False,
    ) -> bool:
        """
        Добавляет тендер в кэш.
        Возвращает True если новый, False если уже был.
        """
        now_iso = datetime.now().isoformat()

        with self._lock:
            # Проверяем существование
            with self._connection() as conn:
                existing = conn.execute(
                    """
                    SELECT total_count, protocols_found FROM tender_results
                    WHERE reg_number = ? AND protocol_date = ?
                """,
                    (reg_number, protocol_date),
                ).fetchone()

                if existing:
                    # Обновляем существующий
                    conn.execute(
                        """
                        UPDATE tender_results
                        SET total_count = total_count + 1,
                            last_check = ?
                        WHERE reg_number = ? AND protocol_date = ?
                    """,
                        (now_iso, reg_number, protocol_date),
                    )
                    logger.debug(f"⏭ Тендер уже в кэше: {reg_number}")
                    return False

                # Новый тендер
                conn.execute(
                    """
                    INSERT INTO tender_results (
                        reg_number, protocol_date, purchase_url,
                        first_found_date, total_count, last_check
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        reg_number,
                        protocol_date,
                        purchase_url,
                        protocol_date,
                        1,
                        now_iso,
                    ),
                )

            logger.info(f"🆕 Новый тендер в кэше: {reg_number}")
            return True

    def add_tender_batch(
        self,
        reg_number: str,
        protocol_date: str,
        purchase_url: str,
    ) -> bool:
        """
        Добавляет в буфер для batch-вставки.
        Возвращает True если новый (ещё не в буфере и не в БД).
        """
        with self._lock:
            # Проверяем в БД
            if self.is_tender_processed(reg_number, protocol_date):
                return False

            # Проверяем в буфере
            for r, d, _ in self._pending_tenders:
                if r == reg_number and d == protocol_date:
                    return False

            self._pending_tenders.append((reg_number, protocol_date, purchase_url))

            if len(self._pending_tenders) >= self.SAVE_BATCH_SIZE:
                self._flush_batch()

            return True

    def _flush_batch(self):
        """Сбрасывает буфер в БД."""
        if not self._pending_tenders:
            return

        now_iso = datetime.now().isoformat()
        with self._lock:
            with self._connection() as conn:
                for reg_number, protocol_date, purchase_url in self._pending_tenders:
                    try:
                        conn.execute(
                            """
                            INSERT INTO tender_results (
                                reg_number, protocol_date, purchase_url,
                                first_found_date, total_count, last_check
                            ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                            (
                                reg_number,
                                protocol_date,
                                purchase_url,
                                protocol_date,
                                1,
                                now_iso,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        pass  # Уже есть

            count = len(self._pending_tenders)
            self._pending_tenders.clear()
            logger.debug(f"💾 Batch сохранён: {count} тендеров")

    def get_tender(self, reg_number: str, protocol_date: str = None) -> Optional[Dict]:
        """Возвращает запись о тендере."""
        with self._lock:
            with self._connection() as conn:
                if protocol_date:
                    row = conn.execute(
                        """
                        SELECT * FROM tender_results
                        WHERE reg_number = ? AND protocol_date = ?
                    """,
                        (reg_number, protocol_date),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT * FROM tender_results WHERE reg_number = ?
                    """,
                        (reg_number,),
                    ).fetchone()

                if row:
                    return dict(row)
                return None

    def get_all_reg_numbers(self) -> Set[str]:
        """Все reg_number из кэша тендеров."""
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT reg_number FROM tender_results"
                ).fetchall()
                return {r[0] for r in rows}

    def get_recent_tenders(self, days: int = 7) -> List[Dict]:
        """Тендеры за последние N дней."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM tender_results WHERE last_check > ?
                    ORDER BY last_check DESC
                """,
                    (cutoff,),
                ).fetchall()
                return [dict(r) for r in rows]

    # ------------------------------------------------------------------------
    # SEARCH SESSIONS (аналитика)
    # ------------------------------------------------------------------------

    def save_search_session(self, session: SearchSession) -> int:
        """Сохраняет сессию поиска, возвращает ID."""
        now = datetime.now().isoformat()
        with self._lock:
            with self._connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO search_sessions (
                        search_params_hash, okpd2_ids, fz_types,
                        price_from, publish_date_from,
                        total_found, relevant_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        session.search_params_hash,
                        session.okpd2_ids,
                        session.fz_types,
                        session.price_from,
                        session.publish_date_from,
                        session.total_found,
                        session.relevant_count,
                        now,
                    ),
                )
                return cursor.lastrowid

    def get_search_stats(self, days: int = 30) -> List[Dict]:
        """Статистика поисков за период."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM search_sessions
                    WHERE created_at > ?
                    ORDER BY created_at DESC
                """,
                    (cutoff,),
                ).fetchall()
                return [dict(r) for r in rows]

    # ------------------------------------------------------------------------
    # CLEANUP & MAINTENANCE
    # ------------------------------------------------------------------------

    def _cleanup_old_entries(self):
        """Удаляет устаревшие записи по TTL."""
        if not self.ttl_days:
            return

        cutoff = (datetime.now() - timedelta(days=self.ttl_days)).isoformat()

        with self._lock:
            with self._connection() as conn:
                # Очищаем purchase_states
                result = conn.execute(
                    """
                    DELETE FROM purchase_states WHERE checked_at < ?
                """,
                    (cutoff,),
                )
                ps_deleted = result.rowcount

                # Очищаем tender_results
                result = conn.execute(
                    """
                    DELETE FROM tender_results WHERE last_check < ?
                """,
                    (cutoff,),
                )
                tr_deleted = result.rowcount

            total = ps_deleted + tr_deleted
            if total > 0:
                logger.info(
                    f"🧹 Очищено {total} записей (> {self.ttl_days} дней): "
                    f"purchase_states={ps_deleted}, tender_results={tr_deleted}"
                )

    def _enforce_max_entries(self):
        """Удаляет самые старые записи если превышен лимит."""
        with self._lock:
            with self._connection() as conn:
                # Проверяем purchase_states
                count = conn.execute("SELECT COUNT(*) FROM purchase_states").fetchone()[
                    0
                ]

                if count > self.max_entries:
                    to_remove = count - self.max_entries
                    conn.execute(
                        """
                        DELETE FROM purchase_states
                        WHERE reg_number IN (
                            SELECT reg_number FROM purchase_states
                            ORDER BY checked_at ASC
                            LIMIT ?
                        )
                    """,
                        (to_remove,),
                    )
                    logger.info(
                        f"🧹 Удалено {to_remove} purchase_states (лимит {self.max_entries})"
                    )

                # Проверяем tender_results
                count = conn.execute("SELECT COUNT(*) FROM tender_results").fetchone()[
                    0
                ]

                if count > self.max_entries:
                    to_remove = count - self.max_entries
                    conn.execute(
                        """
                        DELETE FROM tender_results
                        WHERE id IN (
                            SELECT id FROM tender_results
                            ORDER BY last_check ASC
                            LIMIT ?
                        )
                    """,
                        (to_remove,),
                    )
                    logger.info(
                        f"🧹 Удалено {to_remove} tender_results (лимит {self.max_entries})"
                    )

    def flush(self):
        """Принудительно сбрасывает буфер и сохраняет всё."""
        with self._lock:
            self._flush_batch()
        logger.debug("💾 Кэш принудительно сброшен")

    def clear_all(self):
        """Полная очистка кэша."""
        with self._lock:
            with self._connection() as conn:
                conn.execute("DELETE FROM purchase_states")
                conn.execute("DELETE FROM tender_results")
                conn.execute("DELETE FROM search_sessions")
                conn.execute("VACUUM")
            self._pending_tenders.clear()
        logger.warning("🗑️ Кэш полностью очищен")

    def remove_purchase(self, reg_number: str) -> bool:
        """Удаляет закупку из кэша."""
        with self._lock:
            with self._connection() as conn:
                result = conn.execute(
                    "DELETE FROM purchase_states WHERE reg_number = ?", (reg_number,)
                )
                return result.rowcount > 0

    # ------------------------------------------------------------------------
    # STATISTICS
    # ------------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Общая статистика кэша."""
        with self._lock:
            with self._connection() as conn:
                ps_total = conn.execute(
                    "SELECT COUNT(*) FROM purchase_states"
                ).fetchone()[0]
                ps_empty = conn.execute(
                    "SELECT COUNT(*) FROM purchase_states WHERE is_empty = 1"
                ).fetchone()[0]
                ps_evaders = conn.execute(
                    "SELECT COUNT(*) FROM purchase_states WHERE has_evaders = 1"
                ).fetchone()[0]

                tr_total = conn.execute(
                    "SELECT COUNT(*) FROM tender_results"
                ).fetchone()[0]

                ps_oldest = conn.execute(
                    "SELECT checked_at FROM purchase_states ORDER BY checked_at ASC LIMIT 1"
                ).fetchone()
                ps_newest = conn.execute(
                    "SELECT checked_at FROM purchase_states ORDER BY checked_at DESC LIMIT 1"
                ).fetchone()

                db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

                return {
                    "purchase_states": {
                        "total": ps_total,
                        "empty": ps_empty,
                        "with_evaders": ps_evaders,
                        "active": ps_total - ps_empty,
                    },
                    "tender_results": {
                        "total": tr_total,
                    },
                    "oldest_check": ps_oldest[0] if ps_oldest else None,
                    "newest_check": ps_newest[0] if ps_newest else None,
                    "db_size_mb": round(db_size / 1024 / 1024, 2),
                }

    def __len__(self) -> int:
        with self._lock:
            with self._connection() as conn:
                ps = conn.execute("SELECT COUNT(*) FROM purchase_states").fetchone()[0]
                tr = conn.execute("SELECT COUNT(*) FROM tender_results").fetchone()[0]
                return ps + tr

    def __contains__(self, reg_number: str) -> bool:
        with self._lock:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM purchase_states WHERE reg_number = ?", (reg_number,)
                ).fetchone()
                return row is not None
