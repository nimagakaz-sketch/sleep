import asyncio
from main import (
    app, serve_index, get_status, api_start_sleep, api_stop_sleep,
    api_get_daily_stats, StartSleepRequest, StopSleepRequest
)
import database as db

async def test_routes():
    await db.init_db()

    print("Testing serve_index ...")
    r = await serve_index()
    print("✅ serve_index returned:", type(r))

    print("Testing get_status ...")
    data = await get_status(telegram_id=99999, username="TestDad")
    assert "baby" in data
    assert "family" in data
    assert data["baby"]["age_text"] == "2 месяца 7 дней"
    print("✅ get_status works:", data["baby"])

    print("Testing api_start_sleep ...")
    fam_id = data["family"]["id"]
    r = await api_start_sleep(StartSleepRequest(family_id=fam_id, user_id=99999))
    assert r["success"] is True
    print("✅ api_start_sleep works!")

    print("Testing get_status (sleeping state) ...")
    data = await get_status(telegram_id=99999)
    assert data["is_sleeping"] is True
    print("✅ Sleeping status detected!")

    print("Testing api_stop_sleep ...")
    r = await api_stop_sleep(StopSleepRequest(family_id=fam_id, user_id=99999))
    assert r["success"] is True
    print("✅ api_stop_sleep works!")

    print("Testing api_get_daily_stats ...")
    today = data["family"]["created_at"].split("T")[0]
    stats = await api_get_daily_stats(family_id=fam_id, date=today)
    assert "total_sleep_str" in stats
    print("✅ api_get_daily_stats works! Total sleep:", stats["total_sleep_str"])

    print("\n🎉 ALL ROUTE TESTS PASSED!")

if __name__ == "__main__":
    asyncio.run(test_routes())
