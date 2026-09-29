from datetime import date, datetime, timedelta

from app import config, db
from app.domain import (EVERY_WEEK, clean_filter, is_eligible, now_utc, occurrences, strip_geometry,
                        today_msk, window_status)

FEED_DAYS = 14

WINDOWS = """
    SELECT w.*, v.name AS venue, v.address, COALESCE(w.image_url, v.image_url) AS cover_url
    FROM windows w LEFT JOIN venues v ON v.id = w.venue_id
"""
# то же, но с отметкой «в избранном» для конкретного пользователя
MY_WINDOWS = """
    SELECT w.*, v.name AS venue, v.address, COALESCE(w.image_url, v.image_url) AS cover_url,
           rm.user_id IS NOT NULL AS reminded
    FROM windows w
    LEFT JOIN venues v ON v.id = w.venue_id
    LEFT JOIN reminders rm ON rm.window_id = w.id AND rm.user_id = $1
"""
RULES = """
    SELECT r.*, v.name AS venue, v.address, v.district, v.url AS venue_url, v.image_url AS cover_url
    FROM benefit_rules r JOIN venues v ON v.id = r.venue_id
"""


def window_view(row, moment: datetime, reminded: bool = False) -> dict:
    left, width = strip_geometry(row, moment)
    return dict(row) | {
        "key": f"w{row['id']}",
        "kind": "registration",
        "status": window_status(row, moment),
        # проценты уезжают в data-атрибуты, размеры полосы проставляет app.js
        "strip_left": left,
        "strip_width": width,
        "reminded": reminded,
    }


def benefit_view(row, day: date) -> dict:
    return dict(row) | {
        "key": f"b{row['id']}:{day}",
        "kind": "benefit",
        "date": day,
        "topic": "museum",
        "eligibility": list(row["categories"]),
    }


async def upsert_user(user_id: int, first_name: str | None) -> dict:
    return dict(await db.row(
        """INSERT INTO users (id, first_name) VALUES ($1, $2)
           ON CONFLICT (id) DO UPDATE SET first_name = COALESCE(EXCLUDED.first_name, users.first_name), blocked = FALSE
           RETURNING *""",
        user_id, first_name,
    ))


# главные администраторы заданы в .env, редакторов они выдают через админку
def is_chief(user: dict) -> bool:
    return user["id"] in config.ADMIN_IDS


def is_staff(user: dict) -> bool:
    return is_chief(user) or user.get("role") == "editor"


async def list_editors() -> list[dict]:
    return await db.rows("SELECT id, first_name FROM users WHERE role = 'editor' ORDER BY first_name NULLS LAST, id")


async def set_role(user_id: int, editor: bool) -> None:
    await db.run(
        """INSERT INTO users (id, role) VALUES ($1,$2)
           ON CONFLICT (id) DO UPDATE SET role = EXCLUDED.role""",
        user_id, "editor" if editor else "user",
    )


async def get_user(user_id: int) -> dict | None:
    found = await db.row("SELECT * FROM users WHERE id = $1", user_id)
    return dict(found) if found else None


async def set_category(user_id: int, category: str | None) -> None:
    await db.run("UPDATE users SET category = $2, onboarded = TRUE WHERE id = $1", user_id, category)


