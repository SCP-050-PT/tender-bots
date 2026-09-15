"""
core/analysis/guard_engine.py
Guard'ы для валидации и коррекции данных тендера.
Вынесено из analyzer.py (v6.8.6-r3).

ИСПРАВЛЕНО (v7.8.1):
- FIX: Guard 5 (Запрещенные направления) теперь учитывает тип тендера.
  - Если тип 'education', игнорируем общие триггеры ПБ (обучение ПБ - это наш профиль).
  - Для СОУТ/ОПР/ПЛК проверяем 'пожарная безопасность' только в названии, а не в тексте ТЗ (чтобы не блокировать из-за ссылок на нормы).
  - Добавлены специфичные триггеры ПБ/Сантехники для проверки в тексте.
"""

from typing import Dict, Any, List, Tuple
from loguru import logger


class GuardEngine:
    """
    Применяет guard'ы для исправления противоречивых данных.
    """

    VERSION = "v7.8.1"

    MAX_STUDENTS_CONFIDENCE = 500
    MIN_CONFIDENCE_THRESHOLD = 0.5
    OPR_RM_THRESHOLD = 200

    def apply(
        self, tender_info: Dict[str, Any], tender_type: str
    ) -> Tuple[Dict[str, Any], List[str]]:
        guards = []
        info = dict(tender_info)

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
        # Разделяем ключевые слова на "строгие" (блокируем везде) и "контекстные" (блокируем только в названии или если не обучение)

        # 1. Строгие триггеры (Поставка, Сантехника, Специфичные услуги ПБ) - блокируем всегда, даже в обучении (если это поставка огнетушителей)
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
        ]

        # 2. Контекстные триггеры (блокируем только если это НЕ обучение и встречается в НАЗВАНИИ)
        # "пожарная безопасность" в тексте ТЗ по СОУТ - это норма. В названии "Услуги по пожарной безопасности" - это не наш профиль (если не обучение).
        CONTEXT_FORBIDDEN_TITLE_ONLY = [
            "пожарная безопасность",
            "медицинские работники",
            "медицинский персонал",
        ]

        purchase_name = info.get("purchase_name", "").lower()
        documents_text_lower = info.get("documents_text", "").lower()[:2000]

        is_forbidden = False
        forbidden_kw_found = ""

        # Проверка строгих триггеров (в названии ИЛИ в тексте)
        for kw in STRICT_FORBIDDEN:
            if kw in purchase_name or kw in documents_text_lower:
                is_forbidden = True
                forbidden_kw_found = kw
                break

        # Проверка контекстных триггеров (только в названии, и только если это НЕ обучение)
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
            info["review_reason"] = f"Не профильное направление: {forbidden_kw_found}"

        # Guard 6: Признаки договорняка
        SUSPICIOUS_PATTERNS = [
            "торговая марка",
            "товарный знак",
            "конкретный производитель",
            "единственный поставщик",
        ]

        # Проверяем только текст документов на договорняк
        for pattern in SUSPICIOUS_PATTERNS:
            if pattern in documents_text_lower:  # Используем уже обрезанный текст
                # Исключение: "единственный поставщик" может быть в разделе "Способ определения поставщика" как описание метода,
                # но если это в требованиях к участнику - плохо. Пока оставим простую проверку, но с логом.
                # Чтобы избежать ложных срабатываний на описание метода закупки, можно проверить контекст, но пока оставим так.
                guards.append(f"Подозрение на договорняк: '{pattern}' в ТЗ")
                logger.warning(
                    f"[{self.VERSION}] GUARD: Подозрение на договорняк — '{pattern}' в ТЗ"
                )
                break

        return info, guards
