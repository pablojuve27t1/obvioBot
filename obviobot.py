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

load_dotenv()

PRIVATE_KEY     = os.getenv("POLY_PRIVATE_KEY", "")
FUNDER          = os.getenv("POLY_FUNDER", "")
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "50.88"))
STOP_DAILY_USD  = float(os.getenv("STOP_LOSS_DAILY_USD", "5.0"))
STOP_TOTAL_PCT  = float(os.getenv("STOP_LOSS_TOTAL_PCT", "0.20"))
MAX_CONCURRENT  = int(os.getenv("MAX_CONCURRENT", "7"))

POLY_GAMMA_API  = "https://gamma-api.polymarket.com"
POLY_CLOB_API   = "https://clob.polymarket.com"

# Precio mínimo para comprar NO (95¢ o más = casi seguro)
MIN_NO_PRICE    = 0.94
MAX_NO_PRICE    = 0.99

# Retorno mínimo esperado (1¢ por share mínimo)
MIN_RETURN      = 0.01

# Apuesta fija por trade
BET_PCT         = 0.05  # 5% del capital disponible por trade
BET_USD         = 2.0   # se calcula dinamicamente en evaluate_market

# Categorías a buscar
SCAN_TAGS       = ["politics", "crypto", "science", "technology", "world", "economics"]

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
        self.capital        = INITIAL_CAPITAL
        self.open_positions = {}
        self.daily_pnl      = 0.0
        self.wins           = 0
        self.losses         = 0
        self.scanned        = 0

        from executor import Executor
        self.executor = Executor(private_key=PRIVATE_KEY, funder=FUNDER, paper=paper)
        if not paper:
            self.reconcile_open_positions()

        tag = "PAPER" if paper else "REAL MONEY"
        log.info("=" * 60)
        log.info(f"  Obvio Bot — Mercados imposibles -- {tag}")
        log.info(f"  Capital: ${INITIAL_CAPITAL:.2f}  Bet: ${BET_USD}/trade")
        log.info(f"  Estrategia: comprar NO cuando precio >= {MIN_NO_PRICE:.0%}")
        log.info(f"  Stop diario: -${STOP_DAILY_USD}  Max posiciones: {MAX_CONCURRENT}")
        log.info("=" * 60)

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

    def scan_markets(self, tag: str, offset: int = 0) -> list:
        """Escanea mercados activos por tag."""
        try:
            r = requests.get(
                f"{POLY_GAMMA_API}/markets",
                params={
                    "tag": tag,
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
                return r.json() if isinstance(r.json(), list) else []
        except Exception as e:
            log.warning(f"Error scan {tag}: {e}")
        return []

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
        except:
            return 999

    async def evaluate_market(self, market: dict):
        """Evalúa si un mercado vale la pena."""
        condition_id = market.get("conditionId", "")
        if not condition_id or condition_id in BLACKLIST_IDS:
            return
        if condition_id in self.open_positions:
            return
        real_count = self.get_real_position_count()
        if real_count >= MAX_CONCURRENT:
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

        # Solo agregar a blacklist antes — NO a open_positions todavia
        BLACKLIST_IDS.add(condition_id)

        log.info(f"MERCADO OBVIO: {title[:60]}")
        log.info(f"  NO price={no_price:.3f}  retorno={expected_return:.1%}  "
                 f"liq=${liquidity:.0f}  cierra en {hours_left:.1f}h")

        # Calcular bet dinamico: 5% del efectivo disponible, min $2 max $15
        effective_cash = self.capital - sum(
            p.get("spent", BET_USD) for p in self.open_positions.values()
            if not p.get("pending")
        )
        dynamic_bet = round(max(2.0, min(15.0, effective_cash * BET_PCT)), 2)
        shares = max(5, int(dynamic_bet / no_price))
        spent  = round(shares * no_price, 2)

        log.info(f"  COMPRANDO NO: shares={shares}  spent=${spent:.2f}  bet_dinamico=${dynamic_bet:.2f}")

        order = self.executor.buy_fok(
            token_id=no_token_id,
            direction="NO",
            budget_usd=BET_USD,
            price=no_price,
            min_price=MIN_NO_PRICE
        )

        if order and (order.success if hasattr(order, 'success') else True):
            BLACKLIST_IDS.add(condition_id)
            self.open_positions[condition_id] = {
                "title": title,
                "no_price": no_price,
                "shares": shares,
                "spent": spent,
                "expected_return": expected_return,
                "hours_left": hours_left,
                "token_id": no_token_id,
                "time": time.time(),
                "order": str(order)
            }
            log.info(f"  Orden: {order}")
        else:
            log.warning(f"  Orden fallida: {order}")
            if condition_id in self.open_positions and self.open_positions[condition_id].get("pending"):
                del self.open_positions[condition_id]
            BLACKLIST_IDS.discard(condition_id)

    def check_resolved(self):
        """Verifica posiciones resueltas."""
        resolved = []
        for cond_id, pos in self.open_positions.items():
            try:
                r = requests.get(
                    f"{POLY_GAMMA_API}/markets?condition_id={cond_id}",
                    timeout=10
                )
                if r.status_code == 200:
                    data = r.json()
                    market = data[0] if isinstance(data, list) and data else data
                    if market.get("closed", False):
                        winner = (market.get("winner") or "").upper()
                        won = "NO" in winner or "DOWN" in winner or winner == "0"
                        if won:
                            self.wins += 1
                            pnl = pos["shares"] * (1.0 - pos["no_price"])
                            result = "WIN"
                        else:
                            self.losses += 1
                            pnl = -pos["spent"]
                            result = "LOSS"
                        self.capital   += pnl
                        self.daily_pnl += pnl
                        log.info(f"{result} {pos['title'][:50]}  pnl={pnl:+.2f}  capital=${self.capital:.2f}")
                        resolved.append(cond_id)
            except:
                pass
        for cond_id in resolved:
            del self.open_positions[cond_id]

    async def run(self):
        log.info("Iniciando escaneo de mercados obvios...")
        cycle = 0

        while True:
            # Stop loss checks
            if self.daily_pnl <= -STOP_DAILY_USD:
                log.warning(f"STOP LOSS DIARIO: {self.daily_pnl:.2f} -- parando")
                break
            if (self.capital - INITIAL_CAPITAL) <= -(INITIAL_CAPITAL * STOP_TOTAL_PCT):
                log.warning("STOP LOSS TOTAL -- parando")
                break

            # Escanear mercados
            log.info(f"Escaneando mercados... (ciclo {cycle})")
            total_found = 0

            for tag in SCAN_TAGS:
                markets = self.scan_markets(tag)
                for market in markets:
                    cid = market.get("conditionId", "")
                    if cid and cid in BLACKLIST_IDS:
                        continue
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

            cycle += 1
            # Escanear cada 10 minutos
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
