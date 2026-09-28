"""
Единый сервис fallback-оценок по НМЦК.
v7.1.2: унификация points_source / measurement_points_source.
"""

from typing import Dict, Any
from datetime import datetime
from loguru import logger

COEFFICIENTS = {
    "sout": {"price_per_unit": 1200, "unit_name": "РМ"},
    "plk": {"price_per_unit": 170, "unit_name": "точек"},
    "testing": {"price_per_unit": 170, "unit_name": "точек"},
    "education": {"price_per_unit": 2500, "unit_name": "слушателей"},
    "opr": {"price_per_unit": 500, "unit_name": "должностей"},
}


class FallbackService:
    VERSION = "v7.1.2"

    @staticmethod
    def apply(tender_info: Dict[str, Any], tender_type: str) -> Dict[str, Any]:
        nmck = tender_info.get("nmck", 0) or 0
        if nmck <= 0:
            return tender_info

        coeff = COEFFICIENTS.get(tender_type)
        if not coeff:
            return tender_info

        price_per_unit = coeff["price_per_unit"]

        if tender_type == "sout" and not tender_info.get("rm_total"):
            estimated = int(round(nmck / price_per_unit))
            if estimated > 0:
                tender_info["rm_total"] = estimated
                tender_info["rm_total_source"] = "nmck_estimate"
                logger.info(
                    f"[{FallbackService.VERSION}] FALLBACK sout: "
                    f"estimated_rm={estimated} (НМЦК {nmck:,.0f} / {price_per_unit})"
                )

        elif tender_type == "plk" and not tender_info.get("measurement_points"):
            estimated = int(round(nmck / price_per_unit))
            if estimated > 0:
                tender_info["measurement_points"] = estimated
                tender_info["points_count"] = estimated
                tender_info["points_source"] = "nmck_estimate"
                tender_info["measurement_points_source"] = "nmck_estimate"
                logger.info(
                    f"[{FallbackService.VERSION}] FALLBACK plk: "
                    f"estimated_points={estimated} (НМЦК {nmck:,.0f} / {price_per_unit})"
                )

        elif tender_type == "testing" and not tender_info.get("measurement_points"):
            estimated = int(round(nmck / price_per_unit))
            if estimated > 0:
                tender_info["measurement_points"] = estimated
                tender_info["points_count"] = estimated
                tender_info["points_source"] = "nmck_estimate_testing"
                tender_info["measurement_points_source"] = "nmck_estimate_testing"
                logger.info(
                    f"[{FallbackService.VERSION}] FALLBACK testing: "
                    f"estimated_points={estimated}"
                )

        elif tender_type == "education":
            programs = tender_info.get("programs")
            if programs and not tender_info.get("students_count"):
                total_unit_sum = tender_info.get("total_unit_price_sum")
                if total_unit_sum and total_unit_sum > 0:
                    estimated = int(round(nmck / total_unit_sum))
                else:
                    estimated = int(round(nmck / price_per_unit))

                if estimated > 0:
                    tender_info["students_count"] = estimated
                    tender_info["estimated_students"] = estimated
                    tender_info["students_count_source"] = "nmck_estimate"

                    protocol_programs = [
                        p for p in programs if p.get("doc_type") == "protocol"
                    ]
                    if protocol_programs and not tender_info.get("protocols_count"):
                        tender_info["protocols_count"] = estimated

                    contract_end = tender_info.get("contract_end_date")
                    if contract_end and not tender_info.get("contract_months"):
                        try:
                            end_date = datetime.strptime(str(contract_end), "%Y-%m-%d")
                            now = datetime.now()
                            months = max(
                                1,
                                (end_date.year - now.year) * 12
                                + end_date.month
                                - now.month,
                            )
                            tender_info["contract_months"] = months
                            tender_info["delivery_count"] = months
                        except Exception as e:
                            logger.debug(
                                f"[{FallbackService.VERSION}] "
                                f"Ошибка парсинга contract_end_date: {e}"
                            )

                    logger.info(
                        f"[{FallbackService.VERSION}] Programs→scalar: "
                        f"estimated_students={estimated}"
                    )

            elif not programs and not tender_info.get("students_count"):
                estimated = int(round(nmck / price_per_unit))
                if estimated > 0:
                    tender_info["students_count"] = estimated
                    tender_info["estimated_students"] = estimated
                    tender_info["students_count_source"] = "nmck_estimate"
                    if not tender_info.get("protocols_count"):
                        tender_info["protocols_count"] = estimated
                    logger.info(
                        f"[{FallbackService.VERSION}] FALLBACK education: "
                        f"estimated_students={estimated} "
                        f"(НМЦК {nmck:,.0f} / {price_per_unit})"
                    )

        elif tender_type == "opr" and not tender_info.get("opr_positions"):
            estimated = int(round(nmck / price_per_unit))
            estimated = min(estimated, 500)
            # НМЦК > 5 млн при «ОПР» почти наверняка чужой профиль
            if nmck >= 5_000_000:
                tender_info["needs_manual_review"] = True
                tender_info["agent_blocked"] = True
                tender_info["agent_block_reason"] = (
                    f"Подозрительно большой НМЦК для ОПР ({nmck:,.0f} ₽) — "
                    "возможна диагностика/стройка, не оценка рисков"
                )
                logger.warning(
                    f"[{FallbackService.VERSION}] OPR blocked: NMCK={nmck:,.0f}"
                )
                return tender_info
