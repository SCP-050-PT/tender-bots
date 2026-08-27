#!/usr/bin/env python3
"""
scheduler.py
Планировщик запуска ИИ-агента каждые 4 часа.
+ Health-check сервер
+ Автоперезапуск при краше
+ Telegram-уведомления об ошибках

Запуск: python scheduler.py
Остановка: Ctrl+C
"""

import subprocess
import sys
import time
import signal
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

# === НАСТРОЙКИ ===
INTERVAL_HOURS = 4  # Интервал между запусками
MAX_RESULTS = 10  # Максимум тендеров за запуск
MAX_CONSECUTIVE_FAILURES = 3  # Максимум подряд идущих ошибок
RESTART_DELAY_SECONDS = 60  # Задержка перед перезапуском после краша
HEALTH_PORT = 8080  # Порт health-check сервера
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Логирование планировщика
logger.add(
    LOG_DIR / "scheduler.log",
    rotation="10 MB",
    retention="30 days",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
)
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
)

# Глобальный флаг для graceful shutdown
running = True
consecutive_failures = 0


def signal_handler(signum, frame):
    global running
    logger.info(f"🛑 Получен сигнал {signum}, завершение...")
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def run_agent():
    """Запускает ИИ-агента и возвращает код возврата."""
    global consecutive_failures

    logger.info("=" * 60)
    logger.info("🚀 Запуск ИИ-агента...")
    logger.info("=" * 60)

    start_time = datetime.now()

    # v7.2.2: Проверка лимитов ДО запуска
    try:
        from core.daily_limiter import DailyLimiter

        limiter = DailyLimiter()
        can_run, reason = limiter.can_run()
        if not can_run:
            logger.info(f"⏭️  Пропуск запуска: {reason}")
            # Обновляем health state
            try:
                from core.health_server import get_health_state

                get_health_state().update(status="skipped", tenders_processed=0)
            except ImportError:
                pass
            return 0
    except ImportError as e:
        logger.error(f"❌ Не удалось импортировать DailyLimiter: {e}")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--analyze",
                "--max-results",
                str(MAX_RESULTS),
            ],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=600,  # 10 минут максимум
            encoding="utf-8",
            errors="replace",
        )

        elapsed = datetime.now() - start_time

        if result.returncode == 0:
            logger.info(f"✅ Агент завершён успешно за {elapsed.total_seconds():.0f}с")
            consecutive_failures = 0  # Сброс счётчика ошибок

            # Подсчёт тендеров из stdout
            tenders_count = result.stdout.count("РЕЗУЛЬТАТ:")

            # Обновляем health state
            try:
                from core.health_server import get_health_state

                get_health_state().update(
                    status="success",
                    tenders_processed=tenders_count,
                )
            except ImportError:
                pass

        else:
            consecutive_failures += 1
            logger.error(
                f"❌ Агент завершился с кодом {result.returncode} "
                f"(подряд ошибок: {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})"
            )
            if result.stderr:
                logger.error(f"STDERR: ...{result.stderr[-500:]}")

            # Обновляем health state
            try:
                from core.health_server import get_health_state

                get_health_state().update(
                    status="error",
                    tenders_processed=0,
                    error=f"Код возврата: {result.returncode}",
                )
            except ImportError:
                pass

        # Сохраняем stdout в отдельный лог
        if result.stdout:
            run_log = LOG_DIR / f"run_{start_time.strftime('%Y%m%d_%H%M%S')}.log"
            with open(run_log, "w", encoding="utf-8") as f:
                f.write(result.stdout)
            logger.info(f"💾 Лог запуска сохранён: {run_log.name}")

        return result.returncode

    except subprocess.TimeoutExpired:
        consecutive_failures += 1
        elapsed = datetime.now() - start_time
        logger.error(
            f"⏰ Таймаут! Агент не завершился за {elapsed.total_seconds():.0f}с "
            f"(подряд ошибок: {consecutive_failures}/{MAX_CONSECUTITIVE_FAILURES})"
        )

        try:
            from core.health_server import get_health_state

            get_health_state().update(
                status="timeout",
                error=f"Таймаут после {elapsed.total_seconds():.0f}с",
            )
        except ImportError:
            pass

        return -1

    except Exception as e:
        consecutive_failures += 1
        logger.error(
            f"💥 Ошибка запуска агента: {e} "
            f"(подряд ошибок: {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})"
        )

        try:
            from core.health_server import get_health_state

            get_health_state().update(
                status="crash",
                error=str(e)[:200],
            )
        except ImportError:
            pass

        return -1


def main():
    global running, consecutive_failures

    logger.info("🤖 Планировщик ИИ-агента запущен")
    logger.info(f"⏱️  Интервал: каждые {INTERVAL_HOURS} часов")
    logger.info(f"📊 Максимум тендеров за запуск: {MAX_RESULTS}")
    logger.info(f"🔄 Максимум подряд ошибок: {MAX_CONSECUTIVE_FAILURES}")
    logger.info(f"🏥 Health-check порт: {HEALTH_PORT}")
    logger.info(f"🛑 Остановка: Ctrl+C")

    # v7.2.2: Запуск health-check сервера
    try:
        from core.health_server import start_health_server, get_health_state

        start_health_server(port=HEALTH_PORT)
        get_health_state().update(status="started")
    except ImportError:
        logger.warning("⚠️ HealthServer не доступен, пропускаем")
    except Exception as e:
        logger.warning(f"⚠️ HealthServer ошибка: {e}")

    # Первый запуск — сразу
    # v7.2.2: Привязка к ровным часам (0, 4, 8, 12, 16, 20)
    now = datetime.now()
    # Находим ближайший следующий слот
    next_slot_hour = ((now.hour // INTERVAL_HOURS) + 1) * INTERVAL_HOURS
    if next_slot_hour >= 24:
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )
    else:
        next_run = now.replace(hour=next_slot_hour, minute=0, second=0, microsecond=0)

    # Если сейчас ровно на слоте — запускаем сразу
    if now.hour % INTERVAL_HOURS == 0 and now.minute < 5:
        next_run = now

    logger.info(
        f"⏰ Первый запуск запланирован на: {next_run.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    while running:
        now = datetime.now()

        if now >= next_run:
            return_code = run_agent()

            # === АВТОПЕРЕЗАПУСК ПРИ КРАШЕ ===
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    f"🔴 {consecutive_failures} подряд ошибок! "
                    f"Пауза {RESTART_DELAY_SECONDS}с перед следующим запуском..."
                )
                # Пауза перед следующим запуском (не выходим из цикла!)
                for _ in range(RESTART_DELAY_SECONDS):
                    if not running:
                        break
                    time.sleep(1)
                consecutive_failures = 0  # Сброс после паузы
                logger.info("🔄 Сброс счётчика ошибок, продолжаем работу")

            next_run = now + timedelta(hours=INTERVAL_HOURS)
            logger.info(
                f"⏭️  Следующий запуск: {next_run.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        # Спим 60 секунд между проверками
        for _ in range(60):
            if not running:
                break
            time.sleep(1)

    logger.info("👋 Планировщик остановлен")


if __name__ == "__main__":
    main()
