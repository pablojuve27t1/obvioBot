"""
journal.py -- registra cada operacion COMPLETA (entrada + resolucion) en un
CSV persistente, para poder auditar cada trade despues sin depender de la
memoria del proceso (que se pierde al reiniciar el bot). Equivalente a
journal.js del bot de BTC.
"""
import csv
from pathlib import Path

JOURNAL_PATH = Path(__file__).parent / "logs" / "trade_journal.csv"

HEADER = [
    "settled_at_iso", "mode", "condition_id", "title", "entered_at_iso",
    "no_price_entry", "shares", "spent_usd", "expected_return", "hours_left_at_entry",
    "requested_shares", "fill_ratio",
    "corrob_source", "corrob_verdict", "corrob_city", "corrob_forecast",
    "corrob_threshold", "corrob_unit", "corrob_margin",
    "won", "pnl_usd", "capital_after", "horas_espera_post_cierre_nominal",
]


def _ensure_header():
    """Crea el archivo si no existe, y migra el header (+ rellena filas viejas
    con columnas de menos) si HEADER crecio desde la ultima vez que se escribio
    -- sin esto, agregar una columna nueva deja el header desalineado con las
    filas que se van agregando despues (se detecto en produccion: el analisis
    con csv.DictReader devolvia None para columnas que en realidad si estaban
    en el archivo, solo que corridas de posicion)."""
    JOURNAL_PATH.parent.mkdir(exist_ok=True)
    if not JOURNAL_PATH.exists():
        with open(JOURNAL_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADER)
        return
    with open(JOURNAL_PATH, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        existing_header = next(reader, None)
        if existing_header == HEADER:
            return
        rows = list(reader)
    fixed_rows = [r + [""] * (len(HEADER) - len(r)) for r in rows]
    with open(JOURNAL_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(fixed_rows)


def append_trade(row: dict):
    _ensure_header()
    with open(JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([row.get(h, "") for h in HEADER])
