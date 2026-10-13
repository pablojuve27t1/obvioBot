"""
executor.py — Polymarket order execution (FAK con precio real del order book)
"""

import logging, time, os, requests, math
from dataclasses import dataclass, field
from dotenv import load_dotenv

import polymarket_dns  # parchea DNS para *.polymarket.com (bloqueo ISP)

load_dotenv()
log = logging.getLogger("executor")

try:
    from py_clob_client_v2 import (ClobClient, OrderArgs, OrderType,
                                    PartialCreateOrderOptions, Side,
                                    SignatureTypeV2)
    from py_clob_client_v2 import ApiCreds
    SDK_OK = True
except ImportError as e:
    SDK_OK = False
    log.warning(f"py-clob-client-v2 no instalado -> paper: {e}")

POLY_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137


@dataclass
class OrderResult:
    success:          bool
    order_id:         str   = ""
    shares:           float = 0.0
    price:            float = 0.0
    spent:            float = 0.0
    filled:           float = 0.0
    requested_shares: float = 0.0  # tamaño que se intento comprar, para medir fill parcial
    fail_category:    str   = ""   # "" en exito; liquidez/precio_movido/invalido/otro en fallo
    simulated:        bool  = False
    error:            str   = ""
    ts:               float = field(default_factory=time.time)

    @property
    def fill_ratio(self) -> float:
        if self.requested_shares <= 0:
            return 1.0 if self.success else 0.0
        return round(self.filled / self.requested_shares, 4)

    def __str__(self):
        tag = "[SIM]" if self.simulated else "[REAL]"
        if not self.success:
            return f"{tag} FALLO ({self.fail_category or 'otro'}): {self.error}"
        return (f"{tag} order={self.order_id[:12]}  "
                f"shares={self.shares:.2f}  price={self.price:.4f}  "
                f"spent={self.spent:.2f}  filled={self.filled:.2f}  "
                f"fill={self.fill_ratio:.0%}")


def _classify_error(error: str) -> str:
    """Clasifica por que fallo una orden, para poder distinguir friccion de
    liquidez real de otros motivos (precio que se movio, datos invalidos)."""
    e = (error or "").lower()
    if "timeout" in e or "sin contraparte" in e or "sin match" in e or "sin liquidez" in e:
        return "liquidez"
    if "ask fuera de rango" in e or "ask demasiado bajo" in e:
        return "liquidez"
    if "minimo" in e or "fuera de rango" in e:
        return "precio_movido"
    if "invalid token" in e:
        return "invalido"
    if "order book" in e:
        return "otro"
    return "otro"


