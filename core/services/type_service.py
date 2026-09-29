"""
Единый сервис определения типа тендера.
Заменяет: type_resolver.py (KEYWORDS), tender_type.py (TYPE_KEYWORDS, _normalize_alias),
          main.py (_detect_type_from_title).

v7.2.1:
  - Education: стем «обучен» (обучение/обучению/обучения), hard-rule до title-heuristic
  - Короткий «опр» только как целое слово (границы)
  - Title-порядок: education → sout → plk → opr → combined
  - purchase_name + title + object_info для education/title
  - Негативы: ПСД, диагностика трубопроводов, изоляция и т.п.
"""

import re
from typing import Optional, Dict, Any, Tuple
from loguru import logger


class TypeService:
    """Определяет тип тендера по каскадной логике. Единый источник ключевых слов."""

    VERSION = "v7.2.1"

    # === НЕГАТИВНЫЕ КЛЮЧЕВЫЕ СЛОВА (сразу "other") ===
    NEGATIVE_KEYWORDS = [
        # Строительная / техническая экспертиза объёмов и качества
        "экспертиза объёмов",
        "экспертизы объёмов",
        "экспертиза объемов",
        "экспертизы объемов",
        "экспертиза качества выполненных работ",
        "экспертизы качества выполненных работ",
        "объёмов и качества выполненных работ",
        "объемов и качества выполненных работ",
        "строительно-техническая экспертиза",
        "строительная экспертиза",
        "экспертиза сметной документации",
        "экспертиза исполнительной документации",
        "экспертиза проектной документации",
        # Другие чужие направления
        "экспертиза промышленной безопасности",
        "экспертиза промышленной",
        "техническое диагностирование",
        "диагностирование технических устройств",
        "лицензия мчс",
        "лицензии мчс",
        # Электроустановки / изоляция — не ОПР
        "сопротивления изоляции",
        "сопротивление изоляции",
        "замер сопротивления",
        "замеры сопротивления",
        # ПСД / изыскания
        "проектно-изыскательск",
        "предпроектн",
        "выполнение псд",
        "изыскательских работ",
        # Экология / выбросы
        "источников выбросов",
        "предельно-допустимых выбросов",
        "производственного экологического контроля",
        "физико-химических показателей источников",
        "контроль выбросов",
        # Нормирование труда
        "нормирован труда",
        "нормирования труда",
        "нормирование труда",
        "нормы труда",
        "хронометраж",
        "фотография рабочего дня",
        "фотографии рабочего дня",
        "положение о нормировании",
        "положения о нормировании",
        "оптимальных показателей для выполнения",
        "определению оптимальных показателей",
        "должностей, подлежащих нормированию",
        "подлежащих нормированию",
        # Диагностика трубопроводов (не ОПР)
        "диагностическое обследование",
        "диагностическому обследованию",
        "диагностических обследований",
        "обследование надземных",
        "обследование технологических",
        "обследование трубопровод",
        "надземных трубопровод",
        "технологических трубопровод",
        "внутритрубн",
        "маркетинговые исследования",
    ]

    # === ЕДИНСТВЕННЫЙ источник ключевых слов для текста документов ===
    KEYWORDS = {
        "testing": [
            "испытание",
            "испытания",
            "пожарных лестниц",
            "наружных лестниц",
            "техническое диагностирование",
        ],
        "education": [
            "обучен",  # стем: обучение / обучению / обучения
            "слушател",
            "программа обучения",
            "удостоверение",
            "повышение квалификации",
            "переподготовка",
            "инструктаж",
            "стажировка",
            "профессиональное обучение",
            "курсы",
            "образовательные услуги",
            "обучение охране труда",
            "обучению охране труда",
            "обучения охране труда",
            "пожарная безопасность",
            "пожарной безопасности",
            "промышленная безопасность",
            "промышленной безопасности",
            "обучение рабочих профессий",
            "рабочих специальностей",
            "проверка знаний",
            "тренинги",
            "образовательные",
        ],
        "sout": [
            "специальная оценка",
            "соут",
            "вредные факторы",
            "класс условий труда",
            "оценка условий труда",
            "оценка рабочих мест",
            "карты соут",
            "специальной оценки условий труда",
            "специальной оценке условий труда",
            "специальной оценкой условий труда",
        ],
        "plk": [
            "производственный контроль",
            "плк",
            "лабораторные исследования",
            "лабораторный контроль",
            "замеры шума",
            "замеры вибрации",
            "санитарно-гигиенические исследования",
            "производственного лабораторного контроля",
            "лабораторные испытания",
            "лабораторно-инструментальн",
            "инструментального контроля",
            "уровней воздействия",
            "вредных производственных факторов",
            "физических и химических факторов",
            "плановый периодический контроль",
            "внеплановый оперативный контроль",
        ],
        "opr": [
            "профессиональный риск",
            "оценка рисков",
            "идентификация опасностей",
            "мероприятия по снижению рисков",
            "оценка профессиональных рисков",
            "оценки профессиональных рисков",
            "оценке профессиональных рисков",
            "оценкой профессиональных рисков",
            "профессиональных рисков",
            "профессиональные риски",
            # короткий «опр» — только через _has_opr_token, не здесь
        ],
    }

    TITLE_KEYWORDS = {
        "education": [
            "обучен",  # стем
            "обучению",
            "обучения",
            "обучение",
            "обучение по охране труда",
            "обучение охране труда",
            "повышение квалификации",
            "переподготовка",
            "профессиональное обучение",
            "программа обучения",
            "курсы повышения квалификации",
            "пожарная безопасность",
            "промышленная безопасность",
            "обучение рабочих",
            "рабочих специальностей",
            "обучение и проверка знаний",
            "проверка знаний требований охраны труда",
            "проверка знаний по охране труда",
            "проверке знаний",
            "образовательных услуг",
            "образовательные услуги",
            "слушател",
        ],
        "sout": [
            "специальная оценка условий труда",
            "специальной оценки условий труда",
            "специальной оценке условий труда",
            "специальной оценкой условий труда",
            "соут",
            "оценка условий труда",
            "оценки условий труда",
            "оценке условий труда",
            "специальная оценка рабочих мест",
        ],
        "plk": [
            "производственный лабораторный контроль",
            "производственного лабораторного контроля",
            "лабораторный контроль",
            "плк",
            "лабораторные исследования",
            "лабораторные испытания",
            "производственного контроля",
            "производственный контроль",
            "вредных производственных факторов",
            "замеры воздуха",
            "воздуха рабочей зоны",
            "инструментальные замеры",
            "инструментальный контроль",
        ],
        "opr": [
            "оценка профессиональных рисков",
            "оценки профессиональных рисков",
            "оценке профессиональных рисков",
            "оценкой профессиональных рисков",
            "профессиональный риск",
            "профессиональных рисков",
            "профессиональные риски",
            # «опр» — только через _has_opr_token
        ],
        "combined": [
            "комплекс работ",
            "мероприятия по улучшению условий труда",
            "соут и производственный контроль",
            "соут и плк",
            "оценка условий труда и производственный контроль",
        ],
    }

    # Порядок проверки title (education первым!)
    TITLE_ORDER = ("education", "sout", "plk", "opr", "combined")

    TYPE_ALIASES = {
        "sout": "sout",
        "соут": "sout",
        "специальная оценка": "sout",
        "специальной оценки": "sout",
        "education": "education",
        "обучение": "education",
        "обучения": "education",
        "opr": "opr",
        "опр": "opr",
        "оценка профессиональных рисков": "opr",
        "оценки профессиональных рисков": "opr",
        "plk": "plk",
        "плк": "plk",
        "производственный контроль": "plk",
        "производственного контроля": "plk",
        "combined": "combined",
        "комбинированный": "combined",
        "комбинированного": "combined",
        "other": "other",
        "unknown": "unknown",
        "testing": "testing",
    }

    @staticmethod
    def _has_opr_token(text: str) -> bool:
        """«опр» только как отдельный токен, не подстрока чужого слова."""
        if not text:
            return False
        return bool(
            re.search(r"(?<![а-яёa-z0-9])опр(?![а-яёa-z0-9])", text, re.IGNORECASE)
        )

    @staticmethod
    def _title_blob(tender_info: Dict[str, Any]) -> str:
        parts = [
            tender_info.get("purchase_name") or "",
            tender_info.get("title") or "",
            tender_info.get("object_info") or "",
        ]
        return " ".join(parts).lower()

    def normalize(self, raw_type: str) -> str:
        if not raw_type:
            return "unknown"
        return self.TYPE_ALIASES.get(raw_type.lower().strip(), raw_type.lower().strip())

    def resolve(
        self,
        tender_info: Dict[str, Any],
        documents_text: str,
        llm_classification: Optional[str] = None,
        llm_confidence: float = 0.0,
        tender_type_hint: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        """
        Каскадное определение типа (v7.2.1).

        Приоритет:
        0. Негативы → other
        1. Education hard-rule (стем «обучен» в title)
        2. Слабая модель (confidence >= 0.5)
        3. Title heuristic (education → sout → plk → opr)
        4. Hint / текст / КТРУ
        """
        title_l = self._title_blob(tender_info)
        purchase_name = (tender_info.get("purchase_name") or title_l).lower()
        doc_text_lower = (documents_text or "").lower()
        full_text = (title_l + " " + doc_text_lower).lower()

        # ============================================================
        # ШАГ 0: Негативные ключевые слова
        # ============================================================
        our_profile_markers = (
            "обучен",
            "охрана труда",
            "охране труда",
            "проверка знаний",
            "соут",
            "специальная оценка",
            "специальной оценки",
            "оценки условий труда",
            "оценка условий труда",
            "производственный контроль",
            "производственного контроля",
            "плк",
            "лабораторн",
            "оценка профессиональных рисков",
            "профессиональных рисков",
            "измерен",
            "вредных и (или) опасных",
            "вредных производственных",
            "физических фактор",
            "рабочих мест",  # часто в СОУТ
            "карты соут",
        )
        has_our_profile = any(m in full_text for m in our_profile_markers)

        soft_negatives = (
            "экспертиза проектной документации",
            "техническое диагностирование",
            "диагностирование технических устройств",
            # нормировка — часто в приложениях к СОУТ, не предмет закупки
            "хронометраж",
            "фотография рабочего дня",
            "фотографии рабочего дня",
            "нормирован труда",
            "нормирования труда",
            "нормирование труда",
            "нормы труда",
            "положение о нормировании",
            "положения о нормировании",
        )

        for neg_kw in self.NEGATIVE_KEYWORDS:
            if neg_kw not in full_text:
                continue
            if has_our_profile and neg_kw in soft_negatives:
                logger.info(
                    f"[{self.VERSION}] Негатив '{neg_kw}' проигнорирован "
                    f"(есть маркеры ПЛК/СОУТ/ОТ)"
                )
                continue
            logger.warning(f"[{self.VERSION}] НЕГАТИВ: найдено '{neg_kw}' → other")
            return "other", "negative_keyword", "heuristic"

        # Выбросы / ПЭК без ОПР
        air_or_emission = any(
            m in full_text
            for m in (
                "источников выбросов",
                "предельно-допустимых выбросов",
                "выбросов в атмосферу",
                "производственного экологического контроля",
                "физико-химических показателей источников",
            )
        )
        opr_markers_text = any(
            m in full_text
            for m in (
                "оценка профессиональных рисков",
                "профессиональных рисков",
                "идентификация опасностей",
            )
        ) or self._has_opr_token(full_text)

        if air_or_emission and not opr_markers_text:
            logger.warning(f"[{self.VERSION}] Выбросы/ПЭК без маркеров ОПР → other")
            return "other", "emission_not_opr", "heuristic"

        if any(
            m in full_text
            for m in (
                "сопротивления изоляции",
                "сопротивление изоляции",
                "замер сопротивления изоляции",
            )
        ):
            logger.warning(
                f"[{self.VERSION}] Сопротивление изоляции → other (не ОПР/ПЛК)"
            )
            return "other", "electrical_not_our", "heuristic"

        # Замеры воздуха → PLK
        plk_air_markers = [
            "замеры воздуха",
            "замер воздуха",
            "воздуха рабочей зоны",
            "воздух рабочей зоны",
            "инструментальные замеры",
            "инструментальный замер",
            "контроль воздуха",
            "анализ воздуха рабочей",
        ]
        if any(m in full_text for m in plk_air_markers):
            if not opr_markers_text:
                logger.info(f"[{self.VERSION}] Замеры воздуха/рабочей зоны → plk")
                return "plk", "air_measurement_guard", "heuristic"

        # ============================================================
        # ШАГ 1: HARD RULE — Education (стем, до OPR/title)
        # ============================================================
        EDU_STEMS = (
            "обучен",
            "слушател",
            "повышен квалиф",
            "переподготов",
            "проверк знан",
            "образовательн услуг",
            "программ обучен",
            "подготовк кадров",
        )
        BLOCK_EDU = (
            "специальная оценка",
            " соут",
            "соут ",
            "соут,",
            "производственный контроль",
            "производственного контроля",
            "оценка профессиональных рисков",
            "профессиональных рисков",
            "идентификация опасностей",
        )
        has_edu = any(s in title_l for s in EDU_STEMS)
        has_block = any(b in title_l for b in BLOCK_EDU) or self._has_opr_token(title_l)

        if has_edu and not has_block:
            logger.info(
                f"[{self.VERSION}] HARD RULE: education stem in title → education "
                f"('{title_l[:70]}...')"
            )
            return "education", "hard_rule_education", "heuristic"

        # Текст документов: много «обучен» + слушатели
        if (
            doc_text_lower.count("обучен") >= 2
            and ("слушател" in doc_text_lower or "проверк знан" in doc_text_lower)
            and not any(
                b in doc_text_lower[:3000]
                for b in (
                    "оценка профессиональных рисков",
                    "специальная оценка условий",
                )
            )
        ):
            logger.info(
                f"[{self.VERSION}] HARD RULE: education markers in text → education"
            )
            return "education", "hard_rule_education_text", "heuristic"

        # ============================================================
        # ШАГ 2: Слабая модель
        # ============================================================
        if llm_classification and llm_confidence >= 0.5:
            normalized = self.normalize(llm_classification)
            # Не даём weak-model перебить явный education из title
            if has_edu and not has_block and normalized not in ("education",):
                logger.info(
                    f"[{self.VERSION}] Weak model '{normalized}' отклонена: "
                    f"title = education"
                )
            else:
                logger.info(
                    f"[{self.VERSION}] Тип из слабой модели: {normalized} "
                    f"(conf={llm_confidence:.2f})"
                )
                return normalized, "weak_model", "classify"

        # ============================================================
        # ШАГ 3: Title heuristic (education → sout → plk → opr)
        # ============================================================
        name_for_title = title_l if title_l.strip() else purchase_name

        if name_for_title.strip():
            for ttype in self.TITLE_ORDER:
                keywords = self.TITLE_KEYWORDS.get(ttype) or []
                if ttype == "opr":
                    if self._has_opr_token(name_for_title) or any(
                        kw in name_for_title for kw in keywords
                    ):
                        logger.info(
                            f"[{self.VERSION}] Тип из title: opr "
                            f"('{name_for_title[:60]}...')"
                        )
                        return "opr", "title_heuristic", "heuristic"
                    continue

                if any(kw in name_for_title for kw in keywords):
                    logger.info(
                        f"[{self.VERSION}] Тип из title: {ttype} "
                        f"('{name_for_title[:60]}...')"
                    )
                    return ttype, "title_heuristic", "heuristic"

        # ============================================================
        # ШАГ 4: Hint из detailed_parser
        # ============================================================
        is_education_title = has_edu
        is_education_text = any(
            kw in doc_text_lower[:2000]
            for kw in ("слушател", "учебный план", "программа обучения", "проверк знан")
        )

        if tender_type_hint:
            normalized = self.normalize(tender_type_hint)

            if normalized == "opr" and (is_education_title or is_education_text):
                logger.warning(
                    f"[{self.VERSION}] Конфликт: Hint=OPR, маркеры обучения → education"
                )
                return "education", "hint_override_education", "heuristic"

            if normalized == "opr" and any(
                kw in name_for_title for kw in ("лабораторн", "испытан", "замер")
            ):
                logger.warning(
                    f"[{self.VERSION}] Конфликт: Hint=OPR, лаборатория → plk"
                )
                return "plk", "hint_override_title", "heuristic"

            logger.info(f"[{self.VERSION}] Тип из detailed_parser hint: {normalized}")
            return normalized, "detailed_parser_hint", "hint"

        # ============================================================
        # ШАГ 5: Комбо по тексту
        # ============================================================
        has_sout_kw = any(
            kw in doc_text_lower
            for kw in ("специальная оценка", "соут", "оценка условий труда")
        )
        has_opr_kw = any(
            kw in doc_text_lower
            for kw in (
                "оценка профессиональных рисков",
                "профессиональных рисков",
            )
        ) or self._has_opr_token(doc_text_lower)
        has_plk_kw = any(
            kw in doc_text_lower
            for kw in (
                "производственный контроль",
                "плк",
                "лабораторные исследования",
                "замеры",
            )
        )

        combo_count = sum([has_sout_kw, has_opr_kw, has_plk_kw])
        if combo_count >= 2:
            logger.info(f"[{self.VERSION}] Комбо-тендер по тексту ({combo_count} типа)")
            return "combined", "text_heuristic_combo", "heuristic"

        # ============================================================
        # ШАГ 6: Защита от ложного ОПР
        # ============================================================
        if tender_type_hint == "opr":
            if any(
                kw in doc_text_lower
                for kw in (
                    "лабораторные исследования",
                    "испытания проб",
                    "сточной воды",
                )
            ):
                logger.warning(
                    f"[{self.VERSION}] Ложный ОПР: признаки лаборатории → plk"
                )
                return "plk", "text_guard_opr", "heuristic"

            if doc_text_lower.count("обучен") > 2 and "слушател" in doc_text_lower:
                logger.warning(
                    f"[{self.VERSION}] Ложный ОПР: признаки обучения → education"
                )
                return "education", "text_guard_opr_edu", "heuristic"

        # ============================================================
        # ШАГ 7: КТРУ-данные
        # ============================================================
        has_rm = bool(tender_info.get("rm_total") and tender_info["rm_total"] > 0)
        has_students = bool(
            tender_info.get("students_count") and tender_info["students_count"] > 0
        )
        has_points = bool(
            tender_info.get("points_count") and tender_info["points_count"] > 0
        )

        if (has_rm and has_points) or (has_rm and has_students):
            logger.info(f"[{self.VERSION}] Тип из КТРУ: combined")
            return "combined", "ktru", "data"

        if has_rm:
            logger.info(
                f"[{self.VERSION}] Тип из КТРУ: sout ({tender_info['rm_total']} РМ)"
            )
            return "sout", "ktru", "data"
        if has_points:
            logger.info(
                f"[{self.VERSION}] Тип из КТРУ: plk "
                f"({tender_info['points_count']} точек)"
            )
            return "plk", "ktru", "data"
        if has_students:
            logger.info(
                f"[{self.VERSION}] Тип из КТРУ: education "
                f"({tender_info['students_count']} слушателей)"
            )
            return "education", "ktru", "data"

        # ============================================================
        # ШАГ 8: Эвристика по тексту (education раньше opr в KEYWORDS)
        # ============================================================
        text_order = ("education", "sout", "plk", "opr", "testing")
        for ttype in text_order:
            keywords = self.KEYWORDS.get(ttype) or []
            if ttype == "opr":
                matched = any(
                    kw in doc_text_lower for kw in keywords
                ) or self._has_opr_token(doc_text_lower)
            else:
                matched = any(kw in doc_text_lower for kw in keywords)
            if not matched:
                continue
            if ttype == "education" and (
                "охрана труда" in doc_text_lower or "охране труда" in doc_text_lower
            ):
                logger.info(f"[{self.VERSION}] Тип из текста: education (ОТ)")
                return "education", "text_heuristic", "heuristic"
            logger.info(f"[{self.VERSION}] Тип из текста: {ttype}")
            return ttype, "text_heuristic", "heuristic"

        # ============================================================
        # ШАГ 9: Fallback
        # ============================================================
        logger.warning(f"[{self.VERSION}] Тип не определён → unknown")
        return "unknown", "fallback", "none"
