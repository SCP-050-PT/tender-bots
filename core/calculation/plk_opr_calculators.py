"""
core/calculation/plk_opr_calculators.py
Расчёт цены для клиента на ПЛК и ОПР.

v7.8.4:
  - PlkCalculator: добавлен fallback для логистики по 1 городу/объекту с флагом неуверенности или регионального выезда.
  - OprCalculator: сохранен механизм авторасчета по объектам.
"""

from loguru import logger

from core.calculation.cost_loader import load_costs
from core.calculation.calculation_result import CalculationResult


class PlkCalculator:
    """Расчёт цены для клиента на ПЛК."""

    def __init__(self):
        self.all_costs = load_costs()
        self.costs = self.all_costs["plk"]

    def calculate(
        self,
        points_count: int,
        factors_count: int = 0,
        delivery_count: int = 1,
        is_annual: bool = False,
        needs_subcontractor: bool = False,
        distance_km: float = 0,
        transport_cost: float = 0,
        accommodation_cost: float = 0,
        trip_days: int = 0,
        cities_count: int = 1,
        addresses_count: int = 1,
        is_remote_region: bool = False,  # Доп. флаг удалённого региона
    ) -> CalculationResult:
        """Расчёт цены для клиента на ПЛК."""
        annual_mult = 12 if is_annual else 1

        points_cost = (
            points_count * self.costs["base_cost_per_point"]["cost"] * annual_mult
        )
        measurer_cost = (
            points_count
            * self.costs["labor"]["measurer_per_point"]["cost"]
            * annual_mult
        )

        materials_cost = (
            self.costs["materials"]["paper_a4"]["cost"]
            * self.costs["materials"]["paper_a4"]["default_quantity"]
            + self.costs["materials"]["ink_per_page"]["cost"]
            * self.costs["materials"]["ink_per_page"]["default_quantity"]
        ) * annual_mult

        actual_delivery = 12 if is_annual else delivery_count
        base_delivery_cost = self.costs["delivery"]["post_russia"]["cost"]

        if trip_days and trip_days <= 5:
            base_delivery_cost *= 2
            logger.info(
                f"[PlkCalc v7.2.0] Срочная доставка (trip_days={trip_days} ≤ 5): "
                f"стоимость ×2 = {base_delivery_cost:,.0f}₽"
            )
        delivery_cost = actual_delivery * base_delivery_cost

        subcontractor_cost = (
            self.costs["subcontractor"]["default_cost"] if needs_subcontractor else 0
        )

        # === РАСЧЁТ ТРАНСПОРТА ===
        travel = self.costs.get("travel", {})

        # 1. Приоритет: Точный километраж
        if distance_km > 0:
            fuel = self.all_costs.get("travel", {}).get(
                "fuel", {"consumption_l_per_100km": 10, "price_per_liter": 60}
            )
            transport_cost = (
                distance_km
                / 100
                * fuel["consumption_l_per_100km"]
                * fuel["price_per_liter"]
            )
            logger.info(
                f"[PlkCalc v7.8.4] Транспорт по километражу: "
                f"{distance_km}km → {transport_cost:,.0f}₽"
            )

        # 2. Fallback: Явная стоимость транспорта извне
        elif transport_cost > 0:
            logger.info(
                f"[PlkCalc v7.8.4] Использован внешний transport_cost={transport_cost:,.0f}."
            )

        # 3. Эвристика по количеству городов / объектов
        else:
            effective_locations = max(cities_count, addresses_count)
            trip_cost = travel.get("fixed_trip_cost", 4000)

            if effective_locations > 1:
                trips = effective_locations - 1
                transport_cost = trips * trip_cost

                if effective_locations > 3 and accommodation_cost == 0:
                    accom_per_night = travel.get("accommodation_per_night", 2500)
                    nights = max(1, trips // 2)
                    accommodation_cost = nights * accom_per_night
                    logger.info(
                        f"[PlkCalc v7.8.4] Добавлено проживание: {nights} ночей * {accom_per_night} = {accommodation_cost:,.0f}₽"
                    )

                logger.warning(
                    f"[PlkCalc v7.8.4] Транспорт рассчитан эвристически по {effective_locations} локациям: "
                    f"{trips} поездок * {trip_cost} = {transport_cost:,.0f}₽"
                )
            elif is_remote_region or trip_days > 0:
                # Fallback для 1 города/объекта, если закупка удалённая или заложена выездная работа
                transport_cost = trip_cost
                logger.info(
                    f"[PlkCalc v7.8.4] Заложен базовый выезд в регион (1 поездка = {transport_cost:,.0f}₽)"
                )
            else:
                transport_cost = 0
                logger.info(
                    "[PlkCalc v7.8.4] Транспорт = 0 (местный объект без выезда)"
                )

        # === РАСЧЁТ ПРОЖИВАНИЯ И СУТОЧНЫХ ===
        if accommodation_cost == 0 and trip_days > 1:
            accommodation_cost = (trip_days - 1) * travel.get(
                "accommodation_per_night", 2500
            )

        daily_allowance = 0
        if trip_days > 0:
            daily_allowance = trip_days * travel.get("daily_allowance", 1000)
        elif cities_count > 1:
            daily_allowance = (cities_count - 1) * travel.get("daily_allowance", 1000)

        cost_price = (
            points_cost
            + measurer_cost
            + materials_cost
            + delivery_cost
            + subcontractor_cost
            + transport_cost
            + accommodation_cost
            + daily_allowance
        )

        margin_percent = float(self.costs.get("margin_percent", 10.0))
        margin_rub = cost_price * (margin_percent / 100.0)
        recommended_price = cost_price + margin_rub

        if recommended_price < 15000:
            recommended_price = 15000
            margin_rub = recommended_price - cost_price
            margin_percent = (margin_rub / cost_price) * 100 if cost_price > 0 else 0

        return CalculationResult(
            cost_price=cost_price,
            recommended_price=recommended_price,
            margin_percent=margin_percent,
            margin_rub=margin_rub,
            transport_cost=transport_cost,
            subcontractor_cost=subcontractor_cost,
            guarantee_cost=0,
            details={
                "type": "plk",
                "points_count": points_count,
                "factors_count": factors_count,
                "points_cost": points_cost,
                "measurer_cost": measurer_cost,
                "materials_cost": materials_cost,
                "delivery_cost": delivery_cost,
                "is_annual": is_annual,
                "needs_subcontractor": needs_subcontractor,
                "distance_km": distance_km,
                "cities_count": cities_count,
                "transport_cost": transport_cost,
                "accommodation_cost": accommodation_cost,
                "daily_allowance": daily_allowance,
            },
        )


class OprCalculator:
    """Расчёт цены для клиента на ОПР."""

    def __init__(self):
        self.all_costs = load_costs()
        self.costs = self.all_costs["opr"]

    def calculate(
        self,
        rm_count: int = 0,
        opr_positions: int = 0,
        opr_persons: int = 0,
        delivery_count: int = 1,
        needs_siz_norms: bool = False,
        needs_dsiz_norms: bool = False,
        needs_iot_norms: bool = False,
        transport_cost: float = 0,
        trip_days: int = 0,
        cities_count: int = 1,
        addresses_count: int = 1,
    ) -> CalculationResult:
        """Расчёт цены для клиента на ОПР."""
        if opr_positions > 0:
            base_count = opr_positions
            rate = self.costs["rates"]["per_position"]["cost"]
            rate_type = "per_position"
        elif opr_persons > 0:
            base_count = opr_persons
            rate = self.costs["rates"]["per_person"]["cost"]
            rate_type = "per_person"
        elif rm_count > 0:
            base_count = rm_count
            rate = self.costs["rates"]["per_position"]["cost"]
            rate_type = "per_position (fallback from rm_count)"
            logger.info(
                f"[OprCalc v7.8.0] Fallback: rm_count={rm_count} → opr_positions"
            )
        else:
            return CalculationResult(
                cost_price=0,
                recommended_price=0,
                margin_percent=0,
                margin_rub=0,
                needs_manual_review=True,
                review_reason="Не указано количество должностей/работников для ОПР",
            )

        position_cost = base_count * rate

        materials_cost = (
            self.costs["materials"]["paper_a4"]["cost"]
            * self.costs["materials"]["paper_a4"]["default_quantity"]
            + self.costs["materials"]["ink_per_page"]["cost"]
            * self.costs["materials"]["ink_per_page"]["default_quantity"]
        )

        additional_cost = 0
        if needs_siz_norms:
            additional_cost += base_count * 200
        if needs_dsiz_norms:
            additional_cost += base_count * 200
        if needs_iot_norms:
            additional_cost += base_count * 200

        base_delivery_cost = self.costs["delivery"]["post_russia"]["cost"]
        if trip_days and trip_days <= 5:
            base_delivery_cost *= 2
            logger.info(
                f"[OprCalc v7.2.0] Срочная доставка (trip_days={trip_days} ≤ 5): "
                f"стоимость ×2 = {base_delivery_cost:,.0f}₽"
            )
        delivery_cost = delivery_count * base_delivery_cost

        travel = self.costs.get("travel", {})

        final_transport_cost = transport_cost
        final_accommodation_cost = 0
        final_daily_allowance = 0

        effective_locations = max(cities_count, addresses_count)

        if transport_cost > 0:
            logger.info(
                f"[OprCalc v7.8.2] Использован внешний transport_cost={transport_cost:,.0f}"
            )

        elif effective_locations > 1:
            trip_cost = travel.get("fixed_trip_cost", 4000)
            trips = effective_locations - 1

            final_transport_cost = trips * trip_cost

            accom_per_night = travel.get("accommodation_per_night", 2500)
            final_accommodation_cost = trips * accom_per_night

            daily_rate = travel.get("daily_allowance", 1000)
            final_daily_allowance = trips * daily_rate

            logger.warning(
                f"[OprCalc v7.8.2] Логистика рассчитана по {effective_locations} объектам: "
                f"Транспорт={final_transport_cost:,.0f}, "
                f"Проживание={final_accommodation_cost:,.0f}, "
                f"Суточные={final_daily_allowance:,.0f}"
            )

        elif trip_days > 0:
            final_accommodation_cost = max(0, trip_days - 1) * travel.get(
                "accommodation_per_night", 2500
            )
            final_daily_allowance = trip_days * travel.get("daily_allowance", 1000)

        cost_price = (
            materials_cost
            + position_cost
            + additional_cost
            + delivery_cost
            + final_transport_cost
            + final_accommodation_cost
            + final_daily_allowance
        )

        margin_percent = float(self.costs.get("margin_percent", 10.0))
        margin_rub = cost_price * (margin_percent / 100.0)
        recommended_price = cost_price + margin_rub

        if recommended_price < 15000:
            recommended_price = 15000
            margin_rub = recommended_price - cost_price
            margin_percent = (margin_rub / cost_price) * 100 if cost_price > 0 else 0

        if margin_percent > 20.0:
            logger.warning(
                f"[OprCalc v7.8.2] ВНИМАНИЕ: margin_percent из конфига = {margin_percent}%. "
                f"Ожидалось ~10%. Проверьте costs_db.json → opr.margin_percent"
            )

        return CalculationResult(
            cost_price=cost_price,
            recommended_price=recommended_price,
            margin_percent=margin_percent,
            margin_rub=margin_rub,
            transport_cost=final_transport_cost,
            subcontractor_cost=0,
            guarantee_cost=0,
            details={
                "type": "opr",
                "base_count": base_count,
                "rate_type": rate_type,
                "rate_per_unit": rate,
                "position_cost": position_cost,
                "materials_cost": materials_cost,
                "additional_cost": additional_cost,
                "delivery_cost": delivery_cost,
                "transport_cost": final_transport_cost,
                "accommodation_cost": final_accommodation_cost,
                "daily_allowance": final_daily_allowance,
                "cities_count": cities_count,
                "needs_siz_norms": needs_siz_norms,
                "needs_dsiz_norms": needs_dsiz_norms,
                "needs_iot_norms": needs_iot_norms,
                "config_margin_percent": margin_percent,
            },
        )
