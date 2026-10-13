"""
decision_log.py -- registra CADA candidato que llega al filtro final (paso
por todos los filtros de titulo/liquidez/horas y supero MIN_RETURN), se haya
comprado o no. Es el "embudo" completo: cuantos candidatos aparecen, cuantos
se descartan y por que (corroboracion meteorologica en contra, cupo maximo
de posiciones, orden fallida). Equivalente a decisionLog.js del bot de BTC.
"""
import csv
from pathlib import Path

DECISION_LOG_PATH = Path(__file__).parent / "logs" / "decision_log.csv"

HEADER = [
    "tick_iso", "condition_id", "title", "obvious_by", "no_price", "expected_return",
    "liquidity", "hours_left", "corrob_source", "corrob_verdict", "corrob_city",
    "corrob_forecast", "corrob_threshold", "corrob_unit", "corrob_margin",
    "outcome", "skip_reason", "fail_category", "requested_shares", "filled_shares", "fill_ratio",
]


def _ensure_header():
    """Crea el archivo si no existe, y migra el header (+ rellena filas viejas
    con columnas de menos) si HEADER crecio desde la ultima vez que se escribio.
    Ver el comentario equivalente en journal.py -- mismo problema, mismo fix."""
    DECISION_LOG_PATH.parent.mkdir(exist_ok=True)
    if not DECISION_LOG_PATH.exists():
        with open(DECISION_LOG_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADER)
        return
    with open(DECISION_LOG_PATH, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        existing_header = next(reader, None)
        if existing_header == HEADER:
            return
        rows = list(reader)
    fixed_rows = [r + [""] * (len(HEADER) - len(r)) for r in rows]
    with open(DECISION_LOG_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(fixed_rows)


def append_decision(row: dict):
    _ensure_header()
    with open(DECISION_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([row.get(h, "") for h in HEADER])
