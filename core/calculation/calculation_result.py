"""
core/calculation/calculation_result.py
Единый dataclass для результатов расчёта.
Вынесен из отдельных калькуляторов.

v8.0.0-Optimized:
  - Защита от None в численных полях и details.
  - Добавлен метод from_dict для десериализации.
  - Безопасная сериализация с защитой от TypeError при округлении.
  - Добавлено свойство-хелпер is_zero.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any


def _safe_round(value: Any, ndigits: int = 2) -> float:
    """Вспомогательное безопасное округление чисел."""
    if value is None:
        return 0.0
    try:
        return round(float(value), ndigits)
    except (ValueError, TypeError):
        return 0.0


@dataclass
class CalculationResult:
    """Результат расчёта себестоимости тендера."""

    cost_price: float = 0.0
    recommended_price: float = 0.0
    margin_percent: float = 0.0
    margin_rub: float = 0.0
    transport_cost: float = 0.0
    subcontractor_cost: float = 0.0
    guarantee_cost: float = 0.0
    needs_manual_review: bool = False
    review_reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Гарантирует корректную инициализацию details."""
        if self.details is None:
            self.details = {}

    @property
    def is_zero(self) -> bool:
        """Возвращает True, если расчет нулевой или незавершенный."""
        return self.cost_price <= 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует результат расчёта в словарь."""
        return {
            "cost_price": _safe_round(self.cost_price),
            "recommended_price": _safe_round(self.recommended_price),
            "margin_percent": _safe_round(self.margin_percent),
            "margin_rub": _safe_round(self.margin_rub),
            "transport_cost": _safe_round(self.transport_cost),
            "subcontractor_cost": _safe_round(self.subcontractor_cost),
            "guarantee_cost": _safe_round(self.guarantee_cost),
            "needs_manual_review": self.needs_manual_review,
            "review_reason": self.review_reason,
            "details": self.details,
            "is_zero": self.is_zero,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CalculationResult":
        """Создает экземпляр CalculationResult из словаря."""
        if not data:
            return cls()

        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered_data)
