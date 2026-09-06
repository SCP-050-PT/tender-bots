"""
core/tender_type.py
Единое определение типа тендера.
ИСПРАВЛЕНО (29.07.2026 v6.5):
  - detect_variant: контекстная проверка "протокол" в СОУТ vs обучение
  - "протокол проверки знаний" → НЕ variant=3
  - "протокол комиссии" → НЕ variant=3
  - Только "протоколы СОУТ" или "комплекты протоколов СОУТ" → variant=3
"""

import re
from typing import Optional, Tuple
from dataclasses import dataclass
from loguru import logger


@dataclass
class TypeDetectionResult:
    tender_type: str
    confidence: float
    is_combined: bool
    primary_type: str
    secondary_type: str
    matched_keywords: list
    reason: str


class TenderTypeDetector:
    """Единый детектор типа тендера."""

    TYPE_ALIASES = {
        "sout": "соут",
        "education": "обучение",
        "plk": "плк",
        "opr": "опр",
        "combined": "комбинированный",
        "unknown": "соут",
        "соут": "соут",
        "обучение": "обучение",
        "плк": "плк",
        "опр": "опр",
        "комбинированный": "комбинированный",
        "неизвестно": "соут",
        "oplk": "плк",
        "sout_education": "комбинированный",
        "education_sout": "комбинированный",
        "plk_opr": "комбинированный",
        "opr_plk": "комбинированный",
        "sout_opr": "комбинированный",
        "opr_sout": "комбинированный",
    }

    TYPE_KEYWORDS = {
        "соут": [
            ("специальная оценка условий труда", 10),
            ("специальная оценка труда", 9),
            ("оценка условий труда", 8),
            ("оценка рабочих мест", 8),
            ("специальная оценка", 7),
            ("соут", 10),
            ("сои", 8),
        ],
        "обучение": [
            ("обучение охране труда", 100),
            ("обучение по охране труда", 100),
            ("обучению охране труда", 100),
            ("обучению по охране труда", 100),
            ("оказание услуг по обучению", 100),
            ("услуги по обучению", 100),
            ("обучение работников", 100),
            ("обучение работодателей", 100),
            ("обучение", 5),
            ("повышение квалификации", 8),
            ("переподготовка", 8),
            ("профессиональная подготовка", 7),
            ("профессиональное образование", 7),
            ("дополнительное образование", 7),
            ("переквалификация", 7),
            ("обучение безработных", 7),
            ("проверка знаний", 6),
            ("аттестация", 6),
            ("инструктаж", 5),
            ("тренинг", 5),
            ("пожарная безопасность", 6),
            ("промышленная безопасность", 6),
            ("электробезопасность", 6),
            ("работы на высоте", 6),
            ("газоопасные работы", 6),
            ("первая помощь", 6),
            ("технологические карты", 6),
            ("озп", 6),
            ("ппр", 6),
            ("техносферная безопасность", 6),
        ],
        "плк": [
            ("производственный лабораторный контроль", 10),
            ("лабораторный контроль", 9),
            ("лабораторные исследования", 8),
            ("замеры вредных факторов", 8),
            ("плк", 10),
            ("санитарно-защитная зона", 7),
            ("сзз", 7),
            ("гигиеническая оценка", 7),
            ("факторы среды", 6),
        ],
        "опр": [
            ("оценка профессиональных рисков", 10),
            ("опр", 10),
            ("профессиональные риски", 8),
            ("управление профессиональными рисками", 8),
        ],
    }

    # ← v6.5: УЛУЧШЕННЫЕ ВАРИАНТЫ СОУТ С КОНТЕКСТНОЙ ПРОВЕРКОЙ
    VARIANT_KEYWORDS = {
        2: {
            "positive": [
                "карта",
                "карты",
                "карт",
                "индивидуальная карта",
                "оценочная карта",
            ],
            "negative": [],  # Нет ложных срабатываний для карт
        },
        3: {
            "positive": [
                "протоколы соут",
                "протоколы специальной оценки",
                "комплекты протоколов соут",
                "комплекты протоколов специальной оценки",
                "протоколы условий труда",
            ],
            "negative": [
                "протокол проверки знаний",
                "протокол аттестации",
                "протокол обучения",
                "протокол комиссии",
                "протокол идентификации",
                "протокол заседания",
                "протокол собрания",
            ],
        },
    }

    def __init__(self):
        logger.info("TenderTypeDetector инициализирован (v6.5)")

    def detect(
        self, text: str, llm_type_hint: Optional[str] = None
    ) -> TypeDetectionResult:
        if not text:
            return self._result("соут", 0.0, [], "пустой текст")

        text_lower = text.lower()

        scores = {}
        matched_keywords = []

        for ttype, keywords in self.TYPE_KEYWORDS.items():
            score = 0
            for keyword, weight in keywords:
                count = self._count_keyword_safe(text, keyword)
                if count > 0:
                    score += count * weight
                    matched_keywords.append((ttype, keyword, weight, count))
            scores[ttype] = score

        exclusive_education = False
        for ttype, keywords in self.TYPE_KEYWORDS.items():
            for keyword, weight in keywords:
                if weight >= 100 and keyword.lower() in text_lower:
                    if ttype == "обучение":
                        exclusive_education = True
                        logger.info(
                            f"[tender_type] Эксклюзивное ключевое слово обучения: '{keyword}'"
                        )
                        break

        has_sout = scores.get("соут", 0) > 0
        has_opr = scores.get("опр", 0) > 0
        has_education = scores.get("обучение", 0) > 0
        has_plk = scores.get("плк", 0) > 0

        is_combined = has_sout and has_opr and not exclusive_education

        if exclusive_education:
            final_type = "обучение"
            confidence = min(1.0, 0.5 + scores.get("обучение", 0) / 100)
            reason = f"эксклюзивное ключевое слово обучения (score={scores.get('обучение', 0)})"

        elif is_combined:
            final_type = "комбинированный"
            confidence = min(
                1.0, 0.6 + (scores.get("соут", 0) + scores.get("опр", 0)) / 50
            )
            reason = (
                f"combined: СОУТ({scores.get('соут', 0)}) + ОПР({scores.get('опр', 0)})"
            )

        else:
            if not scores or max(scores.values()) == 0:
                final_type = "соут"
                confidence = 0.1
                reason = "нет ключевых слов → fallback 'соут'"
            else:
                candidate_scores = {
                    k: v for k, v in scores.items() if k != "комбинированный"
                }
                best_type = max(candidate_scores, key=candidate_scores.get)
                best_score = candidate_scores[best_type]

                if (
                    has_education
                    and has_sout
                    and scores.get("обучение", 0) == scores.get("соут", 0)
                ):
                    if "обучение" in text_lower:
                        final_type = "обучение"
                        reason = "равные скоры, приоритет 'обучение' (по правилу)"
                    else:
                        final_type = best_type
                        reason = f"максимальный скор: {best_type}={best_score}"
                else:
                    final_type = best_type
                    reason = f"максимальный скор: {best_type}={best_score}"

                total_score = sum(candidate_scores.values())
                if total_score > 0:
                    confidence = min(1.0, best_score / total_score * 2)
                else:
                    confidence = 0.3

        if (
            final_type == "обучение"
            and "рабочих мест" in text_lower
            and "слушател" not in text_lower
        ):
            logger.warning(
                f"[tender_type] Тип 'обучение', но найдено 'рабочих мест' без 'слушателей' — "
                f"возможно неверное определение"
            )
            confidence *= 0.7

        if llm_type_hint:
            normalized_hint = self._normalize_alias(llm_type_hint)
            if normalized_hint != final_type and confidence < 0.5:
                logger.info(
                    f"[tender_type] Низкий confidence={confidence:.2f}, "
                    f"LLM говорит '{normalized_hint}' → используем LLM"
                )
                final_type = normalized_hint
                confidence = 0.5
                reason += f" (скорректировано по LLM: {normalized_hint})"

        return TypeDetectionResult(
            tender_type=final_type,
            confidence=round(confidence, 2),
            is_combined=is_combined,
            primary_type="соут" if is_combined else final_type,
            secondary_type="опр" if is_combined else "",
            matched_keywords=[
                f"{t}:{k}({w}×{c})" for t, k, w, c in matched_keywords if w < 100
            ],
            reason=reason,
        )

    def detect_variant(self, text: str, llm_variant: Optional[int] = None) -> int:
        """
        Определяет вариант СОУТ (1, 2, 3) с контекстной проверкой.
        """
        if llm_variant in (1, 2, 3):
            logger.info(f"Вариант СОУТ из LLM: {llm_variant}")
            return llm_variant

        text_lower = text.lower()

        # ← v6.5: Проверяем вариант 3 с контекстной фильтрацией
        variant3_positive = self.VARIANT_KEYWORDS[3]["positive"]
        variant3_negative = self.VARIANT_KEYWORDS[3]["negative"]

        for kw in variant3_positive:
            if kw in text_lower:
                # Дополнительная проверка: не попадает ли в negative?
                is_false_positive = False
                for neg_kw in variant3_negative:
                    if neg_kw in text_lower:
                        # Проверяем: positive находится рядом с "соут" или "специальная оценка"?
                        # Если нет — возможно false positive
                        context_window = text_lower[
                            max(0, text_lower.find(kw) - 100) : text_lower.find(kw)
                            + 100
                        ]
                        if (
                            "соут" not in context_window
                            and "специальная оценка" not in context_window
                        ):
                            is_false_positive = True
                            logger.info(
                                f"[v6.5] Пропущен false positive: '{kw}' найдено, но рядом '{neg_kw}' без контекста СОУТ"
                            )
                            break

                if not is_false_positive:
                    logger.info(
                        f"[v6.5] Вариант СОУТ 3 (по keywords '{kw}'): протоколы/комплекты СОУТ"
                    )
                    return 3

        # ← v6.5: Проверяем, не попало ли слово "протокол" в negative-список
        for neg_kw in variant3_negative:
            if neg_kw in text_lower:
                logger.info(
                    f"[v6.5] Слово 'протокол' в контексте '{neg_kw}' — это НЕ variant=3 (обучение/комиссия)"
                )

        # Проверяем вариант 2 (карты)
        for kw in self.VARIANT_KEYWORDS[2]["positive"]:
            if kw in text_lower:
                logger.info(f"[v6.5] Вариант СОУТ 2 (по keywords '{kw}'): карты")
                return 2

        logger.info("[v6.5] Вариант СОУТ 1 (по умолчанию): 20% основных + аналогия")
        return 1

    def _normalize_alias(self, raw_type: str) -> str:
        if not raw_type:
            return "соут"
        raw_lower = raw_type.lower().strip()
        if raw_lower in self.TYPE_ALIASES:
            return self.TYPE_ALIASES[raw_lower]
        for alias, normalized in self.TYPE_ALIASES.items():
            if alias in raw_lower or raw_lower in alias:
                return normalized
        return "соут"

    def _result(
        self, ttype: str, confidence: float, matched: list, reason: str
    ) -> TypeDetectionResult:
        return TypeDetectionResult(
            tender_type=ttype,
            confidence=confidence,
            is_combined=(ttype == "комбинированный"),
            primary_type=ttype,
            secondary_type="",
            matched_keywords=matched,
            reason=reason,
        )

    def validate_education_has_students(
        self, text: str, students_count: int
    ) -> Tuple[bool, str]:
        if students_count > 0:
            return True, ""

        text_lower = text.lower()
        has_students_keyword = any(
            kw in text_lower
            for kw in ["слушател", "человек", "участник", "сотрудник", "групп"]
        )

        if not has_students_keyword:
            return False, "Тип 'обучение', но количество слушателей не найдено в тексте"

        return True, ""

    def validate_sout_has_rm(self, text: str, rm_total: int) -> Tuple[bool, str]:
        if rm_total > 0:
            return True, ""

        text_lower = text.lower()
        has_rm_keyword = any(
            kw in text_lower
            for kw in ["рабочих мест", "оценке рабочих", "специальная оценка"]
        )

        if not has_rm_keyword:
            return False, "Тип 'соут', но количество РМ не найдено в тексте"

        return True, ""
    
    def _count_keyword_safe(self, text: str, keyword: str) -> int:
        """Безопасный подсчет ключевых слов с учетом границ."""
        text_lower = text.lower()
        kw_lower = keyword.lower()
        
        # Для коротких аббревиатур (опр, плк, соут) ищем как отдельные слова
        if len(kw_lower) <= 4:
            # Используем regex с границами слов \b
            pattern = r'\b' + re.escape(kw_lower) + r'\b'
            return len(re.findall(pattern, text_lower))
        else:
            # Для длинных фраз обычный count безопасен
            return text_lower.count(kw_lower)


_type_detector: Optional[TenderTypeDetector] = None


def get_type_detector() -> TenderTypeDetector:
    global _type_detector
    if _type_detector is None:
        _type_detector = TenderTypeDetector()
    return _type_detector

