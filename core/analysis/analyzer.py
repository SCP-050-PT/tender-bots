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
from core.parsers.address_parser import AddressParser

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

    def _apply_excel_quantities(
        self, tender_info: dict, documents_text: str, tender_type: str
    ) -> None:
        """Парсит маркеры ExcelExtractor. Для СОУТ не берём слепой max/сумму НМЦК."""
        if not documents_text:
            return
        found = [
            int(x)
            for x in re.findall(
                r"=== ИЗВЛЕЧЕНО ИЗ ТАБЛИЦЫ:\s*Количество\s*=\s*(\d+)",
                documents_text,
                flags=re.IGNORECASE,
            )
        ]
        if not found:
            found = [
                int(x)
                for x in re.findall(
                    r"Количество\s*=\s*(\d{1,5})",
                    documents_text[:8000],
                )
            ]
        if not found:
            return

        nmck = float(tender_info.get("nmck") or 0)

        def pick_near_target(candidates: list, target: float, lo: int, hi: int) -> int | None:
            pool = [q for q in candidates if lo <= q <= hi]
            if not pool:
                pool = [q for q in candidates if q >= 1]
            if not pool:
                return None
            if target > 0 and len(pool) > 1:
                return min(pool, key=lambda q: abs(q - target))
            return max(pool)

        # --- OPR ---
        if tender_type == "opr" and not tender_info.get("opr_positions"):
            target = (nmck / 800.0) if nmck > 0 else 0
            qty = pick_near_target(found, target, 2, 2000)
            if qty:
                tender_info["opr_positions"] = qty
                tender_info["opr_positions_source"] = "excel_nmck"
                logger.info(f"[{self.VERSION}] Excel→opr_positions={qty}")

        if tender_type == "opr":
            if tender_info.get("opr_positions") and not tender_info.get("opr_persons"):
                tender_info["opr_persons"] = tender_info["opr_positions"]
            if tender_info.get("opr_persons") and not tender_info.get("opr_positions"):
                tender_info["opr_positions"] = tender_info["opr_persons"]
            return

        # --- PLK ---
        if tender_type == "plk" and not (
            tender_info.get("measurement_points") or tender_info.get("points_count")
        ):
            target = (nmck / 400.0) if nmck > 0 else 0
            qty = pick_near_target(found, target, 1, 5000)
            if qty:
                # sanity: слишком много точек относительно НМЦК
                if nmck > 0 and qty * 50 > nmck:
                    logger.warning(
                        f"[{self.VERSION}] Excel PLK qty={qty} отвергнут "
                        f"(нереалистично к НМЦК {nmck:,.0f})"
                    )
                else:
                    tender_info["measurement_points"] = qty
                    tender_info["points_count"] = qty
                    tender_info["points_source"] = "excel_nmck"
                    logger.info(f"[{self.VERSION}] Excel→measurement_points={qty}")
            return

        # --- SOUT ---
        if tender_type in ("sout", "sout_opr") and not tender_info.get("rm_total"):
            # цель: рыночный ориентир ~800–1500 ₽/РМ в НМЦК
            target = (nmck / 1200.0) if nmck > 0 else 0
            candidates = [q for q in found if 1 <= q <= 5000]
            if not candidates:
                return
            qty = pick_near_target(candidates, target, 1, 3000)
            if not qty:
                return

            # отсев суммы строк обоснования (кейс 1740 при НМЦК ~200k)
            if nmck > 0:
                # грубо: 213 ₽/РМ себест. → qty не должен давать cost ≫ НМЦК
                if qty * 213 > nmck * 0.95:
                    logger.warning(
                        f"[{self.VERSION}] Excel SOUT qty={qty} отвергнут "
                        f"(qty×213 > 0.95×НМЦК {nmck:,.0f}); ждут агент/КТРУ"
                    )
                    return
                # слишком далеко от оценки по НМЦК (сумма мусора)
                if target >= 5 and qty > max(target * 3, target + 150):
                    logger.warning(
                        f"[{self.VERSION}] Excel SOUT qty={qty} отвергнут "
                        f"(далеко от target≈{target:.0f})"
                    )
                    return

            tender_info["rm_total"] = qty
            tender_info["rm_total_source"] = "excel_nmck"
            logger.info(f"[{self.VERSION}] Excel→rm_total={qty}")

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
            # Excel до агента — чтобы context_override уже содержал qty
            self._apply_excel_quantities(tender_info, documents_text, tender_type)
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

            self._apply_excel_quantities(tender_info, documents_text, tender_type)
            self.fallback_service.apply(tender_info, tender_type)
            # Всё qty-estimate только через FallbackService (cap + manual_review)

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

        # Жёсткий отказ агента по decision
        dec = str(extracted.get("decision") or "").lower().replace("ё", "е")
        if "не рекоменд" in dec or dec in ("reject", "blocked", "нерекомендуется"):
            reason = (
                extracted.get("reason")
                or extracted.get("agent_block_reason")
                or "Агент: не рекомендуется"
            )
            logger.warning(
                f"[{self.VERSION}] Агент decision=не рекомендуется — {reason}"
            )
            agent_logger.warning(f"🚫 АГЕНТ REJECT: {reason}")
            tender_info["agent_blocked"] = True
            tender_info["agent_block_reason"] = reason
            tender_info["needs_manual_review"] = True
            return
        agent_logger.info(f"🔄 СРАВНЕНИЕ ДАННЫХ ПАРСЕРА И АГЕНТА ДЛЯ {tender_id}:")
        pre_agent_students = int(tender_info.get("students_count") or 0)

        # Поля, которые Excel уже заполнил — агент не имеет права их затирать
        PROTECTED = {
            "rm_total": "rm_total_source",
            "opr_positions": "opr_positions_source",
            "opr_persons": "opr_positions_source",  # если используете
            "measurement_points": "points_source",
            "points_count": "points_source",
            "students_count": "students_count_source",
        }
        TRUSTED_SOURCES = ("ktru", "excel", "excel_nmck", "excel_table")
        nmck = float(tender_info.get("nmck") or 0)

        # === ЛОГ ОТВЕТА АГЕНТА (quantity + geo) ===
        agent_logger.info(
            f"📦 АГЕНТ RAW GEO/QTY {tender_id}: "
            f"rm_total={extracted.get('rm_total')!r} | "
            f"opr_positions={extracted.get('opr_positions')!r} | "
            f"points={extracted.get('measurement_points') or extracted.get('points_count')!r} | "
            f"students={extracted.get('students_count')!r} | "
            f"addresses_count={extracted.get('addresses_count')!r} | "
            f"cities_count={extracted.get('cities_count')!r} | "
            f"regions_count={extracted.get('regions_count')!r} | "
            f"addresses={extracted.get('addresses')!r} | "
            f"cities={extracted.get('cities')!r}"
        )
        logger.info(
            f"[{self.VERSION}] Агент geo/qty: "
            f"addr={extracted.get('addresses_count')} "
            f"cities={extracted.get('cities_count')} "
            f"regions={extracted.get('regions_count')} "
            f"list_addr={extracted.get('addresses')} "
            f"list_cities={extracted.get('cities')}"
        )

        # null по quantity — явно в лог (MCP сам не вызовется)
        for qk in (
            "rm_total",
            "opr_positions",
            "measurement_points",
            "points_count",
            "students_count",
            "addresses_count",
        ):
            if qk in extracted and extracted.get(qk) is None:
                logger.warning(
                    f"[{self.VERSION}] Агент вернул {qk}=null — будет Excel/fallback, не MCP"
                )
                agent_logger.warning(f"⚠️ NULL от агента: {qk}")

        for key, value in extracted.items():
            if value is None or key.startswith("_"):
                continue

            src_key = PROTECTED.get(key)
            if src_key:
                src = str(tender_info.get(src_key) or "").lower()
                old = tender_info.get(key)
                if old is not None and int(old or 0) > 0:
                    if any(t in src for t in TRUSTED_SOURCES):
                        logger.info(
                            f"[{self.VERSION}] Qty защищён ({src}): {key}={old} "
                            f"(агент хотел {value})"
                        )
                        agent_logger.info(
                            f"🛡️ QTY: {key}={old} source={src}, агент {value} отклонён"
                        )
                        continue

            old_val = tender_info.get(key)
            tender_info[key] = value
            if old_val != value and old_val is not None:
                logger.warning(
                    f"[{self.VERSION}] ⚠️ РАСХОЖДЕНИЕ в {tender_id}: "
                    f"{key} изменено с {old_val} на {value}"
                )
                agent_logger.warning(f"⚠️ РАСХОЖДЕНИЕ: {key} {old_val} → {value}")
            else:
                logger.info(
                    f"[{self.VERSION}] ✅ Подтверждено агентом: {key}={value}"
                )
                agent_logger.info(f"✅ ПОДТВЕРЖДЕНО: {key}={value}")

        # Синхронизация points
        if tender_info.get("measurement_points") and not tender_info.get("points_count"):
            tender_info["points_count"] = tender_info["measurement_points"]
        elif tender_info.get("points_count") and not tender_info.get("measurement_points"):
            tender_info["measurement_points"] = tender_info["points_count"]
        if tender_info.get("measurement_points"):
            tender_info["points_count"] = tender_info["measurement_points"]
        # === Education: students = уникальные люди, protocols = сумма документов ===
        if tender_type == "education":
            programs = tender_info.get("programs") or []
            if isinstance(programs, list) and programs:
                doc_sum = 0
                unique_candidates = []
                for p in programs:
                    if not isinstance(p, dict):
                        continue
                    cnt = int(p.get("count") or 0)
                    if cnt <= 0:
                        continue
                    unique_candidates.append(cnt)
                    doc_type = (p.get("doc_type") or "protocol").lower()
                    if doc_type in ("protocol", "протокол"):
                        doc_sum += cnt
                    elif doc_type in ("diploma", "диплом"):
                        tender_info["diplomas"] = (
                            int(tender_info.get("diplomas") or 0) + cnt
                        )
                    elif doc_type in ("certificate", "удостоверение", "cert"):
                        tender_info["certificates"] = (
                            int(tender_info.get("certificates") or 0) + cnt
                        )
                    elif doc_type in ("qual_cert", "свидетельство", "pk"):
                        tender_info["qual_certs"] = (
                            int(tender_info.get("qual_certs") or 0) + cnt
                        )
                    else:
                        doc_sum += cnt

                # Уникальные слушатели: КТРУ (до агента) важнее суммы программ
                if pre_agent_students > 0:
                    unique_students = pre_agent_students
                elif unique_candidates:
                    unique_students = max(unique_candidates)
                else:
                    unique_students = int(tender_info.get("students_count") or 0)

                if unique_students > 0:
                    old = tender_info.get("students_count")
                    tender_info["students_count"] = unique_students
                    if old and int(old) != unique_students:
                        logger.info(
                            f"[{self.VERSION}] Education students: "
                            f"{old} → {unique_students} (unique/KTRU, not sum)"
                        )
                if doc_sum > 0 and not tender_info.get("protocols_count"):
                    tender_info["protocols_count"] = doc_sum
                elif doc_sum > 0:
                    # protocols не меньше суммы protocol-программ
                    tender_info["protocols_count"] = max(
                        int(tender_info.get("protocols_count") or 0), doc_sum
                    )

        # === Пост-проверка аккредитации (без lookbehind, без ложных срабатываний) ===
        blocked_factors = []
        types_list = extracted.get("measurement_types") or []
        if isinstance(types_list, list):
            SAFE = (
                "неионизир",
                "уф-ради",
                "уф ради",
                "уф-",
                "ультрафиолет",
                "бактериальн",  # обсеменённость воздуха — не смывы
            )
            BLOCK = (
                "рентген",
                "гамма-изл",
                "гамма изл",
                "амбиентн",
                "нейтронн",
                "смыв",
                "бактериологич",
                "гельминт",
                "асбест",
                "ионизирующ",
                "ионизир. изл",
            )
            for raw in types_list:
                t = str(raw).lower().strip()
                if any(s in t for s in SAFE):
                    continue
                # «радиация» без УФ
                if "радиац" in t and "уф" not in t and "ультрафиолет" not in t:
                    blocked_factors.append("радиация")
                    continue
                if "ионизир" in t and "неионизир" not in t:
                    blocked_factors.append("ионизирующие")
                    continue
                for needle in BLOCK:
                    if needle in t:
                        blocked_factors.append(needle)
                        break

        if blocked_factors:
            reason = (
                f"Факторы вне аккредитации: {', '.join(sorted(set(blocked_factors)))}"
            )
            logger.warning(f"[{self.VERSION}] АККРЕДИТАЦИЯ (post-agent): {reason}")
            agent_logger.warning(f"🚫 АККРЕДИТАЦИЯ: {reason}")
            tender_info["agent_blocked"] = True
            tender_info["agent_block_reason"] = reason
            tender_info["needs_manual_review"] = True
            return

        # === География ===
        parser_geo = AddressParser().count_addresses(documents_text or "")
        agent_addr = int(tender_info.get("addresses_count") or 0)
        agent_cities = int(tender_info.get("cities_count") or 0)
        p_cities = int(parser_geo.get("cities_count") or 0)
        p_regions = int(parser_geo.get("regions_count") or 1)
        p_reliable = bool(parser_geo.get("is_reliable"))
        p_city_list = parser_geo.get("cities") or parser_geo.get("city_list") or []
        p_addr_list = (
            parser_geo.get("addresses") or parser_geo.get("address_list") or []
        )

        logger.info(
            f"[{self.VERSION}] GEO сравнение {tender_id}: "
            f"агент addr={agent_addr} cities={agent_cities} regions={tender_info.get('regions_count')} | "
            f"parser cities={p_cities} regions={p_regions} reliable={p_reliable} | "
            f"parser_cities={p_city_list} parser_addresses={p_addr_list}"
        )
        agent_logger.info(
            f"📍 GEO: агент a={agent_addr} c={agent_cities} | "
            f"parser c={p_cities} r={p_regions} reliable={p_reliable} | "
            f"города парсера: {p_city_list}"
        )

        # Есть ли у агента реальные списки (не голые цифры)
        agent_city_list = extracted.get("cities") or extracted.get("city_list") or []
        agent_addr_list = (
            extracted.get("addresses") or extracted.get("address_list") or []
        )
        agent_has_list = bool(
            (isinstance(agent_city_list, list) and len(agent_city_list) > 0)
            or (isinstance(agent_addr_list, list) and len(agent_addr_list) > 0)
        )

        # 1) Агент = 1, парсер надёжно больше → парсер
        if agent_addr <= 1 and agent_cities <= 1 and p_reliable and p_cities > 1:
            tender_info["addresses_count"] = p_cities
            tender_info["cities_count"] = p_cities
            tender_info["regions_count"] = max(1, p_regions)
            logger.warning(
                f"[{self.VERSION}] Гео: агент=1, берём AddressParser "
                f"({p_cities} городов, {p_regions} регионов) → {p_city_list}"
            )
            agent_logger.warning(f"📍 Переопределение geo парсером: {p_city_list}")

        # 2a) Парсер reliable, агент раздул без списка → не выше парсера
        elif (
            p_reliable
            and p_cities >= 1
            and max(agent_addr, agent_cities) > p_cities
            and not agent_has_list
        ):
            tender_info["cities_count"] = p_cities
            tender_info["addresses_count"] = max(
                p_cities, min(int(agent_addr or p_cities), p_cities + 1)
            )
            tender_info["regions_count"] = max(
                1, int(tender_info.get("regions_count") or p_regions)
            )
            logger.info(
                f"[{self.VERSION}] GEO: parser reliable c={p_cities}, "
                f"агент {max(agent_addr, agent_cities)} без списка → cap к парсеру"
            )
            agent_logger.info(f"📍 GEO cap→parser: cities={p_cities} (агент без list)")

        # 2b) Агент много, парсер 0–1 и НЕ reliable → можно оставить агента
        elif max(agent_addr, agent_cities) >= 3 and p_cities <= 1 and not p_reliable:
            logger.info(
                f"[{self.VERSION}] Гео: агент={max(agent_addr, agent_cities)}, "
                f"parser={p_cities} unreliable — оставляем агента"
            )

        # 2c) Агент много + есть список → агент (кап ниже)
        elif max(agent_addr, agent_cities) >= 3 and agent_has_list:
            logger.info(
                f"[{self.VERSION}] Гео: агент со списком "
                f"cities={agent_city_list or agent_addr_list}"
            )
        # 3) Кап
        cities = max(
            int(tender_info.get("cities_count") or 1),
            int(tender_info.get("addresses_count") or 1),
        )
        regions = int(tender_info.get("regions_count") or 1)
        capped = min(cities, 3) if regions <= 1 else min(cities, 4)
        if cities > capped:
            logger.warning(
                f"[{self.VERSION}] Гео-кап: {cities} → {capped} (regions={regions})"
            )
            agent_logger.warning(f"📍 Гео-кап: {cities} → {capped}")
            tender_info["cities_count"] = capped
            tender_info["addresses_count"] = capped

        # Итог geo после всех правил
        logger.info(
            f"[{self.VERSION}] GEO ИТОГ {tender_id}: "
            f"addresses={tender_info.get('addresses_count')} "
            f"cities={tender_info.get('cities_count')} "
            f"regions={tender_info.get('regions_count')} "
            f"remote={tender_info.get('is_remote_region')}"
        )
        agent_logger.info(
            f"📍 GEO ИТОГ: a={tender_info.get('addresses_count')} "
            f"c={tender_info.get('cities_count')} "
            f"r={tender_info.get('regions_count')}"
        )

        region = (
            tender_info.get("region") or tender_info.get("customer_region") or ""
        ).lower()
        home_markers = ("свердлов", "екатеринбург", "екат")
        tender_info["is_remote_region"] = bool(region) and not any(
            h in region for h in home_markers
        )

        # === Sanity quantity vs НМЦК ===
        nmck = float(tender_info.get("nmck") or 0)

        def _src(field_src: str) -> str:
            return str(tender_info.get(field_src) or "").lower()

        def _trusted(field_src: str) -> bool:
            s = _src(field_src)
            return any(t in s for t in ("ktru", "excel"))

        if nmck > 0:
            rm = int(tender_info.get("rm_total") or 0)
            if tender_type in ("sout", "sout_opr") and rm > 0:
                unit = nmck / rm
                if _trusted("rm_total_source"):
                    if unit < 150:
                        logger.warning(
                            f"[{self.VERSION}] Низкая цена извещения ~{unit:.0f}₽/РМ "
                            f"при qty={rm} (source={tender_info.get('rm_total_source')}). "
                            f"Qty не трогаем; cost от costs_db (213₽/РМ)."
                        )
                        tender_info["needs_manual_review"] = True
                        tender_info["low_nmck_unit_price"] = round(unit, 2)
                else:
                    if rm > 100_000 or (rm > 100 and unit < 20):
                        logger.warning(
                            f"[{self.VERSION}] Sanity: rm_total={rm} "
                            f"без trusted source → сброс"
                        )
                        tender_info["rm_total"] = None
                        tender_info.pop("rm_total_source", None)

            opr = int(tender_info.get("opr_positions") or 0)
            if (
                tender_type == "opr"
                and opr > 0
                and not _trusted("opr_positions_source")
            ):
                if opr > 50_000 or (nmck / opr < 20 and opr > 50):
                    logger.warning(
                        f"[{self.VERSION}] Sanity: opr_positions={opr} "
                        f"без trusted source → сброс"
                    )
                    tender_info["opr_positions"] = None
                    tender_info.pop("opr_positions_source", None)

            pts = int(
                tender_info.get("measurement_points")
                or tender_info.get("points_count")
                or 0
            )
            if tender_type == "plk" and pts > 0 and not _trusted("points_source"):
                if pts > 100_000 or (nmck / pts < 5 and pts > 200):
                    logger.warning(
                        f"[{self.VERSION}] Sanity: points={pts} "
                        f"без trusted source → сброс"
                    )
                    tender_info["measurement_points"] = None
                    tender_info["points_count"] = None
                    tender_info.pop("points_source", None)

        # === Safety-отказ агента ===
        raw_response = extracted.get("raw_response") if extracted else None
        if raw_response and isinstance(raw_response, str):
            refusal_markers = [
                "я не могу обсуждать",
                "давайте поговорим о чём-нибудь ещё",
                "i cannot discuss",
                "i can't discuss",
                "не могу обсуждать эту тему",
            ]
            if any(m in raw_response.lower() for m in refusal_markers):
                logger.warning(
                    f"[{self.VERSION}] Агент отказался анализировать {tender_id} (safety filter)"
                )
                agent_logger.warning(f"🚫 ОТКАЗ АГЕНТА (safety): {raw_response[:120]}")
                tender_info["agent_blocked"] = True
                tender_info["agent_block_reason"] = (
                    "Агент отказался анализировать (safety filter)"
                )
                tender_info["needs_manual_review"] = True
                return

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
