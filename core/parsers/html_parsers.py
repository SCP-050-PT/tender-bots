"""
core/parsers/html_parsers.py
Парсинг HTML карточек тендеров 44-ФЗ и 223-ФЗ.
Вынесено из detailed_parser.py (v6.5).
"""

import re
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger

from utils.price_parser import get_price_parser


class BaseHtmlParser:
    """Базовый класс для HTML-парсеров."""

    BASE_URL = "https://zakupki.gov.ru"

    def __init__(self):
        self.price_parser = get_price_parser()

    def _extract_text_by_title(self, soup: BeautifulSoup, title_text: str) -> str:
        for block in soup.find_all("div", class_="col-9 mr-auto"):
            title_elem = block.find("div", class_="common-text__title")
            if (
                title_elem
                and title_text.lower() in title_elem.get_text(strip=True).lower()
            ):
                value_elem = block.find("div", class_="common-text__value")
                if value_elem:
                    return value_elem.get_text(strip=True)
        return ""

    def _extract_url_by_title(self, soup: BeautifulSoup, title_text: str) -> str:
        for block in soup.find_all("div", class_="col-9 mr-auto"):
            title_elem = block.find("div", class_="common-text__title")
            if (
                title_elem
                and title_text.lower() in title_elem.get_text(strip=True).lower()
            ):
                link = block.find("a", href=True)
                if link:
                    href = link.get("href", "")
                    return urljoin(self.BASE_URL, href) if href else ""
        return ""

    def _extract_inn_from_soup(self, soup: BeautifulSoup) -> str:
        inn_elem = soup.find("div", class_="common-text__value--gray", string="ИНН")
        if inn_elem:
            inn_val = inn_elem.find_next("div", class_="common-text__value")
            if inn_val:
                return inn_val.get_text(strip=True)
        return ""

    def _extract_email_from_soup(self, soup: BeautifulSoup) -> str:
        mailto = soup.find("a", href=re.compile(r"mailto:", re.IGNORECASE))
        if mailto:
            href = mailto.get("href", "")
            return href.replace("mailto:", "").strip()
        return ""

    def _detect_file_type(self, src: str) -> str:
        src_lower = src.lower()
        extensions = ["docx", "doc", "xlsx", "xls", "pdf", "zip", "rar"]
        for ext in extensions:
            if ext in src_lower:
                return ext
        return ""


