"""Safely clone the production PostgreSQL public schema into a local database.

The command is deliberately separate from the application configuration.  It
forces every production connection into read-only mode and refuses every target
except an explicitly allow-listed loopback PostgreSQL database.  A custom dump
is restored and sanitized in a staging database before the local database is
swapped, so a failed dump or restore never destroys the current local copy.

Run ``python -m tools.dev.prod_db_clone --help`` from ``backend``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit


class CloneSafetyError(RuntimeError):
    """Raised before or during a clone when a safety invariant is violated."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SENSITIVE_COLUMN_RE = re.compile(
    r"(?:^|_)(?:email|password|passwd|token|session|secret|api_key|credential)(?:$|_)",
    re.IGNORECASE,
)
_KNOWN_SENSITIVE_COLUMNS = frozenset({"users.email", "users.password_hash"})
_CORE_COUNT_TABLES = (
    "cities",
    "markers",
    "place_images",
    "place_insights",
    "place_chains",
)


def _csv(value: str) -> frozenset[str]:
    return frozenset(part.strip().casefold() for part in value.split(",") if part.strip())


def _csv_int(value: str) -> frozenset[int]:
    try:
        return frozenset(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise CloneSafetyError("port allow-list must contain only integers") from exc


def _identifier(value: str, *, label: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise CloneSafetyError(f"unsafe PostgreSQL {label}: {value!r}")
    return value


def _quote_ident(value: str) -> str:
    return f'"{_identifier(value, label="identifier")}"'


def _quote_literal(value: str) -> str:
    if "\x00" in value:
        raise CloneSafetyError("NUL is not allowed in SQL values")
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class PgEndpoint:
    host: str
    port: int
    database: str
    username: str
    password: str
    sslmode: str

    @property
    def canonical(self) -> tuple[str, int, str]:
        return self.host.casefold(), self.port, self.database.casefold()

    @property
    def redacted(self) -> str:
        return (
            f"postgresql://{self.username}:***@{self.host}:{self.port}/"
            f"{self.database}?sslmode={self.sslmode}"
        )


def parse_postgres_url(raw: str, *, label: str, default_sslmode: str) -> PgEndpoint:
    """Parse app-style or libpq-style URLs without passing secrets to argv."""

    value = raw.strip()
    if not value:
        raise CloneSafetyError(f"{label} URL is required")
    parsed = urlsplit(value)
    scheme = parsed.scheme.casefold()
    if scheme not in {"postgres", "postgresql", "postgresql+psycopg2"}:
        raise CloneSafetyError(f"{label} must be a PostgreSQL URL")
    host = (parsed.hostname or "").strip().casefold()
    database = unquote(parsed.path.lstrip("/"))
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not host or not database or not username or not password:
        raise CloneSafetyError(
            f"{label} URL must include host, database, username, and password"
        )
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise CloneSafetyError(f"{label} URL has an invalid port") from exc
    query = parse_qs(parsed.query, keep_blank_values=False)
    sslmode = str(query.get("sslmode", [default_sslmode])[-1]).casefold()
    if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise CloneSafetyError(f"{label} URL has an invalid sslmode")
    return PgEndpoint(
        host=host,
        port=port,
        database=_identifier(database, label=f"{label} database"),
        username=_identifier(username, label=f"{label} username"),
        password=password,
        sslmode=sslmode,
    )


@dataclass(frozen=True)
class CloneConfig:
    source: PgEndpoint
    target: PgEndpoint
    allowed_source_hosts: frozenset[str]
    allowed_source_databases: frozenset[str]
    allowed_source_users: frozenset[str]
    allowed_target_databases: frozenset[str]
    allowed_target_users: frozenset[str]
    allowed_target_ports: frozenset[int]
    local_admin_email: str
    local_admin_password: str
    strict_source_role: bool
    tool_mode: str
    docker_image: str
    pg_bin_dir: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CloneConfig":
        values = os.environ if env is None else env

        def required(name: str) -> str:
            value = str(values.get(name) or "").strip()
            if not value:
                raise CloneSafetyError(f"{name} is required")
            return value

        source = parse_postgres_url(
            resolve_source_url(values),
            label="source",
            default_sslmode="require",
        )
        target = parse_postgres_url(
            required("PROD_CLONE_TARGET_URL"),
            label="target",
            default_sslmode="disable",
        )
        pg_bin_raw = str(values.get("PROD_CLONE_PG_BIN") or "").strip()
        return cls(
            source=source,
            target=target,
            allowed_source_hosts=_csv(required("PROD_CLONE_ALLOWED_SOURCE_HOSTS")),
            allowed_source_databases=_csv(required("PROD_CLONE_ALLOWED_SOURCE_DBS")),
            allowed_source_users=_csv(required("PROD_CLONE_ALLOWED_SOURCE_USERS")),
            allowed_target_databases=_csv(
                str(values.get("PROD_CLONE_ALLOWED_TARGET_DBS") or "cloudmiddle_local")
            ),
            allowed_target_users=_csv(
                str(values.get("PROD_CLONE_ALLOWED_TARGET_USERS") or "cloudmiddle_local")
            ),
            allowed_target_ports=_csv_int(
                str(values.get("PROD_CLONE_ALLOWED_TARGET_PORTS") or "55432")
            ),
            local_admin_email=str(
                values.get("PROD_CLONE_LOCAL_ADMIN_EMAIL") or "test@test.com"
            ).strip().casefold(),
            local_admin_password=str(
                values.get("PROD_CLONE_LOCAL_ADMIN_PASSWORD") or "test1234"
            ),
            strict_source_role=str(
                values.get("PROD_CLONE_STRICT_SOURCE_ROLE") or "false"
            ).strip().casefold() in {"1", "true", "yes", "on"},
            tool_mode=str(values.get("PROD_CLONE_TOOL_MODE") or "docker").strip().casefold(),
            docker_image=str(
                values.get("PROD_CLONE_DOCKER_IMAGE") or "postgres:16-alpine"
            ).strip(),
            pg_bin_dir=Path(pg_bin_raw).resolve() if pg_bin_raw else None,
        )


def resolve_source_url(values: Mapping[str, str]) -> str:
    """Load the source URL directly or from Secrets Manager without logging it."""

    direct = str(values.get("PROD_CLONE_SOURCE_URL") or "").strip()
    secret_id = str(values.get("PROD_CLONE_AWS_SECRET_ID") or "").strip()
    if direct and secret_id:
        raise CloneSafetyError(
            "set exactly one of PROD_CLONE_SOURCE_URL or PROD_CLONE_AWS_SECRET_ID"
        )
    if direct:
        return direct
    if not secret_id:
        raise CloneSafetyError(
            "PROD_CLONE_SOURCE_URL or PROD_CLONE_AWS_SECRET_ID is required"
        )
    region = str(
        values.get("PROD_CLONE_AWS_REGION")
        or values.get("AWS_REGION")
        or values.get("AWS_DEFAULT_REGION")
        or ""
    ).strip()
    if not region:
        raise CloneSafetyError("AWS region is required when loading the source secret")
    secret_key = str(values.get("PROD_CLONE_AWS_SECRET_KEY") or "DATABASE_URL").strip()
    try:
        import boto3

        response = boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=secret_id
        )
        payload = json.loads(str(response.get("SecretString") or ""))
        value = str(payload.get(secret_key) or "").strip()
    except Exception as exc:
        raise CloneSafetyError("failed to load the production database secret") from exc
    if not value:
        raise CloneSafetyError(f"production secret does not contain {secret_key!r}")
    return value


def validate_config(
    config: CloneConfig,
    *,
    confirmation: str | None,
    destructive: bool,
) -> None:
    """Fail closed before invoking any command, especially a destructive one."""

    source = config.source
    target = config.target
    if source.host in _LOOPBACK_HOSTS:
        raise CloneSafetyError("production source must not be a loopback host")
    if source.host not in config.allowed_source_hosts:
        raise CloneSafetyError("source host is not explicitly allow-listed")
    if source.database.casefold() not in config.allowed_source_databases:
        raise CloneSafetyError("source database is not explicitly allow-listed")
    if source.username.casefold() not in config.allowed_source_users:
        raise CloneSafetyError("source username is not explicitly allow-listed")
    if target.host not in _LOOPBACK_HOSTS:
        raise CloneSafetyError("clone target must be localhost/loopback")
    if target.database.casefold() not in config.allowed_target_databases:
        raise CloneSafetyError("target database is not explicitly allow-listed")
    if target.username.casefold() not in config.allowed_target_users:
        raise CloneSafetyError("target username is not explicitly allow-listed")
    if target.port not in config.allowed_target_ports:
        raise CloneSafetyError("target port is not explicitly allow-listed")
    if target.database.casefold() in {"postgres", "template0", "template1"}:
        raise CloneSafetyError("maintenance/template databases can never be clone targets")
    if source.canonical == target.canonical:
        raise CloneSafetyError("source and target resolve to the same database")
    if len(config.local_admin_password) < 8:
        raise CloneSafetyError("local admin password must be at least 8 characters")
    if config.local_admin_password in {source.password, target.password}:
        raise CloneSafetyError("local admin password must differ from database passwords")
    if "@" not in config.local_admin_email:
        raise CloneSafetyError("admin emails are invalid")
    if config.tool_mode not in {"docker", "native"}:
        raise CloneSafetyError("PROD_CLONE_TOOL_MODE must be 'docker' or 'native'")
    if config.tool_mode == "docker" and not config.docker_image:
        raise CloneSafetyError("PROD_CLONE_DOCKER_IMAGE is required in docker mode")
    if destructive:
        expected = f"RESET-{target.database}"
        if confirmation != expected:
            raise CloneSafetyError(f"destructive confirmation must be exactly {expected!r}")


class CommandRunner:
    """Small injectable subprocess boundary; argv must never contain passwords."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        input_text: str | None = None,
    ) -> str:
        completed = subprocess.run(
            list(argv),
            env=dict(env),
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            command = " ".join(str(part) for part in argv)
            detail = (completed.stderr or completed.stdout or "unknown PostgreSQL error")[-3000:]
            raise CloneSafetyError(f"command failed: {command}\n{detail}")
        return completed.stdout or ""


@dataclass(frozen=True)
class PgTools:
    mode: str
    bin_dir: Path | None
    docker_image: str
    work_dir: Path

    def command(self, name: str, env: Mapping[str, str]) -> list[str]:
        if self.mode == "docker":
            argv = [
                "docker", "run", "--rm", "-i",
                "--add-host", "host.docker.internal:host-gateway",
                "--volume", f"{self.work_dir}:/work",
                "--workdir", "/work",
            ]
            for key in ("PGPASSFILE", "PGSSLMODE", "PGCONNECT_TIMEOUT", "PGOPTIONS"):
                if key in env:
                    argv.extend(["--env", key])
            # Windows bind mounts do not preserve POSIX 0600 permissions, and
            # libpq correctly refuses an exposed .pgpass.  Copy it inside the
            # short-lived container and lock its mode before exec; no password
            # appears in argv, logs, or Docker metadata.
            argv.extend(
                [
                    self.docker_image,
                    "sh",
                    "-c",
                    (
                        'cp "$PGPASSFILE" /tmp/cloudmiddle.pgpass && '
                        "chmod 600 /tmp/cloudmiddle.pgpass && "
                        "export PGPASSFILE=/tmp/cloudmiddle.pgpass && "
                        'exec "$@"'
                    ),
                    "clone-helper",
                    name,
                ]
            )
            return argv
        filename = name + (".exe" if os.name == "nt" else "")
        return [str(self.bin_dir / filename) if self.bin_dir else name]

    def endpoint(self, endpoint: PgEndpoint) -> PgEndpoint:
        if self.mode != "docker" or endpoint.host not in _LOOPBACK_HOSTS:
            return endpoint
        return PgEndpoint(
            host="host.docker.internal",
            port=endpoint.port,
            database=endpoint.database,
            username=endpoint.username,
            password=endpoint.password,
            sslmode=endpoint.sslmode,
        )

    def path(self, path: Path) -> str:
        if self.mode == "docker":
            return f"/work/{path.name}"
        return str(path)


def _pgpass_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def write_pgpass(path: Path, endpoint: PgEndpoint) -> None:
    fields = (
        endpoint.host,
        str(endpoint.port),
        "*",
        endpoint.username,
        endpoint.password,
    )
    path.write_text(":".join(_pgpass_value(item) for item in fields) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def pg_environment(
    endpoint: PgEndpoint,
    *,
    pgpass: str | Path,
    read_only: bool,
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PROD_CLONE_") and key not in {"PGPASSWORD", "DATABASE_URL"}
    }
    env.update(
        {
            "PGPASSFILE": str(pgpass),
            "PGSSLMODE": endpoint.sslmode,
            "PGCONNECT_TIMEOUT": "15",
        }
    )
    if read_only:
        env["PGOPTIONS"] = (
            "-c default_transaction_read_only=on "
            "-c statement_timeout=300000 -c lock_timeout=30000"
        )
    return env


def connection_args(endpoint: PgEndpoint, *, database: str | None = None) -> list[str]:
    return [
        "--host", endpoint.host,
        "--port", str(endpoint.port),
        "--username", endpoint.username,
        "--dbname", database or endpoint.database,
    ]


def _json_from_psql(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            value = json.loads(line)
            if isinstance(value, dict):
                return value
    raise CloneSafetyError("PostgreSQL preflight did not return JSON")


def _psql(
    runner: CommandRunner,
    tools: PgTools,
    endpoint: PgEndpoint,
    env: Mapping[str, str],
    sql: str,
    *,
    database: str | None = None,
) -> str:
    effective = tools.endpoint(endpoint)
    argv = [
        *tools.command("psql", env),
        "--no-psqlrc",
        "--set", "ON_ERROR_STOP=1",
        "--quiet",
        "--tuples-only",
        "--no-align",
        *connection_args(effective, database=database),
        "--file=-",
    ]
    return runner.run(argv, env=env, input_text=sql)


_SOURCE_PREFLIGHT_SQL = """
SELECT json_build_object(
  'role', current_user,
  'database', current_database(),
  'read_only', current_setting('transaction_read_only') = 'on',
  'server_version_num', current_setting('server_version_num')::integer,
  'is_superuser', COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false),
  'can_create_database', COALESCE((SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user), false),
  'can_create_in_database', has_database_privilege(current_user, current_database(), 'CREATE'),
  'can_create_in_schema', EXISTS (
    SELECT 1 FROM information_schema.schemata s
    WHERE s.schema_name NOT LIKE 'pg_%'
      AND s.schema_name <> 'information_schema'
      AND has_schema_privilege(current_user, quote_ident(s.schema_name), 'CREATE')
  ),
  'write_grants', COALESCE((
    SELECT json_agg(table_schema || '.' || table_name || ':' || privilege_type)
    FROM information_schema.role_table_grants
    WHERE grantee = current_user
      AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER')
  ), '[]'::json),
  'tables', COALESCE((
    SELECT json_agg(table_name ORDER BY table_name)
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
  ), '[]'::json),
  'core_counts', json_build_object(
    'cities', (SELECT count(*) FROM public.cities),
    'markers', (SELECT count(*) FROM public.markers),
    'place_images', (SELECT count(*) FROM public.place_images),
    'place_insights', (SELECT count(*) FROM public.place_insights),
    'place_chains', (SELECT count(*) FROM public.place_chains)
  )
)::text;
"""


def validate_source_preflight(
    data: Mapping[str, Any], config: CloneConfig
) -> list[str]:
    if str(data.get("role") or "").casefold() not in config.allowed_source_users:
        raise CloneSafetyError("source server returned an unexpected role")
    if str(data.get("database") or "").casefold() not in config.allowed_source_databases:
        raise CloneSafetyError("source server returned an unexpected database")
    if data.get("read_only") is not True:
        raise CloneSafetyError("source transaction is not read-only")
    elevated = (
        bool(data.get("is_superuser"))
        or bool(data.get("can_create_database"))
        or bool(data.get("can_create_in_database"))
        or bool(data.get("can_create_in_schema"))
        or bool(data.get("write_grants"))
    )
    warnings: list[str] = []
    if elevated and config.strict_source_role:
        raise CloneSafetyError(
            "source credential is not read-only; create a dedicated SELECT-only role"
        )
    if elevated:
        warnings.append(
            "source role has write/elevated grants; this run is session-read-only, "
            "but a dedicated SELECT-only clone role is strongly recommended"
        )
    tables = {str(item) for item in (data.get("tables") or [])}
    required = {"users", "cities", "markers"}
    if not required.issubset(tables):
        raise CloneSafetyError("source schema is missing required application tables")
    return warnings


def sensitive_columns_from_rows(rows: Iterable[str]) -> set[str]:
    sensitive: set[str] = set()
    for raw in rows:
        value = raw.strip().casefold()
        if not value or "." not in value:
            continue
        _, column = value.split(".", 1)
        if _SENSITIVE_COLUMN_RE.search(column):
            sensitive.add(value)
    return sensitive


def validate_sensitive_columns(columns: Iterable[str]) -> None:
    unexpected = sensitive_columns_from_rows(columns) - _KNOWN_SENSITIVE_COLUMNS
    if unexpected:
        raise CloneSafetyError(
            "restored schema has unhandled credential/session columns: "
            + ", ".join(sorted(unexpected))
        )


def build_sanitization_sql(
    *,
    local_admin_email: str,
    local_admin_hash: str,
    disabled_password_hash: str,
    retain_private_content: bool,
) -> str:
    """Build one transactional sanitizer; plaintext passwords never enter SQL."""

    statements = [
        "BEGIN;",
        "DO $clone_sanitize$",
        "DECLARE local_admin_id integer;",
        "BEGIN",
        (
            "  SELECT id INTO local_admin_id FROM public.users "
            f"WHERE lower(email) = lower({_quote_literal(local_admin_email)}) "
            "ORDER BY id LIMIT 1;"
        ),
        "  IF local_admin_id IS NULL THEN",
        "    SELECT id INTO local_admin_id FROM public.users ORDER BY id LIMIT 1;",
        "  END IF;",
        "  UPDATE public.users",
        "  SET email = 'user-' || id::text || '@local.invalid',",
        "      display_name = 'Local User ' || id::text,",
        f"      password_hash = {_quote_literal(disabled_password_hash)};",
        "  IF local_admin_id IS NULL THEN",
        (
            "    INSERT INTO public.users (email, display_name, password_hash) VALUES ("
            f"{_quote_literal(local_admin_email)}, 'Local Admin', "
            f"{_quote_literal(local_admin_hash)}) RETURNING id INTO local_admin_id;"
        ),
        "  ELSE",
        "    UPDATE public.users",
        (
            f"    SET email = {_quote_literal(local_admin_email)}, "
            f"display_name = 'Local Admin', password_hash = {_quote_literal(local_admin_hash)}"
        ),
        "    WHERE id = local_admin_id;",
        "  END IF;",
        "END",
        "$clone_sanitize$;",
    ]
    if not retain_private_content:
        # Direct messages and operational traces can echo user text.  Safe mode
        # intentionally keeps shared map/content tables but removes these rows.
        statements.extend(
            [
                "DELETE FROM public.user_messages;",
                "DELETE FROM public.place_appeals;",
                "DELETE FROM public.place_notes;",
                "DELETE FROM public.place_favorites;",
                "DELETE FROM public.place_contributors;",
                "UPDATE public.markers SET user_id = NULL;",
                "UPDATE public.place_images SET uploaded_by_user_id = NULL;",
                "UPDATE public.place_chains SET created_by_user_id = NULL;",
                "UPDATE public.agent_proposals SET decided_by_user_id = NULL;",
                "DELETE FROM public.travel_chat_messages;",
                "DELETE FROM public.travel_chat_work;",
                "DELETE FROM public.travel_plans;",
                "DELETE FROM public.place_events;",
                "DELETE FROM public.agent_knowledge_uses;",
                "DELETE FROM public.agent_checkpoints;",
                "DELETE FROM public.agent_evidence;",
                "DELETE FROM public.agent_run_steps;",
                "UPDATE public.agent_missions SET last_run_id = NULL;",
                "UPDATE public.agent_work_items SET last_run_id = NULL;",
                "DELETE FROM public.agent_runs;",
                "DELETE FROM public.agent_work_items;",
                "DELETE FROM public.agent_missions;",
                "DELETE FROM public.agent_tasks;",
                "DELETE FROM public.agent_lessons;",
                "DELETE FROM public.agent_proposals;",
                "DELETE FROM public.agent_search_logs;",
                "DELETE FROM public.agent_search_results;",
                "DELETE FROM public.agent_web_visits;",
                "DELETE FROM public.agent_knowledge_archive;",
                "DELETE FROM public.agent_knowledge;",
            ]
        )
    statements.extend(["COMMIT;", ""])
    return "\n".join(statements)


def _hash_password(password: str) -> str:
    from passlib.context import CryptContext

    return CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password)


class CloneLock:
    """Cross-process host lock held for the complete clone and database swap."""

    def __init__(self, target: PgEndpoint):
        digest = hashlib.sha256(
            f"{target.host}:{target.port}:{target.database}".encode("utf-8")
        ).hexdigest()[:16]
        self.path = Path(tempfile.gettempdir()) / f"cloudmiddle-prod-clone-{digest}.lock"
        self.handle: Any = None

    def __enter__(self) -> "CloneLock":
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
            self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            raise CloneSafetyError("another production clone is already running") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is None:
            return
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


class CloneService:
    def __init__(self, config: CloneConfig, *, runner: CommandRunner | None = None):
        self.config = config
        self.runner = runner or CommandRunner()
        self.tools = PgTools(
            config.tool_mode, config.pg_bin_dir, config.docker_image, Path.cwd()
        )

    def _tool_versions(self, env: Mapping[str, str]) -> None:
        for name in ("psql", "pg_dump", "pg_restore"):
            self.runner.run([*self.tools.command(name, env), "--version"], env=env)

    def _source_preflight(self, env: Mapping[str, str]) -> dict[str, Any]:
        output = _psql(
            self.runner,
            self.tools,
            self.config.source,
            env,
            _SOURCE_PREFLIGHT_SQL,
        )
        data = _json_from_psql(output)
        data["warnings"] = validate_source_preflight(data, self.config)
        return data

    def _destination_preflight(self, env: Mapping[str, str]) -> dict[str, Any]:
        sql = """
