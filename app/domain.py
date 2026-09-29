# Доменные правила: категории, темы, льготные дни, состояние окна записи.
from datetime import date, datetime, timedelta, timezone

from app import config

MSK = timezone(timedelta(minutes=config.TZ_OFFSET_MIN))

# (id, полное название, короткое, подсказка)
CATEGORIES = [
    ("student", "Студент вуза или колледжа", "Студент", "Бесплатные дни в музеях, лекции для студентов"),
    ("schoolkid", "Школьник", "Школьник", "Детские дни в музеях, квесты и экскурсии"),
    ("preschool", "Ребёнок до 7 лет", "Дошкольник", "Бесплатный вход в большинство музеев"),
    ("large_family", "Многодетная семья", "Многодетная семья", "Льготы для всей семьи"),
    ("pensioner", "Пенсионер", "Пенсионер", "Бесплатные дни и экскурсии для старшего возраста"),
    ("disability", "Человек с инвалидностью", "Инвалидность", "Бесплатный вход и доступная среда, часто с сопровождающим"),
    ("veteran", "Ветеран боевых действий или член семьи", "Ветеран", "Льготы для участников СВО, ветеранов и их семей"),
    ("orphan", "Сирота или под опекой", "Сирота", "Бесплатный вход и городские программы"),
    ("teacher", "Педагог", "Педагог", "Бесплатные дни для учителей и преподавателей"),
    ("none", "Льгот нет", "Без льгот", "Покажем события, открытые для всех"),
]

TOPICS = [
    ("excursion", "Экскурсии"),
    ("museum", "Музеи"),
    ("lecture", "Лекции"),
    ("masterclass", "Мастер-классы"),
]
FORMATS = [("offline", "Оффлайн"), ("online", "Онлайн")]
FORMAT_IDS = {f[0] for f in FORMATS}

DISTRICTS = ["ЦАО", "САО", "СВАО", "ВАО", "ЮВАО", "ЮАО", "ЮЗАО", "ЗАО", "СЗАО"]

# как задаётся повторение льготного дня
NTH_OPTIONS = [
    (1, "Первый"), (2, "Второй"), (3, "Третий"), (4, "Четвёртый"), (-1, "Последний"),
    (WEEK_RULE := 0, "Музейная неделя"), (EVERY_WEEK := 9, "Каждую неделю"),
]
KINDS = ["benefit", "registration"]

CATEGORY_IDS = {c[0] for c in CATEGORIES}
TOPIC_IDS = {t[0] for t in TOPICS}
WEEKDAYS = ["воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"]
_ORD = {
    "m": {1: "Первый", 2: "Второй", 3: "Третий", 4: "Четвёртый", -1: "Последний"},
    "f": {1: "Первая", 2: "Вторая", 3: "Третья", 4: "Четвёртая", -1: "Последняя"},
    "n": {1: "Первое", 2: "Второе", 3: "Третье", 4: "Четвёртое", -1: "Последнее"},
}
_GENDER = ["n", "m", "m", "f", "m", "f", "f"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_msk(moment: datetime | None = None) -> date:
    return (moment or now_utc()).astimezone(MSK).date()


def category_label(cid: str | None, short: bool = True) -> str:
    for key, full, brief, _ in CATEGORIES:
        if key == cid:
            return brief if short else full
    return "Не выбрана"


def topic_label(tid: str | None) -> str:
    return dict(TOPICS).get(tid or "", "")


# «Последний вторник месяца», «Каждый четверг», «Московская музейная неделя, пятница».
def rule_text(weekday: int, nth: int) -> str:
    name = WEEKDAYS[weekday % 7]
    if nth == EVERY_WEEK:
        return f"Кажд{'ое' if weekday == 0 else 'ую' if weekday in (3, 5, 6) else 'ый'} {name}"
    if nth == WEEK_RULE:
        return f"Московская музейная неделя, {name}"
    return f"{_ORD[_GENDER[weekday % 7]][nth]} {name} месяца"


# понедельник третьей календарной недели месяца; первая неделя — та, в которую попало 1-е число
def week_three(day: date) -> date:
    first = day.replace(day=1)
    return first - timedelta(days=first.weekday()) + timedelta(days=14)


def matches_rule(day: date, weekday: int, nth: int) -> bool:
    if (day.weekday() + 1) % 7 != weekday:
        return False
    if nth == EVERY_WEEK:
        return True
    if nth == WEEK_RULE:
        return day == week_three(day) + timedelta(days=(weekday - 1) % 7)
    if nth == -1:
        return (day + timedelta(days=7)).month != day.month
    return (day.day - 1) // 7 + 1 == nth


def occurrences(weekday: int, nth: int, start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days) if matches_rule(start + timedelta(days=i), weekday, nth)]


