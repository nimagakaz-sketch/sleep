"""
Модуль работы с базой данных для трекера сна ребенка.
Поддерживает:
1. Neon PostgreSQL (на базе asyncpg с connection pooling и поддержкой ветвления) при наличии DATABASE_URL.
2. Локальный SQLite (aiosqlite) в качестве запасного/автономного режима при отсутствии DATABASE_URL.
"""

import os
import secrets
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

import time_utils

# Переменные окружения Neon PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_UNPOOLED = os.getenv("DATABASE_URL_UNPOOLED")
DB_PATH = os.getenv("SQLITE_PATH", "sleep_tracker.db")

# Главный администратор бота (владелец)
SUPERADMIN_ID = 374188835

_pg_pool = None

def is_postgres() -> bool:
    """Проверяет, настроена ли внешняя PostgreSQL база данных (Neon)."""
    return bool(os.getenv("DATABASE_URL"))

def _clean_pg_url(url: str) -> str:
    """Удаляет несовместимые с asyncpg query-параметры (channel_binding, sslmode) из URL."""
    parsed = urlparse(url)
    return f"postgresql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port or 5432}{parsed.path}"

def generate_invite_code() -> str:
    """Генерирует короткий удобный код приглашения для второго родителя, например 'BABY-7X9K'."""
    code = secrets.token_hex(2).upper()
    return f"BABY-{code}"

async def get_pg_pool():
    """Возвращает или инициализирует глобальный connection pool asyncpg."""
    global _pg_pool
    if _pg_pool is None:
        import asyncpg
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL is not set")
        clean_url = _clean_pg_url(db_url)
        _pg_pool = await asyncpg.create_pool(clean_url, ssl="require", min_size=1, max_size=10)
    return _pg_pool

async def close_db():
    """Закрывает пул соединений PostgreSQL при завершении приложения."""
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None

