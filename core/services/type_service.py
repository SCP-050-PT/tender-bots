"""
Единый сервис определения типа тендера.
Заменяет: type_resolver.py (KEYWORDS), tender_type.py (TYPE_KEYWORDS, _normalize_alias),
          main.py (_detect_type_from_title).
"""

from typing import Optional, Dict, Any, Tuple
from loguru import logger


class TypeService:
    """Определяет тип тендера по каскадной логике. Единый источник ключевых слов."""

    VERSION = "v7.0.0"

    # === ЕДИНСТВЕННЫЙ источник ключевых слов для всех типов ===
    KEYWORDS = {
        "testing": [
            "испытание",
            "испытания",
            "пожарных лестниц",
            "наружных лестниц",
            "техническое диагностирование",
        ],
        "education": [
            "обучение",
            "слушатели",
            "программа обучения",
            "удостоверение",
            "повышение квалификации",
            "переподготовка",
            "инструктаж",
            "стажировка",
            "профессиональное обучение",
            "курсы",
            "образовательные услуги",
            "обучение охране труда",
            "обучению охране труда",
            "обучения охране труда",
            "пожарная безопасность",
            "пожарной безопасности",
            "промышленная безопасность",
            "промышленной безопасности",
            "обучение рабочих профессий",
            "рабочих специальностей",
            "технологические карты",
            "ппр",
            "санитарно-защитная зона",
            "тренинги",
            "образовательные"
        ],
        "sout": [
            "специальная оценка",
            "соут",
            "вредные факторы",
            "класс условий труда",
            "оценка условий труда",
            "оценка рабочих мест",
            "карты соут",
            "специальной оценки условий труда",
            "специальной оценке условий труда",
            "специальной оценкой условий труда",
        ],
        "plk": [
                    "производственный контроль",
                    "плк",
                    "лабораторные исследования",
                    "лабораторный контроль",
                    "замеры шума",
                    "замеры вибрации",
                    "санитарно-гигиенические исследования",
                    "производственного лабораторного контроля",
                    "лабораторные испытания",
                    "лабораторно-инструментальн",
                    "инструментального контроля",
                    "уровней воздействия",
                    "вредных производственных факторов",
                    "физических и химических факторов",
                    "плановый периодический контроль",
                    "внеплановый оперативный контроль",
        ],
        "opr": [
            "профессиональный риск",
            "опр",
            "оценка рисков",
            "идентификация опасностей",
            "мероприятия по снижению рисков",
            "оценка профессиональных рисков",
            "оценки профессиональных рисков",
            "оценке профессиональных рисков",
            "оценкой профессиональных рисков",
            "профессиональных рисков",
            "профессиональные риски",
        ],
    }

    # Ключевые слова для title (приоритет над КТРУ)
    TITLE_KEYWORDS = {
        "plk": [
            "производственный лабораторный контроль",
            "производственного лабораторного контроля",
            "лабораторный контроль",
            "плк",
            "лабораторные исследования",
            "лабораторные испытания",
            "производственного контроля",
            "производственный контроль",
            "вредных производственных факторов",
        ],
        "opr": [
            "оценка профессиональных рисков",
            "оценки профессиональных рисков",
            "оценке профессиональных рисков",
            "оценкой профессиональных рисков",
            "опр",
            "профессиональный риск",
            "профессиональных рисков",
            "профессиональные риски",
        ],
        "sout": [
            "специальная оценка условий труда",
            "специальной оценки условий труда",
            "специальной оценке условий труда",
            "специальной оценкой условий труда",
            "соут",
            "оценка условий труда",
            "оценки условий труда",
            "оценке условий труда",
            "специальная оценка рабочих мест",
        ],
        "education": [
            "обучение по охране труда",
            "обучение охране труда",
            "повышение квалификации",
            "переподготовка",
            "профессиональное обучение",
            "программа обучения",
            "курсы повышения квалификации",
            "пожарная безопасность",
            "промышленная безопасность",
            "обучение рабочих",
            "рабочих специальностей",
            "технологические карты",
            "ппр",
            "санитарно-защитная зона",
            "обучение и проверка знаний",
            "проверка знаний требований охраны труда",
            "проверка знаний по охране труда",
        ],
    }

    # === ЕДИНСТВЕННЫЙ источник алиасов ===
    TYPE_ALIASES = {
        "sout": "sout",
        "соут": "sout",
        "специальная оценка": "sout",
        "специальной оценки": "sout",
        "education": "education",
        "обучение": "education",
        "обучения": "education",
        "opr": "opr",
        "опр": "opr",
        "оценка профессиональных рисков": "opr",
        "оценки профессиональных рисков": "opr",
        "plk": "plk",
        "плк": "plk",
        "производственный контроль": "plk",
        "производственного контроля": "plk",
        "combined": "combined",
        "комбинированный": "combined",
        "комбинированного": "combined",
    }

    def normalize(self, raw_type: str) -> str:
        """Нормализует строковый тип тендера."""
        if not raw_type:
            return "unknown"
        return self.TYPE_ALIASES.get(raw_type.lower().strip(), raw_type.lower().strip())

    def resolve(
        self,
        tender_info: Dict[str, Any],
        documents_text: str,
        llm_classification: Optional[str] = None,
        llm_confidence: float = 0.0,
        tender_type_hint: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        """
        Каскадное определение типа тендера.

        Returns:
            (type, source, method)
        """
        # Шаг 1: hint из detailed_parser
        if tender_type_hint:
            normalized = self.normalize(tender_type_hint)
            logger.info(f"[{self.VERSION}] Тип из detailed_parser hint: {normalized}")
            return normalized, "detailed_parser_hint", "hint"

        # Шаг 1.5: Проверка title (приоритет над КТРУ)
        purchase_name = tender_info.get("purchase_name", "")
        if purchase_name:
            name_lower = purchase_name.lower()
            for ttype, keywords in self.TITLE_KEYWORDS.items():
                if any(kw in name_lower for kw in keywords):
                    logger.info(
                        f"[{self.VERSION}] Тип из title: {ttype} ('{purchase_name[:60]}...')"
                    )
                    return ttype, "title_heuristic", "heuristic"

        # Шаг 2: LLM классификация (высокий confidence)
        if llm_classification and llm_confidence >= 0.7:
            normalized = self.normalize(llm_classification)
            logger.info(f"[{self.VERSION}] Тип из LLM классификации: {normalized}")
            return normalized, "llm_classification", "classify"

        # Шаг 3: КТРУ-данные
        has_rm = bool(tender_info.get("rm_total") and tender_info["rm_total"] > 0)
        has_students = bool(
            tender_info.get("students_count") and tender_info["students_count"] > 0
        )

        if has_rm and has_students:
            logger.info(f"[{self.VERSION}] Тип из КТРУ: combined (РМ + слушатели)")
            return "combined", "ktru", "data"
        if has_rm:
            logger.info(
                f"[{self.VERSION}] Тип из КТРУ: sout ({tender_info['rm_total']} РМ)"
            )
            return "sout", "ktru", "data"
        if has_students:
            logger.info(
                f"[{self.VERSION}] Тип из КТРУ: education ({tender_info['students_count']} слушателей)"
            )
            return "education", "ktru", "data"

        # Шаг 4: Эвристика по тексту документов
        text_lower = documents_text.lower()
        for ttype, keywords in self.KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                if ttype == "education" and (
                    "охрана труда" in text_lower or "охране труда" in text_lower
                ):
                    logger.info(f"[{self.VERSION}] Тип из текста: education (ОТ)")
                    return "education", "text_heuristic", "heuristic"
                logger.info(f"[{self.VERSION}] Тип из текста: {ttype}")
                return ttype, "text_heuristic", "heuristic"

        # Шаг 5: Fallback
        logger.warning(f"[{self.VERSION}] Тип не определён, будет ручная проверка")
        return "unknown", "fallback", "none"

    def detect_variant(self, text: str, llm_variant: Optional[int] = None) -> int:
        """Определяет вариант СОУТ (1, 2, 3)."""
        if llm_variant in (1, 2, 3):
            logger.info(f"[{self.VERSION}] Вариант СОУТ из LLM: {llm_variant}")
            return llm_variant

        text_lower = text.lower()

        # Вариант 3: протоколы/комплекты
        variant3_positive = ["протокол проверки знаний", "комплект протоколов"]
        variant3_negative = ["обучение", "комиссия", "заседание"]

        for kw in variant3_positive:
            if kw in text_lower:
                context_window = text_lower[
                    max(0, text_lower.find(kw) - 100) : text_lower.find(kw) + 100
                ]
                is_false_positive = any(
                    neg in context_window for neg in variant3_negative
                )
                if not is_false_positive:
                    logger.info(f"[{self.VERSION}] Вариант СОУТ 3: протоколы")
                    return 3

        # Вариант 2: карты
        if any(
            kw in text_lower
            for kw in ["карты соут", "карта специальной оценки", "карты специальной"]
        ):
            logger.info(f"[{self.VERSION}] Вариант СОУТ 2: карты")
            return 2

        logger.info(f"[{self.VERSION}] Вариант СОУТ 1: по умолчанию")
        return 1
