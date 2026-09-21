"""
core/search/parser.py
Парсинг HTML-карточек тендеров с zakupki.gov.ru.
Вынесено из searcher.py (v6.6-r2).

РЕФАКТОРИНГ v6.8.6:
  - Добавлено извлечение region из адреса заказчика
  - Добавлен _extract_region_from_address()
"""

import re
from typing import Optional, List, Dict, Any, Generator
from dataclasses import dataclass, field
from loguru import logger

from bs4 import BeautifulSoup
from knowledge.regions import RegionResolver

@dataclass
class TenderSearchResult:
    """Результат поиска тендера."""

    tender_id: str
    title: str
    url: str
    nmck: Optional[float] = None
    region: Optional[str] = None
    publish_date: Optional[str] = None
    deadline_date: Optional[str] = None
    etp: str = "zakupki.gov.ru"
    law: str = "44-FZ"
    okpd2: List[str] = field(default_factory=list)
    customer: Optional[str] = None
    status: Optional[str] = None
    notice_guid: Optional[str] = None
    raw_html: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "tender_id": self.tender_id,
            "title": self.title,
            "url": self.url,
            "nmck": self.nmck,
            "region": self.region,
            "publish_date": self.publish_date,
            "deadline_date": self.deadline_date,
            "etp": self.etp,
            "law": self.law,
            "okpd2": self.okpd2,
            "customer": self.customer,
            "status": self.status,
            "notice_guid": self.notice_guid,
        }


class SearchResultParser:
    """Парсит HTML-страницы поиска тендеров."""

    def __init__(self, url_builder=None, price_parser=None):
        self.url_builder = url_builder
        self.price_parser = price_parser

    def parse_search_page(self, html: str) -> Generator[TenderSearchResult, None, None]:
        """Парсит страницу поиска и возвращает результаты."""
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", class_="registry-entry__form")

        logger.info(f"[SearchResultParser] Найдено карточек: {len(cards)}")

        for card in cards:
            try:
                result = self._parse_card(card)
                if result:
                    yield result
            except Exception as e:
                logger.debug(f"[SearchResultParser] Ошибка парсинга карточки: {e}")
                continue

    def _parse_card(self, card: Any) -> Optional[TenderSearchResult]:
        """Парсит одну карточку тендера."""
        law_elem = card.find("div", class_="registry-entry__header-top__title")
        law_text = law_elem.get_text(strip=True) if law_elem else ""

        if "223" in law_text:
            law = "223-FZ"
        elif "615" in law_text:
            law = "615-FZ"
        elif "44" in law_text:
            law = "44-FZ"
        else:
            law = "44/223/615"

        number_elem = card.find("div", class_="registry-entry__header-mid__number")
        if not number_elem:
            return None

        link = number_elem.find("a", href=True)
        if not link:
            return None

        tender_id = link.get_text(strip=True).replace("№", "").strip()
        if not tender_id:
            return None

        # URL карточки
        if self.url_builder:
            purchase_url = self.url_builder.build_common_info_url(
                reg_number=tender_id, law_type=law.replace("-FZ", "")
            )
        else:
            purchase_url = f"https://zakupki.gov.ru/epz/order/notice/ea44/view/common-info.html?regNumber={tender_id}"

        # Статус
        status_elem = card.find("div", class_="registry-entry__header-mid__title")
        status = status_elem.get_text(strip=True) if status_elem else None

        # Название
        title = ""
        obj_block = card.find("div", class_="registry-entry__body-block")
        if obj_block:
            title_elem = obj_block.find("div", class_="registry-entry__body-value")
            if title_elem:
                title = title_elem.get_text(strip=True)

        # Заказчик
        customer = None
        customer_elem = card.find("div", class_="registry-entry__body-href")
        if customer_elem:
            customer_link = customer_elem.find("a")
            if customer_link:
                customer = customer_link.get_text(strip=True)

        # Цена
        nmck = None
        price_elem = card.find("div", class_="price-block__value")
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            if self.price_parser:
                nmck = self.price_parser.parse(price_text)
            else:
                nmck = self._parse_price_fallback(price_text)

        # Даты
        publish_date = None
        deadline_date = None

        data_blocks = card.find_all("div", class_="data-block__value")
        data_titles = card.find_all("div", class_="data-block__title")

        for title_elem, value_elem in zip(data_titles, data_blocks):
            title_text = title_elem.get_text(strip=True)
            value_text = value_elem.get_text(strip=True)

            if "Размещено" in title_text:
                publish_date = value_text
            elif "Окончание подачи заявок" in title_text:
                deadline_date = value_text

        # noticeGuid для 223-ФЗ
        notice_guid = None
        if law in ["223-FZ", "615-FZ"]:
            docs_href = card.find("a", href=re.compile(r"noticeGuid="))
            if docs_href:
                href_docs = docs_href.get("href", "")
                match = re.search(r"noticeGuid=([^&]+)", href_docs)
                if match:
                    notice_guid = match.group(1)

        # ========== v6.8.6: ИЗВЛЕЧЕНИЕ РЕГИОНА ==========
        region = self._extract_region(card, customer)

        return TenderSearchResult(
            tender_id=tender_id,
            title=title,
            url=purchase_url,
            nmck=nmck,
            region=region,
            publish_date=publish_date,
            deadline_date=deadline_date,
            etp="zakupki.gov.ru",
            law=law,
            okpd2=[],
            customer=customer,
            status=status,
            notice_guid=notice_guid,
        )

    def _extract_region(self, card: Any, customer: Optional[str]) -> Optional[str]:
        """Извлекает регион из карточки тендера (поисковая выдача)."""
        # Пробуем найти адрес заказчика в карточке
        address = None
        address_elems = card.find_all("div", class_="registry-entry__body-value")
        for elem in address_elems:
            text = elem.get_text(strip=True)
            if any(
                kw in text.lower()
                for kw in ["обл.", "край", "респ.", "г.", "ул.", "проспект"]
            ):
                address = text
                break

        # Используем единый резолвер
        text_to_search = address or customer or ""
        return RegionResolver.resolve_from_search_text(text_to_search)

    def extract_total_count(self, html: str) -> int:
        """Извлекает общее количество результатов из HTML."""
        soup = BeautifulSoup(html, "html.parser")
        patterns = [
            r"более\s*([\d\s]+)\s*записей",
            r"([\d\s]+)\s*записей",
            r"найдено\s*[:—]?\s*([\d\s]+)",
            r"найдено\s*([\d\s]+)\s*запис",
            r"всего\s*([\d\s]+)",
            r"результатов:\s*([\d\s]+)",
        ]
        text = soup.get_text()
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                count_str = (
                    match.group(1)
                    .replace(" ", "")
                    .replace("\xa0", "")
                    .replace("\u202f", "")
                )
                try:
                    return int(count_str)
                except ValueError:
                    continue
        return 0

    @staticmethod
    def _parse_price_fallback(price_text: str) -> Optional[float]:
        """Fallback-парсинг цены."""
        if not price_text:
            return None
        cleaned = re.sub(r"[^\d,\.]", "", price_text)
        cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
