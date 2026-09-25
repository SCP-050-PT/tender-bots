"""
core/calculation/sout_calculator.py
Расчёт цены на СОУТ.
v7.8.2:
  - Учтены cities_count и addresses_count при расчёте количества поездок.
  - Динамическое получение margin_percent из costs_db.json.
  - Защита от IndexError в _calc_subcontractor при пустых ranges.
"""

from loguru import logger

from core.calculation.cost_loader import load_costs
from core.calculation.calculation_result import CalculationResult


class SoutCalculator:
    """Расчёт цены на СОУТ (упрощённая единая формула)."""

    VERSION = "v7.8.2"

    def __init__(self):
        self.all_costs = load_costs()
        self.costs = self.all_costs["sout"]
        self.travel = self.costs.get("travel", {})

    def calculate(
        self,
        rm_total: int,
        rm_with_iii: int = 0,
        needs_subcontractor: bool = False,
        delivery_count: int = 1,
        is_annual: bool = False,
        trip_days: int = 3,
        regions_count: int = 1,
        transport_cost: float = 0,
        is_seasonal: bool = False,
        region: str = "",
        cities_count: int = 1,
        addresses_count: int = 1,
    ) -> CalculationResult:
        """
        Единый расчёт СОУТ.
        Формула: (РМ × ставка) + Материалы + Доставка + Транспорт + Субподряд ИИИ
        """
        annual_mult = 12 if is_annual else 1

        # 1. Основная работа (единая ставка за РМ)
        base_rate = self.costs.get("base_price_per_rm", 213)
        main_cost = rm_total * base_rate * annual_mult

        # 2. Материалы (бланки, карты, печать)
        materials_cost = self._calc_materials(rm_total) * annual_mult

        # 3. Доставка документов
        delivery_cost = self._calc_delivery(delivery_count, is_annual)

        # 4. Командировочные / Транспорт (с учётом регионов, городов и адресов)
        travel_details = self._calc_travel(
            trip_days=trip_days,
            regions_count=regions_count,
            cities_count=cities_count,
            addresses_count=addresses_count,
            transport_cost=transport_cost,
            is_seasonal=is_seasonal,
            region=region,
        )

        # 5. Субподряд на инструментальные измерения (если есть ИИИ)
        subcontractor_cost, needs_manual_review = self._calc_subcontractor(
            rm_with_iii, needs_subcontractor
        )

        # === ИТОГОВАЯ СЕБЕСТОИМОСТЬ ===
        cost_price = (
            main_cost
            + materials_cost
            + delivery_cost
            + travel_details["total"]
            + subcontractor_cost
        )

        # Динамическая маржа из конфига (default: 10.0%)
        margin_percent = float(self.costs.get("margin_percent", 10.0))
        margin_rub = cost_price * (margin_percent / 100.0)
        recommended_price = cost_price + margin_rub

        # Минимальный порог цены для СОУТ
        min_price = self.costs.get("min_contract_sum", 20000)
        if recommended_price < min_price:
            recommended_price = min_price
            margin_rub = recommended_price - cost_price
            margin_percent = (margin_rub / cost_price * 100) if cost_price > 0 else 0

        review_reason = ""
        if needs_manual_review:
            review_reason = (
                "ИИИ в ТЗ без точного кол-ва РМ — требуется ручная проверка субподряда."
            )

        logger.info(
            f"[{self.VERSION}] СОУТ: {rm_total} РМ × {base_rate}₽ = {main_cost:,.0f}₽ "
            f"(мат={materials_cost:,.0f}, дост={delivery_cost:,.0f}, "
            f"транс={travel_details['total']:,.0f}, суб={subcontractor_cost:,.0f})"
        )

        return CalculationResult(
            cost_price=cost_price,
            recommended_price=recommended_price,
            margin_percent=margin_percent,
            margin_rub=margin_rub,
            transport_cost=travel_details["total"],
            subcontractor_cost=subcontractor_cost,
            guarantee_cost=0,
            needs_manual_review=needs_manual_review,
            review_reason=review_reason,
            details={
                "type": "sout",
                "rm_total": rm_total,
                "base_rate_per_rm": base_rate,
                "main_calculation": main_cost,
                "materials_cost": materials_cost,
                "delivery_cost": delivery_cost,
                "travel_breakdown": travel_details,
                "subcontractor_cost": subcontractor_cost,
                "is_annual": is_annual,
                "is_seasonal": is_seasonal,
                "regions_count": regions_count,
                "cities_count": cities_count,
                "addresses_count": addresses_count,
                "trip_days": trip_days,
            },
        )

    def _calc_materials(self, rm_total: int) -> float:
        """Расчёт материалов пропорционально количеству РМ."""
        mat_config = self.costs.get("materials", {})
        per_rm_cost = mat_config.get("cost_per_rm", 73)
        return rm_total * per_rm_cost

    def _calc_delivery(self, delivery_count: int, is_annual: bool) -> float:
        """Расчёт доставки."""
        actual_count = 12 if is_annual else max(1, delivery_count)
        base_cost = (
            self.costs.get("delivery", {}).get("post_russia", {}).get("cost", 2000)
        )
        return actual_count * base_cost

    def _calc_travel(
        self,
        trip_days: int,
        regions_count: int,
        cities_count: int,
        addresses_count: int,
        transport_cost: float,
        is_seasonal: bool,
        region: str,
    ) -> dict:
        """Упрощённый расчёт транспорта и командировок."""
        seasonal_mult = self.travel.get("seasonal_multiplier", 2) if is_seasonal else 1

        # Количество поездок рассчитывается по максимальному числу локаций
        regions_count = max(1, int(regions_count or 1))
        cities_count = max(1, int(cities_count or 1))
        addresses_count = max(1, int(addresses_count or 1))

        # Выезды ≈ регионы; города/адреса только как мягкий множитель с капом
        if regions_count <= 1:
            trips = min(max(cities_count, addresses_count), 3)
        else:
            trips = max(regions_count, min(cities_count, regions_count * 2))
        trips = max(1, trips)

        # Если передана точная стоимость транспорта — берём её
        if transport_cost > 0:
            total = transport_cost * trips * seasonal_mult
            return {"total": total, "source": "explicit", "trips": trips}

        # Иначе считаем по нормативу
        fixed_trip = self.travel.get("fixed_trip_cost", 12000)
        daily_rate = self.travel.get("daily_measurer_rate", 5000)
        accommodation = self.travel.get("accommodation_per_night", 2500)

        # Бензин/выезд на каждую локацию
        auto_cost = fixed_trip * trips * seasonal_mult

        # Суточные / Работа замерщика (зависят от дней)
        daily_cost = daily_rate * max(1, trip_days) * seasonal_mult

        # Проживание (ночи = дни - 1)
        nights = max(0, trip_days - 1)
        accom_cost = nights * accommodation * seasonal_mult

        total = auto_cost + daily_cost + accom_cost

        return {
            "total": total,
            "source": "calculated",
            "trips": trips,
            "auto_cost": auto_cost,
            "daily_cost": daily_cost,
            "accommodation_cost": accom_cost,
        }

    def _calc_subcontractor(self, rm_with_iii: int, needs_subcontractor: bool) -> tuple:
        """Расчёт субподряда на ИИИ."""
        iii_config = self.costs.get("iii_subcontractor", {})

        if rm_with_iii > 0:
            ranges = iii_config.get("ranges", [])
            for r in ranges:
                if rm_with_iii <= r.get("max_rm", 9999):
                    return r["cost"], False

            # Fallback если превышен максимум или ranges пуст
            base = ranges[-1]["cost"] if ranges else 7000
            max_rm_in_range = ranges[-1].get("max_rm", 20) if ranges else 20
            extra = (rm_with_iii - max_rm_in_range) * 350
            return base + extra, False

        elif needs_subcontractor:
            min_cost = iii_config.get("min_cost", 7000)
            logger.warning(
                f"[{self.VERSION}] ИИИ без кол-ва РМ. Мин. субподряд: {min_cost}₽"
            )
            return min_cost, True

        return 0, False
