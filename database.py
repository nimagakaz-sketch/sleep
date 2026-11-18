"""
База данных SQLite для трекера сна ребенка с поддержкой семейного доступа (нескольких родителей).
Используется aiosqlite для полностью асинхронной работы с FastAPI и Aiogram.
"""

import aiosqlite
import secrets
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any
import time_utils

DB_PATH = "sleep_tracker.db"

def generate_invite_code() -> str:
    """Генерирует короткий удобный код приглашения для второго родителя, например 'BABY-7X9K'."""
    code = secrets.token_hex(2).upper()
    return f"BABY-{code}"

async def init_db():
    """Создание таблиц и индексов при старте приложения."""
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
        
        # Индексы для ускорения выборок
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_family ON sleep_sessions(family_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_start ON sleep_sessions(start_time);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_family ON users(family_id);")
        
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # Проверяем, существует ли пользователь
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            user = await cursor.fetchone()
            
        if user:
            # Обновляем профиль (имя, username)
            await db.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                (username or user["username"], first_name or user["first_name"], telegram_id)
            )
            await db.commit()
            
            async with db.execute("SELECT * FROM families WHERE id = ?", (user["family_id"],)) as f_cur:
                family = await f_cur.fetchone()
            return {"user": dict(user), "family": dict(family) if family else None}
            
        # Пользователь новый
        family_id = None
        if invite_code:
            code_clean = invite_code.strip().upper()
            async with db.execute("SELECT id FROM families WHERE invite_code = ?", (code_clean,)) as cur:
                fam_row = await cur.fetchone()
                if fam_row:
                    family_id = fam_row["id"]
                    
        # Если кода нет или он недействителен, создаем новую семью
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM families WHERE invite_code = ?", (code_clean,)) as cur:
            family = await cur.fetchone()
            
        if not family:
            return None
            
        await db.execute(
            "UPDATE users SET family_id = ? WHERE telegram_id = ?",
            (family["id"], telegram_id)
        )
        await db.commit()
        return dict(family)

async def get_family_info(family_id: int) -> Optional[Dict[str, Any]]:
    """Получает информацию о семье и список родителей."""
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
        
    # Автоматическое определение дневной / ночной сон по локальному времени (20:30 - 07:30 = ночь)
    if is_night is None:
        is_night = 1 if time_utils.is_night_sleep(start_time_iso) else 0

    now_iso = now.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # Если уже был активный сон, завершаем его текущим временем
        await db.execute(
            "UPDATE sleep_sessions SET end_time = ?, ended_by_user_id = ? WHERE family_id = ? AND end_time IS NULL",
            (start_time_iso, user_id, family_id)
        )

        # Вычисляем номер дневного сна за сегодня (если это дневной сон)
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

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # Ищем активный сон
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
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM sleep_sessions WHERE id = ? AND family_id = ?", (sleep_id, family_id))
        await db.commit()
        return cursor.rowcount > 0

async def get_sleeps_for_day(family_id: int, target_date: date) -> List[Dict[str, Any]]:
    """
    Получает все сны, относящиеся к указанному дню (с 00:00 до 23:59:59).
    Сон считается относящимся к дню, если он начался в этот день,
    или завершился в этот день, или пересекает этот день.
    """
    start_of_day = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0).isoformat()
    end_of_day = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59).isoformat()
    
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM families WHERE notifications_enabled = 1"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
