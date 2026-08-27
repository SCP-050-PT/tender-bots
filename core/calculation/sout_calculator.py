"""
core/calculation/sout_calculator.py
Расчёт цены для клиента на СОУТ (3 варианта).
Вынесено из calculator.py (v6.5).
ИСПРАВЛЕНО (v6.7.3):
  - Убран дублирующийся CalculationResult (импорт из calculation_result.py)
"""

from typing import Literal
from loguru import logger

from core.calculation.cost_loader import load_costs
from core.calculation.calculation_result import CalculationResult


class SoutCalculator:
    """Расчёт цены для клиента на СОУТ."""

    def __init__(self):
        self.costs = load_costs()["sout"]
        self.cat = self.costs["category_rates"]
        self.travel = self.costs.get("travel", {})

    def calculate(
        self,
        rm_total: int,
        rm_category_1: int = 0,
        rm_category_2: int = 0,
        rm_with_iii: int = 0,
        variant: Literal[1, 2, 3] = 1,
        delivery_count: int = 1,
        is_annual: bool = False,
        needs_subcontractor: bool = False,
        cities_count: int = 1,
        addresses_count: int = 1,
        trip_days: int = 3,
        regions_count: int = 1,
        transport_cost: float = 0,
        is_seasonal: bool = False,
        region: str = "",
    ) -> CalculationResult:
        """
        Расчёт цены для клиента на СОУТ.
        v6.4.2: trips = regions_count, унифицированные командировочные.
        v6.9.1: Годовые тендеры — основной расчёт, материалы, доставка ×12
        """
        # v6.9.1: Годовой множитель
        annual_mult = 12 if is_annual else 1

        # === Субподряд ИИИ ===
        subcontractor_cost, needs_manual_review_iii = self._calc_subcontractor(
            rm_with_iii, needs_subcontractor
        )

        # === Основной расчёт (v7.2.5: исправлен вызов) ===
        # Убираем rm_cat_1, rm_cat_2, variant из вызова, так как метод упрощен до 213*rm
        # Но сам метод _calc_main_price оставим с аргументами для совместимости, просто не будем их использовать
        price = (
            self._calc_main_price(rm_total, rm_category_1, rm_category_2, rm_with_iii, variant) 
            * annual_mult
        )

        # === Материалы и доставка ===
        materials_cost = self._calc_materials() * annual_mult
        delivery_cost = self._calc_delivery(
            delivery_count,
            is_annual,
            is_urgent=(trip_days <= 5 if trip_days else False),
        )

        # === Командировочные (v7.2.5: добавлен расчет билетов) ===
        travel_cost_auto, measurer_and_daily, accommodation_cost_auto, flight_cost = (
            self._calc_travel(
                trip_days, regions_count, transport_cost, is_seasonal, cities_count, region # <-- ПЕРЕДАТЬ REGION
            )
        )

        if is_annual:
            travel_cost_auto *= annual_mult
            measurer_and_daily *= annual_mult
            accommodation_cost_auto *= annual_mult
            flight_cost *= annual_mult

        # === Итого ===
        cost_price = (
            price
            + materials_cost
            + delivery_cost
            + travel_cost_auto
            + measurer_and_daily
            + accommodation_cost_auto
            + flight_cost # <-- УЖЕ БЫЛО, НО ТЕПЕРЬ flight_cost НЕ 0
            + subcontractor_cost
        )

        margin_percent = 10.0
        margin_rub = cost_price * 0.1
        recommended_price = cost_price + margin_rub

        # Минимум 20 000₽ для СОУТ
        if recommended_price < 20000:
            recommended_price = 20000
            margin_rub = recommended_price - cost_price
            margin_percent = (margin_rub / cost_price) * 100 if cost_price > 0 else 0

        review_reason = ""
        if needs_manual_review_iii:
            review_reason = (
                "ИИИ в ТЗ, но кол-во РМ не указано — требуется ручная проверка."
            )

        return CalculationResult(
            cost_price=cost_price,
            recommended_price=recommended_price,
            margin_percent=margin_percent,
            margin_rub=margin_rub,
            transport_cost=travel_cost_auto + flight_cost,
            subcontractor_cost=subcontractor_cost,
            guarantee_cost=0,
            needs_manual_review=needs_manual_review_iii,
            review_reason=review_reason,
            details={
                "type": "sout",
                "variant": variant,
                "rm_total": rm_total,
                "rm_category_1": rm_category_1,
                "rm_category_2": rm_category_2,
                "rm_with_iii": rm_with_iii,
                "needs_manual_review_iii": needs_manual_review_iii,
                "main_calculation": price,
                "materials_cost": materials_cost,
                "delivery_cost": delivery_cost,
                "travel_cost": travel_cost_auto,
                "measurer_and_daily": measurer_and_daily,
                "accommodation_cost": accommodation_cost_auto,
                "flight_cost": flight_cost,
                "cities_count": cities_count,
                "regions_count": regions_count,
                "addresses_count": addresses_count,
                "trip_days": trip_days,
                "is_annual": is_annual,
                "is_seasonal": is_seasonal,
            },
        )

    def _calc_subcontractor(self, rm_with_iii: int, needs_subcontractor: bool) -> tuple:
        """Расчёт субподряда ИИИ."""
        if rm_with_iii > 0:
            for range_info in self.costs["iii_subcontractor"]["ranges"]:
                if rm_with_iii <= range_info["max_rm"]:
                    return range_info["cost"], False
            return 7000 + (rm_with_iii - 20) * 350, False
        elif needs_subcontractor:
            min_cost = self.costs["iii_subcontractor"]["ranges"][0]["cost"]
            logger.warning(
                f"[SoutCalc] ИИИ в ТЗ, но кол-во РМ не указано. "
                f"Заложен мин. субподряд {min_cost}₽. ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА."
            )
            return min_cost, True
        return 0, False

    def _calc_main_price(self, rm_total, rm_cat_1, rm_cat_2, rm_iii, variant):
        """
        v7.2.4: Упрощённый расчёт СОУТ для тендеров.
        """
        base_cost_per_rm = 213
        main_calculation = rm_total * base_cost_per_rm

        logger.info(
            f"[SoutCalc v7.2.4] Расчёт: {rm_total} РМ × {base_cost_per_rm}₽ = "
            f"{main_calculation:,.0f}₽ (себестоимость, тендерная цена)"
        )
        return main_calculation

    def _calc_materials(self) -> float:
        """Расчёт материалов."""
        return (
            self.costs["materials"]["paper_a4"]["cost"]
            * self.costs["materials"]["paper_a4"]["default_quantity"]
            + self.costs["materials"]["ink_per_page"]["cost"]
            * self.costs["materials"]["ink_per_page"]["default_quantity"]
        )

    def _calc_delivery(self, delivery_count: int, is_annual: bool, is_urgent: bool = False) -> float:
        actual_delivery = 12 if is_annual else delivery_count
        base_cost = self.costs["delivery"]["post_russia"]["cost"]
        if is_urgent:
            base_cost *= 2  # курьер вместо почты
        return actual_delivery * base_cost

    def _calc_travel(
        self,
        trip_days: int,
        regions_count: int,
        transport_cost: float,
        is_seasonal: bool,
        cities_count: int,
        region: str = "",  # <-- ДОБАВИТЬ ПАРАМЕТР
    ) -> tuple:
        """Расчёт командировочных. Возвращает (travel_auto, measurer_daily, accommodation, flight)."""
        seasonal_mult = self.travel.get("seasonal_multiplier", 2) if is_seasonal else 1
        fixed_trip = self.travel.get("fixed_trip_cost", 12000)
        accommodation_rate = self.travel.get("accommodation_per_night", 2500)
        daily_measurer_rate = self.travel.get("daily_measurer_rate", 5000)

        trips = max(1, regions_count)

        travel_cost_auto = fixed_trip * trips * seasonal_mult
        measurer_and_daily = daily_measurer_rate * trip_days * trips * seasonal_mult
        accommodation_cost_auto = (
            max(0, trip_days - 1) * trips * accommodation_rate * seasonal_mult
        )

        # v7.2.5: РАСЧЁТ БИЛЕТОВ (Средняя заглушка)
        # Если транспорт не указан явно (transport_cost=0) и регион не офисный (Екб), закладываем среднюю цену
        office_cities = [
            "екатеринбург",
            "верхняя пышма",
            "березовский",
            "арти",
        ]  # можно расширить
        is_office = (
            any(city in region.lower() for city in office_cities) if region else False
        )

        flight_cost = 0
        if transport_cost > 0:
            flight_cost = transport_cost
        elif not is_office and trips > 0:
            # Средняя цена плацкарта/самолета туда-обратно ~8000₽
            avg_ticket_price = 8000
            flight_cost = avg_ticket_price * trips * seasonal_mult
            logger.info(
                f"[SoutCalc v7.2.5] Билеты (среднее): {flight_cost}₽ ({trips} выездов × {avg_ticket_price}₽)"
            )

        if cities_count > 5 and regions_count == 1:
            logger.warning(
                f"[SoutCalc] Много адресов ({cities_count}) в 1 регионе — "
                f"проверьте маршрут выезда"
            )

        logger.info(
            f"[SoutCalc] Командировочные: регионов={regions_count}, выездов={trips}, "
            f"дней={trip_days}, сезон={is_seasonal}, бензин/выезд={travel_cost_auto}, "
            f"суточные+замерщик={measurer_and_daily}, прожив={accommodation_cost_auto}, "
            f"билеты={flight_cost}"
        )

        return (
            travel_cost_auto,
            measurer_and_daily,
            accommodation_cost_auto,
            flight_cost,
        )
