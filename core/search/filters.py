"""
core/search/filters.py
Фильтры для результатов поиска тендеров.
Вынесено из searcher.py (v6.6-r2).

ИСПРАВЛЕНО (v7.4.2):
- Точный расчет дней до дедлайна с учетом дат (.date()).
- Поддержка dict и getattr для универсальной сортировки sort_by_deadline.
- Оптимизирована проверка составных фраз _check_composite.
"""

import re
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Union
from loguru import logger


class TenderFilters:
    """Фильтрует тендеры по релевантности, запрещённым словам, срокам."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self._exclude_keywords: List[str] = self.config.get("exclude_keywords", [])
        self._relevance_keywords: List[str] = self.config.get("relevance_keywords", [])
        self._exclude_composite: List[Dict[str, Any]] = self.config.get(
            "exclude_composite", []
        )
        self._exclude_context_exceptions: List[str] = self.config.get(
            "exclude_context_exceptions", []
        )
        self._min_days_to_deadline: int = self.config.get("min_days_to_deadline", 3)

        logger.info(
            f"[TenderFilters] Загружено {len(self._relevance_keywords)} ключей релевантности"
        )

    def is_relevant(self, title: str) -> bool:
        """Проверяет релевантность по ключевым словам."""
        if not title:
            logger.debug("[FILTER] Пустой заголовок → не релевантно")
            return False

        # Очистка от спецсимволов ЕИС
        text_lower = title.lower().replace("\xa0", " ").strip()

        found_matches = [
            kw.lower() for kw in self._relevance_keywords if kw.lower() in text_lower
        ]

        if found_matches:
            logger.debug(
                f"[FILTER] ✅ РЕЛЕВАНТНО ('{title[:50]}...'). Найдены ключи: {found_matches[:3]}"
            )
            return True

        logger.debug(
            f"[FILTER] ❌ НЕ РЕЛЕВАНТНО ('{title[:60]}...'). "
            f"Проверено {len(self._relevance_keywords)} ключей."
        )
        return False

    def has_excluded_keywords(self, title: str) -> bool:
        """Проверяет наличие запрещённых слов с контекстной проверкой."""
        if not title:
            return False

        text_lower = title.lower().replace("\xa0", " ")

        # Контекстные исключения (СОУТ/ОПР)
        has_sout_context = any(
            exc.lower() in text_lower for exc in self._exclude_context_exceptions
        )

        # Точные подстроки
        for keyword in self._exclude_keywords:
            keyword_lower = keyword.lower()
            if keyword_lower in text_lower:
                if has_sout_context and "информационная безопасность" in keyword_lower:
                    logger.info(
                        f"  [Filters] Пропущено '{keyword}' (контекст СОУТ/ОПР)"
                    )
                    continue
                logger.debug(f"  [Filters] Найдено запрещённое: '{keyword}'")
                return True

        # Составные фразы
        for composite in self._exclude_composite:
            if self._check_composite(text_lower, composite):
                if has_sout_context and composite.get("check_context", False):
                    logger.info(
                        "  [Filters] Пропущена составная фраза (контекст СОУТ/ОПР)"
                    )
                    continue
                logger.debug(f"  [Filters] Составная фраза: {composite.get('words')}")
                return True

        return False

    def _check_composite(self, text: str, composite: dict) -> bool:
        """Проверяет, находятся ли слова из composite рядом в тексте."""
        words = [w.lower() for w in composite.get("words", [])]
        max_distance = composite.get("max_distance", 5)

        if len(words) < 2:
            return False

        text_words = re.findall(r"\w+", text)
        word_positions = {w: [] for w in words}

        for idx, token in enumerate(text_words):
            for target_word in words:
                if target_word in token:
                    word_positions[target_word].append(idx)

        # Проверяем, есть ли хотя бы одна пара слов в пределах max_distance
        first_word_positions = word_positions[words[0]]
        for pos1 in first_word_positions:
            for other_word in words[1:]:
                for pos2 in word_positions[other_word]:
                    if abs(pos1 - pos2) <= max_distance:
                        return True

        return False

    def check_deadline(self, deadline_str: Optional[str]) -> tuple[bool, Optional[int]]:
        """Проверяет, достаточно ли дней до дедлайна."""
        if not deadline_str or self._min_days_to_deadline <= 0:
            return True, None

        days_left = self._days_to_deadline(deadline_str)
        if days_left is None:
            return True, None

        if days_left < self._min_days_to_deadline:
            logger.info(
                f"  [Filters] До дедлайна {days_left} дней "
                f"(< {self._min_days_to_deadline}) — пропущен"
            )
            return False, days_left

        return True, days_left

    def _days_to_deadline(self, deadline_str: str) -> Optional[int]:
        """Возвращает количество дней до дедлайна с точной сверкой по календарным дням."""
        deadline_dt = self._parse_deadline(deadline_str)
        if not deadline_dt:
            return None

        today = date.today()
        deadline_date = deadline_dt.date()

        # Считаем разницу в целых календарных днях
        return (deadline_date - today).days

    def _parse_deadline(self, deadline_str: Union[str, datetime]) -> Optional[datetime]:
        """Парсит строку или объект дедлайна в datetime."""
        if isinstance(deadline_str, datetime):
            return deadline_str

        if not deadline_str or not isinstance(deadline_str, str):
            return None

        formats = [
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ]

        cleaned_str = deadline_str.strip()
        for fmt in formats:
            try:
                return datetime.strptime(cleaned_str, fmt)
            except ValueError:
                continue

        # Fallback: поиск регулярным выражением
        match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", cleaned_str)
        if match:
            try:
                return datetime.strptime(match.group(0), "%d.%m.%Y")
            except ValueError:
                pass

        return None

    def sort_by_deadline(self, results: list) -> list:
        """Универсальная сортировка результатов по дедлайну (ближайшие первые)."""

        def extract_deadline(item: Any) -> datetime:
            val = None
            if isinstance(item, dict):
                val = item.get("deadline") or item.get("deadline_date")
            else:
                val = getattr(item, "deadline", None) or getattr(
                    item, "deadline_date", None
                )

            parsed = self._parse_deadline(val)
            return parsed if parsed else datetime.max

        return sorted(results, key=extract_deadline)
