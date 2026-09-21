"""
core/analysis/result.py
Результат анализа тендера (единый dataclass).
Заменяет: AnalysisResult из analyzer.py + TenderAnalysis из result_formatter.py.

v8.0.0-Optimized:
  - Добавлена поддержка флагов блокировки агента (agent_blocked, agent_block_reason).
  - Добавлен метод from_dict для десериализации.
  - Добавлены свойства-хелперы is_blocked для удобной проверки статуса.
  - Оптимизирован метод форматирования однострочного комментария.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional


@dataclass
class AnalysisResult:
    """Результат анализа тендера."""

    tender_type: str = ""
    cost_price: float = 0.0
    recommended_price: float = 0.0
    margin_percent: float = 0.0
    risk_level: str = "low"
    decision: str = "рекомендуется"
    needs_manual_review: bool = False
    llm_confidence: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    comment: str = ""
    review_reason: str = ""
    type_detection_source: str = ""
    classification_method: str = ""
    guards_triggered: List[str] = field(default_factory=list)
    nmck: float = 0.0
    red_flags: List[str] = field(default_factory=list)

    # Поля оптимизации и блокировки v8.0.0
    agent_blocked: bool = False
    agent_block_reason: str = ""

    @property
    def is_blocked(self) -> bool:
        """Возвращает True, если тендер отклонен по непрофильности или guard'ам."""
        return self.agent_blocked or self.decision in ("не рекомендуется", "отклонен")

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует объект в словарь."""
        return {
            "tender_type": self.tender_type,
            "cost_price": self.cost_price,
            "recommended_price": self.recommended_price,
            "margin_percent": self.margin_percent,
            "risk_level": self.risk_level,
            "decision": self.decision,
            "needs_manual_review": self.needs_manual_review,
            "llm_confidence": self.llm_confidence,
            "details": self.details,
            "comment": self.comment,
            "review_reason": self.review_reason,
            "type_detection_source": self.type_detection_source,
            "classification_method": self.classification_method,
            "guards_triggered": self.guards_triggered,
            "nmck": self.nmck,
            "red_flags": self.red_flags,
            "agent_blocked": self.agent_blocked,
            "agent_block_reason": self.agent_block_reason,
            "is_blocked": self.is_blocked,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisResult":
        """Создает экземпляр AnalysisResult из словаря."""
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered_data)

    def get_formatted_comment(self) -> str:
        """Форматирует комментарий для Google Sheets / Экспорта (одна строка)."""
        clean_comment = self.comment.replace("\n", " ").strip()
        parts = []

        if clean_comment:
            parts.append(clean_comment)

        if self.review_reason and self.review_reason not in clean_comment:
            parts.append(f"⚠️ {self.review_reason}")

        if self.agent_blocked and self.agent_block_reason:
            parts.append(f"🚫 Заблокирован: {self.agent_block_reason}")

        if self.guards_triggered and "Guards:" not in clean_comment:
            parts.append(f"Guards: {', '.join(self.guards_triggered)}")

        return " | ".join(filter(None, parts))

    def _format_comment(self) -> str:
        """Приватный алиас для обратной совместимости."""
        return self.get_formatted_comment()
