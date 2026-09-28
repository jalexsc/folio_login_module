# folio_export_to_csv.py
# -*- coding: utf-8 -*-
"""
Exporta datos desde FOLIO a un archivo CSV.

Reutiliza folio_login_module.py para la conexión/autenticación (cookies + retry
automático en 401) y setting_data.json para resolver el pathPattern a partir
de un --endpointName, siguiendo la misma convención que
folio_export_endpoint_pro.py.

Uso:
    python folio_export_to_csv.py --libraryName usb --endpointName instances

Los registros de FOLIO suelen tener campos anidados (objetos/arreglos), por lo
que se aplanan a columnas "padre.hijo" antes de escribir el CSV; los arreglos
se serializan como JSON dentro de la celda para no perder información.
"""

import argparse
import csv
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from folio_login_module import folio_login_module

SETTINGS_FILE = "setting_data.json"
DEFAULT_LIMIT = 1000
DEFAULT_RPS = 5.0
RETRY_STATUSES = {429, 502, 503, 504, 505}


# -----------------------------
# Throttler (RPS)
# -----------------------------
class Throttler:
    def __init__(self, rps: float):
        self.min_interval = 1.0 / rps if rps and rps > 0 else 0.0
        self.last_call = 0.0

    def wait(self) -> None:
        if not self.min_interval:
            return
        elapsed = time.time() - self.last_call
        sleep_time = self.min_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
        self.last_call = time.time()


# -----------------------------
# Settings: resolver endpointName -> pathPattern
# -----------------------------
def load_endpoint(endpoint_name: str) -> Dict[str, Any]:
    settings_path = Path(SETTINGS_FILE)
    if not settings_path.exists():
        raise FileNotFoundError(f"No existe {SETTINGS_FILE} en {Path.cwd()}")

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    settings = data.get("settings")
    if not isinstance(settings, list):
        raise ValueError("Estructura inválida: se esperaba {'settings': [...]}")

    for s in settings:
        if isinstance(s, dict) and str(s.get("name", "")).strip() == endpoint_name:
            path = str(s.get("pathPattern", "")).strip()
            if not path:
                raise ValueError(f"Endpoint '{endpoint_name}' no tiene pathPattern")
            if not path.startswith("/"):
                path = "/" + path
            return {"name": endpoint_name, "pathPattern": path}

    names = [x.get("name") for x in settings if isinstance(x, dict) and x.get("name")]
    raise ValueError(f"Endpoint '{endpoint_name}' no encontrado. Ejemplos: {', '.join(names[:25])}")


def detect_list_payload(js: Dict[str, Any]) -> Tuple[Optional[str], Optional[List[Any]]]:
    if not isinstance(js, dict):
        return None, None
    for k, v in js.items():
        if isinstance(v, list):
            return k, v
    return None, None


# -----------------------------
# Request con retry/backoff
# -----------------------------
def request_with_retry(
    client: folio_login_module,
    throttler: Throttler,
    path: str,
    params: Dict[str, Any],
    max_retries: int = 8,
    base_delay: float = 1.0,
):
    attempt = 0
    delay = base_delay

    while True:
        throttler.wait()
        resp = client.get(path, params=params)

        if resp.status_code not in RETRY_STATUSES:
            if resp.status_code >= 400:
                raise RuntimeError(f"GET {path} failed: {resp.status_code} - {resp.text[:300]}")
            return resp

        attempt += 1
        if attempt > max_retries:
            raise RuntimeError(f"GET {path} retries agotados ({max_retries}). Último: {resp.status_code}")

        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass

        logging.warning(f"{resp.status_code} en {path} (attempt {attempt}/{max_retries}) -> sleep {delay:.1f}s")
        time.sleep(delay)
        delay = min(delay * 2, 60.0)


