import asyncio
import os
from datetime import datetime, date, timedelta
import database as db
import norms
import analytics

async def run_tests():
    if os.path.exists("sleep_tracker.db"):
        os.remove("sleep_tracker.db")

    print("1. Initializing DB...")
    await db.init_db()

    print("2. Testing age calculation for 2 months 7 days...")
    # 2 months 7 days ago
    birth_date = date.today() - timedelta(days=67)
    age_info = norms.calculate_age_info(birth_date)
    print("   Age info:", age_info)
    assert age_info["days"] == 67
    assert age_info["norm_wb_min"] == 75
    assert age_info["norm_wb_max"] == 90
    print("   ✅ Norms calculation verified!")

    print("3. Testing user & family creation...")
    user1_id = 11111
    user1 = await db.get_or_create_user(user1_id, "dad", "Папа")
    family = user1["family"]
    code = family["invite_code"]
    print("   Family created:", family["id"], "Code:", code)

    print("4. Testing second parent joining...")
    user2_id = 22222
    user2 = await db.get_or_create_user(user2_id, "mom", "Мама", invite_code=code)
    assert user2["family"]["id"] == family["id"]
    fam_info = await db.get_family_info(family["id"])
    assert len(fam_info["members"]) == 2
    print(f"   ✅ Both parents successfully linked to family {code}!")

    print("5. Testing sleep tracking (Dad puts baby to bed, Mom marks wake up)...")
    # Sleep starts 1 hour ago
    sleep_start = (datetime.now() - timedelta(hours=1, minutes=15)).isoformat()
    session = await db.start_sleep(family["id"], user1_id, start_time_iso=sleep_start, is_night=0)
    assert session["family_id"] == family["id"]

    active = await db.get_active_sleep(family["id"])
    assert active is not None
    assert active["id"] == session["id"]

    # Mom marks wake up
    wake_time = (datetime.now() - timedelta(minutes=15)).isoformat()
    ended = await db.stop_sleep(family["id"], user2_id, end_time_iso=wake_time)
    assert ended["end_time"] == wake_time
    print(f"   ✅ Sleep recorded: duration {ended['start_time']} -> {ended['end_time']}")

    print("6. Testing next sleep recommendation...")
    wake_dt = datetime.fromisoformat(ended["end_time"])
    next_sleep = norms.calculate_next_sleep(wake_dt, birth_date)
    print("   Next sleep recommendation:", next_sleep["status_text"], "Target:", next_sleep["target_sleep_time"], "Minutes left:", next_sleep["minutes_left"])
    assert next_sleep["target_wb_min"] == 80
    print("   ✅ Next sleep recommendation verified!")

    print("7. Testing daily summary & timeline...")
    sleep_date = wake_dt.date()
    sleeps = await db.get_sleeps_for_day(family["id"], sleep_date)
    summary = analytics.calculate_daily_summary(sleeps, sleep_date, birth_date)
    print("   Total sleep:", summary["total_sleep_str"])
    print("   Timeline segments count:", len(summary["timeline_segments"]))
    assert summary["total_sleep_min"] == 60 # 1h 15m - 15m = 60m
    print("   ✅ Daily summary verified!")

    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(run_tests())
