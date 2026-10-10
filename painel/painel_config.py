# -*- coding: utf-8 -*-
"""Leitura, validação e gravação do config.json usado pelo painel e pelo runner."""

import copy
import json
import os
import re
from pathlib import Path

PAINEL_DIR = Path(__file__).resolve().parent
ROOT = PAINEL_DIR.parent
DATA_DIR = PAINEL_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
STATUS_FILE = DATA_DIR / "status.json"

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{2,25}$")

STRATEGIES = [
    "SMART", "MOST_VOTED", "HIGH_ODDS", "PERCENTAGE", "SMART_MONEY",
    "NUMBER_1", "NUMBER_2", "NUMBER_3", "NUMBER_4",
    "NUMBER_5", "NUMBER_6", "NUMBER_7", "NUMBER_8",
]
PRIORITIES = ["STREAK", "DROPS", "ORDER", "SUBSCRIBED", "POINTS_ASCENDING", "POINTS_DESCENDING"]
CHAT_MODES = ["ONLINE", "ALWAYS", "NEVER", "OFFLINE"]
DELAY_MODES = ["FROM_END", "FROM_START", "PERCENTAGE"]
FILTER_KEYS = ["TOTAL_USERS", "TOTAL_POINTS", "PERCENTAGE_USERS", "ODDS_PERCENTAGE", "ODDS", "TOP_POINTS"]
CONDITIONS = ["GT", "LT", "GTE", "LTE"]

DEFAULT_CONFIG = {
    "username": "",
    "claim_drops_startup": False,
    "priority": ["STREAK", "DROPS", "ORDER"],
    "followers": False,
    "followers_order": "ASC",
    "enable_analytics": False,
    "analytics_port": 5000,
    "simple_logs": True,
    "auto_restart": True,
    "start_miner_with_panel": True,
    "auto_update": False,
    "defaults": {
        "make_predictions": False,
        "follow_raid": True,
        "claim_drops": True,
        "claim_moments": True,
        "watch_streak": True,
        "community_goals": False,
        "chat": "ONLINE",
        "bet": {
            "strategy": "SMART",
            "percentage": 5,
            "percentage_gap": 20,
            "max_points": 50000,
            "minimum_points": 0,
            "stealth_mode": True,
            "delay_mode": "FROM_END",
            "delay": 6,
            "filter": {"enabled": False, "by": "TOTAL_USERS", "where": "GTE", "value": 10},
        },
    },
    "streamers": [],
}


class ConfigError(ValueError):
    pass


def _merge(base, override):
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    else:
        data = {}
    return _merge(DEFAULT_CONFIG, data)


def _choice(value, options, field):
    if value not in options:
        raise ConfigError(f"Valor inválido para {field}: {value}")
    return value


def _int(value, field, minimum=0, maximum=None, optional=False):
    if optional and (value is None or value == ""):
        return None
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        raise ConfigError(f"{field} precisa ser um número")
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"{field} precisa estar entre {minimum} e {maximum}")
    return value


def _validate_bet(bet, optional):
    out = {}
    if not optional or bet.get("strategy"):
        out["strategy"] = _choice(bet.get("strategy"), STRATEGIES, "estratégia")
    out["percentage"] = _int(bet.get("percentage"), "Porcentagem", 1, 100, optional)
    out["max_points"] = _int(bet.get("max_points"), "Máximo de pontos", 1, None, optional)
    if not optional:
        out["percentage_gap"] = _int(bet.get("percentage_gap"), "Diferença (gap)", 0, 100)
        out["minimum_points"] = _int(bet.get("minimum_points"), "Mínimo de pontos", 0)
        out["stealth_mode"] = bool(bet.get("stealth_mode"))
        out["delay_mode"] = _choice(bet.get("delay_mode"), DELAY_MODES, "modo de atraso")
        try:
            out["delay"] = float(bet.get("delay"))
        except (TypeError, ValueError):
            raise ConfigError("Atraso precisa ser um número")
        flt = bet.get("filter") or {}
        out["filter"] = {
            "enabled": bool(flt.get("enabled")),
            "by": _choice(flt.get("by", "TOTAL_USERS"), FILTER_KEYS, "filtro"),
            "where": _choice(flt.get("where", "GTE"), CONDITIONS, "condição do filtro"),
            "value": float(flt.get("value") or 0),
        }
    return {k: v for k, v in out.items() if v is not None}


def validate(data):
    cfg = _merge(DEFAULT_CONFIG, data)
    username = str(cfg["username"]).strip().lower()
    if username and not USERNAME_RE.match(username):
        raise ConfigError("Nome de usuário da Twitch inválido")
    cfg["username"] = username

    for key in ("claim_drops_startup", "followers", "enable_analytics",
                "simple_logs", "auto_restart", "start_miner_with_panel", "auto_update"):
        cfg[key] = bool(cfg[key])
    cfg["followers_order"] = _choice(cfg["followers_order"], ["ASC", "DESC"], "ordem dos seguidos")
    cfg["analytics_port"] = _int(cfg["analytics_port"], "Porta dos gráficos", 1024, 65535)
    cfg["priority"] = [_choice(p, PRIORITIES, "prioridade") for p in cfg["priority"] if p]

    d = cfg["defaults"]
    for key in ("make_predictions", "follow_raid", "claim_drops", "claim_moments",
                "watch_streak", "community_goals"):
        d[key] = bool(d[key])
    d["chat"] = _choice(d["chat"], CHAT_MODES, "chat")
    d["bet"] = _validate_bet(d["bet"], optional=False)

    streamers, seen = [], set()
    for s in cfg["streamers"]:
        name = str(s.get("username", "")).strip().lower()
        if not USERNAME_RE.match(name):
            raise ConfigError(f"Canal inválido: {name or '(vazio)'}")
        if name in seen:
            continue
        seen.add(name)
        settings = s.get("settings") or {}
        clean = {}
        if settings.get("make_predictions") in (True, False):
            clean["make_predictions"] = settings["make_predictions"]
        bet = _validate_bet(settings.get("bet") or {}, optional=True)
        if bet:
            clean["bet"] = bet
        streamers.append({"username": name, "enabled": bool(s.get("enabled", True)), "settings": clean})
    cfg["streamers"] = streamers
    return cfg


def save_config(data):
    cfg = validate(data)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
    return cfg


def cookies_file(username):
    return ROOT / "cookies" / f"{username}.pkl"
