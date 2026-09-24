"""
utils/llm_client.py
Клиент для Yandex AI Studio / YandexGPT и Timeweb (OpenAI-compatible).
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
    """Клиент LLM: Yandex (по умолчанию) или Timeweb при LLM_PROVIDER=timeweb."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        folder_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self.provider = (os.getenv("LLM_PROVIDER") or "yandex").strip().lower()

        # --- Timeweb ---
        self.timeweb_api_key = os.getenv("TIMEWEB_API_KEY")
        self.timeweb_base_url = (
            os.getenv("TIMEWEB_BASE_URL") or ""
        ).rstrip("/")

        # --- Yandex ---
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

        self.openai_client = None
        self.timeweb_client = None

        if self.provider == "timeweb":
            if OpenAI and self.timeweb_api_key and self.timeweb_base_url:
                self.timeweb_client = OpenAI(
                    api_key=self.timeweb_api_key,
                    base_url=self.timeweb_base_url,
                )
                logger.info(
                    f"[LLM] Провайдер: timeweb | base={self.timeweb_base_url}"
                )
            else:
                logger.error(
                    "[LLM] LLM_PROVIDER=timeweb, но нет TIMEWEB_API_KEY / "
                    "TIMEWEB_BASE_URL или пакета openai"
                )
        else:
            if OpenAI and self.api_key:
                self.openai_client = OpenAI(
                    api_key=self.api_key,
                    base_url="https://ai.api.cloud.yandex.net/v1",
                    project=self.folder_id,
                )
            logger.info("[LLM] Провайдер: yandex")

    def send(
        self,
        prompt: Optional[str] = None,
        system_prompt: Optional[str] = None,
        documents_text: str = "",
        user_message: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        effective_prompt = prompt or user_message or ""
        return self.analyze_documents(
            prompt=effective_prompt,
            documents_text=documents_text,
            system_prompt=system_prompt,
        )

    def classify_tender_type(self, title: str) -> str:
        """Быстрая классификация по названию (работает на обоих провайдерах)."""
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
            response_text = self._completion(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=20,
                temperature=0.0,
            )
            clean_res = (
                response_text.strip().lower().replace('"', "").replace("'", "")
            )
            valid_types = {"sout", "opr", "sout_opr", "education", "plk", "other"}
            if clean_res in valid_types:
                return clean_res
            for vt in valid_types:
                if vt in clean_res:
                    return vt
            return "other"
        except Exception as e:
            logger.error(f"[Classify] Ошибка классификации: {e}")
            return "unknown"

    def analyze_documents(
        self,
        prompt: str,
        documents_text: str = "",
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        full_user_prompt = f"{prompt}\n\nТекст документов:\n{documents_text[:100000]}"
        sys = system_prompt or "Ты профессиональный аналитик тендеров."

        # --- Timeweb (Qwen и др.) ---
        if self.provider == "timeweb" and self.timeweb_client:
            try:
                logger.info("[LLM] Запрос в Timeweb (OpenAI-compatible)...")
                create_kwargs = {
                    "model": "qwen",  # Timeweb берёт модель агента
                    "messages": [
                        {"role": "system", "content": sys},
                        {"role": "user", "content": full_user_prompt},
                    ],
                    "max_tokens": 4000,
                }
                # GPT-6 Luna / gpt-6* не принимают temperature
                # если вернётесь на Qwen — можно снова добавить temperature=0.25
                response = self.timeweb_client.chat.completions.create(**create_kwargs)
                output_text = response.choices[0].message.content or ""
                logger.info(f"📄 ТЕЛО ОТВЕТА TIMEWEB:\n{output_text[:2000]}")
                parsed = self._extract_json(output_text)
                if parsed:
                    return parsed
                return {"raw_response": output_text}
            except Exception as e:
                logger.error(f"[LLM] Ошибка Timeweb: {e}")
                return {
                    "error": True,
                    "reason": f"Timeweb API error: {e}",
                    "llm_unreliable": True,
                }

        # --- Yandex Agent ---
        if self.openai_client and self.agent_id:
            try:
                logger.info(
                    f"Отправка запроса в AI Studio Agent (agent_id={self.agent_id})..."
                )
                response = self.openai_client.responses.create(
                    prompt={"id": self.agent_id},
                    input=full_user_prompt,
                )
                if getattr(response, "status", None) == "failed":
                    error_msg = getattr(response, "error", None)
                    logger.error(f"Yandex Agent failed: {error_msg}")
                    raise RuntimeError(f"Agent failed: {error_msg}")

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
                    f"Ошибка AI Studio Agent: {e}. REST fallback..."
                )
        else:
            if not self.agent_id and self.provider != "timeweb":
                logger.warning(
                    "YANDEX_AGENT_ID не задан. REST fallback..."
                )

        # --- Yandex REST fallback ---
        raw_text = self._fallback_completion(
            system_prompt=sys,
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

    def _completion(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2000,
        temperature: float = 0.1,
    ) -> str:
        """Единая точка completion для classify и простых вызовов."""
        if self.provider == "timeweb" and self.timeweb_client:
            create_kwargs = {
                "model": "qwen",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
            }
            response = self.timeweb_client.chat.completions.create(**create_kwargs)
            return response.choices[0].message.content or ""
        return self._fallback_completion(
            system_prompt, user_prompt, max_tokens, temperature
        )

    def _fallback_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2000,
        temperature: float = 0.1,
    ) -> str:
        """Прямой REST к Yandex Foundation Models."""
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
                "[REST Fallback 401] Проверьте API-ключ и Scope."
            )
        response.raise_for_status()
        data = response.json()
        alternatives = data.get("result", {}).get("alternatives", [])
        if alternatives:
            return alternatives[0].get("message", {}).get("text", "")
        return ""

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
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
        