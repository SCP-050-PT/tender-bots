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
import os
from config.settings import settings
from utils.mcp_client import gather_tender_mcp_context

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
        force_mcp = getattr(settings, "FORCE_MCP_DOCS", False) or os.getenv(
            "FORCE_MCP_DOCS", ""
        ).strip().lower() in ("1", "true", "yes")

        mcp_context = ""
        if force_mcp and tender_id:
            logger.info(
                f"[{self.VERSION}] FORCE_MCP_DOCS: вызываем MCP tools для {tender_id}..."
            )
            mcp_context = gather_tender_mcp_context(tender_id, tender_type)
            # текст ТЗ агенту не нужен — только MCP + парсер
            documents_text = ""

        prompt = self.build_prompt(
            tender_type=tender_type,
            documents_text=documents_text,
            tender_id=tender_id,
            context_override=context_override,
            nmck=nmck,
        )
        if mcp_context:
            prompt = (
                prompt
                + "\n\n"
                + mcp_context
                + "\n\nПо tool_results выше заполни JSON. "
                "Нет числа в tools → null. Не выдумывай.\n"
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
        nmck: float = 0,
    ) -> str:
        """User-сообщение. При FORCE_MCP_DOCS — только tools, без сырого ТЗ."""
        header = (
            f"РЕГИСТРАЦИОННЫЙ НОМЕР ТЕНДЕРА: {tender_id}\n\n" if tender_id else ""
        )
        verification_block = ""
        if context_override:
            verification_block = (
                "=== КОНТЕКСТ ПАРСЕРА (КТРУ/карточка, не полный ТЗ) ===\n"
                f"{context_override}\n"
                "Верифицируй через MCP. Нет явной цифры в tools — null.\n\n"
            )

        force_mcp = getattr(settings, "FORCE_MCP_DOCS", False) or os.getenv(
            "FORCE_MCP_DOCS", ""
        ).strip().lower() in ("1", "true", "yes")

        if force_mcp and tender_id:
            qty_q = {
                "sout": "рабочих мест",
                "opr": "должност",
                "plk": "точек",
                "education": "слушател",
            }.get(tender_type, "количество")

            mcp_block = f"""
=== РЕЖИМ FORCE_MCP_DOCS ===
Сырой текст документов НЕ приложен. Данные только через MCP.

ОБЯЗАТЕЛЬНО до JSON вызови tools по порядку:
1) list_tender_files(tender_id="{tender_id}")
2) search_documents(tender_id="{tender_id}", query="адрес")
3) search_documents(tender_id="{tender_id}", query="место оказания")
4) search_documents(tender_id="{tender_id}", query="{qty_q}")
5) find_qty_hints(tender_id="{tender_id}", kind="{tender_type}")
6) если в list есть xls/xlsx — read_excel_table(tender_id="{tender_id}")

Кратко перечисли tool_results, затем строго JSON:
type, rm_total / opr_positions / measurement_points / students_count,
addresses_count, cities_count, regions_count, has_iii, confidence.
Не выдумывай числа без tool_results.
"""
            parts = [
                header,
                verification_block,
                f"Тип (подсказка): {tender_type}\n",
                mcp_block,
            ]
            logger.info(
                f"[{self.VERSION}] FORCE_MCP_DOCS=1 → промпт без текста ТЗ "
                f"(tender_id={tender_id})"
            )
            return "".join(parts)

        text_limit = 30000 if tender_type == "education" else 25000
        parts = [
            header,
            verification_block,
            f"Текст тендера:\n{(documents_text or '')[:text_limit]}",
            "\nВерни результат строго в JSON.",
        ]
        return "".join(parts)
