import asyncio
import json
import re
import socket
import ssl as ssl_module
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import asyncpg

from app import config

_pool: asyncpg.Pool | None = None
VERIFYING_MODES = {"verify-ca", "verify-full"}


# Куда именно мы идём — без пароля. Чтобы в логах не гадать, какая строка подключения доехала.
def target() -> str:
    try:
        parts = urlsplit(config.DATABASE_URL)
    except ValueError:
        return "строка подключения не разбирается"
    if not parts.hostname:
        return "в строке подключения нет хоста"
    user = f"{parts.username}@" if parts.username else ""
    return f"{user}{parts.hostname}:{parts.port or 5432}{parts.path or ''}"


# Управляемые базы дают строку с sslmode=verify-full и отдельным корневым
# сертификатом. Без сертификата asyncpg ищет его в ~/.postgresql/root.crt,
# не находит и падает. Сертификат кладём в переменную DATABASE_CA_CERT —
# текстом PEM или путём к файлу — и подставляем в строку подключения.
def _ca_path() -> str | None:
    source = config.DATABASE_CA_CERT
    if not source:
        return None
    if "BEGIN CERTIFICATE" in source:
        handle = tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False)
        handle.write(source.replace("\\n", "\n"))
        handle.close()
        return handle.name
    return source if Path(source).is_file() else None


def prepare_dsn() -> str:
    dsn = config.DATABASE_URL.replace("postgres://", "postgresql://")
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    mode = (query.get("sslmode") or "").lower()
    if mode not in VERIFYING_MODES or query.get("sslrootcert"):
        return dsn

    path = _ca_path()
    if path:
        query["sslrootcert"] = path
        print(f"[db] проверяю сертификат базы по {path}", flush=True)
        return urlunsplit(parts._replace(query=urlencode(query)))

    raise RuntimeError(
        f"Строка подключения требует sslmode={mode}, то есть проверку сертификата базы, "
        "но самого сертификата нет. Либо положите корневой сертификат управляемой базы "
        "в переменную DATABASE_CA_CERT, либо замените в строке подключения "
        f"sslmode={mode} на sslmode=require — тогда соединение останется шифрованным, "
        "но сертификат проверяться не будет."
    )


def _ipv4(host: str) -> str | None:
    # Уже литеральный IPv4 — резолвить нечего. inet_aton принимает и «1»,
    # поэтому требуем четыре октета.
    if host.count(".") == 3:
        try:
            socket.inet_aton(host)
            return host
        except OSError:
            pass
    try:
        # Только A-запись. Именно этот вызов в контейнере на Timeweb
        # возвращает адрес сервиса db, когда getaddrinfo уже отвечает EAI_NONAME.
        return socket.gethostbyname(host)
    except OSError:
        return None


# Явные параметры поверх DSN. asyncpg режет netloc по первому «@», поэтому
# пароль с «@» он разбирает иначе, чем urllib: пользователя и пароль берём
# из urlsplit. Хост подменяем на IPv4, кроме sslmode=verify-*: сертификат
# выписан на имя, и проверка по IP не сойдётся.
def pool_endpoint(dsn: str) -> dict:
    parts = urlsplit(dsn)
    host = parts.hostname
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    mode = (query.get("sslmode") or "").lower()
    chosen = host
    if host and mode not in VERIFYING_MODES:
        ip = _ipv4(host)
        if ip:
            chosen = ip
    endpoint: dict = {}
    if chosen:
        endpoint["host"] = chosen
        endpoint["port"] = parts.port or 5432
    if parts.username:
        endpoint["user"] = unquote(parts.username)
    if parts.password is not None:
        endpoint["password"] = unquote(parts.password)
    database = unquote(parts.path[1:]) if parts.path.startswith("/") else ""
    if database:
        endpoint["database"] = database
    return endpoint


# Библиотечные сообщения про сертификаты малопонятны, переводим их в действие
def _ssl_hint(exc: ssl_module.SSLError) -> str:
    text = str(exc)
    if "PEM lib" in text or "PEM_read" in text:
        return ("сертификат в DATABASE_CA_CERT не читается. Нужен корневой сертификат базы "
                "целиком, вместе со строками BEGIN CERTIFICATE и END CERTIFICATE.")
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return (f"сертификат базы не прошёл проверку ({text}). Убедитесь, что в DATABASE_CA_CERT "
                "лежит корневой сертификат именно этой базы, а хост в строке подключения "
                "совпадает с тем, что выдал хостинг.")
    return f"не приняла защищённое соединение: {text}"


async def connect(retries: int = 30) -> asyncpg.Pool:
    global _pool
    dsn = prepare_dsn()
    hostname = urlsplit(dsn).hostname or ""
    printed = False
    printed_ip = False
    for attempt in range(1, retries + 1):
        endpoint = pool_endpoint(dsn)
        host = endpoint.get("host") or ""
        if not printed:
            extra = f", адрес {host}" if host and host != hostname else ""
            print(f"[db] подключаюсь к {target()}{extra}", flush=True)
            printed = True
            printed_ip = bool(extra)
        elif host and host != hostname and not printed_ip:
            print(f"[db] имя {hostname} открылось как {host}", flush=True)
            printed_ip = True
        try:
            _pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=10, init=_init_codecs, **endpoint
            )
            return _pool
        # SSLError — наследник OSError, поэтому ловим его раньше: ждать тут нечего,
        # от повторов сертификат не станет подходящим
        except ssl_module.SSLError as exc:
            raise RuntimeError(f"База {target()}: {_ssl_hint(exc)}") from exc
        except OSError as exc:
            print(f"[db] жду базу ({attempt}/{retries}): {exc}", flush=True)
            await asyncio.sleep(2)
        except asyncpg.PostgresError as exc:
            # база ответила и отказала: неверный пароль, нет такой базы —
            # ждать бесполезно, повторять нечего
            raise RuntimeError(f"База {target()} отказала: {exc}") from exc
    raise RuntimeError(f"База {target()} недоступна: за {retries * 2} с соединение так и не открылось")


# jsonb сразу приходит и уходит как обычный dict
async def _init_codecs(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def close() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def migrate() -> None:
    await _pool.execute((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))


async def rows(query: str, *args) -> list[asyncpg.Record]:
    return await _pool.fetch(query, *args)


async def row(query: str, *args) -> asyncpg.Record | None:
    return await _pool.fetchrow(query, *args)


async def value(query: str, *args):
    return await _pool.fetchval(query, *args)


async def run(query: str, *args) -> str:
    return await _pool.execute(query, *args)


async def run_many(query: str, args_list: list[tuple]) -> None:
    await _pool.executemany(query, args_list)


# в текст запроса попадают только имена столбцов, поэтому проверяем их форму
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


# вставка или правка одной строки: имя столбца и значение едут парой, перепутать порядок нечем
async def upsert(table: str, data: dict, row_id: int | None = None) -> int:
    names = list(data)
    if not _NAME.match(table) or not all(_NAME.match(name) for name in names):
        raise ValueError("недопустимое имя таблицы или столбца")
    values = list(data.values())
    if row_id:
        assignments = ", ".join(f"{name} = ${i}" for i, name in enumerate(names, start=2))
        await run(f"UPDATE {table} SET {assignments} WHERE id = $1", row_id, *values)
        return row_id
    slots = ", ".join(f"${i}" for i in range(1, len(names) + 1))
    return await value(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({slots}) RETURNING id", *values
    )
