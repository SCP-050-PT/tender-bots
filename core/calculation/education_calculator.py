"""
core/calculation/education_calculator.py
Расчёт цены для клиента на обучение.
Вынесено из calculator.py (v6.5).
ИСПРАВЛЕНО (v6.9.0):
  - Аренда помещения = 0 в офисных городах (Екатеринбург, Ижевск, Тюмень, Новосибирск)
  - Добавлен параметр region
"""

from dataclasses import dataclass
from typing import Optional
from loguru import logger

from core.calculation.cost_loader import load_costs
from core.calculation.calculation_result import CalculationResult


class EducationCalculator:
    """Расчёт цены для клиента на обучение."""

    # Офисные города — аренда не нужна
    OFFICE_CITIES = ["екатеринбург", "ижевск", "тюмень", "новосибирск"]

    def __init__(self):
        self.costs = load_costs()["education"]
        self.docs = self.costs["documents"]
        self.materials = self.costs["materials"]
        self.labor = self.costs["labor"]
        self.delivery = self.costs["delivery"]
        self.overhead = self.costs["overhead"]
        self.forms = self.costs["forms"]
        self.rates = self.costs["rates"]

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
        """
        Расчёт цены для клиента на обучение.
        v6.9.0: Аренда = 0 в офисных городах.
        v6.9.1: Годовые тендеры — документы, материалы, труд, доставка ×12
        """

        # v6.9.1: Годовой множитель
        annual_mult = 12 if is_annual else 1
        # === Guard для обучения ОТ ===
        if students_count > 0 and tender_text:
            text_lower = tender_text.lower()
            ot_keywords = [
                "охрана труда",
                "обучение по охране труда",
                "обучение работников вопросам охраны труда",
                "проверка знаний по охране труда",
                "инструктаж по охране труда",
            ]
            is_ot = any(kw in text_lower for kw in ot_keywords)
            if (
                is_ot
                and protocols_count == 0
                and (qual_certs + diplomas + certificates + worker_certs) > 0
            ):
                logger.warning(
                    f"[EducationCalc v6.9.0] Обнаружен ОТ, но LLM дал другие документы. "
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
                    f"[EducationCalc v6.9.0] Авто-определение при "
                    f"llm_confidence={llm_confidence:.2f} >= 0.5 — review НЕ требуется"
                )
                needs_manual_review = False
            else:
                needs_manual_review = True
                review_reason = (
                    "Авто-определение типа документов при низком confidence. "
                    "Требуется ручная проверка ТЗ."
                )

            if tender_text:
                text_lower = tender_text.lower()
                if any(
                    kw in text_lower
                    for kw in [
                        "высота",
                        "газоопасные",
                        "газ",
                        "промбезопасность",
                        "энергобезопасность",
                        "электробезопасность",
                    ]
                ):
                    certificates = students_count
                    logger.info(
                        f"[EducationCalc] Авто: высота/газ → certificates={students_count}"
                    )
                elif any(
                    kw in text_lower
                    for kw in [
                        "охрана труда",
                        "обучение по охране труда",
                        "проверка знаний по охране труда",
                    ]
                ):
                    protocols_count = students_count
                    logger.info(
                        f"[EducationCalc] Авто: ОТ → protocols_count={students_count}"
                    )

                    # v7.2.4: ПРИНУДИТЕЛЬНЫЙ ДИСТАНТ ДЛЯ ОТ
                    # Если в ТЗ есть "дистанц" или нет слов "очно/полигон" -> это дистант
                    # Источник: "расчет_цен_для_участия_в_тендерах.docx" п.1.3
                    has_distance_kw = any(kw in text_lower for kw in [
                        "дистанц", "электронн", "онлайн", "distance", "remote"
                    ])
                    has_onsite_kw = any(kw in text_lower for kw in [
                        "очно", "очная форма", "полигон", "выездное обучение", 
                        "практическое занятие", "тренировочный полигон"
                    ])

                    # Если явно указан дистант ИЛИ не указана очка -> считаем дистантом
                    if has_distance_kw or not has_onsite_kw:
                        is_distance = True
                        logger.info(
                            f"[EducationCalc v7.2.4] Авто: ОТ без 'очно' → "
                            f"is_distance=True (принудительный дистант)"
                        )
                elif any(
                    kw in text_lower
                    for kw in [
                        "переподготовка",
                        "профпереподготовка",
                        "профессиональная переподготовка",
                    ]
                ):
                    diplomas = students_count
                    logger.info(
                        f"[EducationCalc] Авто: переподготовка → diplomas={students_count}"
                    )
                elif any(
                    kw in text_lower
                    for kw in ["повышение квалификации", "квалификация"]
                ):
                    qual_certs = students_count
                    logger.info(
                        f"[EducationCalc] Авто: повышение квалификации → qual_certs={students_count}"
                    )
                else:
                    protocols_count = students_count
                    logger.info(
                        f"[EducationCalc] Авто: тип неясен → protocols_count={students_count} (предполагаем ОТ)"
                    )
            else:
                protocols_count = students_count
                logger.info(
                    f"[EducationCalc] Авто: нет текста → protocols_count={students_count}"
                )

        logger.info(
            f"[EducationCalc] ВХОД: students={students_count}, certs={certificates}, "
            f"diplomas={diplomas}, worker_certs={worker_certs}, qual_certs={qual_certs}, "
            f"protocols={protocols_count}, is_distance={is_distance}, "
            f"teacher_days={teacher_days}, acc_nights={accommodation_nights}, "
            f"transport_km={transport_km}, venue_days={venue_days}, "
            f"manikin_days={manikin_days}, delivery={delivery_count}, "
            f"auto={auto}, needs_review={needs_manual_review}, "
            f"llm_confidence={llm_confidence:.2f}, region={region}"
        )

        # === Документы ===
        docs_cost = (
            certificates * self.docs["certificate"]["cost"]
            + diplomas * self.docs["diploma"]["cost"]
            + worker_certs * self.docs["certificate_worker"]["cost"]
            + qual_certs * self.docs["certificate_qualification"]["cost"]
            + protocols_count * self.docs["protocol"]["cost"]
        ) * annual_mult

        # Материалы
        total_docs = (
            certificates + diplomas + worker_certs + qual_certs + protocols_count
        )
        paper_cost = total_docs * self.materials["paper_a4"]["cost"] * annual_mult
        ink_cost = total_docs * self.materials["ink_per_page"]["cost"] * annual_mult
        lamination_cost = (
            certificates * self.materials["lamination"]["cost"] * annual_mult
        )
        materials_cost = paper_cost + ink_cost + lamination_cost

        # Доставка
        actual_delivery = 12 if is_annual else delivery_count
        delivery_cost = actual_delivery * self.delivery["post_russia"]["cost"]

        # Накладные
        overhead_cost = self.overhead["base"]["cost"] * annual_mult

        # === Трудозатраты (v7.2.3: по калькулятору Александры) ===
        # Нагрузка тендерного специалиста (все типы) — из "для бота тенедры.docx"
        specialist_cost = 3 * 100 * annual_mult  # 3ч × 100₽ = 300₽

        # Часы методиста (только обучение) — из Калькулятор тендеры.xlsx
        # "В формуле учитывается 3 часа работы методиста и РО"
        methodist_hours = 3
        methodist_rate = self.labor["methodist_hour"]["cost"]  # 227₽
        methodist_cost = methodist_hours * methodist_rate * annual_mult  # 681₽

        # Часы РО (только обучение) — из Калькулятор тендеры.xlsx
        ro_hours = 3
        ro_rate = self.labor["ro_hour"]["cost"]  # 600₽
        ro_cost = ro_hours * ro_rate * annual_mult  # 1800₽

        # Портал — за каждого слушателя
        portal_cost = (
            self.labor["portal_access"]["cost"] * students_count * annual_mult
        )  # 57₽ × N

        labor_cost = specialist_cost + methodist_cost + ro_cost + portal_cost

        logger.info(
            f"[EducationCalc v7.2.3] Трудозатраты: "
            f"специалист={specialist_cost}₽, "
            f"методист={methodist_cost}₽ ({methodist_hours}ч×{methodist_rate}₽), "
            f"РО={ro_cost}₽ ({ro_hours}ч×{ro_rate}₽), "
            f"портал={portal_cost}₽ ({students_count}×{self.labor['portal_access']['cost']}₽), "
            f"итого={labor_cost}₽"
        )

        # === Очные затраты ===
        full_time_cost = 0
        transport_cost = 0
        accommodation_cost = 0
        daily_allowance_cost = 0
        venue_cost = 0
        manikin_cost = 0

        if not is_distance:
            # Преподаватель
            if teacher_days > 0 and teacher_rate > 0:
                teacher_cost = teacher_days * teacher_rate
            elif teacher_days > 0:
                teacher_cost = teacher_days * self.rates["teacher_daily"]["cost"]
            else:
                auto_teacher_days = max(1, (students_count + 24) // 25)
                teacher_cost = auto_teacher_days * self.rates["teacher_daily"]["cost"]
                teacher_days = auto_teacher_days
                logger.info(
                    f"[Education] Авто-оценка teacher_days={teacher_days} ({students_count} слуш.)"
                )

            # Транспорт
            if transport_km > 0:
                fuel_cost = (
                    transport_km
                    * self.forms["full_time"]["fuel_cost_per_km"]
                    / 100
                    * 11
                )
                transport_cost = fuel_cost
            else:
                transport_cost = self.rates["transport_fixed"]["cost"]
                logger.info(
                    f"[EducationCalc] Транспорт fallback: {transport_cost:,.0f}₽ (km=0)"
                )

            # Проживание
            if accommodation_nights > 0:
                accommodation_cost = (
                    accommodation_nights
                    * self.forms["full_time"]["accommodation_per_night"]
                )
            else:
                accommodation_cost = (
                    teacher_days * self.forms["full_time"]["accommodation_per_night"]
                )

            # Суточные
            daily_allowance_cost = (
                teacher_days * self.forms["full_time"]["daily_allowance"]
            )

            # Аренда помещения (v6.9.0: 0 в офисных городах)
            region_lower = region.lower() if region else ""
            is_office_city = any(city in region_lower for city in self.OFFICE_CITIES)

            if is_office_city:
                venue_cost = 0
                logger.info(
                    f"[EducationCalc v6.9.0] Аренда = 0 (офисный город: {region})"
                )
            else:
                if venue_days > 0:
                    venue_cost = venue_days * self.rates["venue_daily"]["cost"]
                else:
                    venue_cost = teacher_days * self.rates["venue_daily"]["cost"]

            # Манекен
            if manikin_days > 0:
                manikin_cost = manikin_days * self.rates["manikin_daily"]["cost"]

            full_time_cost = (
                teacher_cost
                + transport_cost
                + accommodation_cost
                + daily_allowance_cost
                + venue_cost
                + manikin_cost
            )

            logger.info(
                f"[Education] Очные затраты: препод={teacher_cost:,.0f}, "
                f"проезд={transport_cost:,.0f}, прожив={accommodation_cost:,.0f}, "
                f"суточные={daily_allowance_cost:,.0f}, аренда={venue_cost:,.0f}, "
                f"манекен={manikin_cost:,.0f}"
            )

        # === Итого ===
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
            recommended_price = minimum
            margin_rub = recommended_price - cost_price
            margin_percent = (margin_rub / cost_price) * 100 if cost_price > 0 else 0

        logger.info(
            f"[EducationCalc] РЕЗУЛЬТАТ: cost_price={cost_price:,.0f}, "
            f"recommended={recommended_price:,.0f}, margin={margin_percent:.1f}%, "
            f"docs={docs_cost:,.0f}, protocol_docs={protocols_count * self.docs['protocol']['cost']:,.0f}, "
            f"full_time={full_time_cost:,.0f}, transport={transport_cost:,.0f}, "
            f"needs_review={needs_manual_review}"
        )

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