def is_eligible(eligibility, category: str | None) -> bool:
    if not eligibility or "all" in eligibility:
        return True
    return bool(category) and category in eligibility


# soon / open / closed / past для окна записи.
def window_status(row, moment: datetime | None = None) -> str:
    moment = moment or now_utc()
    if moment < row["reg_opens_at"]:
        return "soon"
    if moment < row["reg_closes_at"]:
        return "open"
    if moment < row["starts_at"]:
        return "closed"
    return "past"


# «через 2 д 4 ч» до указанного момента.
# Число и единица склеены неразрывным пробелом, чтобы перенос не оставлял строку с «ч» или «мин».
def human_delta(target: datetime, moment: datetime | None = None) -> str:
    seconds = (target - (moment or now_utc())).total_seconds()
    if seconds <= 0:
        return "сейчас"
    minutes = int(seconds // 60)
    if minutes < 60:
        parts = (str(max(minutes, 1)), "мин")
    else:
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            parts = (str(hours), "ч", str(minutes), "мин") if minutes else (str(hours), "ч")
        else:
            days, hours = divmod(hours, 24)
            parts = (str(days), "д", str(hours), "ч") if hours else (str(days), "д")
    return "через\u00a0" + "\u00a0".join(parts)


# Текст и тон подписи под полосой окна записи.
def window_label(row, moment: datetime | None = None) -> tuple[str, str]:
    status = window_status(row, moment)
    if status == "soon":
        return "soon", f"Запись откроется {human_delta(row['reg_opens_at'], moment)}"
    if status == "open":
        return "open", f"Запись открыта, закроется {human_delta(row['reg_closes_at'], moment)}"
    return "closed", "Запись закрыта"


def clean_filter(topics=None, districts=None, kinds=None, formats=None) -> dict:
    def pick(values, allowed):
        values = values or []
        if isinstance(values, str):
            values = values.split(",")
        return sorted({v for v in values if v in allowed})

    return {
        "topics": pick(topics, TOPIC_IDS),
        "districts": pick(districts, set(DISTRICTS)),
        "kinds": pick(kinds, set(KINDS)),
        "formats": pick(formats, FORMAT_IDS),
    }


def describe_filter(flt: dict) -> str:
    kinds = flt.get("kinds") or []
    parts = ["Льготные дни" if kinds == ["benefit"] else "Запись на события" if kinds == ["registration"] else "Льготы и запись"]
    parts.append(", ".join(topic_label(t).lower() for t in flt["topics"]) if flt.get("topics") else "любые темы")
    parts.append(", ".join(flt["districts"]) if flt.get("districts") else "вся Москва")
    if flt.get("formats"):
        parts.append(", ".join(dict(FORMATS)[f].lower() for f in flt["formats"]))
    return "; ".join(parts)


# Отступ и ширина отрезка «запись открыта» в процентах от полосы.
def strip_geometry(row, moment: datetime | None = None) -> tuple[float, float]:
    moment = moment or now_utc()
    start = min(moment, row["reg_opens_at"])
    end = max(row["starts_at"], row["reg_closes_at"])
    span = max((end - start).total_seconds(), 1)
    left = (row["reg_opens_at"] - start).total_seconds() / span * 100
    width = (row["reg_closes_at"] - row["reg_opens_at"]).total_seconds() / span * 100
    return round(left, 2), round(max(width, 3), 2)


def format_label(fid: str | None) -> str:
    return dict(FORMATS).get(fid or "", "")