class Html44Parser(BaseHtmlParser):
    """Парсинг HTML карточки 44-ФЗ."""

    def parse_common_info(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """Парсит common-info для 44-ФЗ."""
        result = {}

        # Объект закупки
        obj_section = soup.find(
            "span", class_="cardMainInfo__title", string="Объект закупки"
        )
        if obj_section:
            content = obj_section.find_next("span", class_="cardMainInfo__content")
            if content:
                result["purchase_name"] = content.get_text(strip=True)

        # Способ закупки
        title_div = soup.find("div", class_="cardMainInfo__title")
        if title_div:
            full_title = title_div.get_text(strip=True)
            result["purchase_method"] = full_title.replace("44-ФЗ", "").strip()

        # Заказчик
        org_section = soup.find(
            "span",
            class_="cardMainInfo__title",
            string=re.compile(r"Организация|Заказчик"),
        )
        if org_section:
            org_link = org_section.find_next("a")
            if org_link:
                result["customer_name"] = org_link.get_text(strip=True)

        # НМЦК
        price_elem = soup.find("span", class_="cardMainInfo__content cost")
        if price_elem:
            result["nmck"] = (
                self.price_parser.parse(price_elem.get_text(strip=True)) or 0.0
            )

        # Даты
        for section in soup.find_all("div", class_="cardMainInfo__section"):
            title = section.find("span", class_="cardMainInfo__title")
            value = section.find("span", class_="cardMainInfo__content")
            if not title or not value:
                continue
            title_text = title.get_text(strip=True)
            value_text = value.get_text(strip=True)

            if "Размещено" in title_text:
                result["publish_date"] = value_text
            elif "Окончание подачи заявок" in title_text:
                result["deadline_date"] = value_text
            elif "Обновлено" in title_text:
                result["current_revision_date"] = value_text

        # Регион
        region = self._extract_section_value_v5(soup, "Регион")
        if region:
            result["customer_region"] = region
        else:
            tz_elem = soup.find("div", class_="time-zone__value")
            if tz_elem:
                tz_text = tz_elem.get_text(strip=True)
                result["customer_region"] = tz_text.split()[0] if tz_text else ""

        # ЭТП
        result["platform_name"] = self._extract_section_value_v5(
            soup, "Наименование электронной площадки"
        )
        result["platform_url"] = self._extract_section_url_v5(
            soup, "Адрес электронной площадки"
        )

        # Требования
        result["requirements"] = self._extract_section_value_v5(
            soup, "Требования к участникам"
        )
        if not result.get("requirements"):
            result["requirements"] = self._extract_section_value_v5(
                soup, "Требования к участникам закупки"
            )

        # Обеспечение
        self._parse_guarantee_44(soup, result)

        # Адреса
        customer_addr = self._extract_section_value_v5(soup, "Почтовый адрес")
        if not customer_addr:
            customer_addr = self._extract_section_value_v5(soup, "Место нахождения")
        if customer_addr:
            result["customer_address"] = customer_addr

        delivery_addr = self._extract_section_value_v5(soup, "Место поставки")
        if delivery_addr:
            result["delivery_address"] = delivery_addr
        elif customer_addr:
            result["delivery_address"] = customer_addr

        # Контакты
        if not result.get("contact_person"):
            result["contact_person"] = self._extract_section_value_v5(
                soup, "Контактное лицо"
            )
        if not result.get("contact_email"):
            result["contact_email"] = self._extract_section_value_v5(
                soup, "Адрес электронной почты"
            )
        if not result.get("contact_phone"):
            result["contact_phone"] = self._extract_section_value_v5(
                soup, "Контактный телефон"
            )
        if not result.get("customer_inn"):
            result["customer_inn"] = self._extract_section_value_v5(soup, "ИНН")

        return result

    def _parse_guarantee_44(self, soup: BeautifulSoup, result: Dict[str, Any]):
        """Парсит обеспечение для 44-ФЗ."""
        app_guarantee = (
            self._extract_section_value_v5(soup, "Обеспечение заявки")
            or self._extract_section_value_v5(soup, "Обеспечение заявок")
            or self._extract_section_value_v5(soup, "Обеспечение заявки на участие")
            or self._extract_section_value_v5(soup, "Требование об обеспечении заявки")
            or self._extract_section_value_v5(
                soup, "Обеспечение заявки на участие в электронном аукционе"
            )
        )

        purchase_method = result.get("purchase_method", "").lower()
        if not app_guarantee and "аукцион" in purchase_method:
            app_guarantee = "Не требуется"
        elif not app_guarantee:
            has_app_block = bool(
                soup.find(
                    "span",
                    class_="section__title",
                    string=re.compile(r"Обеспечение заявки"),
                )
            )
            if not has_app_block:
                app_guarantee = "Не требуется"

        result["application_guarantee"] = app_guarantee

        contract_raw = (
            self._extract_section_value_v5(soup, "Обеспечение исполнения контракта")
            or self._extract_section_value_v5(soup, "Обеспечение контракта")
            or self._extract_section_value_v5(soup, "Обеспечение исполнения")
        )
        contract_percent = self._extract_section_value_v5(
            soup, "Размер обеспечения исполнения контракта"
        )
        if contract_raw == "Да" and contract_percent:
            contract_guarantee = f"Да ({contract_percent})"
        else:
            contract_guarantee = contract_raw
        result["contract_guarantee"] = contract_guarantee

        guarantee_method = (
            self._extract_section_value_v5(soup, "Способ обеспечения")
            or self._extract_section_value_v5(soup, "Способы обеспечения")
            or self._extract_section_value_v5(soup, "Вид обеспечения")
        )
        result["guarantee_method"] = guarantee_method

        if not result.get("application_guarantee") and not result.get(
            "contract_guarantee"
        ):
            requirements = result.get("requirements", "")
            if requirements and "не требуется" in requirements.lower():
                result["application_guarantee"] = "Не требуется"
                result["contract_guarantee"] = "Не требуется"

    def parse_ktru_positions(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """Парсит таблицу позиций КТРУ из HTML карточки 44-ФЗ."""
        result = {
            "rm_total": None,
            "students_count": None,
            "unit_type": None,
            "price_per_unit": None,
            "ktru_confidence": 0.0,
        }

        table_container = soup.find("div", id="purchaseObjectTruTable1") or soup.find(
            "table", {"class": "blockInfo__table"}
        )
        if not table_container:
            return result

        table = (
            table_container
            if table_container.name == "table"
            else table_container.find("table", {"class": "blockInfo__table"})
        )
        if not table:
            return result

        rows = table.find_all("tr", {"class": "tableBlock__row"})
        total_quantity = 0.0
        has_rm = False
        has_person = False
        prices = []
        row_count = 0

        for row in rows:
            row_classes = " ".join(row.get("class", []))
            if "tableBlock__foot" in row_classes:
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

            # Извлечение чисел через регулярные выражения для безопасной конвертации
            qty_match = re.search(
                r"[\d\.,]+", qty_text.replace(" ", "").replace("\xa0", "")
            )
            price_match = re.search(
                r"[\d\.,]+", price_text.replace(" ", "").replace("\xa0", "")
            )

            qty_val = float(qty_match.group().replace(",", ".")) if qty_match else 0.0
            price_val = (
                float(price_match.group().replace(",", ".")) if price_match else None
            )

            if "рабочее место" in unit or "раб место" in unit:
                has_rm = True
                row_count += 1
                total_quantity += qty_val
                if price_val is not None:
                    prices.append(price_val)
            elif "человек" in unit:
                has_person = True
                row_count += 1
                total_quantity += qty_val

        if has_rm and total_quantity > 0:
            result["rm_total"] = int(total_quantity)
            result["unit_type"] = "rm"
            result["ktru_confidence"] = 1.0
            if prices:
                result["price_per_unit"] = sum(prices) / len(prices)
            logger.info(
                f"   [KTRU] Найдено {result['rm_total']} РМ ({row_count} позиций)"
            )
        elif has_person and total_quantity > 0:
            result["students_count"] = int(total_quantity)
            result["unit_type"] = "person"
            result["ktru_confidence"] = 1.0
            logger.info(
                f"   [KTRU] Найдено {result['students_count']} слушателей ({row_count} позиций)"
            )

        return result

    def parse_documents(self, soup: BeautifulSoup) -> List[Any]:
        """Парсит документы для 44-ФЗ. Возвращает список TenderDocument."""
        from core.parsers.detailed_parser import TenderDocument

        documents = []
        for section in soup.find_all(
            "div", class_=re.compile(r"notice-documents|protocols|changes")
        ):
            is_active = True
            status_elem = section.find(
                "div",
                class_="section__value",
                string=re.compile(r"Действующая|Недействующая"),
            )
            if status_elem:
                is_active = "Действующая" in status_elem.get_text(strip=True)

            date = ""
            date_elem = section.find(
                "div", class_="section__attrib", string="Размещено"
            )
            if date_elem:
                date_val = date_elem.find_next("div", class_="section__value")
                if date_val:
                    date = date_val.get_text(strip=True)

            for attachment in section.find_all("div", class_="attachment"):
                doc = self._parse_attachment_44(attachment, is_active, date)
                if doc:
                    documents.append(doc)

        return documents

    def _parse_attachment_44(
        self, attachment, is_active: bool = True, doc_date: str = ""
    ) -> Optional[Any]:
        """Парсит одно вложение 44-ФЗ."""
        from core.parsers.detailed_parser import TenderDocument

        try:
            name_span = attachment.find("span", class_="section__value")
            if not name_span:
                return None

            a_tag = name_span.find("a")
            if a_tag:
                name = a_tag.get("title", "") or a_tag.get_text(strip=True)
                file_url = a_tag.get("href", "")
            else:
                name = name_span.get_text(strip=True)
                file_url = ""

            if file_url:
                file_url = urljoin(self.BASE_URL, file_url)

            file_type = ""
            img = attachment.find("img", src=re.compile(r"/type/"))
            if img:
                file_type = self._detect_file_type(img.get("src", ""))

            return TenderDocument(
                name=name,
                url=file_url,
                file_type=file_type,
                date=doc_date,
                is_active=is_active,
                file_url=file_url,
            )
        except Exception as e:
            logger.debug(f"Ошибка парсинга документа 44-ФЗ: {e}")
            return None

    def _extract_section_value_v5(self, soup: BeautifulSoup, title_text: str) -> str:
        elements = (
            soup.find_all("section", class_="blockInfo__section")
            + soup.find_all("div", class_="blockInfo__section")
            + soup.find_all("div", class_="row")
        )
        for elem in elements:
            title_elem = elem.find(["span", "div"], class_="section__title")
            if (
                title_elem
                and title_text.lower() in title_elem.get_text(strip=True).lower()
            ):
                value_elem = elem.find(
                    ["span", "div"], class_=re.compile(r"section__info|section__value")
                )
                if value_elem:
                    return value_elem.get_text(strip=True)
        return ""

    def _extract_section_url_v5(self, soup: BeautifulSoup, title_text: str) -> str:
        elements = (
            soup.find_all("section", class_="blockInfo__section")
            + soup.find_all("div", class_="blockInfo__section")
            + soup.find_all("div", class_="row")
        )
        for elem in elements:
            title_elem = elem.find(["span", "div"], class_="section__title")
            if (
                title_elem
                and title_text.lower() in title_elem.get_text(strip=True).lower()
            ):
                link = elem.find("a", href=True)
                if link:
                    href = link.get("href", "")
                    return urljoin(self.BASE_URL, href) if href else ""
        return ""


class Html223Parser(BaseHtmlParser):
    """Парсинг HTML карточки 223-ФЗ."""

    def parse_common_info(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """Парсит common-info для 223-ФЗ."""
        result = {}

        result["purchase_name"] = self._extract_text_by_title(
            soup, "Наименование закупки"
        ) or self._extract_text_by_title(soup, "Объект закупки")
        result["purchase_method"] = self._extract_text_by_title(
            soup, "Способ осуществления закупки"
        )
        result["customer_name"] = self._extract_text_by_title(
            soup, "Наименование организации"
        )
        result["customer_inn"] = self._extract_inn_from_soup(soup)

        customer_addr = self._extract_text_by_title(
            soup, "Место нахождения"
        ) or self._extract_text_by_title(soup, "Почтовый адрес")
        result["customer_address"] = customer_addr

        delivery_addr = self._extract_text_by_title(soup, "Место поставки")
        if delivery_addr:
            result["delivery_address"] = delivery_addr
        elif customer_addr:
            result["delivery_address"] = customer_addr

        price_text = self._extract_text_by_title(soup, "Начальная цена")
        if price_text:
            result["nmck"] = self.price_parser.parse(price_text) or 0.0

        result["publish_date"] = self._extract_text_by_title(
            soup, "Дата размещения извещения"
        ) or self._extract_text_by_title(soup, "Дата размещения")
        result["deadline_date"] = self._extract_text_by_title(
            soup, "Дата и время окончания срока подачи заявок"
        ) or self._extract_text_by_title(soup, "Окончание подачи заявок")
        result["current_revision_date"] = self._extract_text_by_title(
            soup, "Дата размещения текущей редакции извещения"
        )

        result["platform_name"] = self._extract_text_by_title(
            soup, "Наименование электронной площадки"
        )
        result["platform_url"] = self._extract_url_by_title(
            soup, "Адрес электронной площадки"
        )

        result["requirements"] = self._extract_text_by_title(
            soup, "Требования к участникам закупки"
        )

        result["contact_person"] = self._extract_text_by_title(soup, "Контактное лицо")
        result["contact_email"] = self._extract_email_from_soup(soup)
        result["contact_phone"] = self._extract_text_by_title(
            soup, "Контактный телефон"
        )

        self._parse_guarantee_223(soup, result)

        return result

    def _parse_guarantee_223(self, soup: BeautifulSoup, result: Dict[str, Any]):
        """Парсит обеспечение для 223-ФЗ."""
        app_guarantee = (
            self._extract_text_by_title(soup, "Обеспечение заявки")
            or self._extract_text_by_title(soup, "Обеспечение заявок")
            or self._extract_text_by_title(soup, "Требование об обеспечении заявки")
        )
        result["application_guarantee"] = app_guarantee

        contract_guarantee = (
            self._extract_text_by_title(soup, "Обеспечение исполнения контракта")
            or self._extract_text_by_title(soup, "Обеспечение контракта")
            or self._extract_text_by_title(soup, "Обеспечение исполнения")
        )
        result["contract_guarantee"] = contract_guarantee

        guarantee_method = self._extract_text_by_title(
            soup, "Способ обеспечения"
        ) or self._extract_text_by_title(soup, "Способы обеспечения")
        result["guarantee_method"] = guarantee_method

        if not result.get("application_guarantee") and not result.get(
            "contract_guarantee"
        ):
            requirements = result.get("requirements", "")
            if requirements and "не требуется" in requirements.lower():
                result["application_guarantee"] = "Не требуется"
                result["contract_guarantee"] = "Не требуется"

    def parse_documents(self, soup: BeautifulSoup) -> List[Any]:
        """Парсит документы для 223-ФЗ без дублирования."""
        from core.parsers.detailed_parser import TenderDocument

        documents = []
        seen_urls = set()

        # Находим прямо все ссылки на файлы в документе
        file_links = soup.find_all(
            "a", href=re.compile(r"filestore|download", re.IGNORECASE)
        )

        for file_link in file_links:
            href = file_link.get("href", "")
            if not href:
                continue

            file_url = urljoin(self.BASE_URL, href)
            if file_url in seen_urls:
                continue
            seen_urls.add(file_url)

            file_name = file_link.get_text(strip=True) or file_link.get("title", "")

            # Тип файла
            file_type = ""
            img = file_link.find_previous("img", src=re.compile(r"/type/"))
            if img:
                file_type = self._detect_file_type(img.get("src", ""))

            # Извлечение метаданных из родительского блока attachment
            date = ""
            is_active = True
            attachment_block = file_link.find_parent("div", class_="attachment")
            if attachment_block:
                date_text = attachment_block.find(
                    "div", class_="attachment__text", string=re.compile(r"Размещено")
                )
                if date_text:
                    date_val = date_text.find_next("div", class_="attachment__value")
                    if date_val:
                        date = date_val.get_text(strip=True)

                status_elem = attachment_block.find(
                    "div",
                    class_="attachment__value",
                    string=re.compile(r"Действующая|Недействующая"),
                )
                if status_elem:
                    is_active = "Действующая" in status_elem.get_text(strip=True)

            doc = TenderDocument(
                name=file_name,
                url=file_url,
                file_type=file_type,
                date=date,
                is_active=is_active,
                file_url=file_url,
            )
            documents.append(doc)
            logger.debug(f"[223-DOC] Найден файл: {file_name} ({file_type})")

        logger.info(f"[223-DOC] Всего найдено уникальных файлов: {len(documents)}")
        return documents
