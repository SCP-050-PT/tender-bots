"""
core/parsers/ktru_parser.py
Парсинг КТРУ из common-info (44-ФЗ) и lot-list (223-ФЗ).

v7.5.0:
  - Добавлен Sanity Check для защиты от абсурдных значений (P0-1)
  - Умный парсер для обучения с детекцией инверсии колонок
  - Передача nmck для валидации
  - Замена суммирования на max(unique) для обучения
"""

import re
from typing import Dict, Any, Optional, List
from bs4 import BeautifulSoup
from loguru import logger


class KtruParser:
    """Извлекает количества из КТРУ (44-ФЗ) и lot-list (223-ФЗ)."""

    # Минимальные рыночные цены за единицу (руб) для sanity check
    MIN_PRICES = {
        "education": 1500,  # Минимум за слушателя (дистант)
        "sout": 800,  # Минимум за РМ
        "opr": 500,  # Минимум за должность
        "plk": 300,  # Минимум за точку
    }

    @staticmethod
    def parse(soup: BeautifulSoup, nmck: float = 0) -> Dict[str, Any]:
        result = {
            "rm_total": None,
            "students_count": None,
            "points_count": None,
            "opr_positions": None,
            "unit_type": None,
            "price_per_unit": None,
            "ktru_confidence": 0.0,
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

        # Собираем данные по типам
        rm_data = {"qty": [], "prices": []}
        person_data = {"qty": [], "prices": []}
        point_data = {"qty": []}
        position_data = {"qty": []}

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

        # Обработка РМ
        if rm_data["qty"]:
            total_rm = sum(rm_data["qty"])
            sanitized_rm = KtruParser._sanitize_quantity(total_rm, nmck, "sout")
            if sanitized_rm > 0:
                result["rm_total"] = int(sanitized_rm)
                result["unit_type"] = "rm"
                result["ktru_confidence"] = 1.0
                if rm_data["prices"]:
                    result["price_per_unit"] = sum(rm_data["prices"]) / len(
                        rm_data["prices"]
                    )
                logger.info(
                    f"[KTRU] Найдено {result['rm_total']} РМ ({len(rm_data['qty'])} позиций)"
                )
            else:
                logger.warning(
                    f"[KTRU] Sanity Check: РМ={total_rm} сброшено в 0 (НМЦК={nmck})"
                )

        # Обработка обучения (умный парсер)
        elif person_data["qty"]:
            students = KtruParser._parse_education_smart(person_data, nmck)
            if students and students > 0:
                result["students_count"] = int(students)
                result["unit_type"] = "person"
                result["ktru_confidence"] = 1.0
                logger.info(
                    f"[KTRU] Найдено {result['students_count']} слушателей ({len(person_data['qty'])} позиций)"
                )
            else:
                logger.warning(
                    f"[KTRU] Sanity Check: Слушатели сброшены в 0 (НМЦК={nmck})"
                )

        # Обработка точек ПЛК
        elif point_data["qty"]:
            total_points = sum(point_data["qty"])
            sanitized_points = KtruParser._sanitize_quantity(total_points, nmck, "plk")
            if sanitized_points > 0:
                result["points_count"] = int(sanitized_points)
                result["unit_type"] = "point"
                result["ktru_confidence"] = 1.0
                logger.info(
                    f"[KTRU] Найдено {result['points_count']} точек ({len(point_data['qty'])} позиций)"
                )
            else:
                logger.warning(
                    f"[KTRU] Sanity Check: Точки={total_points} сброшены в 0 (НМЦК={nmck})"
                )

        # Обработка должностей ОПР
        elif position_data["qty"]:
            total_positions = sum(position_data["qty"])
            sanitized_positions = KtruParser._sanitize_quantity(
                total_positions, nmck, "opr"
            )
            if sanitized_positions > 0:
                result["opr_positions"] = int(sanitized_positions)
                result["unit_type"] = "position"
                result["ktru_confidence"] = 1.0
                logger.info(
                    f"[KTRU] Найдено {result['opr_positions']} должностей ({len(position_data['qty'])} позиций)"
                )
            else:
                logger.warning(
                    f"[KTRU] Sanity Check: Должности={total_positions} сброшены в 0 (НМЦК={nmck})"
                )

        return result

    @staticmethod
    def parse_223_lot_list(soup: BeautifulSoup, nmck: float = 0) -> Dict[str, Any]:
        result = {
            "rm_total": None,
            "students_count": None,
            "points_count": None,
            "opr_positions": None,
            "unit_type": None,
            "ktru_confidence": 0.0,
        }

        table = soup.find("table", {"class": "table"})
        if not table:
            logger.debug("[KTRU-223] Таблица лотов не найдена")
            return result

        rows = table.find_all("tr")

        rm_qtys = []
        person_qtys = []
        point_qtys = []
        position_qtys = []

        for row in rows:
            if row.find("th"):
                continue
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            lot_name = cols[0].get_text(strip=True).lower() if len(cols) > 0 else ""
            qty = 0

            rm_match = re.search(r"(\d+)\s*рабоч", lot_name)
            if rm_match:
                qty = int(rm_match.group(1))
                rm_qtys.append(qty)
                continue

            person_match = re.search(r"(\d+)\s*(?:человек|слушател)", lot_name)
            if person_match:
                qty = int(person_match.group(1))
                person_qtys.append(qty)
                continue

            point_match = re.search(r"(\d+)\s*(?:точ|замер)", lot_name)
            if point_match:
                qty = int(point_match.group(1))
                point_qtys.append(qty)
                continue

            position_match = re.search(r"(\d+)\s*(?:должност|позиц)", lot_name)
            if position_match:
                qty = int(position_match.group(1))
                position_qtys.append(qty)
                continue

        # Обработка с sanity check
        if rm_qtys:
            total = sum(rm_qtys)
            sanitized = KtruParser._sanitize_quantity(total, nmck, "sout")
            if sanitized > 0:
                result["rm_total"] = int(sanitized)
                result["unit_type"] = "rm"
                result["ktru_confidence"] = 0.8
                logger.info(
                    f"[KTRU-223] Найдено {result['rm_total']} РМ ({len(rm_qtys)} лотов)"
                )

        elif person_qtys:
            # Для 223-ФЗ тоже применяем умную логику, если есть данные
            unique_persons = set(person_qtys)
            students = max(unique_persons) if unique_persons else sum(person_qtys)
            sanitized = KtruParser._sanitize_quantity(students, nmck, "education")
            if sanitized > 0:
                result["students_count"] = int(sanitized)
                result["unit_type"] = "person"
                result["ktru_confidence"] = 0.8
                logger.info(
                    f"[KTRU-223] Найдено {result['students_count']} слушателей ({len(person_qtys)} лотов)"
                )

        elif point_qtys:
            total = sum(point_qtys)
            sanitized = KtruParser._sanitize_quantity(total, nmck, "plk")
            if sanitized > 0:
                result["points_count"] = int(sanitized)
                result["unit_type"] = "point"
                result["ktru_confidence"] = 0.8
                logger.info(
                    f"[KTRU-223] Найдено {result['points_count']} точек ({len(point_qtys)} лотов)"
                )

        elif position_qtys:
            total = sum(position_qtys)
            sanitized = KtruParser._sanitize_quantity(total, nmck, "opr")
            if sanitized > 0:
                result["opr_positions"] = int(sanitized)
                result["unit_type"] = "position"
                result["ktru_confidence"] = 0.8
                logger.info(
                    f"[KTRU-223] Найдено {result['opr_positions']} должностей ({len(position_qtys)} лотов)"
                )

        return result

    @staticmethod
    def _sanitize_quantity(quantity: float, nmck: float, tender_type: str) -> float:
        """Защита от абсурдных значений из КТРУ (P0-1)."""
        if not quantity or quantity <= 0:
            return 0

        min_price = KtruParser.MIN_PRICES.get(tender_type, 1000)

        # Если количество × мин_цена > НМЦК × 2 — явная ошибка парсинга
        if nmck > 0 and (quantity * min_price) > (nmck * 2):
            logger.warning(
                f"⚠️ SANITY CHECK: КТРУ дал {quantity} ({tender_type}), но "
                f"{quantity} × {min_price}₽ = {quantity * min_price:,.0f}₽ >> НМЦК {nmck:,.0f}₽. "
                f"Сбрасываем в 0 для fallback."
            )
            return 0

        return quantity

    @staticmethod
    def _parse_education_smart(data: Dict[str, List[float]], nmck: float) -> float:
        """Умный парсер для обучения с детекцией инверсии колонок."""
        qtys = data["qty"]
        prices = data["prices"]

        if not qtys:
            return 0

        # Детекция инверсии: если "количество" содержит большие числа (>100),
        # а "цена за ед." — маленькие (<100), значит реальное кол-во людей в "цене за ед."
        avg_qty = sum(qtys) / len(qtys) if qtys else 0
        avg_price = sum(prices) / len(prices) if prices else 0

        if avg_qty > 100 and 0 < avg_price < 100:
            # Количество людей — в колонке "цена за ед."
            unique_people = set(int(p) for p in prices if p > 0)
            if unique_people:
                students = max(unique_people)  # Берём максимум (основная группа)
                logger.info(
                    f"[KTRU] Обнаружена инверсия колонок: "
                    f"'количество'={avg_qty:.0f} (это цена), "
                    f"'цена за ед.'={avg_price:.0f} (это люди). "
                    f"Используем max(unique)={students}"
                )
                return KtruParser._sanitize_quantity(students, nmck, "education")

        # Стандартная логика: берем max(unique) вместо суммы
        # (одни и те же люди проходят несколько программ)
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
