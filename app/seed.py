import json
from datetime import datetime, timedelta
from pathlib import Path

from app import db
from app.domain import MSK, now_utc, occurrences, today_msk

DATA = json.loads((Path(__file__).parent / "seed_data.json").read_text(encoding="utf-8"))


# {"hours": N} — сдвиг от запуска, {"days": N, "hour": H} — день и час по Москве,
# {"rule": {"weekday": W, "nth": N, "hour": H}} — ближайшая дата по правилу повторения
def _moment(spec: dict, start: datetime) -> datetime:
    if "hours" in spec:
        return start + timedelta(hours=spec["hours"])
    if "rule" in spec:
        rule = spec["rule"]
        dates = occurrences(rule["weekday"], rule["nth"], today_msk(start), 70)
        day, hour = dates[0], rule.get("hour", 12)
    else:
        day, hour = today_msk(start) + timedelta(days=spec["days"]), spec.get("hour", 12)
    return datetime(day.year, day.month, day.day, hour, tzinfo=MSK)


async def seed_if_empty() -> bool:
    if await db.value("SELECT count(*) FROM windows"):
        return False

    start = now_utc()
    await db.run_many(
        """INSERT INTO venues (name, district, address, url, image_url) VALUES ($1,$2,$3,$4,$5)
           ON CONFLICT (name) DO NOTHING""",
        [(v["name"], v["district"], v.get("address"), v.get("url"), v.get("image_url")) for v in DATA["venues"]],
    )
    venues = {r["name"]: r["id"] for r in await db.rows("SELECT id, name FROM venues")}

    await db.run_many(
        """INSERT INTO benefit_rules (venue_id, title, categories, weekday, nth, conditions, documents,
                                      source_name, source_url, is_model_data)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (venue_id, title) DO NOTHING""",
        [
            (venues[r["venue"]], r["title"], r["categories"], r["weekday"], r["nth"], r.get("conditions"),
             r.get("documents", []), r["source_name"], r.get("source_url"), r.get("is_model_data", True))
            for r in DATA["benefit_rules"]
        ],
    )

    await db.run_many(
        """INSERT INTO windows (title, topic, district, venue_id, starts_at, reg_opens_at, reg_closes_at,
                                eligibility, conditions, documents, register_url, source_name, source_url, format)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
        [
            (w["title"], w["topic"], w["district"], venues[w["venue"]], _moment(w["starts"], start),
             _moment(w["reg_opens"], start), _moment(w["reg_closes"], start), w["eligibility"],
             w.get("conditions"), w.get("documents", []), w.get("register_url"), w["source_name"], w.get("source_url"),
             w.get("format", "offline"))
            for w in DATA["windows"]
        ],
    )
    print("[seed] загружены начальные данные")
    return True
