#!/usr/bin/env python3
"""
main.py
Интеграционный скрипт TENDER-BOT v7.7.0.
Пайплайн: Поиск -> Детальный парсинг -> LLM-анализ -> Расчёт -> Риски -> Google Sheets

v7.7.0: Добавлено сквозное логирование "мышления" агента и MCP-вызовов.
"""

import sys
import os
import argparse
import time
import string
import json
import csv
from pathlib import Path
from datetime import datetime
from core.daily_limiter import DailyLimiter
from loguru import logger

# === ЛОГИРОВАНИЕ ===
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Удаляем стандартные хендлеры
logger.remove()

# Стандартный вывод в консоль
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
)

# Общий лог файл
logger.add(
    "tender.log",
    level="DEBUG",
    rotation="10 MB",
    retention="7 days",
    encoding="utf-8",
    backtrace=True,
    diagnose=True,
)

# Лог запуска
logger.add(
    LOG_DIR / "run_{time:YYYYMMDD_HHmmss}.log",
    level="DEBUG",
    rotation=None,
    retention="30 days",
    encoding="utf-8",
    backtrace=True,
    diagnose=True,
)

# === НОВОЕ: Отдельный лог для "мышления" агента ===
logger.add(
    LOG_DIR / "agent_thinking_{time:YYYYMMDD}.log",
    level="DEBUG",
    rotation="10 MB",
    retention="30 days",
    encoding="utf-8",
    format="{time:HH:mm:ss} | {level} | {message}",
    filter=lambda record: record["extra"].get("type") == "agent_thinking",
)

# === ИМПОРТЫ ===
from core.calculation.calculator import TenderCalculator
from core.risk_rules import RiskAnalyzer
from core.tender_type import get_type_detector
from core.google_sheets import SHEET_COLUMNS
from utils.price_parser import (
    format_for_sheets as _format_nmck,
    format_for_sheets as _format_price,
)
from core.search import create_searcher
from core.parsers import DetailedParser
from core.analysis import TenderAnalyzer
from utils.formatters import (
    parse_deadline_to_days,
    sanitize_for_sheets,
    get_quantity,
    build_calculation_breakdown,
    build_short_recommendation,
)


def build_tender_text(detail, documents_text: str, tender_info: dict = None) -> str:
    """Строит структурированный текст тендера для LLM с расширенным контекстом КТРУ."""
    parts = [
        f"НАЗВАНИЕ ЗАКУПКИ: {detail.purchase_name or detail.tender_id}",
        f"ЗАКАЗЧИК: {detail.customer_name or 'не указан'}",
        f"РЕГИОН: {detail.customer_region or 'не указан'}",
        f"НМЦК: {detail.nmck:,.2f} ₽" if detail.nmck else "НМЦК: не указана",
        f"ЭТП: {detail.platform_name or 'не указана'}",
        f"СРОК ПОДАЧИ ЗАЯВОК: {detail.deadline_date or 'не указан'}",
    ]

    if tender_info:
        ktru_parts = []
        if tender_info.get("rm_total"):
            ktru_parts.append(
                f"  РМ: {tender_info['rm_total']} ({tender_info.get('rm_total_source', 'парсер')})"
            )
        if tender_info.get("students_count"):
            ktru_parts.append(
                f"  Слушатели: {tender_info['students_count']} ({tender_info.get('students_count_source', 'парсер')})"
            )
        if tender_info.get("points_count"):
            ktru_parts.append(f"  Точки ПЛК: {tender_info['points_count']}")
        if tender_info.get("opr_positions"):
            ktru_parts.append(f"  Должности ОПР: {tender_info['opr_positions']}")

        if ktru_parts:
            parts.extend(["", "[ДАННЫЕ КТРУ]:"] + ktru_parts)

    if documents_text and len(documents_text) > 100:
        parts.append(f"\nТЕКСТ ДОКУМЕНТОВ:\n{documents_text[:12000]}")
    else:
        parts.append("\nДОКУМЕНТЫ: не извлечены")

    return "\n".join(parts)


