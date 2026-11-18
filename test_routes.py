import asyncio
from main import (
    app, serve_index, health_check, get_status, api_start_sleep, api_stop_sleep,
    api_get_daily_stats, api_get_advice, api_get_weekly_stats,
    api_export_csv, api_export_text, StartSleepRequest, StopSleepRequest
)
import database as db

async def test_routes():
    await db.init_db()

    print("Testing serve_index ...")
    r = await serve_index()
    print("✅ serve_index returned:", type(r))

    print("Testing health_check (/health & /ping) ...")
    h = await health_check()
    assert h["status"] == "ok"
    print("✅ health_check works:", h)

    print("Testing get_status ...")
    data = await get_status(telegram_id=99999, username="TestDad")
    assert "baby" in data
    assert "family" in data
    assert "expert_advice" in data
    assert data["baby"]["age_text"] == "2 месяца 7 дней"
    print("✅ get_status works:", data["baby"])
    print("   Expert advice stage:", data["expert_advice"]["stage_title"])

    print("Testing api_get_advice ...")
    fam_id = data["family"]["id"]
    adv = await api_get_advice(family_id=fam_id)
    assert "fatigue_signs" in adv
    assert len(adv["fatigue_signs"]) > 0
    print("✅ api_get_advice works! Fatigue signs count:", len(adv["fatigue_signs"]))

    print("Testing api_start_sleep ...")
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

    print("Testing api_get_weekly_stats ...")
    weekly = await api_get_weekly_stats(family_id=fam_id)
    assert "days" in weekly
    assert len(weekly["days"]) == 7
    assert "averages" in weekly
    print("✅ api_get_weekly_stats works! 7 days present, trend:", weekly["trend"]["title"])

    print("Testing api_export_csv ...")
    csv_resp = await api_export_csv(family_id=fam_id)
    assert csv_resp.media_type == "text/csv; charset=utf-8"
    assert b"\xef\xbb\xbf" in csv_resp.body # UTF-8 BOM
    print("✅ api_export_csv works! Length:", len(csv_resp.body))

    print("Testing api_export_text ...")
    text_resp = await api_export_text(family_id=fam_id)
    assert "report_text" in text_resp
    assert "Отчет по сну" in text_resp["report_text"]
    print("✅ api_export_text works! Preview:\n", text_resp["report_text"][:120], "...")

    print("\n🎉 ALL ROUTE TESTS PASSED!")

if __name__ == "__main__":
    asyncio.run(test_routes())
