"""
core/analysis/calculator_router.py
Маршрутизация расчётов по типам тендеров.
Вынесено из analyzer.py (v6.8.6-r3).

ИСПРАВЛЕНО (v6.9.0):
- _calc_education теперь передаёт region в calculate_education()
- _calc_combined передаёт documents_text и region
- _calc_opr передаёт opr_positions/opr_persons корректно

ИСПРАВЛЕНО (v7.1.0):
- testing маршрутизируется на PLK-калькулятор (ближайший аналог)

ИСПРАВЛЕНО (v7.2.9):
- Защита от ложного ОПР для электролаборатории.
"""

from typing import Dict, Any
from loguru import logger

from core.calculation.calculator import TenderCalculator
from core.calculation.calculation_result import CalculationResult


class CalculatorRouter:
    """
    Маршрутизирует расчёты по типам тендеров.

    Делегирует вычисления TenderCalculator в зависимости от типа.
    """

    VERSION = "v7.2.9"

    def __init__(self, calculator: TenderCalculator):
        self.calculator = calculator

    def calculate(
        self, tender_info: Dict[str, Any], tender_type: str, documents_text: str
    ) -> CalculationResult:
        """
        Выбирает и выполняет расчёт по типу тендера.

        Args:
            tender_info: Параметры тендера
            tender_type: Тип (sout | education | opr | plk | testing | combined)
            documents_text: Текст документов (для education)

        Returns:
            CalculationResult
        """
        if tender_type == "sout":
            return self._calc_sout(tender_info)
        elif tender_type == "education":
            return self._calc_education(tender_info, documents_text)
        elif tender_type == "opr":
            return self._calc_opr(tender_info, documents_text)
        elif tender_type in ("plk", "testing"):
            if tender_type == "testing":
                logger.info(
                    f"[{self.VERSION}] Testing → маршрутизация на PLK-калькулятор"
                )
            return self._calc_plk(tender_info)
        elif tender_type == "combined":
            return self._calc_combined(tender_info, documents_text)
        else:
            return self._manual_review("Неизвестный тип тендера")

    # ==================== СОУТ ====================

    def _calc_sout(self, info: Dict[str, Any]) -> CalculationResult:
        """Расчёт СОУТ."""
        rm_total = info.get("rm_total", 0)
        if not rm_total:
            return self._manual_review("Не определено количество РМ")

        region = info.get("region", "") or info.get("customer_region", "")

        return self.calculator.calculate_sout(
            rm_total=rm_total,
            variant=info.get("variant", 1),
            addresses_count=info.get("addresses_count", 1),
            cities_count=info.get("cities_count", 1),
            regions_count=info.get("regions_count", 1),
            trip_days=info.get("trip_days", 3),
            rm_with_iii=info.get("rm_with_iii", 0),
            is_seasonal=info.get("is_seasonal", False),
            is_annual=info.get("is_annual", False),
            transport_cost=info.get("transport_cost", 0),
            region=region,
        )

    # ==================== Обучение ====================

    def _calc_education(
        self, info: Dict[str, Any], documents_text: str
    ) -> CalculationResult:
        """Расчёт обучения."""
        students = info.get("students_count", 0)
        if not students:
            return self._manual_review("Не определено количество слушателей")

        doc_types = self._detect_education_docs(info, documents_text)

        region = info.get("region", "") or info.get("customer_region", "")

        return self.calculator.calculate_education(
            students_count=students,
            protocols_count=doc_types.get("protocols", 0),
            qual_certs=doc_types.get("qual_certs", 0),
            diplomas=doc_types.get("diplomas", 0),
            is_distance=info.get("is_distance", False),
            teacher_days=info.get("teacher_days", 0),
            transport_km=info.get("transport_km", 0),
            accommodation_nights=info.get("accommodation_nights", 0),
            venue_days=info.get("venue_days", 0),
            manikin_days=info.get("manikin_days", 0),
            delivery_count=info.get("delivery_count", 1),
            region=region,
            is_annual=info.get("is_annual", False),
            tender_text=documents_text,
            needs_manual_review=info.get("needs_manual_review", False),
            review_reason=info.get("review_reason", ""),
            llm_confidence=info.get("llm_confidence", 0.0),
        )

    def _detect_education_docs(self, info: Dict[str, Any], text: str) -> Dict[str, int]:
        """
        Определяет типы документов для обучения.
        Ключевое правило: обучение ОТ -> всегда protocols_count = students_count.
        """
        text_lower = text.lower()
        students = info.get("students_count", 0)

        if "охрана труда" in text_lower or "обучение по охране труда" in text_lower:
            logger.info(
                f"[{self.VERSION}] Обнаружено обучение ОТ -> protocols={students}"
            )
            return {"protocols": students, "qual_certs": 0, "diplomas": 0}

        protocols = info.get("protocols_count", 0)
        qual_certs = info.get("qual_certs", 0)
        diplomas = info.get("diplomas", 0)

        if protocols > 0 or qual_certs > 0 or diplomas > 0:
            return {
                "protocols": protocols,
                "qual_certs": qual_certs,
                "diplomas": diplomas,
            }

        if "переподготовка" in text_lower or "повышение квалификации" in text_lower:
            return {"protocols": 0, "qual_certs": students, "diplomas": 0}

        return {"protocols": students, "qual_certs": 0, "diplomas": 0}

    # ==================== ОПР ====================

    def _calc_opr(
        self, info: Dict[str, Any], documents_text: str = ""
    ) -> CalculationResult:
        """Расчёт ОПР."""
        positions = info.get("opr_positions", 0)
        persons = info.get("opr_persons", 0)

        # v7.2.9: Защита от ложного ОПР (Электролаборатория)
        if documents_text and any(
            kw in documents_text.lower()
            for kw in [
                "замеров сопротивления изоляции",
                "измерения заземления",
                "сопротивления цепи фаза-нуль",
                "электролаборатория",
                "испытания электроизолирующих перчаток",
                "сопротивления растеканию тока",
            ]
        ):
            logger.warning(
                f"[{self.VERSION}] Обнаружены признаки электролаборатории в тендере ОПР. "
                f"Перенаправляю на ручную проверку."
            )
            return self._manual_review(
                "Вероятно, это электролаборатория (ЭТЛ), а не ОПР. Требуется ручной расчет или черный список."
            )

        # v7.2.8: Защита от ложного ОПР (Испытания пожарных систем)
        if documents_text and any(
            kw in documents_text.lower()
            for kw in [
                "гидравлическое испытание",
                "испытание пожарных кранов",
                "испытание пожарных гидрантов",
                "перекатка пожарных рукавов",
            ]
        ):
            logger.warning(
                f"[{self.VERSION}] Обнаружены признаки испытаний пожарных систем в тендере ОПР. "
                f"Перенаправляю на ручную проверку."
            )
            return self._manual_review(
                "Вероятно, это испытания пожарных систем, а не ОПР. Требуется ручной расчет."
            )

        return self.calculator.calculate_opr(
            rm_count=info.get("rm_total", 0),
            opr_positions=positions,
            opr_persons=persons,
            delivery_count=info.get("delivery_count", 1),
            needs_siz_norms=info.get("needs_siz_norms", False),
            needs_dsiz_norms=info.get("needs_dsiz_norms", False),
            needs_iot_norms=info.get("needs_iot_norms", False),
            transport_cost=info.get("transport_cost", 0),
        )

    # ==================== ПЛК ====================

    def _calc_plk(self, info: Dict[str, Any]) -> CalculationResult:
        """Расчёт ПЛК."""
        points = info.get("measurement_points", 0)
        if not points:
            return self._manual_review("Не определено количество точек замера")

        return self.calculator.calculate_plk(
            points_count=points,
            factors_count=info.get("factors_count", 0),
            delivery_count=info.get("delivery_count", 1),
            is_annual=info.get("is_annual", False),
            needs_subcontractor=info.get("needs_subcontractor", False),
            distance_km=info.get("distance_km", 0),
            transport_cost=info.get("transport_cost", 0),
            accommodation_cost=info.get("accommodation_cost", 0),
        )

    # ==================== Комбинированный ====================

    def _calc_combined(
        self, info: Dict[str, Any], documents_text: str
    ) -> CalculationResult:
        """
        Расчёт комбинированного тендера (СОУТ + обучение).
        """
        total_cost = 0.0
        total_recommended = 0.0
        parts = []

        if info.get("rm_total"):
            sout = self._calc_sout(info)
            total_cost += sout.cost_price
            total_recommended += sout.recommended_price
            parts.append(sout.to_dict())

        if info.get("students_count"):
            edu = self._calc_education(info, documents_text)
            total_cost += edu.cost_price
            total_recommended += edu.recommended_price
            parts.append(edu.to_dict())

        if not parts:
            return self._manual_review(
                "Не определены параметры комбинированного тендера"
            )

        margin_rub = total_recommended - total_cost
        margin_percent = (margin_rub / total_cost * 100) if total_cost > 0 else 0.0

        return CalculationResult(
            cost_price=total_cost,
            recommended_price=total_recommended,
            margin_percent=margin_percent,
            margin_rub=margin_rub,
            transport_cost=0.0,
            subcontractor_cost=0.0,
            needs_manual_review=True,
            review_reason="Комбинированный тендер — требуется ручная проверка",
            details={"parts": parts},
        )

    # ==================== Утилиты ====================

    def _manual_review(self, reason: str) -> CalculationResult:
        """Создаёт результат с требованием ручной проверки."""
        return CalculationResult(
            cost_price=0.0,
            recommended_price=0.0,
            margin_percent=0.0,
            margin_rub=0.0,
            transport_cost=0.0,
            subcontractor_cost=0.0,
            needs_manual_review=True,
            review_reason=reason,
        )
