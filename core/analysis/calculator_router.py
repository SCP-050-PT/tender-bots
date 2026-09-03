"""
core/analysis/calculator_router.py
Маршрутизация расчётов по типам тендеров.
v7.3.0:
  - Fallback для ОПР (оценка по НМЦК).
  - Поддержка ОПР в комбинированных тендерах.
  - Проверка аккредитации для ПЛК.
  - Защита от ложного ОПР (ЭТЛ, пожарка).
"""

from typing import Dict, Any
from loguru import logger
import json
from pathlib import Path

from core.calculation.calculator import TenderCalculator
from core.calculation.calculation_result import CalculationResult


class CalculatorRouter:
    VERSION = "v7.3.0"

    def __init__(self, calculator: TenderCalculator):
        self.calculator = calculator
        self.accreditation = self._load_accreditation()

    def _load_accreditation(self) -> Dict:
        try:
            path = (
                Path(__file__).resolve().parent.parent.parent
                / "knowledge"
                / "area_accreditation.json"
            )
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"[{self.VERSION}] Не удалось загрузить аккредитацию: {e}")
        return {}

    def calculate(
        self, tender_info: Dict[str, Any], tender_type: str, documents_text: str
    ) -> CalculationResult:
        if tender_type == "sout":
            return self._calc_sout(tender_info)
        elif tender_type == "education":
            return self._calc_education(tender_info, documents_text)
        elif tender_type == "opr":
            return self._calc_opr(tender_info, documents_text)
        elif tender_type in ("plk", "testing"):
            return self._calc_plk(tender_info, documents_text)
        elif tender_type == "combined":
            return self._calc_combined(tender_info, documents_text)
        else:
            return self._manual_review("Неизвестный тип тендера")

    # ==================== СОУТ ====================
    def _calc_sout(self, info: Dict[str, Any]) -> CalculationResult:
        rm_total = info.get("rm_total", 0)
        if not rm_total:
            return self._manual_review("Не определено количество РМ")

        region = info.get("region", "") or info.get("customer_region", "")

        # v7.3.0: Мульти-регион detection
        cities_count = info.get("cities_count", 1)
        regions_count = info.get("regions_count", 1)

        return self.calculator.calculate_sout(
            rm_total=rm_total,
            variant=info.get("variant", 1),
            addresses_count=info.get("addresses_count", 1),
            cities_count=cities_count,
            regions_count=regions_count,
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
        text_lower = text.lower()
        students = info.get("students_count", 0)

        if "охрана труда" in text_lower or "обучение по охране труда" in text_lower:
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
            # v7.3.0: Проверка на удостоверение для пожарки
            if "пожарн" in text_lower and (
                "удостоверение" in text_lower or "удостоверения" in text_lower
            ):
                return {
                    "protocols": 0,
                    "qual_certs": 0,
                    "diplomas": 0,
                    "certificates": students,
                }
            return {"protocols": 0, "qual_certs": students, "diplomas": 0}

        return {"protocols": students, "qual_certs": 0, "diplomas": 0}

    # ==================== ОПР ====================
    def _calc_opr(
        self, info: Dict[str, Any], documents_text: str = ""
    ) -> CalculationResult:
        positions = info.get("opr_positions", 0)
        persons = info.get("opr_persons", 0)
        nmck = info.get("nmck", 0)

        # v7.2.9/v7.3.0: Защита от ложного ОПР
        if documents_text:
            text_lower = documents_text.lower()
            forbidden_kw = [
                "замеров сопротивления изоляции",
                "измерения заземления",
                "электролаборатория",
                "гидравлическое испытание",
                "испытание пожарных кранов",
                "перекатка пожарных рукавов",
                "диагностика технического состояния",
                "холодильной машины",
                "чиллер",
            ]
            if any(kw in text_lower for kw in forbidden_kw):
                return self._manual_review(
                    "Обнаружены признаки ЭТЛ/Пожарки/Диагностики. Не ОПР."
                )

        # v7.3.0: Fallback для количества РМ
        if positions == 0 and persons == 0:
            if nmck > 0:
                estimated_rm = int(nmck / 700)
                logger.warning(
                    f"[{self.VERSION}] ОПР: кол-во не найдено. Оценка по НМЦК: {estimated_rm} РМ"
                )
                positions = estimated_rm
                info["needs_manual_review"] = True
                info["review_reason"] = (
                    "Количество РМ не найдено, использована оценка по НМЦК"
                )
            else:
                return self._manual_review(
                    "Не определено количество РМ/должностей и нет НМЦК для оценки"
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
    def _calc_plk(
        self, info: Dict[str, Any], documents_text: str = ""
    ) -> CalculationResult:
        points = info.get("measurement_points", 0) or info.get("points_count", 0)

        # v7.3.0: Проверка аккредитации
        needs_subcontractor = info.get("needs_subcontractor", False)
        if documents_text and self.accreditation:
            # Простая проверка на ключевые слова вне аккредитации
            text_lower = documents_text.lower()
            cannot_measure = self.accreditation.get("cannot_measure", {})
            for cat, data in cannot_measure.items():
                examples = data.get("examples", [])
                if any(ex.lower() in text_lower for ex in examples):
                    logger.warning(
                        f"[{self.VERSION}] ПЛК: Обнаружены факторы вне аккредитации ({cat})"
                    )
                    needs_subcontractor = True
                    info["needs_manual_review"] = True
                    info["review_reason"] = (
                        f"Факторы вне аккредитации ({cat}). Требуется субподряд."
                    )
                    break

        if not points:
            # Fallback для ПЛК
            nmck = info.get("nmck", 0)
            if nmck > 0:
                points = int(nmck / 500)  # Средняя цена точки ~500
                logger.warning(
                    f"[{self.VERSION}] ПЛК: кол-во точек не найдено. Оценка: {points}"
                )
                info["needs_manual_review"] = True
            else:
                return self._manual_review("Не определено количество точек замера")

        return self.calculator.calculate_plk(
            points_count=points,
            factors_count=info.get("factors_count", 0),
            delivery_count=info.get("delivery_count", 1),
            is_annual=info.get("is_annual", False),
            needs_subcontractor=needs_subcontractor,
            distance_km=info.get("distance_km", 0),
            transport_cost=info.get("transport_cost", 0),
            accommodation_cost=info.get("accommodation_cost", 0),
        )

    # ==================== Комбинированный ====================
    def _calc_combined(
        self, info: Dict[str, Any], documents_text: str
    ) -> CalculationResult:
        total_cost = 0.0
        total_recommended = 0.0
        parts = []

        # v7.3.0: Поддержка СОУТ + ОПР + Обучение
        if info.get("rm_total"):
            sout = self._calc_sout(info)
            total_cost += sout.cost_price
            total_recommended += sout.recommended_price
            parts.append(sout.to_dict())

        if info.get("opr_positions") or info.get("opr_persons"):
            opr = self._calc_opr(info, documents_text)
            total_cost += opr.cost_price
            total_recommended += opr.recommended_price
            parts.append(opr.to_dict())

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

    def _manual_review(self, reason: str) -> CalculationResult:
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
