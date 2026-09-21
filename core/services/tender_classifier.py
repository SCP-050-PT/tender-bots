"""
Сервис классификации типов тендеров.
Использует базовую модель YandexGPT для быстрого анализа объекта закупки.
"""

import json
import re
import time
from typing import Optional, Dict, Any
from loguru import logger

from utils.llm_client import YandexGPTClient


class TenderClassifierService:
    """Классификатор типа тендера через базовую YandexGPT."""

    VERSION = "v1.0.0"
    MAX_RETRIES = 2
    RETRY_DELAY = 2

    def __init__(self, llm_client=None):
        self._llm = llm_client or YandexGPTClient()

    @property
    def llm(self):
        return self._llm

    def classify_tender_type(self, title: str, text_snippet: str = "") -> Optional[str]:
        """
        Определяет тип тендера (sout, education, opr, plk) по наименованию объекта закупки.
        """
        system_prompt = (
            "Ты — эксперт-аналитик госзакупок. "
            "Определи категорию тендера по наименованию объекта закупки.\n"
            "Допустимые категории:\n"
            "- sout (Специальная оценка условий труда / СОУТ)\n"
            "- education (Обучение / Повышение квалификации / Охрана труда)\n"
            "- opr (Оценка профессиональных рисков / ОПР)\n"
            "- plk (Производственный лабораторный контроль / ПЛК)\n"
            "- other (Если не подходит ни под одну из вышеуказанных)\n\n"
            'Верни ответ STRICTLY в формате JSON: {"tender_type": "категория"}'
        )

        user_message = f"Объект закупки: {title}"
        if text_snippet:
            user_message += f"\nДополнительный фрагмент:\n{text_snippet[:1500]}"

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                if attempt > 0:
                    time.sleep(self.RETRY_DELAY)

                response = self.llm.send(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    temperature=0.0,
                    max_tokens=100,
                )

                raw_text = (
                    response.get("raw_text", "")
                    if isinstance(response, dict)
                    else str(response)
                )
                parsed = self._parse_json(raw_text)

                if parsed and "tender_type" in parsed:
                    tender_type = str(parsed["tender_type"]).lower().strip()
                    logger.info(
                        f"[{self.VERSION}] Успешная классификация типа: '{tender_type}'"
                    )
                    return tender_type

            except Exception as e:
                logger.error(
                    f"[{self.VERSION}] Ошибка определения типа (попытка {attempt+1}): {e}"
                )
                continue

        logger.warning(f"[{self.VERSION}] Не удалось определить тип тендера.")
        return None

    def _parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return None
