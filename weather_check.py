"""
weather_check.py -- corroboracion independiente para mercados de temperatura.

La estrategia del bot es comprar "NO" en mercados de temperatura con umbral
simple (ej. "Will the high in Miami reach 95F on Aug 27?") cuando el precio
ya sugiere que el mercado lo ve casi imposible. Este modulo intenta verificar
esa apuesta contra una fuente independiente -- el pronostico meteorologico
real (Open-Meteo, gratis, sin API key) -- en vez de confiar solo en que el
precio de Polymarket ya "sabe" lo correcto.

No es infalible: depende de poder extraer la ciudad y el umbral del titulo
del mercado, y de que el pronostico a <=24h sea razonablemente preciso
(generalmente lo es). Cuando no se puede parsear o el pronostico no esta
disponible, se devuelve verdict="SIN_DATOS" y no bloquea la operacion -- es
una señal adicional, no un oraculo.

La ciudad NO se busca contra una lista fija: se extrae directamente del
titulo con regex (todo lo que sigue a "in <Ciudad>" antes del verbo/fecha) y
se geocodifica en vivo contra Open-Meteo, asi cubre cualquier ciudad que
Polymarket use sin necesidad de precargarla. Las pocas abreviaturas que no
geocodifican bien solas (NYC, LA, SF, DC) se normalizan antes.
"""
import logging
import re
import time

import requests

log = logging.getLogger("weather_check")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Abreviaturas que no geocodifican bien por si solas -- se normalizan antes
# de mandarlas a Open-Meteo. Todo lo demas se extrae y geocodifica en vivo.
_ABBREV_MAP = {
    "NYC": "New York", "LA": "Los Angeles", "SF": "San Francisco",
    "DC": "Washington, District of Columbia", "Philly": "Philadelphia",
}
_ABBREV_RE = re.compile(r"\b(NYC|LA|SF|DC|Philly)\b")

# "in <Ciudad> be/reach/exceed/..." -- patron dominante en los titulos de
# Polymarket ("Will the highest temperature in Tokyo be 35°C on August 27?").
# Se prueban en orden: el primero que matchee gana.
_CITY_EXTRACT_PATTERNS = [
    re.compile(
        r"\bin\s+([A-Z][A-Za-z.''-]+(?:\s+[A-Z][A-Za-z.''-]+){0,3}?)\s+"
        r"(?:be|reach|reaches|exceed|exceeds|hit|hits|drop|drops|fall|falls|"
        r"stay|stays|get|gets|climb|climbs|today|tomorrow|on\s+\w)"
    ),
    re.compile(r"\bin\s+([A-Z][A-Za-z.''-]+(?:\s+[A-Z][A-Za-z.''-]+){0,3}?)[,.?!]"),
    re.compile(r"\bin\s+([A-Z][A-Za-z.''-]+(?:\s+[A-Z][A-Za-z.''-]+){0,3})\b"),
]

GTE_PHRASES = [
    "at least", "or higher", "or above", "or more", "reach", "reaches",
    "exceed", "exceeds", "above", "over", "higher than", "greater than",
]
LTE_PHRASES = [
    "at most", "or lower", "or below", "or less", "below", "under",
    "lower than", "less than", "drop below", "fall below",
]

_TEMP_RE = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*(f|c)\b", re.IGNORECASE)

_GEOCODE_CACHE = {}   # city -> (lat, lon) | None
_FORECAST_CACHE = {}  # (lat, lon, date, unit) -> value | None, con timestamp
_CACHE_TTL = 1800  # 30 min


def _extract_city(title: str):
    """Extrae el nombre de ciudad del titulo original (con mayusculas), sin
    depender de una lista precargada. None si no se encuentra nada parseable."""
    abbrev = _ABBREV_RE.search(title)
    if abbrev:
        return _ABBREV_MAP[abbrev.group(1)]
    for pat in _CITY_EXTRACT_PATTERNS:
        m = pat.search(title)
        if m:
            candidate = m.group(1).strip()
            if 2 <= len(candidate) <= 40:
                return candidate
    return None


def _find_direction(title_lower: str):
    """'gte'/'lte' solo si el titulo trae una palabra de comparacion explicita
    (exceed/at least/below/...). Si no la trae, None -- la mayoria de los
    mercados de temperatura de Polymarket son de "bucket" (resuelven YES si
    la lectura real cae en el rango que contiene exactamente ese valor, ej.
    "Will the highest temperature in Wuhan be 35C?" sin calificador), NO un
    umbral abierto tipo "al menos". Confirmado empiricamente: los mercados
    hermanos de un mismo dia/ciudad (33C, 34C, 35C, ... 41C) muestran una
    campana de probabilidad centrada en el valor real, no una escalera
    acumulativa -- ver notas en corroborate().
    """
    for p in LTE_PHRASES:
        if p in title_lower:
            return "lte"
    for p in GTE_PHRASES:
        if p in title_lower:
            return "gte"
    return None


