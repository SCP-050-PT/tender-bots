"""
core/extractors/pdf_extractor.py
Экстрактор текста из PDF файлов.

v6.7-r1:
  - Исправлена логика _is_valid_pdf (мелкие PDF не бракуются, если это не HTML)
  - Поддержка смещенной сигнатуры %PDF- (поиск в первых 1024 байтах)
  - Использован корректный импорт PDF_MAGIC
  - Улучшено извлечение табличного текста через считывание блоков (blocks)
  - Добавлен контекстный менеджер (with fitz.open) для безопасной работы с ресурсами
"""

from pathlib import Path
from typing import List
from loguru import logger

from core.config.document_config import PDF_MAGIC
from core.extractors.base_extractor import BaseExtractor

try:
    import fitz  # PyMuPDF

    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False
    logger.warning("PyMuPDF не установлен, PDF файлы будут пропущены")


class PdfExtractor(BaseExtractor):
    """Извлекает текст из PDF файлов."""

    SUPPORTED_EXTENSIONS = ["pdf"]

    def extract(self, file_path: Path, doc_name: str = "") -> str:
        if not HAS_PYMUPDF:
            logger.warning("[PdfExtractor] PyMuPDF не доступен")
            return ""

        # Проверяем валидность файла
        if not self._is_valid_pdf(file_path):
            return ""

        try:
            texts: List[str] = []
            with fitz.open(file_path) as doc:
                for page_num, page in enumerate(doc, start=1):
                    # Извлекаем текст по блокам для сохранения структуры/таблиц
                    blocks = page.get_text("blocks")
                    page_texts = []

                    for block in blocks:
                        # block format: (x0, y0, x1, y1, "text", block_no, block_type)
                        if len(block) >= 5 and block[4].strip():
                            page_texts.append(block[4].strip())

                    if page_texts:
                        texts.append(
                            f"--- Страница {page_num} ---\n" + "\n".join(page_texts)
                        )

            result = "\n\n".join(texts)
            logger.info(
                f"[PdfExtractor] Успешно извлечен текст из {file_path.name} ({len(texts)} страниц)"
            )
            return result

        except Exception as e:
            logger.error(f"[PdfExtractor] Ошибка при чтении PDF {file_path.name}: {e}")
            return ""

    def _is_valid_pdf(self, file_path: Path) -> bool:
        """Проверяет, что файл действительно является PDF (не HTML-страница ошибки и не битый файл)."""
        try:
            file_size = file_path.stat().st_size
            if file_size == 0:
                logger.warning(f"[PdfExtractor] Файл пуст: {file_path.name}")
                return False

            # Читаем первые 1024 байт для проверки сигнатуры и HTML
            with open(file_path, "rb") as f:
                header_bytes = f.read(1024)

            # Проверяем на HTML-страницы ошибок WAF / ЕИС
            header_lower = header_bytes.lower()
            if b"<html" in header_lower or b"<!doctype html" in header_lower:
                logger.warning(
                    f"[PdfExtractor] Файл {file_path.name} — это HTML-страница ошибки, не PDF"
                )
                return False

            # Сигнатурный поиск %PDF- (используем конфиг или фоллбэк)
            magic_target = PDF_MAGIC if isinstance(PDF_MAGIC, bytes) else b"%PDF-"
            if magic_target not in header_bytes:
                logger.warning(
                    f"[PdfExtractor] Не найдена сигнатура PDF в первых 1024 байтах: {file_path.name}"
                )
                return False

            return True

        except Exception as e:
            logger.error(
                f"[PdfExtractor] Ошибка валидации PDF файла {file_path.name}: {e}"
            )
            return False
