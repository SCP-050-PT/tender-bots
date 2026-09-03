"""
utils/llm_client.py
Клиент для YandexGPT и AI Studio Agents через Responses API.
"""

import json
import re
from typing import Optional
from loguru import logger

try:
    import openai

    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    logger.warning("openai SDK не установлен. Агент будет недоступен.")

from config.settings import settings


class YandexGPTClient:
    # Эндпоинт для обычной модели
    BASE_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    # Эндпоинт для агентов (OpenAI compatible)
    AGENT_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

    def __init__(
        self, folder_id=None, api_key=None, model=None, max_retries=3, timeout=60
    ):
        self.folder_id = folder_id or settings.YANDEX_FOLDER_ID
        self.max_retries = max_retries
        self.timeout = timeout

        # Настройки Агента
        self.use_agent = settings.USE_AI_AGENT
        self.agent_id = settings.YANDEX_AGENT_ID
        self.agent_api_key = settings.YANDEX_AGENT_API_KEY

        # Настройки обычной модели
        self.model_api_key = api_key or settings.YANDEX_API_KEY
        self.model_name = model or settings.YANDEX_GPT_MODEL

        if not self.folder_id:
            raise ValueError("YANDEX_FOLDER_ID не задан")

        if self.use_agent:
            if not HAS_OPENAI:
                logger.error("openai SDK не установлен. Переключаюсь на модель.")
                self.use_agent = False
            elif not self.agent_api_key or not self.agent_id:
                logger.warning("USE_AI_AGENT=True, но ключи агента не заданы.")
                self.use_agent = False
            else:
                logger.info(f" Режим АГЕНТА (Responses API): {self.agent_id}")
        else:
            if not self.model_api_key:
                raise ValueError("YANDEX_API_KEY не задан")
            logger.info(f" Режим МОДЕЛИ: {self.model_name}")

    def send(
        self, system_prompt: str, user_message: str, temperature=0.3, max_tokens=2000
    ) -> Optional[dict]:

        mode_name = "МОДЕЛЬ"

        # === РЕЖИМ АГЕНТА ЧЕРЕZ RESPONSES API ===
        if self.use_agent:
            try:
                mode_name = "АГЕНТ (Responses API)"
                logger.info(f"Запрос к Yandex ({mode_name})")

                # Создаем клиент OpenAI для Yandex Cloud
                client = openai.OpenAI(
                    api_key=self.agent_api_key,
                    base_url=self.AGENT_BASE_URL,
                    project=self.folder_id,  # Это folder_id в терминологии Yandex
                )

                # Вызываем агента через Responses API
                response = client.responses.create(
                    prompt={
                        "id": self.agent_id,
                    },
                    input=user_message,
                    # Параметры температуры и токенов берутся из настроек агента в AI Studio
                )

                
                text = response.output_text
                logger.info(f" ПОЛНЫЙ ОТВЕТ АГЕНТА:\n{text[:1000]}...")

                if text:
                    parsed = self._extract_json(text)
                    
                    # === ПРОВЕРКА НА БЛОКИРОВКУ ОТ АГЕНТА ===
                    if parsed and parsed.get("decision") == "не рекомендуется":
                        logger.warning(f"🛑 АГЕНТ ЗАБЛОКИРОВАЛ ТЕНДЕР: {parsed.get('reason')}")
                        return {
                            "decision": "не рекомендуется",
                            "reason": parsed.get("reason"),
                            "confidence": 1.0,
                            "blocked_by_agent": True
                        }
                    # ==========================================
                    
                    if parsed:
                        logger.info("Ответ получен от АГЕНТ, извлекаю JSON...")
                        return parsed
                    else:
                        return {"raw_text": text, "parse_error": True}
                else:
                    logger.error("Пустой ответ от агента")
                    return None

            except Exception as e:
                logger.error(f"Ошибка вызова агента: {e}")
                logger.warning("⚠️ Агент недоступен. Переключаюсь на обычную модель.")
                # Автоматический fallback на обычную модель
                self.use_agent = False
                # Продолжаем выполнение ниже с обычной моделью

        # === РЕЖИМ ОБЫЧНОЙ МОДЕЛИ (или FALLBACK) ===
        if not self.use_agent:
            mode_name = "МОДЕЛЬ (FALLBACK)" if hasattr(self, "_was_agent") else "МОДЕЛЬ"

            import requests
            import time

            model_uri = f"gpt://{self.folder_id}/{self.model_name}/latest"
            payload = {
                "modelUri": model_uri,
                "completionOptions": {
                    "stream": False,
                    "temperature": temperature,
                    "maxTokens": str(max_tokens),
                },
                "messages": [
                    {"role": "system", "text": system_prompt},
                    {"role": "user", "text": user_message},
                ],
            }
            headers = {
                "Authorization": f"Api-Key {self.model_api_key}",
                "x-folder-id": self.folder_id,
                "Content-Type": "application/json",
            }

            for attempt in range(1, self.max_retries + 1):
                try:
                    logger.info(
                        f"Запрос к Yandex ({mode_name}) попытка {attempt}/{self.max_retries}"
                    )

                    response = requests.post(
                        self.BASE_URL,
                        headers=headers,
                        json=payload,
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    result = response.json()

                    if "result" in result and "alternatives" in result["result"]:
                        text = result["result"]["alternatives"][0]["message"]["text"]
                        logger.info(f"Ответ получен от {mode_name}, извлекаю JSON...")
                        parsed = self._extract_json(text)
                        return (
                            parsed
                            if parsed
                            else {"raw_text": text, "parse_error": True}
                        )
                    else:
                        logger.error(f"Неожиданная структура ответа: {result}")
                        return None

                except requests.exceptions.Timeout:
                    logger.warning(f"Таймаут (попытка {attempt})")
                    if attempt < self.max_retries:
                        time.sleep(2**attempt)
                    continue
                except requests.exceptions.HTTPError as e:
                    logger.error(f"HTTP ошибка: {e}")
                    if response.status_code == 429:
                        time.sleep(10)
                        continue
                    return None
                except Exception as e:
                    logger.error(f"Ошибка: {e}")
                    if attempt < self.max_retries:
                        time.sleep(2**attempt)
                    continue

        return None

    def _extract_json(self, text: str) -> Optional[dict]:
        """Извлекает JSON из текста ответа."""
        json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            json_match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                json_match = re.search(r"(\{.*\})", text, re.DOTALL)
                if json_match:
                    json_str = json_match.group(1)
                else:
                    return None
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    def analyze_tender(
        self, tender_text: str, system_prompt: Optional[str] = None
    ) -> Optional[dict]:
        from config.prompts import load_system_prompt

        prompt = system_prompt or load_system_prompt()
        return self.send(
            system_prompt=prompt,
            user_message=tender_text,
            temperature=0.2,
            max_tokens=2500,
        )