def get_guarantee_info(detail, analysis) -> tuple:
    """Извлекает информацию об обеспечении заявки и контракта."""
    app_guarantee = contract_guarantee = guarantee_method = ""

    if detail:
        app_guarantee = (detail.application_guarantee or "").strip()
        contract_guarantee = (detail.contract_guarantee or "").strip()
        guarantee_method = (detail.guarantee_method or "").strip()

    if analysis and hasattr(analysis, "details") and analysis.details:
        d = analysis.details
        if isinstance(d, dict):
            app_guarantee = app_guarantee or d.get("application_guarantee", "")
            contract_guarantee = contract_guarantee or d.get("contract_guarantee", "")
            guarantee_method = guarantee_method or d.get("guarantee_method", "")
        else:
            app_guarantee = app_guarantee or getattr(d, "application_guarantee", "")
            contract_guarantee = contract_guarantee or getattr(
                d, "contract_guarantee", ""
            )
            guarantee_method = guarantee_method or getattr(d, "guarantee_method", "")

    if not app_guarantee:
        app_guarantee = "Не требуется"
    if not contract_guarantee:
        contract_guarantee = "Не требуется"
    if not guarantee_method:
        guarantee_method = "Не требуется"

    return app_guarantee, contract_guarantee, guarantee_method


def build_sheets_row(analysis, detail, tender) -> dict:
    """Формирует строку для Google Sheets с приоритетом данных парсера."""

    # === FIX #22: Берем количество из detail (парсера), если оно есть ===
    quantity = None
    t_type = analysis.tender_type

    if detail:
        if t_type == "sout":
            quantity = getattr(detail, "rm_total", None)
        elif t_type == "plk":
            quantity = getattr(detail, "points_count", None)
        elif t_type == "education":
            quantity = getattr(detail, "students_count", None)
        elif t_type == "opr":
            # Для ОПР часто используется rm_total или opr_positions в detail
            quantity = getattr(detail, "opr_positions", None) or getattr(
                detail, "rm_total", None
            )

    # Если в detail нет (или 0), берем из analysis (там может быть fallback или данные агента)
    if not quantity:
        quantity = get_quantity(analysis)

    # Финальная защита от None/0
    if not quantity:
        quantity = "?"
    # ============================================================

    app_guarantee, contract_guarantee, guarantee_method = get_guarantee_info(
        detail, analysis
    )

    law = tender.law.replace("-FZ", "-ФЗ") if tender.law else ""
    procurement = (
        f"{detail.purchase_method}, {law}" if detail and detail.purchase_method else law
    )

    ai_comment = sanitize_for_sheets(analysis.comment or "")
    purchase_name = tender.title or ""
    if detail and detail.purchase_name and len(detail.purchase_name) > 10:
        purchase_name = detail.purchase_name

    etp_name = ""
    if detail:
        etp_name = detail.platform_name or detail.etp or ""
    if not etp_name and hasattr(tender, "etp") and tender.etp:
        etp_name = tender.etp

    deadline = (
        detail.deadline_date
        if detail and detail.deadline_date
        else getattr(tender, "deadline_date", "")
    )

    decision_override = analysis.decision
    if hasattr(analysis, "details") and analysis.details:
        d = analysis.details
        is_forbidden = (
            isinstance(d, dict) and d.get("_forbidden_direction")
        ) or getattr(d, "_forbidden_direction", False)
        if is_forbidden:
            decision_override = "не рекомендуется"
            ai_comment = "[FORBIDDEN] ЗАПРЕЩЁННОЕ НАПРАВЛЕНИЕ: " + ai_comment

    return {
        "ID тендера": tender.tender_id,
        "Ссылка на тендер": tender.url,
        "Наименование услуг": purchase_name,
        "Способ проведения закупки": procurement,
        "ЭТП": etp_name,
        "Комиссия ЭТП": (
            f"{analysis.details.get('etp_commission', 0):,.0f} ₽"
            if analysis.details
            else ""
        ),
        "Регион": (
            detail.customer_region if detail else getattr(tender, "region", "") or ""
        ),
        "Обеспечение заявки": app_guarantee,
        "Обеспечение контракта": contract_guarantee,
        "Способ обеспечения исполнения": guarantee_method,
        "Срок подачи заявки до": deadline,
        "НМЦК": _format_nmck((detail.nmck if detail else 0) or analysis.nmck),
        "Количество": quantity,
        "Цена предложения": _format_price(analysis.recommended_price),
        "Возможности экономии": "",
        "Решение по участию": decision_override,
        "Расчёты": sanitize_for_sheets(build_calculation_breakdown(analysis)),
        "Комментарий от ИИ-агента": ai_comment,
        "Рекомендации": sanitize_for_sheets(build_short_recommendation(analysis)),
        "Комментарии руководителя отдела по участию": "",
        "Дата заключения контракта": "",
        "Дата выполнения работ": "",
        "Результат": "",
    }


