# Настройки берутся из переменных окружения, секретов в коде нет.
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None or value == "" else value.lower() in {"1", "true", "yes"}


def _ids(name: str) -> set[int]:
    return {int(x) for x in (os.getenv(name) or "").replace(" ", "").split(",") if x.isdigit()}


# Пустая переменная — это «не задана», а не пустая строка: в панелях хостинга
# легко создать переменную и не заполнить её. Имя ищем без учёта регистра,
# потому что DATABASE_URL и database_url для панели одно и то же, а для
# os.getenv — нет.
def _str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        wanted = name.lower()
        for key, candidate in os.environ.items():
            if key.lower() == wanted:
                value = candidate
                break
    value = (value or "").strip()
    return value or default


# метка сборки: видно в логах, чтобы не гадать, какая версия запущена
BUILD = "2026-09-29.7"

BOT_TOKEN = _str("MAX_BOT_TOKEN")
API_BASE = _str("MAX_API_BASE")  # переопределяется только для локальной проверки
BOT_USERNAME = _str("MAX_BOT_USERNAME").lstrip("@")
DATABASE_URL = _str("DATABASE_URL", "postgres://aperio:aperio@localhost:5432/aperio")
# Корневой сертификат управляемой базы: текст PEM целиком или путь к файлу.
# Нужен, только если в строке подключения стоит sslmode=verify-ca или verify-full.
DATABASE_CA_CERT = _str("DATABASE_CA_CERT")
PORT = _int("PORT", 8080)
ADMIN_IDS = _ids("ADMIN_USER_IDS")
ALLOW_DEV_AUTH = _bool("ALLOW_DEV_AUTH")
INIT_DATA_TTL = _int("INIT_DATA_TTL_SEC", 3600)
SESSION_TTL = _int("SESSION_TTL_SEC", 86400)
ADMIN_SESSION_TTL = _int("ADMIN_SESSION_TTL_SEC", 7200)
DEMO_DELAY = _int("DEMO_DELAY_SEC", 60)
WORKER_INTERVAL = _int("WORKER_INTERVAL_SEC", 15)
SEED_ON_START = _bool("SEED_ON_START", True)
TZ_OFFSET_MIN = _int("TZ_OFFSET_MIN", 180)
