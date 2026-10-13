"""
obviobot.py — Bot de mercados obvios (NO a 95-97¢)
Estrategia: comprar NO en mercados donde el resultado es casi imposible
Ej: "Will Jesus return in 2026?" NO=0.97 → comprar → ganar 3¢ por share
"""
import re
import asyncio, logging, os, sys, time, requests, json
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

import polymarket_dns  # parchea DNS para *.polymarket.com (bloqueo ISP)
import weather_check
import decision_log
import journal

load_dotenv()

PRIVATE_KEY     = os.getenv("POLY_PRIVATE_KEY", "")
FUNDER          = os.getenv("POLY_FUNDER", "")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "50.88"))
STOP_DAILY_USD  = float(os.getenv("STOP_LOSS_DAILY_USD", "5.0"))
STOP_TOTAL_PCT  = float(os.getenv("STOP_LOSS_TOTAL_PCT", "0.20"))
MAX_CONCURRENT  = int(os.getenv("MAX_CONCURRENT", "3"))

POLY_GAMMA_API  = "https://gamma-api.polymarket.com"
POLY_CLOB_API   = "https://clob.polymarket.com"

# Precio mínimo para comprar NO (95¢ o más = casi seguro)
MIN_NO_PRICE    = 0.94
MAX_NO_PRICE    = 0.99

# Retorno mínimo esperado (1¢ por share mínimo)
MIN_RETURN      = 0.01

# Apuesta fija por trade
BET_PCT         = 0.05  # 5% del capital disponible por trade -- sin usar mientras EXPERIMENT_FEW_HIGH_CONF este activo
BET_USD         = 2.0   # se calcula dinamicamente en evaluate_market

# --- Experimento: pocas operaciones, alta confianza, mayor saldo por operacion ---
# En vez de diversificar en muchas apuestas chicas (BET_PCT al 5%, techo $15),
# esto prueba lo opuesto: pocos cupos (MAX_CONCURRENT=3), exige corroboracion
# CONFIRMA con margen amplio (no alcanza con "no CONTRADICE"), y el tamano de
# cada apuesta es saldo_disponible / cupos_restantes -- sin techo de $15.
#
# Es una apuesta de alto riesgo si "alta confianza" no reduce de verdad la
# probabilidad de fallo por debajo de lo que ya implica el precio (94-99%):
# con solo 3 cupos, una perdida se lleva una porcion mucho mas grande del
# capital que con 7+ cupos. Corre en PAPER, sin riesgo de capital real -- el
# objetivo es juntar evidencia de si el grupo CONFIRMA+margen>=3 realmente
# falla menos que el resto, antes de considerar esto con plata real.
EXPERIMENT_FEW_HIGH_CONF   = True
HIGH_CONFIDENCE_MIN_MARGIN = 3.0

# El parametro "tag" de /markets NO filtra nada -- confirmado en vivo:
# tag=politics, tag=world, tag=crypto (e incluso un tag inventado que no
# existe) devuelven exactamente los mismos resultados que sin tag alguno.
# El bot original escaneaba "6 categorias" que en realidad eran la MISMA
# lista global (activos, no cerrados, ordenados por volumen) consultada 6
# veces de forma redundante. La lista real tiene ~2100 mercados activos
# unicos en total, con mercados de temperatura repartidos a lo largo de TODA
# la lista (no concentrados al principio) -- offset=0 no trae ninguno, pero
# aparecen a partir de offset~200 y siguen apareciendo hasta el final.
#
# Ademas, el orden por volumen no es estable entre paginas (offset=100 puede
# traer volumenes mas altos que offset=0), asi que "solo mirar el principio"
# no es una aproximacion segura de "los mas relevantes".
#
# Por eso: una sola pasada (sin tag) paginando toda la lista, en vez de 6
# pasadas redundantes y poco profundas. ~21 paginas cubre el total actual con
# margen; cada pagina es una llamada HTTP, sigue entrando comodo en los 5
# minutos entre ciclos.
SCAN_OFFSETS    = list(range(0, 2200, 100))

# El escaneo de arriba (/markets, ordenado por volumen) tiene un problema real
# descubierto en produccion: un mercado de Hong Kong con $18,000 de liquidez,
# completamente activo y operable, NUNCA aparecio en esa paginacion -- ni
# pidiendo 500 resultados por pagina (la API los recorta a 100 en silencio sin
# avisar). El orden por volumen se reordena entre paginas, asi que mercados
# genuinamente activos y liquidos quedan fuera de las ~2100 posiciones que se
# alcanzan a cubrir. Esto hizo pensar por un momento que el inventario de
# clima se habia desplomado -- en realidad habia 358 eventos de clima activos
# (~3900 mercados) que el escaneo normal simplemente no veia.
#
# /events?tag_slug=weather pagina de forma ESTABLE (cero solapamiento entre
# paginas, confirmado) y cada evento trae sus mercados (los ~11 buckets de
# umbral) completos, con los mismos campos que espera evaluate_market. Se usa
# como fuente ADICIONAL especifica para clima, sin reemplazar el escaneo
# general (que sigue sirviendo para lo no-climatico: acciones, politica, etc).
WEATHER_EVENT_OFFSETS = list(range(0, 450, 100))

