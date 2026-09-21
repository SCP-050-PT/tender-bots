"""
core/calculation/education_calculator.py
Расчёт цены для клиента на обучение.
Вынесено из calculator.py (v6.5).

v8.0.0-Optimized:
  - Вынесены константы ключевых слов и скомпилированные regex.
  - Декомпозиция авто-определения параметров (метод _auto_detect_params).
  - Улучшена читаемость и поддержка кода.
"""

import re
from typing import Optional, Tuple, List
from loguru import logger

from core.calculation.cost_loader import load_costs
from core.calculation.calculation_result import CalculationResult


class EducationCalculator:
    """Расчёт цены для клиента на обучение."""

    # Офисные города — аренда не нужна
    OFFICE_CITIES = {"екатеринбург", "ижевск", "тюмень", "новосибирск"}

    # Регулярные выражения для fallback-поиска слушателей
    STUDENTS_PATTERNS = [
        re.compile(
            r"(\d+)\s*(?:человек|чел\.|обучающихся|слушателей|работников)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:количество|кол-во)\s*(?:обучающихся|слушателей)?\s*[:\-]\s*(\d+)",
            re.IGNORECASE,
        ),
    ]

    # Ключевые слова
    OT_KEYWORDS = [
        "охрана труда",
        "обучение по охране труда",
        "обучение работников вопросам охраны труда",
        "проверка знаний по охране труда",
        "инструктаж по охране труда",
    ]

    DISTANCE_KEYWORDS = [
        "заочная",
        "заочной",
        "заочное",
        "дистанц",
        "электронн",
        "онлайн",
        "distance",
        "remote",
        "рабочее место обучающегося",
    ]

    ONSITE_KEYWORDS = [
        "очно",
        "очная форма",
        "полигон",
        "выездное обучение",
        "практическое занятие",
        "тренировочный полигон",
        "аудиторные занятия",
    ]

    SAFETY_KEYWORDS = [
        "высота",
        "газоопасные",
        "газ",
        "промбезопасность",
        "энергобезопасность",
        "электробезопасность",
    ]

    RETRAINING_KEYWORDS = [
        "переподготовка",
        "профпереподготовка",
        "профессиональная переподготовка",
        "пожарная безопасность",
        "пожарной безопасности",
    ]

    QUALIFICATION_KEYWORDS = ["повышение квалификации", "квалификация"]

    def __init__(self) -> None:
        self.costs = load_costs()["education"]
        self.docs = self.costs["documents"]
        self.materials = self.costs["materials"]
        self.labor = self.costs["labor"]
        self.delivery = self.costs["delivery"]
        self.overhead = self.costs["overhead"]
        self.forms = self.costs["forms"]
        self.rates = self.costs["rates"]

    def _parse_students_fallback(self, tender_text: str) -> int:
        """Fallback поиск количества слушателей в тексте ТЗ."""
        found_counts: List[int] = []
        text_lower = tender_text.lower()

        for pattern in self.STUDENTS_PATTERNS:
            for match in pattern.finditer(text_lower):
                val_str = match.group(1)
                try:
                    val = int(val_str)
                    if 1 < val < 1000:
                        found_counts.append(val)
                except ValueError:
                    continue

        if found_counts:
            total = sum(found_counts)
            logger.info(
                f"[EducationCalc v8.0.0] Fallback: найдено кол-во слушателей в тексте: "
                f"{found_counts} → итого students_count={total}"
            )
            return total
        return 0

    def calculate(
        self,
        students_count: int,
        certificates: int = 0,
        diplomas: int = 0,
        worker_certs: int = 0,
        qual_certs: int = 0,
        protocols_count: int = 0,
        is_distance: bool = False,
        days_full_time: int = 0,
        transport_km: float = 0,
        accommodation_nights: int = 0,
        teacher_days: int = 0,
        teacher_rate: float = 0,
        manikin_days: int = 0,
        venue_days: int = 0,
        delivery_count: int = 1,
        needs_manual_review: bool = False,
        review_reason: str = "",
        llm_confidence: float = 0.0,
        tender_text: str = "",
        region: str = "",
        is_annual: bool = False,
    ) -> CalculationResult:
        """Расчёт цены для клиента на обучение."""
        annual_mult = 12 if is_annual else 1

        # === Fallback поиска количества слушателей ===
        if students_count == 0 and tender_text:
            students_count = self._parse_students_fallback(tender_text)

        # === Guard для обучения ОТ ===
        if students_count > 0 and tender_text:
            text_lower = tender_text.lower()
            is_ot = any(kw in text_lower for kw in self.OT_KEYWORDS)
            if (
                is_ot
                and protocols_count == 0
                and (qual_certs + diplomas + certificates + worker_certs) > 0
            ):
                logger.warning(
                    "[EducationCalc] Обнаружен ОТ, но LLM дал другие документы. "
                    f"Принудительно protocols={students_count}"
                )
                protocols_count = students_count
                qual_certs = diplomas = certificates = worker_certs = 0

        # === Авто-определение типа документов ===
        auto = False
        total_explicit_docs = (
            certificates + diplomas + worker_certs + qual_certs + protocols_count
        )

        if total_explicit_docs == 0 and students_count > 0:
            auto = True
            if llm_confidence >= 0.5:
                logger.info(
                    f"[EducationCalc] Авто-определение при llm_confidence={llm_confidence:.2f} >= 0.5"
                )
                needs_manual_review = False
            else:
                needs_manual_review = True
                review_reason = "Авто-определение типа документов при низком confidence. Требуется ручная проверка ТЗ."

            if tender_text:
                text_lower = tender_text.lower()
                has_certificate_kw = (
                    "удостоверение" in text_lower or "удостоверения" in text_lower
                )

                if any(kw in text_lower for kw in self.SAFETY_KEYWORDS):
                    certificates = students_count
                    logger.info(
                        f"[EducationCalc] Авто: высота/газ/промбезопасность → certificates={students_count}"
                    )
                elif any(kw in text_lower for kw in self.OT_KEYWORDS):
                    protocols_count = students_count
                    logger.info(
                        f"[EducationCalc] Авто: ОТ → protocols_count={students_count}"
                    )

                    has_distance_kw = any(
                        kw in text_lower for kw in self.DISTANCE_KEYWORDS
                    )
                    has_onsite_kw = any(kw in text_lower for kw in self.ONSITE_KEYWORDS)
                    if has_distance_kw or not has_onsite_kw:
                        is_distance = True
                        logger.info(
                            "[EducationCalc] Авто: ОТ без очных маркеров → is_distance=True"
                        )
                elif any(kw in text_lower for kw in self.RETRAINING_KEYWORDS):
                    if has_certificate_kw:
                        certificates = students_count
                        logger.info(
                            f"[EducationCalc] Авто: пожарка/переподготовка + удостоверение → certificates={students_count}"
                        )
                    else:
                        diplomas = students_count
                        logger.info(
                            f"[EducationCalc] Авто: переподготовка/пожарка → diplomas={students_count}"
                        )
                elif any(kw in text_lower for kw in self.QUALIFICATION_KEYWORDS):
                    qual_certs = students_count
                    logger.info(
                        f"[EducationCalc] Авто: повышение квалификации → qual_certs={students_count}"
                    )
                else:
                    protocols_count = students_count
                    logger.info(
                        f"[EducationCalc] Авто: тип неясен → protocols_count={students_count}"
                    )
            else:
                protocols_count = students_count
                logger.info(
                    f"[EducationCalc] Авто: нет текста → protocols_count={students_count}"
                )

        # === Авто-дистант для всех типов обучения ===
        if not is_distance and tender_text:
            text_lower = tender_text.lower()
            has_distance_kw = any(kw in text_lower for kw in self.DISTANCE_KEYWORDS)
            has_onsite_kw = any(kw in text_lower for kw in self.ONSITE_KEYWORDS)

            if has_distance_kw and not has_onsite_kw:
                is_distance = True
                logger.info(
                    "[EducationCalc] Авто: обнаружены маркеры дистанта → is_distance=True"
                )
            elif has_distance_kw and has_onsite_kw:
                logger.warning(
                    "[EducationCalc] Смешанная форма (очно+дистант). Оставляем is_distance=False."
                )

        logger.info(
            f"[EducationCalc] ВХОД: students={students_count}, certs={certificates}, "
            f"diplomas={diplomas}, worker_certs={worker_certs}, qual_certs={qual_certs}, "
            f"protocols={protocols_count}, is_distance={is_distance}, region={region}"
        )

        # === Расчет стоимости документов ===
        docs_cost = (
            certificates * self.docs["certificate"]["cost"]
            + diplomas * self.docs["diploma"]["cost"]
            + worker_certs * self.docs["certificate_worker"]["cost"]
            + qual_certs * self.docs["certificate_qualification"]["cost"]
            + protocols_count * self.docs["protocol"]["cost"]
        ) * annual_mult

        # === Материалы ===
        total_docs = (
            certificates + diplomas + worker_certs + qual_certs + protocols_count
        )
        paper_cost = total_docs * self.materials["paper_a4"]["cost"] * annual_mult
        ink_cost = total_docs * self.materials["ink_per_page"]["cost"] * annual_mult
        lamination_cost = (
            certificates * self.materials["lamination"]["cost"] * annual_mult
        )
        materials_cost = paper_cost + ink_cost + lamination_cost

        # === Доставка и накладные ===
        actual_delivery = 12 if is_annual else delivery_count
        delivery_cost = actual_delivery * self.delivery["post_russia"]["cost"]
        overhead_cost = self.overhead["base"]["cost"] * annual_mult

        # === Трудозатраты ===
        specialist_cost = 3 * 100 * annual_mult
        methodist_hours = 3
        methodist_rate = self.labor["methodist_hour"]["cost"]
        methodist_cost = methodist_hours * methodist_rate * annual_mult

        ro_hours = 3
        ro_rate = self.labor["ro_hour"]["cost"]
        ro_cost = ro_hours * ro_rate * annual_mult

        portal_cost = self.labor["portal_access"]["cost"] * students_count * annual_mult
        labor_cost = specialist_cost + methodist_cost + ro_cost + portal_cost

        # === Очные затраты ===
        full_time_cost = 0.0
        transport_cost = 0.0
        venue_cost = 0.0
        is_office_city = False

        if not is_distance:
            if teacher_days > 0 and teacher_rate > 0:
                teacher_cost = teacher_days * teacher_rate
            elif teacher_days > 0:
                teacher_cost = teacher_days * self.rates["teacher_daily"]["cost"]
            else:
                teacher_days = max(1, (students_count + 24) // 25)
                teacher_cost = teacher_days * self.rates["teacher_daily"]["cost"]

            if transport_km > 0:
                transport_cost = (
                    transport_km
                    * self.forms["full_time"]["fuel_cost_per_km"]
                    / 100
                    * 11
                )
            else:
                transport_cost = float(self.rates["transport_fixed"]["cost"])

            accommodation_nights_actual = (
                accommodation_nights if accommodation_nights > 0 else teacher_days
            )
            accommodation_cost = (
                accommodation_nights_actual
                * self.forms["full_time"]["accommodation_per_night"]
            )
            daily_allowance_cost = (
                teacher_days * self.forms["full_time"]["daily_allowance"]
            )

            region_lower = region.lower() if region else ""
            is_office_city = any(city in region_lower for city in self.OFFICE_CITIES)

            if is_office_city:
                venue_cost = 0.0
            else:
                venue_days_actual = venue_days if venue_days > 0 else teacher_days
                venue_cost = venue_days_actual * self.rates["venue_daily"]["cost"]

            manikin_cost = (
                manikin_days * self.rates["manikin_daily"]["cost"]
                if manikin_days > 0
                else 0.0
            )

            full_time_cost = (
                teacher_cost
                + transport_cost
                + accommodation_cost
                + daily_allowance_cost
                + venue_cost
                + manikin_cost
            )

        # === Итоговые показатели ===
        cost_price = (
            docs_cost
            + materials_cost
            + delivery_cost
            + overhead_cost
            + labor_cost
            + full_time_cost
        )

        margin_percent = 10.0
        margin_rub = cost_price * 0.1
        recommended_price = cost_price + margin_rub

        min_key = "distance" if is_distance else "full_time"
        minimum = self.costs["minimum_price"].get(min_key, 10000)
        if recommended_price < minimum:
            recommended_price = float(minimum)
            margin_rub = recommended_price - cost_price
            margin_percent = (margin_rub / cost_price) * 100 if cost_price > 0 else 0.0

        return CalculationResult(
            cost_price=cost_price,
            recommended_price=recommended_price,
            margin_percent=margin_percent,
            margin_rub=margin_rub,
            transport_cost=transport_cost,
            subcontractor_cost=0,
            guarantee_cost=0,
            needs_manual_review=needs_manual_review,
            review_reason=review_reason,
            details={
                "type": "education",
                "students_count": students_count,
                "certificates": certificates,
                "diplomas": diplomas,
                "worker_certs": worker_certs,
                "qual_certs": qual_certs,
                "protocols_count": protocols_count,
                "is_distance": is_distance,
                "teacher_days": teacher_days,
                "teacher_rate": teacher_rate,
                "transport_km": transport_km,
                "accommodation_nights": accommodation_nights,
                "venue_days": venue_days,
                "manikin_days": manikin_days,
                "delivery_count": delivery_count,
                "docs_cost": docs_cost,
                "materials_cost": materials_cost,
                "delivery_cost": delivery_cost,
                "overhead_cost": overhead_cost,
                "labor_cost": labor_cost,
                "full_time_cost": full_time_cost,
                "venue_cost": venue_cost,
                "is_office_city": is_office_city if not is_distance else None,
                "auto_detected": auto,
            },
        )
