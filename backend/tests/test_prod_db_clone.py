from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.dev.prod_db_clone import (
    CloneConfig,
    CloneService,
    CloneSafetyError,
    PgEndpoint,
    PgTools,
    build_sanitization_sql,
    connection_args,
    parse_postgres_url,
    pg_environment,
    resolve_source_url,
    validate_config,
    validate_sensitive_columns,
    validate_source_preflight,
)


def endpoint(
    host: str,
    database: str,
    username: str,
    *,
    port: int = 5432,
    password: str = "db-password",
) -> PgEndpoint:
    return PgEndpoint(host, port, database, username, password, "require")


def config(**overrides: object) -> CloneConfig:
    values: dict[str, object] = {
        "source": endpoint("prod.example.rds.amazonaws.com", "cloudmiddle", "app_user"),
        "target": endpoint(
            "127.0.0.1",
            "cloudmiddle_local",
            "cloudmiddle_local",
            port=55432,
            password="local-db-password",
        ),
        "allowed_source_hosts": frozenset({"prod.example.rds.amazonaws.com"}),
        "allowed_source_databases": frozenset({"cloudmiddle"}),
        "allowed_source_users": frozenset({"app_user"}),
        "allowed_target_databases": frozenset({"cloudmiddle_local"}),
        "allowed_target_users": frozenset({"cloudmiddle_local"}),
        "allowed_target_ports": frozenset({55432}),
        "local_admin_email": "test@test.com",
        "local_admin_password": "test1234",
        "strict_source_role": False,
        "tool_mode": "docker",
        "docker_image": "postgres:16-alpine",
        "pg_bin_dir": None,
    }
    values.update(overrides)
    return CloneConfig(**values)  # type: ignore[arg-type]


class ConfigSafetyTests(unittest.TestCase):
    def test_url_decodes_password_without_putting_it_in_connection_args(self) -> None:
        parsed = parse_postgres_url(
            "postgresql+psycopg2://clone:p%40ss%3Aword@db.example/prod?sslmode=verify-full",
            label="source",
            default_sslmode="require",
        )
        self.assertEqual(parsed.password, "p@ss:word")
        self.assertNotIn(parsed.password, connection_args(parsed))
        self.assertNotIn(parsed.password, parsed.redacted)

    def test_destructive_target_requires_exact_loopback_allowlist_and_token(self) -> None:
        validate_config(
            config(), confirmation="RESET-cloudmiddle_local", destructive=True
        )
        with self.assertRaisesRegex(CloneSafetyError, "confirmation"):
            validate_config(config(), confirmation="yes", destructive=True)
        with self.assertRaisesRegex(CloneSafetyError, "loopback"):
            validate_config(
                config(target=endpoint("dev.example", "cloudmiddle_local", "cloudmiddle_local")),
                confirmation="RESET-cloudmiddle_local",
                destructive=True,
            )

    def test_source_secret_loader_supports_aws_without_exposing_secret(self) -> None:
        fake_client = SimpleNamespace(
            get_secret_value=lambda **_: {
                "SecretString": '{"DATABASE_URL":"postgresql://u:p@host/db"}'
            }
        )
        fake_boto3 = SimpleNamespace(client=lambda *_args, **_kwargs: fake_client)
        env = {
            "PROD_CLONE_AWS_SECRET_ID": "prod/database",
            "PROD_CLONE_AWS_REGION": "ap-northeast-2",
        }
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            self.assertEqual(resolve_source_url(env), "postgresql://u:p@host/db")

    def test_source_url_and_secret_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(CloneSafetyError, "exactly one"):
            resolve_source_url(
                {
                    "PROD_CLONE_SOURCE_URL": "postgresql://u:p@host/db",
                    "PROD_CLONE_AWS_SECRET_ID": "prod/database",
                }
            )


class SourceReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = {
            "role": "app_user",
            "database": "cloudmiddle",
            "read_only": True,
            "is_superuser": False,
            "can_create_database": False,
            "can_create_in_database": False,
            "can_create_in_schema": False,
            "write_grants": [],
            "tables": ["users", "cities", "markers"],
        }

    def test_writer_credential_is_allowed_with_warning_in_forced_readonly_session(self) -> None:
        data = {**self.preflight, "write_grants": ["public.markers:UPDATE"]}
        warnings = validate_source_preflight(data, config())
        self.assertEqual(len(warnings), 1)
        self.assertIn("session-read-only", warnings[0])

    def test_strict_mode_rejects_writer_credential(self) -> None:
        data = {**self.preflight, "is_superuser": True}
        with self.assertRaisesRegex(CloneSafetyError, "SELECT-only"):
            validate_source_preflight(data, config(strict_source_role=True))

    def test_transaction_readonly_is_always_required(self) -> None:
        with self.assertRaisesRegex(CloneSafetyError, "not read-only"):
            validate_source_preflight({**self.preflight, "read_only": False}, config())

    def test_source_environment_forces_readonly_and_scrubs_app_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "must-not-leak",
                "PGPASSWORD": "must-not-leak",
                "PROD_CLONE_SOURCE_URL": "must-not-leak",
            },
            clear=True,
        ):
            env = pg_environment(
                config().source, pgpass="/work/source.pgpass", read_only=True
            )
        self.assertEqual(env["PGPASSFILE"], "/work/source.pgpass")
        self.assertIn("default_transaction_read_only=on", env["PGOPTIONS"])
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn("PGPASSWORD", env)
        self.assertNotIn("PROD_CLONE_SOURCE_URL", env)