SELECT json_build_object(
  'role', current_user,
  'is_superuser', COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false),
  'can_create_database', COALESCE((SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user), false)
)::text;
"""
        data = _json_from_psql(
            _psql(
                self.runner,
                self.tools,
                self.config.target,
                env,
                sql,
                database="postgres",
            )
        )
        if str(data.get("role") or "").casefold() not in self.config.allowed_target_users:
            raise CloneSafetyError("local server returned an unexpected role")
        if not (data.get("is_superuser") or data.get("can_create_database")):
            raise CloneSafetyError("local clone role needs CREATEDB for staging restore")
        return data

    def _database_exists(self, name: str, env: Mapping[str, str]) -> bool:
        sql = (
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = "
            f"{_quote_literal(name)})::text;"
        )
        output = _psql(
            self.runner,
            self.tools,
            self.config.target,
            env,
            sql,
            database="postgres",
        )
        return any(line.strip().casefold() == "true" for line in output.splitlines())

    def _terminate(self, name: str, env: Mapping[str, str]) -> None:
        sql = (
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = {_quote_literal(name)} AND pid <> pg_backend_pid();"
        )
        _psql(
            self.runner,
            self.tools,
            self.config.target,
            env,
            sql,
            database="postgres",
        )

    def _drop_database(self, name: str, env: Mapping[str, str]) -> None:
        self._terminate(name, env)
        _psql(
            self.runner,
            self.tools,
            self.config.target,
            env,
            f"DROP DATABASE IF EXISTS {_quote_ident(name)};",
            database="postgres",
        )

    def _rename_database(self, old: str, new: str, env: Mapping[str, str]) -> None:
        _psql(
            self.runner,
            self.tools,
            self.config.target,
            env,
            f"ALTER DATABASE {_quote_ident(old)} RENAME TO {_quote_ident(new)};",
            database="postgres",
        )

    def _swap(self, staging: str, env: Mapping[str, str]) -> str | None:
        target = self.config.target.database
        backup = _identifier(f"{target}_before_prod_clone", label="backup database")
        target_existed = self._database_exists(target, env)
        if self._database_exists(backup, env):
            self._drop_database(backup, env)
        if target_existed:
            self._terminate(target, env)
            # Both renames are one PostgreSQL transaction.  A command failure,
            # process crash, or lost connection cannot leave only the backup
            # name visible: PostgreSQL rolls the whole swap back.
            _psql(
                self.runner,
                self.tools,
                self.config.target,
                env,
                "\n".join(
                    [
                        "BEGIN;",
                        (
                            f"ALTER DATABASE {_quote_ident(target)} "
                            f"RENAME TO {_quote_ident(backup)};"
                        ),
                        (
                            f"ALTER DATABASE {_quote_ident(staging)} "
                            f"RENAME TO {_quote_ident(target)};"
                        ),
                        "COMMIT;",
                    ]
                ),
                database="postgres",
            )
        else:
            self._rename_database(staging, target, env)
        return backup if target_existed else None

    def _apply_backup_retention(
        self,
        backup: str | None,
        env: Mapping[str, str],
        *,
        retain_private_content: bool,
    ) -> str | None:
        """Keep rollback data only under the explicit private-retention contract."""

        if backup is None or retain_private_content:
            return backup
        # A prior target may itself be the result of a diagnostic retain run.
        # Safe mode is not successful until that private rollback copy is gone.
        self._drop_database(backup, env)
        return None

    def _schema_columns(self, database: str, env: Mapping[str, str]) -> list[str]:
        output = _psql(
            self.runner,
            self.tools,
            self.config.target,
            env,
            """
