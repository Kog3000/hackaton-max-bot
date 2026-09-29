CREATE TABLE IF NOT EXISTS users (
  id          BIGINT PRIMARY KEY,
  first_name  TEXT,
  category    TEXT,
  onboarded   BOOLEAN NOT NULL DEFAULT FALSE,
  blocked     BOOLEAN NOT NULL DEFAULT FALSE,
  -- user — обычный пользователь, editor — доступ в админку, выданный главным администратором
  role        TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'editor')),
  -- человек вызвал /admin и сейчас пишет вопрос администратору
  asking      BOOLEAN NOT NULL DEFAULT FALSE,
  -- администратор нажал «Ответить» и пишет ответ этому пользователю
  replying_to BIGINT
);

CREATE TABLE IF NOT EXISTS venues (
  id        SERIAL PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  district  TEXT NOT NULL,
  address   TEXT,
  url       TEXT,
  image_url TEXT
);

CREATE TABLE IF NOT EXISTS windows (
  id            SERIAL PRIMARY KEY,
  title         TEXT NOT NULL,
  topic         TEXT NOT NULL,
  district      TEXT,
  venue_id      INT REFERENCES venues(id),
  external_id   TEXT UNIQUE,
  starts_at     TIMESTAMPTZ NOT NULL,
  reg_opens_at  TIMESTAMPTZ NOT NULL,
  reg_closes_at TIMESTAMPTZ NOT NULL,
  eligibility   TEXT[] NOT NULL DEFAULT '{all}',
  conditions    TEXT,
  documents     TEXT[] NOT NULL DEFAULT '{}',
  register_url  TEXT,
  format        TEXT NOT NULL DEFAULT 'offline',
  image_url     TEXT,
  source_name   TEXT NOT NULL,
  source_url    TEXT,
  checked_at    DATE NOT NULL DEFAULT current_date,
  is_model_data BOOLEAN NOT NULL DEFAULT TRUE,
  -- у демо-окна здесь id создателя: его видит только он
  demo_for      BIGINT,
  CHECK (reg_opens_at < reg_closes_at)
);
CREATE INDEX IF NOT EXISTS windows_reg_idx ON windows (reg_opens_at, reg_closes_at);

CREATE TABLE IF NOT EXISTS benefit_rules (
  id            SERIAL PRIMARY KEY,
  venue_id      INT NOT NULL REFERENCES venues(id),
  title         TEXT NOT NULL,
  categories    TEXT[] NOT NULL,
  -- weekday 0 = воскресенье; nth: 1..4 — n-й день недели месяца, -1 — последний,
  -- 0 — Московская музейная неделя (третья календарная неделя), 9 — каждую неделю
  weekday       SMALLINT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  nth           SMALLINT NOT NULL CHECK (nth IN (-1, 0, 1, 2, 3, 4, 9)),
  conditions    TEXT,
  documents     TEXT[] NOT NULL DEFAULT '{}',
  source_name   TEXT NOT NULL,
  source_url    TEXT,
  checked_at    DATE NOT NULL DEFAULT current_date,
  is_model_data BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE (venue_id, title)
);

-- кто и что менял в админке; название храним снимком, чтобы запись читалась после удаления
CREATE TABLE IF NOT EXISTS audit_log (
  id        SERIAL PRIMARY KEY,
  at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_id   BIGINT NOT NULL,
  action    TEXT NOT NULL,
  entity    TEXT NOT NULL,
  entity_id BIGINT,
  title     TEXT
);

CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC);

-- подписка = сохранённый фильтр каталога
CREATE TABLE IF NOT EXISTS subscriptions (
  id      SERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  filter  JSONB NOT NULL,
  UNIQUE (user_id, filter)
);

-- сердечко на льготном дне: конкретная дата правила, не окно записи
CREATE TABLE IF NOT EXISTS benefit_saves (
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rule_id INT NOT NULL REFERENCES benefit_rules(id) ON DELETE CASCADE,
  day     DATE NOT NULL,
  PRIMARY KEY (user_id, rule_id, day)
);

CREATE TABLE IF NOT EXISTS reminders (
  user_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  window_id INT NOT NULL REFERENCES windows(id) ON DELETE CASCADE,
  -- запись остаётся, но уведомления по ней выключены
  muted     BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (user_id, window_id)
);

-- ключ уведомления пишется до отправки и защищает от дублей
CREATE TABLE IF NOT EXISTS notifications (
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,
  sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);

ALTER TABLE windows ADD COLUMN IF NOT EXISTS format TEXT NOT NULL DEFAULT 'offline';

ALTER TABLE windows ADD COLUMN IF NOT EXISTS external_id TEXT;
ALTER TABLE windows ALTER COLUMN district DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS windows_external_idx ON windows (external_id);

ALTER TABLE benefit_rules DROP CONSTRAINT IF EXISTS benefit_rules_nth_check;
ALTER TABLE benefit_rules ADD CONSTRAINT benefit_rules_nth_check CHECK (nth IN (-1, 0, 1, 2, 3, 4, 9));

ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user';

ALTER TABLE users ADD COLUMN IF NOT EXISTS asking BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE reminders ADD COLUMN IF NOT EXISTS muted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS replying_to BIGINT;
