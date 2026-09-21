"""
core/extractors/docx_extractor.py
Единый экстрактор текста из DOCX/DOC файлов.
"""

import re
import zipfile
import subprocess
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional
from loguru import logger

from core.config.document_config import MAX_DOCX_TIMEOUT, QUANTITY_COLUMN_KEYWORDS
from core.extractors.base_extractor import BaseExtractor

try:
    import docx

    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False
    logger.warning("python-docx не установлен, используем zipfile fallback")


class DocxExtractor(BaseExtractor):
    """Единый экстрактор текста из DOCX/DOC файлов."""

    SUPPORTED_EXTENSIONS = ["docx", "doc"]

    def extract(self, file_path: Path, doc_name: str = "", **kwargs) -> str:
        """Извлекает текст из DOCX/DOC-файла."""
        path = Path(file_path)
        name = doc_name or path.name

        if not path.exists():
            logger.error(f"[DocxExtractor] Файл не найден: {path}")
            return ""

        is_docx = self._is_valid_docx(path)

        # Fallback для старых .doc (OLE)
        if not is_docx and path.suffix.lower() == ".doc":
            text = self._extract_doc_ole(path, name)
            if text:
                return text
            logger.error(f"[DocxExtractor] Не удалось прочитать DOC: {name}")
            return ""

        if not is_docx:
            logger.error(f"[DocxExtractor] Файл не является валидным DOCX: {name}")
            return ""

        # Оптимизация для больших файлов (>100 KB сразу через zipfile)
        file_size = path.stat().st_size
        MAX_SIZE_FOR_COM = 100 * 1024

        if file_size > MAX_SIZE_FOR_COM:
            logger.info(
                f"[DocxExtractor] Файл > 100KB ({file_size} байт). Сразу используем zipfile."
            )
            return self._extract_via_zip(path, name)

        # Пробуем python-docx с ограниченным таймаутом
        text = self._extract_with_docx(path, name)
        if text:
            return text

        logger.info(f"[DocxExtractor] Fallback на zipfile для {name}")
        return self._extract_via_zip(path, name)

    def _is_valid_docx(self, file_path: Path) -> bool:
        """Проверяет, является ли файл валидным DOCX (ZIP с XML)."""
        try:
            with zipfile.ZipFile(file_path, "r") as z:
                names = z.namelist()
                if "word/document.xml" in names:
                    return True
                if "[Content_Types].xml" in names:
                    content = z.read("[Content_Types].xml").decode(
                        "utf-8", errors="ignore"
                    )
                    if "wordprocessingml" in content:
                        return True
            return False
        except Exception:
            return False

    def _extract_doc_ole(self, file_path: Path, doc_name: str) -> str:
        """Fallback для старых .doc файлов (OLE format)."""
        text = ""

        # Попытка 0: pywin32 COM (Windows)
        try:
            import win32com.client

            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            doc = word.Documents.Open(str(file_path.resolve()))
            text = doc.Content.Text
            doc.Close(False)
            word.Quit()
            if text and len(text.strip()) > 50:
                logger.info(f"[DocxExtractor] pywin32 COM: {len(text)} симв.")
                return text
        except Exception as e:
            logger.debug(f"[DocxExtractor] pywin32 COM пропущен: {e}")

        # Попытка 1: LibreOffice headless (наиболее точная конвертация)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                result = subprocess.run(
                    [
                        "soffice",
                        "--headless",
                        "--convert-to",
                        "docx",
                        "--outdir",
                        tmpdir,
                        str(file_path),
                    ],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    converted = Path(tmpdir) / file_path.with_suffix(".docx").name
                    if converted.exists():
                        return self._extract_via_zip(converted, doc_name)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Попытка 2: antiword
        try:
            result = subprocess.run(
                ["antiword", str(file_path)], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout:
                logger.info(f"[DocxExtractor] antiword: {len(result.stdout)} симв.")
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Попытка 3: textract
        try:
            import textract

            raw = textract.process(str(file_path))
            text = raw.decode("utf-8", errors="ignore")
            logger.info(f"[DocxExtractor] textract: {len(text)} симв.")
            return text
        except Exception:
            pass

        # Попытка 4: olefile (pure Python fallback)
        try:
            import olefile

            if olefile.isOleFile(file_path):
                ole = olefile.OleFileIO(file_path)
                if ole.exists("WordDocument"):
                    stream = ole.openstream("WordDocument")
                    data = stream.read()
                    text = self._extract_text_from_ole_word(data)
                    logger.info(f"[DocxExtractor] olefile: {len(text)} симв.")
                    return text
        except Exception as e:
            logger.warning(f"[DocxExtractor] olefile ошибка: {e}")

        return text

    def _extract_text_from_ole_word(self, data: bytes) -> str:
        """Извлекает печатный текст из WordDocument stream с учетом CP1251."""
        text_parts = []
        # Фильтрация непечатных символов
        clean_bytes = bytearray()
        for b in data:
            if b in (9, 10, 13) or (32 <= b <= 126) or (0xC0 <= b <= 0xFF):
                clean_bytes.append(b)
            else:
                clean_bytes.append(32)

        raw_text = clean_bytes.decode("cp1251", errors="ignore")
        words = [w for w in raw_text.split() if len(w) > 1]
        return " ".join(words)

    def _extract_with_docx(self, file_path: Path, doc_name: str) -> str:
        """Извлекает текст через python-docx с таймаутом."""
        if not HAS_DOCX:
            return ""

        def _do_extract():
            try:
                document = docx.Document(file_path)
                paragraphs = [
                    para.text.strip()
                    for para in document.paragraphs
                    if para.text.strip()
                ]
                tables_text = self._extract_tables(document)
                return "\n".join(paragraphs + tables_text)
            except Exception as e:
                logger.error(f"[DocxExtractor] Ошибка python-docx: {e}")
                return ""

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_do_extract)
            res = future.result(timeout=MAX_DOCX_TIMEOUT)
            executor.shutdown(wait=False)
            return res
        except FutureTimeoutError:
            logger.error(f"[DocxExtractor] Таймаут {MAX_DOCX_TIMEOUT}с на {doc_name}")
            executor.shutdown(wait=False)
            return ""
        except Exception as e:
            logger.error(f"[DocxExtractor] Ошибка при исполнении: {e}")
            executor.shutdown(wait=False)
            return ""

    def _extract_tables(self, document) -> list[str]:
        """Извлекает таблицы с заголовками колонок."""
        tables_text = []
        for table_idx, table in enumerate(document.tables):
            try:
                rows_data = []
                for row in table.rows:
                    cells = [self._extract_cell_text(cell) for cell in row.cells]
                    rows_data.append(cells)

                if self._is_data_table(rows_data):
                    formatted = self._format_data_table(rows_data, table_idx)
                    if formatted:
                        tables_text.append(formatted)
                else:
                    headers = self._extract_headers(table)
                    rows_text = self._extract_rows(table, headers)
                    tables_text.extend(rows_text)
            except Exception as e:
                logger.debug(f"[DocxExtractor] Ошибка таблицы {table_idx}: {e}")
                continue
        return tables_text

    def _extract_cell_text(self, cell) -> str:
        """Извлекает текст из ячейки, включая вложенные таблицы."""
        texts = [para.text.strip() for para in cell.paragraphs if para.text.strip()]
        for nested_table in cell.tables:
            nested_text = self._format_table(nested_table, -1)
            if nested_text:
                texts.append(nested_text)
        return " ".join(texts)

    def _is_data_table(self, rows_data: list) -> bool:
        """Определяет, содержит ли таблица структурированные данные."""
        if len(rows_data) < 2:
            return False

        header_text = " ".join(h.lower() for h in rows_data[0])
        data_keywords = [
            "№",
            "наименование",
            "количество",
            "кол-во",
            "ед.",
            "цена",
            "сумма",
            "адрес",
        ]
        has_header = any(kw in header_text for kw in data_keywords)

        total_cells = sum(len(row) for row in rows_data[1:])
        numeric_count = sum(
            1
            for row in rows_data[1:]
            for cell in row
            if cell and re.search(r"\d+", cell)
        )

        has_numbers = numeric_count > 0 and (numeric_count / max(total_cells, 1)) > 0.2
        return has_header or has_numbers

    def _format_table(self, table, table_index: int) -> str:
        """Форматирует таблицу в читаемый вид."""
        rows_data = [
            [self._extract_cell_text(cell) for cell in row.cells] for row in table.rows
        ]
        if not rows_data:
            return ""
        if self._is_data_table(rows_data):
            return self._format_data_table(rows_data, table_index)
        return "\n".join(" | ".join(c for c in row if c) for row in rows_data)

    def _format_data_table(self, rows_data: list, table_index: int) -> str:
        """Форматирует таблицу данных в читаемый текстовый блок."""
        lines = [f"=== ТАБЛИЦА {table_index + 1} ==="]
        if rows_data:
            header = [h.strip() if h else "" for h in rows_data[0]]
            lines.append(" | ".join(header))
            lines.append("-" * 40)

        for row in rows_data[1:]:
            cells = [c.strip() if c else "" for c in row]
            if any(cells):
                lines.append(" | ".join(cells))

        lines.append("=== КОНЕЦ ТАБЛИЦЫ ===")
        return "\n".join(lines)

    def _extract_headers(self, table) -> list[str]:
        """Извлекает заголовки таблицы."""
        try:
            if table.rows:
                return [cell.text.strip().lower() for cell in table.rows[0].cells]
        except Exception:
            pass
        return []

    def _extract_rows(self, table, headers: list) -> list[str]:
        """Извлекает строки таблицы с сопоставлением колонок."""
        rows_text = []
        for row_idx, row in enumerate(table.rows):
            if row_idx == 0 and headers:
                continue
            try:
                row_text = []
                for col_idx, cell in enumerate(row.cells):
                    cell_text = cell.text.strip()
                    if not cell_text:
                        continue
                    if col_idx < len(headers) and headers[col_idx]:
                        header = headers[col_idx]
                        if self._looks_like_quantity(header, cell_text):
                            row_text.append(f"{header}: {cell_text}")
                        else:
                            row_text.append(cell_text)
                    else:
                        row_text.append(cell_text)
                if row_text:
                    rows_text.append(" | ".join(row_text))
            except Exception:
                continue
        return rows_text

    def _looks_like_quantity(self, header: str, cell_text: str) -> bool:
        """Проверяет соответствие заголовка и ячейки параметрам количества."""
        if not cell_text or not re.match(r"^[\d\s.,]+$", cell_text.replace(" ", "")):
            return False
        return any(kw in header.lower() for kw in QUANTITY_COLUMN_KEYWORDS)

    def _extract_via_zip(self, file_path: Path, doc_name: str) -> str:
        """Fallback извлечение чистого текста из XML напрямую."""
        try:
            with zipfile.ZipFile(file_path, "r") as z:
                if "word/document.xml" not in z.namelist():
                    return ""
                xml_content = z.read("word/document.xml").decode(
                    "utf-8", errors="ignore"
                )
                text = re.sub(r"<[^>]+>", " ", xml_content)
                text = re.sub(r"\s+", " ", text)
                text = re.sub(r" (\d+\.) ", r"\n\1 ", text)
                result = text.strip()
                logger.info(f"[DocxExtractor] Zipfile fallback: {len(result)} симв.")
                return result
        except Exception as e:
            logger.error(f"[DocxExtractor] Ошибка zip extraction: {e}")
            return ""
