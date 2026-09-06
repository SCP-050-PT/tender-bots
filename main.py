#!/usr/bin/env python3
"""
main.py
Интеграционный скрипт TENDER-BOT v7.5.0.
Пайплайн: Поиск -> Детальный парсинг -> LLM-анализ -> Расчёт -> Риски -> Google Sheets

v7.5.0: Исправления
  - P1-4: Страховка _get_quantity с fallback по НМЦК (никаких ? в таблице)
  - Расширен контекст агента: добавлены данные КТРУ и структура НМЦК
  - Санитайзер эмодзи для Google Sheets (замена на [HIGH], [LOW] и т.д.)
  - Исправлен подсчет лимита (кэш не расходует слоты)
  - Очищены логи (кэш на DEBUG, добавлена итоговая статистика)
"""

import sys
import re
import argparse
import json
import csv
from pathlib import Path
from datetime import datetime
from core.daily_limiter import DailyLimiter

# === ЛОГИРОВАНИЕ (ТРИ источника) ===
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

try:
    from loguru import logger

    logger.remove()

    # 1. Консольный лог INFO (чистый, без отладочного шума)
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )

    # 2. Единый "живой" лог для мониторинга в реальном времени
    logger.add(
        "tender.log",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )

    # 3. Архивные логи с датой запуска
    logger.add(
        LOG_DIR / "run_{time:YYYYMMDD_HHmmss}.log",
        level="DEBUG",
        rotation=None,
        retention="30 days",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )

except ImportError:
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("tender_bot")


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


def _parse_deadline_to_days(deadline_date_str: str) -> int:
    """Парсит строку даты дедлайна в количество дней до него."""
    if not deadline_date_str:
        return 30

    formats = [
        "%d.%m.%Y",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
    ]

    deadline_date_str = deadline_date_str.strip()

    for fmt in formats:
        try:
            deadline = datetime.strptime(deadline_date_str, fmt)
            delta = deadline - datetime.now()
            return max(0, delta.days)
        except ValueError:
            continue

    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", deadline_date_str)
    if match:
        try:
            deadline = datetime.strptime(match.group(0), "%d.%m.%Y")
            delta = deadline - datetime.now()
            return max(0, delta.days)
        except ValueError:
            pass

    return 30


def _sanitize_for_sheets(text: str) -> str:
    """Заменяет эмодзи на текстовые маркеры для корректного отображения в Google Sheets."""
    if not isinstance(text, str):
        return text

    replacements = {
        "🟢": "[LOW]",
        "🟡": "[MED]",
        "🔴": "[HIGH]",
        "": "[UNKNOWN]",
        "⚠️": "[WARN]",
        "": "[BLOCK]",
        "✅": "[OK]",
        "⛔": "[FORBIDDEN]",
        "📊": "[CALC]",
        "💰": "[PRICE]",
        "📋": "[LIST]",
        "🔬": "[LAB]",
    }

    result = text
    for emoji, replacement in replacements.items():
        result = result.replace(emoji, replacement)

    # Удаляем оставшиеся эмодзи
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"
        "\U0001f300-\U0001f5ff"
        "\U0001f680-\U0001f6ff"
        "\U0001f1e0-\U0001f1ff"
        "\U00002702-\U000027b0"
        "\U000024c2-\U0001f251"
        "]+",
        flags=re.UNICODE,
    )

    return emoji_pattern.sub("", result)


