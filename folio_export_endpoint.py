# folio_export_endpoint.py
# -*- coding: utf-8 -*-

import argparse
import json
import logging
from pathlib import Path
from typing import Dict, Any, List

from folio_login_module import folio_login_module


SETTINGS_FILE = "setting_data.json"
DEFAULT_LIMIT = 1000


# ------------------------------------------------------------
# Cargar endpoints desde setting_data.json
# Estructura real: {"settings": [ {...}, {...} ]} :contentReference[oaicite:1]{index=1}
# ------------------------------------------------------------
def load_endpoint(endpoint_name: str) -> Dict[str, Any]:

    settings_path = Path(SETTINGS_FILE)

    if not settings_path.exists():
        raise FileNotFoundError(f"No existe {SETTINGS_FILE}")

    data = json.loads(settings_path.read_text(encoding="utf-8"))

    settings = data.get("settings")
    if not isinstance(settings, list):
        raise ValueError("Estructura inválida en setting_data.json")

    for s in settings:
        if s.get("name") == endpoint_name:
            path = s.get("pathPattern")

            if not path:
                raise ValueError(f"Endpoint {endpoint_name} no tiene pathPattern")

            if not path.startswith("/"):
                path = "/" + path

            return {
                "name": endpoint_name,
                "pathPattern": path
            }

    raise ValueError(f"Endpoint '{endpoint_name}' no encontrado en setting_data.json")


# ------------------------------------------------------------
# GET con paginación estándar FOLIO
# ------------------------------------------------------------
def get_all_records(client, path: str) -> List[Dict]:

    offset = 0
    results = []

    while True:

        params = {
            "limit": DEFAULT_LIMIT,
            "offset": offset
        }

        response = client.get(path, params=params)

        if response.status_code >= 400:
            raise RuntimeError(f"GET {path} failed: {response.status_code} - {response.text}")

        data = response.json()

        # Detectar array principal
        array_key = None
        for k, v in data.items():
            if isinstance(v, list):
                array_key = k
                break

        if not array_key:
            # respuesta tipo dict simple
            return [data]

        page = data[array_key]
        results.extend(page)

        total = data.get("totalRecords")

        if not total or len(results) >= total:
            break

        offset += DEFAULT_LIMIT

        logging.info(f"offset={offset} acumulado={len(results)}/{total}")

    return results


# ------------------------------------------------------------
# Export
# ------------------------------------------------------------
def export_results(library_name: str, endpoint_name: str, records: List[Dict]):

    export_dir = Path(library_name) / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    output_file = export_dir / f"{endpoint_name}.json"

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    logging.info(f"Exportado: {output_file} ({len(records)} registros)")


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main():

    parser = argparse.ArgumentParser(description="Export FOLIO endpoint por endpointName")
    parser.add_argument("--libraryName", required=True)
    parser.add_argument("--endpointName", required=True)
    parser.add_argument("--customers", default="okapi_customers.json")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    # 1️⃣ buscar endpoint en setting_data.json
    endpoint = load_endpoint(args.endpointName)

    print(f"Endpoint encontrado: {endpoint['pathPattern']}")

    # 2️⃣ autenticación usando folio_login_module
    client = folio_login_module(
        args.libraryName,
        customers_json_path=args.customers
    )

    # 3️⃣ GET
    records = get_all_records(client, endpoint["pathPattern"])

    # 4️⃣ export
    export_results(args.libraryName, args.endpointName, records)

    print("Proceso finalizado.")


if __name__ == "__main__":
    main()