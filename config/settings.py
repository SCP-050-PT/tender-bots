"""
config/settings.py
Главный конфигурационный файл. Читает .env, предоставляет доступ ко всем настройкам.
ИСПРАВЛЕНО (21.07.2026):
  - Убраны дублирующие себестоимостные константы СОУТ
  - Цены теперь только в costs_db.json (единый источник правды)
  - Оставлены только настройки API, поиска, логирования
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Загружаем .env из корня проекта
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    """Единая точка доступа ко всем настройкам бота."""

    # === YANDEX GPT ===
    YANDEX_FOLDER_ID: str = os.getenv("YANDEX_FOLDER_ID", "")
    YANDEX_API_KEY: str = os.getenv("YANDEX_API_KEY", "")
    YANDEX_GPT_MODEL: str = os.getenv("YANDEX_GPT_MODEL", "yandexgpt-lite")

    # === YANDEX AGENT ===
    YANDEX_AGENT_API_KEY: str = os.getenv("YANDEX_AGENT_API_KEY", "")
    YANDEX_AGENT_ID: str = os.getenv("YANDEX_AGENT_ID", "")
    USE_AI_AGENT: bool = os.getenv("USE_AI_AGENT", "False").lower() == "true"

    # === GOOGLE SHEETS ===
    GOOGLE_SHEETS_ID: str = os.getenv(
        "GOOGLE_SHEETS_ID", "1taImEQire-tOjGT85xKglsTQ4PH9cvzryqaxARPKAk8"
    )
    GOOGLE_SHEETS_CREDENTIALS_PATH: str = os.getenv(
        "GOOGLE_SHEETS_CREDENTIALS_PATH", "./config/credentials.json"
    )

    # === TENDER SEARCH ===
    SEARCH_INTERVAL_HOURS: int = int(os.getenv("SEARCH_INTERVAL_HOURS", "4"))

    # === APP ===
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"

    # === BUSINESS CONSTANTS (глобальные пороги, не цены) ===
    MIN_CONTRACT_SUM: int = 10_000  # Минимальная сумма договора
    MIN_NMCK: int = 100_000  # Минимальная НМЦК для поиска
    MIN_MARGIN_PERCENT: float = 10.0  # Минимальная маржа (%)
    MIN_MARGIN_SIZ: float = 5.0  # Минимальная маржа для СИЗ (%)

    # === ТРАНСПОРТНЫЕ НОРМЫ (общие для всех типов) ===
    FUEL_CONSUMPTION_L_PER_100KM: float = 11.0
    FUEL_PRICE_PER_LITER: float = 55.0  # Актуализировать при необходимости

    # === VALIDATION ===
    @classmethod
    def validate(cls) -> list[str]:
        """Проверяет, что все критичные настройки заполнены."""
        errors = []
        if not cls.YANDEX_FOLDER_ID:
            errors.append("YANDEX_FOLDER_ID не задан")
        if not cls.YANDEX_API_KEY:
            errors.append("YANDEX_API_KEY не задан")
        if not cls.GOOGLE_SHEETS_ID:
            errors.append("GOOGLE_SHEETS_ID не задан")
        return errors


# Глобальный инстанс для импорта
settings = Settings()
