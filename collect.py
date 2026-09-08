#!/usr/bin/env python3
"""
Collector de tipo de cambio USD/PEN — Piloto Benchmark Cambix
Fuentes: Cambix (API propia), Rextie (GraphQL, incluye SUNAT), TKambio (WP admin-ajax), Tucambista (JSON embebido)
Destino: tabla `rates` en Supabase (vía REST API)

Variables de entorno requeridas:
  SUPABASE_URL, SUPABASE_KEY, CAMBIX_SUBSCRIPTION_KEY (opcional, tiene default)
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
CAMBIX_SUBSCRIPTION_KEY = os.environ.get("CAMBIX_SUBSCRIPTION_KEY", "d8fe90e920e944838711021952a3d2d5")

HEADERS_UA = {"User-Agent": "Mozilla/5.0 (compatible; PulsoCambiarioBot/1.0)"}


def fetch_with_retry(fn, retries=2, delay=5):
    """Reintenta una función de captura ante errores transitorios (ej. 503 puntual)."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(delay)
    raise last_exc


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

    # Schema real confirmado en la primera ejecución del piloto:
    # {"id": <int>, "sale": <venta>, "purchase": <compra>, "decimalsQuantity": 3}
    if "purchase" not in data or "sale" not in data:
        print(f"⚠️  Cambix: cambió el schema esperado. Respuesta cruda: {json.dumps(data)}")
        raise KeyError("purchase/sale no encontrados — ver respuesta cruda impresa arriba")

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
        json={"query": query, "variables": {"sources": ["SUNAT", "REXTIE", "AVG_BANKS"]}},
        timeout=15,
    )
    resp.raise_for_status()
    rates = resp.json()["data"]["currentFxRates"]
    by_source = {r["source"]: r for r in rates}

    results = []
    if "REXTIE" in by_source:
        r = by_source["REXTIE"]
        results.append({
            "provider": "REXTIE",
            "provider_type": "casa_digital",
            "buy": float(r["bid"]),
            "sell": float(r["ask"]),
        })
    # SUNAT y AVG_BANKS son campos "bono" de la misma respuesta de Rextie —
    # se descartan si vienen en 0 para no ensuciar el histórico con un dato inválido.
    if "SUNAT" in by_source:
        r = by_source["SUNAT"]
        if float(r["bid"]) > 0 and float(r["ask"]) > 0:
            results.append({
                "provider": "SUNAT",
                "provider_type": "benchmark",
                "buy": float(r["bid"]),
                "sell": float(r["ask"]),
            })
    if "AVG_BANKS" in by_source:
        r = by_source["AVG_BANKS"]
        if float(r["bid"]) > 0 and float(r["ask"]) > 0:
            results.append({
                "provider": "AVG_BANKS",
                "provider_type": "benchmark",
                "buy": float(r["bid"]),
                "sell": float(r["ask"]),
            })
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

    results = [{
        "provider": "TKAMBIO",
        "provider_type": "casa_digital",
        "buy": float(data["buying_rate"]),
        "sell": float(data["selling_rate"]),
    }]

    # IBK no tiene calculadora pública en su web (el flujo real vive dentro
    # de su app móvil, vía deep link interbank://, sin endpoint web que
    # replicar). Se usa el valor que TKambio ya reporta como IBK — es un
    # dato de segunda mano, no la fuente directa del banco.
    if "ibk_buying_rate" in data and "ibk_selling_rate" in data:
        ibk_buy = float(data["ibk_buying_rate"])
        ibk_sell = float(data["ibk_selling_rate"])
        if ibk_buy > 0 and ibk_sell > 0:
            results.append({
                "provider": "IBK",
                "provider_type": "banco",
                "buy": ibk_buy,
                "sell": ibk_sell,
            })

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


def fetch_tucambista():
    resp = requests.get("https://tucambista.pe/", headers=HEADERS_UA, timeout=15)
    resp.raise_for_status()
    html = resp.text

    # No anclamos al número de chunk (ej. "3:") porque Next.js puede
    # reasignarlo entre builds. Revisamos todos los chunks hasta encontrar
    # el que contenga "competition".
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    competition = None
    for chunk in chunks:
        if 'competition' not in chunk:  # sin comillas: el texto sigue escapado en este punto
            continue
        try:
            unescaped = chunk.encode().decode('unicode_escape')
        except Exception:
            continue
        m = re.search(r'"competition":(\[.*?\])\}\]\]?$', unescaped) or \
            re.search(r'"competition":(\[.*?\])', unescaped)
        if not m:
            continue
        try:
            competition = json.loads(m.group(1))
            break
        except json.JSONDecodeError:
            continue

    if competition is None:
        raise RuntimeError("No se encontró el bloque 'competition' en ningún chunk de Tucambista")

    for entry in competition:
        if entry.get("entity") == "tucambista":
            return {
                "provider": "TUCAMBISTA",
                "provider_type": "casa_digital",
                "buy": float(entry["buyExchangeRate"]),
                "sell": float(entry["sellExchangeRate"]),
            }
    raise RuntimeError("No se encontró 'tucambista' en la lista de competencia")


def insert_into_supabase(rows):
    captured_minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    for row in rows:
        raw_hash = hashlib.sha256(
            f"{row['provider']}-{row['buy']}-{row['sell']}-{captured_minute}".encode()
        ).hexdigest()
        payload = {**row, "raw_hash": raw_hash}
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
            print(f"⚠️  Error insertando {row['provider']}: {resp.status_code} {resp.text}")
        else:
            print(f"✅ {row['provider']}: compra={row['buy']} venta={row['sell']}")


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
            rows.extend(result if isinstance(result, list) else [result])
        except Exception as e:
            print(f"❌ Falló la captura de {name}: {e}")

    if rows:
        insert_into_supabase(rows)
    else:
        print("No se capturó ningún dato en este ciclo.")


if __name__ == "__main__":
    main()