def _build_tender_text(detail, documents_text: str, tender_info: dict = None) -> str:
    """Строит структурированный текст тендера для LLM с данными КТРУ и НМЦК."""
    parts = [
        f"НАЗВАНИЕ ЗАКУПКИ: {detail.purchase_name or detail.tender_id}",
        f"ЗАКАЗЧИК: {detail.customer_name or 'не указан'}",
        f"РЕГИОН: {detail.customer_region or 'не указан'}",
        f"АДРЕС ЗАКАЗЧИКА: {detail.customer_address or 'не указан'}",
        f"МЕСТО ПОСТАВКИ: {detail.delivery_address or 'не указано'}",
        f"НМЦК: {detail.nmck:,.2f} ₽" if detail.nmck else "НМЦК: не указана",
        f"СПОСОБ ЗАКУПКИ: {detail.purchase_method or 'не указан'}",
        f"ЭТП: {detail.platform_name or 'не указана'}",
        f"СРОК ПОДАЧИ ЗАЯВОК: {detail.deadline_date or 'не указан'}",
        f"ТРЕБОВАНИЯ: {detail.requirements or 'не указаны'}",
        f"ОБЕСПЕЧЕНИЕ ЗАЯВКИ: {detail.application_guarantee or 'не указано'}",
        f"ОБЕСПЕЧЕНИЕ КОНТРАКТА: {detail.contract_guarantee or 'не указано'}",
        f"АДРЕСОВ ПОСТАВКИ: {detail.addresses_count or 0}",
        f"ГОРОДОВ ПОСТАВКИ: {detail.cities_count or 0}",
        f"РЕГИОНОВ ПОСТАВКИ: {detail.regions_count or 1}",
    ]

    # === РАСШИРЕННЫЙ КОНТЕКСТ ДЛЯ АГЕНТА ===
    if tender_info:
        # Данные КТРУ
        ktru_parts = []
        if tender_info.get("rm_total"):
            ktru_parts.append(
                f"  РМ: {tender_info['rm_total']} (источник: {tender_info.get('rm_total_source', 'парсер')})"
            )
        if tender_info.get("students_count"):
            ktru_parts.append(
                f"  Слушатели: {tender_info['students_count']} (источник: {tender_info.get('students_count_source', 'парсер')})"
            )
        if tender_info.get("points_count"):
            ktru_parts.append(f"  Точки ПЛК: {tender_info['points_count']}")
        if tender_info.get("opr_positions"):
            ktru_parts.append(f"  Должности ОПР: {tender_info['opr_positions']}")

        if ktru_parts:
            parts.append("")
            parts.append("[ДАННЫЕ КТРУ ИЗ КАРТОЧКИ ЗАКУПКИ]:")
            parts.extend(ktru_parts)

    # Текст документов
    if documents_text and len(documents_text) > 100:
        prioritized_text = _prioritize_documents(documents_text)
        parts.append(f"\nТЕКСТ ДОКУМЕНТОВ (ТЗ, извещение):\n{prioritized_text[:12000]}")
    else:
        parts.append("\nДОКУМЕНТЫ: не удалось извлечь текст")

    return "\n".join(parts)


def _prioritize_documents(documents_text: str) -> str:
    """Сортирует документы по приоритету: ТЗ > извещение > КД > прочее > контракты."""
    lines = documents_text.split("\n")
    priority_scores = []
    for line in lines:
        score = 0
        lower = line.lower()
        if any(kw in lower for kw in ["техническое задание", "тз", "описание объекта"]):
            score = 100
        elif any(kw in lower for kw in ["извещение", "приглашение", "документация"]):
            score = 80
        elif any(kw in lower for kw in ["квалификационные", "требования", "критерии"]):
            score = 60
        elif any(kw in lower for kw in ["контракт", "договор", "проект контракта"]):
            score = 10
        priority_scores.append((score, line))

    priority_scores.sort(key=lambda x: x[0], reverse=True)
    return "\n".join(line for _, line in priority_scores)


def _get_quantity(analysis) -> int:
    """Извлекает количество из анализа. P1-4: страховка с fallback по НМЦК."""
    if not hasattr(analysis, "details") or analysis.details is None:
        return 1

    details = analysis.details

    if isinstance(details, dict):
        quantity = (
            details.get("rm_total")
            or details.get("points_count")
            or details.get("students_count")
            or details.get("opr_positions")
        )
        if quantity and quantity > 0:
            return int(quantity)

        # === СТРАХОВКА: Fallback по НМЦК если в details пусто ===
        nmck = getattr(analysis, "nmck", 0) or 0
        ttype = getattr(analysis, "tender_type", "")
        if nmck > 0 and ttype:
            divisors = {"sout": 1200, "plk": 500, "opr": 700, "education": 1500}
            divisor = divisors.get(ttype, 1000)
            estimated = int(nmck / divisor)
            logger.debug(
                f"[P1-4] Fallback quantity для {ttype}: {estimated} (НМЦК/ {divisor})"
            )
            return estimated

        return 1

    quantity = (
        getattr(details, "rm_total", None)
        or getattr(details, "points_count", None)
        or getattr(details, "students_count", None)
        or getattr(details, "opr_positions", None)
    )
    if quantity and quantity > 0:
        return int(quantity)

    # Страховка для object-based details
    nmck = getattr(analysis, "nmck", 0) or 0
    ttype = getattr(analysis, "tender_type", "")
    if nmck > 0 and ttype:
        divisors = {"sout": 1200, "plk": 500, "opr": 700, "education": 1500}
        divisor = divisors.get(ttype, 1000)
        return int(nmck / divisor)

    return 1


