-- IIG Reporter — схема PostgreSQL (веб, многопользовательский, одно агентство).
-- Применяется один раз при инициализации БД. Идемпотентна (IF NOT EXISTS).
-- Модель: один агентский Яндекс-токен/бот (глобально), пользователи по приглашению,
-- у клиента есть владелец; привязки/цели/история скоупятся ЧЕРЕЗ clients.owner.

-- Пользователи (заводит админ; публичной регистрации нет)
CREATE TABLE IF NOT EXISTS users (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    pass_hash  TEXT NOT NULL,
    name       TEXT,
    role       TEXT NOT NULL DEFAULT 'user',   -- 'admin' | 'user'
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Telegram-чаты, увиденные ботом (агентские, ключи глобально уникальны — один бот)
CREATE TABLE IF NOT EXISTS chats (
    chat_id    BIGINT PRIMARY KEY,
    type       TEXT,
    title      TEXT,
    username   TEXT,
    status     TEXT,   -- active | removed
    my_status  TEXT,   -- member | administrator | left | kicked ...
    added_at   TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);

-- Клиенты Яндекс.Директа (один общий пул агентства) + ВЛАДЕЛЕЦ (кому назначен)
CREATE TABLE IF NOT EXISTS clients (
    login       TEXT PRIMARY KEY,
    name        TEXT,
    goals       JSONB,          -- [{"id","name","type","active"}]
    attribution TEXT,
    source      TEXT,           -- yandex | config | manual
    owner       BIGINT REFERENCES users(id) ON DELETE SET NULL,  -- NULL = не назначен (общий пул)
    updated_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_clients_owner ON clients(owner);

-- Привязка чат→клиент (владельца наследует через clients.owner)
CREATE TABLE IF NOT EXISTS bindings (
    chat_id   BIGINT PRIMARY KEY,
    login     TEXT NOT NULL,
    confirmed INTEGER DEFAULT 1,
    bound_by  BIGINT,   -- telegram user id, подтвердивший /bind
    bound_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_bindings_login ON bindings(login);

-- История отправок
CREATE TABLE IF NOT EXISTS send_log (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    login       TEXT,
    chat_id     BIGINT,
    period_from TEXT,
    period_to   TEXT,
    status      TEXT,
    error       TEXT,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_send_log_login ON send_log(login);

-- Ключ-значение (offset бота и пр. скаляры) — агентское, глобальное
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
