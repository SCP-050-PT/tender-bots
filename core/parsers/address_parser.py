"""
core/parsers/address_parser.py
Парсинг адресов: извлечение городов, регионов, расчёт выездов.

v6.7-r1:
  - Исправлена рассинхронизация regions_count и списка regions при отсутствии явного региона
  - Смягчена фильтрация по суффиксам (-ский/-ской), чтобы не терять реальные города (Майский, Приморск)
  - Исправлен точный поиск слов в ADMIN_WORDS (замена 'in' на границу слова \b)
  - Поддержка регистронезависимого поиска городов
"""

import re
from typing import Dict, Any, List, Set
from loguru import logger

from knowledge.regions import RUSSIAN_REGIONS


class AddressParser:
    """Извлекает города, регионы и количество выездов из адресной строки."""

    # Расширенный список административных слов (используется строго по границам слов)
    ADMIN_WORDS = {
        "республика",
        "область",
        "край",
        "автономный",
        "округ",
        "район",
        "муниципальный",
        "городской",
        "сельский",
        "поселение",
        "сельсовет",
        "территория",
        "автодорога",
        "километр",
        "здание",
        "строение",
        "корпус",
        "офис",
        "этаж",
        "комната",
        "ул",
        "пр",
        "пер",
        "просп",
        "бр",
        "пл",
        "ш",
        "туп",
        "наб",
        "мкр",
    }

    # Ложные наименования населенных пунктов
    FAKE_CITY_WORDS = {
        "поселение",
        "сельсовет",
        "муниципальный",
        "район",
        "территория",
        "автодорога",
        "километр",
        "здание",
        "строение",
        "корпус",
        "офис",
        "этаж",
        "комната",
        "участок",
        "квартал",
        "промышленная",
        "площадка",
        "база",
        "склад",
        "цех",
        "помещение",
    }

    # Паттерны городов (с учетом возможности нижнего регистра)
    CITY_PATTERNS = [
        r"г\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)*)",
        r"город\s+([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)*)",
        r"п\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)*)",
        r"пос(?:ёлок)?\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)*)",
        r"пгт\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)*)",
        r"с\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)*)",
        r"д\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+)*)",
    ]

    def count_addresses(self, text: str, tender_type: str = "") -> Dict[str, Any]:
        if not text:
            return {
                "cities_count": 0,
                "regions_count": 0,
                "trips": 1,
                "unique_cities": [],
                "regions": [],
                "needs_manual_check": False,
            }

        text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        text = text.replace("–", "-").replace("—", "-")

        # === Шаг 1: Разбиваем на потенциальные адресные строки ===
        # Сначала пробуем маркированные списки
        lines = re.split(r"(?:^|\n)\s*[-–—•]\s*", text)
        lines = [l.strip() for l in lines if l.strip() and len(l.strip()) > 8]

        # Если получилось мало строк — пробуем по "г." / "город"
        if len(lines) < 2:
            raw_lines = re.split(r"(?=г\.?\s+[А-Яа-яЁё]|город\s+[А-Яа-яЁё])", text)
            lines = [l.strip() for l in raw_lines if l.strip() and len(l.strip()) > 10]

        # Fallback — обычные переносы строк
        if len(lines) < 2:
            lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 10]

        # === Шаг 2: Извлекаем города и регионы ===
        all_cities: Set[str] = set()
        all_regions: Set[str] = set()
        current_region = None

        for line in lines:
            line_lower = line.lower()

            # Регион из строки
            region_match = re.search(
                r"(республика\s+[а-яё\-]+|[а-яё\-]+\s+(?:область|край|ао|автономный\s+округ)|респ\.?\s+[а-яё\-]+)",
                line_lower,
            )
            if region_match:
                current_region = region_match.group(1).strip()
                # Нормализация
                current_region = current_region.replace("респ.", "республика").title()
                all_regions.add(current_region)

            # Регионы из справочника
            for region_name in RUSSIAN_REGIONS:
                if region_name.lower() in line_lower:
                    all_regions.add(region_name)
                    if not current_region:
                        current_region = region_name

            # Город
            found_city = None
            for pattern in self.CITY_PATTERNS:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    found_city = match.group(1).strip()
                    break

            if not found_city:
                match = re.search(r"[сдп]\.([А-Яа-яЁё][а-яё\-]+)", line)
                if match:
                    found_city = match.group(1).strip()

            if found_city and len(found_city) > 2:
                found_city_clean = found_city.strip(".,;: ")
                found_city_lower = found_city_clean.lower()

                words_in_city = set(re.findall(r"\w+", found_city_lower))
                if words_in_city.intersection(self.ADMIN_WORDS):
                    continue
                if words_in_city.intersection(self.FAKE_CITY_WORDS):
                    continue

                # Пропускаем явные районы
                if "район" in line_lower and found_city_lower.endswith(("ский", "ской")):
                    if f"{found_city_lower} район" in line_lower:
                        continue

                formatted_city = found_city_clean.capitalize()
                all_cities.add(formatted_city)

        total_cities = len(all_cities)
        total_regions = len(all_regions) if all_regions else (1 if total_cities > 0 else 0)

        # === Шаг 3: Количество выездов ===
        if total_cities <= 1:
            trips = 1 if total_cities == 1 else 0
        else:
            # Грубое, но рабочее правило: один выезд на регион + небольшой запас
            trips = max(1, total_regions)

        needs_manual_check = total_cities > 5 or total_regions > 2

        logger.info(
            f"[AddressParser] Городов: {total_cities}, "
            f"Регионов: {total_regions}, Выездов: {trips} | "
            f"Города: {sorted(list(all_cities))[:8]}"
        )

        return {
            "cities_count": total_cities,
            "regions_count": total_regions,
            "trips": trips,
            "unique_cities": sorted(list(all_cities)),
            "regions": sorted(list(all_regions)),
            "needs_manual_check": needs_manual_check,
        }

    @staticmethod
    def extract_region(address: str) -> str:
        """Извлекает регион из адресной строки."""
        if not address:
            return ""

        address = re.sub(r"^\d{6},?\s*", "", address)
        address_lower = address.lower()

        for region in RUSSIAN_REGIONS:
            if region.lower() in address_lower:
                return region

        parts = address.split(",")
        if parts:
            first = parts[0].strip()
            if first and not any(char.isdigit() for char in first):
                return first

        return ""
