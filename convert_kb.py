"""
convert_kb.py
Конвертирует DOCX-файлы с бизнес-логикой в JSON для Yandex File Search.
"""

import json
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
KNOWLEDGE_DIR.mkdir(exist_ok=True)

# === 1. БАЗА ЗНАНИЙ: ОПРЕДЕЛЕНИЕ ТИПОВ ТЕНДЕРОВ ===
tender_types_kb = {
    "description": "База знаний для классификации типов тендеров. Используется агентом Tender-Analyst-v1.",
    "types": {
        "sout": {
            "name": "СОУТ (Специальная оценка условий труда)",
            "definition": "Оценка условий труда на рабочих местах для определения класса вредности",
            "required_markers": [
                "рабочее место",
                "рабочих мест",
                "РМ",
                "карты СОУТ",
                "класс условий труда",
                "вредные факторы на рабочих местах",
            ],
            "optional_markers": ["специальная оценка", "СОУТ", "соут", "426-ФЗ"],
            "unit_of_measurement": "рабочее место",
            "typical_price_range": "800-2500 ₽/РМ",
            "exclusion_rules": [
                "Если есть 'обучение' + 'слушателей' → это education, не sout",
                "Если есть 'оценка рисков' без 'рабочих мест' → это opr, не sout",
            ],
        },
        "education": {
            "name": "Обучение по охране труда",
            "definition": "Обучение сотрудников требованиям охраны труда, первой помощи, СИЗ",
            "required_markers": [
                "слушателей",
                "обучающихся",
                "человек",
                "удостоверение",
                "протокол проверки знаний",
                "повышение квалификации",
            ],
            "optional_markers": [
                "охрана труда",
                "первая помощь",
                "СИЗ",
                "безопасные методы",
                "дистанционное обучение",
            ],
            "unit_of_measurement": "человек",
            "typical_price_range": "1500-5000 ₽/чел",
            "critical_rule": "ЕСЛИ КТРУ дал students_count > 0 → это ВСЕГДА education, игнорируй другие признаки",
            "exclusion_rules": [
                "Если есть 'рабочих мест' без 'слушателей' → это sout, не education"
            ],
        },
        "opr": {
            "name": "ОПР (Оценка профессиональных рисков)",
            "definition": "Идентификация опасностей и оценка рисков на рабочих местах",
            "required_markers": [
                "оценка профессиональных рисков",
                "ОПР",
                "идентификация опасностей",
                "карты рисков",
                "должностей",
            ],
            "optional_markers": [
                "профессиональные риски",
                "оценка рисков",
                "СИЗ по рискам",
            ],
            "unit_of_measurement": "должность/рабочее место",
            "typical_price_range": "500-1500 ₽/должность",
            "exclusion_rules": [
                "Если КТРУ дал students_count > 0 → это education, не opr",
                "Если есть 'рабочих мест' + 'класс условий' → это sout, не opr",
            ],
        },
        "plk": {
            "name": "ПЛК (Производственный лабораторный контроль)",
            "definition": "Лабораторные замеры вредных факторов производственной среды",
            "required_markers": [
                "точек замеров",
                "проб воздуха",
                "измерения шума",
                "лабораторные исследования",
                "атмосферный воздух",
                "физические факторы",
            ],
            "optional_markers": [
                "производственный контроль",
                "ПЛК",
                "санитарно-гигиенические",
                "инструментальный контроль",
            ],
            "unit_of_measurement": "точка замера",
            "typical_price_range": "300-1200 ₽/точка",
            "exclusion_rules": [
                "Если есть 'рабочих мест' + 'СОУТ' → это combined (sout+plk), не чистый plk",
                "Если только 'исследование воды' без аккредитации → заблокировать",
            ],
        },
        "combined": {
            "name": "Комплексный тендер",
            "definition": "Тендер содержит несколько типов работ (например, СОУТ + ПЛК)",
            "detection_rule": "Если в названии/тексте есть маркеры ДВУХ разных типов → combined",
            "subtypes_priority": ["sout", "plk", "opr", "education"],
            "calculation_rule": "Считать каждую часть отдельно, суммировать себестоимость",
        },
    },
}

# === 2. БАЗА ЗНАНИЙ: ФОРМУЛЫ РАСЧЕТА (Заглушка, нужно заполнить из DOCX) ===
calculation_rules_kb = {
    "description": "Формулы, коэффициенты и guard'ы для расчета себестоимости и цены участия.",
    "global_limits": {
        "min_margin_percent": 10.0,
        "max_cost_to_nmck_ratio": 0.9,
        "urgency_multiplier_threshold_days": 14,
        "urgency_overhead_increase": 0.5,
    },
    "etp_commissions": {
        "ртс-тендер": 1.0,
        "сбербанк-аст": 0.5,
        "фабрикант": 1.5,
        "еэтп": 1.0,
        "агора": 0.8,
    },
    "type_specific_rules": {
        "sout": {
            "base_price_per_rm": 213,
            "materials_per_rm": 73,
            "delivery_cost": 2000,
            "measurer_daily": 15000,
            "accommodation_per_night": 5000,
            "flight_avg": 8000,
            "specialist_hourly": 100,
            "specialist_hours_base": 3,
        },
        "education": {
            "portal_fee_per_student": 57,
            "certificate_cost": 60,
            "diploma_cost": 265,
            "protocol_cost": 3.65,
            "qual_cert_cost": 130,
            "labor_specialist": 300,
            "labor_methodist": 681,
            "labor_ro": 1800,
            "distance_transport_km_rate": 0,
            "full_time_teacher_daily": 0,
            "full_time_venue_daily": 0,
            "full_time_manikin_daily": 0,
        },
        "opr": {"siz_norms_cost_per_position": 200, "margin_percent": 30.0},
        "plk": {"base_cost_per_point": 41.9, "subcontractor_markup": 0},
    },
}

# Сохраняем файлы
with open(KNOWLEDGE_DIR / "tender_types.json", "w", encoding="utf-8") as f:
    json.dump(tender_types_kb, f, ensure_ascii=False, indent=2)
print(f"✅ Создан: {KNOWLEDGE_DIR / 'tender_types.json'}")

with open(KNOWLEDGE_DIR / "calculation_rules.json", "w", encoding="utf-8") as f:
    json.dump(calculation_rules_kb, f, ensure_ascii=False, indent=2)
print(f"✅ Создан: {KNOWLEDGE_DIR / 'calculation_rules.json'}")

print("\n📋 Следующие шаги:")
print("1. Открой Yandex AI Studio → Поисковые индексы → tender-knowledge-base")
print("2. Нажми 'Добавить файл' и загрузи оба созданных JSON-файла")
print("3. Дождись статуса 'Completed'")
print("4. Обнови системную инструкцию агента Tender-Analyst-v1 (см. ниже)")
