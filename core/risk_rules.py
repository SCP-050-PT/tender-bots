"""
core/risk_rules.py
Анализ рисков тендера.

ИСПРАВЛЕНО (v6.8):
- Guard: ОПР с малой себестоимостью -> не считать маржу аномалией
- Guard: цена > НМЦК -> HIGH риск
- Guard: маржа > 200% -> HIGH (с исключением для ОПР)
- Улучшенное логирование

ИСПРАВЛЕНО (v6.9.2):
- FIX: margin > 200% не срабатывает при limit_applied=True (цена поднята лимитом)
- FIX: cost/НМЦК guard убран — теперь в apply_global_limits()
- FIX: добавлен guard на base_cost_price=0 (не удалось рассчитать)
"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from loguru import logger


@dataclass
class RiskResult:
    """Результат анализа рисков."""

    risk_level: str  # low, medium, high
    decision: str  # рекомендуется, не рекомендуется, осторожно
    flags: List[str]
    needs_manual_review: bool
    review_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RiskAnalyzer:
    """Анализатор рисков тендеров."""

    VERSION = "v6.9.3"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.thresholds = {
            "min_margin_percent": 5.0,
            "max_margin_percent": 200.0,
            "min_deadline_days": 3,
            "max_cost_to_nmck_ratio": 0.90,  # Себестоимость не более 90% НМЦК
            "opr_cost_threshold": 50000.0,
        }
        # Переопределение порогов из config при наличии
        self.thresholds.update(self.config.get("thresholds", {}))
        logger.info(f"RiskAnalyzer инициализирован ({self.VERSION})")

    def analyze(
        self,
        tender_type: str,
        nmck: float,
        cost_price: float,
        margin_percent: float,
        deadline_days: int = 30,
        region: str = "",
        needs_manual_review: bool = False,
        limit_applied: bool = False,
        cost_to_nmck_ratio: float = 0.0,
    ) -> Dict[str, Any]:
        """Анализирует риски тендера и возвращает словарь с результатами."""
        flags: List[str] = []
        risk_level = "low"

        # 1. Guard - base_cost_price = 0
        if cost_price <= 0:
            flags.append("Себестоимость = 0 — не удалось рассчитать стоимость")
            logger.error(f"[{self.VERSION}] GUARD: cost_price = 0")
            return RiskResult(
                risk_level="high",
                decision="не рекомендуется",
                flags=flags,
                needs_manual_review=True,
                review_reason="Себестоимость не рассчитана",
            ).to_dict()

        # 2. Guard - cost/НМЦК превышен
        max_cost_ratio = self.thresholds["max_cost_to_nmck_ratio"]
        if cost_to_nmck_ratio > max_cost_ratio:
            flags.append(
                f"Себестоимость ({cost_price:,.0f}₽) составляет {cost_to_nmck_ratio*100:.0f}% от НМЦК ({nmck:,.0f}₽)"
            )
            risk_level = "high"
            logger.error(
                f"[{self.VERSION}] GUARD: cost/НМЦК = {cost_to_nmck_ratio*100:.1f}%"
            )

        # 3. Guard - цена не должна превышать НМЦК
        recommended_price = cost_price * (1 + margin_percent / 100)
        if recommended_price > nmck:
            flags.append(
                f"Рекомендуемая цена ({recommended_price:,.0f}₽) превышает НМЦК ({nmck:,.0f}₽)"
            )
            risk_level = "high"
            logger.error(f"[{self.VERSION}] GUARD: цена > НМЦК")

        # 4. Guard - аномально высокая маржа (>200%)
        if margin_percent > self.thresholds["max_margin_percent"]:
            if limit_applied:
                logger.info(
                    f"[{self.VERSION}] Маржа {margin_percent:.1f}% высокая, "
                    f"но цена поднята лимитом (limit_applied=True) — не аномалия"
                )
            elif (
                tender_type == "opr"
                and cost_price < self.thresholds["opr_cost_threshold"]
            ):
                logger.info(
                    f"[{self.VERSION}] ОПР с себестоимостью {cost_price:,.0f}₽ — "
                    f"маржа {margin_percent:.1f}% не аномалия"
                )
            else:
                flags.append(f"Аномально высокая маржа: {margin_percent:.1f}%")
                risk_level = "high"
                logger.error(
                    f"[{self.VERSION}] GUARD: маржа {margin_percent:.1f}% > {self.thresholds['max_margin_percent']}%"
                )

        # 5. Средние риски (не перекрывают high)
        if margin_percent < self.thresholds["min_margin_percent"]:
            flags.append(f"Низкая маржа: {margin_percent:.1f}%")
            if risk_level == "low":
                risk_level = "medium"
            logger.warning(f"[{self.VERSION}] Низкая маржа: {margin_percent:.1f}%")

        if deadline_days < self.thresholds["min_deadline_days"]:
            flags.append(f"Короткий срок: {deadline_days} дней")
            if risk_level == "low":
                risk_level = "medium"
            logger.warning(f"[{self.VERSION}] Короткий срок: {deadline_days} дней")

        if needs_manual_review:
            flags.append("Требуется ручная проверка")
            if risk_level == "low":
                risk_level = "medium"
            logger.info(
                f"[{self.VERSION}] needs_manual_review -> риск повышен до {risk_level}"
            )

        # Динамическое определение итогового решения по уровню риска
        decision_map = {
            "high": "не рекомендуется",
            "medium": "осторожно",
            "low": "рекомендуется",
        }
        decision = decision_map.get(risk_level, "осторожно")

        return RiskResult(
            risk_level=risk_level,
            decision=decision,
            flags=flags,
            needs_manual_review=needs_manual_review or len(flags) > 0,
        ).to_dict()
