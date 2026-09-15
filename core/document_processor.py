"""
core/document_processor.py
Фасад для обработки документов тендера.

v7.8.1:
  - Добавлена поддержка tender_id для корректной работы MCP-сервера.
  - Файлы сохраняются с префиксом {tender_id}_, чтобы MCP мог их находить.
  - Улучшен вывод данных из Excel-only файлов (добавлены заголовки и контекст).
  - Исправлена логика определения расширений и валидации контента.
"""

import os
import re
import time
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass
from loguru import logger

from core.config.document_config import (
    CONTRACT_PATTERNS,
    SKIP_FILE_PATTERNS,
    EXCEL_ONLY_PATTERNS,
    FILE_PRIORITY,
    MAX_TEXT_LENGTH,
    MAX_CONTRACT_FILE_SIZE,
    PDF_MAGIC,
    ZIP_MAGIC,
    OLE2_MAGIC,
)
from core.extractors import (
    DocxExtractor,
    PdfExtractor,
    ExcelExtractor,
    ZipExtractor,
    TextExtractor,
)


@dataclass
class DocumentInfo:
    name: str
    url: str
    file_type: str = ""
    size: str = ""
    date: str = ""
    is_active: bool = True
    file_url: str = ""
    file_size_bytes: int = 0
    is_contract: bool = False
    priority: int = 0

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "url": self.url,
            "file_type": self.file_type,
            "size": self.size,
            "date": self.date,
            "is_active": self.is_active,
            "file_url": self.file_url,
            "is_contract": self.is_contract,
            "priority": self.priority,
        }


