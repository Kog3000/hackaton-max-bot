"""Запуск всех трёх процессов внутри одного контейнера.

Нужен там, где хостинг собирает образ по Dockerfile и поднимает ровно один
контейнер. Локально и в docker compose этот модуль не используется: каждый
сервис там задаёт свою команду и перекрывает CMD из Dockerfile.

Порядок: сначала web — он создаёт схему и грузит начальные данные. Как только
он ответил на /health, поднимаются bot и worker.

Если упал web — выходим целиком, хостинг перезапустит контейнер. Если упал bot
или worker — поднимаем его заново с нарастающей паузой, а страницы продолжают
работать: мини-приложение важнее, чем немедленная остановка из-за неверного
токена.
"""

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from app import config

PORT = str(config.PORT)
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
HEALTH_TIMEOUT_SEC = 180

RESTART_DELAYS_SEC = [5, 15, 30, 60, 120]  # пауза перед очередной попыткой поднять bot или worker

_children: dict[str, subprocess.Popen] = {}
_commands: dict[str, list[str]] = {}
_failures: dict[str, int] = {}
_stopping = False


def _start(name: str, argv: list[str]) -> None:
    print(f"[run] запускаю {name}", flush=True)
    _commands[name] = argv
    _children[name] = subprocess.Popen(argv)


def _stop_all() -> None:
    global _stopping
    _stopping = True
    for name, child in _children.items():
        if child.poll() is None:
            print(f"[run] останавливаю {name}", flush=True)
            child.terminate()
    for child in _children.values():
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()


def _on_signal(signum, _frame) -> None:
    print(f"[run] сигнал {signum}", flush=True)
    _stop_all()
    sys.exit(0)


# web поднимается не мгновенно: ждём, пока он создаст схему и ответит на /health
def _wait_for_web(web: subprocess.Popen) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if web.poll() is not None:
            print(f"[run] web не поднялся, код {web.returncode}", flush=True)
            return False
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=3) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    print(f"[run] web не ответил на {HEALTH_URL} за {HEALTH_TIMEOUT_SEC} с", flush=True)
    return False


def main() -> None:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    print(f"[run] сборка {config.BUILD}, порт {PORT}", flush=True)
    # все три процесса поднимаем тем же интерпретатором, что и себя: иначе
    # uvicorn может найтись в PATH от другого питона, без наших зависимостей
    _start("web", [
        sys.executable, "-m", "uvicorn", "app.web:app",
        "--host", "0.0.0.0", "--port", PORT, "--loop", "asyncio",
        "--proxy-headers", "--forwarded-allow-ips", "*",
    ])

    if not _wait_for_web(_children["web"]):
        _stop_all()
        raise SystemExit(1)
    print("[run] web отвечает, схема на месте", flush=True)

    if config.BOT_TOKEN:
        _start("bot", [sys.executable, "-m", "app.bot"])
        _start("worker", [sys.executable, "-m", "app.worker"])
    else:
        # без токена бот и уведомления невозможны, но страницы должны открываться
        print("[run] MAX_BOT_TOKEN не задан: bot и worker не запускаются", flush=True)

    while not _stopping:
        for name in list(_children):
            code = _children[name].poll()
            if code is None:
                continue
            if name == "web":
                print(f"[run] web завершился с кодом {code}, останавливаю контейнер", flush=True)
                _stop_all()
                raise SystemExit(code or 1)
            attempt = _failures.get(name, 0)
            delay = RESTART_DELAYS_SEC[min(attempt, len(RESTART_DELAYS_SEC) - 1)]
            _failures[name] = attempt + 1
            print(f"[run] {name} завершился с кодом {code}, подниму снова через {delay} с "
                  f"(попытка {attempt + 1})", flush=True)
            time.sleep(delay)
            if _stopping:
                return
            _start(name, _commands[name])
        time.sleep(2)


if __name__ == "__main__":
    main()