def _get_guarantee_info(detail, analysis) -> tuple:
    """Извлекает информацию об обеспечении."""
    app_guarantee = ""
    contract_guarantee = ""
    guarantee_method = ""

    if detail:
        raw_app = (detail.application_guarantee or "").strip()
        raw_contract = (detail.contract_guarantee or "").strip()
        raw_method = (detail.guarantee_method or "").strip()

        if raw_app:
            app_guarantee = raw_app
        if raw_contract:
            contract_guarantee = raw_contract
        if raw_method:
            guarantee_method = raw_method

    if analysis and hasattr(analysis, "details") and analysis.details:
        details = analysis.details
        if isinstance(details, dict):
            if not app_guarantee and details.get("application_guarantee"):
                app_guarantee = details["application_guarantee"]
            if not contract_guarantee and details.get("contract_guarantee"):
                contract_guarantee = details["contract_guarantee"]
            if not guarantee_method and details.get("guarantee_method"):
                guarantee_method = details["guarantee_method"]
        else:
            if not app_guarantee and getattr(details, "application_guarantee", None):
                app_guarantee = details.application_guarantee
            if not contract_guarantee and getattr(details, "contract_guarantee", None):
                contract_guarantee = details.contract_guarantee
            if not guarantee_method and getattr(details, "guarantee_method", None):
                guarantee_method = details.guarantee_method

    if detail and detail.requirements and not app_guarantee:
        req_lower = detail.requirements.lower()
        if "не требуется" in req_lower:
            app_guarantee = "Не требуется"
            contract_guarantee = "Не требуется"

    if (
        analysis
        and hasattr(analysis, "comment")
        and analysis.comment
        and not app_guarantee
    ):
        comment_lower = analysis.comment.lower()
        if "не требуется" in comment_lower:
            app_guarantee = "Не требуется"
            contract_guarantee = "Не требуется"

    if not app_guarantee:
        app_guarantee = "Не требуется"
    if not contract_guarantee:
        contract_guarantee = "Не требуется"
    if not guarantee_method:
        guarantee_method = "Не требуется"

    return app_guarantee, contract_guarantee, guarantee_method