def run_parse_only(max_pages: int = None, max_results: int = None):
    """Режим только парсинга (без LLM)."""
    logger.info("=" * 60)
    logger.info(" РЕЖИМ: Только парсинг (без LLM)")
    logger.info("=" * 60)

    searcher = create_searcher()
    output_file = f"data/search_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results = searcher.search_and_save(
        output_file=output_file, max_pages=max_pages, max_results=max_results
    )

    logger.info(f" Найдено тендеров: {len(results)}")
    logger.info(f" Сохранено в: {output_file}")
    return results


def run_analyze(
    max_pages: int = None, max_results: int = None, skip_detail: bool = False
):
    """Полный анализ с LLM. v7.7.0: с логированием мышления агента."""
    logger.info("=" * 60)
    logger.info(" РЕЖИМ: Полный анализ с LLM")
    logger.info("  Логирование 'мышления' агента включено: logs/agent_thinking_*.log")

    limiter = DailyLimiter()
    logger.info(limiter.get_status())

    can_run, reason = limiter.can_run()
    if not can_run:
        logger.error(f" Запуск отменён: {reason}")
        return [], []

    limiter.cleanup_all()
    logger.info("=" * 60)

    from config.settings import settings
    from core.tender_cache import TenderCache

    errors = settings.validate()
    if errors:
        logger.error(" Ошибки конфигурации:")
        for err in errors:
            logger.error(f"   • {err}")
        sys.exit(1)

    searcher = create_searcher()
    calculator = TenderCalculator()
    risk_analyzer = RiskAnalyzer()
    type_detector = get_type_detector()
    analyzer = TenderAnalyzer(
        calculator=calculator, risk_analyzer=risk_analyzer, type_detector=type_detector
    )

    cache = None
    try:
        cache_db = Path(__file__).resolve().parent / "data" / "tender_cache.db"
        cache = TenderCache(db_path=cache_db)
        logger.debug(f" Кэш инициализирован: {cache_db}")
    except Exception as e:
        logger.warning(f" Кэш не инициализирован: {e}")

    detailed = None
    if not skip_detail:
        try:
            detailed = DetailedParser(session_manager=searcher.session_manager)
            logger.debug(" DetailedParser инициализирован")
        except Exception as e:
            logger.warning(f" DetailedParser не инициализирован: {e}")

    sheets_manager = None
    if os.getenv("GOOGLE_SHEETS_ENABLED", "True").lower() in ("false", "0", "no"):
        logger.info(" GOOGLE SHEETS ОТКЛЮЧЕН через GOOGLE_SHEETS_ENABLED=False")
    else:
        try:
            from core.google_sheets import get_sheets_manager

            sheets_manager = get_sheets_manager()
            logger.info(" Google Sheets подключен")
        except Exception as e:
            logger.warning(f" Google Sheets не подключен: {e}")

    logger.info(" Начинаю поиск тендеров...")
    results = []
    sheets_rows = []
    analyzed_count = 0
    duplicates_skipped = 0
    logger.info(" Прогрев сессий... Ждем 5 секунд перед первым запросом")
    time.sleep(5)
    for tender in searcher.search(max_pages=max_pages, max_results=None):
        if limiter.is_cached(tender.tender_id):
            logger.debug(f" Кэш: {tender.tender_id}")
            duplicates_skipped += 1
            continue

        if sheets_manager and sheets_manager.check_exists(tender.tender_id):
            logger.info(f"   Уже в Sheets: {tender.tender_id}")
            limiter.add_to_cache(tender.tender_id)
            duplicates_skipped += 1
            continue

        max_per_run = max_results or limiter.MAX_PER_RUN
        if analyzed_count >= max_per_run:
            logger.info(
                f" Достигнут лимит новых тендеров: {analyzed_count}/{max_per_run}"
            )
            break

        logger.info(f"\n{'-' * 60}")
        logger.info(f" {tender.tender_id} | {tender.law}")
        logger.info(f" {tender.title[:80]}...")
        logger.info(
            f" НМЦК: {tender.nmck:,.0f} ₽" if tender.nmck else " НМЦК: не указана"
        )

        detail = None
        documents_text = ""
        tender_text = ""

        # === ШАГ 1: Детальный парсинг ===
        if detailed and not skip_detail:
            try:
                law_clean = tender.law.replace("-FZ", "") if tender.law else "44"
                detail = detailed.fetch_and_parse(
                    reg_number=tender.tender_id,
                    law_type=law_clean,
                    notice_guid=getattr(tender, "notice_guid", None) or "",
                    nmck=tender.nmck or 0,
                    fallback_title=tender.title or "",
                    fallback_region=getattr(tender, "region", "") or "",
                    fallback_customer=getattr(tender, "customer", "") or "",
                )

                if detail:
                    logger.info(
                        f"   Детали получены: {detail.customer_region or 'регион не определён'}"
                    )
                    logger.info(f"   Документов: {len(detail.documents)}")
                    logger.info(
                        f"   ЭТП: {detail.platform_name or detail.etp or 'не определена'}"
                    )

                    documents_text = ""
                    if detail.documents:
                        try:
                            from core.document_processor import (
                                DocumentProcessor,
                                DocumentInfo,
                            )

                            doc_processor = DocumentProcessor(
                                session=searcher.session_manager.get_primary_session()
                            )
                            docs = [
                                DocumentInfo(
                                    name=d.get("name", ""),
                                    url=d.get("link", ""),
                                    file_url=d.get("link", ""),
                                    file_type=d.get("file_type", ""),
                                )
                                for d in detail.documents
                            ]
                            documents_text = doc_processor.process_documents(
                                docs, tender_id=tender.tender_id
                            )
                            detail.documents_text = documents_text
                            logger.info(
                                f"   Текст документов: {len(documents_text)} симв."
                            )
                        except Exception as e:
                            logger.warning(f"   Ошибка обработки документов: {e}")

                    if not documents_text:
                        documents_text = detail.documents_text or ""
                    if documents_text and len(documents_text) > 1000:
                        tender_text = documents_text
                        logger.info(
                            f"   Используется полный текст документов ({len(documents_text)} симв.)"
                        )
                    else:
                        tender_text = build_tender_text(detail, documents_text)
                else:
                    logger.warning(f"   Детальный парсинг вернул None")
            except Exception as e:
                logger.error(f"   Ошибка детального парсинга: {e}")
                detail = None

        # === ШАГ 2: Fallback — упрощённый текст ===
        if not tender_text:
            doc_part = (
                f"\n\nТЕКСТ ДОКУМЕНТОВ:\n{documents_text[:12000]}"
                if documents_text and len(documents_text) > 100
                else ""
            )
            tender_text = f"НАЗВАНИЕ ЗАКУПКИ:\n{tender.title}\n\nЗАКАЗЧИК:\n{tender.customer or 'не указан'}\n\nРЕГИОН:\n{getattr(tender, 'region', '') or 'не указан'}\n\nНМЦК:\n{tender.nmck or 'не указана'}\n\nЗАКОН:\n{tender.law}{doc_part}"
            logger.info("   Используется упрощённый текст (title only)")

        # === ШАГ 3: LLM-анализ ===
        try:
            tender_info = {}
            if detail:
                tender_info = {
                    "reg_number": tender.tender_id,
                    "purchase_name": detail.purchase_name or tender.title,
                    "customer_name": detail.customer_name or tender.customer or "",
                    "customer_region": detail.customer_region
                    or getattr(tender, "region", "")
                    or "",
                    "region": detail.customer_region
                    or getattr(tender, "region", "")
                    or "",
                    "nmck": detail.nmck or tender.nmck or 0,
                    "deadline_date": detail.deadline_date or tender.deadline_date or "",
                    "deadline_days": parse_deadline_to_days(
                        detail.deadline_date or tender.deadline_date or ""
                    ),
                    "platform_name": detail.platform_name or "",
                    "requirements": detail.requirements or "",
                    "application_guarantee": (
                        detail.application_guarantee or ""
                    ).strip(),
                    "contract_guarantee": (detail.contract_guarantee or "").strip(),
                    "guarantee_method": (detail.guarantee_method or "").strip(),
                    "cities_count": detail.cities_count or 1,
                    "regions_count": detail.regions_count or 1,
                    "addresses_count": detail.addresses_count or 1,
                    "is_annual": bool(getattr(detail, "is_annual", False)),
                }

                if not tender_info.get("region"):
                    tender_info["region"] = (
                        tender_info.get("customer_region", "")
                        or getattr(tender, "region", "")
                        or ""
                    )
                if detail.tender_type_hint == "education":
                    tender_info["addresses_count"] = 1

                if (detail.rm_total or 0) > 0:
                    tender_info["rm_total"] = detail.rm_total
                    tender_info["rm_total_source"] = "ktru"
                if (detail.students_count or 0) > 0:
                    tender_info["students_count"] = detail.students_count
                    tender_info["students_count_source"] = "ktru"
                if (detail.points_count or 0) > 0:
                    tender_info["points_count"] = detail.points_count

                if detail.has_full_time:
                    tender_info["has_full_time"] = True
                    tender_info["is_distance"] = False
                for field in [
                    "teacher_days",
                    "accommodation_nights",
                    "transport_km",
                    "venue_rent_days",
                    "manikin_days",
                ]:
                    val = getattr(detail, field, 0) or 0
                    if val > 0:
                        tender_info[field] = val

                if (detail.trip_days or 0) > 0:
                    tender_info["trip_days"] = detail.trip_days
                tender_info["is_seasonal"] = bool(getattr(detail, "is_seasonal", False))
                if (detail.opr_positions or 0) > 0:
                    tender_info["opr_positions"] = detail.opr_positions
                if (detail.opr_persons or 0) > 0:
                    tender_info["opr_persons"] = detail.opr_persons
                tender_info["needs_subcontractor"] = getattr(
                    detail, "needs_subcontractor", False
                )

            type_hint = detail.tender_type_hint if detail else None
            logger.debug(
                f"[DEBUG] Passing to analyzer: nmck={tender.nmck}, students={tender_info.get('students_count', 'N/A')}, rm={tender_info.get('rm_total', 'N/A')}, hint={type_hint}"
            )

            if not type_hint and tender.title:
                from core.services.type_service import TypeService

                _ts = TypeService()
                _title_lower = tender.title.lower()
                for _ttype, _keywords in _ts.TITLE_KEYWORDS.items():
                    if any(_kw in _title_lower for _kw in _keywords):
                        type_hint = _ttype
                        break

            if detail and (detail.students_count or 0) > 0 and type_hint == "sout":
                logger.warning(
                    f"[v7.2.4] Override: sout → education (КТРУ дал {detail.students_count} слушателей)"
                )
                type_hint = "education"

            if detail and not tender_text:
                tender_text = build_tender_text(detail, documents_text, tender_info)

            analysis = analyzer.analyze(
                tender_info=tender_info,
                documents_text=documents_text or tender_text,
                llm_classification=None,
                llm_confidence=0.0,
                tender_type_hint=type_hint,
            )

            result_dict = analysis.to_dict()
            row = build_sheets_row(analysis, detail, tender)
            sheets_rows.append(row)

            analyzed_count += 1
            limiter.add_to_cache(tender.tender_id)
            results.append(result_dict)

            if sheets_manager:
                try:
                    sheets_manager.add_tender_to_top(row, check_duplicate=False)
                    logger.info(f"   Записано в Google Sheets")
                except Exception as e:
                    logger.warning(f"   Ошибка записи в Sheets: {e}")

            print(f"\n{'=' * 60}\n РЕЗУЛЬТАТ: {tender.tender_id}\n{'=' * 60}")
            print(f"Тип: {analysis.tender_type} | НМЦК: {analysis.nmck:,.0f} ₽")
            print(
                f"Себестоимость: {analysis.cost_price:,.0f} ₽ | Цена: {analysis.recommended_price:,.0f} ₽"
            )
            print(
                f"Маржа: {analysis.margin_percent:.1f}% | Риск: {analysis.risk_level} | Решение: {analysis.decision}"
            )
            if getattr(analysis, "needs_manual_review", False):
                print(" ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА")
            if detail and detail.customer_region:
                print(f"Регион: {detail.customer_region}")
            print(
                f"{'-' * 60}\nКомментарий:\n{analysis.comment[:400] + '...' if len(analysis.comment) > 400 else analysis.comment}\n{'=' * 60}"
            )

        except Exception as e:
            logger.error(f" Ошибка анализа тендера {tender.tender_id}: {e}")
            import traceback

            logger.error(traceback.format_exc())
            continue

    # === Сохранение результатов ===
    if results:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_file = f"data/analysis_results_{timestamp}.json"
        Path(json_file).parent.mkdir(parents=True, exist_ok=True)
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "analysis_date": datetime.now().isoformat(),
                    "total": len(results),
                    "duplicates_skipped": duplicates_skipped,
                    "results": results,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        logger.info(f"\n JSON сохранён: {json_file}")

        csv_file = f"data/sheets_export_{timestamp}.csv"
        with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sheets_rows)
        logger.info(f" CSV сохранён: {csv_file}")

        print(f"\n{'=' * 60}\n СВОДКА\n{'=' * 60}")
        print(
            f"Всего уникальных: {len(results)} | Дубликатов пропущено: {duplicates_skipped}"
        )
        decisions = {}
        for r in results:
            decisions[r.get("decision", "unknown")] = (
                decisions.get(r.get("decision", "unknown"), 0) + 1
            )
        for d, count in decisions.items():
            label = (
                "[OK]"
                if d == "рекомендуется"
                else "[BLOCK]" if d == "не участвуем" else ""
            )
            print(f"{label} {d}: {count}")

        risks = {}
        for r in results:
            risks[r.get("risk_level", "unknown")] = (
                risks.get(r.get("risk_level", "unknown"), 0) + 1
            )
        print(f"\nРиски:")
        for risk, count in risks.items():
            label = (
                "[LOW]" if risk == "low" else "[MED]" if risk == "medium" else "[HIGH]"
            )
            print(f"{label} {risk}: {count}")
        print(f"{'=' * 60}")

    logger.info(
        f" Итог поиска: проанализировано {analyzed_count}, пропущено из кэша {duplicates_skipped}"
    )
    limiter.record_tenders(len(results))
    return results, sheets_rows


