"""
core/google_sheets.py
Работа с Google Sheets. Чтение, запись, проверка дубликатов, форматирование.
Версия: v6.0 (26.07.2026) — исправлена опечатка "Способо", расширен диапазон A:T,
добавлены колонки needs_manual_review, llm_confidence.
"""

import json
import traceback
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass
from loguru import logger

try:
    import gspread
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False
    logger.warning("gspread не установлен. Google Sheets недоступен.")

from config.settings import settings

# === СТРУКТУРА ЛИСТА "Тендера 2026 ИИ-бот" ===
# ← v6.0: Исправлена опечатка "Способо" → "Способ", добавлены колонки S, T
SHEET_COLUMNS = [
    "ID тендера",  # A
    "Ссылка на тендер",  # B
    "Наименование услуг",  # C
    "Способ проведения закупки",  # D
    "ЭТП",  # E
    "Комиссия ЭТП",  # F
    "Регион",  # G
    "Обеспечение заявки",  # H
    "Обеспечение контракта",  # I
    "Способ обеспечения исполнения",  # J
    "Срок подачи заявки до",  # K
    "НМЦК",  # L
    "Количество",  # M
    "Цена предложения",  # N
    "Возможности экономии",  # O
    "Решение по участию",  # P
    "Расчёты",  # Q ← НОВАЯ
    "Комментарий от ИИ-агента",  # R
    "Рекомендации",  # S
    "Комментарии руководителя отдела по участию",  # T
    "Дата заключения контракта",  # U
    "Дата выполнения работ",  # V
    "Результат",  # W
]

BOT_COLUMNS_RANGE = "A:W"


@dataclass
class TenderRecord:
    row_number: int
    tender_id: Optional[str]
    service_name: str
    nmck: float
    decision: str
    price: float
    comment: str

    def is_duplicate_of(self, tender_id: str) -> bool:
        return self.tender_id == tender_id


