import pytest
import settings.core as sc


def test_get_returns_default_when_unset(db):
    assert sc.get_int(db, "max_upload_mb") == 500


def test_set_and_get(db):
    sc.set_setting(db, "max_upload_mb", "50"); db.commit()
    assert sc.get(db, "max_upload_mb") == "50"
    assert sc.get_int(db, "max_upload_mb") == 50


def test_get_bool(db):
    sc.set_setting(db, "watch_enabled", "true"); db.commit()
    assert sc.get_bool(db, "watch_enabled") is True
    sc.set_setting(db, "watch_enabled", "false"); db.commit()
    assert sc.get_bool(db, "watch_enabled") is False


def test_set_rejects_unknown_key(db):
    with pytest.raises(ValueError):
        sc.set_setting(db, "not_a_real_key", "x")


def test_all_settings_reports_default_and_override(db):
    sc.set_setting(db, "watch_space", "Engineering"); db.commit()
    rows = {r["key"]: r for r in sc.all_settings(db)}
    assert set(rows) == set(sc.EDITABLE_KEYS)
    assert rows["watch_space"]["value"] == "Engineering"
    assert rows["watch_space"]["is_overridden"] is True
    assert rows["max_upload_mb"]["is_overridden"] is False
    assert rows["max_upload_mb"]["applies"] == "live"
    assert rows["watch_debounce_sec"]["applies"] == "restart"