SELECT table_name || '.' || column_name
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;
""",
            database=database,
        )
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _core_counts(self, database: str, env: Mapping[str, str]) -> dict[str, int]:
        data = _json_from_psql(
            _psql(
                self.runner,
                self.tools,
                self.config.target,
                env,
                """
SELECT json_build_object(
  'restored_core_counts', json_build_object(
    'cities', (SELECT count(*) FROM public.cities),
    'markers', (SELECT count(*) FROM public.markers),
    'place_images', (SELECT count(*) FROM public.place_images),
    'place_insights', (SELECT count(*) FROM public.place_insights),
    'place_chains', (SELECT count(*) FROM public.place_chains)
  )
)::text;
""",
                database=database,
            )
        )
        counts = {
            key: int(value)
            for key, value in dict(data.get("restored_core_counts") or {}).items()
            if key in _CORE_COUNT_TABLES
        }
        if set(counts) != set(_CORE_COUNT_TABLES):
            raise CloneSafetyError("restored database is missing core table counts")
        return counts

    def _verify_restored(
        self,
        database: str,
        env: Mapping[str, str],
        restored_core_counts: Mapping[str, int],
    ) -> None:
        local_email = _quote_literal(self.config.local_admin_email)
        sql = f"""
SELECT json_build_object(
  'core_counts', json_build_object(
    'cities', (SELECT count(*) FROM public.cities),
    'markers', (SELECT count(*) FROM public.markers),
    'place_images', (SELECT count(*) FROM public.place_images),
    'place_insights', (SELECT count(*) FROM public.place_insights),
    'place_chains', (SELECT count(*) FROM public.place_chains)
  ),
  'admin_count', (SELECT count(*) FROM public.users WHERE lower(email) = lower({local_email})),
  'unmasked_users', (
    SELECT count(*) FROM public.users
    WHERE lower(email) <> lower({local_email})
      AND email !~ '^user-[0-9]+@local\\.invalid$'
  )
)::text;
"""
        data = _json_from_psql(
            _psql(
                self.runner,
                self.tools,
                self.config.target,
                env,
                sql,
                database=database,
            )
        )
        target_counts = {
            key: int(value)
            for key, value in dict(data.get("core_counts") or {}).items()
            if key in _CORE_COUNT_TABLES
        }
        if dict(restored_core_counts) != target_counts:
            raise CloneSafetyError("sanitization changed one or more core table counts")
        if int(data.get("admin_count") or 0) != 1:
            raise CloneSafetyError("local admin reset verification failed")
        if int(data.get("unmasked_users") or 0) != 0:
            raise CloneSafetyError("one or more production emails survived sanitization")

    def run(self, *, retain_private_content: bool) -> dict[str, Any]:
        with CloneLock(self.config.target), tempfile.TemporaryDirectory(
            prefix="cloudmiddle-prod-clone-"
        ) as temp_raw:
            temp = Path(temp_raw)
            self.tools = PgTools(
                self.config.tool_mode,
                self.config.pg_bin_dir,
                self.config.docker_image,
                temp,
            )
            source_pass = temp / "source.pgpass"
            target_pass = temp / "target.pgpass"
            dump_path = temp / "production.dump"
            write_pgpass(source_pass, self.tools.endpoint(self.config.source))
            write_pgpass(target_pass, self.tools.endpoint(self.config.target))
            source_env = pg_environment(
                self.config.source,
                pgpass=self.tools.path(source_pass),
                read_only=True,
            )
            target_env = pg_environment(
                self.config.target,
                pgpass=self.tools.path(target_pass),
                read_only=False,
            )
            self._tool_versions(target_env)
            source_preflight = self._source_preflight(source_env)

            dump_argv = [
                *self.tools.command("pg_dump", source_env),
                *connection_args(self.tools.endpoint(self.config.source)),
                "--schema=public",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--serializable-deferrable",
                "--file", self.tools.path(dump_path),
            ]
            self.runner.run(dump_argv, env=source_env)
            if not dump_path.is_file() or dump_path.stat().st_size == 0:
                raise CloneSafetyError("pg_dump produced an empty dump")
            with contextlib.suppress(OSError):
                dump_path.chmod(0o600)
            self.runner.run(
                [
                    *self.tools.command("pg_restore", target_env),
                    "--list",
                    self.tools.path(dump_path),
                ],
                env=target_env,
            )
            self._destination_preflight(target_env)

            suffix = secrets.token_hex(4)
            prefix = self.config.target.database[:48]
            staging = _identifier(f"{prefix}_clone_{suffix}", label="staging database")
            created = False
            swapped = False
            try:
                create_sql = (
                    f"CREATE DATABASE {_quote_ident(staging)} "
                    f"OWNER {_quote_ident(self.config.target.username)} TEMPLATE template0;"
                )
                _psql(
                    self.runner,
                    self.tools,
                    self.config.target,
                    target_env,
                    create_sql,
                    database="postgres",
                )
                created = True
                # PostgreSQL 15+ template0 already owns an empty public schema,
                # while a schema-filtered pg_dump contains CREATE SCHEMA public.
                # The database is a fresh, uniquely named staging target only.
                _psql(
                    self.runner,
                    self.tools,
                    self.config.target,
                    target_env,
                    "DROP SCHEMA public CASCADE;",
                    database=staging,
                )
                restore_argv = [
                    *self.tools.command("pg_restore", target_env),
                    *connection_args(
                        self.tools.endpoint(self.config.target), database=staging
                    ),
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    self.tools.path(dump_path),
                ]
                self.runner.run(restore_argv, env=target_env)
                validate_sensitive_columns(self._schema_columns(staging, target_env))
                restored_core_counts = self._core_counts(staging, target_env)
                source_counts = {
                    key: int(value)
                    for key, value in dict(
                        source_preflight.get("core_counts") or {}
                    ).items()
                    if key in _CORE_COUNT_TABLES
                }
                # The source remains active between preflight and pg_dump, so
                # exact cross-session counts are inherently racy.  Still reject
                # a clearly empty dump when the preflight saw core map data.
                for required_table in ("cities", "markers"):
                    if (
                        source_counts.get(required_table, 0) > 0
                        and restored_core_counts.get(required_table, 0) == 0
                    ):
                        raise CloneSafetyError(
                            f"restored {required_table} is unexpectedly empty"
                        )

                admin_hash = _hash_password(self.config.local_admin_password)
                disabled_hash = _hash_password(secrets.token_urlsafe(48))
                sanitize_sql = build_sanitization_sql(
                    local_admin_email=self.config.local_admin_email,
                    local_admin_hash=admin_hash,
                    disabled_password_hash=disabled_hash,
                    retain_private_content=retain_private_content,
                )
                _psql(
                    self.runner,
                    self.tools,
                    self.config.target,
                    target_env,
                    sanitize_sql,
                    database=staging,
                )
                self._verify_restored(staging, target_env, restored_core_counts)
                backup = self._swap(staging, target_env)
                swapped = True
                backup = self._apply_backup_retention(
                    backup,
                    target_env,
                    retain_private_content=retain_private_content,
                )
            finally:
                if created and not swapped:
                    with contextlib.suppress(Exception):
                        if self._database_exists(staging, target_env):
                            self._drop_database(staging, target_env)

            return {
                "ok": True,
                "source": self.config.source.redacted,
                "target": self.config.target.redacted,
                "retained_private_content": retain_private_content,
                "backup_database": backup,
                "core_counts": restored_core_counts,
                "warnings": source_preflight.get("warnings") or [],
            }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clone production PostgreSQL into an allow-listed local database."
    )
    parser.add_argument(
        "--confirm",
        help="Required destructive token: RESET-<target database>",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and print a redacted plan without connecting.",
    )
    parser.add_argument(
        "--retain-private-content",
        action="store_true",
        help=(
            "Keep chats, notes, appeals, plans, and agent traces for diagnosis. "
            "Emails and every password hash are still replaced. This explicit "
            "mode also keeps the prior local database as a rollback backup; "
            "safe mode removes that backup after a verified swap."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = CloneConfig.from_env()
        validate_config(
            config,
            confirmation=args.confirm,
            destructive=not args.dry_run,
        )
        if args.dry_run:
            print(json.dumps({
                "ok": True,
                "dry_run": True,
                "source": config.source.redacted,
                "target": config.target.redacted,
                "retain_private_content": bool(args.retain_private_content),
                "confirmation_required": f"RESET-{config.target.database}",
            }, ensure_ascii=False, indent=2))
            return 0
        result = CloneService(config).run(
            retain_private_content=bool(args.retain_private_content)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except CloneSafetyError as exc:
        print(f"clone refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
