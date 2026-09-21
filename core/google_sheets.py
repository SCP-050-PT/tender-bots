"""
core/google_sheets.py
Работа с Google Sheets. Чтение, запись, проверка дубликатов, форматирование и сборка строк.
Версия: v7.7.0 — добавлены функции build_sheets_row и get_guarantee_info для полного выноса Sheets-логики из main.py.
"""

import json
import traceback
from pathlib import Path
from typing import Optional, List, Dict, Tuple
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
from utils.price_parser import (
    format_for_sheets as _format_nmck,
    format_for_sheets as _format_price,
)
from utils.formatters import (
    sanitize_for_sheets,
    get_quantity,
    build_calculation_breakdown,
    build_short_recommendation,
)

# === СТРУКТУРА ЛИСТА "Тендера 2026 ИИ-бот" ===
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
    "Расчёты",  # Q
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


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ФОРМИРОВАНИЯ СТРОК (ВЫНЕСЕНО ИЗ MAIN.PY) ===


def get_guarantee_info(detail, analysis) -> Tuple[str, str, str]:
    """Извлекает информацию об обеспечении заявки и контракта."""
    app_guarantee = contract_guarantee = guarantee_method = ""

    if detail:
        app_guarantee = (detail.application_guarantee or "").strip()
        contract_guarantee = (detail.contract_guarantee or "").strip()
        guarantee_method = (detail.guarantee_method or "").strip()

    if analysis and hasattr(analysis, "details") and analysis.details:
        d = analysis.details
        if isinstance(d, dict):
            app_guarantee = app_guarantee or d.get("application_guarantee", "")
            contract_guarantee = contract_guarantee or d.get("contract_guarantee", "")
            guarantee_method = guarantee_method or d.get("guarantee_method", "")
        else:
            app_guarantee = app_guarantee or getattr(d, "application_guarantee", "")
            contract_guarantee = contract_guarantee or getattr(
                d, "contract_guarantee", ""
            )
            guarantee_method = guarantee_method or getattr(d, "guarantee_method", "")

    if not app_guarantee:
        app_guarantee = "Не требуется"
    if not contract_guarantee:
        contract_guarantee = "Не требуется"
    if not guarantee_method:
        guarantee_method = "Не требуется"

    return app_guarantee, contract_guarantee, guarantee_method