# -----------------------------
# Paginación estándar FOLIO (limit/offset/totalRecords)
# -----------------------------
def get_all_records(
    client: folio_login_module,
    path: str,
    limit: int,
    throttler: Throttler,
) -> List[Dict[str, Any]]:
    offset = 0
    all_rows: List[Dict[str, Any]] = []

    r = request_with_retry(client, throttler, path, {"limit": limit, "offset": offset})
    js = r.json()
    total = js.get("totalRecords") if isinstance(js, dict) else None
    _, rows = detect_list_payload(js)

    if not isinstance(rows, list):
        return [js] if isinstance(js, dict) else [{"value": js}]

    all_rows.extend(x if isinstance(x, dict) else {"value": x} for x in rows)
    logging.info(f"offset={offset} acumulado={len(all_rows)}/{total if total is not None else '?'}")

    while True:
        if isinstance(total, int) and len(all_rows) >= total:
            break

        offset += limit
        r = request_with_retry(client, throttler, path, {"limit": limit, "offset": offset})
        js = r.json()
        _, rows2 = detect_list_payload(js)
        if not isinstance(rows2, list) or not rows2:
            break

        all_rows.extend(x if isinstance(x, dict) else {"value": x} for x in rows2)
        logging.info(f"offset={offset} acumulado={len(all_rows)}/{total if total is not None else '?'}")

    return all_rows


# -----------------------------
# Aplanado de registros (dict anidado -> columnas "padre.hijo")
# -----------------------------
def flatten_value(value: Any, full_key: str, flat: Dict[str, Any], sep: str, array_sep: str) -> None:
    if isinstance(value, dict):
        flat.update(flatten_record(value, full_key, sep, array_sep))
    elif isinstance(value, list):
        if not value:
            return  # array vacío -> celda en blanco, no "[]"
        if all(not isinstance(v, (dict, list)) for v in value):
            # array de valores simples (ej. tags.tagList) -> una sola celda unida
            flat[full_key] = array_sep.join(str(v) for v in value)
        else:
            # array de objetos/listas (ej. personal.addresses) -> columnas indexadas
            for i, item in enumerate(value):
                flatten_value(item, f"{full_key}[{i}]", flat, sep, array_sep)
    else:
        flat[full_key] = value


def flatten_record(record: Dict[str, Any], parent_key: str = "", sep: str = ".", array_sep: str = "; ") -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for key, value in record.items():
        full_key = f"{parent_key}{sep}{key}" if parent_key else str(key)
        flatten_value(value, full_key, flat, sep, array_sep)
    return flat


def write_csv(export_dir: Path, endpoint_name: str, rows: List[Dict[str, Any]], delimiter: str, array_sep: str) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    out_csv = export_dir / f"{endpoint_name}.csv"

    flat_rows = [flatten_record(r, array_sep=array_sep) for r in rows]

    # union de columnas preservando el orden de primera aparición
    fieldnames: List[str] = []
    seen = set()
    for row in flat_rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=delimiter, extrasaction="ignore")
        writer.writeheader()
        for row in flat_rows:
            writer.writerow(row)

    return out_csv


# -----------------------------
# MAIN
# -----------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Exporta un endpoint de FOLIO (setting_data.json) a CSV")
    parser.add_argument("--libraryName", required=True, help="Bloque libraryName en okapi_customers.json")
    parser.add_argument("--endpointName", required=True, help="Nombre del endpoint en setting_data.json")
    parser.add_argument("--customers", default="okapi_customers.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--rps", type=float, default=DEFAULT_RPS, help="Requests per second (0 = sin límite)")
    parser.add_argument("--delimiter", default=",", help="Delimitador del CSV (default ',')")
    parser.add_argument(
        "--array-sep",
        default="; ",
        help="Separador usado para unir arrays de valores simples (ej. tags.tagList) en una sola celda (default '; ')",
    )
    parser.add_argument("--loglevel", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.loglevel).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    library = args.libraryName.strip()
    endpoint_name = args.endpointName.strip()

    endpoint = load_endpoint(endpoint_name)
    path = endpoint["pathPattern"]
    logging.info(f"Endpoint: {endpoint_name} -> {path}")

    # Conexión/autenticación centralizada vía folio_login_module
    client = folio_login_module(library, customers_json_path=args.customers)
    throttler = Throttler(args.rps)

    t0 = time.time()
    rows = get_all_records(client, path, args.limit, throttler)
    duration = time.time() - t0

    export_dir = Path(library) / "export"
    out_csv = write_csv(export_dir, endpoint_name, rows, args.delimiter, args.array_sep)

    logging.info(f"✅ Export CSV: {out_csv} ({len(rows)} registros, {duration:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
