"""
core/parsers/type_detector.py
Каскадное определение типа тендера (СОУТ, ОПР, обучение, ПЛК).
Вынесено из detailed_parser.py (v6.8.6-r1).

ИСПРАВЛЕНО (v6.9.2):
- Добавлены ключевые слова: пожарная безопасность, промышленная безопасность,
  обучение рабочих специальностей, ППР, технологические карты, СИЗ
"""

from typing import Optional, Dict, Any, Tuple
from loguru import logger

# Ключевые слова для определения типа
TYPE_KEYWORDS = {
    "sout": [
        "специальная оценка условий труда",
        "специальной оценки условий труда",
        "специальной оценке условий труда",
        "специальную оценку условий труда",
        "соут",
        "оценка условий труда",
        "оценки условий труда",
        "спецоценка",
        "вредные производственные факторы",
        "идентификация потенциально вредных",
        "класс условий труда",
        "классы условий труда",
        "декларация соответствия условий труда",
        "карта соут",
        "карты соут",
        "протоколы измерений",
        "исследования факторов",
        "измерение вредных факторов",
        "замеры вредных факторов",
    ],
    "opr": [
        "оценка профессиональных рисков",
        "опр",
        "профессиональный риск",
        "проф. риск",
        "профриск",
        "проф.риск",
        "декларация о соответствии условий труда",
        "мероприятия по снижению рисков",
        "карта оценки профессиональных рисков",
        "методика оценки профессиональных рисков",
        "идентификация опасностей",
        "анализ рисков",
    ],
    "education": [
        "обучение охране труда",
        "обучение по охране труда",
        "программа обучения",
        "программа повышения квалификации",
        "переподготовка",
        "повышение квалификации",
        "профессиональное обучение",
        "дополнительное образование",
        "слушатели",
        "учебные часы",
        "учебный план",
        "протоколы обучения",
        "удостоверение",
        "инструктаж",
        "стажировка",
        "обучение рабочих",
        "обучение по промышленной безопасности",
        "обучение по пожарной безопасности",
        "обучение по электробезопасности",
        "обучение по газовой безопасности",
        "обучение по высотным работам",
        # === v6.9.2: Добавлено ===
        "обучение рабочих специальностей",
        "обучение рабочих профессий",
        "пожарная безопасность",
        "промышленная безопасность",
        "ппр",
        "технологические карты",
        "санитарно-защитная зона",
        "сиз",
        "средства индивидуальной защиты",
    ],
    "plk": [
        "производственный контроль",
        "производственного контроля",
        "производственному контролю",
        "плк",
        "лабораторные исследования",
        "лабораторный контроль",
        "замеры шума",
        "замеры вибрации",
        "замеры микроклимата",
        "замеры освещенности",
        "замеры электромагнитных полей",
        "анализ воздуха рабочей зоны",
        "санитарно-гигиенические исследования",
        "гигиеническая оценка",
        "санитарно-эпидемиологическая",
        "испытания факторов производственной среды",
    ],
}

# ОКПД2 -> тип
OKPD2_TO_TYPE = {
    "85.42": "education",
    "71.20.11": "plk",
    "71.20.19": "plk",
    "71.20.11.190": "plk",
}


class TypeDetector:
    """Каскадное определение типа тендера."""

    @staticmethod
    def detect_from_title(title: str) -> Tuple[Optional[str], str]:
        """Определяет тип по названию тендера."""
        if not title:
            return None, "empty_title"

        title_lower = title.lower()
        for ttype, keywords in TYPE_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in title_lower:
                    return ttype, f"title_keyword:{keyword}"
                base = keyword.lower().replace("ая ", "а ").replace("ой ", "о ")
                if base in title_lower:
                    return ttype, f"title_keyword_stem:{keyword}"

        return None, "undetermined"

    @staticmethod
    def detect_from_okpd2(okpd2_list: list) -> Tuple[Optional[str], str]:
        """Определяет тип по ОКПД2."""
        if not okpd2_list:
            return None, "no_okpd2"

        for okpd in okpd2_list:
            for pattern, ttype in OKPD2_TO_TYPE.items():
                if okpd.startswith(pattern):
                    logger.info(f"[TypeDetector] ОКПД2 {okpd} -> {ttype}")
                    return ttype, "okpd2"

        return None, "no_match"

    @staticmethod
    def cascade_detect(
        title: str = "",
        lot_object: str = "",
        okpd2_list: list = None,
        common_info_object: str = "",
    ) -> Tuple[Optional[str], str]:
        """
        Каскадное определение типа.
        Приоритет: ОКПД2 > lot_object > common_info_object > title
        """
        text_sources = []

        if lot_object:
            text_sources.append((lot_object, "lot_object"))
        if common_info_object:
            text_sources.append((common_info_object, "common_info_object"))
        if title:
            text_sources.append((title, "title"))

        # Уровень 1: ОКПД2 (наивысший приоритет)
        if okpd2_list:
            ttype, source = TypeDetector.detect_from_okpd2(okpd2_list)
            if ttype:
                return ttype, source

        # Уровень 2: Ключевые слова в текстовых источниках
        for text, source in text_sources:
            text_lower = text.lower()
            for ttype, keywords in TYPE_KEYWORDS.items():
                for keyword in keywords:
                    if keyword.lower() in text_lower:
                        return ttype, f"keyword:{source}"

        return None, "undetermined"