def build_sheets_row(analysis, detail, tender) -> Dict[str, str]:
    """Формирует строковый словарь для Google Sheets с приоритетом данных парсера."""
    quantity = None
    t_type = getattr(analysis, "tender_type", "")

    if detail:
        if t_type == "sout":
            quantity = getattr(detail, "rm_total", None)
        elif t_type == "plk":
            quantity = getattr(detail, "points_count", None)
        elif t_type == "education":
            quantity = getattr(detail, "students_count", None)
        elif t_type == "opr":
            quantity = getattr(detail, "opr_positions", None) or getattr(
                detail, "rm_total", None
            )

    if not quantity:
        quantity = get_quantity(analysis)

    if not quantity:
        quantity = "?"

    app_guarantee, contract_guarantee, guarantee_method = get_guarantee_info(
        detail, analysis
    )

    law = tender.law.replace("-FZ", "-ФЗ") if getattr(tender, "law", None) else ""
    procurement = (
        f"{detail.purchase_method}, {law}"
        if detail and getattr(detail, "purchase_method", None)
        else law
    )

    ai_comment = sanitize_for_sheets(getattr(analysis, "comment", "") or "")
    purchase_name = getattr(tender, "title", "") or ""
    if (
        detail
        and getattr(detail, "purchase_name", None)
        and len(detail.purchase_name) > 10
    ):
        purchase_name = detail.purchase_name

    etp_name = ""
    if detail:
        etp_name = (
            getattr(detail, "platform_name", None) or getattr(detail, "etp", None) or ""
        )
    if not etp_name and hasattr(tender, "etp") and tender.etp:
        etp_name = tender.etp

    deadline = (
        detail.deadline_date
        if detail and getattr(detail, "deadline_date", None)
        else getattr(tender, "deadline_date", "")
    )

    decision_override = getattr(analysis, "decision", "")
    if hasattr(analysis, "details") and analysis.details:
        d = analysis.details
        is_forbidden = (
            isinstance(d, dict) and d.get("_forbidden_direction")
        ) or getattr(d, "_forbidden_direction", False)
        if is_forbidden:
            decision_override = "не рекомендуется"
            ai_comment = "[FORBIDDEN] ЗАПРЕЩЁННОЕ НАПРАВЛЕНИЕ: " + ai_comment

    # Безопасное извлечение комиссии ЭТП
    etp_commission = 0
    if hasattr(analysis, "details") and analysis.details:
        if isinstance(analysis.details, dict):
            etp_commission = analysis.details.get("etp_commission", 0)
        else:
            etp_commission = getattr(analysis.details, "etp_commission", 0)

    nmck_val = (
        detail.nmck if detail and getattr(detail, "nmck", None) else 0
    ) or getattr(analysis, "nmck", 0)

    return {
        "ID тендера": tender.tender_id,
        "Ссылка на тендер": getattr(tender, "url", ""),
        "Наименование услуг": purchase_name,
        "Способ проведения закупки": procurement,
        "ЭТП": etp_name,
        "Комиссия ЭТП": f"{etp_commission:,.0f} ₽" if etp_commission else "0 ₽",
        "Регион": (
            detail.customer_region
            if detail and getattr(detail, "customer_region", None)
            else getattr(tender, "region", "") or ""
        ),
        "Обеспечение заявки": app_guarantee,
        "Обеспечение контракта": contract_guarantee,
        "Способ обеспечения исполнения": guarantee_method,
        "Срок подачи заявки до": deadline,
        "НМЦК": _format_nmck(nmck_val),
        "Количество": quantity,
        "Цена предложения": _format_price(getattr(analysis, "recommended_price", 0)),
        "Возможности экономии": "",
        "Решение по участию": decision_override,
        "Расчёты": sanitize_for_sheets(build_calculation_breakdown(analysis)),
        "Комментарий от ИИ-агента": ai_comment,
        "Рекомендации": sanitize_for_sheets(build_short_recommendation(analysis)),
        "Комментарии руководителя отдела по участию": "",
        "Дата заключения контракта": "",
        "Дата выполнения работ": "",
        "Результат": "",
    }


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

    def _validate_credentials(self) -> Tuple[bool, str]:
        """Проверяет файл credentials перед подключением."""
        creds_path = Path(self.credentials_path)

        if not creds_path.exists():
            return False, f"Файл credentials НЕ НАЙДЕН: {creds_path.absolute()}"

        try:
            with open(creds_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return False, f"Файл credentials не является валидным JSON: {e}"
        except Exception as e:
            return False, f"Ошибка чтения credentials: {e}"

        required_fields = ["private_key", "client_email", "token_uri"]
        missing = [f for f in required_fields if f not in data or not data[f]]
        if missing:
            return False, (
                f"В credentials.json ОТСУТСТВУЮТ обязательные поля: {missing}. "
                f"Убедитесь, что вы скачали ПОЛНЫЙ JSON-ключ из Google Cloud Console."
            )

        if len(data["private_key"]) < 100:
            return False, "private_key слишком короткий — возможно, файл повреждён"

        if "@" not in data.get("client_email", ""):
            return False, "client_email невалиден"

        return True, "OK"

    def ensure_headers(self) -> bool:
        """Проверяет и обновляет заголовки первой строки."""
        try:
            current_headers = self.worksheet.row_values(1)

            if current_headers == SHEET_COLUMNS:
                logger.info("✅ Заголовки актуальны")
                return True

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
            logger.info(f"🔐 Проверка credentials: {self.credentials_path}")
            ok, msg = self._validate_credentials()
            if not ok:
                logger.error(f"❌ {msg}")
                raise ValueError(msg)
            logger.info("✅ Credentials валидны")

            creds_path = Path(self.credentials_path)
            credentials = Credentials.from_service_account_file(
                str(creds_path), scopes=self.SCOPES
            )
            logger.info(
                f"✅ Credentials загружены: {credentials.service_account_email}"
            )

            self.client = gspread.authorize(credentials)
            logger.info("✅ Авторизация gspread успешна")

            logger.info(f"🔓 Открытие таблицы: {self.spreadsheet_id}")
            self.sheet = self.client.open_by_key(self.spreadsheet_id)
            logger.info(f"✅ Таблица открыта: {self.sheet.title}")

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
            decision = (data.get("Решение по участию") or "").lower().strip()

            # === Фильтр: записываем только "рекомендуется" ===
            if decision != "рекомендуется":
                logger.info(
                    f"Пропуск записи в Sheets: {tender_id} "
                    f"(решение = '{data.get('Решение по участию')}')"
                )
                return False

            if check_duplicate and tender_id:
                existing_row = self.find_duplicate(tender_id)
                if existing_row:
                    logger.info(f"Тендер {tender_id} уже есть (строка {existing_row})")
                    return False

            row = [data.get(col, "") for col in SHEET_COLUMNS]
            self.worksheet.insert_row(row, index=2, value_input_option="USER_ENTERED")

            # Зелёное форматирование для рекомендованных
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
            end_col = self._col_index_to_letter(len(SHEET_COLUMNS))
            self.worksheet.update(f"A{row_number}:{end_col}{row_number}", [row])
            logger.info(f"Строка {row_number} обновлена")
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления: {e}")
            return False

    def _col_index_to_letter(self, index: int) -> str:
        """Конвертирует индекс колонки (1-based) в буквенное обозначение."""
        result = ""
        while index > 0:
            index, remainder = divmod(index - 1, 26)
            result = chr(65 + remainder) + result
        return result

    def _format_row_red(self, row_number: int):
        try:
            self.worksheet.format(
                f"A{row_number}:W{row_number}",
                {"backgroundColor": {"red": 0.95, "green": 0.8, "blue": 0.8}},
            )
        except Exception as e:
            logger.warning(f"Не удалось применить красное форматирование: {e}")

    def _format_row_green(self, row_number: int):
        try:
            self.worksheet.format(
                f"A{row_number}:W{row_number}",
                {"backgroundColor": {"red": 0.8, "green": 0.95, "blue": 0.8}},
            )
        except Exception as e:
            logger.warning(f"Не удалось применить зелёное форматирование: {e}")

    def _format_row_yellow(self, row_number: int):
        try:
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

    def check_exists(self, tender_id: str) -> bool:
        """Быстрая проверка наличия тендера в таблице."""
        try:
            cell = self.worksheet.find(str(tender_id), in_column=1)
            return cell is not None
        except Exception:
            return False


_sheets_manager: Optional[GoogleSheetsManager] = None


def get_sheets_manager() -> GoogleSheetsManager:
    global _sheets_manager
    if _sheets_manager is None:
        _sheets_manager = GoogleSheetsManager()
    return _sheets_manager


def reset_sheets_manager():
    global _sheets_manager
    _sheets_manager = None