def run_interactive():
    """Интерактивный режим."""
    from config.settings import settings

    errors = settings.validate()
    if errors:
        logger.error(" Ошибки конфигурации:")
        for err in errors:
            logger.error(f"   • {err}")
        sys.exit(1)

    calculator = TenderCalculator()
    risk_analyzer = RiskAnalyzer()
    type_detector = get_type_detector()
    analyzer = TenderAnalyzer(
        calculator=calculator, risk_analyzer=risk_analyzer, type_detector=type_detector
    )

    print("\n" + "=" * 60 + "\n ИНТЕРАКТИВНЫЙ РЕЖИМ\n" + "=" * 60)
    print("Вставьте текст тендера (ТЗ, извещение) и нажмите Enter дважды:")
    print("-" * 60)
    lines = []
    while True:
        try:
            line = input()
            if line.strip() == "" and lines and lines[-1].strip() == "":
                break
            lines.append(line)
        except EOFError:
            break

    tender_text = "\n".join(lines)
    if not tender_text.strip():
        print("Пустой текст. Отмена.")
        return

    print("\n Анализирую...")
    try:
        result = analyzer.analyze(
            tender_info={"documents_text": tender_text}, documents_text=tender_text
        )
        result_dict = result.to_dict()
        print("\n" + "=" * 60 + "\n РЕЗУЛЬТАТ АНАЛИЗА\n" + "=" * 60)
        print(f"Тип: {result_dict['tender_type']} | НМЦК: {result_dict['nmck']:,.0f} ₽")
        print(
            f"Себестоимость: {result_dict['cost_price']:,.0f} ₽ | Цена: {result_dict['recommended_price']:,.0f} ₽"
        )
        print(
            f"Маржа: {result_dict['margin_percent']:.1f}% | Риск: {result_dict['risk_level']} | Решение: {result_dict['decision']}"
        )
        if result_dict.get("needs_manual_review"):
            print(" ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА")
        if result_dict.get("llm_confidence") is not None:
            print(f"Уверенность ИИ: {result_dict['llm_confidence']:.2f}")
        print("-" * 60 + "\nРиски:")
        for flag in result_dict.get("red_flags", []):
            print(f"  • {flag}")
        print("-" * 60 + "\nКомментарий:\n" + result_dict["comment"] + "\n" + "=" * 60)
    except Exception as e:
        logger.error(f" Ошибка: {e}")
        import traceback

        logger.error(traceback.format_exc())


