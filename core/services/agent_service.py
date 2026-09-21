"""
Сервис глубокого анализа тендеров через AI Studio Agent.
v8.0.0: Работает исключительно через YandexAgentClient.
"""

import json
import re
import time
from typing import Optional, Dict, Any
from loguru import logger

from config.prompts import load_system_prompt
from utils.llm_client import YandexGPTClient


class AgentService:
    """Сервис глубинного анализа документов через AI Studio Agent."""

    VERSION = "v8.0.0"
    MAX_RETRIES = 2
    RETRY_DELAY = 3

    def __init__(self, agent_client=None):
        self._agent = agent_client or YandexGPTClient()

    @property
    def agent(self):
        return self._agent

    def extract_params(
        self,
        tender_type: str,
        documents_text: str,
        nmck: float = 0,
        tender_id: str = "",
        context_override: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Извлекает и верифицирует параметры тендера через AI Агента."""
        prompt = self.build_prompt(
            tender_type=tender_type,
            documents_text=documents_text,
            tender_id=tender_id,
            context_override=context_override,
        )

        system_prompt = (
            "Ты — автономный AI Агент, аналитик тендеров в сфере охраны труда и промышленной безопасности. "
            "Твоя задача — извлекать параметры и проводить верификацию документации. "
            "Используй доступные инструменты и возвращай строго итоговый валидный JSON."
        )

        last_error = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                if attempt > 0:
                    logger.warning(
                        f"[{self.VERSION}] Повторный запрос к Агенту #{attempt} для тендера {tender_id}..."
                    )
                    time.sleep(self.RETRY_DELAY)

                # Вызов именно AI Агента
                response = self.agent.send(
                    system_prompt=system_prompt,
                    user_message=prompt,
                    temperature=0.1,
                )

                raw_text = ""
                if isinstance(response, dict) and "raw_text" in response:
                    raw_text = response["raw_text"]
                elif isinstance(response, str):
                    raw_text = response
                elif isinstance(response, dict):
                    return response

                parsed_data = self.parse_response(raw_text)

                if parsed_data is None:
                    logger.warning(
                        f"[{self.VERSION}] Не удалось распарсить JSON от Агента."
                    )
                    last_error = "ParseError"
                    continue

                return parsed_data

            except Exception as e:
                logger.error(
                    f"[{self.VERSION}] Ошибка AI Агента (попытка {attempt+1}): {e}"
                )
                last_error = str(e)
                continue

        logger.error(
            f"[{self.VERSION}] Агент не смог обработать тендер {tender_id}. Ошибка: {last_error}"
        )
        return None

    def parse_response(self, text: str) -> Optional[Dict[str, Any]]:
        """Извлекает и валидирует JSON из ответа Агента."""
        if not text or not isinstance(text, str):
            return None

        cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        json_match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        return None

    def build_prompt(
        self,
        tender_type: str,
        documents_text: str,
        tender_id: str = "",
        context_override: str = None,
    ) -> str:
        """Формирует промпт для Агента из system_prompt.txt."""
        section_map = {
            "sout": "EXTRACT_SOUT",
            "education": "EXTRACT_EDUCATION",
            "opr": "EXTRACT_OPR",
            "plk": "EXTRACT_PLK",
        }

        section_name = section_map.get(tender_type)
        if not section_name:
            return (
                f"Извлеки параметры для типа {tender_type}:\n{documents_text[:15000]}"
            )

        try:
            full_prompt = load_system_prompt()
            section_text = self._get_section(full_prompt, section_name)
            if section_text:
                text_limit = 30000 if tender_type == "education" else 15000
                header = (
                    f"РЕГИСТРАЦИОННЫЙ НОМЕР ТЕНДЕРА: {tender_id}\n\n"
                    if tender_id
                    else ""
                )

                verification_block = ""
                if context_override:
                    verification_block = f"""
=== КОНТЕКСТ ВЕРИФИКАЦИИ (ДАННЫЕ ОТ ПАРСЕРА) ===
{context_override}

ТВОЯ ЗАДАЧА — ВЕРИФИЦИРОВАТЬ ВЫШЕУКАЗАННЫЕ ДАННЫЕ.
=============================================
"""

                parts = [
                    header,
                    verification_block,
                    section_text,
                    "",
                    f"Текст тендера:\n{documents_text[:text_limit]}",
                    "Верни результат в формате JSON.",
                ]
                return "\n".join(parts)

        except Exception as e:
            logger.error(f"[{self.VERSION}] Ошибка загрузки промпта: {e}")

        return f"Извлеки параметры для типа {tender_type}:\n{documents_text[:15000]}"

    @staticmethod
    def _get_section(prompt_text: str, section_name: str) -> str:
        pattern = re.compile(
            rf"===\s*{section_name}\s*===(.*?)(?====\s*\w+\s*===|\Z)",
            re.DOTALL | re.IGNORECASE,
        )
        match = pattern.search(prompt_text)
        return match.group(1).strip() if match else ""
