"""
core/calculation/calculator_router.py
Маршрутизация расчётов по типам тендеров.

v7.5.0:
  - Добавлена поддержка ПЛК в комбинированных тендерах (P0-2)
  - Улучшена логика определения subtypes для combined
  - Fallback для ОПР (оценка по НМЦК)
  - Проверка аккредитации для ПЛК
  - Защита от ложного ОПР (ЭТЛ, пожарка)
"""

from typing import Dict, Any
from loguru import logger
import json
from pathlib import Path

from core.calculation.calculator import TenderCalculator
from core.calculation.calculation_result import CalculationResult


class CalculatorRouter:
    VERSION = "v7.5.0"

    def __init__(self, calculator: TenderCalculator):
        self.calculator = calculator
        self.accreditation = self._load_accreditation()

    def _load_accreditation(self) -> Dict:
        try:
            path = (
                Path(__file__).resolve().parent.parent.parent
                / "knowledge"
                / "area_accreditation.json"
            )
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"[{self.VERSION}] Не удалось загрузить аккредитацию: {e}")
        return {}

    def calculate(
        self, tender_info: Dict[str, Any], tender_type: str, documents_text: str
    ) -> CalculationResult:

        # === v7.4.0: Проверка на блокировку от Агента ===
        if tender_info.get("blocked_by_agent"):
            logger.warning(
                f"[{self.VERSION}] Тендер заблокирован агентом: {tender_info.get('reason')}"
            )
            return self._manual_review(
                tender_info.get("reason", "Заблокировано агентом")
            )
        # ==========================================

        # 1. Сначала получаем результат расчета в переменную result
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
            return self._manual_review("Неизвестный тип тендера")

        # 2. Теперь применяем SANITY CHECK к полученному результату
        # === SANITY CHECK: Проверка адекватности цены ===
        nmck = tender_info.get("nmck", 0)
        
        # Проверяем, что результат вообще существует и это не ручная проверка без цены
        if result and nmck > 0 and result.recommended_price > 0:
            ratio = result.recommended_price / nmck
            
            # Если наша цена меньше 20% от НМЦК или больше 150% — это подозрительно
            if ratio < 0.2 or ratio > 1.5:
                logger.warning(
                    f"[{self.VERSION}] SANITY CHECK FAIL: Цена {result.recommended_price:.0f} "
                    f"при НМЦК {nmck:.0f} (Ratio: {ratio:.2f})"
                )
                result.needs_manual_review = True
                # Добавляем причину к существующим, если они есть
                reason_prefix = f"Аномальное соотношение цены к НМЦК ({ratio:.0%}). "
                if result.review_reason:
                    result.review_reason = f"{reason_prefix} | {result.review_reason}"
                else:
                    result.review_reason = f"{reason_prefix}Требуется ручная проверка."
        # ==============================================

        # 3. Возвращаем проверенный результат
        return result

    # ==================== СОУТ (v7.6.0: Единая формула) ====================
    def _calc_sout(self, info: Dict[str, Any]) -> CalculationResult:
        # === Проверка на осознанную ошибку/блокировку от Агента ===
        if info.get("agent_blocked"):
            reason = info.get("agent_block_reason", "Заблокировано агентом")
            logger.warning(f"[{self.VERSION}] Тендер заблокирован агентом (нет файлов): {reason}")
            return self._manual_review(reason)
        # ==========================================

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

        # ИСПРАВЛЕНИЕ: Удаляем variant, так как старый калькулятор его не знает
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
            # v7.3.0: Проверка на удостоверение для пожарки
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

        # v7.2.9/v7.3.0: Защита от ложного ОПР
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

        # v7.3.0: Fallback для количества РМ
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
        measurement_types = info.get(
            "measurement_types", []
        )  # <-- НОВОЕ: список факторов от агента

        needs_subcontractor = info.get("needs_subcontractor", False)

        # v7.4.0: Усиленная проверка аккредитации по списку факторов от агента
        if measurement_types and self.accreditation:
            can_measure = set(self.accreditation.get("can_measure", []))
            cannot_measure = set(self.accreditation.get("cannot_measure", []))

            forbidden_found = []

            for factor in measurement_types:
                f_lower = factor.lower()
                # Проверяем запрещенные
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
            # Fallback для ПЛК
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
        parts = []
        subtypes = []

        # v7.5.0: Поддержка СОУТ + ОПР + Обучение + ПЛК
        if info.get("rm_total"):
            sout = self._calc_sout(info)
            total_cost += sout.cost_price
            total_recommended += sout.recommended_price
            parts.append(sout.to_dict())
            subtypes.append("sout")

        if info.get("measurement_points") or info.get("points_count"):
            plk = self._calc_plk(info, documents_text)
            total_cost += plk.cost_price
            total_recommended += plk.recommended_price
            parts.append(plk.to_dict())
            subtypes.append("plk")

        if info.get("opr_positions") or info.get("opr_persons"):
            opr = self._calc_opr(info, documents_text)
            total_cost += opr.cost_price
            total_recommended += opr.recommended_price
            parts.append(opr.to_dict())
            subtypes.append("opr")

        if info.get("students_count"):
            edu = self._calc_education(info, documents_text)
            total_cost += edu.cost_price
            total_recommended += edu.recommended_price
            parts.append(edu.to_dict())
            subtypes.append("education")

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
            transport_cost=0.0,
            subcontractor_cost=0.0,
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
            needs_manual_review=True,
            review_reason=reason,
        )