def _build_calculation_breakdown(analysis) -> str:
    """Формирует детальную разбивку расчётов для колонки Q."""
    if not hasattr(analysis, "details") or analysis.details is None:
        return ""

    d = analysis.details
    if isinstance(d, dict):
        details = d
    else:
        details = {k: getattr(d, k, None) for k in dir(d) if not k.startswith("_")}

    tender_type = details.get("type", analysis.tender_type)
    lines = []

    if tender_type == "sout":
        lines.append(f" СОУТ (Упрощённая формула)")
        lines.append(f"РМ всего: {details.get('rm_total', '?')} × 213₽")
        lines.append(f"База (РМ×213): {details.get('main_calculation', 0):,.0f}₽")
        lines.append(f"Материалы: {details.get('materials_cost', 0):,.0f}₽")
        lines.append(f"Почта: {details.get('delivery_cost', 0):,.0f}₽")

        lines.append(f"Командировочные:")
        lines.append(f"  Выезд/Бензин: {details.get('travel_cost', 0):,.0f}₽")
        lines.append(
            f"  Замерщик+Суточные: {details.get('measurer_and_daily', 0):,.0f}₽"
        )
        lines.append(f"  Проживание: {details.get('accommodation_cost', 0):,.0f}₽")
        lines.append(f"  Билеты (среднее): {details.get('flight_cost', 0):,.0f}₽")

        lines.append(
            f"Города: {details.get('cities_count', 1)}, Регионы: {details.get('regions_count', 1)}"
        )

    elif tender_type == "education":
        lines.append(
            f"Обучение | {'Дистант' if details.get('is_distance') else 'Очно'}"
        )
        lines.append(f"Слушателей: {details.get('students_count', '?')}")
        lines.append(f"Удостоверения: {details.get('certificates', 0)} × 60₽")
        lines.append(f"Дипломы: {details.get('diplomas', 0)} × 265₽")
        lines.append(f"Свидетельства раб.: {details.get('worker_certs', 0)} × 80₽")
        lines.append(f"Повыш. квалиф.: {details.get('qual_certs', 0)} × 130₽")
        lines.append(f"Протоколы: {details.get('protocols_count', 0)} × 3.65₽")
        lines.append(f"Документы: {details.get('docs_cost', 0):,.0f}₽")
        lines.append(f"Материалы: {details.get('materials_cost', 0):,.0f}₽")
        lines.append(f"Трудозатраты: {details.get('labor_cost', 0):,.0f}₽")
        lines.append(f"  (специалист 300₽ + методист 681₽ + РО 1800₽ + портал)")
        lines.append(f"Доставка: {details.get('delivery_cost', 0):,.0f}₽")
        lines.append(f"Накладные: {details.get('overhead_cost', 0):,.0f}₽")
        if not details.get("is_distance"):
            lines.append(f"Очные затраты: {details.get('full_time_cost', 0):,.0f}₽")
            lines.append(f"  Преподаватель: {details.get('teacher_days', 0)} дн.")
            lines.append(f"  Транспорт: {details.get('transport_cost', 0):,.0f}₽")
            lines.append(
                f"  Проживание: {details.get('accommodation_cost', 0) if details.get('accommodation_cost') else 0:,.0f}₽"
            )
            lines.append(f"  Аренда: {details.get('venue_cost', 0):,.0f}₽")
            lines.append(f"  Манекен: {details.get('manikin_days', 0)} дн.")

    elif tender_type == "opr":
        lines.append(f"ОПР")
        lines.append(
            f"РМ/должностей: {details.get('rm_total', details.get('positions_count', '?'))}"
        )
        lines.append(f"СИЗ/ДСИЗ/ИОТ: {details.get('rm_total', 0)} × 200₽")
        lines.append(f"Материалы: {details.get('materials_cost', 0):,.0f}₽")
        lines.append(f"Почта: {details.get('delivery_cost', 0):,.0f}₽")
        lines.append(f"Маржа: 30%")

    elif tender_type == "plk":
        lines.append(f" ПЛК")
        lines.append(f"Точек: {details.get('points_count', '?')}")
        lines.append(f"Себестоимость/точка: 41.9₽")
        lines.append(f"Материалы: {details.get('materials_cost', 0):,.0f}₽")
        lines.append(f"Почта: {details.get('delivery_cost', 0):,.0f}₽")
        plk_travel = details.get("travel_cost", 0) or details.get("transport_cost", 0)
        lines.append(f"Транспорт: {plk_travel:,.0f}₽")

    elif tender_type == "testing":
        lines.append(f"Testing (ПЛК-калькулятор)")
        lines.append(f"Точек: {details.get('points_count', '?')}")

    else:
        lines.append(f"[?] Тип: {tender_type}")

    lines.append(f"──────────────")
    lines.append(f"Себестоимость: {analysis.cost_price:,.0f}₽")
    lines.append(f"Маржа: {analysis.margin_percent:.1f}%")
    lines.append(f"Цена: {analysis.recommended_price:,.0f}₽")

    return "\n".join(lines)