def main():
    parser = argparse.ArgumentParser(
        description="TENDER-BOT v7.7.0: Анализ тендеров с ИИ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Примеры:
  python main.py --parse-only --max-results 10        # Только поиск
  python main.py --analyze --max-pages 2               # Полный анализ
  python main.py --analyze --skip-detail               # Быстрый анализ
  python main.py --interactive                       # Ручной ввод
  python main.py --test                              # Тест калькулятора""",
    )

    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="Только поиск и парсинг тендеров (без LLM)",
    )
    parser.add_argument("--analyze", action="store_true", help="Полный анализ с LLM")
    parser.add_argument(
        "--skip-detail", action="store_true", help="Пропустить детальный парсинг"
    )
    parser.add_argument(
        "--interactive", action="store_true", help="Интерактивный режим"
    )
    parser.add_argument(
        "--test", action="store_true", help="Запуск тестов калькулятора"
    )
    parser.add_argument(
        "--max-pages", type=int, default=None, help="Максимум страниц поиска"
    )
    parser.add_argument(
        "--max-results", type=int, default=None, help="Максимум тендеров для обработки"
    )

    args = parser.parse_args()
    if not any([args.parse_only, args.analyze, args.interactive, args.test]):
        parser.print_help()
        sys.exit(0)

    if args.test:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from tests.test_calculator import run_all_tests

        success = run_all_tests()
        sys.exit(0 if success else 1)

    if args.parse_only:
        run_parse_only(max_pages=args.max_pages, max_results=args.max_results)
    elif args.analyze:
        run_analyze(
            max_pages=args.max_pages,
            max_results=args.max_results,
            skip_detail=args.skip_detail,
        )
    elif args.interactive:
        run_interactive()


if __name__ == "__main__":
    main()