def parse_weather_market(title: str):
    """Extrae ciudad, umbral, unidad y direccion del titulo. None si no
    parece un mercado de temperatura reconocible."""
    m = _TEMP_RE.search(title)
    if not m:
        return None
    city = _extract_city(title)
    if not city:
        return None
    title_lower = title.lower()
    threshold = float(m.group(1))
    unit = m.group(2).upper()
    direction = _find_direction(title_lower)
    temp_kind = "min" if any(w in title_lower for w in ["low temperature", "overnight low", "minimum"]) else "max"
    return {
        "city": city, "threshold": threshold, "unit": unit,
        "direction": direction, "temp_kind": temp_kind,
    }


def _geocode(city: str):
    if city in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[city]
    try:
        r = requests.get(GEOCODE_URL, params={"name": city, "count": 1, "language": "en"}, timeout=5)
        if r.status_code == 200:
            results = r.json().get("results") or []
            if results:
                coords = (results[0]["latitude"], results[0]["longitude"])
                _GEOCODE_CACHE[city] = coords
                return coords
    except Exception as e:
        log.warning(f"geocode error para '{city}': {e}")
    _GEOCODE_CACHE[city] = None
    return None


def _forecast(lat: float, lon: float, date: str, unit: str, temp_kind: str):
    cache_key = (lat, lon, date, unit, temp_kind)
    cached = _FORECAST_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        field = "temperature_2m_max" if temp_kind == "max" else "temperature_2m_min"
        r = requests.get(FORECAST_URL, params={
            "latitude": lat, "longitude": lon,
            "daily": field,
            "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
            "timezone": "auto",
            "start_date": date, "end_date": date,
        }, timeout=5)
        if r.status_code == 200:
            values = (r.json().get("daily") or {}).get(field) or []
            if values and values[0] is not None:
                value = float(values[0])
                _FORECAST_CACHE[cache_key] = (value, time.time() + _CACHE_TTL)
                return value
    except Exception as e:
        log.warning(f"forecast error para ({lat},{lon},{date}): {e}")
    _FORECAST_CACHE[cache_key] = (None, time.time() + _CACHE_TTL)
    return None


def _empty_result(reason: str = "no_parseable"):
    return {
        "matched": False, "source": "open-meteo", "city": None,
        "threshold": None, "unit": None, "direction": None,
        "forecast_value": None, "forecast_date": None, "margin": None,
        "verdict": "SIN_DATOS", "reason": reason,
    }


def corroborate(title: str, end_date_iso: str, no_price: float) -> dict:
    """Punto de entrada principal. No lanza excepciones -- ante cualquier
    fallo devuelve verdict=SIN_DATOS para no bloquear la operacion."""
    if not end_date_iso:
        return _empty_result("sin_fecha_cierre")

    parsed = parse_weather_market(title)
    if not parsed:
        return _empty_result("no_es_mercado_de_temperatura_reconocible")

    coords = _geocode(parsed["city"])
    if not coords:
        return _empty_result(f"geocoding_fallo:{parsed['city']}")

    date_str = end_date_iso[:10]  # YYYY-MM-DD
    forecast_value = _forecast(coords[0], coords[1], date_str, parsed["unit"], parsed["temp_kind"])
    if forecast_value is None:
        return _empty_result("forecast_no_disponible")

    threshold = parsed["threshold"]
    direction = parsed["direction"]

    if direction is None:
        # Mercado de "bucket": YES gana si la lectura real cae CERCA de este
        # valor especifico, en cualquier direccion -- no es un umbral con un
        # solo lado seguro. NO es seguro cuando el pronostico esta lejos del
        # valor de este bucket, sea por arriba o por abajo.
        # Margenes calibrados contra datos reales (ver Wuhan 27-ago-2026: la
        # campana de probabilidad de los buckets hermanos cae a ~2-3% a 3
        # grados del pico y ya es notable (>10%) a 1-2 grados).
        near = 1.5 if parsed["unit"] == "C" else 3.0
        far = 3.0 if parsed["unit"] == "C" else 5.5
        distance = abs(forecast_value - threshold)
        margin = distance
        if distance >= far:
            verdict = "CONFIRMA"
        elif distance <= near:
            verdict = "CONTRADICE"
        else:
            verdict = "INCIERTO"
    elif direction == "gte":
        # YES gana si forecast >= threshold. El bot apuesta a NO.
        buffer_ = 1.0 if parsed["unit"] == "C" else 2.0
        margin = threshold - forecast_value  # positivo = pronostico se queda corto del umbral (apoya NO)
        if forecast_value <= threshold - buffer_:
            verdict = "CONFIRMA"
        elif forecast_value >= threshold + buffer_:
            verdict = "CONTRADICE"
        else:
            verdict = "INCIERTO"
    else:
        # YES gana si forecast <= threshold.
        buffer_ = 1.0 if parsed["unit"] == "C" else 2.0
        margin = forecast_value - threshold  # positivo = pronostico se queda por encima del umbral (apoya NO)
        if forecast_value >= threshold + buffer_:
            verdict = "CONFIRMA"
        elif forecast_value <= threshold - buffer_:
            verdict = "CONTRADICE"
        else:
            verdict = "INCIERTO"

    return {
        "matched": True, "source": "open-meteo", "city": parsed["city"],
        "threshold": threshold, "unit": parsed["unit"], "direction": direction,
        "forecast_value": forecast_value, "forecast_date": date_str,
        "margin": round(margin, 2), "verdict": verdict, "reason": "",
    }
