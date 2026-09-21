"""
utils/llm_client.py
Клиент для работы с Yandex AI Studio Agents и YandexGPT API.
"""

import json
import os
import re
from typing import Optional, Dict, Any
import requests
from loguru import logger

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

agent_logger = logger.bind(type="agent_thinking")


class YandexGPTClient:
    """Клиент YandexGPT с поддержкой AI Studio Agents и классификацией по title."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        folder_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        # Автоматическое подтягивание из .env, если параметры не переданы явно
        self.api_key = (
            api_key or os.getenv("YANDEX_API_KEY") or os.getenv("YANDEX_GPT_API_KEY")
        )
        self.folder_id = folder_id or os.getenv("YANDEX_FOLDER_ID")
        self.agent_id = (
            agent_id or os.getenv("YANDEX_AGENT_ID") or os.getenv("YANDEX_GPT_AGENT_ID")
        )
        self.model_name = (
            model_name or os.getenv("YANDEX_GPT_MODEL") or "yandexgpt/latest"
        )

        if OpenAI and self.api_key:
            # Настройка OpenAI SDK под Yandex AI Studio
            self.openai_client = OpenAI(
                api_key=self.api_key,
                base_url="https://ai.api.cloud.yandex.net/v1",
                project=self.folder_id,
            )
        else:
            self.openai_client = None

    def send(
        self,
        prompt: Optional[str] = None,
        system_prompt: Optional[str] = None,
        documents_text: str = "",
        user_message: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Совместимость с LlmService."""
        effective_prompt = prompt or user_message or ""

        return self.analyze_documents(
            prompt=effective_prompt,
            documents_text=documents_text,
            system_prompt=system_prompt,
        )

    def classify_tender_type(self, title: str) -> str:
        """Быстрая предварительная классификация тендера по названию."""
        if not title or not title.strip():
            return "other"

        system_prompt = (
            "Ты — классификатор государственных закупок. "
            "Твоя задача — определить тип тендера по его названию (объекту закупки).\n\n"
            "Допустимые типы ответов:\n"
            "- sout (Специальная оценка условий труда / СОУТ)\n"
            "- opr (Оценка профессиональных рисков / ОПР)\n"
            "- sout_opr (Совмещенный тендер: СОУТ + ОПР)\n"
            "- education (Обучение по охране труда, ДПО, повышение квалификации, проверка знаний)\n"
            "- plk (Производственный контроль / Лабораторные исследования)\n"
            "- other (Любые другие услуги, товары или работы)\n\n"
            "Отвечай СТРОГО одним словом из этого списка без пояснений, кавычек и знаков препинания."
        )

        user_prompt = f"Объект закупки: {title}"

        try:
            response_text = self._fallback_completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=20,
                temperature=0.0,
            )

            clean_res = response_text.strip().lower().replace('"', "").replace("'", "")

            valid_types = {"sout", "opr", "sout_opr", "education", "plk", "other"}
            if clean_res in valid_types:
                return clean_res

            for vt in valid_types:
                if vt in clean_res:
                    return vt

            return "other"

        except Exception as e:
            logger.error(f"[Classify] Ошибка классификации YandexGPT по title: {e}")
            return "unknown"

    def analyze_documents(
        self,
        prompt: str,
        documents_text: str = "",
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Глубокий анализ документов через AI Studio Agent или REST Fallback."""
        full_user_prompt = f"{prompt}\n\nТекст документов:\n{documents_text[:100000]}"

        # Вызов AI Studio Agent
        if self.openai_client and self.agent_id:
            try:
                logger.info(
                    f"Отправка запроса в AI Studio Agent (agent_id={self.agent_id})..."
                )

                response = self.openai_client.responses.create(
                    prompt={"id": self.agent_id},
                    input=full_user_prompt,
                )

                # Проверяем, не упал ли Агент на стороне Yandex Cloud (например, по MCP 403)
                if getattr(response, "status", None) == "failed":
                    error_msg = getattr(response, "error", None)
                    logger.error(
                        f"Yandex Agent завершился с ошибкой (failed): {error_msg}"
                    )
                    # Переходим на fallback
                    raise RuntimeError(f"Agent failed: {error_msg}")

                # Извлекаем текст
                output_text = getattr(response, "text", None) or getattr(
                    response, "output_text", ""
                )
                logger.info(f"📄 ТЕЛО ОТВЕТА АГЕНТА:\n{output_text}")
                if not output_text:
                    output_text = str(response)

                parsed_json = self._extract_json(output_text)
                if parsed_json:
                    return parsed_json

                return {"raw_response": output_text}

            except Exception as e:
                logger.warning(
                    f"Ошибка вызова AI Studio Agent: {e}. Переход на REST fallback..."
                )

        else:
            if not self.agent_id:
                logger.warning(
                    "YANDEX_AGENT_ID не задан. Пропуск вызова Агента, переход к REST fallback..."
                )

        # Fallback на базовую модель YandexGPT
        raw_text = self._fallback_completion(
            system_prompt=system_prompt or "Ты профессиональный аналитик тендеров.",
            user_prompt=full_user_prompt,
        )

        parsed_json = self._extract_json(raw_text)
        if parsed_json:
            return parsed_json

        return {
            "error": "Не удалось распарсить JSON из ответа модели",
            "raw_response": raw_text,
            "llm_unreliable": True,
        }

    def _fallback_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2000,
        temperature: float = 0.1,
    ) -> str:
        """Прямой REST запрос к Yandex Foundation Models API."""
        url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

        model_name = self.model_name or "yandexgpt/latest"
        if not model_name.startswith("gpt://"):
            model_uri = f"gpt://{self.folder_id}/{model_name}"
        else:
            model_uri = model_name

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {self.api_key}",
            "x-folder-id": self.folder_id or "",
        }

        payload = {
            "modelUri": model_uri,
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": max_tokens,
            },
            "messages": [
                {"role": "system", "text": system_prompt},
                {"role": "user", "text": user_prompt},
            ],
        }

        response = requests.post(url, headers=headers, json=payload, timeout=60)

        if response.status_code == 401:
            logger.error(
                "[REST Fallback 401] Ошибка авторизации. Проверьте API-ключ в .env "
                "и убедитесь, что у него нет ограничений в 'Области действия' (Scope)."
            )

        response.raise_for_status()

        data = response.json()
        alternatives = data.get("result", {}).get("alternatives", [])
        if alternatives:
            return alternatives[0].get("message", {}).get("text", "")

        return ""

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Извлечение и очистка JSON из текста ответа."""
        if not text:
            return None

        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
        if match:
            text = match.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start : end + 1]

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error("Не удалось декодировать JSON из ответа LLM")
            return None
