"""
core/parsers/detailed_parser.py
Фасад для детального парсинга карточки тендера.

v6.9-r1:
  - Изоляция notice_guid в рамки вызова метода (без состояния класса)
  - Оптимизировано извлечение НМЦК (убран дублирующий вызов)
  - Повышена устойчивость поиска дат/требований в верстке ЕИС (замена string= на get_text)
  - Добавлена нормализация текста при поиске обеспечения/гарантий
"""

import re
from typing import Optional, Dict, Any, List
from bs4 import BeautifulSoup
from loguru import logger

from core.parsers.tender_models import TenderDetail, TenderDocument
from core.parsers.type_detector import TypeDetector
from knowledge.regions import RegionResolver
from core.parsers.ktru_parser import KtruParser
from core.parsers.html_parsers import Html44Parser, Html223Parser


class DetailedParser:
    def __init__(self, session_manager=None):
        self.session_manager = session_manager
        self.html44 = Html44Parser()
        self.html223 = Html223Parser()

    def fetch_and_parse(
        self,
        reg_number: str,
        law_type: str,
        notice_guid: str = "",
        nmck: float = 0,
        fallback_title: str = "",
        fallback_region: str = "",
        fallback_customer: str = "",
        fallback_platform: str = "",
    ) -> Optional[TenderDetail]:
        law = law_type if "FZ" in law_type else f"{law_type}-FZ"
        law_clean = law.replace("-FZ", "")

        url = self._build_url(reg_number, law_clean)
        logger.info(f"[DetailedParser] Загрузка common-info: {url}")

        html, is_blocked, extracted_guid = self._fetch_html(url, reg_number)
        current_guid = notice_guid or extracted_guid

        if is_blocked or not html:
            logger.info("[DetailedParser] Fallback на поисковые данные")
            return self._create_fallback(
                reg_number,
                law,
                fallback_title,
                fallback_region,
                fallback_customer,
                fallback_platform,
                nmck,
            )

        # Загрузка lot-list для 223-ФЗ
        lot_list_html = ""
        if law_clean == "223":
            lot_url = self._build_lot_list_url(reg_number, current_guid)
            logger.info(f"[DetailedParser] Загрузка lot-list: {lot_url}")
            lot_list_html, _, new_guid = self._fetch_html(lot_url, reg_number)
            current_guid = current_guid or new_guid

        # Загрузка вкладки "Документы"
        documents_html = ""
        docs_url = self._build_documents_url(reg_number, law_clean, current_guid)
        logger.info(f"[DetailedParser] Загрузка документов: {docs_url}")
        documents_html, docs_blocked, _ = self._fetch_html(docs_url, reg_number)

        return self.parse(
            html=html,
            tender_id=reg_number,
            law=law,
            lot_list_html=lot_list_html,
            documents_html=documents_html,
            notice_guid=current_guid,
        )

    def _build_url(self, reg_number: str, law_clean: str) -> str:
        base = "https://zakupki.gov.ru/epz/order/notice"
        if law_clean == "223":
            return f"{base}/notice223/common-info.html?regNumber={reg_number}"
        elif law_clean == "615":
            return f"{base}/ea615/view/common-info.html?regNumber={reg_number}"
        return f"{base}/ea44/view/common-info.html?regNumber={reg_number}"

    def _build_lot_list_url(self, reg_number: str, notice_guid: str = "") -> str:
        base = "https://zakupki.gov.ru/epz/order/notice/notice223"
        if notice_guid:
            return f"{base}/lot-list.html?purchaseNoticeNumber={reg_number}&noticeGuid={notice_guid}"
        return f"{base}/lot-list.html?purchaseNoticeNumber={reg_number}"

    def _build_documents_url(
        self, reg_number: str, law_clean: str, notice_guid: str = ""
    ) -> str:
        base = "https://zakupki.gov.ru/epz/order/notice"
        if law_clean == "223":
            if notice_guid:
                return f"{base}/notice223/documents.html?purchaseNoticeNumber={reg_number}&noticeGuid={notice_guid}"
            return f"{base}/notice223/documents.html?purchaseNoticeNumber={reg_number}"
        elif law_clean == "615":
            return f"{base}/ea615/view/documents.html?regNumber={reg_number}"
        return f"{base}/ea44/view/documents.html?regNumber={reg_number}"

    def _fetch_html(self, url: str, reg_number: str) -> tuple:
        """Возвращает (html, is_blocked, extracted_notice_guid)"""
        if not self.session_manager:
            return "", True, ""

        headers = {
            "Referer": f"https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString={reg_number}",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
        }

        response = self.session_manager.make_request(
            url, timeout=45, max_retries=3, headers=headers
        )

        if response is None:
            logger.warning(f"[DetailedParser] Не удалось загрузить URL: {url}")
            return "", True, ""

        extracted_guid = ""
        try:
            final_url = str(response.url)
            m = re.search(r"noticeGuid=([0-9a-fA-F\-]{36})", final_url)
            if m:
                extracted_guid = m.group(1)

            if not extracted_guid:
                patterns = [
                    r'href="[^"]*noticeGuid=([0-9a-fA-F\-]{36})[^"]*"',
                    r'data-notice-guid=["\']([0-9a-fA-F\-]{36})["\']',
                    r'noticeGuid["\']?\s*[:=]\s*["\']?([0-9a-fA-F\-]{36})',
                ]
                for pattern in patterns:
                    m2 = re.search(pattern, response.text)
                    if m2:
                        extracted_guid = m2.group(1)
                        break

            html = response.text
            is_blocked = self._check_blocked(html)
            return html, is_blocked, extracted_guid

        except Exception as e:
            logger.warning(f"[DetailedParser] Ошибка обработки ответа: {e}")
            return "", True, ""

    def _check_blocked(self, html: str) -> bool:
        if not html or len(html) < 5000:
            return True
        soup = BeautifulSoup(html, "html.parser")
        has_login = bool(soup.find("input", {"type": "password"}))
        title_tag = soup.find("title")
        is_auth = title_tag and any(
            w in title_tag.get_text() for w in ["Авторизация", "Вход", "Личный кабинет"]
        )
        has_card = bool(soup.find("div", class_="cardMainInfo"))
        return (has_login or (is_auth and not has_card)) or (
            len(html) < 1000 and not has_card
        )

    def _create_fallback(
        self,
        reg_number: str,
        law: str,
        title: str,
        region: str,
        customer: str,
        platform: str,
        nmck: float,
    ) -> TenderDetail:
        tender_type, source = TypeDetector.detect_from_title(title)
        return TenderDetail(
            tender_id=reg_number,
            law=law,
            title=title,
            customer=customer or "",
            region=region or "",
            etp="",
            nmck=nmck or 0.0,
            platform_name=platform or "",
            tender_type_hint=tender_type,
            type_detection_source=source,
            purchase_name=title,
            customer_name=customer or "",
            customer_region=region or "",
        )

    def parse(
        self,
        html: str,
        tender_id: str,
        law: str,
        lot_list_html: str = "",
        documents_html: str = "",
        notice_guid: str = "",
    ) -> Optional[TenderDetail]:
        soup = BeautifulSoup(html, "html.parser")
        lot_list_soup = (
            BeautifulSoup(lot_list_html, "html.parser") if lot_list_html else None
        )

        lot_info = self._parse_lot_list_223(soup, law)
        title = self._extract_title(soup)
        obj_block = self._extract_object_block(soup)

        tender_type, type_source = TypeDetector.cascade_detect(
            title=title,
            lot_object=lot_info.get("object_name", "") if lot_info else "",
            okpd2_list=lot_info.get("okpd2", []) if lot_info else None,
            common_info_object=obj_block,
        )

        region = self._extract_region(soup, law, lot_info)
        nmck_val = self._extract_nmck(soup)

        if law == "223-FZ" and lot_list_soup:
            ktru = KtruParser.parse_223_lot_list(lot_list_soup, nmck=nmck_val)
        else:
            ktru = KtruParser.parse(soup, nmck=nmck_val)

        documents = self._parse_documents(documents_html, law)
        warranty = self._extract_warranty(soup)

        app_guarantee = (
            f"{warranty['app_percent']:.1f}%"
            if warranty["app_required"] and warranty["app_percent"] > 0
            else (warranty["app_text"] if warranty["app_required"] else "Не требуется")
        )

        contract_guarantee = (
            f"{warranty['contract_percent']:.1f}%"
            if warranty["contract_required"] and warranty["contract_percent"] > 0
            else (
                warranty["contract_text"]
                if warranty["contract_required"]
                else "Не требуется"
            )
        )

        guarantee_method = warranty["contract_method"] or "Не требуется"

        return TenderDetail(
            tender_id=tender_id,
            law=law,
            title=title,
            customer=self._extract_customer(soup),
            region=region,
            etp=self._extract_etp(soup),
            nmck=nmck_val,
            publish_date=self._extract_publish_date(soup),
            deadline_date=self._extract_deadline(soup),
            requirements=self._extract_requirements(soup),
            warranty_required=warranty["contract_required"],
            warranty_percent=warranty["contract_percent"],
            application_guarantee=app_guarantee,
            contract_guarantee=contract_guarantee,
            guarantee_method=guarantee_method,
            documents=documents,
            raw_html=html,
            notice_guid=notice_guid or self._extract_notice_guid(soup, law),
            tender_type_hint=tender_type,
            type_detection_source=type_source,
            lot_info=lot_info,
            customer_address=self._extract_customer_address(soup, law),
            purchase_name=title,
            customer_name=self._extract_customer(soup),
            customer_region=region,
            platform_name=self._extract_etp(soup),
            rm_total=ktru.get("rm_total", 0),
            students_count=ktru.get("students_count", 0),
            points_count=ktru.get("points_count", 0),
            opr_positions=ktru.get("opr_positions", 0),
        )

    def _parse_documents(self, documents_html: str, law: str) -> List[Dict]:
        if not documents_html:
            return []

        soup = BeautifulSoup(documents_html, "html.parser")
        docs = (
            self.html223.parse_documents(soup)
            if law == "223-FZ"
            else self.html44.parse_documents(soup)
        )

        result = []
        for doc in docs:
            if doc:
                result.append(
                    {
                        "name": doc.name,
                        "link": doc.file_url or doc.url,
                        "file_type": doc.file_type,
                        "date": doc.date,
                        "is_active": doc.is_active,
                    }
                )
        return result

    def _parse_lot_list_223(self, soup, law):
        if law != "223-FZ":
            return None
        return getattr(self.html223, "_parse_lot_list", lambda s: None)(soup)

    def _extract_title(self, soup):
        for section in soup.find_all("div", class_="cardMainInfo__section"):
            title_span = section.find("span", class_="cardMainInfo__title")
            if title_span and "объект закупки" in title_span.get_text().lower():
                content_span = section.find("span", class_="cardMainInfo__content")
                if content_span:
                    text = content_span.get_text(strip=True)
                    if text and len(text) > 5:
                        return text

        for el in soup.select("span.cardMainInfo__content"):
            text = el.get_text(strip=True)
            if re.match(r"^[\d\s.,₽]+$", text):
                continue
            if text and len(text) > 10:
                return text

        el = soup.select_one("span.cardMainInfo__purchaseLink")
        return el.get_text(strip=True) if el else ""

    def _extract_object_block(self, soup):
        obj = soup.find("div", class_="registry-entry__body-block")
        if obj:
            val = obj.find("div", class_="registry-entry__body-value")
            return val.get_text(strip=True) if val else ""
        return ""

    def _extract_region(self, soup, law, lot_info):
        for section in soup.find_all("section", class_="blockInfo__section"):
            title = section.find("span", class_="section__title")
            if title and title.get_text(strip=True) == "Регион":
                val = section.find("span", class_="section__info")
                if val:
                    return val.get_text(strip=True)
        if law == "223-FZ":
            addr = self._extract_customer_address(soup, law)
            if addr:
                region = RegionResolver.extract_region(addr)
                if region:
                    return region
            if lot_info and lot_info.get("object_name"):
                region = RegionResolver.extract_region(lot_info["object_name"])
                if region:
                    return region
        return ""

    def _extract_customer(self, soup):
        for section in soup.find_all("section", class_="blockInfo__section"):
            title = section.find("span", class_="section__title")
            if title and "организация" in title.get_text().lower():
                val = section.find("span", class_="section__info")
                if val:
                    return val.get_text(strip=True)
        return ""

    def _extract_etp(self, soup):
        for section in soup.find_all("section", class_="blockInfo__section"):
            title = section.find("span", class_="section__title")
            if title:
                t_text = title.get_text().lower()
                if "площадк" in t_text and "электронн" in t_text:
                    val = section.find("span", class_="section__info")
                    if val:
                        return val.get_text(strip=True)
        return ""

    def _extract_nmck(self, soup):
        el = soup.find("span", class_="cardMainInfo__content cost")
        if el:
            text = re.sub(r"[^\d.,]", "", el.get_text(strip=True)).replace(",", ".")
            try:
                return float(text)
            except ValueError:
                pass
        return 0.0

    def _extract_publish_date(self, soup):
        for div in soup.find_all("div", class_="col-6"):
            text = div.get_text()
            if "Размещено" in text:
                val = div.find("span", class_="section__info")
                if val:
                    return val.get_text(strip=True)
        return ""

    def _extract_deadline(self, soup):
        for div in soup.find_all("div", class_="col-6"):
            text = div.get_text()
            if "Окончание подачи заявок" in text:
                val = div.find("span", class_="section__info")
                if val:
                    return val.get_text(strip=True)
        return ""

    def _extract_requirements(self, soup):
        for div in soup.find_all("div", class_="col-12"):
            text = div.get_text()
            if "Требования" in text:
                val = div.find("div")
                if val:
                    return val.get_text(strip=True)
        return ""

    def _extract_warranty(self, soup):
        result = {
            "app_required": False,
            "app_percent": 0.0,
            "app_text": "",
            "contract_required": False,
            "contract_percent": 0.0,
            "contract_text": "",
            "contract_method": "",
        }

        for section in soup.find_all("section", class_="blockInfo__section"):
            title = section.find("span", class_="section__title")
            if not title:
                continue
            title_text = title.get_text(strip=True).lower()

            if re.search(r"обеспечен[ие|ия]\s+заявки", title_text):
                val = section.find("span", class_="section__info")
                if val:
                    text = val.get_text(strip=True)
                    result["app_text"] = text
                    if any(w in text.lower() for w in ["требуется", "да"]):
                        result["app_required"] = True
                    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
                    if m:
                        result["app_percent"] = float(m.group(1).replace(",", "."))

            if "обеспечение исполнения контракта" in title_text and not any(
                w in title_text for w in ["размер", "порядок", "платеж"]
            ):
                val = section.find("span", class_="section__info")
                if val:
                    text = val.get_text(strip=True)
                    result["contract_text"] = text
                    if any(w in text.lower() for w in ["требуется", "да"]):
                        result["contract_required"] = True

            if "размер обеспечения исполнения" in title_text:
                val = section.find("span", class_="section__info")
                if val:
                    text = val.get_text(strip=True)
                    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
                    if m:
                        result["contract_percent"] = float(m.group(1).replace(",", "."))
                        result["contract_required"] = True

            if "порядок предоставления обеспечения" in title_text:
                val = section.find("span", class_="section__info")
                if val:
                    result["contract_method"] = val.get_text(strip=True)[:200]

        return result

    def _extract_notice_guid(self, soup, law):
        if law != "223-FZ":
            return None
        html_text = str(soup)
        m = re.search(
            r'(?:noticeGuid|purchaseNoticeGuid)[=:]\s*["\']([0-9a-fA-F\-]{36})["\']',
            html_text,
        )
        if m:
            return m.group(1)
        for link in soup.find_all("a", href=True):
            m = re.search(r"noticeGuid=([0-9a-fA-F\-]{36})", link.get("href", ""))
            if m:
                return m.group(1)
        return None

    def _extract_customer_address(self, soup, law):
        if law != "223-FZ":
            return None
        for section in soup.find_all("section", class_="common-text"):
            caption = section.find("div", class_="common-text__caption")
            if caption and "заказчик" in caption.get_text().lower():
                for row in section.find_all("div", class_="row"):
                    text = row.get_text(separator=" ", strip=True)
                    if any(w in text.lower() for w in ["местонахождение", "адрес"]):
                        m = re.search(
                            r"(?:местонахождение|адрес)[\s:]*(.+?)(?:$)",
                            text,
                            re.IGNORECASE,
                        )
                        if m:
                            return m.group(1).strip()
        return None
