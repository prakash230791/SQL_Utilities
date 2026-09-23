"""Helpers shared by both phases."""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import logging
import platform
import re
import socket
import sys
from pathlib import Path

from . import __version__
from .analyzer import Target
from .config import split_name

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(s: str) -> str:
    s = _BAD_CHARS.sub("_", s).rstrip(" .")
    return s or "_"


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def setup_logging(log_dir: Path, name: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{name}_{stamp()}.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)
    return path


def runtime_identity() -> dict:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - containers without a passwd entry
        user = "unknown"
    return {"os_user": user, "host": socket.gethostname(),
            "python": platform.python_version(), "tool_version": __version__}


def mapping_for(name: str, cfg: dict) -> dict:
    key = "{}.{}".format(*split_name(name)).casefold()
    for c in cfg["clr_objects"]:
        if "{}.{}".format(*split_name(c["name"])).casefold() == key:
            return c
    return {"name": name, "strategy": "manual"}


def build_targets(dbinv: dict, cfg: dict) -> list[Target]:
    out = []
    for t in dbinv["targets"]:
        if not t.get("found"):
            continue
        out.append(Target(schema=t["schema"], name=t["name"], clr_type=t.get("type", "?"),
                          param_count=t.get("param_count"),
                          synonyms=[tuple(x) for x in t.get("synonyms", [])],
                          mapping=mapping_for(f"{t['schema']}.{t['name']}", cfg)))
    return out


def read_sql(path: Path) -> str:
    # newline='' keeps CRLF exactly as extracted so hashes and diffs are stable.
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text(path: Path, text: str, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        f.write(text)
