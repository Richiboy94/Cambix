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
import hashlib
from datetime import datetime, timezone

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CAMBIX_SUBSCRIPTION_KEY = os.environ.get("CAMBIX_SUBSCRIPTION_KEY", "d8fe90e920e944838711021952a3d2d5")

HEADERS_UA = {"User-Agent": "Mozilla/5.0 (compatible; PulsoCambiarioBot/1.0)"}


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
    if "SUNAT" in by_source:
        r = by_source["SUNAT"]
        results.append({
            "provider": "SUNAT",
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
    return {
        "provider": "TKAMBIO",
        "provider_type": "casa_digital",
        "buy": float(data["buying_rate"]),
        "sell": float(data["selling_rate"]),
    }


def fetch_tucambista():
    resp = requests.get("https://tucambista.pe/", headers=HEADERS_UA, timeout=15)
    resp.raise_for_status()
    html = resp.text

    match = re.search(r'self\.__next_f\.push\(\[1,"(3:.*?)"\]\)', html, re.S)
    if not match:
        raise RuntimeError("No se encontró el bloque de datos de competencia en Tucambista")

    unescaped = match.group(1).encode().decode("unicode_escape")
    competition_match = re.search(r'"competition":(\[.*\])\}\]\]$', unescaped)
    if not competition_match:
        raise RuntimeError("No se pudo extraer 'competition' del bloque de Tucambista")

    competition = json.loads(competition_match.group(1))

    # Filtrar: solo Tucambista (Kambista y Rextie ya se capturan de su propia fuente;
    # el resto de la lista son apps argentinas mezcladas, ver documentación).
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
        ("Rextie (+ SUNAT)", fetch_rextie),
        ("TKambio", fetch_tkambio),
        ("Tucambista", fetch_tucambista),
    ]

    for name, fn in fetchers:
        try:
            result = fn()
            rows.extend(result if isinstance(result, list) else [result])
        except Exception as e:
            print(f"❌ Falló la captura de {name}: {e}")

    if rows:
        insert_into_supabase(rows)
    else:
        print("No se capturó ningún dato en este ciclo.")


if __name__ == "__main__":
    main()
