# -*- coding: utf-8 -*-
"""Inicia o Twitch Channel Points Miner usando o config.json do painel.

O painel executa este arquivo como subprocesso. Para encerrar com segurança,
o painel escreve "stop" no stdin; se o painel fechar (stdin em EOF), o miner
também é encerrado.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

PAINEL_DIR = Path(__file__).resolve().parent
ROOT = PAINEL_DIR.parent
sys.path[:0] = [str(PAINEL_DIR), str(ROOT)]
# O Python portátil do instalador ignora PYTHONIOENCODING; sem isso os emojis dos logs quebram
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
# O miner salva cookies/, logs/ e analytics/ relativos ao diretório atual
os.chdir(ROOT)

from painel_config import STATUS_FILE, load_config  # noqa: E402

from TwitchChannelPointsMiner import TwitchChannelPointsMiner  # noqa: E402
from TwitchChannelPointsMiner.classes.Chat import ChatPresence  # noqa: E402
from TwitchChannelPointsMiner.classes.entities.Bet import (  # noqa: E402
    BetSettings,
    Condition,
    DelayMode,
    FilterCondition,
    OutcomeKeys,
    Strategy,
)
from TwitchChannelPointsMiner.classes.entities.Streamer import (  # noqa: E402
    Streamer,
    StreamerSettings,
)
from TwitchChannelPointsMiner.classes.Settings import (  # noqa: E402
    FollowersOrder,
    Priority,
)
from TwitchChannelPointsMiner.logger import LoggerSettings  # noqa: E402

STREAMER_BOOL_FIELDS = (
    "make_predictions",
    "follow_raid",
    "claim_drops",
    "claim_moments",
    "watch_streak",
    "community_goals",
)


def is_set(value):
    return value is not None and value != ""


def number(value):
    value = float(value)
    return int(value) if value.is_integer() else value


def build_bet(bet: dict):
    if not bet:
        return None
    kwargs = {}
    if is_set(bet.get("strategy")):
        kwargs["strategy"] = Strategy[bet["strategy"]]
    for key in ("percentage", "percentage_gap", "max_points", "minimum_points"):
        if is_set(bet.get(key)):
            kwargs[key] = int(bet[key])
    if is_set(bet.get("stealth_mode")):
        kwargs["stealth_mode"] = bool(bet["stealth_mode"])
    if is_set(bet.get("delay_mode")):
        kwargs["delay_mode"] = DelayMode[bet["delay_mode"]]
    if is_set(bet.get("delay")):
        kwargs["delay"] = float(bet["delay"])
    flt = bet.get("filter") or {}
    if flt.get("enabled") and is_set(flt.get("value")):
        kwargs["filter_condition"] = FilterCondition(
            by=getattr(OutcomeKeys, flt["by"]),
            where=Condition[flt["where"]],
            value=number(flt["value"]),
        )
    return BetSettings(**kwargs) if kwargs else None


def build_streamer_settings(settings: dict):
    if not settings:
        return None
    kwargs = {k: bool(settings[k]) for k in STREAMER_BOOL_FIELDS if is_set(settings.get(k))}
    if is_set(settings.get("chat")):
        kwargs["chat"] = ChatPresence[settings["chat"]]
    bet = build_bet(settings.get("bet"))
    if bet is not None:
        kwargs["bet"] = bet
    return StreamerSettings(**kwargs) if kwargs else None


def write_status(miner):
    """Grava um resumo dos canais para o painel exibir em tempo real."""
    while True:
        try:
            streamers = list(miner.streamers)
            original = list(miner.original_streamers)
            rows = []
            for index, streamer in enumerate(streamers):
                start = original[index] if index < len(original) else None
                rows.append(
                    {
                        "username": streamer.username,
                        "points": streamer.channel_points,
                        "online": streamer.is_online,
                        "game": streamer.stream.game_name() if streamer.is_online else None,
                        "gained": streamer.channel_points - start if start is not None else 0,
                        "history": streamer.history,
                    }
                )
            data = {
                "updated": time.time(),
                "ready": bool(original),
                "streamers": rows,
            }
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, STATUS_FILE)
        except Exception:
            pass
        time.sleep(5)


def watch_stdin():
    """Encerra o miner quando o painel pedir (ou quando o painel fechar)."""
    for line in sys.stdin:
        if line.strip() == "stop":
            break
    # Dispara o mesmo caminho do CTRL+C, que salva tudo antes de sair
    signal.raise_signal(signal.SIGINT)


def main():
    cfg = load_config()
    defaults = cfg["defaults"]

    threading.Thread(target=watch_stdin, name="Painel stdin", daemon=True).start()

    default_settings = build_streamer_settings(defaults) or StreamerSettings()
    if default_settings.bet is None:
        default_settings.bet = BetSettings()

    miner = TwitchChannelPointsMiner(
        username=cfg["username"],
        claim_drops_startup=cfg["claim_drops_startup"],
        priority=[Priority[p] for p in cfg["priority"] if p] or [Priority.ORDER],
        enable_analytics=cfg["enable_analytics"],
        logger_settings=LoggerSettings(
            save=True,
            console_level=logging.INFO,
            file_level=logging.DEBUG,
            emoji=True,
            less=cfg["simple_logs"],
            colored=False,
        ),
        streamer_settings=default_settings,
    )

    if cfg["enable_analytics"]:
        miner.analytics(host="127.0.0.1", port=int(cfg["analytics_port"]), refresh=5, days_ago=7)

    threading.Thread(target=write_status, args=(miner,), name="Painel status", daemon=True).start()

    streamers = [
        Streamer(s["username"], settings=build_streamer_settings(s.get("settings")))
        for s in cfg["streamers"]
        if s.get("enabled", True)
    ]

    miner.mine(
        streamers,
        followers=cfg["followers"],
        followers_order=FollowersOrder[cfg["followers_order"]],
    )


if __name__ == "__main__":
    main()
