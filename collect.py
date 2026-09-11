#!/usr/bin/env python3
"""
Collector de tipo de cambio USD/PEN — Benchmark Cambix
Versión: 1.1 (2026-09-11)

Fuentes:
  - Cambix (API propia)
  - Rextie (GraphQL, incluye SUNAT y AVG_BANKS)
  - TKambio (WP admin-ajax, incluye IBK como proxy)
  - TuCambista (HTML / Next.js RSC con fallback al widget SSR)
  - Kambista (API REST)

Destino:
  - Tabla `rates` en Supabase (REST API)

Variables de entorno requeridas:
  SUPABASE_URL
  SUPABASE_KEY
  CAMBIX_SUBSCRIPTION_KEY (opcional; mantiene fallback actual para no romper el flujo)

Cambios v1.1:
  - TuCambista ya no exige que `competition` esté completo dentro de un solo
    self.__next_f.push(). Se reconstruye el stream RSC concatenando chunks.
  - Fallback adicional: lee Compra/Venta directamente del widget SSR visible.
  - Diagnóstico compacto si TuCambista cambia otra vez su frontend.
  - Validación central de tasas antes de insertar en Supabase.
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone

import requests


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# Se conserva el fallback existente para no romper el collector actual.
# Recomendación posterior: mover siempre esta clave a GitHub Secrets.
CAMBIX_SUBSCRIPTION_KEY = os.environ.get(
    "CAMBIX_SUBSCRIPTION_KEY",
    "d8fe90e920e944838711021952a3d2d5",
)

HEADERS_UA = {
    "User-Agent": "Mozilla/5.0 (compatible; PulsoCambiarioBot/1.0)"
}

# TuCambista usa Next.js y puede tratar de forma distinta algunos User-Agent.
# Solo para esta fuente usamos un UA de navegador convencional.
TUCAMBISTA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def fetch_with_retry(fn, retries=2, delay=5):
    """Reintenta una función de captura ante errores transitorios."""
    last_exc = None

    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc

            if attempt < retries:
                print(
                    f"↻ Reintento {attempt + 1}/{retries} "
                    f"en {delay}s: {exc}"
                )
                time.sleep(delay)

    raise last_exc


def validate_rate(row):
    """
    QA mínimo antes de insertar una tasa.

    Reglas:
      - compra/venta deben ser numéricas y positivas
      - compra no puede superar venta
      - USD/PEN debe caer dentro de un rango defensivo amplio
    """
    provider = row.get("provider", "DESCONOCIDO")
    buy = float(row["buy"])
    sell = float(row["sell"])

    if buy <= 0 or sell <= 0:
        raise ValueError(
            f"{provider}: tasa no positiva (buy={buy}, sell={sell})"
        )

    if buy > sell:
        raise ValueError(
            f"{provider}: spread negativo/invertido "
            f"(buy={buy}, sell={sell})"
        )

    # Rango deliberadamente amplio: sirve solo para detener errores de parseo
    # evidentes (por ejemplo 3377 en lugar de 3.377).
    if not (2.0 <= buy <= 6.0 and 2.0 <= sell <= 6.0):
        raise ValueError(
            f"{provider}: tasa USD/PEN fuera de rango defensivo "
            f"(buy={buy}, sell={sell})"
        )

    row["buy"] = buy
    row["sell"] = sell
    return row


def fetch_cambix():
    resp = requests.get(
        "https://apibcprod01.azure-api.net/cambix/v2/exchange-rates/exchange-rate",
        params={"typeCode": "TC", "documentNumber": "null"},
        headers={
            **HEADERS_UA,
            "Ocp-Apim-Subscription-Key": CAMBIX_SUBSCRIPTION_KEY,
            "Origin": "https://cambix.com.pe",
            "Accept": "application/json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    # Schema real confirmado:
    # {"id": <int>, "sale": <venta>, "purchase": <compra>, ...}
    if "purchase" not in data or "sale" not in data:
        print(
            "⚠️ Cambix: cambió el schema esperado. "
            f"Respuesta cruda: {json.dumps(data)}"
        )
        raise KeyError(
            "purchase/sale no encontrados en respuesta de Cambix"
        )

    return {
        "provider": "CAMBIX",
        "provider_type": "casa_digital",
        "buy": float(data["purchase"]),
        "sell": float(data["sale"]),
    }


def fetch_rextie():
    query = """
    query GetFxRates($sources: [FXRateSource!]!) {
      currentFxRates(sources: $sources) { source ask bid }
    }
    """

    resp = requests.post(
        "https://app.rextie.com/api/graphql/",
        headers={
            **HEADERS_UA,
            "Content-Type": "application/json",
            "rextie-country": "pe",
            "rextie-language": "es",
            "rextie-app-platform": "rextie-web",
            "rextie-app-version": "6.6.9",
        },
        json={
            "query": query,
            "variables": {
                "sources": ["SUNAT", "REXTIE", "AVG_BANKS"]
            },
        },
        timeout=15,
    )
    resp.raise_for_status()

    payload = resp.json()
    rates = payload["data"]["currentFxRates"]
    by_source = {row["source"]: row for row in rates}

    results = []

    if "REXTIE" in by_source:
        row = by_source["REXTIE"]
        results.append(
            {
                "provider": "REXTIE",
                "provider_type": "casa_digital",
                "buy": float(row["bid"]),
                "sell": float(row["ask"]),
            }
        )

    if "SUNAT" in by_source:
        row = by_source["SUNAT"]
        if float(row["bid"]) > 0 and float(row["ask"]) > 0:
            results.append(
                {
                    "provider": "SUNAT",
                    "provider_type": "benchmark",
                    "buy": float(row["bid"]),
                    "sell": float(row["ask"]),
                }
            )

    # Se conserva la captura, pero validate_rate() descartará AVG_BANKS
    # si vuelve a venir con spread negativo.
    if "AVG_BANKS" in by_source:
        row = by_source["AVG_BANKS"]
        if float(row["bid"]) > 0 and float(row["ask"]) > 0:
            results.append(
                {
                    "provider": "AVG_BANKS",
                    "provider_type": "benchmark",
                    "buy": float(row["bid"]),
                    "sell": float(row["ask"]),
                }
            )

    return results


def fetch_tkambio():
    resp = requests.post(
        "https://tkambio.com/wp-admin/admin-ajax.php",
        data={"action": "get_exchange_rate"},
        headers=HEADERS_UA,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    results = [
        {
            "provider": "TKAMBIO",
            "provider_type": "casa_digital",
            "buy": float(data["buying_rate"]),
            "sell": float(data["selling_rate"]),
        }
    ]

    # IBK es un proxy de segunda mano reportado por TKambio.
    if "ibk_buying_rate" in data and "ibk_selling_rate" in data:
        ibk_buy = float(data["ibk_buying_rate"])
        ibk_sell = float(data["ibk_selling_rate"])

        if ibk_buy > 0 and ibk_sell > 0:
            results.append(
                {
                    "provider": "IBK",
                    "provider_type": "banco",
                    "buy": ibk_buy,
                    "sell": ibk_sell,
                }
            )

    return results


def fetch_kambista():
    resp = requests.get(
        "https://api.kambista.com/v1/exchange/calculates",
        params={
            "originCurrency": "USD",
            "destinationCurrency": "PEN",
            "amount": 1000,
            "active": "S",
        },
        headers=HEADERS_UA,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    return {
        "provider": "KAMBISTA",
        "provider_type": "casa_digital",
        "buy": float(data["tc"]["bid"]),
        "sell": float(data["tc"]["ask"]),
    }


def _decode_next_chunk(chunk):
    """
    Decodifica el string de un self.__next_f.push([1,"..."]).

    Intentamos primero JSON porque respeta correctamente escapes de strings
    JavaScript/JSON. `unicode_escape` queda como fallback por compatibilidad.
    """
    try:
        return json.loads(f'"{chunk}"')
    except Exception:
        try:
            return chunk.encode("utf-8").decode("unicode_escape")
        except Exception:
            return chunk


def _extract_tucambista_from_rsc(html):
    """
    Estrategia primaria.

    Reconstruye el stream RSC de Next.js concatenando TODOS los chunks en
    orden. Esto evita el fallo observado el 11-Sep-2026, cuando Next.js partió
    el array `competition` entre dos self.__next_f.push().
    """
    chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
        html,
        re.S,
    )

    if not chunks:
        return None

    # Crucial: concatenar primero el stream lógico.
    stream = "".join(_decode_next_chunk(chunk) for chunk in chunks)

    # En vez de parsear todo `competition`, buscamos solo el objeto
    # de TuCambista. Las entradas actuales no contienen objetos anidados.
    match = re.search(
        r'\{[^{}]*"entity"\s*:\s*"tucambista"[^{}]*\}',
        stream,
        re.S | re.I,
    )

    if match:
        try:
            entry = json.loads(match.group(0))
            return (
                float(entry["buyExchangeRate"]),
                float(entry["sellExchangeRate"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass

    # Fallback RSC más tolerante si la entrada ya no es JSON puro pero
    # conserva las claves.
    match = re.search(
        r'"entity"\s*:\s*"tucambista"'
        r'.{0,1500}?'
        r'"buyExchangeRate"\s*:\s*([0-9]+(?:\.[0-9]+)?)'
        r'.{0,1000}?'
        r'"sellExchangeRate"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        stream,
        re.S | re.I,
    )

    if match:
        return float(match.group(1)), float(match.group(2))

    return None


def _extract_tucambista_from_widget(html):
    """
    Estrategia secundaria.

    TuCambista actualmente renderiza Compra/Venta en el HTML SSR del widget
    principal. Si vuelve a cambiar la serialización RSC, usamos esos valores
    visibles sin necesitar navegador, Selenium ni Playwright.
    """
    buy_match = re.search(
        r'Compra:\s*</span>'
        r'.{0,3000}?'
        r'tc-quote-rate-value[^>]*>'
        r'.{0,500}?'
        r'<span[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*</span>',
        html,
        re.S | re.I,
    )

    sell_match = re.search(
        r'Venta:\s*</span>'
        r'.{0,3000}?'
        r'tc-quote-rate-value[^>]*>'
        r'.{0,500}?'
        r'<span[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*</span>',
        html,
        re.S | re.I,
    )

    if buy_match and sell_match:
        return float(buy_match.group(1)), float(sell_match.group(1))

    return None


def fetch_tucambista():
    resp = requests.get(
        "https://tucambista.pe/",
        headers=TUCAMBISTA_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()

    html = resp.text

    # ------------------------------------------------------------
    # Estrategia 1: stream RSC reconstruido
    # ------------------------------------------------------------
    rates = _extract_tucambista_from_rsc(html)

    if rates:
        buy, sell = rates

        row = validate_rate(
            {
                "provider": "TUCAMBISTA",
                "provider_type": "casa_digital",
                "buy": buy,
                "sell": sell,
            }
        )

        print(
            "ℹ️ Tucambista: capturado vía RSC reconstruido "
            f"(compra={row['buy']}, venta={row['sell']})"
        )
        return row

    # ------------------------------------------------------------
    # Estrategia 2: widget SSR visible
    # ------------------------------------------------------------
    rates = _extract_tucambista_from_widget(html)

    if rates:
        buy, sell = rates

        row = validate_rate(
            {
                "provider": "TUCAMBISTA",
                "provider_type": "casa_digital",
                "buy": buy,
                "sell": sell,
            }
        )

        print(
            "ℹ️ Tucambista: capturado vía widget HTML "
            f"(compra={row['buy']}, venta={row['sell']})"
        )
        return row

    # Diagnóstico pequeño y seguro: NO imprimimos todo el HTML.
    chunks_count = len(
        re.findall(
            r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
            html,
            re.S,
        )
    )

    print(
        "⚠️ Tucambista diagnóstico: "
        f"status={resp.status_code}, "
        f"content_type={resp.headers.get('Content-Type', '')!r}, "
        f"bytes_html={len(html)}, "
        f"next_chunks={chunks_count}, "
        f"tiene_competition={'competition' in html}, "
        f"tiene_buyExchangeRate={'buyExchangeRate' in html}, "
        f"tiene_sellExchangeRate={'sellExchangeRate' in html}, "
        f"tiene_quote_widget={'tc-quote-rate-value' in html}"
    )

    raise RuntimeError(
        "TuCambista respondió, pero no se pudo extraer compra/venta "
        "ni del stream RSC ni del widget SSR."
    )


def insert_into_supabase(rows):
    captured_minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")

    for row in rows:
        raw_hash = hashlib.sha256(
            (
                f"{row['provider']}-"
                f"{row['buy']}-"
                f"{row['sell']}-"
                f"{captured_minute}"
            ).encode()
        ).hexdigest()

        payload = {
            **row,
            "raw_hash": raw_hash,
        }

        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/rates",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=ignore-duplicates",
            },
            json=payload,
            timeout=15,
        )

        if resp.status_code not in (200, 201, 409):
            print(
                f"⚠️ Error insertando {row['provider']}: "
                f"{resp.status_code} {resp.text}"
            )
        else:
            print(
                f"✅ {row['provider']}: "
                f"compra={row['buy']} venta={row['sell']}"
            )


def main():
    rows = []

    fetchers = [
        ("Cambix", fetch_cambix),
        ("Rextie (+ SUNAT + AVG_BANKS)", fetch_rextie),
        ("TKambio (+ IBK)", fetch_tkambio),
        ("Tucambista", fetch_tucambista),
        ("Kambista", fetch_kambista),
    ]

    for name, fn in fetchers:
        try:
            result = fetch_with_retry(fn)
            items = result if isinstance(result, list) else [result]

            for item in items:
                try:
                    rows.append(validate_rate(item))
                except Exception as exc:
                    print(
                        f"⚠️ {name}: dato descartado por QA: {exc}"
                    )

        except Exception as exc:
            print(f"❌ Falló la captura de {name}: {exc}")

    if rows:
        print(
            f"ℹ️ Ciclo listo: {len(rows)} fila(s) válidas "
            "para insertar en Supabase."
        )
        insert_into_supabase(rows)
    else:
        print("❌ No se capturó ningún dato válido en este ciclo.")


if __name__ == "__main__":
    main()
