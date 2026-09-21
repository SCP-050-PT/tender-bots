"""
core/calculation/calculator_router.py
Маршрутизация расчётов по типам тендеров.

v8.0.0-Optimized:
  - Безопасный перехват исключений при расчете.
  - Агрегация транспортных, субподрядных расходов и гарантий в combined-расчете.
  - Унификация флагов agent_blocked / blocked_by_agent.
  - Оптимизация чтения конфигурации аккредитации.
"""

from typing import Dict, Any, Optional
from loguru import logger
import json
from pathlib import Path

from core.calculation.calculator import TenderCalculator
from core.calculation.calculation_result import CalculationResult


class CalculatorRouter:
    VERSION = "v8.0.0-Optimized"
    _accreditation_cache: Optional[Dict] = None

    def __init__(self, calculator: TenderCalculator):
        self.calculator = calculator
        self.accreditation = self._get_accreditation()

    @classmethod
    def _get_accreditation(cls) -> Dict:
        """Ленивая загрузка и кэширование справочника аккредитации."""
        if cls._accreditation_cache is None:
            try:
                path = (
                    Path(__file__).resolve().parent.parent.parent
                    / "knowledge"
                    / "area_accreditation.json"
                )
                if path.exists():
                    with open(path, "r", encoding="utf-8") as f:
                        cls._accreditation_cache = json.load(f)
                else:
                    cls._accreditation_cache = {}
            except Exception as e:
                logger.warning(
                    f"[{cls.VERSION}] Не удалось загрузить аккредитацию: {e}"
                )
                cls._accreditation_cache = {}
        return cls._accreditation_cache

    def calculate(
        self, tender_info: Dict[str, Any], tender_type: str, documents_text: str
    ) -> CalculationResult:

        # === Проверка на блокировку от Агента ===
        is_blocked = tender_info.get("blocked_by_agent") or tender_info.get(
            "agent_blocked"
        )
        if is_blocked:
            reason = (
                tender_info.get("agent_block_reason")
                or tender_info.get("reason")
                or "Заблокировано агентом"
            )
            logger.warning(f"[{self.VERSION}] Тендер заблокирован агентом: {reason}")
            return self._manual_review(reason)

        try:
            # 1. Запуск нужного калькулятора
            if tender_type == "sout":
                result = self._calc_sout(tender_info)
            elif tender_type == "education":
                result = self._calc_education(tender_info, documents_text)
            elif tender_type == "opr":
                result = self._calc_opr(tender_info, documents_text)
            elif tender_type in ("plk", "testing"):
                result = self._calc_plk(tender_info, documents_text)
            elif tender_type == "combined":
                result = self._calc_combined(tender_info, documents_text)
            else:
                return self._manual_review(f"Неизвестный тип тендера: {tender_type}")

        except Exception as e:
            logger.error(
                f"[{self.VERSION}] Ошибка вычислений для {tender_type}: {e}",
                exc_info=True,
            )
            return self._manual_review(f"Ошибка при расчете себестоимости: {e}")

        # 2. SANITY CHECK: Проверка адекватности цены
        nmck = float(tender_info.get("nmck", 0) or 0)

        if result and nmck > 0 and result.recommended_price > 0:
            ratio = result.recommended_price / nmck

            if ratio < 0.2 or ratio > 1.5:
                logger.warning(
                    f"[{self.VERSION}] SANITY CHECK FAIL: Цена {result.recommended_price:.0f} "
                    f"при НМЦК {nmck:.0f} (Ratio: {ratio:.2f})"
                )
                result.needs_manual_review = True
                reason_prefix = f"Аномальное соотношение цены к НМЦК ({ratio:.0%}). "
                if result.review_reason:
                    result.review_reason = f"{reason_prefix} | {result.review_reason}"
                else:
                    result.review_reason = f"{reason_prefix}Требуется ручная проверка."

        return result

    # ==================== СОУТ ====================
    def _calc_sout(self, info: Dict[str, Any]) -> CalculationResult:
        rm_total = info.get("rm_total", 0)

        if not rm_total:
            nmck = info.get("nmck", 0)
            if nmck > 0 and not info.get("llm_found_rm"):
                estimated_rm = int(nmck / 1200)
                logger.warning(
                    f"[{self.VERSION}] СОУТ: кол-во не найдено. Оценка по НМЦК: {estimated_rm} РМ"
                )
                rm_total = estimated_rm
                info["needs_manual_review"] = True
            else:
                return self._manual_review("Не определено количество РМ")

        region = info.get("region", "") or info.get("customer_region", "")
        cities_count = info.get("cities_count", 1)
        regions_count = info.get("regions_count", 1)

        safe_info = dict(info)
        safe_info.pop("variant", None)

        return self.calculator.calculate_sout(
            rm_total=rm_total,
            rm_with_iii=safe_info.get("rm_with_iii", 0),
            needs_subcontractor=safe_info.get("needs_subcontractor", False),
            delivery_count=safe_info.get("delivery_count", 1),
            is_annual=safe_info.get("is_annual", False),
            trip_days=safe_info.get("trip_days", 3),
            regions_count=regions_count,
            transport_cost=safe_info.get("transport_cost", 0),
            is_seasonal=safe_info.get("is_seasonal", False),
            region=region,
            cities_count=cities_count,
            addresses_count=safe_info.get("addresses_count", 1),
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
            trip_days=info.get("trip_days", 0),
            cities_count=info.get("cities_count", 1),
            addresses_count=info.get("addresses_count", 1),
        )

    # ==================== ПЛК ====================
    def _calc_plk(
        self, info: Dict[str, Any], documents_text: str = ""
    ) -> CalculationResult:
        points = info.get("measurement_points", 0) or info.get("points_count", 0)
        measurement_types = info.get("measurement_types", [])

        needs_subcontractor = info.get("needs_subcontractor", False)

        if measurement_types and self.accreditation:
            cannot_measure = set(self.accreditation.get("cannot_measure", []))
            forbidden_found = []

            for factor in measurement_types:
                f_lower = factor.lower()
                if any(
                    cm.lower() in f_lower or f_lower in cm.lower()
                    for cm in cannot_measure
                ):
                    forbidden_found.append(factor)

            if forbidden_found:
                reason = f"Факторы вне аккредитации: {', '.join(forbidden_found)}"
                logger.warning(f"[{self.VERSION}] ПЛК: {reason}")
                needs_subcontractor = True
                info["needs_manual_review"] = True
                info["review_reason"] = reason

        if not points:
            nmck = info.get("nmck", 0)
            if nmck > 0:
                points = int(nmck / 500)
                logger.warning(
                    f"[{self.VERSION}] ПЛК: кол-во точек не найдено. Оценка: {points}"
                )
                info["needs_manual_review"] = True
            else:
                return self._manual_review("Не определено количество точек замера")

        return self.calculator.calculate_plk(
            points_count=points,
            factors_count=len(measurement_types),
            delivery_count=info.get("delivery_count", 1),
            is_annual=info.get("is_annual", False),
            needs_subcontractor=needs_subcontractor,
            distance_km=info.get("distance_km", 0),
            transport_cost=info.get("transport_cost", 0),
            accommodation_cost=info.get("accommodation_cost", 0),
            trip_days=info.get("trip_days", 0),
            cities_count=info.get("cities_count", 1),
            addresses_count=info.get("addresses_count", 1),
        )

    # ==================== Комбинированный ====================
    def _calc_combined(
        self, info: Dict[str, Any], documents_text: str
    ) -> CalculationResult:
        total_cost = 0.0
        total_recommended = 0.0
        total_transport = 0.0
        total_subcontractor = 0.0
        total_guarantee = 0.0

        parts = []
        subtypes = []

        sub_calcs = [
            ("rm_total", "sout", self._calc_sout),
            ("measurement_points", "plk", lambda i: self._calc_plk(i, documents_text)),
            ("opr_positions", "opr", lambda i: self._calc_opr(i, documents_text)),
            (
                "students_count",
                "education",
                lambda i: self._calc_education(i, documents_text),
            ),
        ]

        for trigger_key, name, calc_func in sub_calcs:
            if (
                info.get(trigger_key)
                or (name == "plk" and info.get("points_count"))
                or (name == "opr" and info.get("opr_persons"))
            ):
                sub_res: CalculationResult = calc_func(info)
                total_cost += sub_res.cost_price
                total_recommended += sub_res.recommended_price
                total_transport += sub_res.transport_cost
                total_subcontractor += sub_res.subcontractor_cost
                total_guarantee += sub_res.guarantee_cost
                parts.append(sub_res.to_dict())
                subtypes.append(name)

        if not parts:
            return self._manual_review(
                "Не определены параметры комбинированного тендера (нет данных ни для одной части)"
            )

        margin_rub = total_recommended - total_cost
        margin_percent = (margin_rub / total_cost * 100) if total_cost > 0 else 0.0

        logger.info(
            f"[{self.VERSION}] Combined: рассчитаны части {subtypes}, "
            f"итоговая себестоимость: {total_cost:,.0f}₽"
        )

        return CalculationResult(
            cost_price=total_cost,
            recommended_price=total_recommended,
            margin_percent=margin_percent,
            margin_rub=margin_rub,
            transport_cost=total_transport,
            subcontractor_cost=total_subcontractor,
            guarantee_cost=total_guarantee,
            needs_manual_review=True,
            review_reason=f"Комбинированный тендер ({'+'.join(subtypes)}) — требуется ручная проверка",
            details={"parts": parts, "subtypes": subtypes},
        )

    def _manual_review(self, reason: str) -> CalculationResult:
        return CalculationResult(
            cost_price=0.0,
            recommended_price=0.0,
            margin_percent=0.0,
            margin_rub=0.0,
            transport_cost=0.0,
            subcontractor_cost=0.0,
            guarantee_cost=0.0,
            needs_manual_review=True,
            review_reason=reason,
        )
