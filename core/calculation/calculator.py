"""
core/calculation/calculator.py
Главный калькулятор — фасад для всех типов тендеров.

v8.0.0-Optimized:
  - Безопасная обработка структуры конфигураций (costs.json) с защитой от KeyError/IndexError.
  - Безопасное приведение типов (float/int) для предотвращения TypeError.
  - Оптимизирована логика расчета банковских гарантий и накладных расходов.
"""

from typing import Dict, Any, List
from loguru import logger

from core.calculation.cost_loader import load_costs
from core.calculation.calculation_result import CalculationResult
from core.calculation.education_calculator import EducationCalculator
from core.calculation.sout_calculator import SoutCalculator
from core.calculation.plk_opr_calculators import PlkCalculator, OprCalculator


def _to_float(val: Any, default: float = 0.0) -> float:
    """Безопасное приведение к float."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class TenderCalculator:
    """Главный калькулятор — фасад для всех типов тендеров."""

    def __init__(self):
        self.costs = load_costs() or {}
        self.education_calc = EducationCalculator()
        self.sout_calc = SoutCalculator()
        self.plk_calc = PlkCalculator()
        self.opr_calc = OprCalculator()
        logger.info("TenderCalculator инициализирован (v8.0.0-Optimized)")

    def calculate_education(self, **kwargs) -> CalculationResult:
        """Расчёт обучения."""
        return self.education_calc.calculate(**kwargs)

    def calculate_sout(self, **kwargs) -> CalculationResult:
        """Расчёт СОУТ."""
        return self.sout_calc.calculate(**kwargs)

    def calculate_plk(self, **kwargs) -> CalculationResult:
        """Расчёт ПЛК."""
        return self.plk_calc.calculate(**kwargs)

    def calculate_opr(self, **kwargs) -> CalculationResult:
        """Расчёт ОПР."""
        return self.opr_calc.calculate(**kwargs)

    # ==================== Глобальные затраты ====================

    def apply_global_costs(
        self,
        nmck: float,
        etp_commission_percent: float = 0.0,
        application_guarantee_percent: float = 0.0,
        contract_guarantee_percent: float = 0.0,
        deadline_days: int = 30,
    ) -> Dict[str, Any]:
        """Применяет глобальные затраты: ЭТП, обеспечение, специалист, срочность."""
        nmck = _to_float(nmck)
        etp_commission_percent = _to_float(etp_commission_percent)
        application_guarantee_percent = _to_float(application_guarantee_percent)
        contract_guarantee_percent = _to_float(contract_guarantee_percent)
        deadline_days = int(_to_float(deadline_days, 30.0))

        extra_costs: Dict[str, Any] = {}

        # 1. Комиссия ЭТП
        if etp_commission_percent > 0:
            etp_cost = nmck * (etp_commission_percent / 100)
            extra_costs["etp_commission"] = round(etp_cost, 2)
            logger.info(
                f"[TenderCalculator] ЭТП комиссия: {etp_commission_percent}% = {etp_cost:,.0f}₽"
            )
        else:
            extra_costs["etp_commission"] = 0.0

        # 2. Обеспечение заявки
        if application_guarantee_percent > 0:
            app_guarantee = nmck * (application_guarantee_percent / 100)
            bg_cost = self._calc_bg_cost(app_guarantee)
            extra_costs["application_guarantee"] = round(bg_cost, 2)
            logger.info(
                f"[TenderCalculator] Обеспечение заявки: {application_guarantee_percent}% = {app_guarantee:,.0f}₽, БГ={bg_cost:,.0f}₽"
            )
        else:
            extra_costs["application_guarantee"] = 0.0

        # 3. Обеспечение контракта
        if contract_guarantee_percent > 0:
            contract_guarantee = nmck * (contract_guarantee_percent / 100)
            bg_cost = self._calc_bg_cost(contract_guarantee)
            extra_costs["contract_guarantee"] = round(bg_cost, 2)
        else:
            extra_costs["contract_guarantee"] = 0.0

        # 4. Нагрузка специалиста
        limits = self.costs.get("global_limits", {})
        max_hours = limits.get("max_tender_preparation_hours", 3)
        rate_per_hour = limits.get("tender_specialist_rate_per_hour", 100)
        specialist_cost = max_hours * rate_per_hour
        extra_costs["specialist_cost"] = specialist_cost

        # 5. Сжатые сроки
        urgency_mult = 1.0
        urgency_note = ""
        if deadline_days < 5:
            urgency_mult = 1.5
            urgency_note = "Срок < 5 дней: +50% к накладным"
        elif deadline_days < 14:
            urgency_mult = 1.2
            urgency_note = "Срок < 14 дней: +20% к накладным"

        extra_costs["urgency_multiplier"] = urgency_mult
        extra_costs["urgency_note"] = urgency_note

        # Расчет итоговой суммы дополнительных затрат
        base_extra = (
            extra_costs["etp_commission"]
            + extra_costs["application_guarantee"]
            + extra_costs["contract_guarantee"]
            + extra_costs["specialist_cost"]
        )

        extra_costs["total_extra"] = round(base_extra * urgency_mult, 2)
        return extra_costs

    # ==================== Глобальные лимиты ====================

    def apply_global_limits(
        self,
        cost_price: float,
        recommended_price: float,
        nmck: float,
        tender_type: str,
    ) -> Dict[str, Any]:
        """Применяет global_limits: min_contract_sum, min_margin_percent, max_cost_to_nmck_ratio."""
        cost_price = _to_float(cost_price)
        recommended_price = _to_float(recommended_price)
        nmck = _to_float(nmck)

        limits = self.costs.get("global_limits", {})
        violations: List[str] = []
        review_reasons: List[str] = []
        adjusted_price = recommended_price
        needs_manual_review = False
        risk_level = "low"

        max_ratio = limits.get("max_cost_to_nmck_ratio", 0.85)
        if nmck >= 100000:
            max_ratio = 0.90

        cost_to_nmck_ratio = (cost_price / nmck) if nmck > 0 else 0.0

        # 1. Минимальная сумма контракта
        min_sum = limits.get("min_contract_sum", 10000)
        if recommended_price < min_sum:
            adjusted_price = min_sum
            violations.append(
                f"Цена предложения {recommended_price:,.0f}₽ < min_contract_sum={min_sum:,.0f}₽"
            )
            review_reasons.append(f"Мин. цена по ТЗ: {min_sum:,.0f}₽.")

        # 2. Проверка и корректировка маржи (±0.3 п.п. — шум округления)
        min_margin = limits.get("min_margin_percent", 10.0)
        if cost_price > 0:
            actual_margin = ((recommended_price - cost_price) / cost_price) * 100
            adjusted_margin_percent = actual_margin

            if actual_margin + 0.3 < min_margin:
                adjusted = cost_price * (1 + min_margin / 100)
                adjusted_price = max(adjusted_price, adjusted)
                adjusted_margin_percent = min_margin
                violations.append(
                    f"Маржа {actual_margin:.1f}% < min_margin_percent={min_margin}%"
                )
                review_reasons.append(
                    f"Мин. маржа {min_margin}% (было {actual_margin:.1f}%)."
                )
        else:
            adjusted_margin_percent = 0.0

        # 3. Превышение порога себестоимости к НМЦК
        if nmck > 0 and cost_to_nmck_ratio > max_ratio:
            violations.append(
                f"cost_price/НМЦК = {cost_to_nmck_ratio*100:.1f}% превышает лимит {max_ratio*100:.0f}%"
            )
            review_reasons.append(
                f"Себестоимость/НМЦК = {cost_to_nmck_ratio*100:.1f}% превышает лимит {max_ratio*100:.0f}%. РИСК: цена завышена."
            )
            risk_level = "high"
            needs_manual_review = True

        is_valid = len(violations) == 0

        return {
            "is_valid": is_valid,
            "adjusted_price": round(adjusted_price, 2),
            "adjusted_margin_percent": round(adjusted_margin_percent, 2),
            "violations": violations,
            "review_reason": " ".join(review_reasons),
            "needs_manual_review": needs_manual_review,
            "risk_level": risk_level,
            "cost_to_nmck_ratio": round(cost_to_nmck_ratio, 4),
            "max_ratio_used": max_ratio,
        }

    def _calc_bg_cost(self, guarantee_amount: float) -> float:
        """Расчёт стоимости банковской гарантии по диапазонам."""
        guarantee_amount = _to_float(guarantee_amount)

        ranges = (
            self.costs.get("guarantees", {})
            .get("application", {})
            .get("bank_guarantee_cost", {})
            .get("ranges", [])
        )

        if not ranges:
            return 1000.0  # Базовая стоимость по умолчанию

        for r in ranges:
            if guarantee_amount <= r.get("max_contract", 0):
                return float(r.get("real_cost", 1000.0))

        return float(ranges[-1].get("real_cost", 1000.0))

    # ==================== Утилиты ====================

    def calculate_guarantee(
        self, contract_sum: float, guarantee_type: str = "contract"
    ) -> float:
        """Расчёт стоимости банковской гарантии."""
        return self._calc_bg_cost(contract_sum)

    def calculate_transport(
        self,
        distance_km: float = 0,
        accommodation_nights: int = 0,
        expert_days: int = 0,
    ) -> Dict[str, float]:
        """Расчёт транспортных расходов."""
        travel_cfg = self.costs.get("travel", {})
        fuel_cfg = travel_cfg.get("fuel", {})
        accom_cfg = travel_cfg.get("accommodation", {})
        daily_cfg = travel_cfg.get("daily_allowance", {})

        consumption = fuel_cfg.get("consumption_l_per_100km", 10.0)
        price_per_l = fuel_cfg.get("price_per_liter", 60.0)
        accom_rate = accom_cfg.get("standard_per_night", 2500.0)
        daily_rate = daily_cfg.get("standard", 700.0)

        fuel_cost = (distance_km / 100) * consumption * price_per_l
        accommodation_cost = accommodation_nights * accom_rate
        daily_allowance = expert_days * daily_rate

        return {
            "fuel_cost": round(fuel_cost, 2),
            "accommodation_cost": round(accommodation_cost, 2),
            "daily_allowance": round(daily_allowance, 2),
            "total": round(fuel_cost + accommodation_cost + daily_allowance, 2),
        }
