"""
core/http_session.py
Единая HTTP-сессия на базе curl_cffi.
v6.8.7: Увеличены таймауты, добавлены мягкие задержки и улучшена совместимость с zakupki.gov.ru
"""
import string
import random
import time
from typing import Optional, List, Any
from loguru import logger

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except ImportError:
    import requests as curl_requests

    HAS_CURL_CFFI = False
    logger.warning("⚠️ curl_cffi не найдена, используем обычный requests")

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def get_platform_from_ua(user_agent: str) -> str:
    if "Windows" in user_agent:
        return "Windows"
    elif "Macintosh" in user_agent:
        return "macOS"
    elif "Linux" in user_agent:
        return "Linux"
    return "Windows"


class HTTPSessionManager:
    """
    Единый менеджер HTTP-сессий.
    v6.8.7: Мягкие задержки, увеличенные таймауты, лучшая совместимость с zakupki
    """

    DEFAULT_POOL_SIZE = 3
    REQUEST_DELAY = (1.0, 2.5)  # УВЕЛИЧЕНЫ задержки для "мягкости"
    BACKOFF_BASE = 10  # УВЕЛИЧЕН базовый backoff

    # НОВЫЕ НАСТРОЙКИ ТАЙМАУТОВ
    CONNECT_TIMEOUT = 15  # Время на установление соединения
    READ_TIMEOUT = 90  # Время на чтение ответа (для больших файлов)
    TOTAL_TIMEOUT = 120  # Общее время запроса

    def __init__(self, pool_size: int = 3, proxy: Optional[str] = None):
        self.pool_size = pool_size
        self.proxy = proxy
        self._sessions: List[Any] = []
        self._consecutive_429 = 0
        self._base_delay = self.BACKOFF_BASE

        self._init_pool()
        logger.info(
            f"HTTPSessionManager: {pool_size} сессий (curl_cffi={HAS_CURL_CFFI}, "
            f"таймауты: connect={self.CONNECT_TIMEOUT}s, read={self.READ_TIMEOUT}s)"
        )

    def _init_pool(self):
        """Инициализирует пул сессий."""
        for i in range(self.pool_size):
            session = self._create_session()
            self._sessions.append(session)

    def _create_session(self) -> Any:
        """Создаёт одну сессию с оптимизированными настройками."""
        user_agent = get_random_user_agent()
        platform = get_platform_from_ua(user_agent)

        try:
            if HAS_CURL_CFFI:
                # Используем chrome124 как наиболее стабильный для российских сайтов
                session = curl_requests.Session(impersonate="chrome124")

                # БЕЗОПАСНАЯ НАСТРОЙКА CURL (работает на всех версиях)
                try:
                    # Пробуем новый способ доступа (v0.7+)
                    if hasattr(curl_requests, 'CurlOpt'):
                        session.curl.setopt(curl_requests.CurlOpt.HTTP_VERSION, 
                                           curl_requests.CurlHttpVersion.V1_1)
                        session.curl.setopt(curl_requests.CurlOpt.TCP_KEEPALIVE, 1)
                        session.curl.setopt(curl_requests.CurlOpt.TCP_KEEPIDLE, 60)
                        session.curl.setopt(curl_requests.CurlOpt.TCP_KEEPINTVL, 30)
                    else:
                        # Старый способ (v0.5-v0.6)
                        from curl_cffi import CurlOpt, CurlHttpVersion
                        session.curl.setopt(CurlOpt.HTTP_VERSION, CurlHttpVersion.V1_1)
                        session.curl.setopt(CurlOpt.TCP_KEEPALIVE, 1)
                        session.curl.setopt(CurlOpt.TCP_KEEPIDLE, 60)
                        session.curl.setopt(CurlOpt.TCP_KEEPINTVL, 30)
                except Exception as e:
                    logger.debug(f"Не удалось настроить curl options: {e}. Используем дефолтные.")

            else:
                session = curl_requests.Session()

            if self.proxy:
                session.proxies = {"http": self.proxy, "https": self.proxy}

        except Exception as e:
            logger.error(f"Ошибка создания сессии: {e}")
            session = curl_requests.Session()

        self.update_session_headers(session, user_agent, platform)
        session.cookies.set("EPZ_USER_LANG", "ru_RU", domain=".zakupki.gov.ru")
        session.cookies.set("_ym_uid", str(random.randint(1000000000, 9999999999)), domain=".zakupki.gov.ru")
        session.cookies.set("_ym_d", time.strftime("%Y%m%d"), domain=".zakupki.gov.ru")
        session.cookies.set("JSESSIONID", "".join(random.choices(string.ascii_uppercase + string.digits, k=32)), domain=".zakupki.gov.ru")

        session.verify = False
        return session

    # ========== v6.8.6: ПУБЛИЧНЫЙ МЕТОД ==========
    def update_session_headers(self, session, user_agent: str, platform: str):
        """Обновляет заголовки сессии (публичный метод)."""
        sec_ch_ua = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'

        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Referer": "https://zakupki.gov.ru/",
                "sec-ch-ua": sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": f'"{platform}"',
            }
        )

    # Обратная совместимость (deprecated, будет удалено в v7.0)
    _update_headers = update_session_headers
    _update_session_headers = update_session_headers

    def get_session(self, index: int = 0) -> Any:
        """Получает сессию из пула."""
        if not self._sessions:
            return self._create_session()
        return self._sessions[index % len(self._sessions)]

    def get_primary_session(self) -> Any:
        """Получает основную сессию (индекс 0)."""
        return self.get_session(0)

    def rotate_user_agent(self, session_index: int = 0):
        """Меняет User-Agent для сессии."""
        session = self.get_session(session_index)
        user_agent = get_random_user_agent()
        platform = get_platform_from_ua(user_agent)
        self.update_session_headers(session, user_agent, platform)
        logger.debug(f"🎭 User-Agent изменён для сессии {session_index}")

    def calculate_delay(self) -> float:
        """Рассчитывает задержку с учётом 429."""
        if self._consecutive_429 > 0:
            delay = self._base_delay * (2 ** (self._consecutive_429 - 1))
            delay = min(delay, 300)
            logger.warning(f" Backoff: {delay}с (429 x{self._consecutive_429})")
            return delay
        return random.uniform(*self.REQUEST_DELAY)

    def handle_429(self):
        """Обрабатывает 429 (Too Many Requests)."""
        self._consecutive_429 += 1
        self.rotate_user_agent()
        delay = self.calculate_delay()
        logger.warning(f"🚫 429! Ждём {delay}с...")
        time.sleep(delay)

    def reset_429_counter(self):
        """Сбрасывает счётчик 429."""
        if self._consecutive_429 > 0:
            logger.info(f"✅ Сброс 429 (было: {self._consecutive_429})")
            self._consecutive_429 = 0

    def make_request(
        self,
        url: str,
        session_index: int = 0,
        timeout: int = None,  # Теперь можно переопределять глобальный таймаут
        max_retries: int = 3,
        headers: dict = None,
        allow_redirects: bool = True,
    ) -> Optional[Any]:
        session = self.get_session(session_index)

        # Используем переданный таймаут или глобальные настройки
        effective_timeout = timeout or self.TOTAL_TIMEOUT

        for attempt in range(max_retries):
            delay = self.calculate_delay()
            if attempt > 0:
                logger.info(
                    f"  ⏳ Попытка {attempt + 1}/{max_retries}, задержка {delay:.1f}с..."
                )
            time.sleep(delay)

            try:
                request_headers = headers or {}

                # ИСПОЛЬЗУЕМ КОРТЕЖ ТАЙМАУТОВ (connect, read)
                response = session.get(
                    url,
                    timeout=(self.CONNECT_TIMEOUT, effective_timeout),
                    headers=request_headers,
                    allow_redirects=allow_redirects,
                )

                if response.status_code == 429:
                    self.handle_429()
                    continue

                if response.status_code == 200:
                    self.reset_429_counter()
                    return response

                logger.warning(f"  ⚠️ Статус {response.status_code}: {url[:100]}...")

            except Exception as e:
                error_type = type(e).__name__
                logger.error(f"  ❌ Ошибка запроса ({error_type}): {str(e)[:200]}")

                # Для таймаутов делаем дополнительную паузу
                if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                    time.sleep(10)
                else:
                    time.sleep(5)

        return None

    def close_all(self):
        """Закрывает все сессии."""
        for i, session in enumerate(self._sessions):
            try:
                session.close()
                logger.debug(f"Сессия {i} закрыта")
            except Exception as e:
                logger.warning(f"Ошибка закрытия сессии {i}: {e}")
        self._sessions = []


# Глобальный инстанс
_session_manager = None


def get_session_manager(
    pool_size: int = 3, proxy: Optional[str] = None
) -> HTTPSessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = HTTPSessionManager(pool_size=pool_size, proxy=proxy)
    return _session_manager


def get_session(index: int = 0) -> Any:
    """Удобная функция для получения сессии."""
    return get_session_manager().get_session(index)


def make_request(
    url: str, timeout: int = None, max_retries: int = 3, headers: dict = None
) -> Optional[Any]:
    """Удобная функция для выполнения запроса."""
    return get_session_manager().make_request(
        url, timeout=timeout, max_retries=max_retries, headers=headers
    )