def _build_sheets_row(analysis, detail, tender) -> dict:
    """Формирует строку для Google Sheets. v7.5.0: санитайзер эмодзи."""
    quantity = _get_quantity(analysis)
    app_guarantee, contract_guarantee, guarantee_method = _get_guarantee_info(
        detail, analysis
    )

    law = tender.law.replace("-FZ", "-ФЗ") if tender.law else ""
    if detail and detail.purchase_method:
        procurement = f"{detail.purchase_method}, {law}"
    else:
        procurement = law

    tender_url = tender.url

    # ИИ-комментарий → колонка R (с санитайзером)
    ai_comment = _sanitize_for_sheets(analysis.comment or "")

    purchase_name = tender.title or ""
    if detail and detail.purchase_name and len(detail.purchase_name) > 10:
        purchase_name = detail.purchase_name

    etp_name = ""
    if detail:
        etp_name = detail.platform_name or detail.etp or ""
    if not etp_name and hasattr(tender, "etp") and tender.etp:
        etp_name = tender.etp
    elif detail and hasattr(detail, "etp") and detail.etp:
        etp_name = detail.etp

    deadline = ""
    if detail and detail.deadline_date:
        deadline = detail.deadline_date
    elif hasattr(tender, "deadline_date") and tender.deadline_date:
        deadline = tender.deadline_date

    decision_override = analysis.decision
    if hasattr(analysis, "details") and analysis.details:
        details = analysis.details
        is_forbidden = (
            isinstance(details, dict) and details.get("_forbidden_direction")
        ) or getattr(details, "_forbidden_direction", False)
        if is_forbidden:
            decision_override = "не рекомендуется"
            ai_comment = "[FORBIDDEN] ЗАПРЕЩЁННОЕ НАПРАВЛЕНИЕ: " + ai_comment

    return {
        "ID тендера": tender.tender_id,
        "Ссылка на тендер": tender_url,
        "Наименование услуг": purchase_name,
        "Способ проведения закупки": procurement,
        "ЭТП": etp_name,
        "Комиссия ЭТП": (
            f"{analysis.details.get('etp_commission', 0):,.0f} ₽"
            if analysis.details
            else ""
        ),
        "Регион": (
            detail.customer_region if detail else (getattr(tender, "region", "") or "")
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
        "Расчёты": _sanitize_for_sheets(_build_calculation_breakdown(analysis)),
        "Комментарий от ИИ-агента": ai_comment,
        "Рекомендации": _sanitize_for_sheets(_build_short_recommendation(analysis)),
        "Комментарии руководителя отдела по участию": "",
        "Дата заключения контракта": "",
        "Дата выполнения работ": "",
        "Результат": "",
    }


def _build_short_recommendation(analysis) -> str:
    """Формирует краткую рекомендацию для колонки S (без эмодзи)."""
    parts = []

    parts.append(f"Тип: {analysis.tender_type}")
    parts.append(f"Себестоимость: {analysis.cost_price:,.0f} ₽")
    parts.append(f"Рекомендуемая цена: {analysis.recommended_price:,.0f} ₽")
    parts.append(f"Маржа: {analysis.margin_percent:.1f}%")

    risk_label = {"low": "[LOW]", "medium": "[MED]", "high": "[HIGH]"}.get(
        analysis.risk_level, "[UNKNOWN]"
    )
    parts.append(f"Риск: {risk_label}")

    if hasattr(analysis, "guard_violations") and analysis.guard_violations:
        parts.append(f"[WARN] {len(analysis.guard_violations)} нарушений лимитов")

    return " | ".join(parts)


def run_parse_only(max_pages: int = None, max_results: int = None):
    """Режим только парсинга (без LLM)."""
    logger.info("=" * 60)
    logger.info(" РЕЖИМ: Только парсинг (без LLM)")
    logger.info("=" * 60)

    searcher = create_searcher()

    output_file = f"data/search_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results = searcher.search_and_save(
        output_file=output_file,
        max_pages=max_pages,
        max_results=max_results,
    )

    logger.info(f" Найдено тендеров: {len(results)}")
    logger.info(f" Сохранено в: {output_file}")
    return results


