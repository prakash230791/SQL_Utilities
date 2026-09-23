"""Connections.

Source (Azure SQL MI): Microsoft Entra service principal, via
  - service_principal_secret      azure-identity ClientSecretCredential -> access token
  - service_principal_certificate azure-identity CertificateCredential  -> access token
  - odbc_native                   ODBC 18 Authentication=ActiveDirectoryServicePrincipal

Target (Amazon RDS for SQL Server): RDS does not accept Entra tokens, so live
validation uses SQL authentication with credentials from environment variables
or AWS Secrets Manager (RDS-managed secret JSON with username/password).

Secrets are never read from the config file itself - only the names of the
environment variables / secret ids that hold them.
"""
from __future__ import annotations

import json
import logging
import os
import struct

log = logging.getLogger(__name__)

SQL_COPT_SS_ACCESS_TOKEN = 1256
AZURE_SQL_SCOPE = "https://database.windows.net/.default"


def _env(auth: dict, key: str, required: bool = True) -> str | None:
    var = auth.get(key)
    if not var:
        if required:
            raise RuntimeError(f"auth.{key} (name of an environment variable) is not configured")
        return None
    val = os.environ.get(var)
    if required and not val:
        raise RuntimeError(f"Environment variable {var} (auth.{key}) is not set")
    return val


def _conn_str(c: dict, database: str) -> str:
    return (f"Driver={{{c['driver']}}};Server=tcp:{c['server']},{c['port']};Database={database};"
            f"Encrypt={'yes' if c.get('encrypt', True) else 'no'};"
            f"TrustServerCertificate={'yes' if c.get('trust_server_certificate') else 'no'};"
            f"Connection Timeout={c.get('login_timeout', 30)};APP=clr-migrator;")


def _odbc_escape(v: str) -> str:
    return "{" + v.replace("}", "}}") + "}"


def source_identity(src: dict) -> str:
    """Client id of the service principal (for audit), without failing if unset."""
    var = src.get("auth", {}).get("client_id_env")
    return os.environ.get(var, "") if var else ""


def connect_source(src: dict, database: str):
    import pyodbc

    auth = src["auth"]
    method = auth["method"]
    cs = _conn_str(src, database)
    client_id = _env(auth, "client_id_env")

    if method == "odbc_native":
        secret = _env(auth, "client_secret_env")
        cs += f"Authentication=ActiveDirectoryServicePrincipal;UID={client_id};PWD={_odbc_escape(secret)};"
        log.info("Connecting to %s/%s (ODBC ActiveDirectoryServicePrincipal, client %s)",
                 src["server"], database, client_id)
        return pyodbc.connect(cs)

    from azure.identity import CertificateCredential, ClientSecretCredential

    tenant = _env(auth, "tenant_id_env")
    if method == "service_principal_secret":
        cred = ClientSecretCredential(tenant, client_id, _env(auth, "client_secret_env"))
    else:
        cred = CertificateCredential(tenant, client_id,
                                     certificate_path=_env(auth, "certificate_path_env"),
                                     password=_env(auth, "certificate_password_env", required=False))
    token = cred.get_token(AZURE_SQL_SCOPE).token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token)}s", len(token), token)
    log.info("Connecting to %s/%s (Entra access token, client %s)", src["server"], database, client_id)
    return pyodbc.connect(cs, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})


def connect_target(lv: dict):
    import pyodbc

    auth = lv["auth"]
    if auth["method"] == "aws_secrets_manager":
        import boto3
        sm = boto3.client("secretsmanager", region_name=auth.get("region"))
        secret = json.loads(sm.get_secret_value(SecretId=auth["secret_id"])["SecretString"])
        user, pwd = secret["username"], secret["password"]
    else:
        user, pwd = _env(auth, "username_env"), _env(auth, "password_env")
    cs = _conn_str(lv, lv["database"]) + f"UID={user};PWD={_odbc_escape(pwd)};"
    log.info("Connecting to target %s/%s as %s", lv["server"], lv["database"], user)
    return pyodbc.connect(cs, autocommit=True)


def fetch(cur, sql: str, params: tuple = ()) -> list[dict]:
    cur.execute(sql, params) if params else cur.execute(sql)
    if cur.description is None:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
