"""
Фильтры для результатов поиска тендеров.
Вынесено из searcher.py (v6.6-r2).
ИСПРАВЛЕНО (v7.4.1): Добавлена очистка от неразрывных пробелов ЕИС (\xa0)
"""

import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from loguru import logger


class TenderFilters:
    """Фильтрует тендеры по релевантности, запрещённым словам, срокам."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self._exclude_keywords = self.config.get("exclude_keywords", [])
        self._relevance_keywords = self.config.get("relevance_keywords", [])
        self._exclude_composite = self.config.get("exclude_composite", [])
        self._exclude_context_exceptions = self.config.get(
            "exclude_context_exceptions", []
        )
        self._min_days_to_deadline = self.config.get("min_days_to_deadline", 3)

        # Лог загрузки ключевых слов при инициализации
        logger.info(
            f"[TenderFilters] Загружено {len(self._relevance_keywords)} ключей релевантности"
        )

    def is_relevant(self, title: str) -> bool:
        """Проверяет релевантность по ключевым словам."""
        if not title:
            logger.debug(f"[FILTER] Пустой заголовок → не релевантно")
            return False
            
        # Очистка от спецсимволов ЕИС
        text_lower = title.lower().replace("\xa0", " ").strip()
        
        # Временная диагностика: проверяем каждое ключевое слово
        keywords = self._relevance_keywords
        found_matches = []
        
        for keyword in keywords:
            kw_lower = keyword.lower()
            if kw_lower in text_lower:
                found_matches.append(kw_lower)
                
        if found_matches:
            logger.debug(f"[FILTER] ✅ РЕЛЕВАНТНО ('{title[:50]}...'). Найдены ключи: {found_matches[:3]}")
            return True
            
        # Логируем ПОЧЕМУ не прошло, если ничего не найдено
        logger.debug(
            f"[FILTER] ❌ НЕ РЕЛЕВАНТНО ('{title[:60]}...'). "
            f"Проверено {len(keywords)} ключей. "
            f"Очищенный текст: '{text_lower[:80]}...'"
        )
        return False

    def has_excluded_keywords(self, title: str) -> bool:
        """Проверяет наличие запрещённых слов с контекстной проверкой."""
        if not title:
            return False

        # Также чистим заголовок для проверки исключений
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
                        f"  [Filters] Пропущена составная фраза (контекст СОУТ/ОПР)"
                    )
                    continue
                logger.debug(f"  [Filters] Составная фраза: {composite['words']}")
                return True

        return False

    def _check_composite(self, text: str, composite: dict) -> bool:
        """Проверяет, находятся ли слова из composite рядом в тексте."""
        words = composite.get("words", [])
        max_distance = composite.get("max_distance", 5)

        if len(words) < 2:
            return False

        text_words = text.split()

        for i, w1 in enumerate(text_words):
            for word in words:
                if word in w1:
                    for other_word in words:
                        if other_word == word:
                            continue
                        for j in range(
                            max(0, i - max_distance),
                            min(len(text_words), i + max_distance + 1),
                        ):
                            if other_word in text_words[j]:
                                return True

        return False

    def check_deadline(self, deadline_str: Optional[str]) -> tuple[bool, Optional[int]]:
        """Проверяет, достаточно ли дней до дедлайна.

        Returns:
            (pass_filter: bool, days_left: Optional[int])
        """
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
        """Возвращает количество дней до дедлайна."""
        deadline = self._parse_deadline(deadline_str)
        if not deadline:
            return None
        today = datetime.now()
        delta = deadline - today
        return delta.days

    def _parse_deadline(self, deadline_str: str) -> Optional[datetime]:
        """Парсит строку дедлайна в datetime."""
        if not deadline_str:
            return None

        formats = [
            "%d.%m.%Y",
            "%d.%m.%Y %H:%M",
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(deadline_str.strip(), fmt)
            except ValueError:
                continue

        # Fallback: извлекаем дату из текста
        match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", deadline_str)
        if match:
            try:
                return datetime.strptime(match.group(0), "%d.%m.%Y")
            except ValueError:
                pass

        return None

    def sort_by_deadline(self, results: list) -> list:
        """Сортирует результаты по дедлайну (ближайшие первые)."""
        return sorted(
            results,
            key=lambda x: self._parse_deadline(getattr(x, "deadline_date", None))
            or datetime.max,
        )