class DocumentProcessor:
    """Фасад для обработки документов тендера."""

    def __init__(self, download_dir: Optional[Path] = None, session=None):
        # v7.8.1: Используем абсолютный путь к папке downloads внутри core
        self.download_dir = (
            download_dir or Path(__file__).resolve().parent / "downloads"
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.session = session

        # v6.8.6: Инициализация экстракторов
        self._extractors = {
            "docx": DocxExtractor(),
            "doc": DocxExtractor(),
            "pdf": PdfExtractor(),
            "xlsx": ExcelExtractor(),
            "xls": ExcelExtractor(),
            "7z": ZipExtractor(),
            "zip": ZipExtractor(),
            "txt": TextExtractor(),
            "rtf": TextExtractor(),
        }

    def process_documents(
        self,
        documents: List[DocumentInfo],
        max_docs: int = 5,
        tender_id: str = "",  # v7.8.1: Добавлен параметр tender_id
    ) -> str:
        """
        Обрабатывает список документов тендера.
        v7.8.1: Принимает tender_id для корректного именования файлов при скачивании.
        v7.2.2: Фильтрация по чёрному списку + Excel-only файлы.
        Возвращает объединённый текст для анализа.
        """
        if not documents:
            logger.warning("[DocumentProcessor] Нет документов для обработки")
            return ""

        active_docs = [d for d in documents if d.is_active]
        logger.info(f"[DocumentProcessor] Активных документов: {len(active_docs)}")

        # Классифицируем документы
        skipped_docs = []
        excel_only_docs = []
        for doc in active_docs:
            doc.is_contract = self._is_contract_file(doc.name)
            doc.priority = self._get_file_priority(doc.name)

            # v7.2.2: Чёрный список
            if self._should_skip_file(doc.name):
                skipped_docs.append(doc)
                logger.info(f"[DocumentProcessor] Пропуск (чёрный список): {doc.name}")
                continue

            # v7.2.2: Excel-only (извлекаем qty/price, но не в LLM)
            if self._is_excel_only(doc.name):
                excel_only_docs.append(doc)
                logger.info(f"[DocumentProcessor] Excel-only: {doc.name}")
                continue

            if doc.is_contract:
                logger.info(f"[DocumentProcessor] Контракт/договор: {doc.name}")

        # Убираем пропущенные и excel-only из основного списка
        active_docs = [
            d for d in active_docs if d not in skipped_docs and d not in excel_only_docs
        ]

        # Сортируем: приоритетные первые, контракты в конце
        active_docs.sort(key=lambda d: (-d.priority, d.is_contract))
        docs_to_process = [d for d in active_docs if not d.is_contract][:max_docs]
        contract_docs = [d for d in active_docs if d.is_contract]

        logger.info(
            f"[DocumentProcessor] Обработать: {len(docs_to_process)} "
            f"(контрактов пропущено: {len(contract_docs)}, "
            f"чёрный список: {len(skipped_docs)}, "
            f"excel-only: {len(excel_only_docs)})"
        )

        # v7.8.1: Обрабатываем Excel-only файлы с передачей tender_id
        structured_parts = []
        for doc in excel_only_docs:
            try:
                text = self._process_single_document(doc, tender_id=tender_id)
                if text:
                    # Только первые 2000 символов (qty + price), не полный текст
                    structured_parts.append(text[:2000])
                    logger.info(
                        f"[DocumentProcessor] Excel-only извлечено: {doc.name} "
                        f"({min(len(text), 2000)} симв.)"
                    )
            except Exception as e:
                logger.error(f"[DocumentProcessor] Ошибка Excel-only {doc.name}: {e}")
                continue

        # Обрабатываем основные документы
        texts = []
        for doc in docs_to_process:
            try:
                text = self._process_single_document(doc, tender_id=tender_id)
                if text:
                    texts.append(f"=== ФАЙЛ: {doc.name} ===\n{text}")
            except Exception as e:
                logger.error(f"[DocumentProcessor] Ошибка {doc.name}: {e}")
                continue

        # Добавляем структурированные данные из Excel-only
        if structured_parts:
            texts.append(
                "=== ДАННЫЕ ИЗ ОБОСНОВАНИЯ НМЦК ===\n" + "\n".join(structured_parts)
            )

        # Добавляем предупреждение о контрактах
        if contract_docs:
            contract_names = [d.name for d in contract_docs[:3]]
            texts.append(
                "\n=== ВНИМАНИЕ: СРЕДИ ДОКУМЕНТОВ ЕСТЬ КОНТРАКТЫ/ДОГОВОРЫ ===\n"
            )
            texts.append(f"Пропущены (не анализируются): {', '.join(contract_names)}")
            if len(contract_docs) > 3:
                texts.append(f"... и ещё {len(contract_docs) - 3} файлов")

        result = "\n\n".join(texts)
        logger.info(f"[DocumentProcessor] Итоговый текст: {len(result)} символов")
        return result

    def _is_contract_file(self, filename: str) -> bool:
        """Проверяет, является ли файл контрактом/договором."""
        if not filename:
            return False
        name_lower = filename.lower()
        for pattern in CONTRACT_PATTERNS:
            if re.search(pattern, name_lower, re.IGNORECASE):
                return True
        return False

    def _should_skip_file(self, filename: str) -> bool:
        """v7.2.2: Проверяет, нужно ли пропустить файл (экономия токенов)."""
        if not filename:
            return False
        name_lower = filename.lower()
        for pattern in SKIP_FILE_PATTERNS:
            if re.search(pattern, name_lower, re.IGNORECASE):
                logger.debug(f"[DocumentProcessor] Пропуск (чёрный список): {filename}")
                return True
        return False

    def _is_excel_only(self, filename: str) -> bool:
        """v7.2.2: Файл только для Excel-извлечения, не для LLM."""
        if not filename:
            return False
        name_lower = filename.lower()
        for pattern in EXCEL_ONLY_PATTERNS:
            if re.search(pattern, name_lower, re.IGNORECASE):
                return True
        return False

    def _get_file_priority(self, filename: str) -> int:
        """Определяет приоритет файла по названию."""
        if not filename:
            return 0
        name_lower = filename.lower()
        max_priority = 0
        for pattern, priority in FILE_PRIORITY.items():
            if re.search(pattern, name_lower, re.IGNORECASE):
                max_priority = max(max_priority, priority)
        return max_priority

    def _process_single_document(self, doc: DocumentInfo, tender_id: str = "") -> str:
        """Обрабатывает один документ. v7.8.1: принимает tender_id."""
        # Пропускаем большие контракты
        if doc.is_contract and doc.file_size_bytes > MAX_CONTRACT_FILE_SIZE:
            logger.info(f"[DocumentProcessor] Пропущен (контракт >200 KB): {doc.name}")
            return ""

        # Скачиваем (передаем tender_id для правильного именования)
        file_path = self._download_file(doc, tender_id=tender_id)
        if not file_path:
            return ""

        # Валидируем содержимое
        if not self._validate_file_content(file_path, doc.file_type):
            return ""

        # Извлекаем текст через подходящий экстрактор
        text = self._extract_text(file_path, doc.file_type, doc.name)
        if not text:
            return ""

        # Обрезаем слишком длинный текст
        if len(text) > MAX_TEXT_LENGTH:
            logger.info(
                f"[DocumentProcessor] Обрезано с {len(text)} до {MAX_TEXT_LENGTH}"
            )
            text = (
                text[:MAX_TEXT_LENGTH]
                + "\n[... текст обрезан — слишком длинный файл ...]"
            )

        return text

    def _download_file(self, doc: DocumentInfo, tender_id: str = "") -> Optional[Path]:
        """
        Скачивает файл с правильным расширением.
        v7.8.1: Добавляет tender_id в начало имени файла для MCP-сервера.
        """
        if not doc.file_url:
            return None

        try:
            if self.session:
                response = self.session.get(doc.file_url, timeout=30)
            else:
                import requests

                response = requests.get(doc.file_url, timeout=30, verify=False)

            if response.status_code != 200:
                logger.warning(
                    f"[DocumentProcessor] Статус {response.status_code}: {doc.file_url}"
                )
                if response.status_code == 404:
                    logger.error(
                        f"[DocumentProcessor] 🔴 404 на файл: {doc.file_url}\n"
                        f"   Заголовки: {dict(response.headers)}"
                    )
                return None

            content_length = len(response.content)
            doc.file_size_bytes = content_length

            # === БАГФИКС v6.8.6: корректное расширение ===
            safe_name = re.sub(r"[^\w\-_.]", "_", doc.name)[:80]

            # Пытаемся получить расширение из имени файла
            ext = Path(doc.name).suffix.lower()

            # Если расширения нет — пробуем определить по Content-Type
            if not ext:
                content_type = response.headers.get("Content-Type", "").lower()
                ext = self._ext_from_content_type(content_type, doc.file_type)

            # Если всё ещё нет — по magic bytes из содержимого
            if not ext:
                ext = self._ext_from_magic(response.content[:8])

            # v7.8.1: Формируем имя файла с tender_id для MCP
            prefix = f"{tender_id}_" if tender_id else ""

            if ext:
                file_path = (
                    self.download_dir / f"{prefix}{safe_name}_{int(time.time())}{ext}"
                )
            else:
                file_path = (
                    self.download_dir / f"{prefix}{safe_name}_{int(time.time())}"
                )
                logger.warning(
                    f"[DocumentProcessor] Не удалось определить расширение для {doc.name}"
                )

            with open(file_path, "wb") as f:
                f.write(response.content)

            logger.info(f"[DocumentProcessor] Файл сохранен: {file_path.name}")
            return file_path

        except Exception as e:
            logger.error(f"[DocumentProcessor] Ошибка скачивания: {e}")
            return None

    def _ext_from_content_type(self, content_type: str, file_type: str) -> str:
        """Определяет расширение по Content-Type."""
        mapping = {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-excel": ".xls",
            "application/zip": ".zip",
            "text/plain": ".txt",
            "application/rtf": ".rtf",
        }
        for ct, ext in mapping.items():
            if ct in content_type:
                return ext
        # Fallback на file_type
        if file_type:
            ft = file_type.lower().lstrip(".")
            if ft in ["pdf", "docx", "doc", "xlsx", "xls", "zip", "txt", "rtf"]:
                return f".{ft}"
        return ""

    def _ext_from_magic(self, header: bytes) -> str:
        """Определяет расширение по магическим байтам."""
        if header.startswith(PDF_MAGIC):
            return ".pdf"
        elif header.startswith(ZIP_MAGIC):
            return ".zip"
        elif header.startswith(OLE2_MAGIC):
            return ".doc"
        elif header.startswith(b"7z\xbc\xaf"):
            return ".7z"
        return ""

    def _validate_file_content(self, file_path: Path, file_type: str) -> bool:
        """Проверяет, что файл соответствует заявленному типу."""
        try:
            with open(file_path, "rb") as f:
                header = f.read(8)

            ext = (file_type.lower() if file_type else file_path.suffix.lower()).lstrip(
                "."
            )

            if ext in ["pdf"]:
                if not header.startswith(PDF_MAGIC):
                    if b"<html" in header or b"<!DOCTYPE" in header:
                        logger.error(
                            f"[DocumentProcessor] 🔴 {file_path.name} — HTML-страница, не PDF"
                        )
                    else:
                        logger.error(
                            f"[DocumentProcessor] 🔴 {file_path.name} — не PDF (magic: {header[:4].hex()})"
                        )
                    return False

            elif ext in ["zip", "docx", "xlsx"]:
                if not header.startswith(ZIP_MAGIC):
                    logger.error(
                        f"[DocumentProcessor]  {file_path.name} — не ZIP (magic: {header[:4].hex()})"
                    )
                    return False

            return True
        except Exception as e:
            logger.error(f"[DocumentProcessor] Ошибка проверки {file_path}: {e}")
            return False

    def _extract_text(self, file_path: Path, file_type: str, doc_name: str) -> str:
        """Делегирует извлечение подходящему экстрактору."""
        ext = file_type.lower() if file_type else file_path.suffix.lower()
        ext = ext.lstrip(".")

        # Пробуем найти экстрактор по расширению
        extractor = self._extractors.get(ext)
        if extractor:
            return extractor.extract(file_path, doc_name)

        # Если не нашли — определяем по содержимому
        detected_ext = self._detect_file_type(file_path)
        extractor = self._extractors.get(detected_ext)
        if extractor:
            logger.info(
                f"[DocumentProcessor] Файл {doc_name} — на самом деле {detected_ext}, "
                f"пробуем через {extractor.__class__.__name__}"
            )
            return extractor.extract(file_path, doc_name)

        logger.warning(f"[DocumentProcessor] Неизвестный тип: {ext}")
        return ""

    def _detect_file_type(self, file_path: Path) -> str:
        """Определяет тип файла по магическим байтам."""
        try:
            with open(file_path, "rb") as f:
                header = f.read(8)
            if header.startswith(ZIP_MAGIC):
                return "zip"
            elif header.startswith(OLE2_MAGIC):
                return "doc"
            elif header.startswith(PDF_MAGIC):
                return "pdf"
            elif header.startswith(b"PK"):
                return "zip"
        except Exception as e:
            logger.error(f"[DocumentProcessor] Ошибка определения типа: {e}")
        return file_path.suffix.lower().lstrip(".") or "unknown"