# Palabras que indican mercados obvios (imposibles o casi seguros)
OBVIOUS_NO_KEYWORDS = [
    "aliens", "alien", "ufo", "extraterrestrial",
    "jesus", "god", "divine", "miracle",
    "world war 3", "world war iii", "nuclear war", "nuclear bomb",
    "asteroid", "meteor", "apocalypse", "end of world",
    "immortality", "live forever", "cure death",
    "time travel", "teleportation",
    "flat earth",
    "bigfoot", "sasquatch", "loch ness",
]

# Blacklist de mercados ya operados
BLACKLIST_IDS = set()  # reiniciado con wallet nueva

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-12s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/obviobot.log")
    ],
)
log = logging.getLogger("obviobot")


class ObvioBot:
    def __init__(self, paper=True):
        self.paper          = paper
        self.open_positions = {}
        (self.capital, self.wins, self.losses,
         self.daily_pnl, self.daily_pnl_date) = self._restore_from_journal(paper)
        self.scanned        = 0
        self.bought         = 0
        self.skipped_corrob = 0
        self.skipped_maxpos = 0
        self.orders_failed  = 0
        self.fail_categories = {}  # "liquidez"/"precio_movido"/"invalido"/"otro" -> contador
        self.uncertain_orders = []  # ordenes GTC que no se pudo confirmar que murieron -- requieren revision manual

        from executor import Executor
        self.executor = Executor(private_key=PRIVATE_KEY, funder=FUNDER, paper=paper)
        if not paper:
            self.reconcile_open_positions()

        tag = "PAPER" if paper else "REAL MONEY"
        log.info("=" * 60)
        log.info(f"  Obvio Bot — Mercados imposibles -- {tag}")
        log.info(f"  Capital: ${self.capital:.2f} (inicial ${INITIAL_CAPITAL:.2f})  "
                 f"Bet: {BET_PCT:.0%} del efectivo (min $2, max $15)")
        log.info(f"  Historial restaurado: {self.wins}W/{self.losses}L")
        log.info(f"  Estrategia: comprar NO cuando precio >= {MIN_NO_PRICE:.0%}")
        log.info(f"  Stop diario: -${STOP_DAILY_USD}  Max posiciones: {MAX_CONCURRENT}")
        log.info("=" * 60)

    def _restore_from_journal(self, paper: bool):
        """Reconstruye capital/wins/losses a partir del historial persistente
        en trade_journal.csv, en vez de arrancar siempre en INITIAL_CAPITAL.

        Sin esto, cada reinicio del bot (que hicimos varias veces esta semana
        por distintos fixes) borraba en silencio todo el progreso acumulado de
        sesiones anteriores -- se detecto comparando el capital mostrado en el
        dashboard contra la suma real de pnl_usd en el CSV: coincidian
        perfecto en las primeras 7 filas y despues aparecia un salto de -$2.00
        exacto, justo en el primer reinicio posterior."""
        capital, wins, losses, daily_pnl = INITIAL_CAPITAL, 0, 0, 0.0
        today = datetime.now(timezone.utc).date().isoformat()
        if not journal.JOURNAL_PATH.exists():
            return capital, wins, losses, daily_pnl, today
        try:
            import csv
            mode = "paper" if paper else "real"
            with open(journal.JOURNAL_PATH, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("mode") != mode:
                        continue
                    pnl = float(row.get("pnl_usd") or 0)
                    capital += pnl
                    if row.get("won") == "True":
                        wins += 1
                    elif row.get("won") == "False":
                        losses += 1
                    settled = row.get("settled_at_iso") or ""
                    if settled[:10] == today:
                        daily_pnl += pnl
        except Exception as e:
            log.warning(f"No se pudo restaurar capital desde trade_journal.csv: {e}")
            return INITIAL_CAPITAL, 0, 0, 0.0, today
        return round(capital, 2), wins, losses, round(daily_pnl, 2), today

    def _ensure_daily_pnl_current(self):
        """Resetea daily_pnl a 0 si cambio el dia (UTC) desde la ultima vez
        que se toco. Sin esto, daily_pnl sumaba para siempre desde que
        arranco el proceso en vez de representar "lo que va del dia" -- con
        el bot corriendo varios dias seguidos, el stop-loss diario de -$5
        dejaba de significar eso (necesitaba perder mas que todo lo ganado
        acumulado antes de activarse) y el dashboard mostraba practicamente
        el PnL acumulado bajo la etiqueta "P&L del dia"."""
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.daily_pnl_date:
            self.daily_pnl = 0.0
            self.daily_pnl_date = today

    def reconcile_open_positions(self):
        """Al arrancar (o tras un reinicio), sincroniza el estado en memoria con las
        posiciones REALES que ya existen en la wallet de Polymarket. Sin esto, un
        reinicio del bot olvida que ya tiene posiciones abiertas y las vuelve a
        comprar (bug detectado con Zhengzhou, MrBeast y Ted Cruz)."""
        wallet = os.getenv("POLY_FUNDER", "")
        if not wallet:
            log.warning("RECONCILIACION: no hay POLY_FUNDER configurado, se omite")
            return
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.1",
                timeout=10
            )
            if r.status_code != 200:
                log.warning(f"RECONCILIACION: fallo consulta (status {r.status_code}), se omite")
                return
            positions = r.json()
            if not isinstance(positions, list):
                log.warning("RECONCILIACION: respuesta inesperada, se omite")
                return
            count = 0
            for p in positions:
                cid = p.get("conditionId", "")
                value = float(p.get("currentValue", 0) or 0)
                if not cid or value <= 0:
                    continue
                self.open_positions[cid] = {
                    "title": p.get("title", ""),
                    "no_price": float(p.get("avgPrice", 0) or 0),
                    "shares": float(p.get("size", 0) or 0),
                    "spent": float(p.get("initialValue", 0) or 0),
                    "expected_return": 0.0,
                    "hours_left": None,
                    "token_id": p.get("asset", ""),
                    "time": time.time(),
                    "order": "reconciliado-al-arranque",
                    "pending": False,
                }
                BLACKLIST_IDS.add(cid)
                count += 1
            log.info(f"RECONCILIACION: {count} posiciones reales cargadas desde la wallet "
                      f"(evita recomprar mercados ya abiertos de una corrida anterior)")
        except Exception as e:
            log.warning(f"RECONCILIACION fallo: {e}")

    def get_real_position_count(self) -> int:
        """Consulta Polymarket para contar posiciones reales abiertas."""
        try:
            wallet = os.getenv("POLY_FUNDER", "")
            if not wallet:
                return len(self.open_positions)
            r = requests.get(
                f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=0.1",
                timeout=5
            )
            if r.status_code == 200:
                positions = r.json()
                if isinstance(positions, list):
                    # Solo contar posiciones con valor real > 0
                    active = [p for p in positions if float(p.get("currentValue") or 0) > 0.01]
                    return len(active)
        except Exception as e:
            log.debug(f"Error contando posiciones reales: {e}")
        return len(self.open_positions)

    def scan_markets(self, offset: int = 0):
        """Escanea una pagina de la lista global de mercados activos (por
        volumen). No manda "tag": confirmado en vivo que ese parametro no
        filtra nada en este endpoint (tag=X, tag=Y y un tag inventado
        devuelven exactamente los mismos resultados que sin tag).

        Devuelve None ante un fallo de red/API -- distinto de [] (lista
        vacia), que significa que la lista genuinamente termino en ese
        offset. Sin esta distincion, un timeout puntual en una sola pagina se
        interpretaba como "se acabo la lista" y cortaba la paginacion mas
        profunda para el resto del ciclo (se detecto en produccion: con
        paginacion mas profunda, un ciclo escaneo solo 501 mercados en vez de
        los ~2000+ esperados, por timeouts puntuales tratados como fin de lista)."""
        try:
            r = requests.get(
                f"{POLY_GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                    "order": "volume",
                    "ascending": "false"
                },
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
            log.warning(f"Error scan offset={offset}: status {r.status_code}")
        except Exception as e:
            log.warning(f"Error scan offset={offset}: {e}")
        return None

    def scan_weather_events(self, offset: int = 0):
        """Escanea una pagina de eventos de clima via /events?tag_slug=weather
        (paginacion estable, sin el problema de orden/recorte de /markets --
        ver nota junto a WEATHER_EVENT_OFFSETS). Devuelve la lista PLANA de
        mercados (los buckets de umbral) de todos los eventos de esa pagina,
        ya en el mismo formato que espera evaluate_market.

        Mismo contrato que scan_markets: None en fallo de red/API, [] cuando
        la lista de eventos genuinamente termino en ese offset."""
        try:
            r = requests.get(
                f"{POLY_GAMMA_API}/events",
                params={
                    "tag_slug": "weather",
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                },
                timeout=10
            )
            if r.status_code == 200:
                events = r.json()
                if not isinstance(events, list):
                    return []
                markets = []
                for ev in events:
                    markets.extend(ev.get("markets", []) or [])
                return markets
            log.warning(f"Error scan_weather_events offset={offset}: status {r.status_code}")
        except Exception as e:
            log.warning(f"Error scan_weather_events offset={offset}: {e}")
        return None

    def get_no_price(self, market: dict) -> tuple:
        """
        Obtiene el precio del NO/DOWN del mercado.
        Retorna (no_price, no_token_id, yes_token_id)
        """
        try:
            # Intentar desde outcomePrices
            outcomes_raw = market.get("outcomePrices", ["0.5", "0.5"])
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except:
                    outcomes = ["0.5", "0.5"]
            else:
                outcomes = outcomes_raw

            clob_ids = market.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except:
                    clob_ids = []

            if len(outcomes) >= 2 and len(clob_ids) >= 2:
                yes_price = float(outcomes[0])
                no_price  = float(outcomes[1])
                yes_token = clob_ids[0]
                no_token  = clob_ids[1]
                return no_price, no_token, yes_token

            # Fallback: usar CLOB API
            condition_id = market.get("conditionId", "")
            if condition_id:
                r = requests.get(f"{POLY_CLOB_API}/markets/{condition_id}", timeout=5)
                if r.status_code == 200:
                    clob = r.json()
                    tokens = clob.get("tokens", [])
                    no_token_data  = next((t for t in tokens if t.get("outcome","").lower() in ("no","down")), None)
                    yes_token_data = next((t for t in tokens if t.get("outcome","").lower() in ("yes","up")), None)
                    if no_token_data:
                        return (
                            float(no_token_data.get("price", 0)),
                            no_token_data.get("token_id", ""),
                            yes_token_data.get("token_id", "") if yes_token_data else ""
                        )
        except Exception as e:
            log.debug(f"Error get_no_price: {e}")
        return 0.0, "", ""

    def is_obvious_no(self, title: str) -> bool:
        """Detecta si el mercado es obviamente NO por el título."""
        title_lower = title.lower()
        return any(kw in title_lower for kw in OBVIOUS_NO_KEYWORDS)

    def hours_until_close(self, market: dict) -> float:
        """Calcula horas hasta el cierre del mercado."""
        end_date = market.get("endDate") or market.get("endDateIso")
        if not end_date:
            return 999
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            hours = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            return max(0, hours)
        except Exception as e:
            log.debug(f"hours_until_close: no se pudo parsear '{end_date}': {e}")
            return 999

    def _live_hours_left(self, pos: dict):
        """Horas reales restantes hasta el cierre, recalculadas contra la hora
        actual -- a diferencia de pos['hours_left'], que es un snapshot fijo
        de cuando se compro la posicion."""
        end_date = pos.get("end_date_iso")
        if not end_date:
            return pos.get("hours_left")  # posiciones viejas/reconciliadas sin end_date_iso
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            hours = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            return round(max(0, hours), 2)
        except Exception:
            return pos.get("hours_left")

    def _position_status(self, pos: dict) -> dict:
        """Estado explicito de la posicion, en vez de que 'hours_left=0' quede
        implicito y ambiguo:
          - ABIERTA: todavia no llega la hora nominal de cierre del mercado.
          - ESPERANDO_CIERRE: ya paso la hora nominal, pero Polymarket aun no
            marco el mercado como cerrado (check_resolved no lo detecto todavia)
            -- esta es la franja de tiempo que antes era invisible.
        (RESUELTA no aplica aca: en cuanto check_resolved detecta el cierre,
        la posicion se remueve de open_positions en el mismo paso.)
        """
        end_date = pos.get("end_date_iso")
        if not end_date:
            return {"status": "ABIERTA", "esperando_desde_horas": None}
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            raw_hours = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        except Exception:
            return {"status": "ABIERTA", "esperando_desde_horas": None}
        if raw_hours > 0:
            return {"status": "ABIERTA", "esperando_desde_horas": None}
        return {"status": "ESPERANDO_CIERRE", "esperando_desde_horas": round(-raw_hours, 2)}

    async def evaluate_market(self, market: dict):
        """Evalúa si un mercado vale la pena."""
        condition_id = market.get("conditionId", "")
        if not condition_id or condition_id in BLACKLIST_IDS:
            return
        if condition_id in self.open_positions:
            return

        title = market.get("question", "")[:80]
        if not title:
            return

        # Excluir spreads deportivos — son riesgosos
        title_lower = title.lower()

        # Excluir rangos exactos de temperatura (between X-Y) — margen de error muy angosto
        if "between" in title_lower and any(x in title_lower for x in ["°f", "°c", "temperature"]):
            return
        # Excluir mercados de lluvia si/no — mucho mas volatiles que umbrales de temperatura,
        # el precio puede desplomarse de 96% a ~55% de un dia para otro (visto en Seattle 21 ago)
        if "will it rain" in title_lower or "will it snow" in title_lower:
            return
        if any(x in title_lower for x in ['spread:', 'handicap', '(-', '(+', 'over/under', 'o/u']):
            return
        # Excluir deportes tradicionales en general: formato "equipo vs equipo"
        # o props deportivos comunes (futbol, basquet, etc.)
        if ' vs ' in title_lower or ' vs. ' in title_lower or ' v ' in title_lower:
            return
        if any(x in title_lower for x in [
            'to score first', 'both teams to score', 'clean sheet', 'first goal',
            'goalscorer', 'anytime scorer', 'red card', 'yellow card', 'corner',
            'penalty', 'own goal', 'hat-trick', 'moneyline', 'match winner',
            'to win the match', 'full time result', 'first half', 'second half',
            ' fc ', ' fc:', ' fc,', 'nba', 'nfl', 'nhl', 'mlb', 'premier league',
            'la liga', 'champions league', 'uefa', 'world cup',
        ]):
            return

        # Excluir esports, crypto up/down, resultados exactos y mercados riesgosos
        if any(x in title_lower for x in [
            'counter-strike', 'esport', 'cs2', 'dota', 'league of legends',
            'up or down', 'bnb up', 'eth up', 'btc up', 'sol up', 'xrp',
            'bitcoin', 'ethereum', 'litecoin', 'dogecoin', 'solana',
            'valorant', 'fortnite', 'faceit',
            'exact score', 'score:', 'win 2-0', 'win 3-0', 'win 2-1', 'win 3-1',
            'win 3-2', '0 - 0', '1 - 0', '0 - 1', '2 - 0', '0 - 2',
            'inner circle', 'to win', 'dip to', 'reach $', 'hit $',
        ]):
            return

        # Excluir cualquier mercado de rango de precio (cripto, acciones, etc.)
        # tipo "be between $X and $Y" — mismo motivo que el rango exacto de temperatura
        if re.search(r'between\s*\$[\d,]+(\.\d+)?\s*(and|-|to)\s*\$[\d,]+(\.\d+)?', title_lower):
            return

        # Excluir precios de acciones individuales
        if any(x in title_lower for x in ['(pltr)', '(amzn)', '(tsla)', '(aapl)', '(nvda)',
                                            '(msft)', '(googl)', '(meta)', '(nflx)',
                                            'close at $', 'close above $', 'close below $',
                                            'final day of trading', 'stock price']):
            return

        # Verificar liquidez mínima
        liquidity = float(market.get("liquidity", 0) or 0)
        if liquidity < 50:
            return

        # Verificar que no haya cerrado y que cierre en max 720h (30 dias)
        hours_left = self.hours_until_close(market)
        if hours_left <= 0 or hours_left > 24:
            return

        # Obtener precio del NO
        no_price, no_token_id, yes_token_id = self.get_no_price(market)
        if no_price <= 0 or not no_token_id:
            return

        # Verificar si es un mercado obvio por precio O por título
        is_obvious_price = no_price >= MIN_NO_PRICE
        is_obvious_title = self.is_obvious_no(title)

        if not (is_obvious_price or is_obvious_title):
            return

        # Si es obvio por título, verificar que el precio también sea alto
        if is_obvious_title and no_price < 0.85:
            log.info(f"Obvio por título pero precio bajo ({no_price:.2f}): {title[:50]}")
            return

        # Retorno esperado
        expected_return = (1.0 - no_price) / no_price
        if expected_return < MIN_RETURN:
            return

        obvious_by = "price" if is_obvious_price else "keyword"

        # Corroboracion independiente: para mercados de temperatura, contrasta
        # el umbral del titulo contra el pronostico real (Open-Meteo). No
        # bloquea mercados que no son de clima (ahi no hay fuente para
        # verificar) ni cuando el titulo/ciudad no se pudo parsear.
        corrob = weather_check.corroborate(
            title, market.get("endDate") or market.get("endDateIso"), no_price
        )

        def _log_decision(outcome: str, skip_reason: str = "", order=None):
            row = {
                "tick_iso": datetime.now(timezone.utc).isoformat(),
                "condition_id": condition_id, "title": title, "obvious_by": obvious_by,
                "no_price": round(no_price, 4), "expected_return": round(expected_return, 4),
                "liquidity": round(liquidity, 2), "hours_left": round(hours_left, 2),
                "corrob_source": corrob["source"], "corrob_verdict": corrob["verdict"],
                "corrob_city": corrob["city"] or "", "corrob_forecast": corrob["forecast_value"] or "",
                "corrob_threshold": corrob["threshold"] or "", "corrob_unit": corrob["unit"] or "",
                "corrob_margin": corrob["margin"] if corrob["margin"] is not None else "",
                "outcome": outcome, "skip_reason": skip_reason,
            }
            if order is not None:
                row["fail_category"] = order.fail_category
                row["requested_shares"] = order.requested_shares
                row["filled_shares"] = order.filled
                row["fill_ratio"] = order.fill_ratio
            decision_log.append_decision(row)

        if corrob["verdict"] == "CONTRADICE":
            log.info(f"  Corroboracion meteorologica CONTRADICE la apuesta "
                      f"(pronostico {corrob['forecast_value']}{corrob['unit']} vs "
                      f"umbral {corrob['threshold']}{corrob['unit']} en {corrob['city']}), "
                      f"saltando: {title[:50]}")
            self.skipped_corrob += 1
            _log_decision("skipped", "corroboracion_contradice")
            return

        # Experimento de pocas-operaciones-alta-confianza: no alcanza con "no
        # CONTRADICE" -- exige CONFIRMA con margen amplio. Esto excluye de
        # paso a todo lo SIN_DATOS (no-clima: acciones, politica, deportes),
        # ya que ahi no hay forma de verificar nada de forma independiente.
        if EXPERIMENT_FEW_HIGH_CONF:
            es_alta_confianza = (
                corrob["verdict"] == "CONFIRMA"
                and corrob["margin"] is not None
                and corrob["margin"] >= HIGH_CONFIDENCE_MIN_MARGIN
            )
            if not es_alta_confianza:
                _log_decision("skipped", "no_alta_confianza")
                return

        # Chequeo de cupo real (llamada de red en modo --live): se hace al final,
        # despues de todos los filtros locales, para no pegarle a la API por
        # cada uno de los ~600 mercados evaluados por ciclo -- solo por los
        # pocos que ya pasaron todos los demas filtros.
        real_count = self.get_real_position_count()
        if real_count >= MAX_CONCURRENT:
            self.skipped_maxpos += 1
            _log_decision("skipped", "max_posiciones")
            return

        # Solo agregar a blacklist antes — NO a open_positions todavia
        BLACKLIST_IDS.add(condition_id)

        log.info(f"MERCADO OBVIO: {title[:60]}")
        log.info(f"  NO price={no_price:.3f}  retorno={expected_return:.1%}  "
                 f"liq=${liquidity:.0f}  cierra en {hours_left:.1f}h  "
                 f"corrob={corrob['verdict']}")

        effective_cash = self.capital - sum(
            p.get("spent", BET_USD) for p in self.open_positions.values()
        )
        if EXPERIMENT_FEW_HIGH_CONF:
            # saldo disponible / cupos restantes -- sin techo de $15, a
            # proposito: la idea del experimento es apostar mas fuerte por
            # operacion, no menos.
            cupos_restantes = max(1, MAX_CONCURRENT - real_count)
            dynamic_bet = round(max(2.0, effective_cash / cupos_restantes), 2)
        else:
            # Calcular bet dinamico: 5% del efectivo disponible, min $2 max $15
            dynamic_bet = round(max(2.0, min(15.0, effective_cash * BET_PCT)), 2)

        log.info(f"  COMPRANDO NO: bet_dinamico=${dynamic_bet:.2f}  precio_reportado={no_price:.3f}")

        order = self.executor.buy_fok(
            token_id=no_token_id,
            direction="NO",
            budget_usd=dynamic_bet,
            price=no_price,
            min_price=MIN_NO_PRICE
        )

        if order and (order.success if hasattr(order, 'success') else True):
            BLACKLIST_IDS.add(condition_id)
            # Usar los valores que realmente devolvio la orden (pueden diferir del
            # precio/tamaño estimados si el order book se movio entre la evaluacion
            # y la ejecucion), no los estimados antes de comprar.
            fill_price = order.price if order.price > 0 else no_price
            fill_shares = order.filled if order.filled > 0 else order.shares
            fill_spent = order.spent if order.spent > 0 else round(fill_shares * fill_price, 2)
            self.open_positions[condition_id] = {
                "title": title,
                "no_price": fill_price,
                "shares": fill_shares,
                "spent": fill_spent,
                "expected_return": (1.0 - fill_price) / fill_price if fill_price > 0 else expected_return,
                "hours_left": hours_left,  # snapshot al entrar, se usa en el journal
                "end_date_iso": market.get("endDate") or market.get("endDateIso"),  # para recalcular en vivo
                "token_id": no_token_id,
                "time": time.time(),
                "entered_at_iso": datetime.now(timezone.utc).isoformat(),
                "order": str(order),
                "corroboration": corrob,
                "requested_shares": order.requested_shares,
                "fill_ratio": order.fill_ratio,
            }
            self.bought += 1
            _log_decision("bought", order=order)
            log.info(f"  Orden: {order}")
        else:
            log.warning(f"  Orden fallida: {order}")
            self.orders_failed += 1
            cat = getattr(order, "fail_category", "") or "otro"
            self.fail_categories[cat] = self.fail_categories.get(cat, 0) + 1
            _log_decision("skipped", f"orden_fallida:{getattr(order, 'error', '')}", order=order)
            if cat == "orden_incierta":
                # No se pudo confirmar que la orden GTC haya quedado cancelada
                # -- puede seguir viva en el libro de Polymarket. NO liberar
                # este mercado: si lo hacemos, el bot puede comprar una segunda
                # orden aca mismo, y si la primera en realidad seguia viva,
                # terminamos con dos ordenes reales sobre el mismo mercado
                # (incidente real reportado con este codigo). Se queda
                # bloqueado para el resto de la sesion hasta revision manual.
                self.uncertain_orders.append({
                    "condition_id": condition_id, "title": title,
                    "detected_at_iso": datetime.now(timezone.utc).isoformat(),
                    "order_id": getattr(order, "order_id", ""),
                })
                log.error(f"  ORDEN INCIERTA -- '{title[:60]}' queda bloqueado sin reintentar "
                          f"hasta revision manual (puede haber una orden real viva en Polymarket)")
            else:
                BLACKLIST_IDS.discard(condition_id)

    def check_resolved(self):
        """Verifica posiciones resueltas.

        Usa la CLOB API (/markets/{condition_id}), no la Gamma API: se
        confirmo en produccion que `gamma-api.../markets?condition_id=X`
        ignora ese filtro por completo y devuelve una lista por defecto no
        relacionada (ej. pedir Tokio devolvia un mercado de "Xi Jinping out
        before 2027?") -- el chequeo de resueltos nunca miraba el mercado
        correcto. La CLOB API ademas expone un campo "winner" explicito por
        outcome, evitando tener que inferir el ganador por indice/precio.
        """
        resolved = []
        for cond_id, pos in self.open_positions.items():
            try:
                r = requests.get(
                    f"{POLY_CLOB_API}/markets/{cond_id}",
                    timeout=10
                )
                if r.status_code == 200:
                    market = r.json()
                    if market.get("closed", False):
                        tokens = market.get("tokens", [])
                        no_token = next((t for t in tokens if str(t.get("outcome", "")).lower() == "no"), None)
                        won = bool(no_token.get("winner")) if no_token else False
                        if won:
                            self.wins += 1
                            pnl = pos["shares"] * (1.0 - pos["no_price"])
                            result = "WIN"
                        else:
                            self.losses += 1
                            pnl = -pos["spent"]
                            result = "LOSS"
                        self.capital += pnl
                        self._ensure_daily_pnl_current()
                        self.daily_pnl += pnl
                        log.info(f"{result} {pos['title'][:50]}  pnl={pnl:+.2f}  capital=${self.capital:.2f}")

                        corrob = pos.get("corroboration") or {}
                        espera_status = self._position_status(pos)
                        horas_espera = espera_status["esperando_desde_horas"] or 0.0
                        journal.append_trade({
                            "settled_at_iso": datetime.now(timezone.utc).isoformat(),
                            "mode": "paper" if self.paper else "real",
                            "condition_id": cond_id, "title": pos["title"],
                            "entered_at_iso": pos.get("entered_at_iso", ""),
                            "no_price_entry": round(pos["no_price"], 4),
                            "shares": pos["shares"], "spent_usd": pos["spent"],
                            "expected_return": round(pos.get("expected_return", 0), 4),
                            "hours_left_at_entry": pos.get("hours_left", ""),
                            "requested_shares": pos.get("requested_shares", ""),
                            "fill_ratio": pos.get("fill_ratio", ""),
                            "corrob_source": corrob.get("source", ""),
                            "corrob_verdict": corrob.get("verdict", ""),
                            "corrob_city": corrob.get("city") or "",
                            "corrob_forecast": corrob.get("forecast_value") if corrob.get("forecast_value") is not None else "",
                            "corrob_threshold": corrob.get("threshold") if corrob.get("threshold") is not None else "",
                            "corrob_unit": corrob.get("unit") or "",
                            "corrob_margin": corrob.get("margin") if corrob.get("margin") is not None else "",
                            "won": won, "pnl_usd": round(pnl, 4), "capital_after": round(self.capital, 2),
                            "horas_espera_post_cierre_nominal": horas_espera,
                        })

                        resolved.append(cond_id)
            except Exception as e:
                log.warning(f"check_resolved: error verificando {cond_id}: {e}")
        for cond_id in resolved:
            del self.open_positions[cond_id]

    def save_state(self, cycle: int, next_scan_ts: float):
        """Escribe el estado actual a state.json para que el dashboard lo lea."""
        self._ensure_daily_pnl_current()
        state = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "next_scan_at": datetime.fromtimestamp(next_scan_ts, tz=timezone.utc).isoformat(),
            "paper": self.paper,
            "cycle": cycle,
            "capital": round(self.capital, 2),
            "initial_capital": INITIAL_CAPITAL,
            "daily_pnl": round(self.daily_pnl, 2),
            "pnl_acumulado": round(self.capital - INITIAL_CAPITAL, 2),
            "wins": self.wins,
            "losses": self.losses,
            "scanned": self.scanned,
            "funnel": {
                "bought": self.bought,
                "skipped_corrob": self.skipped_corrob,
                "skipped_maxpos": self.skipped_maxpos,
                "orders_failed": self.orders_failed,
                "fail_categories": self.fail_categories,
            },
            "uncertain_orders": self.uncertain_orders,
            "open_positions": [
                {
                    "title": p.get("title", ""),
                    "no_price": p.get("no_price", 0),
                    "shares": p.get("shares", 0),
                    "spent": p.get("spent", 0),
                    "expected_return": p.get("expected_return", 0),
                    # Recalculado en vivo contra end_date_iso -- p.get("hours_left") es
                    # solo el snapshot al momento de comprar (se congelaba y el
                    # dashboard mostraba siempre el mismo numero, sin countdown real).
                    "hours_left": self._live_hours_left(p),
                    "fill_ratio": p.get("fill_ratio", 1.0),
                    "corrob_verdict": (p.get("corroboration") or {}).get("verdict", "SIN_DATOS"),
                    "corrob_city": (p.get("corroboration") or {}).get("city"),
                    **self._position_status(p),
                }
                for p in self.open_positions.values()
            ],
        }
        # Desglose de saldo. En paper, "pendiente_liberar" siempre da 0 porque
        # check_resolved() acredita el pnl al capital en el mismo instante que
        # detecta el cierre (no hay paso de redencion que simular) -- pero el
        # campo queda listo para cuando haya redencion real en --live (ver
        # conversacion sobre CTF/redeemPositions): ahi "pendiente_liberar" seria
        # el valor de las posiciones ya resueltas pero sin redimir on-chain.
        comprometido = round(sum(p.get("spent", 0) for p in self.open_positions.values()), 2)
        state["balance"] = {
            "total": round(self.capital, 2),
            "comprometido_en_curso": comprometido,
            "pendiente_liberar_polymarket": 0.0,
            "disponible": round(self.capital - comprometido, 2),
        }
        tmp_path = Path("state.json.tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        # En Windows, reemplazar state.json puede chocar transitoriamente con el
        # servidor del dashboard leyendolo (o el antivirus escaneandolo) en el
        # mismo instante -- un PermissionError aca tumbaba TODO el bot (se vio
        # en produccion: crasheo tras 548 ciclos y ~7 trades resueltos, perdiendo
        # el estado de las posiciones abiertas en memoria). Reintentar unas
        # pocas veces y, si sigue fallando, solo loggear -- el dashboard se
        # queda con el dato del ciclo anterior por unos segundos, no es motivo
        # para perder horas de posiciones abiertas.
        for attempt in range(5):
            try:
                tmp_path.replace("state.json")
                break
            except PermissionError as e:
                if attempt == 4:
                    log.warning(f"save_state: no se pudo reemplazar state.json tras varios intentos: {e}")
                else:
                    time.sleep(0.2)

    async def run(self):
        log.info("Iniciando escaneo de mercados obvios...")
        cycle = 0
        self.save_state(cycle, time.time() + 300)

        while True:
            # Stop loss checks
            self._ensure_daily_pnl_current()
            if self.daily_pnl <= -STOP_DAILY_USD:
                log.warning(f"STOP LOSS DIARIO: {self.daily_pnl:.2f} -- parando")
                break
            if (self.capital - INITIAL_CAPITAL) <= -(INITIAL_CAPITAL * STOP_TOTAL_PCT):
                log.warning("STOP LOSS TOTAL -- parando")
                break

            # Escanear mercados: una sola pasada paginando toda la lista
            # global de activos (ver nota en SCAN_OFFSETS sobre por que ya no
            # se itera por tag). seen_this_cycle evita reevaluar el mismo
            # mercado si el orden por volumen se solapa entre paginas
            # (confirmado que no es perfectamente estable).
            log.info(f"Escaneando mercados... (ciclo {cycle})")
            total_found = 0
            seen_this_cycle = set()

            for offset in SCAN_OFFSETS:
                markets = self.scan_markets(offset)
                if markets is None:
                    # fallo de red en esta pagina puntual -- probar la
                    # siguiente en vez de abandonar el resto del escaneo
                    await asyncio.sleep(0.5)
                    continue
                if not markets:
                    break  # fin real de la lista
                for market in markets:
                    cid = market.get("conditionId", "")
                    if cid and (cid in BLACKLIST_IDS or cid in seen_this_cycle):
                        continue
                    if cid:
                        seen_this_cycle.add(cid)
                    await self.evaluate_market(market)
                    total_found += 1
                await asyncio.sleep(0.5)

            # Pasada adicional especifica de clima, via /events (paginacion
            # estable) -- ver nota junto a WEATHER_EVENT_OFFSETS. seen_this_cycle
            # ya evita reevaluar un mercado si tambien aparecio en la pasada de
            # arriba.
            for offset in WEATHER_EVENT_OFFSETS:
                markets = self.scan_weather_events(offset)
                if markets is None:
                    await asyncio.sleep(0.5)
                    continue
                if not markets:
                    break
                for market in markets:
                    cid = market.get("conditionId", "")
                    if cid and (cid in BLACKLIST_IDS or cid in seen_this_cycle):
                        continue
                    if cid:
                        seen_this_cycle.add(cid)
                    await self.evaluate_market(market)
                    total_found += 1
                await asyncio.sleep(0.5)

            self.scanned += total_found

            # Verificar resueltos cada 5 ciclos
            if cycle % 5 == 0:
                self.check_resolved()

            # Status
            wr = f"{self.wins/(self.wins+self.losses)*100:.0f}%" if (self.wins+self.losses) > 0 else "N/A"
            log.info(f"STATUS: capital=${self.capital:.2f}  pnl={self.daily_pnl:+.2f}  "
                     f"{self.wins}W/{self.losses}L ({wr})  "
                     f"pos={len(self.open_positions)}  escaneados={self.scanned}")

            self.save_state(cycle, time.time() + 300)

            cycle += 1
            # Escanear cada 5 minutos
            await asyncio.sleep(300)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    args = p.parse_args()
    if args.live:
        if input("OBVIO BOT REAL. Escribe SI: ").strip() != "SI":
            sys.exit(0)
    bot = ObvioBot(paper=not args.live)
    asyncio.run(bot.run())
