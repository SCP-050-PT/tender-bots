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
        # В TypeService.TITLE_KEYWORDS добавить:
        "combined": [
            "комплекс работ",
            "мероприятия по улучшению условий труда",
            "соут и производственный контроль",
            "соут и плк",
            "оценка условий труда и производственный контроль",
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
        Приоритет: Название (Title) > Жесткие правила > Хинт парсера > Текст > LLM > КТРУ.
        """
        purchase_name = tender_info.get("purchase_name", "").lower()
        doc_text_lower = documents_text.lower() if documents_text else ""

        # === ШАГ 0: Жесткий приоритет Обучения (Расширенный) ===
        # Проверяем название И начало текста на явные маркеры обучения
        education_markers_title = [
            "обучение", "подготовка кадров", "образовательных услуг", 
            "повышение квалификации", "переподготовка", "курсы"
        ]
        education_markers_text = [
            "слушателей", "учебный план", "программа обучения", 
            "удостоверение", "диплом", "проверка знаний"
        ]
        
        is_education_title = any(kw in purchase_name for kw in education_markers_title)
        is_education_text = any(kw in doc_text_lower[:2000] for kw in education_markers_text) #只看前2000字符

        # Если это явно обучение (по названию или тексту), и это НЕ чистый СОУТ/ПЛК/ОПР лот
        if is_education_title or (is_education_text and "обучение" in doc_text_lower[:500]):
            # Исключения: если в названии явно указано другое
            if not any(kw in purchase_name for kw in ["специальная оценка", "соут", "производственный контроль", "оценка профессиональных рисков"]):
                logger.info(f"[{self.VERSION}] HARD RULE: Маркеры обучения найдены -> Education")
                return "education", "hard_rule_education", "heuristic"

        # === ШАГ 1: Проверка TITLE (Приоритет над Hint!) ===
        if purchase_name:
            # 1.1 Проверяем Combined в названии
            if "combined" in self.TITLE_KEYWORDS:
                for kw in self.TITLE_KEYWORDS["combined"]:
                    if kw in purchase_name:
                        logger.info(f"[{self.VERSION}] Тип из title: combined ('{purchase_name[:60]}...')")
                        return "combined", "title_heuristic", "heuristic"

            # 1.2 Проверяем остальные типы в названии
            for ttype, keywords in self.TITLE_KEYWORDS.items():
                if ttype == "combined": continue
                if any(kw in purchase_name for kw in keywords):
                    logger.info(f"[{self.VERSION}] Тип из title: {ttype} ('{purchase_name[:60]}...')")
                    return ttype, "title_heuristic", "heuristic"

        # === ШАГ 2: Hint из detailed_parser ===
        if tender_type_hint:
            normalized = self.normalize(tender_type_hint)
            # Защита: если хинт 'opr', но в названии/тексте явное обучение -> education
            if normalized == "opr" and (is_education_title or is_education_text):
                 logger.warning(f"[{self.VERSION}] Конфликт: Hint=OPR, но найдены маркеры обучения -> Education")
                 return "education", "hint_override_education", "heuristic"
            
            # Защита: если хинт 'opr', но в названии есть 'лабораторные' -> plk
            if normalized == "opr" and any(kw in purchase_name for kw in ["лабораторн", "испытан", "замер"]):
                 logger.warning(f"[{self.VERSION}] Конфликт: Hint=OPR, но в названии лаборатория -> PLK")
                 return "plk", "hint_override_title", "heuristic"
            
            logger.info(f"[{self.VERSION}] Тип из detailed_parser hint: {normalized}")
            return normalized, "detailed_parser_hint", "hint"

        # === ШАГ 3: Проверка на комбо-тендер по ТЕКСТУ ===
        has_sout_kw = any(kw in doc_text_lower for kw in ["специальная оценка", "соут", "оценка условий труда"])
        has_opr_kw = any(kw in doc_text_lower for kw in ["оценка профессиональных рисков", "опр", "профессиональных рисков"])
        has_plk_kw = any(kw in doc_text_lower for kw in ["производственный контроль", "плк", "лабораторные исследования", "замеры"])

        combo_count = sum([has_sout_kw, has_opr_kw, has_plk_kw])
        if combo_count >= 2:
            logger.info(f"[{self.VERSION}] Обнаружен комбо-тендер по тексту ({combo_count} типа)")
            return "combined", "text_heuristic_combo", "heuristic"

        # === ШАГ 4: Защита от ложного ОПР (лаборатория/обучение) по тексту ===
        if tender_type_hint == "opr":
            if any(kw in doc_text_lower for kw in ["лабораторные исследования", "испытания проб", "сточной воды"]):
                logger.warning(f"[{self.VERSION}] Ложный ОПР: признаки лаборатории -> PLK")
                return "plk", "text_guard_opr", "heuristic"
            
            # Если в тексте много про обучение, а хинт ОПР - это ошибка
            if doc_text_lower.count("обучение") > 2 and doc_text_lower.count("слушател") > 0:
                logger.warning(f"[{self.VERSION}] Ложный ОПР: признаки обучения -> Education")
                return "education", "text_guard_opr_edu", "heuristic"

        # === ШАГ 5: LLM классификация ===
        if llm_classification and llm_confidence >= 0.7:
            normalized = self.normalize(llm_classification)
            logger.info(f"[{self.VERSION}] Тип из LLM классификации: {normalized}")
            return normalized, "llm_classification", "classify"

        # === ШАГ 6: КТРУ-данные ===
        has_rm = bool(tender_info.get("rm_total") and tender_info["rm_total"] > 0)
        has_students = bool(tender_info.get("students_count") and tender_info["students_count"] > 0)
        has_points = bool(tender_info.get("points_count") and tender_info["points_count"] > 0)

        if (has_rm and has_points) or (has_rm and has_students):
             logger.info(f"[{self.VERSION}] Тип из КТРУ: combined (данные)")
             return "combined", "ktru", "data"
        
        if has_rm:
            logger.info(f"[{self.VERSION}] Тип из КТРУ: sout ({tender_info['rm_total']} РМ)")
            return "sout", "ktru", "data"
        if has_points:
             logger.info(f"[{self.VERSION}] Тип из КТРУ: plk ({tender_info['points_count']} точек)")
             return "plk", "ktru", "data"
        if has_students:
            logger.info(f"[{self.VERSION}] Тип из КТРУ: education ({tender_info['students_count']} слушателей)")
            return "education", "ktru", "data"

        # === ШАГ 7: Эвристика по тексту документов ===
        for ttype, keywords in self.KEYWORDS.items():
            if any(kw in doc_text_lower for kw in keywords):
                if ttype == "education" and ("охрана труда" in doc_text_lower or "охране труда" in doc_text_lower):
                    logger.info(f"[{self.VERSION}] Тип из текста: education (ОТ)")
                    return "education", "text_heuristic", "heuristic"
                
                logger.info(f"[{self.VERSION}] Тип из текста: {ttype}")
                return ttype, "text_heuristic", "heuristic"

        # === ШАГ 8: Fallback ===
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
