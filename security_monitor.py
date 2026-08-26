#!/usr/bin/env python3
"""
security_monitor.py — Monitor de seguridad para wallet de Polymarket

Corre en background y chequea cada N minutos:
1. El balance de la wallet (EOA + FUNDER)
2. Actividad reciente (transacciones nuevas)
3. Si detecta una caida anomala de balance sin que haya trades/redeems
   que lo expliquen -> ALERTA por Telegram

Este script NO puede prevenir un robo (nada puede, una vez que la clave
esta comprometida), pero te avisa en minutos en vez de dias, dandote
tiempo de reaccion para mover lo que quede a una wallet segura.
"""
import os
import time
import json
import requests
import logging
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
POLY_FUNDER      = os.getenv("POLY_FUNDER", "")
POLY_EOA         = os.getenv("POLY_EOA", "")  # direccion publica de tu wallet

CHECK_INTERVAL   = 300   # revisar cada 5 minutos
DROP_THRESHOLD   = 0.30  # alertar si el balance cae >30% entre checks sin explicacion
MIN_BALANCE_ALERT = 1.0  # no alertar por fluctuaciones bajo $1 (ruido)

POLY_DATA_API = "https://data-api.polymarket.com"

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-12s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/security_monitor.log")
    ],
)
log = logging.getLogger("security")


def send_telegram(msg: str, urgent: bool = False):
    """Envia una alerta por Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado, no se puede alertar")
        return
    prefix = "🚨 ALERTA URGENTE 🚨\n\n" if urgent else "ℹ️ "
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": prefix + msg},
            timeout=10
        )
        if r.status_code != 200:
            log.error(f"Error enviando Telegram: {r.text[:200]}")
    except Exception as e:
        log.error(f"Error Telegram: {e}")


def get_positions_value(wallet: str) -> float:
    """Suma el valor actual de todas las posiciones abiertas."""
    try:
        r = requests.get(
            f"{POLY_DATA_API}/positions",
            params={"user": wallet, "sizeThreshold": 0.01},
            timeout=10
        )
        if r.status_code == 200:
            positions = r.json()
            if isinstance(positions, list):
                return sum(float(p.get("currentValue", 0) or 0) for p in positions)
    except Exception as e:
        log.error(f"Error obteniendo posiciones: {e}")
    return -1  # -1 indica error, no confundir con balance real 0


def get_recent_activity(wallet: str, limit: int = 10) -> list:
    """Obtiene actividad reciente de la wallet."""
    try:
        r = requests.get(
            f"{POLY_DATA_API}/activity",
            params={"user": wallet, "limit": limit},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        log.error(f"Error obteniendo actividad: {e}")
    return []


def format_usd(v: float) -> str:
    return f"${v:.2f}"


class SecurityMonitor:
    def __init__(self):
        self.last_balance = None
        self.last_activity_ids = set()
        self.consecutive_errors = 0
        self.started_at = datetime.now(timezone.utc)

        log.info("=" * 60)
        log.info("  Security Monitor iniciado")
        log.info(f"  Wallet FUNDER: {POLY_FUNDER[:10]}...")
        log.info(f"  Chequeo cada {CHECK_INTERVAL}s")
        log.info(f"  Alerta si cae >{DROP_THRESHOLD*100:.0f}% sin explicacion")
        log.info("=" * 60)

        send_telegram(
            "Monitor de seguridad iniciado.\n"
            f"Wallet: {POLY_FUNDER[:10]}...{POLY_FUNDER[-6:]}\n"
            f"Revisando cada {CHECK_INTERVAL//60} minutos."
        )

    def check_once(self):
        wallet = POLY_FUNDER
        if not wallet:
            log.error("POLY_FUNDER no configurado en .env")
            return

        current_value = get_positions_value(wallet)

        if current_value == -1:
            self.consecutive_errors += 1
            log.warning(f"Error consultando balance ({self.consecutive_errors} veces seguidas)")
            if self.consecutive_errors >= 5:
                send_telegram(
                    "No se puede consultar el balance de la wallet hace varios "
                    "chequeos seguidos. Puede ser un problema de red o de la API "
                    "de Polymarket. Revisa manualmente.",
                    urgent=False
                )
                self.consecutive_errors = 0
            return
        self.consecutive_errors = 0

        # Obtener actividad reciente para detectar transacciones nuevas
        activity = get_recent_activity(wallet, limit=15)
        current_ids = set()
        new_events = []
        for a in activity:
            if not isinstance(a, dict):
                continue
            aid = a.get("id") or a.get("transactionHash", "")
            if aid:
                current_ids.add(aid)
                if self.last_activity_ids and aid not in self.last_activity_ids:
                    new_events.append(a)

        # Primera corrida: solo establecer baseline
        if self.last_balance is None:
            self.last_balance = current_value
            self.last_activity_ids = current_ids
            log.info(f"Balance inicial (posiciones abiertas): {format_usd(current_value)}")
            return

        # Detectar caida de balance
        if self.last_balance > MIN_BALANCE_ALERT:
            drop = (self.last_balance - current_value) / self.last_balance
            if drop > DROP_THRESHOLD:
                # Verificar si hay eventos que expliquen la caida (redeems, trades normales)
                explicable = any(
                    e.get("type") in ("REDEEM", "TRADE") for e in new_events
                )
                if not explicable or len(new_events) == 0:
                    msg = (
                        f"Caida de balance sospechosa detectada.\n\n"
                        f"Antes: {format_usd(self.last_balance)}\n"
                        f"Ahora: {format_usd(current_value)}\n"
                        f"Diferencia: -{drop*100:.1f}%\n\n"
                        f"No se encontraron trades o cobros que expliquen esta caida.\n"
                        f"Revisa tu wallet AHORA: https://polygonscan.com/address/{wallet}"
                    )
                    log.warning(msg)
                    send_telegram(msg, urgent=True)

        # Reportar actividad nueva relevante (informativo, no urgente)
        for e in new_events:
            etype = e.get("type", "")
            title = str(e.get("title", ""))[:50]
            size = e.get("size") or e.get("amount") or 0
            if etype == "REDEEM":
                log.info(f"Cobro detectado: {title} (${size})")
            elif etype == "TRADE":
                log.info(f"Trade detectado: {title} (${size})")

        self.last_balance = current_value
        self.last_activity_ids = current_ids

        log.info(f"Check OK — balance posiciones: {format_usd(current_value)}  "
                  f"eventos nuevos: {len(new_events)}")

    def run(self):
        while True:
            try:
                self.check_once()
            except Exception as e:
                log.error(f"Error en check: {e}")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    monitor = SecurityMonitor()
    monitor.run()
