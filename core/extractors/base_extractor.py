"""
core/extractors/base.py
Базовый класс для всех экстракторов документов.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
from loguru import logger


class BaseExtractor:
    """Базовый класс экстрактора текста из файла."""

    SUPPORTED_EXTENSIONS: list[str] = []

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def can_extract(self, file_path: Path, file_type: str = "") -> bool:
        """Проверяет, может ли экстрактор обработать этот файл."""
        ext = (file_type.lower() if file_type else file_path.suffix.lower()).lstrip(".")
        return ext in self.SUPPORTED_EXTENSIONS

    @abstractmethod
    def extract(self, file_path: Path, doc_name: str = "") -> str:
        """Извлекает текст из файла. Возвращает пустую строку при ошибке."""
        pass

    def _detect_by_magic(self, file_path: Path) -> Optional[str]:
        """Определяет тип файла по магическим байтам."""
        try:
            with open(file_path, "rb") as f:
                header = f.read(512)  # Увеличен буфер для длинных HTML тегов и BOM

            from core.config.document_config import PDF_MAGIC, ZIP_MAGIC, OLE2_MAGIC

            if header.startswith(PDF_MAGIC):
                return "pdf"
            elif header.startswith(ZIP_MAGIC):
                return "zip"
            elif header.startswith(OLE2_MAGIC):
                return "doc"

            # Проверка HTML с защитой от пробелов, BOM и регистра
            header_lower = header.lower()
            if (
                b"<html" in header_lower
                or b"<!doctype" in header_lower
                or b"<table" in header_lower
            ):
                return "html"

        except Exception as e:
            logger.debug(f"Ошибка определения типа файла {file_path.name}: {e}")

        return None
