"""
utils/llm_client.py
Клиент для YandexGPT и AI Studio Agents через Responses API.
v7.7.1: Исправлены пустые ответы агента (убрана обрезка input).
"""

import json
import re
from typing import Optional
from loguru import logger

# Создаем отдельный логгер для "мышления" агента
agent_logger = logger.bind(type="agent_thinking")

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

        # === РЕЖИМ АГЕНТА ЧЕРЕЗ ПРЯМОЙ HTTP-ЗАПРОС К RESPONSES API ===
        if self.use_agent:
            import requests as req_lib
            import time

            mode_name = "АГЕНТ (Responses API)"
            agent_uri = f"gpt://{self.folder_id}/{self.agent_id}/latest"
            url = "https://ai.api.cloud.yandex.net/v1/responses"
            headers = {
                "Authorization": f"Api-Key {self.agent_api_key}",
                "Content-Type": "application/json",
                "x-folder-id": self.folder_id,
            }

            payload = {
                "input": user_message,
                "prompt": {"id": self.agent_id},
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            }

            log_payload = dict(payload)
            if len(user_message) > 500:
                log_payload["input"] = user_message[:500] + "..."
            agent_logger.debug(
                f"📤 ЗАПРОС К АГЕНТУ:\n{json.dumps(log_payload, ensure_ascii=False, indent=2)}"
            )

            # === v7.8.0: RETRY ЛОГИКА ДЛЯ АГЕНТА ===
            agent_retries = 3
            for attempt in range(1, agent_retries + 1):
                try:
                    logger.info(
                        f"Запрос к Yandex ({mode_name}) ID: {self.agent_id} (попытка {attempt}/{agent_retries})"
                    )
                    logger.info(f"Вызов агента с prompt.id: {self.agent_id}")

                    response = req_lib.post(
                        url, headers=headers, json=payload, timeout=self.timeout
                    )
                    response.raise_for_status()
                    result = response.json()

                    # Парсим ответ от Responses API
                    text = ""
                    if "output" in result:
                        for item in result.get("output", []):
                            if item.get("type") == "message":
                                for content in item.get("content", []):
                                    if content.get("type") == "output_text":
                                        text += content.get("text", "")
                    elif "result" in result:
                        text = (
                            result.get("result", {})
                            .get("alternatives", [{}])[0]
                            .get("message", {})
                            .get("text", "")
                        )

                    agent_logger.debug(f"📥 СЫРОЙ ОТВЕТ АГЕНТА:\n{text}")
                    logger.info(f" ПОЛНЫЙ ОТВЕТ АГЕНТА:\n{text[:1000]}...")

                    # === v7.8.0: Если ответ пустой — пробуем снова ===
                    if not text or len(text.strip()) < 5:
                        logger.warning(
                            f"[{mode_name}] Пустой ответ (попытка {attempt}/{agent_retries})"
                        )
                        agent_logger.warning(f"⚠️ ПУСТОЙ ОТВЕТ (попытка {attempt})")
                        if attempt < agent_retries:
                            time.sleep(2 * attempt)
                            continue
                        else:
                            logger.error(
                                "❌ ПУСТОЙ ОТВЕТ ОТ АГЕНТА (все попытки исчерпаны)"
                            )
                            agent_logger.error(
                                "❌ ПУСТОЙ ОТВЕТ ОТ АГЕНТА (все попытки исчерпаны)"
                            )
                            return None
                    # ==========================================

                    parsed = self._extract_json(text)

                    # Обработка осознанных ошибок агента
                    if parsed and parsed.get("error") == "true":
                        reason = parsed.get("reason", "Неизвестная ошибка")
                        suggestion = parsed.get("suggestion", "")
                        logger.warning(f"⚠️ Агент ОТКАЗАЛСЯ анализировать: {reason}")
                        agent_logger.error(f"❌ ОШИБКА АГЕНТА: {reason}")
                        if suggestion:
                            agent_logger.info(f"💡 СОВЕТ АГЕНТА: {suggestion}")
                        return {
                            "blocked_by_error": True,
                            "reason": reason,
                            "suggestion": suggestion,
                        }

                    # Логирование мыслительного процесса
                    if parsed:
                        agent_logger.info(f" МЫСЛИ АГЕНТА:")
                        agent_logger.info(
                            f"   Уверенность: {parsed.get('confidence', 'N/A')}"
                        )
                        agent_logger.info(
                            f"   Решение: {parsed.get('decision', parsed.get('recommendation', 'N/A'))}"
                        )
                        params = {
                            k: v
                            for k, v in parsed.items()
                            if k
                            not in [
                                "reason",
                                "confidence",
                                "decision",
                                "recommendation",
                                "blocked_by_agent",
                            ]
                        }
                        agent_logger.info(
                            f"   Извлеченные параметры: {json.dumps(params, ensure_ascii=False, indent=4)}"
                        )

                    # Проверка на блокировку от агента
                    if parsed and parsed.get("decision") == "не рекомендуется":
                        logger.warning(
                            f"🛑 АГЕНТ ЗАБЛОКИРОВАЛ ТЕНДЕР: {parsed.get('reason')}"
                        )
                        agent_logger.warning(f"🚫 БЛОКИРОВКА: {parsed.get('reason')}")
                        return {
                            "decision": "не рекомендуется",
                            "reason": parsed.get("reason"),
                            "confidence": 1.0,
                            "blocked_by_agent": True,
                        }

                    # Маркировка ненадежных данных
                    if parsed and parsed.get("confidence", 0) < 0.5:
                        logger.warning(
                            f"️ Низкая уверенность агента ({parsed.get('confidence')}). Помечаю для fallback."
                        )
                        parsed["llm_unreliable"] = True
                        agent_logger.warning(
                            f"⚠️ НИЗКАЯ УВЕРЕННОСТЬ: {parsed.get('confidence')}"
                        )

                    if parsed:
                        logger.info("Ответ получен от АГЕНТ, извлекаю JSON...")
                        return parsed
                    else:
                        # JSON не распарсился — возможно, модель вернула мусор. Пробуем снова.
                        logger.warning(
                            f"[{mode_name}] Не удалось распарсить JSON (попытка {attempt}/{agent_retries}): {text[:200]}"
                        )
                        if attempt < agent_retries:
                            time.sleep(2 * attempt)
                            continue
                        else:
                            return {"raw_text": text, "parse_error": True}

                except req_lib.exceptions.Timeout:
                    logger.warning(
                        f"[{mode_name}] Таймаут (попытка {attempt}/{agent_retries})"
                    )
                    agent_logger.warning(f"⏰ ТАЙМАУТ (попытка {attempt})")
                    if attempt < agent_retries:
                        time.sleep(2 * attempt)
                        continue
                    else:
                        logger.error(f"[{mode_name}] Все попытки исчерпаны (таймаут)")
                        return None

                except req_lib.exceptions.HTTPError as e:
                    error_msg = str(e)
                    logger.error(f"HTTP ошибка агента: {e}")
                    agent_logger.error(f"❌ HTTP ОШИБКА: {error_msg}")

                    if "403" in error_msg or "Forbidden" in error_msg:
                        logger.error(
                            "❌ Ошибка 403: API-ключ не имеет прав на вызов агентов."
                        )
                        logger.error(
                            "💡 Решение: Создайте новый API-ключ для SA с полными правами на AI Studio."
                        )
                        self.use_agent = False
                        break  # Выходим из цикла retry, переключаемся на модель
                    elif "400" in error_msg or "Bad Request" in error_msg:
                        logger.error("❌ Ошибка 400: Проверьте ID агента и Folder ID.")
                        self.use_agent = False
                        break
                    elif "429" in error_msg:
                        logger.warning(
                            f"[{mode_name}] Rate limit (429), ждем 10 сек..."
                        )
                        if attempt < agent_retries:
                            time.sleep(10)
                            continue
                        else:
                            return None
                    else:
                        if attempt < agent_retries:
                            time.sleep(2 * attempt)
                            continue
                        else:
                            return None

                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Ошибка вызова агента: {e}")
                    agent_logger.error(f"❌ ОШИБКА ВЫЗОВА АГЕНТА: {error_msg}")
                    if attempt < agent_retries:
                        time.sleep(2 * attempt)
                        continue
                    else:
                        logger.warning(
                            "⚠️ Агент недоступен после всех попыток. Переключаюсь на обычную модель."
                        )
                        self.use_agent = False
                        break
            # === КОНЕЦ RETRY ЦИКЛА ===
        # === РЕЖИМ ОБЫЧНОЙ МОДЕЛИ (FALLBACK) ===
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
                    if response.status_code == 400:
                        logger.error(
                            "Ошибка 400 на модели. Возможно, неверное имя модели или закончилась квота."
                        )
                        return None
                    return None
                except Exception as e:
                    logger.error(f"Ошибка: {e}")
                    if attempt < self.max_retries:
                        time.sleep(2**attempt)
                    continue

        return None

    def _extract_json(self, text: str) -> Optional[dict]:
        """Извлекает JSON из текста ответа."""
        if "{" in text and "}" in text:
            # Находим последнюю закрывающую скобку основного объекта
            brace_count = 0
            last_brace_idx = -1
            for i, char in enumerate(text):
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        last_brace_idx = i
                        break

            if last_brace_idx != -1:
                text = text[: last_brace_idx + 1]
        # ==========================================

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