class GoogleSheetsManager:
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(
        self,
        spreadsheet_id: Optional[str] = None,
        credentials_path: Optional[str] = None,
        worksheet_name: Optional[str] = None,
    ):
        self.spreadsheet_id = spreadsheet_id or getattr(
            settings, "GOOGLE_SHEETS_ID", None
        )
        self.credentials_path = credentials_path or getattr(
            settings, "GOOGLE_SHEETS_CREDENTIALS_PATH", "./config/credentials.json"
        )
        self.worksheet_name = worksheet_name or getattr(
            settings, "GOOGLE_SHEETS_WORKSHEET", "Тендера 2026 ИИ-бот"
        )
        self.client = None
        self.sheet = None
        self.worksheet = None

        if not GOOGLE_AVAILABLE:
            raise ImportError(
                "gspread не установлен. Установите: pip install gspread google-auth"
            )

        if not self.spreadsheet_id:
            raise ValueError("GOOGLE_SHEETS_ID не задан в .env или settings")

        self._connect()

    def _validate_credentials(self) -> tuple[bool, str]:
        """
        Проверяет файл credentials перед подключением.
        Возвращает (ok, message).
        """
        creds_path = Path(self.credentials_path)

        # 1. Проверка существования файла
        if not creds_path.exists():
            return False, f"Файл credentials НЕ НАЙДЕН: {creds_path.absolute()}"

        # 2. Проверка что это JSON
        try:
            with open(creds_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return False, f"Файл credentials не является валидным JSON: {e}"
        except Exception as e:
            return False, f"Ошибка чтения credentials: {e}"

        # 3. Проверка обязательных полей
        required_fields = ["private_key", "client_email", "token_uri"]
        missing = [f for f in required_fields if f not in data or not data[f]]
        if missing:
            return False, (
                f"В credentials.json ОТСУТСТВУЮТ обязательные поля: {missing}. "
                f"Убедитесь, что вы скачали ПОЛНЫЙ JSON-ключ из Google Cloud Console."
            )

        # 4. Проверка что private_key не пустой
        if len(data["private_key"]) < 100:
            return False, "private_key слишком короткий — возможно, файл повреждён"

        # 5. Проверка client_email
        if "@" not in data.get("client_email", ""):
            return False, "client_email невалиден"

        return True, "OK"
    
    def ensure_headers(self) -> bool:
        """Проверяет и обновляет заголовки первой строки."""
        try:
            current_headers = self.worksheet.row_values(1)

            # Если заголовки уже правильные — ничего не делаем
            if current_headers == SHEET_COLUMNS:
                logger.info("✅ Заголовки актуальны")
                return True

            # Иначе — перезаписываем
            end_col = self._col_index_to_letter(len(SHEET_COLUMNS))
            self.worksheet.update(f"A1:{end_col}1", [SHEET_COLUMNS])
            logger.info(f"✅ Заголовки обновлены: {len(SHEET_COLUMNS)} колонок")
            return True

        except Exception as e:
            logger.error(f"Ошибка обновления заголовков: {e}")
            return False

    def _connect(self):
        """Устанавливает соединение с Google Sheets с детальным логированием."""
        try:
            # Шаг 1: Валидация credentials
            logger.info(f"🔐 Проверка credentials: {self.credentials_path}")
            ok, msg = self._validate_credentials()
            if not ok:
                logger.error(f"❌ {msg}")
                raise ValueError(msg)
            logger.info("✅ Credentials валидны")

            # Шаг 2: Загрузка credentials
            creds_path = Path(self.credentials_path)
            credentials = Credentials.from_service_account_file(
                str(creds_path), scopes=self.SCOPES
            )
            logger.info(
                f"✅ Credentials загружены: {credentials.service_account_email}"
            )

            # Шаг 3: Авторизация
            self.client = gspread.authorize(credentials)
            logger.info("✅ Авторизация gspread успешна")

            # Шаг 4: Открытие таблицы
            logger.info(f"🔓 Открытие таблицы: {self.spreadsheet_id}")
            self.sheet = self.client.open_by_key(self.spreadsheet_id)
            logger.info(f"✅ Таблица открыта: {self.sheet.title}")

            # Шаг 5: Подключение к листу
            logger.info(f'📄 Поиск листа: "{self.worksheet_name}"')
            available_sheets = [w.title for w in self.sheet.worksheets()]
            logger.info(f"   Доступные листы: {available_sheets}")

            try:
                self.worksheet = self.sheet.worksheet(self.worksheet_name)
                self.ensure_headers()
            except gspread.WorksheetNotFound:
                error_msg = (
                    f'Лист "{self.worksheet_name}" НЕ НАЙДЕН. '
                    f"Доступные: {available_sheets}"
                )
                logger.error(error_msg)
                raise ValueError(error_msg)

            logger.info(
                f'📊 Подключено к листу "{self.worksheet_name}" '
                f"(строк: {self.worksheet.row_count}, колонок: {self.worksheet.col_count})"
            )

        except Exception as e:
            # Детальный вывод ошибки с traceback
            error_msg = (
                f"Ошибка подключения к Google Sheets: {type(e).__name__}: {str(e)}"
            )
            logger.error(error_msg)
            logger.error(f"Traceback:\n{traceback.format_exc()}")
            raise

    def find_duplicate(self, tender_id: str) -> Optional[int]:
        if not tender_id:
            return None
        try:
            col_a = self.worksheet.col_values(1)
            for i, val in enumerate(col_a[1:], start=2):
                if str(tender_id) in str(val):
                    logger.info(f"Найден дубликат {tender_id} в строке {i}")
                    return i
            return None
        except Exception as e:
            logger.error(f"Ошибка поиска дубликата: {e}")
            return None

    def add_tender_to_top(self, data: Dict, check_duplicate: bool = True) -> bool:
        try:
            tender_id = data.get("ID тендера", "")

            if check_duplicate and tender_id:
                existing_row = self.find_duplicate(tender_id)
                if existing_row:
                    logger.info(f"Тендер {tender_id} уже есть (строка {existing_row})")
                    return False

            row = [data.get(col, "") for col in SHEET_COLUMNS]
            self.worksheet.insert_row(row, index=2, value_input_option="USER_ENTERED")

            decision = data.get("Решение по участию", "")
            if decision == "не участвуем":
                self._format_row_red(2)
            elif decision == "рекомендуется":
                self._format_row_green(2)

            logger.info(f'✅ Тендер {tender_id} добавлен в "{self.worksheet_name}"')
            return True

        except Exception as e:
            logger.error(f"Ошибка добавления тендера: {e}")
            logger.error(f"Traceback:\n{traceback.format_exc()}")
            return False

    def update_tender(self, row_number: int, data: Dict) -> bool:
        try:
            row = [data.get(col, "") for col in SHEET_COLUMNS]
            # ← v6.0: Динамический расчёт end_col для 20 колонок
            end_col = self._col_index_to_letter(len(SHEET_COLUMNS))
            self.worksheet.update(f"A{row_number}:{end_col}{row_number}", [row])
            logger.info(f"Строка {row_number} обновлена")
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления: {e}")
            return False

    # ← v6.0: Вспомогательный метод для конвертации индекса колонки в букву
    def _col_index_to_letter(self, index: int) -> str:
        """Конвертирует индекс колонки (1-based) в буквенное обозначение."""
        result = ""
        while index > 0:
            index, remainder = divmod(index - 1, 26)
            result = chr(65 + remainder) + result
        return result

    def _format_row_red(self, row_number: int):
        try:
            # ← v6.0: Расширен диапазон до T
            self.worksheet.format(
                f"A{row_number}:W{row_number}", 
                {"backgroundColor": {"red": 0.95, "green": 0.8, "blue": 0.8}},
            )
        except Exception as e:
            logger.warning(f"Не удалось применить красное форматирование: {e}")

    def _format_row_green(self, row_number: int):
        try:
            # ← v6.0: Расширен диапазон до T
            self.worksheet.format(
                f"A{row_number}:W{row_number}",
                {"backgroundColor": {"red": 0.8, "green": 0.95, "blue": 0.8}},
            )
        except Exception as e:
            logger.warning(f"Не удалось применить зелёное форматирование: {e}")

    def _format_row_yellow(self, row_number: int):
        try:
            # ← v6.0: Расширен диапазон до T
            self.worksheet.format(
                f"A{row_number}:W{row_number}",
                {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.8}},
            )
        except Exception as e:
            logger.warning(f"Не удалось применить жёлтое форматирование: {e}")

    def get_all_records(self) -> List[Dict]:
        try:
            records = self.worksheet.get_all_records()
            logger.info(f"Получено {len(records)} записей")
            return records
        except Exception as e:
            logger.error(f"Ошибка чтения таблицы: {e}")
            return []

    def get_last_row_number(self) -> int:
        try:
            return len(self.worksheet.get_all_values())
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            return 1

_sheets_manager: Optional[GoogleSheetsManager] = None


def get_sheets_manager() -> GoogleSheetsManager:
    global _sheets_manager
    if _sheets_manager is None:
        _sheets_manager = GoogleSheetsManager()
    return _sheets_manager


def reset_sheets_manager():
    global _sheets_manager
    _sheets_manager = None
