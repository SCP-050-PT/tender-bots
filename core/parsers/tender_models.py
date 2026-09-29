"""
core/parsers/tender_models.py
Dataclasses для тендеров.
Вынесено из detailed_parser.py (v6.8.6-r1).

ИСПРАВЛЕНО (v6.8.6-r4):
- @property reg_number и @reg_number.setter перенесены ВНУТРЬ класса TenderDetail
  (были вне класса — синтаксическая ошибка Python)
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List


@dataclass
class TenderDocument:
    """Документ тендера."""

    name: str
    url: str = ""
    file_type: Optional[str] = None
    date: str = ""
    is_active: bool = True
    file_url: str = ""


@dataclass
class TenderDetail:
    """Детальная информация о тендере."""

    tender_id: str = ""
    law: str = ""
    title: str = ""
    customer: str = ""
    region: str = ""
    etp: str = ""
    nmck: float = 0.0
    publish_date: str = ""
    deadline_date: str = ""
    requirements: str = ""
    warranty_required: bool = False
    warranty_percent: float = 0.0
    documents: List[Dict[str, Any]] = field(default_factory=list)
    raw_html: str = ""
    notice_guid: Optional[str] = None
    tender_type_hint: Optional[str] = None
    lot_info: Optional[Dict] = None
    customer_address: Optional[str] = None
    type_detection_source: Optional[str] = None
    purchase_name: Optional[str] = None
    customer_name: Optional[str] = None
    customer_region: Optional[str] = None
    delivery_address: Optional[str] = None
    purchase_method: Optional[str] = None
    platform_name: Optional[str] = None
    platform_url: Optional[str] = None
    application_guarantee: Optional[str] = None
    contract_guarantee: Optional[str] = None
    guarantee_method: Optional[str] = None
    documents_text: Optional[str] = None
    contact_person: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    customer_inn: Optional[str] = None
    current_revision_date: Optional[str] = None
    # Количества
    addresses_count: int = 1
    cities_count: int = 1
    regions_count: int = 1
    rm_total: Optional[int] = None
    students_count: Optional[int] = None
    points_count: Optional[int] = None
    measurement_points: Optional[int] = None
    opr_positions: Optional[int] = None
    opr_persons: Optional[int] = None

    # Источник qty (ktru / excel / …) — для PROTECTED в analyzer
    rm_total_source: Optional[str] = None
    students_count_source: Optional[str] = None
    points_source: Optional[str] = None
    opr_positions_source: Optional[str] = None
    ktru_confidence: Optional[float] = None
    price_per_unit: Optional[float] = None  # цена из извещения, НЕ себестоимость
    # Флаги
    has_full_time: bool = False
    teacher_days: int = 0
    accommodation_nights: int = 0
    transport_km: int = 0
    venue_rent_days: int = 0
    manikin_days: int = 0
    trip_days: int = 0
    is_seasonal: bool = False
    needs_subcontractor: bool = False
    factors_count: int = 0

    # FIX v6.8.6-r4: @property и setter ВНУТРИ класса (не снаружи!)
    @property
    def reg_number(self) -> str:
        """Обратная совместимость: reg_number -> tender_id."""
        return self.tender_id

    @reg_number.setter
    def reg_number(self, value: str) -> None:
        self.tender_id = value
