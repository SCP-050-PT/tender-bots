"""
Сервис глубокого анализа тендеров через AI Studio Agent.
v8.0.0: Работает исключительно через YandexAgentClient.
"""

import json
import re
import time
from typing import Optional, Dict, Any
from loguru import logger
from config.prompts import build_agent_system_prompt
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

        system_prompt = build_agent_system_prompt(tender_type)

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
        """User-сообщение: контекст парсера + текст ТЗ. EXTRACT_* уже в system."""
        text_limit = 30000 if tender_type == "education" else 25000
        header = (
            f"РЕГИСТРАЦИОННЫЙ НОМЕР ТЕНДЕРА: {tender_id}\n\n" if tender_id else ""
        )

        verification_block = ""
        if context_override:
            verification_block = (
                "=== КОНТЕКСТ ПАРСЕРА ===\n"
                f"{context_override}\n"
                "Верифицируй и дополни из ТЗ. Нет явной цифры — null, не выдумывай.\n\n"
            )

        parts = [
            header,
            verification_block,
            f"Текст тендера:\n{(documents_text or '')[:text_limit]}",
            "\nВерни результат строго в JSON.",
        ]
        return "".join(parts)