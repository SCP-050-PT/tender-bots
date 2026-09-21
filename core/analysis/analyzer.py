"""
core/analysis/analyzer.py
Фасад для анализа тендеров.

v8.1.0-Separated Services:
  - Четкое разделение: TenderClassifierService (старый YandexGPT) для типа тендера,
    AgentService (AI Studio Agent) для анализа ТЗ.
"""

import json
import re
from typing import Optional, Dict, Any, List
from loguru import logger

from core.calculation.calculator import TenderCalculator
from core.risk_rules import RiskAnalyzer
from core.tender_type import TenderTypeDetector
from utils.llm_client import YandexGPTClient
from core.calculation.calculation_result import CalculationResult
from core.analysis.result import AnalysisResult
from core.analysis.guard_engine import GuardEngine
from core.calculation.calculator_router import CalculatorRouter

from core.services.type_service import TypeService
from core.services.tender_classifier import TenderClassifierService
from core.services.agent_service import AgentService
from core.services.fallback_service import FallbackService

agent_logger = logger.bind(type="agent_thinking")


class TenderAnalyzer:
    """Фасад для анализа тендеров v8.1.0"""

    VERSION = "v8.1.0-Separated"

    ETP_COMMISSION_RATES = {
        "ртс-тендер": 1.0,
        "ртс": 1.0,
        "сбербанк-аст": 0.5,
        "аст": 0.5,
        "фабрикант": 1.5,
        "еэтп": 1.0,
        "электронная площадка": 1.0,
        "этп гпб": 1.0,
        "агора": 0.8,
    }

    PROFITABLE_TYPES = {"sout", "opr", "sout_opr", "plk", "education"}

    def __init__(
        self,
        calculator: TenderCalculator,
        risk_analyzer: RiskAnalyzer,
        type_detector: Optional[TenderTypeDetector] = None,
        llm_client: Optional[YandexGPTClient] = None,
        agent_client: Any = None,
    ):
        self.calculator = calculator
        self.risk_analyzer = risk_analyzer

        # 1. Сервис определения типа тендера (Легкий YandexGPT)
        self.classifier_service = TenderClassifierService(llm_client=llm_client)

        # 2. Сервис анализа самого тендера (AI Studio Agent)
        self.agent_service = AgentService(agent_client=agent_client)

        self.type_service = TypeService()
        self.fallback_service = FallbackService()
        self.guard_engine = GuardEngine()
        self.calculator_router = CalculatorRouter(calculator)
        logger.info(f"TenderAnalyzer инициализирован ({self.VERSION})")

    def _determine_tender_type_pipeline(
        self,
        tender_info: Dict[str, Any],
        documents_text: str = "",
        llm_classification: Optional[str] = None,
        llm_confidence: float = 0.0,
        tender_type_hint: Optional[str] = None,
    ) -> tuple[str, str, str]:

        # --- 1. КТРУ Override ---
        if tender_info.get("students_count", 0) > 0:
            logger.info(
                f"[{self.VERSION}] [Pipeline] Override типа: 'education' "
                f"(КТРУ дал {tender_info['students_count']} слушателей)"
            )
            agent_logger.info("🔄 OVERRIDE ТИПА: education (по данным КТРУ)")
            return "education", "ktru_override", "ktru"

        # --- 2. Стандартный TypeService (Regex / Parser hint) ---
        tender_type, type_source, method = self.type_service.resolve(
            tender_info=tender_info,
            documents_text=documents_text,
            llm_classification=llm_classification,
            llm_confidence=llm_confidence,
            tender_type_hint=tender_type_hint,
        )

        if tender_type in self.PROFITABLE_TYPES:
            return tender_type, type_source, method

        # --- 3. YandexGPT Classifier по Title (Вызываем ClassifierService!) ---
        title = tender_info.get("title") or tender_info.get("object_info") or ""
        if title:
            logger.info(
                f"[{self.VERSION}] [Pipeline] TypeService вернул '{tender_type}'. "
                f"Запуск легкого YandexGPT классификатора по title..."
            )

            # Вызываем классификатор:
            gpt_type = self.classifier_service.classify_tender_type(title)

            logger.info(f"[{self.VERSION}] [Pipeline] YandexGPT вердикт: '{gpt_type}'")

            if gpt_type in self.PROFITABLE_TYPES:
                return gpt_type, "yandexgpt_title_classifier", "llm_fast_title"

        return tender_type, type_source, method

    def analyze(
        self,
        tender_info: Dict[str, Any],
        documents_text: str = "",
        llm_classification: Optional[str] = None,
        llm_confidence: float = 0.0,
        tender_type_hint: Optional[str] = None,
    ) -> AnalysisResult:
        tender_id = tender_info.get("reg_number", "UNKNOWN")
        logger.info(f"[{self.VERSION}] Начинаю анализ тендера {tender_id}")
        agent_logger.info(f"🔍 НАЧАЛО АНАЛИЗА ТЕНДЕРА: {tender_id}")

        # Шаг 1: Определение типа тендера
        tender_type, type_source, method = self._determine_tender_type_pipeline(
            tender_info=tender_info,
            documents_text=documents_text,
            llm_classification=llm_classification,
            llm_confidence=llm_confidence,
            tender_type_hint=tender_type_hint,
        )
        agent_logger.info(f"🏷️ ОПРЕДЕЛЕН ТИП: {tender_type} (источник: {type_source})")

        # Шаг 2: Guard'ы
        tender_info, guards = self.guard_engine.apply(tender_info, tender_type)
        if guards:
            agent_logger.info(f"🛡️ СРАБОТАЛИ GUARD'Ы: {guards}")

        # === Вызов AI Studio Agent ===
        if tender_type not in self.PROFITABLE_TYPES:
            logger.info(
                f"[{self.VERSION}] ⚡ Пропуск AI Studio Agent для непрофильного типа '{tender_type}'."
            )
            agent_logger.info(
                f"⏩ СКИП АГЕНТА: Тип '{tender_type}' не относится к целевым закупкам."
            )
            tender_info["agent_blocked"] = True
            tender_info["agent_block_reason"] = (
                f"Непрофильный тип тендера: {tender_type}"
            )
        else:
            # Вызываем Агента
            self._verify_with_agent(tender_info, documents_text, tender_type)

        # === БЛОКИРОВКА И FALLBACK ===
        if tender_info.get("agent_blocked"):
            reason = tender_info.get("agent_block_reason", "Неизвестная причина")
            logger.warning(
                f"[{self.VERSION}] 🚫 АГЕНТ/ФИЛЬТР ЗАБЛОКИРОВАЛ ТЕНДЕР {tender_id}. Fallback ОТМЕНЕН."
            )
            agent_logger.error(f"❌ Fallback отменен для {tender_id}: {reason}")
        else:
            logger.info(f"[{self.VERSION}] Запуск fallback-оценок...")
            self.fallback_service.apply(tender_info, tender_type)

            nmck = tender_info.get("nmck", 0)
            if nmck > 0:
                if tender_type in ("sout", "sout_opr") and not tender_info.get(
                    "rm_total"
                ):
                    estimated = int(nmck / 1200)
                    tender_info["rm_total"] = estimated
                    tender_info["rm_total_source"] = "fallback_nmck"
                    agent_logger.info(f"📉 FALLBACK СОУТ: {estimated} РМ (из НМЦК)")
                elif (
                    tender_type == "plk"
                    and not tender_info.get("measurement_points")
                    and not tender_info.get("points_count")
                ):
                    estimated = int(nmck / 500)
                    tender_info["measurement_points"] = estimated
                    tender_info["points_count"] = estimated
                    tender_info["points_source"] = "fallback_nmck"
                    agent_logger.info(f"📉 FALLBACK ПЛК: {estimated} точек (из НМЦК)")
                elif (
                    tender_type == "opr"
                    and not tender_info.get("opr_positions")
                    and not tender_info.get("opr_persons")
                ):
                    estimated = int(nmck / 700)
                    tender_info["opr_positions"] = estimated
                    tender_info["opr_positions_source"] = "fallback_nmck"
                    agent_logger.info(f"📉 FALLBACK ОПР: {estimated} позиций (из НМЦК)")
                elif tender_type == "education" and not tender_info.get(
                    "students_count"
                ):
                    estimated = int(nmck / 1500)
                    tender_info["students_count"] = estimated
                    tender_info["students_count_source"] = "fallback_nmck"
                    agent_logger.info(
                        f"📉 FALLBACK ОБУЧЕНИЕ: {estimated} слушателей (из НМЦК)"
                    )

        # Шаг 4: Глобальные затраты
        nmck = tender_info.get("nmck", 0)
        deadline_days = tender_info.get("deadline_days", 30)
        etp_commission = self._resolve_etp_commission(tender_info)
        app_guarantee = self._parse_guarantee_percent(
            tender_info.get("application_guarantee", "")
        )
        contract_guarantee = self._parse_guarantee_percent(
            tender_info.get("contract_guarantee", "")
        )

        extra_costs = self.calculator.apply_global_costs(
            nmck=nmck,
            etp_commission_percent=etp_commission,
            application_guarantee_percent=app_guarantee,
            contract_guarantee_percent=contract_guarantee,
            deadline_days=deadline_days,
        )

        # Шаг 5: Расчёт БАЗОВОЙ себестоимости
        base_result = self.calculator_router.calculate(
            tender_info, tender_type, documents_text
        )
        result = self._apply_extra_costs(base_result, extra_costs)

        # Шаг 6: Глобальные лимиты / Guard'ы
        limits_result = self.calculator.apply_global_limits(
            cost_price=result.cost_price,
            recommended_price=result.recommended_price,
            nmck=nmck,
            tender_type=tender_type,
        )

        if not limits_result["is_valid"]:
            logger.warning(
                f"[{self.VERSION}] GUARD: обнаружены нарушения лимитов: "
                f"{limits_result['violations']}"
            )
            agent_logger.warning(f"⚠️ НАРУШЕНИЕ ЛИМИТОВ: {limits_result['violations']}")
            old_price = result.recommended_price
            result = CalculationResult(
                cost_price=result.cost_price,
                recommended_price=limits_result["adjusted_price"],
                margin_percent=limits_result["adjusted_margin_percent"],
                margin_rub=limits_result["adjusted_price"] - result.cost_price,
                transport_cost=result.transport_cost,
                subcontractor_cost=result.subcontractor_cost,
                guarantee_cost=result.guarantee_cost,
                needs_manual_review=(
                    result.needs_manual_review or limits_result["needs_manual_review"]
                ),
                review_reason=self._merge_review_reasons(
                    result.review_reason,
                    limits_result["review_reason"],
                    limits_result["violations"],
                ),
                details={
                    **(result.details or {}),
                    "global_limits_violations": limits_result["violations"],
                    "original_recommended_price": old_price,
                    "limit_applied": True,
                },
            )

        # Шаг 7: Анализ рисков
        if deadline_days is None or (
            isinstance(deadline_days, (int, float)) and deadline_days <= 0
        ):
            deadline_days = 30

        risk_result = self.risk_analyzer.analyze(
            tender_type=tender_type,
            nmck=nmck,
            cost_price=result.cost_price,
            margin_percent=result.margin_percent,
            deadline_days=deadline_days,
            region=tender_info.get("region", ""),
            needs_manual_review=result.needs_manual_review,
            limit_applied=result.details.get("limit_applied", False),
            cost_to_nmck_ratio=limits_result.get("cost_to_nmck_ratio", 0),
        )

        limits_risk = limits_result.get("risk_level", "low")
        if limits_risk == "high" and risk_result.get("risk_level") != "high":
            risk_result["risk_level"] = "high"
            max_ratio = self.calculator.costs.get("global_limits", {}).get(
                "max_cost_to_nmck_ratio", 0.85
            )
            risk_result["flags"] = risk_result.get("flags", []) + [
                f"Себестоимость составляет >{max_ratio*100:.0f}% от НМЦК — высокий риск убыточности"
            ]

        # Шаг 8: Комментарий
        comment = self._build_comment(
            tender_type=tender_type,
            result=result,
            type_source=type_source,
            method=method,
            guards=guards,
            extra_costs=extra_costs,
            limits_result=limits_result,
        )

        agent_logger.info(
            f"✅ АНАЛИЗ ЗАВЕРШЕН: тип={tender_type}, решение={risk_result['decision']}"
        )

        return AnalysisResult(
            tender_type=tender_type,
            cost_price=result.cost_price,
            recommended_price=result.recommended_price,
            margin_percent=result.margin_percent,
            risk_level=risk_result["risk_level"],
            decision=risk_result["decision"],
            needs_manual_review=result.needs_manual_review,
            llm_confidence=llm_confidence,
            details=result.details,
            comment=comment,
            review_reason=result.review_reason,
            type_detection_source=type_source,
            classification_method=method,
            guards_triggered=guards,
            nmck=nmck,
            red_flags=risk_result.get("flags", []),
        )

    # ==================== ВЕРИФИКАЦИЯ ЧЕРЕЗ АГЕНТА ====================

    def _verify_with_agent(
        self, tender_info: Dict[str, Any], documents_text: str, tender_type: str
    ) -> None:
        """
        Верификация запускается ТОЛЬКО для подтвержденных целевых тендеров.
        Использует AgentService (AI Studio Agent).
        """
        tender_id = tender_info.get("reg_number", "UNKNOWN_ID")
        truncated_text = documents_text[:25000] if documents_text else ""

        parser_context = {
            "tender_id": tender_id,
            "nmck": tender_info.get("nmck", 0),
            "parsed_rm": tender_info.get("rm_total"),
            "parsed_opr_positions": tender_info.get("opr_positions"),
            "parsed_plk_points": tender_info.get("measurement_points"),
            "parsed_students": tender_info.get("students_count"),
            "detected_type": tender_type,
            "raw_text_length": len(documents_text),
        }

        logger.info(
            f"[{self.VERSION}] 🚀 Запуск AI Studio Agent для профильного тендера {tender_id} (тип: {tender_type})..."
        )
        agent_logger.info(f"🤖 ЗАПУСК ВЕРИФИКАЦИИ АГЕНТОМ: {tender_id}")

        # Вызываем метод из AgentService:
        extracted = self.agent_service.extract_params(
            tender_type=tender_type,
            documents_text=truncated_text,
            tender_id=tender_id,
            context_override=json.dumps(parser_context, ensure_ascii=False),
        )

        if extracted and extracted.get("blocked_by_error"):
            reason = extracted.get("reason", "Ошибка анализа")
            logger.warning(
                f"[{self.VERSION}] Агент отказался анализировать {tender_id}: {reason}"
            )
            agent_logger.warning(f"🚫 ОТКАЗ АГЕНТА: {reason}")
            tender_info["agent_blocked"] = True
            tender_info["agent_block_reason"] = reason
            return

        if not extracted:
            logger.warning(
                f"[{self.VERSION}] Агент не вернул верифицированные данные для {tender_id}"
            )
            agent_logger.warning(f"⚠️ АГЕНТ НЕ ВЕРНУЛ ДАННЫЕ ДЛЯ {tender_id}")
            return

        agent_logger.info(f"🔄 СРАВНЕНИЕ ДАННЫХ ПАРСЕРА И АГЕНТА ДЛЯ {tender_id}:")

        for key, value in extracted.items():
            if value is not None:
                old_val = tender_info.get(key)
                tender_info[key] = value

                if old_val != value and old_val is not None:
                    logger.warning(
                        f"[{self.VERSION}] ⚠️ РАСХОЖДЕНИЕ в {tender_id}: {key} изменено с {old_val} на {value}"
                    )
                    agent_logger.warning(f"⚠️ РАСХОЖДЕНИЕ: {key} {old_val} → {value}")
                else:
                    logger.info(
                        f"[{self.VERSION}] ✅ Подтверждено агентом: {key}={value}"
                    )
                    agent_logger.info(f"✅ ПОДТВЕРЖДЕНО: {key}={value}")

    # ==================== Утилиты ====================

    def _merge_review_reasons(
        self, existing: str, limits_reason: str, violations: List[str]
    ) -> str:
        parts = []
        if existing:
            parts.append(existing)
        if limits_reason:
            parts.append(limits_reason)
        if violations:
            parts.append("Нарушения лимитов: " + "; ".join(violations))
        return " | ".join(parts) if parts else ""

    def _resolve_etp_commission(self, tender_info: Dict[str, Any]) -> float:
        etp = tender_info.get("etp_commission_percent", 0)
        if etp > 0:
            return etp

        platform = tender_info.get("platform_name", "").lower()
        for key, val in self.ETP_COMMISSION_RATES.items():
            if key in platform:
                return val
        return 0.0

    def _parse_guarantee_percent(self, guarantee_raw: str) -> float:
        if not guarantee_raw:
            return 0.0
        text = str(guarantee_raw).lower()
        if "не требуется" in text or "нет" in text:
            return 0.0
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if m:
            return float(m.group(1))
        return 0.0

    def _apply_extra_costs(
        self, base_result: CalculationResult, extra_costs: dict
    ) -> CalculationResult:
        total_extra = extra_costs.get("total_extra", 0)

        if base_result.cost_price <= 0:
            return base_result

        new_cost = base_result.cost_price + total_extra
        new_recommended = base_result.recommended_price + total_extra

        margin_rub = new_recommended - new_cost
        margin_percent = (margin_rub / new_cost * 100) if new_cost > 0 else 0.0

        details = dict(base_result.details) if base_result.details else {}
        details.update(
            {
                "etp_commission": extra_costs.get("etp_commission", 0),
                "application_guarantee": extra_costs.get("application_guarantee", 0),
                "contract_guarantee": extra_costs.get("contract_guarantee", 0),
                "specialist_cost": extra_costs.get("specialist_cost", 0),
                "urgency_multiplier": extra_costs.get("urgency_multiplier", 1.0),
                "urgency_note": extra_costs.get("urgency_note", ""),
                "base_cost_price": base_result.cost_price,
                "global_costs_total": total_extra,
            }
        )

        return CalculationResult(
            cost_price=new_cost,
            recommended_price=new_recommended,
            margin_percent=margin_percent,
            margin_rub=margin_rub,
            transport_cost=base_result.transport_cost,
            subcontractor_cost=base_result.subcontractor_cost,
            guarantee_cost=extra_costs.get("application_guarantee", 0),
            needs_manual_review=base_result.needs_manual_review,
            review_reason=base_result.review_reason,
            details=details,
        )

    def _build_comment(
        self,
        tender_type: str,
        result,
        type_source: str,
        method: str,
        guards: List[str],
        extra_costs: dict = None,
        limits_result: dict = None,
    ) -> str:
        lines = [
            f"Анализ тендера типа «{tender_type}»",
            "",
            f"Расчётная себестоимость: {result.cost_price:,.0f} ₽",
            f"Рекомендуемая цена: {result.recommended_price:,.0f} ₽",
            f"Маржа: {result.margin_percent:.1f}%",
            "",
            f"Определение типа: {type_source} ({method})",
        ]

        if guards:
            lines.append("")
            lines.append("Сработавшие guard'ы:")
            for guard in guards:
                lines.append(f"  • {guard}")

        if extra_costs:
            lines.append("")
            lines.append("Глобальные затраты:")
            if extra_costs.get("etp_commission", 0) > 0:
                lines.append(
                    f"  • Комиссия ЭТП: {extra_costs['etp_commission']:,.0f} ₽"
                )
            if extra_costs.get("application_guarantee", 0) > 0:
                lines.append(
                    f"  • Обеспечение заявки (БГ): "
                    f"{extra_costs['application_guarantee']:,.0f} ₽"
                )
            if extra_costs.get("contract_guarantee", 0) > 0:
                lines.append(
                    f"  • Обеспечение контракта (БГ): "
                    f"{extra_costs['contract_guarantee']:,.0f} ₽"
                )
            if extra_costs.get("specialist_cost", 0) > 0:
                lines.append(
                    f"  • Нагрузка специалиста: {extra_costs['specialist_cost']} ₽"
                )
            if extra_costs.get("urgency_note"):
                lines.append(f"  • {extra_costs['urgency_note']}")

        if result.details and result.details.get("base_cost_price"):
            lines.append("")
            lines.append(
                f"  • Базовая себестоимость: "
                f"{result.details['base_cost_price']:,.0f} ₽"
            )
            lines.append(
                f"  • Global costs: "
                f"{result.details.get('global_costs_total', 0):,.0f} ₽"
            )

        if limits_result and not limits_result.get("is_valid", True):
            lines.append("")
            lines.append("⚠️ Нарушения глобальных лимитов:")
            for v in limits_result.get("violations", []):
                lines.append(f"  • {v}")
            if limits_result.get("original_recommended_price"):
                lines.append(
                    f"  • Цена скорректирована: "
                    f"{limits_result['original_recommended_price']:,.0f} ₽ → "
                    f"{limits_result['adjusted_price']:,.0f} ₽"
                )

        if result.review_reason:
            lines.append("")
            lines.append(f"⚠️ {result.review_reason}")

        return "\n".join(lines)