async def build_feed(user: dict, flt: dict | None = None) -> dict:
    flt = flt or clean_filter()
    moment, start = now_utc(), today_msk()
    kinds, topics, districts, formats = flt["kinds"], flt["topics"], flt["districts"], flt["formats"]

    saved = {
        (row["rule_id"], row["day"])
        for row in await db.rows("SELECT rule_id, day FROM benefit_saves WHERE user_id = $1", user["id"])
    }

    benefits = []
    # льготные дни всегда офлайн, поэтому при выборе «онлайн» их не показываем
    if (not kinds or "benefit" in kinds) and (not topics or "museum" in topics) and (not formats or "offline" in formats):
        for row in await db.rows(RULES + "WHERE ($1::text[] = '{}' OR v.district = ANY($1))", districts):
            # без категории (нажали «Пропустить») показываем все льготные дни
            if not user["category"] or is_eligible(row["categories"], user["category"]):
                for day in occurrences(row["weekday"], row["nth"], start, FEED_DAYS):
                    benefits.append(benefit_view(row, day) | {"reminded": (row["id"], day) in saved})
        benefits.sort(key=lambda item: item["date"])

    registrations = []
    if not kinds or "registration" in kinds:
        rows = await db.rows(
            MY_WINDOWS + """WHERE w.starts_at > $2 AND (w.demo_for IS NULL OR w.demo_for = $1)
                           AND ($3::text[] = '{}' OR w.topic = ANY($3))
                           AND ($4::text[] = '{}' OR w.district = ANY($4))
                           AND ($5::text[] = '{}' OR w.format = ANY($5))
                         ORDER BY LEAST(GREATEST(w.reg_opens_at, $2), w.reg_closes_at)""",
            user["id"], moment, topics, districts, formats,
        )
        registrations = [
            window_view(r, moment, r["reminded"])
            for r in rows
            if not user["category"] or is_eligible(r["eligibility"], user["category"])
        ]

    return {"benefits": benefits, "registrations": registrations, "filter": flt}


async def get_window(window_id: int, user: dict) -> dict | None:
    found = await db.row(
        MY_WINDOWS.replace("$1", "$2") + "WHERE w.id = $1 AND (w.demo_for IS NULL OR w.demo_for = $2)",
        window_id, user["id"],
    )
    if not found:
        return None
    return window_view(found, now_utc(), found["reminded"]) | {
        "eligible_for_you": is_eligible(found["eligibility"], user["category"])
    }


async def get_benefit(rule_id: int, day: str | None, user: dict) -> dict | None:
    found = await db.row(RULES + "WHERE r.id = $1", rule_id)
    if not found:
        return None
    upcoming = occurrences(found["weekday"], found["nth"], today_msk(), 62)
    chosen = next((d for d in upcoming if str(d) == day), upcoming[0])
    saved = await db.value(
        "SELECT 1 FROM benefit_saves WHERE user_id = $1 AND rule_id = $2 AND day = $3",
        user["id"], rule_id, chosen,
    )
    return benefit_view(found, chosen) | {
        "next_dates": [d for d in upcoming[:3] if d != chosen],
        "eligible_for_you": is_eligible(found["categories"], user["category"]),
        "reminded": bool(saved),
    }


async def set_benefit_save(user_id: int, rule_id: int, day: date, on: bool) -> None:
    if on:
        await db.run(
            """INSERT INTO benefit_saves (user_id, rule_id, day) VALUES ($1, $2, $3)
               ON CONFLICT DO NOTHING""",
            user_id, rule_id, day,
        )
    else:
        await db.run(
            "DELETE FROM benefit_saves WHERE user_id = $1 AND rule_id = $2 AND day = $3",
            user_id, rule_id, day,
        )


async def list_benefit_saves(user_id: int) -> list[dict]:
    rows = await db.rows(
        RULES.replace("SELECT r.*", "SELECT r.*, s.day AS saved_day")
        + "JOIN benefit_saves s ON s.rule_id = r.id WHERE s.user_id = $1 AND s.day >= $2 ORDER BY s.day",
        user_id, today_msk(),
    )
    return [benefit_view(row, row["saved_day"]) | {"reminded": True} for row in rows]


async def set_reminder(user_id: int, window_id: int, on: bool) -> None:
    if on:
        await db.run("INSERT INTO reminders VALUES ($1,$2) ON CONFLICT DO NOTHING", user_id, window_id)
    else:
        await db.run("DELETE FROM reminders WHERE user_id = $1 AND window_id = $2", user_id, window_id)


