"""
utils/formatters.py
Вспомогательные функции для форматирования данных, дат, текста для Google Sheets и логирования.
Версия: v7.8.0
"""

import re
from datetime import datetime
from typing import Any, Dict, Optional
from loguru import logger


def parse_deadline_to_days(deadline_date_str: str) -> int:
    """Парсит строку даты дедлайна в количество дней до него."""
    if not deadline_date_str:
        return 30

    formats = ["%d.%m.%Y", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"]
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


def sanitize_for_sheets(text: str) -> str:
    """Заменяет эмодзи на текстовые маркеры для Google Sheets во избежание сбоев кодировки."""
    if not isinstance(text, str):
        return str(text) if text is not None else ""

    replacements = {
        "🟢": "[LOW]",
        "🟡": "[MED]",
        "🔴": "[HIGH]",
        "⚠️": "[WARN]",
        "✅": "[OK]",
        "⛔": "[FORBIDDEN]",
        "📊": "[CALC]",
        "💰": "[PRICE]",
        "📋": "[LIST]",
        "🔬": "[LAB]",
        "❌": "[ERR]",
        "ℹ️": "[INFO]",
    }

    result = text
    for emoji, replacement in replacements.items():
        result = result.replace(emoji, replacement)

    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map symbols
        "\U0001f1e0-\U0001f1ff"  # flags (iOS)
        "\U00002702-\U000027b0"
        "\U000024c2-\U0001f251"
        "]+",
        flags=re.UNICODE,
    )
    result = emoji_pattern.sub("", result)

    return result


def get_quantity(analysis: Any) -> int:
    """Извлекает итоговое количество (РМ/слушателей/точек) из объекта анализа."""
    if not hasattr(analysis, "details") or analysis.details is None:
        return 1

    details = analysis.details
    quantity = None

    if isinstance(details, dict):
        quantity = (
            details.get("rm_total")
            or details.get("points_count")
            or details.get("students_count")
            or details.get("opr_positions")
        )
    else:
        quantity = (
            getattr(details, "rm_total", None)
            or getattr(details, "points_count", None)
            or getattr(details, "students_count", None)
            or getattr(details, "opr_positions", None)
        )

    if quantity and int(quantity) > 0:
        return int(quantity)

    # Fallback расчет по НМЦК, если парсер не вытащил точное количество
    nmck = getattr(analysis, "nmck", 0) or 0
    ttype = getattr(analysis, "tender_type", "")
    if nmck > 0 and ttype:
        divisors = {"sout": 1200, "plk": 500, "opr": 700, "education": 1500}
        divisor = divisors.get(ttype, 1000)
        estimated = int(nmck / divisor)
        logger.debug(f"[Fallback] Количество для {ttype}: {estimated}")
        return estimated

    return 1


