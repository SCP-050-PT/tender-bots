"""
core/parsers/address_parser.py
Парсинг адресов: извлечение городов, регионов, расчёт выездов.

v6.8.0:
  - Whitelist городов через CITY_TO_REGION (отсекает мусор)
  - Расширенный FAKE_CITY_WORDS / JUNK_SUBSTRINGS
  - Убран баг: повторный split lines внутри цикла for line
  - Если валидных городов 0 — cities_count=0 (не раздувать транспорт)
"""

import re
from typing import Dict, Any, Set
from loguru import logger

from knowledge.regions import RUSSIAN_REGIONS, CITY_TO_REGION


class AddressParser:
    """Извлекает города, регионы и количество выездов из адресной строки."""

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

    # Подстроки, при наличии которых «город» — мусор
    JUNK_SUBSTRINGS = (
        "заявк",
        "договор",
        "товар",
        "услуг",
        "требован",
        "характеристик",
        "поставк",
        "обеспечен",
        "заказчик",
        "участник",
        "контракт",
        "исполнен",
        "приемк",
        "гарант",
        "извещен",
        "протокол",
        "таблиц",
        "согласно",
        "соответств",
        "указывает",
        "значение",
        "диапазон",
        "коррупц",
        "метрополитен",
        "технолог",
        "организац",
        "коллектив",
        # v8.2: мусор из КТРУ/ОКПД
        "окпд",
        "окато",
        "оксм",
        "кбк",
        "ктру",
        "окейд",
    )

    CITY_PATTERNS = [
        r"г\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+){0,2})",
        r"город\s+([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+){0,2})",
        r"пгт\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+){0,2})",
        r"пос(?:ёлок|елок)?\.?\s*([А-Яа-яЁё\-]+(?:\s+[А-Яа-яЁё\-]+){0,2})",
    ]

    def count_addresses(self, text: str, tender_type: str = "") -> Dict[str, Any]:
        empty = {
            "cities_count": 0,
            "regions_count": 0,
            "trips": 1,
            "unique_cities": [],
            "regions": [],
            "needs_manual_check": False,
            "is_reliable": False,
        }
        if not text:
            return empty

        text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        text = text.replace("–", "-").replace("—", "-")

        # --- Разбиение на строки (один раз, до цикла) ---
        lines = re.split(r"(?:^|\n)\s*[-–—•]\s+", text)
        lines = [l.strip() for l in lines if l.strip() and len(l.strip()) > 8]

        if len(lines) < 2:
            raw_lines = re.split(r"(?=г\.?\s+[А-Яа-яЁё]|город\s+[А-Яа-яЁё])", text)
            lines = [l.strip() for l in raw_lines if l.strip() and len(l.strip()) > 10]

        if len(lines) < 2:
            lines = [
                l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 10
            ]

        all_cities: Set[str] = set()
        all_regions: Set[str] = set()

        known_cities_lower = set(CITY_TO_REGION.keys())

        for line in lines:
            line_lower = line.lower()

            # Регионы
            region_match = re.search(
                r"(республика\s+[а-яё\-]+|[а-яё\-]+\s+(?:область|край|ао|автономный\s+округ)|респ\.?\s+[а-яё\-]+)",
                line_lower,
            )
            if region_match:
                reg = (
                    region_match.group(1).strip().replace("респ.", "республика").title()
                )
                all_regions.add(reg)

            for region_name in RUSSIAN_REGIONS:
                if region_name.lower() in line_lower:
                    all_regions.add(region_name)

            # Город только по явным маркерам г./город/пгт
            found_city = None
            for pattern in self.CITY_PATTERNS:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    found_city = match.group(1).strip()
                    break

            if not found_city or len(found_city) < 2 or len(found_city) > 30:
                continue

            found_city_clean = found_city.strip(".,;: ")
            found_city_lower = found_city_clean.lower()

            words_in_city = set(re.findall(r"\w+", found_city_lower))
            if words_in_city.intersection(self.ADMIN_WORDS):
                continue
            if words_in_city.intersection(self.FAKE_CITY_WORDS):
                continue
            if any(j in found_city_lower for j in self.JUNK_SUBSTRINGS):
                continue
            if "район" in line_lower and found_city_lower.endswith(("ский", "ской")):
                if f"{found_city_lower} район" in line_lower:
                    continue

            # Whitelist: либо известный город, либо короткий токен после «г.»
            is_known = found_city_lower in known_cities_lower
            # допускаем «г. X» из 1–2 слов, если нет мусора
            is_plausible = (
                len(found_city_clean.split()) <= 2
                and found_city_clean[0].isupper()
                and not any(c.isdigit() for c in found_city_clean)
            )

            if not is_known and not is_plausible:
                continue

            # Если не из словаря — помечаем, но не считаем «надёжным»
            all_cities.add(
                found_city_clean if is_known else found_city_clean.capitalize()
            )

        # Оставляем только известные города для подсчёта транспорта
        validated = [c for c in all_cities if c.lower() in known_cities_lower]
        # Если whitelist пуст, но нашли 1–2 plausible — осторожно принимаем
        if not validated and 0 < len(all_cities) <= 2:
            validated = list(all_cities)
            is_reliable = False
        else:
            is_reliable = len(validated) > 0

        total_cities = len(validated)
        total_regions = (
            len(all_regions) if all_regions else (1 if total_cities > 0 else 0)
        )

        if total_cities <= 1:
            trips = 1 if total_cities == 1 else 0
        else:
            trips = max(1, total_regions)

        needs_manual_check = total_cities > 5 or total_regions > 2

        logger.info(
            f"[AddressParser] Городов: {total_cities}, "
            f"Регионов: {total_regions}, Выездов: {trips} | "
            f"Города: {sorted(validated)[:8]} | reliable={is_reliable}"
        )

        return {
            "cities_count": total_cities,
            "regions_count": total_regions,
            "trips": trips,
            "unique_cities": sorted(validated),
            "regions": sorted(list(all_regions)),
            "needs_manual_check": needs_manual_check,
            "is_reliable": is_reliable,
        }

    @staticmethod
    def extract_region(address: str) -> str:
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
