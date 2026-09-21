"""
core/analysis/guard_engine.py
Guard'ы для валидации и коррекции данных тендера.
Вынесено из analyzer.py.

v8.0.0-Optimized:
- Добавлена интеграция с селективной системой блокировки Агента (передача agent_blocked).
- Оптимизирован анализ текста документов: строгий срез до 2000 символов для быстрого поиска ключевых слов.
- Исключение ложных срабатываний паттерна "единственный поставщик" при описании формы закупки.
"""

from typing import Dict, Any, List, Tuple, Optional
from loguru import logger


class GuardEngine:
    """
    Применяет guard'ы для исправления противоречивых данных и отсева непрофильных закупок.
    """

    VERSION = "v8.0.0-Optimized"

    MAX_STUDENTS_CONFIDENCE = 500
    MIN_CONFIDENCE_THRESHOLD = 0.5
    OPR_RM_THRESHOLD = 200

    def apply(
        self,
        tender_info: Dict[str, Any],
        tender_type: str,
        documents_text: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        Применяет набор защитных правил (guards) к данным тендера.
        """
        guards = []
        info = dict(tender_info)

        # Контекст документов: приоритет у прямо переданного текста, затем из tender_info
        raw_doc_text = documents_text or info.get("documents_text", "")
        documents_text_lower = raw_doc_text.lower()[:2000]

        # Guard 1: СОУТ/ОПР/ПЛК не имеют слушателей
        if tender_type in ("sout", "opr", "plk"):
            if info.get("students_count") and info["students_count"] > 0:
                old = info["students_count"]
                info["students_count"] = 0
                guards.append(f"students_count={old} при типе={tender_type} -> 0")
                logger.warning(
                    f"[{self.VERSION}] GUARD: students_count обнулён при {tender_type}"
                )

        # Guard 2: Обучение не имеет рабочих мест
        if tender_type == "education":
            if info.get("rm_total") and info["rm_total"] > 0:
                old = info["rm_total"]
                info["rm_total"] = 0
                guards.append(f"rm_total={old} при типе=education -> 0")
                logger.warning(
                    f"[{self.VERSION}] GUARD: rm_total обнулён при education"
                )

        # Guard 3: ОПР с rm_total > 200 -> возможно это СОУТ
        if tender_type == "opr":
            rm = info.get("rm_total", 0)
            if rm > self.OPR_RM_THRESHOLD:
                guards.append(
                    f"opr с rm_total={rm} > {self.OPR_RM_THRESHOLD}, возможно СОУТ"
                )
                logger.warning(
                    f"[{self.VERSION}] GUARD: ОПР с {rm} РМ -> проверьте, возможно СОУТ"
                )

        # Guard 4: Фантомные students_count
        if (
            info.get("students_count")
            and info["students_count"] > self.MAX_STUDENTS_CONFIDENCE
        ):
            source = info.get("students_count_source", "")
            confidence = info.get("extraction_confidence", 0)

            if source == "ktru":
                logger.info(
                    f"[{self.VERSION}] GUARD: students_count из КТРУ — оставляем"
                )
            elif confidence < self.MIN_CONFIDENCE_THRESHOLD:
                old = info["students_count"]
                info["students_count"] = 0
                guards.append(f"students_count={old} фантом -> 0")
                logger.warning(
                    f"[{self.VERSION}] GUARD: Фантомные students_count обнулены"
                )

        # Guard 5: Запрещённые направления (умная проверка)
        STRICT_FORBIDDEN = [
            "поставка сиз",
            "поставка средств индивидуальной защиты",
            "поставка спецодежды",
            "поставка обуви",
            "поставка касок",
            "поставка перчаток",
            "поставка аптечек",
            "поставка огнетушителей",
            "поставка знаков безопасности",
            "пожарных рукавов",
            "перекатка пожарных",
            "пожарных кранов",
            "внутреннего водопровода",
            "внутреннего противопожарного",
            "гидрант",
            "водоотдача",
            "испытание пожарных",
            "проверка работоспособности внутреннего",
            "монтаж пожарной",
            "техническое обслуживание пожарной",
            "лицензия мчс",
            "экспертиза промышленной безопасности",
            "обслуживание оборудования",
            "ремонт оборудования",
            "информационная безопасность",
            "водительских прав",
            "гражданская оборона",
            "охранники с оружием",
            "лицензия фсб",
            "государственная тайна",
            "гостайна",
            "исследования по воде",
            "смывы",
            "яйца гельминтов",
            "биология",
            "сзз",
            "санитарно-защитная зона",
            "проект сзз",
            "дератизация",
            "дезинсекция",
            "дезинфекция",
            "строительный контроль",
            "обследование зданий",
        ]

        CONTEXT_FORBIDDEN_TITLE_ONLY = [
            "пожарная безопасность",
            "медицинские работники",
            "медицинский персонал",
        ]

        purchase_name = info.get("purchase_name", "").lower()

        is_forbidden = False
        forbidden_kw_found = ""

        # 1. Проверка строгих триггеров
        for kw in STRICT_FORBIDDEN:
            if kw in purchase_name or kw in documents_text_lower:
                is_forbidden = True
                forbidden_kw_found = kw
                break

        # 2. Проверка контекстных триггеров (в названии, только если не обучение)
        if not is_forbidden and tender_type != "education":
            for kw in CONTEXT_FORBIDDEN_TITLE_ONLY:
                if kw in purchase_name:
                    is_forbidden = True
                    forbidden_kw_found = kw
                    break

        if is_forbidden:
            guards.append(f"Запрещённое направление: '{forbidden_kw_found}'")
            logger.warning(
                f"[{self.VERSION}] GUARD: Запрещённое направление '{forbidden_kw_found}' → не участвуем"
            )
            info["_forbidden_direction"] = True
            info["agent_blocked"] = True
            info["agent_block_reason"] = (
                f"Запрещённое направление: {forbidden_kw_found}"
            )
            info["review_reason"] = f"Непрофильное направление: {forbidden_kw_found}"

        # Guard 6: Признаки ограничений конкуренции / договорняка
        SUSPICIOUS_PATTERNS = [
            "торговая марка",
            "товарный знак",
            "конкретный производитель",
        ]

        for pattern in SUSPICIOUS_PATTERNS:
            if pattern in documents_text_lower:
                guards.append(
                    f"Подозрение на ограничение конкуренции: '{pattern}' в ТЗ"
                )
                logger.warning(
                    f"[{self.VERSION}] GUARD: Подозрение на договорняк — '{pattern}' в ТЗ"
                )
                break

        return info, guards
