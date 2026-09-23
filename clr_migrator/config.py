"""YAML configuration loading, validation and defaults."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


STRATEGIES = {"rename", "template", "string_agg", "manual"}
SOURCE_AUTH = {"service_principal_secret", "service_principal_certificate", "odbc_native"}
TARGET_AUTH = {"sql_password_env", "aws_secrets_manager"}

_NAME_RX = re.compile(
    r"^\s*(?:(\[(?:[^\]]|\]\])+\]|[^.\[\]\s]+)\s*\.\s*)?(\[(?:[^\]]|\]\])+\]|[^.\[\]\s]+)\s*$")


def _unq(p: str) -> str:
    return p[1:-1].replace("]]", "]") if p.startswith("[") else p


def split_name(qualified: str, default_schema: str = "dbo") -> tuple[str, str]:
    m = _NAME_RX.match(qualified or "")
    if not m:
        raise ConfigError(f"Invalid object name {qualified!r}; expected schema.name")
    return (_unq(m.group(1)) if m.group(1) else default_schema), _unq(m.group(2))


def load_config(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Config file not found: {p}")
    raw = p.read_bytes()
    cfg = yaml.safe_load(raw) or {}
    cfg["_path"] = str(p.resolve())
    cfg["_dir"] = str(p.resolve().parent)
    cfg["_sha256"] = hashlib.sha256(raw).hexdigest()
    _defaults(cfg)
    _validate(cfg)
    return cfg


def resolve_path(cfg: dict, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (Path(cfg["_dir"]) / q).resolve()


def _defaults(cfg: dict) -> None:
    cfg.setdefault("project", "clr-retirement")
    cfg.setdefault("output_root", "./output")
    cfg.setdefault("prerequisite_scripts", [])
    cfg.setdefault("clr_objects", [])
    opts = cfg.setdefault("options", {})
    opts.setdefault("case_sensitive", False)
    opts.setdefault("include_object_types", ["P", "FN", "IF", "TF", "V", "TR"])
    opts.setdefault("exclude_schemas", [])
    opts.setdefault("exclude_objects", [])
    opts.setdefault("create_or_alter", True)

    src = cfg.setdefault("source", {})
    src.setdefault("port", 1433)
    src.setdefault("driver", "ODBC Driver 18 for SQL Server")
    src.setdefault("encrypt", True)
    src.setdefault("trust_server_certificate", False)
    src.setdefault("login_timeout", 30)
    src.setdefault("auth", {}).setdefault("method", "service_principal_secret")

    tgt = cfg.setdefault("target", {})
    tgt.setdefault("platform", "aws_rds_sqlserver")
    tgt.setdefault("engine_major_version", 15)
    tgt.setdefault("compat_level", 150)
    tgt.setdefault("database_map", {})
    tgt.setdefault("required_types", [])
    lv = tgt.setdefault("live_validation", {})
    lv.setdefault("enabled", False)
    lv.setdefault("port", 1433)
    lv.setdefault("driver", "ODBC Driver 18 for SQL Server")
    lv.setdefault("encrypt", True)
    lv.setdefault("trust_server_certificate", False)
    lv.setdefault("login_timeout", 30)
    lv.setdefault("auth", {}).setdefault("method", "sql_password_env")

    for c in cfg["clr_objects"]:
        c.setdefault("strategy", "manual")
        if c["strategy"] == "string_agg":
            sa = c.setdefault("string_agg", {})
            sa.setdefault("delimiter", ",")
            sa.setdefault("delimiter_from_arg", None)
            sa.setdefault("cast_to_nvarchar_max", True)
            sa.setdefault("empty_result", "empty_string")
            sa.setdefault("within_group_order_by", None)


def _validate(cfg: dict) -> None:
    src = cfg["source"]
    for k in ("server", "databases"):
        if not src.get(k):
            raise ConfigError(f"source.{k} is required")
    if not isinstance(src["databases"], list):
        raise ConfigError("source.databases must be a list")
    if src["auth"]["method"] not in SOURCE_AUTH:
        raise ConfigError(f"source.auth.method must be one of {sorted(SOURCE_AUTH)}")
    lv = cfg["target"]["live_validation"]
    if lv["auth"]["method"] not in TARGET_AUTH:
        raise ConfigError(f"target.live_validation.auth.method must be one of {sorted(TARGET_AUTH)}")
    seen = set()
    for i, c in enumerate(cfg["clr_objects"]):
        if not c.get("name"):
            raise ConfigError(f"clr_objects[{i}].name is required")
        key = "{}.{}".format(*split_name(c["name"])).casefold()
        if key in seen:
            raise ConfigError(f"clr_objects: duplicate entry {c['name']}")
        seen.add(key)
        s = c["strategy"]
        if s not in STRATEGIES:
            raise ConfigError(f"clr_objects[{i}].strategy must be one of {sorted(STRATEGIES)}")
        if s == "rename" and not c.get("replacement_object"):
            raise ConfigError(f"clr_objects[{i}] strategy rename requires replacement_object")
        if s == "template" and not c.get("call_template"):
            raise ConfigError(f"clr_objects[{i}] strategy template requires call_template")
        if s == "string_agg" and c["string_agg"]["empty_result"] not in ("empty_string", "null"):
            raise ConfigError(f"clr_objects[{i}].string_agg.empty_result must be empty_string or null")