class DockerToolTests(unittest.TestCase):
    def test_docker_is_default_compatible_with_windows_host_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tools = PgTools("docker", None, "postgres:16-alpine", Path(raw))
            translated = tools.endpoint(config().target)
            command = tools.command(
                "pg_dump",
                {
                    "PGPASSFILE": "/work/source.pgpass",
                    "PGOPTIONS": "-c default_transaction_read_only=on",
                },
            )
        self.assertEqual(translated.host, "host.docker.internal")
        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertIn("host.docker.internal:host-gateway", command)
        self.assertIn("postgres:16-alpine", command)
        command_text = " ".join(command)
        self.assertIn('cp "$PGPASSFILE" /tmp/cloudmiddle.pgpass', command_text)
        self.assertIn("chmod 600 /tmp/cloudmiddle.pgpass", command_text)
        self.assertNotIn(config().source.password, command)

    def test_fresh_staging_public_schema_is_dropped_before_restore(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.calls: list[tuple[list[str], str | None, dict[str, str]]] = []

            def run(self, argv, *, env, input_text=None):  # type: ignore[no-untyped-def]
                args = list(argv)
                copied_env = dict(env)
                self.calls.append((args, input_text, copied_env))
                if "pg_dump" in args:
                    service.tools.work_dir.joinpath("production.dump").write_bytes(b"dump")
                    return ""
                sql = input_text or ""
                if "server_version_num" in sql:
                    return json.dumps(
                        {
                            "role": "app_user",
                            "database": "cloudmiddle",
                            "read_only": True,
                            "is_superuser": False,
                            "can_create_database": False,
                            "can_create_in_database": False,
                            "can_create_in_schema": False,
                            "write_grants": [],
                            "tables": ["users", "cities", "markers"],
                            "core_counts": {
                                "cities": 10,
                                "markers": 20,
                                "place_images": 30,
                                "place_insights": 40,
                                "place_chains": 50,
                            },
                        }
                    )
                if "'is_superuser'" in sql:
                    return json.dumps(
                        {
                            "role": "cloudmiddle_local",
                            "is_superuser": True,
                            "can_create_database": True,
                        }
                    )
                if "information_schema.columns" in sql:
                    return "users.email\nusers.password_hash\n"
                if "'restored_core_counts'" in sql:
                    return json.dumps(
                        {
                            "restored_core_counts": {
                                "cities": 1,
                                "markers": 2,
                                "place_images": 3,
                                "place_insights": 4,
                                "place_chains": 5,
                            }
                        }
                    )
                if "'admin_count'" in sql:
                    return json.dumps(
                        {
                            "core_counts": {
                                "cities": 1,
                                "markers": 2,
                                "place_images": 3,
                                "place_insights": 4,
                                "place_chains": 5,
                            },
                            "admin_count": 1,
                            "unmasked_users": 0,
                        }
                    )
                if "SELECT EXISTS" in sql:
                    return (
                        "true\n"
                        if "datname = 'cloudmiddle_local'" in sql
                        else "false\n"
                    )
                return ""

        fake = FakeRunner()
        service = CloneService(config(), runner=fake)  # type: ignore[arg-type]
        with patch("tools.dev.prod_db_clone._hash_password", return_value="$2b$fake"):
            result = service.run(retain_private_content=False)
        # Source preflight and pg_dump are separate read-only snapshots.  Normal
        # production writes between them must not cause a false failure.
        self.assertEqual(result["core_counts"]["markers"], 2)
        self.assertIsNone(result["backup_database"])
        drop_index = next(
            i
            for i, (_argv, sql, _env) in enumerate(fake.calls)
            if sql == "DROP SCHEMA public CASCADE;"
        )
        restore_index = next(
            i
            for i, (argv, _sql, _env) in enumerate(fake.calls)
            if "pg_restore" in argv and "--single-transaction" in argv
        )
        self.assertLess(drop_index, restore_index)
        self.assertTrue(
            any(
                sql
                == 'DROP DATABASE IF EXISTS '
                '"cloudmiddle_local_before_prod_clone";'
                for _argv, sql, _env in fake.calls
            )
        )
        dump_call = next(call for call in fake.calls if "--format=custom" in call[0])
        self.assertIn("default_transaction_read_only=on", dump_call[2]["PGOPTIONS"])

    def test_existing_target_and_staging_are_renamed_in_one_transaction(self) -> None:
        service = CloneService(config())
        target_name = "cloudmiddle_local"
        backup_name = "cloudmiddle_local_before_prod_clone"
        with (
            patch.object(
                service,
                "_database_exists",
                side_effect=lambda name, _env: name == target_name,
            ),
            patch.object(service, "_terminate") as terminate,
            patch("tools.dev.prod_db_clone._psql", return_value="") as psql,
        ):
            backup = service._swap("cloudmiddle_local_clone_deadbeef", {})
        self.assertEqual(backup, backup_name)
        terminate.assert_called_once_with(target_name, {})
        sql = psql.call_args.args[4]
        self.assertTrue(sql.startswith("BEGIN;"))
        self.assertIn(
            'ALTER DATABASE "cloudmiddle_local" '
            'RENAME TO "cloudmiddle_local_before_prod_clone";',
            sql,
        )
        self.assertIn(
            'ALTER DATABASE "cloudmiddle_local_clone_deadbeef" '
            'RENAME TO "cloudmiddle_local";',
            sql,
        )
        self.assertTrue(sql.endswith("COMMIT;"))

    def test_retain_then_safe_run_removes_private_rollback_database(self) -> None:
        service = CloneService(config())
        backup = "cloudmiddle_local_before_prod_clone"
        target_env = {"PGPASSFILE": "/work/target.pgpass"}
        with patch.object(service, "_drop_database") as drop_database:
            retained = service._apply_backup_retention(
                backup,
                target_env,
                retain_private_content=True,
            )
            self.assertEqual(retained, backup)
            drop_database.assert_not_called()

            safe_result = service._apply_backup_retention(
                retained,
                target_env,
                retain_private_content=False,
            )
        self.assertIsNone(safe_result)
        drop_database.assert_called_once_with(backup, target_env)


class SanitizationTests(unittest.TestCase):
    def sanitizer(self, *, retain: bool) -> str:
        return build_sanitization_sql(
            local_admin_email="test@test.com",
            local_admin_hash="$2b$admin-hash",
            disabled_password_hash="$2b$disabled-hash",
            retain_private_content=retain,
        )

    def test_admin_prefers_existing_test_row_then_falls_back_to_lowest_id(self) -> None:
        sql = self.sanitizer(retain=False)
        email_lookup = sql.index("WHERE lower(email) = lower('test@test.com')")
        lowest_id = sql.index("FROM public.users ORDER BY id LIMIT 1")
        mask = sql.index("SET email = 'user-' || id::text || '@local.invalid'")
        self.assertLess(email_lookup, lowest_id)
        self.assertLess(lowest_id, mask)
        self.assertIn("password_hash = '$2b$admin-hash'", sql)
        self.assertNotIn("test1234", sql)

    def test_safe_mode_removes_private_and_agent_operational_data(self) -> None:
        sql = self.sanitizer(retain=False)
        for table in (
            "user_messages",
            "place_appeals",
            "place_notes",
            "place_favorites",
            "travel_plans",
            "travel_chat_messages",
            "agent_runs",
            "agent_run_steps",
        ):
            self.assertIn(f"DELETE FROM public.{table};", sql)
        self.assertIn("UPDATE public.markers SET user_id = NULL;", sql)

    def test_private_content_requires_explicit_retain_mode(self) -> None:
        sql = self.sanitizer(retain=True)
        self.assertNotIn("DELETE FROM public.user_messages", sql)
        self.assertIn("SET email = 'user-' || id::text || '@local.invalid'", sql)

    def test_unknown_future_token_column_fails_closed(self) -> None:
        validate_sensitive_columns(["users.email", "users.password_hash"])
        with self.assertRaisesRegex(CloneSafetyError, "api_token"):
            validate_sensitive_columns(
                ["users.email", "users.password_hash", "users.api_token"]
            )


if __name__ == "__main__":
    unittest.main()