# события, на которые пользователь откликнулся: предстоящие или уже прошедшие
async def list_reminders(user_id: int, past: bool = False) -> list[dict]:
    moment = now_utc()
    where, order = ("<=", " DESC") if past else (">", "")
    rows = await db.rows(
        f"""SELECT w.*, v.name AS venue, v.address, COALESCE(w.image_url, v.image_url) AS cover_url, rm.muted
            FROM windows w
            LEFT JOIN venues v ON v.id = w.venue_id
            JOIN reminders rm ON rm.window_id = w.id
            WHERE rm.user_id = $1 AND w.starts_at {where} $2
            ORDER BY w.starts_at{order}""",
        user_id, moment,
    )
    return [window_view(r, moment, True) for r in rows]


# уведомления по событию можно выключить, не убирая его из списка
async def mute_reminder(user_id: int, window_id: int, muted: bool) -> None:
    await db.run("UPDATE reminders SET muted = $3 WHERE user_id = $1 AND window_id = $2",
                 user_id, window_id, muted)


async def list_subscriptions(user_id: int) -> list[dict]:
    return [dict(r) for r in await db.rows("SELECT id, filter FROM subscriptions WHERE user_id = $1 ORDER BY id DESC", user_id)]


async def add_subscription(user_id: int, flt: dict) -> None:
    if await db.value("SELECT count(*) FROM subscriptions WHERE user_id = $1", user_id) >= 10:
        raise ValueError("Можно сохранить не больше 10 подписок")
    await db.run("INSERT INTO subscriptions (user_id, filter) VALUES ($1,$2) ON CONFLICT DO NOTHING", user_id, flt)


async def delete_subscription(user_id: int, sub_id: int) -> None:
    await db.run("DELETE FROM subscriptions WHERE user_id = $1 AND id = $2", user_id, sub_id)


async def create_demo_window(user_id: int) -> None:
    await db.run("DELETE FROM windows WHERE demo_for = $1", user_id)
    opens = now_utc() + timedelta(seconds=config.DEMO_DELAY)
    window_id = await db.value(
        """INSERT INTO windows (title, topic, district, starts_at, reg_opens_at, reg_closes_at, conditions,
                                source_name, demo_for)
           VALUES ('Демо: бесплатная экскурсия по Замоскворечью', 'excursion', 'ЦАО', $1, $2, $3,
                   'Тестовое окно для проверки уведомлений. Реальной записи нет.', 'Демо-режим', $4)
           RETURNING id""",
        opens + timedelta(days=3), opens, opens + timedelta(hours=1), user_id,
    )
    await db.run("INSERT INTO reminders VALUES ($1,$2) ON CONFLICT DO NOTHING", user_id, window_id)


# окно, которое откроется через минуту и уйдёт всем подходящим подписчикам
async def create_probe_window() -> None:
    opens = now_utc() + timedelta(seconds=config.DEMO_DELAY)
    await db.run(
        """INSERT INTO windows (title, topic, district, starts_at, reg_opens_at, reg_closes_at, eligibility,
                                conditions, source_name, format)
           VALUES ('Проверка уведомлений: экскурсия по Замоскворечью', 'excursion', 'ЦАО', $1, $2, $3,
                   ARRAY['all'], 'Проверочное окно. Реальной записи нет, его можно удалить из админки.',
                   'Проверка уведомлений', 'offline')""",
        opens + timedelta(days=2), opens, opens + timedelta(hours=2),
    )


# льготный день на завтра: правило «каждую неделю» с завтрашним днём недели
async def create_probe_rule() -> None:
    tomorrow = today_msk() + timedelta(days=1)
    venue = await db.value("SELECT id FROM venues ORDER BY id LIMIT 1")
    await db.run(
        """INSERT INTO benefit_rules (venue_id, title, categories, weekday, nth, conditions, source_name)
           VALUES ($1, 'Проверка уведомлений: льготный день', ARRAY['all'], $2, $3,
                   'Проверочное правило. Его можно удалить из админки.', 'Проверка уведомлений')
           ON CONFLICT (venue_id, title) DO UPDATE SET weekday = EXCLUDED.weekday""",
        venue, (tomorrow.weekday() + 1) % 7, EVERY_WEEK,
    )


