#!/usr/bin/env python3
"""
main.py
Интеграционный скрипт TENDER-BOT v7.8.0.
Пайплайн: Поиск -> Детальный парсинг -> LLM-анализ -> Расчёт -> Риски -> Google Sheets
Запуск:
  python main.py --analyze --max-results 10
  python main.py --analyze --max-results 5 --opr
  python main.py --analyze --max-results 5 --sout --plk
  python main.py --analyze --max-results 5 --types education,sout
"""

import sys
import os
import argparse
import time
import json
import csv
from pathlib import Path
from datetime import datetime
from loguru import logger

from core.daily_limiter import DailyLimiter

# === ЛОГИРОВАНИЕ ===
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger.remove()

logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
)

logger.add(
    "tender.log",
    level="DEBUG",
    rotation="10 MB",
    retention="7 days",
    encoding="utf-8",
    backtrace=True,
    diagnose=True,
)

logger.add(
    LOG_DIR / "run_{time:YYYYMMDD_HHmmss}.log",
    level="DEBUG",
    rotation=None,
    retention="30 days",
    encoding="utf-8",
    backtrace=True,
    diagnose=True,
)

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
from core.google_sheets import SHEET_COLUMNS, build_sheets_row, get_sheets_manager
from core.search import create_searcher
from core.parsers import DetailedParser
from core.analysis import TenderAnalyzer
from utils.formatters import parse_deadline_to_days, log_pipeline_summary


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
                f"   РМ: {tender_info['rm_total']} "
                f"({tender_info.get('rm_total_source', 'парсер')})"
            )
        if tender_info.get("students_count"):
            ktru_parts.append(
                f"   Слушатели: {tender_info['students_count']} "
                f"({tender_info.get('students_count_source', 'парсер')})"
            )
        if tender_info.get("points_count"):
            ktru_parts.append(
                f"   Точки ПЛК: {tender_info['points_count']} "
                f"({tender_info.get('points_source', 'парсер')})"
            )
        if tender_info.get("opr_positions"):
            ktru_parts.append(
                f"   Должности ОПР: {tender_info['opr_positions']} "
                f"({tender_info.get('opr_positions_source', 'парсер')})"
            )

        if ktru_parts:
            parts.extend(["", "[ДАННЫЕ КТРУ]:"] + ktru_parts)

    if documents_text and len(documents_text) > 100:
        parts.append(f"\nТЕКСТ ДОКУМЕНТОВ:\n{documents_text[:12000]}")
    else:
        parts.append("\nДОКУМЕНТЫ: не извлечены")

    return "\n".join(parts)


def _guess_type_hint(tender, detail) -> str | None:
    """Быстрый hint по КТРУ / title до полного analyze (для type_filter)."""
    if detail:
        if (getattr(detail, "students_count", 0) or 0) > 0:
            return "education"
        if (getattr(detail, "rm_total", 0) or 0) > 0:
            return "sout"
        if (getattr(detail, "points_count", 0) or 0) > 0:
            return "plk"
        if (getattr(detail, "opr_positions", 0) or 0) > 0:
            return "opr"
        hint = getattr(detail, "tender_type_hint", None)
        if hint:
            return hint

    title = (getattr(tender, "title", None) or "").lower()
    if not title:
        return None

    try:
        from core.services.type_service import TypeService

        ts = TypeService()
        for ttype, keywords in getattr(ts, "TITLE_KEYWORDS", {}).items():
            if any(kw in title for kw in keywords):
                return ttype
        # NEGATIVE → other
        for neg in getattr(ts, "NEGATIVE_KEYWORDS", []):
            if neg in title:
                return "other"
    except Exception:
        pass

    return None


