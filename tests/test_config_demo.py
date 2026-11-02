import os

from harness import env

from salesdesk import seed as seed_module
from salesdesk.config import ConfigError, Settings
from salesdesk.db import Database


def test_development_needs_only_a_token_and_production_is_strict(tmp_path):
    os.chdir(tmp_path)  # no .env here
    with env(BALE_BOT_TOKEN="t"):
        assert Settings.from_env().env == "development"
    with env(APP_ENV="production", BALE_BOT_TOKEN="t"):
        try:
            Settings.from_env()
        except ConfigError as exc:
            assert all(k in str(exc) for k in ("DASHBOARD_KEY", "TEAM_CODE", "CEO_IDS"))
        else:
            raise AssertionError("production without secrets must fail for the real bot")
    with env(APP_ENV="production", BALE_BOT_TOKEN="t", DASHBOARD_KEY="k" * 16, TEAM_CODE="c", CEO_IDS="bale:1"):
        assert Settings.from_env().is_ceo("bale", "1")


def test_demo_and_tooling_work_without_secrets_even_in_production(tmp_path):
    os.chdir(tmp_path)
    with env(APP_ENV="production"):
        cfg = Settings.from_env(need_platform=False)  # what seed / web --demo / admin use
        assert cfg.env == "production" and not cfg.tokens


def test_bad_values_give_clear_errors(tmp_path):
    os.chdir(tmp_path)
    for name, value, hint in (("DIGEST_HOUR", "abc", "DIGEST_HOUR"), ("DIGEST_HOUR", "30", "DIGEST_HOUR"),
                              ("TIMEZONE", "Mars/Base", "TIMEZONE"), ("APP_ENV", "staging", "APP_ENV")):
        with env(BALE_BOT_TOKEN="t", **{name: value}):
            try:
                Settings.from_env()
            except ConfigError as exc:
                assert hint in str(exc)
            else:
                raise AssertionError(f"{name}={value} should be rejected")


def test_make_demo_entrypoint_works_and_is_repeatable(tmp_path):
    os.chdir(tmp_path)
    with env():
        assert seed_module.main([]) == 0  # the exact entry point behind `make demo`
        assert seed_module.main([]) == 0  # re-running refreshes the demo database
    demo = Database(tmp_path / "data" / "demo.db")
    assert demo.kv_get("demo") == "1" and demo.status_counts()


def test_seed_never_overwrites_real_data(tmp_path):
    os.chdir(tmp_path)
    with env():
        real = Database(tmp_path / "data" / "salesdesk.db")
        user = real.upsert_user("bale", "1", "1", "سارا", "employee", active=True)
        real.add_lead("مشتری واقعی", "", "", user.id)
        assert seed_module.main(["--db", str(tmp_path / "data" / "salesdesk.db")]) == 2
        assert real.search_leads()[1] == 1  # untouched
        assert seed_module.main(["--db", str(tmp_path / "other.db")]) == 0
