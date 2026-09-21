"""
core/calculation/cost_loader.py
Ленивая загрузка базы цен из costs_db.json.
Вынесено из calculator.py (v6.5).

v8.0.0-Optimized:
  - Добавлена возможность инвалидации кэша (reload_costs).
  - Улучшена обработка ошибок чтения и парсинга JSON.
  - Точная типизация Dict[str, Any].
"""

import json
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger

COSTS: Optional[Dict[str, Any]] = None


def reload_costs() -> Dict[str, Any]:
    """Принудительно сбрасывает кэш и перезагружает базу цен из файла."""
    global COSTS
    COSTS = None
    return load_costs()


def load_costs() -> Dict[str, Any]:
    """Ленивая загрузка базы цен."""
    global COSTS
    if COSTS is not None:
        return COSTS

    costs_db_path = (
        Path(__file__).resolve().parent.parent.parent / "knowledge" / "costs_db.json"
    )

    if costs_db_path.exists():
        try:
            with open(costs_db_path, "r", encoding="utf-8") as f:
                COSTS = json.load(f)
            logger.info(f"✅ База цен загружена: {costs_db_path}")
        except json.JSONDecodeError as e:
            logger.error(
                f"❌ Ошибка синтаксиса JSON в costs_db.json (строка {e.lineno}, колонка {e.colno}): {e}"
            )
            COSTS = _get_default_costs()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки costs_db.json: {e}")
            COSTS = _get_default_costs()
    else:
        logger.warning(f"⚠️ Файл costs_db.json не найден: {costs_db_path}")
        logger.warning("⚠️ Используются значения по умолчанию")
        COSTS = _get_default_costs()

    return COSTS


def _get_default_costs() -> Dict[str, Any]:
    """Встроенные значения по умолчанию — ЦЕНЫ ДЛЯ КЛИЕНТА.

    ВАЖНО: Этот fallback используется ТОЛЬКО если costs_db.json не найден
    или повреждён. Все значения должны соответствовать актуальным ценам.
    """
    return {
        "education": {
            "documents": {
                "certificate": {"cost": 60},
                "diploma": {"cost": 265},
                "certificate_worker": {"cost": 80},
                "certificate_qualification": {"cost": 130},
                "protocol": {"cost": 3.65},
            },
            "materials": {
                "paper_a4": {"cost": 1.15},
                "ink_per_page": {"cost": 2.5},
                "lamination": {"cost": 10},
            },
            "labor": {
                "methodist_hour": {"cost": 227},
                "ro_hour": {"cost": 600},
                "portal_access": {"cost": 57},
            },
            "delivery": {
                "post_russia": {"cost": 300},
            },
            "overhead": {
                "base": {"cost": 100},
            },
            "minimum_price": {
                "distance": 10000,
                "full_time": 10000,
            },
            "rates": {
                "teacher_daily": {"cost": 8000, "unit": "день"},
                "manikin_daily": {"cost": 15000, "unit": "день"},
                "venue_daily": {"cost": 3000, "unit": "день"},
                "transport_fixed": {
                    "cost": 10000,
                    "unit": "выезд",
                    "range": [5000, 15000],
                },
            },
            "forms": {
                "full_time": {
                    "fuel_cost_per_km": 5,
                    "accommodation_per_night": 2500,
                    "daily_allowance": 1000,
                }
            },
        },
        "sout": {
            "category_rates": {
                "1": {"full_cost": 900, "card_cost": 1500, "analogy_cost": 100},
                "2": {"full_cost": 1800, "card_cost": 2000, "analogy_cost": 100},
            },
            "analogy_protocol_set": {"cost": 1000},
            "materials": {
                "paper_a4": {"cost": 1.15, "default_quantity": 20},
                "ink_per_page": {"cost": 2.5, "default_quantity": 20},
            },
            "delivery": {
                "post_russia": {"cost": 500},
                "post_russia_high": {"cost": 1000},
            },
            "iii_subcontractor": {
                "ranges": [
                    {"max_rm": 10, "cost": 5000},
                    {"max_rm": 15, "cost": 6000},
                    {"max_rm": 20, "cost": 7000},
                ]
            },
            "travel": {
                "fixed_trip_cost": 12000,
                "trip_days_default": 3,
                "daily_allowance": 500,
                "accommodation_per_night": 2500,
                "measurer_daily_rate": 5000,
                "seasonal_multiplier": 2,
            },
        },
        "plk": {
            "base_cost_per_point": {"cost": 41.9},
            "labor": {
                "measurer_per_point": {"cost": 20},
            },
            "materials": {
                "paper_a4": {"cost": 1.15, "default_quantity": 6},
                "ink_per_page": {"cost": 2.5, "default_quantity": 6},
            },
            "delivery": {
                "post_russia": {"cost": 500},
            },
            "subcontractor": {
                "default_cost": 10000,
            },
            "travel": {
                "accommodation_default": 4000,
                "daily_allowance": 4000,
            },
        },
        "opr": {
            "margin_percent": 10,
            "rates": {
                "per_position": {"cost": 500, "unit": "должность"},
                "per_person": {"cost": 150, "unit": "человек"},
            },
            "materials": {
                "paper_a4": {"cost": 1.15, "default_quantity": 16},
                "ink_per_page": {"cost": 2.5, "default_quantity": 16},
            },
            "labor": {
                "sot_per_rm": {"cost": 40},
                "processing_per_rm": {"cost": 10},
                "program_per_day": {"cost": 10},
            },
            "delivery": {
                "post_russia": {"cost": 1000},
            },
        },
        "guarantees": {
            "application": {
                "bank_guarantee_cost": {
                    "ranges": [
                        {"max_contract": 50000, "real_cost": 1000},
                        {"max_contract": 100000, "real_cost": 1200},
                        {"max_contract": 500000, "real_cost": 2000},
                        {"max_contract": 1000000, "real_cost": 4000},
                        {"max_contract": 5000000, "real_cost": 10000},
                    ]
                }
            }
        },
        "travel": {
            "fuel": {
                "consumption_l_per_100km": 11,
                "price_per_liter": 65,
            },
            "accommodation": {
                "standard_per_night": 2500,
            },
            "daily_allowance": {
                "standard": 500,
            },
        },
        "global_limits": {
            "min_contract_sum": 10000,
            "min_margin_percent": 10.0,
            "max_cost_to_nmck_ratio": 0.85,
            "max_tender_preparation_hours": 3,
            "tender_specialist_rate_per_hour": 100,
        },
    }