# кому уйдёт уведомление: избранное плюс подходящие подписки; пустой список в фильтре означает «любые»
async def recipients(kind: str, topic: str, district: str, eligibility: list[str], window_id: int | None = None,
                     event_format: str = "offline") -> list[int]:
    rows = await db.rows(
        """SELECT DISTINCT u.id FROM users u
           LEFT JOIN subscriptions s ON s.user_id = u.id
           LEFT JOIN reminders rm ON rm.user_id = u.id AND rm.window_id = $5
           WHERE NOT u.blocked AND (
                 (rm.window_id IS NOT NULL AND NOT rm.muted)
                 OR (s.id IS NOT NULL
                     AND ('all' = ANY($4::text[]) OR u.category = ANY($4::text[]))
                     AND (jsonb_array_length(s.filter->'kinds') = 0 OR s.filter->'kinds' ? $1)
                     AND (jsonb_array_length(s.filter->'topics') = 0 OR s.filter->'topics' ? $2)
                     AND (jsonb_array_length(s.filter->'districts') = 0 OR s.filter->'districts' ? $3)
                     AND (s.filter->'formats' IS NULL OR jsonb_array_length(s.filter->'formats') = 0
                          OR s.filter->'formats' ? $6)))""",
        kind, topic, district, eligibility, window_id, event_format,
    )
    return [r["id"] for r in rows]


# сколько человек сейчас получит уведомление о таком событии и сколько всего пользователей
async def reach_counts(item: dict | None) -> tuple[int, int]:
    total = await db.value("SELECT count(*) FROM users WHERE NOT blocked") or 0
    if item:
        users = await recipients("registration", item["topic"], item["district"], list(item["eligibility"]),
                                 item["id"], item["format"])
        return len(users), total
    return await db.value("SELECT count(DISTINCT user_id) FROM subscriptions") or 0, total


async def set_asking(user_id: int, on: bool) -> None:
    await db.run("UPDATE users SET asking = $2 WHERE id = $1", user_id, on)


# администратор нажал «Ответить»: запоминаем, кому пишем
async def set_replying(admin_id: int, target_id: int | None) -> None:
    await db.run("UPDATE users SET replying_to = $2 WHERE id = $1", admin_id, target_id)


async def block_user(user_id: int) -> None:
    await db.run("UPDATE users SET blocked = TRUE WHERE id = $1", user_id)


async def admin_stats() -> dict:
    moment = now_utc()
    return dict(await db.row(
        """SELECT (SELECT count(*) FROM windows WHERE demo_for IS NULL AND reg_opens_at <= $1 AND reg_closes_at > $1) AS windows_open,
                  (SELECT count(*) FROM windows WHERE demo_for IS NULL AND reg_opens_at > $1) AS windows_soon,
                  (SELECT count(DISTINCT user_id) FROM subscriptions) AS subscribers,
                  (SELECT count(*) FROM notifications WHERE sent_at > $2) AS notifications_week,
                  (SELECT count(*) FROM benefit_rules WHERE checked_at < $3) AS rules_stale""",
        moment, moment - timedelta(days=7), today_msk() - timedelta(days=90),
    ))