def build_calculation_breakdown(analysis: Any) -> str:
    """Формирует детальную разбивку расчётов для колонки Q (Расчёты)."""
    if not hasattr(analysis, "details") or analysis.details is None:
        return ""

    d = analysis.details
    details = (
        d
        if isinstance(d, dict)
        else {k: getattr(d, k, None) for k in dir(d) if not k.startswith("_")}
    )
    tender_type = details.get("type", getattr(analysis, "tender_type", ""))
    lines = []

    # === СОУТ ===
    if tender_type == "sout":
        lines.append("СОУТ (Единая формула)")
        rm_source = details.get("rm_total_source", "из ТЗ")
        rm_count = details.get("rm_total", "?")
        lines.append(
            f"РМ всего: {rm_count} ({rm_source}) × {details.get('base_rate_per_rm', 213)}₽"
        )
        lines.append(f"База: {details.get('main_calculation', 0):,.0f}₽")
        lines.append(f"Материалы: {details.get('materials_cost', 0):,.0f}₽")
        lines.append(f"Доставка: {details.get('delivery_cost', 0):,.0f}₽")

        travel = details.get("travel_breakdown", {})
        if travel:
            lines.append("Командировочные:")
            if travel.get("source") == "explicit":
                lines.append(f"  Транспорт (явный): {travel['total']:,.0f}₽")
            elif travel.get("source") == "heuristic_by_locations":
                lines.append(f"  Транспорт (по городам): {travel['total']:,.0f}₽")
            else:
                lines.append(f"  Выезд/бензин: {travel.get('auto_cost', 0):,.0f}₽")
                lines.append(
                    f"  Суточные+замерщик: {travel.get('daily_cost', 0):,.0f}₽"
                )
                lines.append(
                    f"  Проживание: {travel.get('accommodation_cost', 0):,.0f}₽"
                )

        lines.append(
            f"Регионов: {details.get('regions_count', 1)}, Дней: {details.get('trip_days', 3)}"
        )

    # === ОБУЧЕНИЕ ===
    elif tender_type == "education":
        lines.append(
            f"Обучение | {'Дистант' if details.get('is_distance') else 'Очно'}"
        )
        students_source = details.get("students_count_source", "из ТЗ")
        lines.append(
            f"Слушателей: {details.get('students_count', '?')} ({students_source})"
        )

        protocols = details.get("protocols_count", 0)
        qual_certs = details.get("qual_certs", 0)
        diplomas = details.get("diplomas", 0)

        if protocols > 0:
            lines.append(f"Протоколы ОТ: {protocols} шт.")
        if qual_certs > 0:
            lines.append(f"Удостоверения ПК: {qual_certs} шт.")
        if diplomas > 0:
            lines.append(f"Дипломы ПП: {diplomas} шт.")

        lines.append(f"Трудозатраты: {details.get('labor_cost', 0):,.0f}₽")
        lines.append(f"Доставка: {details.get('delivery_cost', 0):,.0f}₽")

        if not details.get("is_distance"):
            lines.append(f"Очные затраты: {details.get('full_time_cost', 0):,.0f}₽")

    # === ОПР ===
    elif tender_type == "opr":
        lines.append("ОПР (Оценка проф. рисков)")
        positions_source = details.get("opr_positions_source", "из ТЗ")
        positions = details.get("opr_positions", details.get("positions_count", "?"))
        lines.append(f"Должностей: {positions} ({positions_source})")
        lines.append(f"База (работы): {details.get('position_cost', 0):,.0f}₽")
        lines.append(f"Материалы: {details.get('materials_cost', 0):,.0f}₽")
        lines.append(f"Доставка: {details.get('delivery_cost', 0):,.0f}₽")

        transport = details.get("transport_cost", 0)
        accom = details.get("accommodation_cost", 0)
        daily = details.get("daily_allowance", 0)

        if transport > 0 or accom > 0 or daily > 0:
            lines.append("Логистика:")
            if transport > 0:
                lines.append(f"  Транспорт: {transport:,.0f}₽")
            if accom > 0:
                lines.append(f"  Проживание: {accom:,.0f}₽")
            if daily > 0:
                lines.append(f"  Суточные: {daily:,.0f}₽")

        if details.get("additional_cost", 0) > 0:
            lines.append(f"СИЗ/ДСИЗ/ИОТ: {details.get('additional_cost', 0):,.0f}₽")

    # === ПЛК ===
    elif tender_type == "plk":
        lines.append("ПЛК (Производственный контроль)")
        points_source = details.get("points_source", "из ТЗ")
        points = details.get("points_count", "?")
        lines.append(f"Точек замеров: {points} ({points_source})")
        lines.append(
            f"База (замеры): {details.get('points_cost', 0) + details.get('measurer_cost', 0):,.0f}₽"
        )
        lines.append(f"Материалы: {details.get('materials_cost', 0):,.0f}₽")
        lines.append(f"Доставка: {details.get('delivery_cost', 0):,.0f}₽")

        transport = details.get("transport_cost", 0)
        accom = details.get("accommodation_cost", 0)
        daily = details.get("daily_allowance", 0)

        if transport > 0 or accom > 0 or daily > 0:
            lines.append("Логистика:")
            if transport > 0:
                lines.append(f"  Транспорт: {transport:,.0f}₽")
            if accom > 0:
                lines.append(f"  Проживание: {accom:,.0f}₽")
            if daily > 0:
                lines.append(f"  Суточные: {daily:,.0f}₽")
        else:
            lines.append("Транспорт: 0₽ (1 адрес или нет данных)")

    else:
        lines.append(f"[?] Тип: {tender_type}")

    lines.append("──────────────")
    lines.append(f"Себестоимость: {getattr(analysis, 'cost_price', 0):,.0f}₽")
    lines.append(f"Маржа: {getattr(analysis, 'margin_percent', 0):.1f}%")
    lines.append(f"Цена: {getattr(analysis, 'recommended_price', 0):,.0f}₽")

    if hasattr(analysis, "review_reason") and analysis.review_reason:
        lines.append(f"⚠️ {analysis.review_reason}")

    return "\n".join(lines)


def build_short_recommendation(analysis: Any) -> str:
    """Формирует сжатую рекомендацию для колонки S (Рекомендации)."""
    parts = [
        f"Тип: {getattr(analysis, 'tender_type', 'unknown')}",
        f"Себестоимость: {getattr(analysis, 'cost_price', 0):,.0f} ₽",
        f"Рекомендуемая цена: {getattr(analysis, 'recommended_price', 0):,.0f} ₽",
        f"Маржа: {getattr(analysis, 'margin_percent', 0):.1f}%",
    ]

    risk = getattr(analysis, "risk_level", "low")
    risk_label = {"low": "[LOW]", "medium": "[MED]", "high": "[HIGH]"}.get(
        risk, "[UNKNOWN]"
    )
    parts.append(f"Риск: {risk_label}")

    if hasattr(analysis, "guard_violations") and analysis.guard_violations:
        parts.append(f"[WARN] {len(analysis.guard_violations)} нарушений лимитов")

    return " | ".join(parts)


def log_pipeline_summary(stats: Dict[str, int]) -> None:
    """Печатает красивую итоговую статистику выполнения скрипта в консоль."""
    logger.info("=" * 60)
    logger.info("📊 ИТОГИ ВЫПОЛНЕНИЯ ПАЙПЛАЙНА:")
    logger.info(f"   • Всего найдено закупок:     {stats.get('total', 0)}")
    logger.info(f"   • Успешно обработано:       {stats.get('processed', 0)}")
    logger.info(f"   • Пропущено (дубликаты/фильтры): {stats.get('skipped', 0)}")
    logger.info(f"   • Записано в Google Sheets: {stats.get('added_to_sheets', 0)}")
    logger.info(f"   • Ошибок обработки:         {stats.get('errors', 0)}")
    logger.info("=" * 60)
