"""Импорт событий из согласованного JSON-фида партнёра.

Ожидаемый ответ: {"events": [{"external_id", "title", "topic", "district",
"starts_at", "reg_opens_at", "reg_closes_at", "source_name", "source_url"}] }.
Даты ISO-8601 должны включать часовой пояс. Неизвестные/неполные записи пропускаются.
"""
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp

from app import config, db


def _dt(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("не задана дата")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("дата должна включать часовой пояс")
    return parsed.astimezone(timezone.utc)


def _http_url(value: str | None, required: bool = False) -> str | None:
    if not value:
        if required:
            raise ValueError("не задан URL источника")
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("ссылки источника должны использовать HTTPS")
    return value


def normalize_event(item: dict) -> tuple:
    if not isinstance(item, dict):
        raise ValueError("событие должно быть объектом")
    external_id = str(item.get("external_id", "")).strip()
    title = str(item.get("title", "")).strip()
    topic = str(item.get("topic", "")).strip()
    district = str(item.get("district", "")).strip() or None
    source_name = str(item.get("source_name", "")).strip()
    source_url = _http_url(item.get("source_url"), required=True)
    if not external_id or not title or not topic or not source_name:
        raise ValueError("нужны external_id, title, topic и source_name")
    starts_at = _dt(item.get("starts_at"))
    reg_opens_at = _dt(item.get("reg_opens_at"))
    reg_closes_at = _dt(item.get("reg_closes_at"))
    if reg_opens_at >= reg_closes_at:
        raise ValueError("запись должна закрываться позже открытия")
    if len(title) > 200 or len(external_id) > 200 or len(source_name) > 300:
        raise ValueError("одно из текстовых полей слишком длинное")
    register_url = _http_url(item.get("register_url"))
    format_value = item.get("format", "offline")
    if format_value not in {"offline", "online"}:
        format_value = "offline"
    eligibility = item.get("eligibility") or ["all"]
    if not isinstance(eligibility, list) or not all(isinstance(x, str) for x in eligibility):
        raise ValueError("eligibility должен быть списком строк")
    documents = item.get("documents") or []
    if not isinstance(documents, list) or not all(isinstance(x, str) for x in documents):
        raise ValueError("documents должен быть списком строк")
    return (external_id, title, topic, district, starts_at, reg_opens_at, reg_closes_at,
            eligibility, str(item.get("conditions", ""))[:1000] or None,
            documents,
            register_url, source_name, source_url, format_value)


async def sync() -> tuple[int, int]:
    """Возвращает (сохранено, пропущено); исключение оставляет ошибку видимой в логах."""
    if not config.PARTNER_FEED_URL:
        return 0, 0
    _http_url(config.PARTNER_FEED_URL, required=True)
    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(config.PARTNER_FEED_URL, headers={"Accept": "application/json"}) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
    events = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise ValueError("JSON-фид должен быть массивом событий или объектом с полем events")
    saved = skipped = 0
    for event in events:
        try:
            args = normalize_event(event)
            await db.run(
                """INSERT INTO windows (external_id, title, topic, district, starts_at, reg_opens_at,
                       reg_closes_at, eligibility, conditions, documents, register_url, source_name,
                       source_url, format, checked_at, is_model_data)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,CURRENT_DATE,FALSE)
                   ON CONFLICT (external_id) DO UPDATE SET title=EXCLUDED.title, topic=EXCLUDED.topic,
                       district=EXCLUDED.district, starts_at=EXCLUDED.starts_at,
                       reg_opens_at=EXCLUDED.reg_opens_at, reg_closes_at=EXCLUDED.reg_closes_at,
                       eligibility=EXCLUDED.eligibility, conditions=EXCLUDED.conditions,
                       documents=EXCLUDED.documents, register_url=EXCLUDED.register_url,
                       source_name=EXCLUDED.source_name, source_url=EXCLUDED.source_url,
                       format=EXCLUDED.format, checked_at=CURRENT_DATE, is_model_data=FALSE""",
                *args,
            )
            saved += 1
        except (ValueError, TypeError, KeyError) as exc:
            skipped += 1
            print(f"[partner-feed] пропускаю некорректную запись: {exc}")
    print(f"[partner-feed] синхронизация завершена: сохранено {saved}, пропущено {skipped}")
    return saved, skipped