def run_analyze(
    max_pages: int = None,
    max_results: int = None,
    skip_detail: bool = False,
    type_filter: list = None,
):
    """Полный анализ с LLM."""
    logger.info("=" * 60)
    logger.info(
        " РЕЖИМ: Полный анализ с LLM. "
        "Логирование 'мышления' агента включено: logs/agent_thinking_*.log"
    )
    limiter = DailyLimiter()
    logger.info(limiter.get_status())

    type_filter = [t.lower().strip() for t in (type_filter or []) if t]
    if type_filter:
        logger.info(f" Фильтр типов: {', '.join(type_filter)}")

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

    try:
        cache_db = Path(__file__).resolve().parent / "data" / "tender_cache.db"
        cache = TenderCache(db_path=cache_db)
        logger.debug(f" Кэш инициализирован: {cache_db}")
    except Exception as e:
        logger.warning(f" Кэш не инициализирован: {e}")
        cache = None

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
            sheets_manager = get_sheets_manager()
            logger.info(" Google Sheets подключен")
        except Exception as e:
            logger.warning(f" Google Sheets не подключен: {e}")

    logger.info(" Начинаю поиск тендеров...")
    results = []
    sheets_rows = []
    analyzed_count = 0
    duplicates_skipped = 0
    type_skipped = 0
    added_to_sheets_count = 0
    errors_count = 0
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
                        f"   Детали получены: "
                        f"{detail.customer_region or 'регион не определён'}"
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
                            f"   Используется полный текст документов "
                            f"({len(documents_text)} симв.)"
                        )
                else:
                    logger.warning("   Детальный парсинг вернул None")
            except Exception as e:
                logger.error(f"   Ошибка детального парсинга: {e}")
                detail = None

        # === Ранний type_filter (до LLM, экономия токенов) ===
        early_hint = _guess_type_hint(tender, detail)
        if type_filter and early_hint and early_hint not in type_filter:
            logger.info(
                f"   Пропуск по типу (early): {early_hint} ∉ {type_filter} "
                f"({tender.tender_id})"
            )
            type_skipped += 1
            # не кладём в кэш — иначе при смене фильтра тендер пропадёт
            continue

        # === ШАГ 2: tender_info ===
        tender_info = {}
        if detail:
            tender_info = {
                "reg_number": tender.tender_id,
                "purchase_name": detail.purchase_name or tender.title,
                "customer_name": detail.customer_name or tender.customer or "",
                "customer_region": detail.customer_region
                or getattr(tender, "region", "")
                or "",
                "region": detail.customer_region or getattr(tender, "region", "") or "",
                "nmck": detail.nmck or tender.nmck or 0,
                "deadline_date": detail.deadline_date or tender.deadline_date or "",
                "deadline_days": parse_deadline_to_days(
                    detail.deadline_date or tender.deadline_date or ""
                ),
                "platform_name": detail.platform_name or "",
                "requirements": detail.requirements or "",
                "application_guarantee": (detail.application_guarantee or "").strip(),
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
                tender_info["points_source"] = "ktru"
            if (detail.opr_positions or 0) > 0:
                tender_info["opr_positions"] = detail.opr_positions
                tender_info["opr_positions_source"] = "ktru"
            if (getattr(detail, "opr_persons", 0) or 0) > 0:
                tender_info["opr_persons"] = detail.opr_persons

            if getattr(detail, "has_full_time", False):
                tender_info["has_full_time"] = True

            tender_info["needs_subcontractor"] = getattr(
                detail, "needs_subcontractor", False
            )

        # === ШАГ 3: Fallback-текст ===
        if not tender_text:
            if detail:
                tender_text = build_tender_text(detail, documents_text, tender_info)
            else:
                doc_part = (
                    f"\n\nТЕКСТ ДОКУМЕНТОВ:\n{documents_text[:12000]}"
                    if documents_text and len(documents_text) > 100
                    else ""
                )
                tender_text = (
                    f"НАЗВАНИЕ ЗАКУПКИ:\n{tender.title}\n\n"
                    f"ЗАКАЗЧИК:\n{tender.customer or 'не указан'}\n\n"
                    f"РЕГИОН:\n{getattr(tender, 'region', '') or 'не указан'}\n\n"
                    f"НМЦК:\n{tender.nmck or 'не указана'}\n\n"
                    f"ЗАКОН:\n{tender.law}{doc_part}"
                )
                logger.info("   Используется упрощённый текст (title only)")

        # === ШАГ 4: LLM-анализ ===
        try:
            type_hint = early_hint
            if detail and getattr(detail, "tender_type_hint", None):
                type_hint = detail.tender_type_hint or type_hint

            if detail and (detail.students_count or 0) > 0 and type_hint == "sout":
                logger.warning(
                    f"[v7.2.4] Override: sout → education "
                    f"(КТРУ дал {detail.students_count} слушателей)"
                )
                type_hint = "education"

            logger.debug(
                f"[DEBUG] analyzer: nmck={tender.nmck}, "
                f"students={tender_info.get('students_count', 'N/A')}, "
                f"rm={tender_info.get('rm_total', 'N/A')}, hint={type_hint}"
            )

            analysis = analyzer.analyze(
                tender_info=tender_info,
                documents_text=documents_text or tender_text,
                llm_classification=None,
                llm_confidence=0.0,
                tender_type_hint=type_hint,
            )

            # Финальный type_filter (после analyze — на случай смены типа агентом)
            ttype = (getattr(analysis, "tender_type", "") or "").lower()
            if type_filter and ttype and ttype not in type_filter:
                logger.info(
                    f"   Пропуск по типу (post-analyze): {ttype} ∉ {type_filter} "
                    f"({tender.tender_id})"
                )
                type_skipped += 1
                continue

            result_dict = analysis.to_dict()
            row = build_sheets_row(analysis, detail, tender)
            sheets_rows.append(row)

            analyzed_count += 1
            limiter.add_to_cache(tender.tender_id)
            results.append(result_dict)

            if sheets_manager:
                try:
                    was_added = sheets_manager.add_tender_to_top(
                        row, check_duplicate=False
                    )
                    if was_added:
                        added_to_sheets_count += 1
                        logger.info("   Записано в Google Sheets")
                except Exception as e:
                    logger.warning(f"   Ошибка записи в Sheets: {e}")

            print(f"\n{'=' * 60}\n РЕЗУЛЬТАТ: {tender.tender_id}\n{'=' * 60}")
            print(f"Тип: {analysis.tender_type} | НМЦК: {analysis.nmck:,.0f} ₽")
            print(
                f"Себестоимость: {analysis.cost_price:,.0f} ₽ | "
                f"Цена: {analysis.recommended_price:,.0f} ₽"
            )
            print(
                f"Маржа: {analysis.margin_percent:.1f}% | "
                f"Риск: {analysis.risk_level} | Решение: {analysis.decision}"
            )
            if getattr(analysis, "needs_manual_review", False):
                print(" ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА")
            if detail and detail.customer_region:
                print(f"Регион: {detail.customer_region}")
            comment = analysis.comment or ""
            print(
                f"{'-' * 60}\nКомментарий:\n"
                f"{comment[:400] + '...' if len(comment) > 400 else comment}\n"
                f"{'=' * 60}"
            )

        except Exception as e:
            errors_count += 1
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
                    "type_skipped": type_skipped,
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

    log_pipeline_summary(
        {
            "total": analyzed_count + duplicates_skipped + type_skipped,
            "processed": analyzed_count,
            "skipped": duplicates_skipped + type_skipped,
            "added_to_sheets": added_to_sheets_count,
            "errors": errors_count,
        }
    )
    if type_skipped:
        logger.info(f"   • Пропущено по type_filter: {type_skipped}")

    limiter.record_tenders(len(results))
    return results, sheets_rows


def main():
    parser = argparse.ArgumentParser(
        description="TENDER-BOT v7.8.0: Анализ тендеров с ИИ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python main.py --analyze --max-results 5 --opr\n"
            "  python main.py --analyze --max-results 10 --sout --plk\n"
            "  python main.py --analyze --max-results 5 --types education,sout\n"
        ),
    )

    parser.add_argument(
        "--analyze",
        action="store_true",
        required=True,
        help="Запустить полный анализ с LLM",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None, help="Максимум страниц поиска"
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Максимум тендеров для полной обработки",
    )
    parser.add_argument(
        "--skip-detail",
        action="store_true",
        help="Быстрый анализ без глубокого скачивания документов",
    )
    parser.add_argument(
        "--types",
        type=str,
        default=None,
        help="Типы через запятую: sout,opr,plk,education (или all)",
    )
    parser.add_argument("--sout", action="store_true", help="Только СОУТ")
    parser.add_argument("--opr", action="store_true", help="Только ОПР")
    parser.add_argument("--plk", action="store_true", help="Только ПЛК")
    parser.add_argument("--education", action="store_true", help="Только обучение")

    args = parser.parse_args()

    type_filter = []
    if args.types and args.types.strip().lower() not in ("all", "*", ""):
        type_filter.extend(
            x.strip().lower() for x in args.types.split(",") if x.strip()
        )
    if args.sout:
        type_filter.append("sout")
    if args.opr:
        type_filter.append("opr")
    if args.plk:
        type_filter.append("plk")
    if args.education:
        type_filter.append("education")
    type_filter = list(dict.fromkeys(type_filter))

    if args.analyze:
        run_analyze(
            max_pages=args.max_pages,
            max_results=args.max_results,
            skip_detail=args.skip_detail,
            type_filter=type_filter or None,
        )


if __name__ == "__main__":
    main()