class Executor:
    def __init__(self, private_key="", funder="", paper=True):
        self.paper    = paper or not SDK_OK or not private_key
        self._client  = None
        self._sim_seq = 0
        self._funder  = funder
        if not self.paper and SDK_OK and private_key:
            self._init_client(private_key)

    def _init_client(self, key):
        try:
            creds = ApiCreds(
                api_key        = os.getenv("POLY_API_KEY", ""),
                api_secret     = os.getenv("POLY_API_SECRET", ""),
                api_passphrase = os.getenv("POLY_API_PASSPHRASE", ""),
            )
            self._client = ClobClient(
                host           = POLY_HOST,
                chain_id       = CHAIN_ID,
                key            = key,
                creds          = creds,
                signature_type = SignatureTypeV2.POLY_1271,
                funder         = self._funder,
            )
            log.info(f"ClobClient POLY_1271 OK -- {self._client.get_address()}")
        except Exception as e:
            log.error(f"ClobClient error: {e} -> paper")
            self._client = None
            self.paper   = True

    def get_best_ask(self, token_id: str):
        """Obtiene el mejor precio ask real del order book. None si falla la
        consulta o no hay asks -- nunca inventa un precio, para que el llamador
        decida explicitamente que hacer ante un error de red."""
        try:
            r = requests.get(
                f"{POLY_HOST}/book?token_id={token_id}",
                timeout=3
            )
            book = r.json()
            asks = book.get("asks", [])
            if asks:
                prices = sorted([float(a["price"]) for a in asks])
                return prices[0]
        except Exception as e:
            log.warning(f"orderbook error: {e}")
        return None

    def buy_fok(self, token_id, direction, budget_usd, price, min_price=None):
        """FAK con precio real del order book — evita comprar a precios extremos."""
        price_r = round(price, 2)
        if not (0.01 <= price_r <= 0.99):
            return OrderResult(success=False, error=f"precio {price_r} fuera de rango",
                                fail_category=_classify_error(f"precio {price_r} fuera de rango"))

        if self.paper:
            price_cents  = round(price_r * 100)
            budget_cents = round(budget_usd * 100)
            shares       = float(max(budget_cents // price_cents, 5))
            spent        = round(shares * price_r, 2)
            log.info(f"FAK BUY {direction}: price={price_r:.2f}  shares={shares:.0f}  spent={spent:.2f}")
            return self._sim_buy(direction, price_r, shares, spent)

        # Obtener mejor ask real del order book
        best_ask = self.get_best_ask(token_id)
        if best_ask is None:
            err = "No se pudo consultar el order book real"
            return OrderResult(success=False, error=err, fail_category=_classify_error(err))
        log.info(f"Mejor ask real: {best_ask:.4f}  (reportado: {price_r:.4f})")

        # Verificar que el ask es razonable
        # Para mercados obvios (NO > 0.90) se permite precio alto
        if best_ask < 0.05:
            log.warning(f"Ask demasiado bajo ({best_ask:.4f}), skip")
            err = f"Ask fuera de rango: {best_ask:.4f}"
            return OrderResult(success=False, error=err, fail_category=_classify_error(err))

        # Abortar si el precio real ya no cumple el umbral minimo de la estrategia
        # (evita comprar a precios muy peores que el reportado, ej. mercado que crasheo)
        if min_price is not None and best_ask < min_price:
            log.warning(f"Ask real ({best_ask:.4f}) por debajo del minimo aceptable "
                        f"({min_price:.4f}), abortando compra")
            err = f"Precio real {best_ask:.4f} < minimo {min_price:.4f}"
            return OrderResult(success=False, error=err, fail_category=_classify_error(err))

        # Calcular shares con precio real
        price_real   = min(0.99, round(best_ask, 2))
        price_cents  = round(price_real * 100)
        budget_cents = round(budget_usd * 100)
        shares       = float(max(budget_cents // price_cents, 1))
        min_shares   = max(5, int(math.ceil(1.0 / price_real)))
        shares       = float(max(shares, min_shares))
        spent        = round(shares * price_real, 2)

        log.info(f"FAK BUY {direction}: price={price_real:.2f}  shares={shares:.0f}  spent={spent:.2f}")

        try:
            resp = self._client.create_and_post_order(
                order_args=OrderArgs(
                    token_id=token_id,
                    price=price_real,
                    side=Side.BUY,
                    size=shares,
                ),
                options=PartialCreateOrderOptions(tick_size="0.01"),
                order_type=OrderType.GTC,
            )
            log.info(f"GTC respuesta: {resp}")
            if resp and resp.get("success"):
                taking = float(resp.get("takingAmount", "") or 0)
                making = float(resp.get("makingAmount", "") or 0)
                order_id = resp.get("orderID", "")
                if taking > 0:
                    log.info(f"GTC llenado inmediato: taking={taking}  making={making}")
                    return OrderResult(
                        success  = True,
                        order_id = order_id,
                        shares   = taking,
                        price    = price_real,
                        spent    = round(making, 2),
                        filled   = taking,
                        requested_shares = shares,
                    )
                elif order_id:
                    log.info(f"GTC en book: {order_id[:16]}  esperando 30s...")
                    for _ in range(30):
                        time.sleep(1)
                        try:
                            order = self._client.get_order(order_id)
                            status = order.get("status", "")
                            matched = float(order.get("size_matched", order.get("sizeMatched", 0)) or 0)
                            if status in ("MATCHED", "FILLED") or matched > 0:
                                log.info(f"GTC llenado: matched={matched}")
                                try:
                                    self._client.cancel_order(order_id)
                                except:
                                    pass
                                return OrderResult(
                                    success  = True,
                                    order_id = order_id,
                                    shares   = matched or shares,
                                    price    = price_real,
                                    spent    = round((matched or shares) * price_real, 2),
                                    filled   = matched or shares,
                                    requested_shares = shares,
                                )
                            if status in ("CANCELLED", "EXPIRED"):
                                break
                        except:
                            pass
                    # Chequeo final antes de cancelar - cierra la ventana de carrera
                    # donde la orden se llena justo cuando el polling ya termino
                    final_matched = 0.0
                    try:
                        final_check = self._client.get_order(order_id)
                        log.warning(f"GTC timeout - chequeo final: {final_check}")
                        final_matched = float(final_check.get("size_matched", final_check.get("sizeMatched", 0)) or 0)
                        if str(final_check.get("status", "")).upper() in ("MATCHED", "FILLED") and final_matched == 0:
                            final_matched = shares  # status confirma match pero size_matched no vino; usar tamano de la orden
                    except Exception as e:
                        log.warning(f"GTC timeout - chequeo final fallo: {e}")
                    if final_matched > 0:
                        log.info(f"GTC match detectado en chequeo final: matched={final_matched}")
                        try:
                            self._client.cancel_order(order_id)
                        except Exception:
                            pass
                        return OrderResult(
                            success  = True,
                            order_id = order_id,
                            shares   = final_matched,
                            price    = price_real,
                            spent    = round(final_matched * price_real, 2),
                            filled   = final_matched,
                            requested_shares = shares,
                        )

                    # Intentar cancelar, y verificar DE VERDAD que la orden quedo
                    # muerta -- no asumir que cancel_order() funciono solo porque
                    # no lanzo excepcion, ni asumir que la orden esta muerta si
                    # cancel_order() fallo. Sin esto: una cancelacion que falla
                    # en silencio (red, API, o la orden se lleno justo en esa
                    # ventana) deja una orden REAL viva en el libro de Polymarket
                    # mientras el bot la trata como "fallida" y libera el mercado
                    # para volver a comprarlo -- terminas con dos ordenes en el
                    # mismo mercado, llenandose las dos cuando el precio se mueve
                    # (incidente real reportado por un operador con este mismo
                    # codigo, con otra wallet).
                    cancel_ok = False
                    try:
                        cancel_resp = self._client.cancel_order(order_id)
                        cancel_ok = bool(cancel_resp) if cancel_resp is not None else True
                    except Exception as e:
                        log.warning(f"GTC cancel_order fallo: {e}")

                    try:
                        verify = self._client.get_order(order_id)
                        v_status = str(verify.get("status", "")).upper()
                        v_matched = float(verify.get("size_matched", verify.get("sizeMatched", 0)) or 0)
                    except Exception as e:
                        log.warning(f"GTC verificacion post-cancelacion fallo: {e}")
                        v_status, v_matched = "", 0.0

                    if v_matched > 0 or v_status in ("MATCHED", "FILLED"):
                        log.info(f"GTC match detectado en verificacion post-cancelacion: matched={v_matched}")
                        return OrderResult(
                            success  = True,
                            order_id = order_id,
                            shares   = v_matched or shares,
                            price    = price_real,
                            spent    = round((v_matched or shares) * price_real, 2),
                            filled   = v_matched or shares,
                            requested_shares = shares,
                        )

                    if v_status in ("CANCELLED", "EXPIRED") or cancel_ok:
                        log.warning(f"GTC cancelado por timeout (confirmado: status={v_status or 'desconocido'})")
                        err = "GTC timeout - sin contraparte en 30s"
                        return OrderResult(success=False, error=err, requested_shares=shares,
                                            fail_category=_classify_error(err))

                    # No se pudo confirmar que la orden este muerta: pudo quedar
                    # viva en el libro. No es seguro tratar esto como un fallo
                    # comun -- el llamador NO debe volver a intentar este mismo
                    # mercado todavia.
                    log.error(f"GTC orden_id={order_id} -- no se pudo confirmar cancelacion ni estado final "
                              f"(status reportado: '{v_status or 'desconocido'}'). Puede seguir viva en el "
                              f"libro de Polymarket. Revisar manualmente antes de reintentar este mercado.")
                    return OrderResult(success=False, error="orden_incierta_posible_viva",
                                        requested_shares=shares, fail_category="orden_incierta")
            err = str(resp)
            return OrderResult(success=False, error=err, requested_shares=shares,
                                fail_category=_classify_error(err))
        except Exception as e:
            err_str = str(e)
            if "no orders found to match with FAK" in err_str:
                log.warning(f"FAK sin match: {err_str[:80]}")
                err = "FAK no match - sin liquidez"
                return OrderResult(success=False, error=err, requested_shares=shares,
                                    fail_category=_classify_error(err))
            if "Invalid token id" in err_str:
                log.warning(f"Token ID invalido, skip")
                return OrderResult(success=False, error="Invalid token id", requested_shares=shares,
                                    fail_category=_classify_error("Invalid token id"))
            log.error(f"buy_fak error: {e}")
            return OrderResult(success=False, error=err_str, requested_shares=shares,
                                fail_category=_classify_error(err_str))

    def cancel_all(self):
        if self.paper:
            return True
        try:
            self._client.cancel_all()
            return True
        except Exception as e:
            log.error(f"cancel_all error: {e}")
            return False

    def _sim_buy(self, direction, price, shares, spent):
        self._sim_seq += 1
        return OrderResult(
            success=True,
            order_id=f"SIM-{self._sim_seq:04d}",
            shares=shares, price=price,
            spent=spent, filled=shares,
            requested_shares=shares,
            simulated=True
        )
