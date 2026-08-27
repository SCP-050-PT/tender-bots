"""
core/analysis/guard_engine.py
Guard'ы для валидации и коррекции данных тендера.
Вынесено из analyzer.py (v6.8.6-r3).

ИСПРАВЛЕНО (v6.9.2):
- FIX: Guard 4 фантомных students_count теперь проверяет source, не только confidence
  - Если source == "ktru" — доверять даже при confidence=0
  - Если source == "llm" и confidence < 0.5 — обнулять
- Добавлено поле extraction_source в tender_info
"""

from typing import Dict, Any, List, Tuple
from loguru import logger


class GuardEngine:
    """
    Применяет guard'ы для исправления противоречивых данных.

    Guard'ы предотвращают ошибки при смешанных типах тендеров
    (например, СОУТ с students_count или обучение с rm_total).
    """

    VERSION = "v6.9.2"

    # Пороги
    MAX_STUDENTS_CONFIDENCE = 500
    MIN_CONFIDENCE_THRESHOLD = 0.5
    OPR_RM_THRESHOLD = 200

    def apply(
        self, tender_info: Dict[str, Any], tender_type: str
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        Применяет guard'ы к данным тендера.

        Args:
            tender_info: Словарь с параметрами тендера
            tender_type: Определённый тип тендера

        Returns:
            (modified_info, guards_triggered)
        """
        guards = []
        info = dict(tender_info)  # Копия, чтобы не мутировать оригинал

        # Guard 1: СОУТ/ОПР/ПЛК не имеют слушателей
        if tender_type in ("sout", "opr", "plk"):
            if info.get("students_count") and info["students_count"] > 0:
                old = info["students_count"]
                info["students_count"] = 0
                guards.append(f"students_count={old} при типе={tender_type} -> 0")
                logger.warning(
                    f"[{self.VERSION}] GUARD: students_count={old} обнулён при {tender_type}"
                )

        # Guard 2: Обучение не имеет рабочих мест
        if tender_type == "education":
            if info.get("rm_total") and info["rm_total"] > 0:
                old = info["rm_total"]
                info["rm_total"] = 0
                guards.append(f"rm_total={old} при типе=education -> 0")
                logger.warning(
                    f"[{self.VERSION}] GUARD: rm_total={old} обнулён при education"
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

        # Guard 4: Фантомные students_count при низком confidence
        # v6.9.2 FIX: Проверяем source, не только confidence
        if (
            info.get("students_count")
            and info["students_count"] > self.MAX_STUDENTS_CONFIDENCE
        ):
            source = info.get("students_count_source", "")
            confidence = info.get("extraction_confidence", 0)

            # v6.9.2: Если извлечено из КТРУ — доверять даже при confidence=0
            if source == "ktru":
                logger.info(
                    f"[{self.VERSION}] GUARD: students_count={info['students_count']} "
                    f"из КТРУ (source=ktru) — оставляем"
                )
            elif confidence < self.MIN_CONFIDENCE_THRESHOLD:
                old = info["students_count"]
                info["students_count"] = 0
                guards.append(
                    f"students_count={old} фантом (confidence={confidence}, source={source}) -> 0"
                )
                logger.warning(
                    f"[{self.VERSION}] GUARD: Фантомные students_count={old} "
                    f"при confidence={confidence} (source={source}) -> обнулены"
                )

        # Guard 4: Запрещённые направления (не наш профиль)
        FORBIDDEN_KEYWORDS = [
            # Поставка СИЗ без услуг (маржа плохая)
            "поставка сиз",
            "поставка средств индивидуальной защиты",
            "поставка спецодежды",
            "поставка обуви",
            "поставка касок",
            "поставка перчаток",
            "поставка аптечек",
            "поставка огнетушителей",
            "поставка знаков безопасности",
            # Запрещённые направления из ТЗ Александры
            "лицензия мчс",
            "экспертиза промышленной безопасности",
            "обслуживание оборудования",
            "ремонт оборудования",
            "медицинские работники",
            "медицинский персонал",
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
            # СЗЗ (не наш профиль)
            "сзз",
            "санитарно-защитная зона",
            "проект сзз",
        ]

        purchase_name = info.get("purchase_name", "").lower()
        for kw in FORBIDDEN_KEYWORDS:
            if kw in purchase_name:
                guards.append(f"Запрещённое направление: '{kw}' в названии")
                logger.warning(
                    f"[{self.VERSION}] GUARD: Запрещённое направление '{kw}' → не участвуем"
                )
                info["_forbidden_direction"] = True
                break

        # Guard 6: Признаки договорняка
        SUSPICIOUS_PATTERNS = [
            # НМЦК совпадает с ценой единственного поставщика
            # (проверяется в main.py, не здесь)
            # Слишком короткий срок подачи (< 3 дней для 44-ФЗ)
            # (проверяется в main.py)
            # Текст ТЗ содержит конкретное название бренда/модели
            "торговая марка",
            "товарный знак",
            "конкретный производитель",
            "единственный поставщик",
        ]

        purchase_name = info.get("purchase_name", "").lower()
        documents_text = info.get("documents_text", "").lower()[:5000]

        for pattern in SUSPICIOUS_PATTERNS:
            if pattern in documents_text:
                guards.append(f"Подозрение на договорняк: '{pattern}' в ТЗ")
                logger.warning(
                    f"[{self.VERSION}] GUARD: Подозрение на договорняк — '{pattern}' в ТЗ"
                )
                break
        return info, guards
