"""
core/parsers/ktru_parser.py
Парсинг КТРУ из common-info (44-ФЗ) и lot-list (223-ФЗ).

v7.6.0:
  - Qty из ЕИС не сбрасывается по «рыночной» цене 800₽/РМ
  - Себестоимость тендеров — costs_db (СОУТ base_cost_per_rm=213 и т.д.)
  - Сброс только при явном мусоре парсинга / инверсии колонок
  - rm_total_source / students_count_source / points_source / opr_positions_source = ktru
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from loguru import logger


class KtruParser:
    """Извлекает количества из КТРУ (44-ФЗ) и lot-list (223-ФЗ)."""

    # Низкая цена в извещении — только WARNING, не обнуление qty.
    # Не путать с себестоимостью из costs_db (СОУТ 213 ₽/РМ и т.п.).
    LOW_UNIT_PRICE_WARN = {
        "education": 500,
        "sout": 150,
        "opr": 100,
        "plk": 30,
    }

    @staticmethod
    def parse(soup: BeautifulSoup, nmck: float = 0) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "rm_total": None,
            "students_count": None,
            "points_count": None,
            "opr_positions": None,
            "unit_type": None,
            "price_per_unit": None,
            "ktru_confidence": 0.0,
            "rm_total_source": None,
            "students_count_source": None,
            "points_source": None,
            "opr_positions_source": None,
        }

        table_container = soup.find("div", id="purchaseObjectTruTable1")
        if not table_container:
            table_container = soup.find("table", {"class": "blockInfo__table"})

        if not table_container:
            return result

        table = table_container.find("table", {"class": "blockInfo__table"})
        if not table:
            table = table_container if table_container.name == "table" else None

        if not table:
            return result

        rows = table.find_all("tr", {"class": "tableBlock__row"})

        rm_data: Dict[str, List[float]] = {"qty": [], "prices": []}
        person_data: Dict[str, List[float]] = {"qty": [], "prices": []}
        point_data: Dict[str, List[float]] = {"qty": []}
        position_data: Dict[str, List[float]] = {"qty": []}

        for row in rows:
            if "tableBlock__foot" in " ".join(row.get("class", [])):
                continue

            cols = row.find_all("td", {"class": "tableBlock__col"})
            if len(cols) < 5:
                continue

            unit_idx, qty_idx, price_idx = (3, 4, 5) if len(cols) >= 7 else (2, 3, 4)

            unit = (
                cols[unit_idx].get_text(strip=True).lower()
                if len(cols) > unit_idx
                else ""
            )
            qty_text = (
                cols[qty_idx].get_text(strip=True) if len(cols) > qty_idx else "0"
            )
            price_text = (
                cols[price_idx].get_text(strip=True) if len(cols) > price_idx else ""
            )

            qty_clean = (
                qty_text.replace(",", ".")
                .replace(" ", "")
                .replace("\xa0", "")
                .replace("\u00a0", "")
            )
            price_clean = (
                price_text.replace(",", ".")
                .replace(" ", "")
                .replace("\xa0", "")
                .replace("\u00a0", "")
                .replace("₽", "")
            )

            qty = KtruParser._parse_float(qty_clean)
            price = KtruParser._parse_float(price_clean)

            if "рабочее место" in unit or "раб место" in unit:
                if qty:
                    rm_data["qty"].append(qty)
                if price:
                    rm_data["prices"].append(price)

            elif "человек" in unit:
                if qty:
                    person_data["qty"].append(qty)
                if price:
                    person_data["prices"].append(price)

            elif "точка" in unit or "точек" in unit:
                if qty:
                    point_data["qty"].append(qty)

            elif "должность" in unit:
                if qty:
                    position_data["qty"].append(qty)

        # --- РМ (СОУТ) ---
        if rm_data["qty"]:
            total_rm = sum(rm_data["qty"])
            avg_price = (
                sum(rm_data["prices"]) / len(rm_data["prices"])
                if rm_data["prices"]
                else None
            )
            if rm_data["qty"] and rm_data["prices"] and nmck > 0:
                try:
                    n = min(len(rm_data["qty"]), len(rm_data["prices"]))
                    line_sum = sum(
                        rm_data["qty"][i] * rm_data["prices"][i] for i in range(n)
                    )
                    if abs(line_sum - nmck) / max(nmck, 1) < 0.05:
                        logger.info(
                            f"[KTRU] qty×price ≈ НМЦК ({line_sum:,.0f} ≈ {nmck:,.0f}) — qty канон"
                        )
                except Exception:
                    pass

            sanitized_rm = KtruParser._sanitize_quantity(
                total_rm, nmck, "sout", price_per_unit=avg_price
            )
            if sanitized_rm > 0:
                result["rm_total"] = int(sanitized_rm)
                result["rm_total_source"] = "ktru"
                result["unit_type"] = "rm"
                result["ktru_confidence"] = 1.0
                if avg_price:
                    result["price_per_unit"] = avg_price
                logger.info(
                    f"[KTRU] Найдено {result['rm_total']} РМ "
                    f"({len(rm_data['qty'])} позиций, source=ktru)"
                )
            else:
                logger.warning(
                    f"[KTRU] РМ={total_rm} сброшены как мусор парсинга (НМЦК={nmck})"
                )

        # --- Обучение ---
        elif person_data["qty"]:
            students = KtruParser._parse_education_smart(person_data, nmck)
            if students and students > 0:
                result["students_count"] = int(students)
                result["students_count_source"] = "ktru"
                result["unit_type"] = "person"
                result["ktru_confidence"] = 1.0
                logger.info(
                    f"[KTRU] Найдено {result['students_count']} слушателей "
                    f"({len(person_data['qty'])} позиций, source=ktru)"
                )
            else:
                logger.warning(
                    f"[KTRU] Слушатели сброшены (мусор/инверсия, НМЦК={nmck})"
                )

        # --- ПЛК ---
        elif point_data["qty"]:
            total_points = sum(point_data["qty"])
            sanitized_points = KtruParser._sanitize_quantity(total_points, nmck, "plk")
            if sanitized_points > 0:
                result["points_count"] = int(sanitized_points)
                result["points_source"] = "ktru"
                result["unit_type"] = "point"
                result["ktru_confidence"] = 1.0
                logger.info(
                    f"[KTRU] Найдено {result['points_count']} точек "
                    f"({len(point_data['qty'])} позиций, source=ktru)"
                )
            else:
                logger.warning(
                    f"[KTRU] Точки={total_points} сброшены как мусор (НМЦК={nmck})"
                )

        # --- ОПР ---
        elif position_data["qty"]:
            total_positions = sum(position_data["qty"])
            sanitized_positions = KtruParser._sanitize_quantity(
                total_positions, nmck, "opr"
            )
            if sanitized_positions > 0:
                result["opr_positions"] = int(sanitized_positions)
                result["opr_positions_source"] = "ktru"
                result["unit_type"] = "position"
                result["ktru_confidence"] = 1.0
                logger.info(
                    f"[KTRU] Найдено {result['opr_positions']} должностей "
                    f"({len(position_data['qty'])} позиций, source=ktru)"
                )
            else:
                logger.warning(
                    f"[KTRU] Должности={total_positions} сброшены как мусор (НМЦК={nmck})"
                )

        return result

    @staticmethod
    def parse_223_lot_list(soup: BeautifulSoup, nmck: float = 0) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "rm_total": None,
            "students_count": None,
            "points_count": None,
            "opr_positions": None,
            "unit_type": None,
            "ktru_confidence": 0.0,
            "rm_total_source": None,
            "students_count_source": None,
            "points_source": None,
            "opr_positions_source": None,
        }

        table = soup.find("table", {"class": "table"})
        if not table:
            logger.debug("[KTRU-223] Таблица лотов не найдена")
            return result

        rows = table.find_all("tr")

        rm_qtys: List[int] = []
        person_qtys: List[int] = []
        point_qtys: List[int] = []
        position_qtys: List[int] = []

        for row in rows:
            if row.find("th"):
                continue
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            lot_name = cols[0].get_text(strip=True).lower() if len(cols) > 0 else ""

            rm_match = re.search(r"(\d+)\s*рабоч", lot_name)
            if rm_match:
                rm_qtys.append(int(rm_match.group(1)))
                continue

            person_match = re.search(r"(\d+)\s*(?:человек|слушател)", lot_name)
            if person_match:
                person_qtys.append(int(person_match.group(1)))
                continue

            point_match = re.search(r"(\d+)\s*(?:точ|замер)", lot_name)
            if point_match:
                point_qtys.append(int(point_match.group(1)))
                continue

            position_match = re.search(r"(\d+)\s*(?:должност|позиц)", lot_name)
            if position_match:
                position_qtys.append(int(position_match.group(1)))
                continue

        if rm_qtys:
            total = sum(rm_qtys)
            sanitized = KtruParser._sanitize_quantity(total, nmck, "sout")
            if sanitized > 0:
                result["rm_total"] = int(sanitized)
                result["rm_total_source"] = "ktru"
                result["unit_type"] = "rm"
                result["ktru_confidence"] = 0.8
                logger.info(
                    f"[KTRU-223] Найдено {result['rm_total']} РМ "
                    f"({len(rm_qtys)} лотов, source=ktru)"
                )

        elif person_qtys:
            unique_persons = set(person_qtys)
            students = max(unique_persons) if unique_persons else sum(person_qtys)
            sanitized = KtruParser._sanitize_quantity(students, nmck, "education")
            if sanitized > 0:
                result["students_count"] = int(sanitized)
                result["students_count_source"] = "ktru"
                result["unit_type"] = "person"
                result["ktru_confidence"] = 0.8
                logger.info(
                    f"[KTRU-223] Найдено {result['students_count']} слушателей "
                    f"({len(person_qtys)} лотов, source=ktru)"
                )

        elif point_qtys:
            total = sum(point_qtys)
            sanitized = KtruParser._sanitize_quantity(total, nmck, "plk")
            if sanitized > 0:
                result["points_count"] = int(sanitized)
                result["points_source"] = "ktru"
                result["unit_type"] = "point"
                result["ktru_confidence"] = 0.8
                logger.info(
                    f"[KTRU-223] Найдено {result['points_count']} точек "
                    f"({len(point_qtys)} лотов, source=ktru)"
                )

        elif position_qtys:
            total = sum(position_qtys)
            sanitized = KtruParser._sanitize_quantity(total, nmck, "opr")
            if sanitized > 0:
                result["opr_positions"] = int(sanitized)
                result["opr_positions_source"] = "ktru"
                result["unit_type"] = "position"
                result["ktru_confidence"] = 0.8
                logger.info(
                    f"[KTRU-223] Найдено {result['opr_positions']} должностей "
                    f"({len(position_qtys)} лотов, source=ktru)"
                )

        return result

    @staticmethod
    def _sanitize_quantity(
        quantity: float,
        nmck: float,
        tender_type: str,
        price_per_unit: Optional[float] = None,
    ) -> float:
        """
        Qty из КТРУ — объём из ЕИС.
        Не сравнивать с себестоимостью costs_db (213 ₽/РМ и т.п.).
        Сброс только при явном мусоре парсинга.
        """
        if not quantity or quantity <= 0:
            return 0

        if quantity > 100_000:
            logger.warning(
                f"[KTRU] Sanity: qty={quantity} > 100000 → сброс (мусор парсинга)"
            )
            return 0

        if nmck > 0:
            unit_from_nmck = nmck / quantity
            # Похоже на перепутанные колонки (огромный qty, копейки за единицу)
            if unit_from_nmck < 20 and quantity > 100:
                logger.warning(
                    f"[KTRU] Sanity: qty={quantity}, НМЦК/qty={unit_from_nmck:.1f}₽ "
                    f"→ сброс (похоже на инверсию колонок)"
                )
                return 0

            warn_floor = KtruParser.LOW_UNIT_PRICE_WARN.get(tender_type, 50)
            ppu = (
                price_per_unit
                if price_per_unit and price_per_unit > 0
                else unit_from_nmck
            )
            if ppu < warn_floor:
                logger.warning(
                    f"[KTRU] Низкая цена в извещении: ~{ppu:.0f}₽/ед. "
                    f"(тип {tender_type}, qty={quantity}, НМЦК={nmck:,.0f}). "
                    f"Qty оставляем; экономика — в калькуляторе (costs_db)."
                )

        return quantity

    @staticmethod
    def _parse_education_smart(data: Dict[str, List[float]], nmck: float) -> float:
        """Умный парсер для обучения с детекцией инверсии колонок."""
        qtys = data["qty"]
        prices = data["prices"]

        if not qtys:
            return 0

        avg_qty = sum(qtys) / len(qtys) if qtys else 0
        avg_price = sum(prices) / len(prices) if prices else 0

        if avg_qty > 100 and 0 < avg_price < 100:
            unique_people = set(int(p) for p in prices if p > 0)
            if unique_people:
                students = max(unique_people)
                logger.info(
                    f"[KTRU] Обнаружена инверсия колонок: "
                    f"'количество'={avg_qty:.0f} (это цена), "
                    f"'цена за ед.'={avg_price:.0f} (это люди). "
                    f"Используем max(unique)={students}"
                )
                return KtruParser._sanitize_quantity(students, nmck, "education")

        unique_qtys = set(int(q) for q in qtys if q > 0)
        if unique_qtys:
            students = max(unique_qtys)
        else:
            students = sum(qtys)

        return KtruParser._sanitize_quantity(students, nmck, "education")

    @staticmethod
    def _parse_float(text: str) -> Optional[float]:
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