def run_analyze(
    max_pages: int = None, max_results: int = None, skip_detail: bool = False
):
    """Полный анализ с LLM. v7.5.0: расширенный контекст агента + санитайзер."""
    logger.info("=" * 60)
    logger.info(" РЕЖИМ: Полный анализ с LLM")

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
        calculator=calculator,
        risk_analyzer=risk_analyzer,
        type_detector=type_detector,
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

    for tender in searcher.search(max_pages=max_pages, max_results=None):

        # 1. КЭШ
        if limiter.is_cached(tender.tender_id):
            logger.debug(f" Кэш: {tender.tender_id}")
            duplicates_skipped += 1
            continue

        # 2. SHEETS
        if sheets_manager and sheets_manager.check_exists(tender.tender_id):
            logger.info(f"   Уже в Sheets: {tender.tender_id}")
            limiter.add_to_cache(tender.tender_id)
            duplicates_skipped += 1
            continue

        # 3. ЛИМИТ ТОЛЬКО ПО НОВЫМ
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
                    logger.info(
                        f"   Обеспечение: {detail.application_guarantee or 'не указано'}"
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
                            docs = []
                            for doc_dict in detail.documents:
                                docs.append(
                                    DocumentInfo(
                                        name=doc_dict.get("name", ""),
                                        url=doc_dict.get("link", ""),
                                        file_url=doc_dict.get("link", ""),
                                        file_type=doc_dict.get("file_type", ""),
                                    )
                                )
                            documents_text = doc_processor.process_documents(docs)
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
                        tender_text = _build_tender_text(detail, documents_text)
                else:
                    logger.warning(f"   Детальный парсинг вернул None")
            except Exception as e:
                logger.error(f"   Ошибка детального парсинга: {e}")
                detail = None

        # === ШАГ 2: Fallback — упрощённый текст ===
        if not tender_text:
            doc_part = ""
            if documents_text and len(documents_text) > 100:
                doc_part = f"\n\nТЕКСТ ДОКУМЕНТОВ:\n{documents_text[:12000]}"

            tender_text = f"""НАЗВАНИЕ ЗАКУПКИ:
{tender.title}

ЗАКАЗЧИК:
{tender.customer or 'не указан'}

РЕГИОН:
{getattr(tender, 'region', '') or 'не указан'}

НМЦК:
{tender.nmck or 'не указана'}

ЗАКОН:
{tender.law}{doc_part}"""
            logger.info("   Используется упрощённый текст (title only)")

        # === ШАГ 3: LLM-анализ ===
        try:
            tender_info = {}
            if detail:
                tender_info = {
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
                    "deadline_days": _parse_deadline_to_days(
                        detail.deadline_date or tender.deadline_date or ""
                    ),
                    "platform_name": detail.platform_name or "",
                    "requirements": detail.requirements or "",
                    "application_guarantee": (
                        detail.application_guarantee or ""
                    ).strip(),
                    "contract_guarantee": (detail.contract_guarantee or "").strip(),
                    "guarantee_method": (detail.guarantee_method or "").strip(),
                }

                tender_info["cities_count"] = detail.cities_count or 1
                tender_info["regions_count"] = detail.regions_count or 1
                tender_info["addresses_count"] = detail.addresses_count or 1
                tender_info["is_annual"] = bool(getattr(detail, "is_annual", False))

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
                f"[DEBUG] Passing to analyzer: nmck={tender.nmck}, "
                f"region={tender_info.get('region', 'N/A')}, "
                f"text_length={len(tender_text)}, "
                f"students_count={tender_info.get('students_count', 'N/A')}, "
                f"rm_total={tender_info.get('rm_total', 'N/A')}, "
                f"regions_count={tender_info.get('regions_count', 'N/A')}, "
                f"type_hint={type_hint}"
            )

            if not type_hint and tender.title:
                from core.services.type_service import TypeService

                _ts = TypeService()
                _title_lower = tender.title.lower()
                for _ttype, _keywords in _ts.TITLE_KEYWORDS.items():
                    if any(_kw in _title_lower for _kw in _keywords):
                        type_hint = _ttype
                        logger.debug(f"[v7.1.0] Type hint из search title: {_ttype}")
                        break

            # ПРИОРИТЕТ EDUCATION НАД SOUT (дублируем здесь для надежности)
            if detail and (detail.students_count or 0) > 0 and type_hint == "sout":
                logger.warning(
                    f"[v7.2.4] Override: sout → education "
                    f"(КТРУ дал {detail.students_count} слушателей)"
                )
                type_hint = "education"

            # Передаем tender_info в _build_tender_text для расширенного контекста
            if detail and not tender_text:
                tender_text = _build_tender_text(detail, documents_text, tender_info)

            analysis = analyzer.analyze(
                tender_info=tender_info,
                documents_text=documents_text or tender_text,
                llm_classification=None,
                llm_confidence=0.0,
                tender_type_hint=type_hint,
            )

            logger.debug(
                f"[DEBUG] Analysis result: type={analysis.tender_type}, "
                f"cost_price={analysis.cost_price}, "
                f"recommended_price={analysis.recommended_price}, "
                f"margin_percent={analysis.margin_percent}, "
                f"needs_manual_review={getattr(analysis, 'needs_manual_review', 'N/A')}, "
                f"llm_confidence={getattr(analysis, 'llm_confidence', 'N/A')}"
            )

            result_dict = analysis.to_dict()

            row = _build_sheets_row(analysis, detail, tender)
            sheets_rows.append(row)

            # ИНКРЕМЕНТ СЧЕТЧИКА И КЭШ
            analyzed_count += 1
            limiter.add_to_cache(tender.tender_id)
            results.append(result_dict)

            # Запись в Sheets
            if sheets_manager:
                try:
                    sheets_manager.add_tender_to_top(row, check_duplicate=False)
                    logger.info(f"   Записано в Google Sheets")
                except Exception as e:
                    logger.warning(f"   Ошибка записи в Sheets: {e}")

            print(f"\n{'=' * 60}")
            print(f" РЕЗУЛЬТАТ: {tender.tender_id}")
            print(f"{'=' * 60}")
            print(f"Тип: {analysis.tender_type}")
            print(f"НМЦК: {analysis.nmck:,.0f} ₽")
            print(f"Себестоимость: {analysis.cost_price:,.0f} ₽")
            print(f"Рекомендуемая цена: {analysis.recommended_price:,.0f} ₽")
            print(f"Маржа: {analysis.margin_percent:.1f}%")
            print(f"Риск: {analysis.risk_level} | Решение: {analysis.decision}")
            if getattr(analysis, "needs_manual_review", False):
                print(f" ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА")
            if detail and detail.customer_region:
                print(f"Регион: {detail.customer_region}")
            if detail and detail.platform_name:
                print(f"ЭТП: {detail.platform_name}")
            print(f"{'-' * 60}")
            print(f"Комментарий:")
            comment = analysis.comment
            print(comment[:400] + "..." if len(comment) > 400 else comment)
            print(f"{'=' * 60}")

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

        print(f"\n{'=' * 60}")
        print(f" СВОДКА")
        print(f"{'=' * 60}")
        print(f"Всего уникальных: {len(results)}")
        print(f"Дубликатов пропущено: {duplicates_skipped}")

        decisions = {}
        for r in results:
            d = r.get("decision", "unknown")
            decisions[d] = decisions.get(d, 0) + 1
        for d, count in decisions.items():
            label = (
                "[OK]"
                if d == "рекомендуется"
                else "[BLOCK]" if d == "не участвуем" else ""
            )
            print(f"{label} {d}: {count}")

        risks = {}
        for r in results:
            risk = r.get("risk_level", "unknown")
            risks[risk] = risks.get(risk, 0) + 1
        print(f"\nРиски:")
        for risk, count in risks.items():
            label = (
                "[LOW]" if risk == "low" else "[MED]" if risk == "medium" else "[HIGH]"
            )
            print(f"{label} {risk}: {count}")
        print(f"{'=' * 60}")

    # === ФИНАЛЬНАЯ СТАТИСТИКА ===
    logger.info(
        f" Итог поиска: проанализировано {analyzed_count}, пропущено из кэша {duplicates_skipped}"
    )

    unique_count = len(results)
    limiter.record_tenders(unique_count)

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
        calculator=calculator,
        risk_analyzer=risk_analyzer,
        type_detector=type_detector,
    )

    print("\n" + "=" * 60)
    print(" ИНТЕРАКТИВНЫЙ РЕЖИМ")
    print("=" * 60)
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
        tender_info = {"documents_text": tender_text}
        result = analyzer.analyze(
            tender_info=tender_info,
            documents_text=tender_text,
        )
        result_dict = result.to_dict()

        print("\n" + "=" * 60)
        print(" РЕЗУЛЬТАТ АНАЛИЗА")
        print("=" * 60)
        print(f"Тип: {result_dict['tender_type']}")
        print(f"НМЦК: {result_dict['nmck']:,.0f} ₽")
        print(f"Себестоимость: {result_dict['cost_price']:,.0f} ₽")
        print(f"Рекомендуемая цена: {result_dict['recommended_price']:,.0f} ₽")
        print(f"Маржа: {result_dict['margin_percent']:.1f}%")
        print(f"Риск: {result_dict['risk_level']}")
        print(f"Решение: {result_dict['decision']}")
        if result_dict.get("needs_manual_review"):
            print(" ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА")
        if result_dict.get("llm_confidence") is not None:
            print(f"Уверенность ИИ: {result_dict['llm_confidence']:.2f}")
        print("-" * 60)
        print("Риски:")
        for flag in result_dict.get("red_flags", []):
            print(f"  • {flag}")
        print("-" * 60)
        print("Комментарий:")
        print(result_dict["comment"])
        print("=" * 60)

    except Exception as e:
        logger.error(f" Ошибка: {e}")
        import traceback

        logger.error(traceback.format_exc())


def main():
    parser = argparse.ArgumentParser(
        description="TENDER-BOT v7.5.0: Анализ тендеров с ИИ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python main.py --parse-only --max-results 10        # Только поиск
  python main.py --analyze --max-pages 2               # Полный анализ
  python main.py --analyze --skip-detail               # Быстрый анализ
  python main.py --interactive                       # Ручной ввод
  python main.py --test                              # Тест калькулятора
        """,
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
