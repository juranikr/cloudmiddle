from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import main as main_module
from app.config import Settings
from app.db import engine_kwargs_for


def _request(method: str, path: str) -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    })


class DatabaseModeSettingsTests(unittest.TestCase):
    def test_local_postgres_accepts_only_the_isolated_database(self) -> None:
        config = Settings(
            _env_file=None,
            app_db_mode="local",
            database_url=(
                "postgresql+psycopg2://cloudmiddle_local:password@"
                "127.0.0.1:55432/cloudmiddle_local"
            ),
        )

        self.assertEqual(config.runtime_db_mode, "local")

        for database_url in (
            "postgresql+psycopg2://u:p@db.example.com:5432/cloudmiddle_local",
            "postgresql+psycopg2://u:p@127.0.0.1:5432/cloudmiddle_local",
            "postgresql+psycopg2://u:p@127.0.0.1:55432/production",
        ):
            with self.subTest(database_url=database_url), self.assertRaises(ValidationError):
                Settings(
                    _env_file=None,
                    app_db_mode="local",
                    database_url=database_url,
                )

    def test_local_sqlite_remains_available_for_host_development(self) -> None:
        config = Settings(
            _env_file=None,
            app_db_mode="local",
            database_url="sqlite:///./test-local.db",
        )

        self.assertTrue(config.is_sqlite)
        self.assertEqual(config.runtime_db_mode, "local")

    def test_production_readonly_requires_postgres_and_sets_driver_guard(self) -> None:
        config = Settings(
            _env_file=None,
            app_db_mode="production_readonly",
            database_url="postgresql+psycopg2://reader:redacted@db.example.com/travel",
        )

        self.assertEqual(
            engine_kwargs_for(config)["connect_args"]["options"],
            "-c default_transaction_read_only=on",
        )
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                app_db_mode="production_readonly",
                database_url="sqlite:///./not-production.db",
            )

    def test_unset_mode_preserves_existing_application_runtime(self) -> None:
        config = Settings(_env_file=None, database_url="sqlite:///./legacy.db")

        self.assertEqual(config.runtime_db_mode, "application")
        self.assertFalse(config.is_production_readonly)


class ProductionReadonlyHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_login_post_is_allowed(self) -> None:
        readonly = SimpleNamespace(
            is_production_readonly=True,
            runtime_db_mode="production_readonly",
        )
        downstream = AsyncMock(return_value=JSONResponse({"ok": True}))

        with patch.object(main_module, "settings", readonly):
            login_response = await main_module.enforce_production_readonly(
                _request("POST", "/api/auth/login"), downstream
            )
            blocked_response = await main_module.enforce_production_readonly(
                _request("POST", "/api/markers"), downstream
            )

        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(
            login_response.headers["X-Cloudmiddle-DB-Mode"],
            "production_readonly",
        )
        self.assertEqual(blocked_response.status_code, 503)
        self.assertEqual(
            blocked_response.headers["X-Cloudmiddle-DB-Mode"],
            "production_readonly",
        )
        self.assertIn(
            "production_readonly",
            json.loads(blocked_response.body)["detail"],
        )
        downstream.assert_awaited_once()

    async def test_read_methods_are_allowed_and_mode_header_is_added(self) -> None:
        readonly = SimpleNamespace(
            is_production_readonly=True,
            runtime_db_mode="production_readonly",
        )
        downstream = AsyncMock(return_value=JSONResponse({"ok": True}))

        with patch.object(main_module, "settings", readonly):
            response = await main_module.enforce_production_readonly(
                _request("GET", "/api/markers"), downstream
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Cloudmiddle-DB-Mode"], "production_readonly")
        downstream.assert_awaited_once()


class ProductionReadonlyStartupTests(unittest.TestCase):
    def test_startup_skips_every_bootstrap_write(self) -> None:
        readonly = SimpleNamespace(is_production_readonly=True)
        session_factory = Mock()

        with (
            patch.object(main_module, "settings", readonly),
            patch.object(main_module.Base.metadata, "create_all") as create_all,
            patch.object(main_module, "ensure_schema") as ensure_schema,
            patch.object(main_module, "seed_data") as seed_data,
            patch.object(main_module, "reconcile_proposal_tasks") as reconcile,
            patch.object(main_module, "SessionLocal", session_factory),
        ):
            main_module.on_startup()

        create_all.assert_not_called()
        ensure_schema.assert_not_called()
        seed_data.assert_not_called()
        reconcile.assert_not_called()
        session_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