async def migrate_sqlite_to_postgres(sqlite_path: str = DB_PATH):
    """Импортирует данные из SQLite файла в Neon PostgreSQL (если их там еще нет)."""
    if not is_postgres() or not os.path.exists(sqlite_path):
        return
    import sqlite3
    try:
        s_conn = sqlite3.connect(sqlite_path)
        s_conn.row_factory = sqlite3.Row
        s_cur = s_conn.cursor()
        
        tables = [r[0] for r in s_cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "families" not in tables or "users" not in tables:
            s_conn.close()
            return
            
        fam_rows = s_cur.execute("SELECT * FROM families").fetchall()
        user_rows = s_cur.execute("SELECT * FROM users").fetchall()
        sleep_rows = s_cur.execute("SELECT * FROM sleep_sessions").fetchall() if "sleep_sessions" in tables else []
        s_conn.close()
        
        if not user_rows:
            return
            
        pool = await get_pg_pool()
        async with pool.acquire() as pg:
            family_id_map = {}
            for fam in fam_rows:
                f_dict = dict(fam)
                existing = await pg.fetchrow("SELECT id FROM families WHERE invite_code = $1", f_dict["invite_code"])
                if existing:
                    family_id_map[f_dict["id"]] = existing["id"]
                else:
                    new_id = await pg.fetchval(
                        """
                        INSERT INTO families (invite_code, baby_name, baby_birth_date, custom_wake_window_min, notifications_enabled, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING id
                        """,
                        f_dict["invite_code"], f_dict.get("baby_name", "Сева"), f_dict.get("baby_birth_date", "2026-09-11"),
                        f_dict.get("custom_wake_window_min"), f_dict.get("notifications_enabled", 1), f_dict["created_at"]
                    )
                    family_id_map[f_dict["id"]] = new_id

            for u in user_rows:
                u_dict = dict(u)
                old_fid = u_dict["family_id"]
                target_fid = family_id_map.get(old_fid)
                if not target_fid:
                    continue
                exists = await pg.fetchrow("SELECT telegram_id FROM users WHERE telegram_id = $1", u_dict["telegram_id"])
                if not exists:
                    await pg.execute(
                        """
                        INSERT INTO users (telegram_id, family_id, username, first_name, role, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        u_dict["telegram_id"], target_fid, u_dict.get("username"), u_dict.get("first_name", "Родитель"),
                        u_dict.get("role", "parent"), u_dict["created_at"]
                    )

            for s in sleep_rows:
                s_dict = dict(s)
                old_fid = s_dict["family_id"]
                target_fid = family_id_map.get(old_fid)
                if not target_fid:
                    continue
                exists = await pg.fetchrow(
                    "SELECT id FROM sleep_sessions WHERE family_id = $1 AND start_time = $2",
                    target_fid, s_dict["start_time"]
                )
                if not exists:
                    await pg.execute(
                        """
                        INSERT INTO sleep_sessions (family_id, start_time, end_time, is_night, started_by_user_id, ended_by_user_id, notes, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        target_fid, s_dict["start_time"], s_dict.get("end_time"), s_dict.get("is_night", 0),
                        s_dict.get("started_by_user_id"), s_dict.get("ended_by_user_id"), s_dict.get("notes"), s_dict["created_at"]
                    )
    except Exception as e:
        print(f"Warning: sqlite migration error: {e}")

async def export_neon_to_sqlite(target_path: str = "sleep_tracker_backup.db") -> str:
    """Экспортирует все данные из Neon Postgres в SQLite файл для скачивания резервной копии."""
    import sqlite3
    if os.path.exists(target_path):
        os.remove(target_path)
    s_conn = sqlite3.connect(target_path)
    s_cur = s_conn.cursor()
    s_cur.execute("""
    CREATE TABLE families (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invite_code TEXT UNIQUE NOT NULL,
        baby_name TEXT NOT NULL,
        baby_birth_date TEXT NOT NULL,
        custom_wake_window_min INTEGER,
        notifications_enabled INTEGER,
        created_at TEXT NOT NULL
    );
    """)
    s_cur.execute("""
    CREATE TABLE users (
        telegram_id INTEGER PRIMARY KEY,
        family_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        role TEXT,
        created_at TEXT NOT NULL
    );
    """)
    s_cur.execute("""
    CREATE TABLE sleep_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        family_id INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT,
        is_night INTEGER NOT NULL,
        started_by_user_id INTEGER,
        ended_by_user_id INTEGER,
        notes TEXT,
        created_at TEXT NOT NULL
    );
    """)
    pool = await get_pg_pool()
    async with pool.acquire() as pg:
        fams = await pg.fetch("SELECT * FROM families")
        for f in fams:
            s_cur.execute(
                "INSERT INTO families VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f["id"], f["invite_code"], f["baby_name"], f["baby_birth_date"], f["custom_wake_window_min"], f["notifications_enabled"], f["created_at"])
            )
        users = await pg.fetch("SELECT * FROM users")
        for u in users:
            s_cur.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
                (u["telegram_id"], u["family_id"], u["username"], u["first_name"], u["role"], u["created_at"])
            )
        sleeps = await pg.fetch("SELECT * FROM sleep_sessions")
        for s in sleeps:
            s_cur.execute(
                "INSERT INTO sleep_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s["id"], s["family_id"], s["start_time"], s["end_time"], s["is_night"], s["started_by_user_id"], s["ended_by_user_id"], s["notes"], s["created_at"])
            )
    s_conn.commit()
    s_conn.close()
    return target_path

async def init_db():
    """Создание таблиц и индексов при старте приложения."""
    if is_postgres():
        import asyncpg
        # Согласно рекомендациям Neon, для DDL и миграций схемы используется прямое подключение (UNPOOLED)
        direct_url = os.getenv("DATABASE_URL_UNPOOLED") or os.getenv("DATABASE_URL")
        clean_url = _clean_pg_url(direct_url)
        conn = await asyncpg.connect(clean_url, ssl="require")
        try:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS families (
                id SERIAL PRIMARY KEY,
                invite_code VARCHAR(50) UNIQUE NOT NULL,
                baby_name VARCHAR(100) NOT NULL DEFAULT 'Сева',
                baby_birth_date VARCHAR(20) NOT NULL DEFAULT '2026-09-11',
                custom_wake_window_min INTEGER DEFAULT NULL,
                notifications_enabled INTEGER DEFAULT 1,
                created_at VARCHAR(50) NOT NULL
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                family_id INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
                username VARCHAR(100),
                first_name VARCHAR(100),
                role VARCHAR(50) DEFAULT 'parent',
                created_at VARCHAR(50) NOT NULL
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS sleep_sessions (
                id SERIAL PRIMARY KEY,
                family_id INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
                start_time VARCHAR(50) NOT NULL,
                end_time VARCHAR(50),
                is_night INTEGER NOT NULL DEFAULT 0,
                started_by_user_id BIGINT,
                ended_by_user_id BIGINT,
                notes TEXT,
                created_at VARCHAR(50) NOT NULL
            );
            """)

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS access_whitelist (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(100),
                full_name VARCHAR(100),
                added_by BIGINT,
                notes VARCHAR(255),
                created_at VARCHAR(50) NOT NULL
            );
            """)

            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_family ON sleep_sessions(family_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_start ON sleep_sessions(start_time);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_family ON users(family_id);")

            # Добавляем суперадмина и маму по умолчанию
            now_iso = time_utils.get_local_now().isoformat()
            await conn.execute("""
            INSERT INTO access_whitelist (telegram_id, username, full_name, added_by, notes, created_at)
            VALUES (374188835, 'youngyannniii', 'Администратор (Папа)', 374188835, 'Владелец бота', $1)
            ON CONFLICT (telegram_id) DO NOTHING;
            """, now_iso)
            await conn.execute("""
            INSERT INTO access_whitelist (telegram_id, username, full_name, added_by, notes, created_at)
            VALUES (991615028, 'Яна', 'Мама Севы', 374188835, 'Мама Севы', $1)
            ON CONFLICT (telegram_id) DO NOTHING;
            """, now_iso)
        finally:
            await conn.close()

        # Инициализируем pooled connection pool для веб-приложения
        await get_pg_pool()
        # Автоматически мигрируем данные из локального SQLite файла, если он есть
        await migrate_sqlite_to_postgres()
        return

    # Fallback на SQLite (если DATABASE_URL не задан)
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS families (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invite_code TEXT UNIQUE NOT NULL,
            baby_name TEXT NOT NULL DEFAULT 'Сева',
            baby_birth_date TEXT NOT NULL DEFAULT '2026-09-11',
            custom_wake_window_min INTEGER DEFAULT NULL,
            notifications_enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            family_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            role TEXT DEFAULT 'parent',
            created_at TEXT NOT NULL,
            FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS sleep_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            family_id INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            is_night INTEGER NOT NULL DEFAULT 0,
            started_by_user_id INTEGER,
            ended_by_user_id INTEGER,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (family_id) REFERENCES families(id) ON DELETE CASCADE
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS access_whitelist (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            added_by INTEGER,
            notes TEXT,
            created_at TEXT NOT NULL
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_family ON sleep_sessions(family_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_start ON sleep_sessions(start_time);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_family ON users(family_id);")

        now_iso = time_utils.get_local_now().isoformat()
        await db.execute("""
        INSERT OR IGNORE INTO access_whitelist (telegram_id, username, full_name, added_by, notes, created_at)
        VALUES (374188835, 'youngyannniii', 'Администратор (Папа)', 374188835, 'Владелец бота', ?);
        """, (now_iso,))
        await db.execute("""
        INSERT OR IGNORE INTO access_whitelist (telegram_id, username, full_name, added_by, notes, created_at)
        VALUES (991615028, 'Яна', 'Мама Севы', 374188835, 'Мама Севы', ?);
        """, (now_iso,))
        await db.commit()

async def get_or_create_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    invite_code: Optional[str] = None,
    default_birth_date: str = "2026-09-11"
) -> Dict[str, Any]:
    """Получает пользователя или регистрирует нового (создает семью или присоединяет к существующей)."""
    now_iso = time_utils.get_local_now().isoformat()

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
            if user:
                await conn.execute(
                    "UPDATE users SET username = $1, first_name = $2 WHERE telegram_id = $3",
                    username or user["username"], first_name or user["first_name"], telegram_id
                )
                user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
                family = await conn.fetchrow("SELECT * FROM families WHERE id = $1", user["family_id"])
                return {"user": dict(user), "family": dict(family) if family else None}

            family_id = None
            if invite_code:
                code_clean = invite_code.strip().upper()
                fam_row = await conn.fetchrow("SELECT id FROM families WHERE invite_code = $1", code_clean)
                if fam_row:
                    family_id = fam_row["id"]

            if not family_id:
                new_code = generate_invite_code()
                family_id = await conn.fetchval(
                    """
                    INSERT INTO families (invite_code, baby_name, baby_birth_date, created_at)
                    VALUES ($1, 'Малыш', $2, $3)
                    RETURNING id
                    """,
                    new_code, default_birth_date, now_iso
                )

            await conn.execute(
                """
                INSERT INTO users (telegram_id, family_id, username, first_name, created_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                telegram_id, family_id, username, first_name or "Родитель", now_iso
            )
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
            family = await conn.fetchrow("SELECT * FROM families WHERE id = $1", family_id)
            return {"user": dict(user), "family": dict(family)}

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            user = await cursor.fetchone()

        if user:
            await db.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                (username or user["username"], first_name or user["first_name"], telegram_id)
            )
            await db.commit()
            async with db.execute("SELECT * FROM families WHERE id = ?", (user["family_id"],)) as f_cur:
                family = await f_cur.fetchone()
            return {"user": dict(user), "family": dict(family) if family else None}

        family_id = None
        if invite_code:
            code_clean = invite_code.strip().upper()
            async with db.execute("SELECT id FROM families WHERE invite_code = ?", (code_clean,)) as cur:
                fam_row = await cur.fetchone()
                if fam_row:
                    family_id = fam_row["id"]

        if not family_id:
            new_code = generate_invite_code()
            cursor = await db.execute(
                """
                INSERT INTO families (invite_code, baby_name, baby_birth_date, created_at)
                VALUES (?, 'Малыш', ?, ?)
                """,
                (new_code, default_birth_date, now_iso)
            )
            family_id = cursor.lastrowid

        await db.execute(
            """
            INSERT INTO users (telegram_id, family_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, family_id, username, first_name or "Родитель", now_iso)
        )
        await db.commit()

        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as u_cur:
            user = await u_cur.fetchone()
        async with db.execute("SELECT * FROM families WHERE id = ?", (family_id,)) as f_cur:
            family = await f_cur.fetchone()

        return {"user": dict(user), "family": dict(family)}

async def join_family(telegram_id: int, invite_code: str) -> Optional[Dict[str, Any]]:
    """Присоединяет пользователя к существующей семье по коду приглашения."""
    code_clean = invite_code.strip().upper()

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            family = await conn.fetchrow("SELECT * FROM families WHERE invite_code = $1", code_clean)
            if not family:
                return None
            await conn.execute("UPDATE users SET family_id = $1 WHERE telegram_id = $2", family["id"], telegram_id)
            return dict(family)

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM families WHERE invite_code = ?", (code_clean,)) as cur:
            family = await cur.fetchone()
        if not family:
            return None
        await db.execute("UPDATE users SET family_id = ? WHERE telegram_id = ?", (family["id"], telegram_id))
        await db.commit()
        return dict(family)

async def get_family_info(family_id: int) -> Optional[Dict[str, Any]]:
    """Получает информацию о семье и список родителей."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            family = await conn.fetchrow("SELECT * FROM families WHERE id = $1", family_id)
            if not family:
                return None
            members = await conn.fetch("SELECT telegram_id, username, first_name, role, created_at FROM users WHERE family_id = $1", family_id)
            fam_dict = dict(family)
            fam_dict["members"] = [dict(m) for m in members]
            return fam_dict

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM families WHERE id = ?", (family_id,)) as cur:
            family = await cur.fetchone()
        if not family:
            return None
        async with db.execute("SELECT telegram_id, username, first_name, role, created_at FROM users WHERE family_id = ?", (family_id,)) as cur:
            members = await cur.fetchall()
        fam_dict = dict(family)
        fam_dict["members"] = [dict(m) for m in members]
        return fam_dict

async def update_baby_profile(
    family_id: int,
    baby_name: Optional[str] = None,
    baby_birth_date: Optional[str] = None,
    custom_wake_window_min: Optional[int] = None,
    notifications_enabled: Optional[int] = None
) -> Dict[str, Any]:
    """Обновляет профиль малыша и настройки сна."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            updates = []
            params = []
            idx = 1
            if baby_name is not None:
                updates.append(f"baby_name = ${idx}")
                params.append(baby_name.strip())
                idx += 1
            if baby_birth_date is not None:
                updates.append(f"baby_birth_date = ${idx}")
                params.append(baby_birth_date.strip())
                idx += 1
            if custom_wake_window_min is not None:
                updates.append(f"custom_wake_window_min = ${idx}")
                params.append(custom_wake_window_min if custom_wake_window_min > 0 else None)
                idx += 1
            if notifications_enabled is not None:
                updates.append(f"notifications_enabled = ${idx}")
                params.append(1 if notifications_enabled else 0)
                idx += 1

            if updates:
                params.append(family_id)
                sql = f"UPDATE families SET {', '.join(updates)} WHERE id = ${idx}"
                await conn.execute(sql, *params)

            fam = await conn.fetchrow("SELECT * FROM families WHERE id = $1", family_id)
            return dict(fam) if fam else {}

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        updates = []
        params = []
        if baby_name is not None:
            updates.append("baby_name = ?")
            params.append(baby_name.strip())
        if baby_birth_date is not None:
            updates.append("baby_birth_date = ?")
            params.append(baby_birth_date.strip())
        if custom_wake_window_min is not None:
            updates.append("custom_wake_window_min = ?")
            params.append(custom_wake_window_min if custom_wake_window_min > 0 else None)
        if notifications_enabled is not None:
            updates.append("notifications_enabled = ?")
            params.append(1 if notifications_enabled else 0)

        if updates:
            params.append(family_id)
            sql = f"UPDATE families SET {', '.join(updates)} WHERE id = ?"
            await db.execute(sql, params)
            await db.commit()

        async with db.execute("SELECT * FROM families WHERE id = ?", (family_id,)) as cur:
            fam = await cur.fetchone()
        return dict(fam) if fam else {}

async def get_active_sleep(family_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает текущую незавершенную сессию сна (если малыш спит прямо сейчас)."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.*, u.first_name as started_by_name
                FROM sleep_sessions s
                LEFT JOIN users u ON s.started_by_user_id = u.telegram_id
                WHERE s.family_id = $1 AND s.end_time IS NULL
                ORDER BY s.start_time DESC
                LIMIT 1
                """,
                family_id
            )
            return dict(row) if row else None

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.*, u.first_name as started_by_name
            FROM sleep_sessions s
            LEFT JOIN users u ON s.started_by_user_id = u.telegram_id
            WHERE s.family_id = ? AND s.end_time IS NULL
            ORDER BY s.start_time DESC
            LIMIT 1
            """,
            (family_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_last_completed_sleep(family_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает последний завершенный сон."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.*, u.first_name as ended_by_name
                FROM sleep_sessions s
                LEFT JOIN users u ON s.ended_by_user_id = u.telegram_id
                WHERE s.family_id = $1 AND s.end_time IS NOT NULL
                ORDER BY s.end_time DESC
                LIMIT 1
                """,
                family_id
            )
            return dict(row) if row else None

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.*, u.first_name as ended_by_name
            FROM sleep_sessions s
            LEFT JOIN users u ON s.ended_by_user_id = u.telegram_id
            WHERE s.family_id = ? AND s.end_time IS NOT NULL
            ORDER BY s.end_time DESC
            LIMIT 1
            """,
            (family_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def start_sleep(
    family_id: int,
    user_id: int,
    start_time_iso: Optional[str] = None,
    is_night: Optional[int] = None
) -> Dict[str, Any]:
    """Начинает новую сессию сна с автоматическим определением день/ночь в часовом поясе пользователя."""
    now = time_utils.get_local_now()
    if not start_time_iso:
        start_time_iso = now.isoformat()
    else:
        start_dt = time_utils.to_local_datetime(start_time_iso)
        start_time_iso = start_dt.isoformat()

    if is_night is None:
        is_night = 1 if time_utils.is_night_sleep(start_time_iso) else 0

    now_iso = now.isoformat()

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            # Завершаем предыдущий незавершенный сон
            await conn.execute(
                "UPDATE sleep_sessions SET end_time = $1, ended_by_user_id = $2 WHERE family_id = $3 AND end_time IS NULL",
                start_time_iso, user_id, family_id
            )
            nap_number = None
            if not is_night:
                day_str = start_time_iso.split("T")[0]
                cnt = await conn.fetchval(
                    "SELECT COUNT(*) FROM sleep_sessions WHERE family_id = $1 AND is_night = 0 AND start_time LIKE $2",
                    family_id, f"{day_str}%"
                )
                nap_number = (cnt or 0) + 1

            row = await conn.fetchrow(
                """
                INSERT INTO sleep_sessions (family_id, start_time, is_night, started_by_user_id, created_at)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                family_id, start_time_iso, is_night, user_id, now_iso
            )
            res = dict(row)
            res["nap_number"] = nap_number
            return res

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "UPDATE sleep_sessions SET end_time = ?, ended_by_user_id = ? WHERE family_id = ? AND end_time IS NULL",
            (start_time_iso, user_id, family_id)
        )
        nap_number = None
        if not is_night:
            day_str = start_time_iso.split("T")[0]
            async with db.execute(
                "SELECT COUNT(*) FROM sleep_sessions WHERE family_id = ? AND is_night = 0 AND start_time LIKE ?",
                (family_id, f"{day_str}%")
            ) as cnt_cur:
                cnt_row = await cnt_cur.fetchone()
                nap_number = (cnt_row[0] if cnt_row else 0) + 1

        cursor = await db.execute(
            """
            INSERT INTO sleep_sessions (family_id, start_time, is_night, started_by_user_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (family_id, start_time_iso, is_night, user_id, now_iso)
        )
        session_id = cursor.lastrowid
        await db.commit()

        async with db.execute("SELECT * FROM sleep_sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
            res = dict(row)
            res["nap_number"] = nap_number
            return res

async def stop_sleep(
    family_id: int,
    user_id: int,
    end_time_iso: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Завершает текущую активную сессию сна (малыш проснулся)."""
    now = time_utils.get_local_now()
    if not end_time_iso:
        end_time_iso = now.isoformat()
    else:
        end_dt = time_utils.to_local_datetime(end_time_iso)
        end_time_iso = end_dt.isoformat()

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            active = await conn.fetchrow(
                "SELECT * FROM sleep_sessions WHERE family_id = $1 AND end_time IS NULL ORDER BY start_time DESC LIMIT 1",
                family_id
            )
            if not active:
                return None
            row = await conn.fetchrow(
                "UPDATE sleep_sessions SET end_time = $1, ended_by_user_id = $2 WHERE id = $3 RETURNING *",
                end_time_iso, user_id, active["id"]
            )
            return dict(row) if row else None

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sleep_sessions WHERE family_id = ? AND end_time IS NULL ORDER BY start_time DESC LIMIT 1",
            (family_id,)
        ) as cur:
            active = await cur.fetchone()
        if not active:
            return None
        await db.execute(
            "UPDATE sleep_sessions SET end_time = ?, ended_by_user_id = ? WHERE id = ?",
            (end_time_iso, user_id, active["id"])
        )
        await db.commit()
        async with db.execute("SELECT * FROM sleep_sessions WHERE id = ?", (active["id"],)) as cur:
            row = await cur.fetchone()
            return dict(row)

async def add_manual_sleep(
    family_id: int,
    user_id: int,
    start_time_iso: str,
    end_time_iso: str,
    is_night: Optional[int] = None,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """Добавляет сон вручную за прошедший период."""
    start_dt = time_utils.to_local_datetime(start_time_iso)
    end_dt = time_utils.to_local_datetime(end_time_iso)

    if end_dt <= start_dt:
        raise ValueError("Время окончания сна должно быть позже времени начала")

    if is_night is None:
        is_night = 1 if time_utils.is_night_sleep(start_dt) else 0

    now_iso = time_utils.get_local_now().isoformat()

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sleep_sessions (family_id, start_time, end_time, is_night, started_by_user_id, ended_by_user_id, notes, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                family_id, start_dt.isoformat(), end_dt.isoformat(), is_night, user_id, user_id, notes, now_iso
            )
            return dict(row)

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            INSERT INTO sleep_sessions (family_id, start_time, end_time, is_night, started_by_user_id, ended_by_user_id, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (family_id, start_dt.isoformat(), end_dt.isoformat(), is_night, user_id, user_id, notes, now_iso)
        )
        session_id = cursor.lastrowid
        await db.commit()
        async with db.execute("SELECT * FROM sleep_sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)

async def update_sleep(
    sleep_id: int,
    family_id: int,
    start_time_iso: Optional[str] = None,
    end_time_iso: Optional[str] = None,
    is_night: Optional[int] = None,
    notes: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Редактирует запись о сне."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            updates = []
            params = []
            idx = 1
            if start_time_iso is not None:
                updates.append(f"start_time = ${idx}")
                params.append(datetime.fromisoformat(start_time_iso).isoformat())
                idx += 1
            if end_time_iso is not None:
                updates.append(f"end_time = ${idx}")
                params.append(datetime.fromisoformat(end_time_iso).isoformat() if end_time_iso else None)
                idx += 1
            if is_night is not None:
                updates.append(f"is_night = ${idx}")
                params.append(1 if is_night else 0)
                idx += 1
            if notes is not None:
                updates.append(f"notes = ${idx}")
                params.append(notes.strip())
                idx += 1

            if not updates:
                return None

            params.extend([sleep_id, family_id])
            sql = f"UPDATE sleep_sessions SET {', '.join(updates)} WHERE id = ${idx} AND family_id = ${idx + 1} RETURNING *"
            row = await conn.fetchrow(sql, *params)
            return dict(row) if row else None

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        updates = []
        params = []
        if start_time_iso is not None:
            updates.append("start_time = ?")
            params.append(datetime.fromisoformat(start_time_iso).isoformat())
        if end_time_iso is not None:
            updates.append("end_time = ?")
            params.append(datetime.fromisoformat(end_time_iso).isoformat() if end_time_iso else None)
        if is_night is not None:
            updates.append("is_night = ?")
            params.append(1 if is_night else 0)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes.strip())

        if not updates:
            return None

        params.extend([sleep_id, family_id])
        sql = f"UPDATE sleep_sessions SET {', '.join(updates)} WHERE id = ? AND family_id = ?"
        await db.execute(sql, params)
        await db.commit()

        async with db.execute("SELECT * FROM sleep_sessions WHERE id = ?", (sleep_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def delete_sleep(sleep_id: int, family_id: int) -> bool:
    """Удаляет запись о сне."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            res = await conn.execute("DELETE FROM sleep_sessions WHERE id = $1 AND family_id = $2", sleep_id, family_id)
            count = int(res.split(" ")[-1]) if " " in res else 0
            return count > 0

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM sleep_sessions WHERE id = ? AND family_id = ?", (sleep_id, family_id))
        await db.commit()
        return cursor.rowcount > 0

async def get_sleeps_for_day(family_id: int, target_date: date) -> List[Dict[str, Any]]:
    """Получает все сны, относящиеся к указанному дню (с 00:00 до 23:59:59)."""
    start_of_day = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0).isoformat()
    end_of_day = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59).isoformat()

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.*, 
                       u1.first_name as started_by_name,
                       u2.first_name as ended_by_name
                FROM sleep_sessions s
                LEFT JOIN users u1 ON s.started_by_user_id = u1.telegram_id
                LEFT JOIN users u2 ON s.ended_by_user_id = u2.telegram_id
                WHERE s.family_id = $1
                  AND (
                      (s.start_time <= $2 AND (s.end_time >= $3 OR s.end_time IS NULL))
                      OR (s.start_time >= $4 AND s.start_time <= $5)
                  )
                ORDER BY s.start_time ASC
                """,
                family_id, end_of_day, start_of_day, start_of_day, end_of_day
            )
            return [dict(r) for r in rows]

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.*, 
                   u1.first_name as started_by_name,
                   u2.first_name as ended_by_name
            FROM sleep_sessions s
            LEFT JOIN users u1 ON s.started_by_user_id = u1.telegram_id
            LEFT JOIN users u2 ON s.ended_by_user_id = u2.telegram_id
            WHERE s.family_id = ?
              AND (
                  (s.start_time <= ? AND (s.end_time >= ? OR s.end_time IS NULL))
                  OR (s.start_time >= ? AND s.start_time <= ?)
              )
            ORDER BY s.start_time ASC
            """,
            (family_id, end_of_day, start_of_day, start_of_day, end_of_day)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_all_sleeps_for_period(family_id: int, start_date: date, end_date: date) -> List[Dict[str, Any]]:
    """Получает все сны за период между датами (для экспорта и недельной аналитики)."""
    start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0).isoformat()
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59).isoformat()

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.*, 
                       u1.first_name as started_by_name,
                       u2.first_name as ended_by_name
                FROM sleep_sessions s
                LEFT JOIN users u1 ON s.started_by_user_id = u1.telegram_id
                LEFT JOIN users u2 ON s.ended_by_user_id = u2.telegram_id
                WHERE s.family_id = $1
                  AND s.start_time >= $2 AND s.start_time <= $3
                ORDER BY s.start_time ASC
                """,
                family_id, start_dt, end_dt
            )
            return [dict(r) for r in rows]

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.*, 
                   u1.first_name as started_by_name,
                   u2.first_name as ended_by_name
            FROM sleep_sessions s
            LEFT JOIN users u1 ON s.started_by_user_id = u1.telegram_id
            LEFT JOIN users u2 ON s.ended_by_user_id = u2.telegram_id
            WHERE s.family_id = ?
              AND s.start_time >= ? AND s.start_time <= ?
            ORDER BY s.start_time ASC
            """,
            (family_id, start_dt, end_dt)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_all_families_for_reminders() -> List[Dict[str, Any]]:
    """Получает список семей с включенными уведомлениями."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM families WHERE notifications_enabled = 1")
            return [dict(r) for r in rows]

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM families WHERE notifications_enabled = 1") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

# =====================================================================
# БЕЛЫЙ СПИСОК И АДМИНИСТРИРОВАНИЕ ДОСТУПА (WHITELIST)
# =====================================================================

async def is_user_allowed(telegram_id: int) -> bool:
    """Проверяет, разрешен ли доступ пользователю к боту и трекеру."""
    # Суперадмин всегда имеет доступ
    if telegram_id == SUPERADMIN_ID:
        return True

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT telegram_id FROM access_whitelist WHERE telegram_id = $1", telegram_id)
            return row is not None

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT telegram_id FROM access_whitelist WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
            return row is not None

async def get_whitelist() -> List[Dict[str, Any]]:
    """Возвращает список всех пользователей с доступом."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM access_whitelist ORDER BY created_at ASC")
            return [dict(r) for r in rows]

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM access_whitelist ORDER BY created_at ASC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def add_to_whitelist(
    telegram_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
    added_by: int = SUPERADMIN_ID,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """Добавляет пользователя в белый список доступа."""
    now_iso = time_utils.get_local_now().isoformat()
    clean_username = username.lstrip("@").strip() if username else None
    display_name = full_name.strip() if full_name else (f"@{clean_username}" if clean_username else f"ID {telegram_id}")

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO access_whitelist (telegram_id, username, full_name, added_by, notes, created_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    notes = EXCLUDED.notes
                """,
                telegram_id, clean_username, display_name, added_by, notes, now_iso
            )
            row = await conn.fetchrow("SELECT * FROM access_whitelist WHERE telegram_id = $1", telegram_id)
            return dict(row)

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT OR REPLACE INTO access_whitelist (telegram_id, username, full_name, added_by, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, clean_username, display_name, added_by, notes, now_iso)
        )
        await db.commit()
        async with db.execute("SELECT * FROM access_whitelist WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
            return dict(row)

async def remove_from_whitelist(telegram_id: int) -> bool:
    """Удаляет пользователя из белого списка (суперадмина удалить нельзя)."""
    if telegram_id == SUPERADMIN_ID:
        return False

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            res = await conn.execute("DELETE FROM access_whitelist WHERE telegram_id = $1", telegram_id)
            count = int(res.split(" ")[-1]) if " " in res else 0
            return count > 0

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM access_whitelist WHERE telegram_id = ?", (telegram_id,))
        await db.commit()
        return cursor.rowcount > 0