async def admin_windows(scope: str = "active") -> list[dict]:
    moment = now_utc()
    sign, order = ("<=", "DESC") if scope == "past" else (">", "ASC")
    rows = await db.rows(
        f"""SELECT w.*, v.name AS venue, v.address, COALESCE(w.image_url, v.image_url) AS cover_url,
                   count(DISTINCT rm.user_id) AS reminders_count,
                   count(DISTINCT n.user_id) AS notified_count
            FROM windows w
            LEFT JOIN venues v ON v.id = w.venue_id
            LEFT JOIN reminders rm ON rm.window_id = w.id
            LEFT JOIN notifications n ON n.key = 'reg_open:w' || w.id
            WHERE w.demo_for IS NULL AND w.starts_at {sign} $1
            GROUP BY w.id, v.name, v.address, v.image_url
            ORDER BY w.starts_at {order} LIMIT 200""",
        moment,
    )
    return [window_view(r, moment) for r in rows]


async def admin_window(window_id: int) -> dict | None:
    return next((w for w in await admin_windows("active") + await admin_windows("past") if w["id"] == window_id), None)


async def admin_rules() -> list[dict]:
    start = today_msk()
    rows = await db.rows(RULES + "ORDER BY v.name, r.title")
    return [dict(r) | {"next_date": next(iter(occurrences(r["weekday"], r["nth"], start, 62)), None)} for r in rows]


async def admin_rule(rule_id: int) -> dict | None:
    found = await db.row(RULES + "WHERE r.id = $1", rule_id)
    return dict(found) if found else None


async def list_venues() -> list[dict]:
    return [dict(r) for r in await db.rows("SELECT id, name, district FROM venues ORDER BY name")]


async def venue_id(data: dict) -> int | None:
    if data.get("venue_id"):
        return int(data["venue_id"])
    name = (data.get("venue_name") or "").strip()
    if not name:
        return None
    return await db.value(
        """INSERT INTO venues (name, district, address) VALUES ($1,$2,$3)
           ON CONFLICT (name) DO UPDATE SET address = COALESCE(EXCLUDED.address, venues.address) RETURNING id""",
        name, data.get("district"), data.get("venue_address"),
    )


# запись в журнал не должна ломать действие, поэтому ошибки только в лог
async def log_action(user: dict, action: str, entity: str, entity_id: int | None, title: str | None) -> None:
    try:
        await db.run(
            "INSERT INTO audit_log (user_id, action, entity, entity_id, title) VALUES ($1,$2,$3,$4,$5)",
            user["id"], action, entity, entity_id, (title or "")[:200] or None,
        )
    except Exception as exc:
        print(f"[audit] не записал {action} {entity}: {exc}")


async def audit_log(limit: int = 100) -> list[dict]:
    return [dict(r) for r in await db.rows(
        """SELECT a.*, u.first_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
           ORDER BY a.at DESC LIMIT $1""", limit)]


async def save_window(data: dict, window_id: int | None) -> int:
    return await db.upsert("windows", {
        "title": data["title"],
        "topic": data["topic"],
        "district": data["district"],
        "venue_id": await venue_id(data),
        "starts_at": data["starts_at"],
        "reg_opens_at": data["reg_opens_at"],
        "reg_closes_at": data["reg_closes_at"],
        "eligibility": data["eligibility"],
        "conditions": data["conditions"],
        "documents": data["documents"],
        "register_url": data["register_url"],
        "image_url": data["image_url"],
        "source_name": data["source_name"],
        "source_url": data["source_url"],
        "format": data["format"],
    }, window_id)


async def save_rule(data: dict, rule_id: int | None) -> int:
    return await db.upsert("benefit_rules", {
        "venue_id": await venue_id(data),
        "title": data["title"],
        "categories": data["categories"],
        "weekday": data["weekday"],
        "nth": data["nth"],
        "conditions": data["conditions"],
        "documents": data["documents"],
        "source_name": data["source_name"],
        "source_url": data["source_url"],
        "checked_at": data["checked_at"] or today_msk(),
    }, rule_id)


async def delete_window(window_id: int) -> None:
    await db.run("DELETE FROM windows WHERE id = $1 AND demo_for IS NULL", window_id)


async def delete_rule(rule_id: int) -> None:
    await db.run("DELETE FROM benefit_rules WHERE id = $1", rule_id)
