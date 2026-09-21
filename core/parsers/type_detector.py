"""
core/parsers/type_detector.py
Каскадное определение типа тендера (СОУТ, ОПР, обучение, ПЛК).
Вынесено из detailed_parser.py (v6.8.6-r1).

ИСПРАВЛЕНО (v6.9.3):
- Использование boundaries (re.search \b) для предотвращения ложных срабатываний
  на короткие аббревиатуры (СИЗ, ППР, ОПР, ПЛК).
- Уточнены контексты для "пожарная безопасность", "промышленная безопасность" и СИЗ.
"""

import re
from typing import Optional, Dict, Any, Tuple
from loguru import logger

# Ключевые слова и регулярные выражения по типам
TYPE_KEYWORDS = {
    "sout": [
        r"\bсоут\b",
        r"специальн\w+\s+оценк\w+\s+условий\s+труда",
        r"оценк\w+\s+условий\s+труда",
        r"\bспецоценк\w*",
        r"вредные\s+производственные\s+факторы",
        r"идентификация\s+потенциально\s+вредных",
        r"класс\w*\s+условий\s+труда",
        r"декларация\s+соответствия\s+условий\s+труда",
        r"карт\w*\s+соут",
        r"протокол\w*\s+измерений",
        r"измерени\w*\s+вредных\s+факторов",
        r"замер\w*\s+вредных\s+факторов",
    ],
    "opr": [
        r"оценк\w+\s+профессиональных\s+рисков",
        r"\boпр\b",
        r"профессиональн\w+\s+риск\w*",
        r"проф\.?\s*риск\w*",
        r"мероприятия\s+по\s+снижению\s+рисков",
        r"карт\w*\s+оценки\s+профессиональных\s+рисков",
        r"идентификация\s+опасностей",
        r"анализ\s+рисков",
    ],
    "education": [
        r"обучени\w*",
        r"программ\w*\s+обучения",
        r"программ\w*\s+повышения\s+квалификации",
        r"переподготовк\w*",
        r"повышение\w*\s+квалификации",
        r"профессиональное\s+обучение",
        r"дополнительное\s+образование",
        r"слушател\w*",
        r"учебные\s+часы",
        r"учебный\s+план",
        r"удостоверен\w*",
        r"инструктаж\w*",
        r"стажировк\w*",
        r"обучение\s+по\s+промышленной\s+безопасности",
        r"обучение\s+по\s+пожарной\s+безопасности",
        r"обучение\s+по\s+электробезопасности",
        r"обучение\s+по\s+высотным\s+работам",
        r"обучение\s+рабочих\s+профессий",
        # Контекстное обучение для СИЗ и ППР
        r"обучение\s+применению\s+сиз",
        r"обучение\s+по\s+сиз",
        r"разработка\s+ппр",
    ],
    "plk": [
        r"производственн\w+\s+контрол\w*",
        r"\bплк\b",
        r"лабораторн\w+\s+исследования",
        r"лабораторный\s+контроль",
        r"замер\w*\s+шума",
        r"замер\w*\s+вибрации",
        r"замер\w*\s+микроклимата",
        r"замер\w*\s+освещенности",
        r"замер\w*\s+электромагнитных\s+полей",
        r"анализ\s+воздуха\s+рабочей\s+зоны",
        r"санитарно-гигиенические\s+исследования",
        r"гигиеническая\s+оценка",
        r"испытания\s+факторов\s+производственной\s+среды",
    ],
}

# Компиляция регулярных выражений для производительности
COMPILED_PATTERNS = {
    ttype: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for ttype, patterns in TYPE_KEYWORDS.items()
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
    def detect_from_text(text: str) -> Tuple[Optional[str], str]:
        """Определяет тип по тексту (наименование, объект закупки) с использованием Regex."""
        if not text:
            return None, "empty_text"

        for ttype, patterns in COMPILED_PATTERNS.items():
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    return ttype, f"matched_pattern:{match.group(0)}"

        return None, "undetermined"

    @staticmethod
    def detect_from_okpd2(okpd2_list: list) -> Tuple[Optional[str], str]:
        """Определяет тип по ОКПД2."""
        if not okpd2_list:
            return None, "no_okpd2"

        for okpd in okpd2_list:
            if not isinstance(okpd, str):
                continue
            for pattern, ttype in OKPD2_TO_TYPE.items():
                if okpd.startswith(pattern):
                    logger.info(f"[TypeDetector] ОКПД2 {okpd} -> {ttype}")
                    return ttype, f"okpd2:{pattern}"

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
        # Уровень 1: ОКПД2 (наивысший приоритет)
        if okpd2_list:
            ttype, source = TypeDetector.detect_from_okpd2(okpd2_list)
            if ttype:
                return ttype, source

        # Уровень 2: Текстовые источники
        text_sources = [
            (lot_object, "lot_object"),
            (common_info_object, "common_info_object"),
            (title, "title"),
        ]

        for text, source in text_sources:
            if not text:
                continue
            ttype, match_info = TypeDetector.detect_from_text(text)
            if ttype:
                return ttype, f"{source}:{match_info}"

        return None, "undetermined"
