# folio_login_module.py
# -*- coding: utf-8 -*-
"""
folio_login_module.py (production-ready)

Qué hace este módulo
--------------------
Este módulo encapsula la autenticación contra FOLIO usando el endpoint:
    POST /authn/login-with-expiry

En algunos tenants, NO se retorna x-okapi-token en headers (o no se usa token),
por lo que la autenticación efectiva queda en cookies de sesión.
Por eso usamos requests.Session() para persistir cookies automáticamente.

Objetivo
--------
Que tus scripts externos NO se preocupen de:
- leer okapi_customers.json
- hacer login
- refrescar cookies cuando expira la sesión (401)
- repetir headers base x-okapi-tenant / content-type
- reintentos simples ante fallos 5xx

Uso en scripts externos (lo mínimo)
-----------------------------------
    from folio_login_module import folio_login_module

    client = folio_login_module("usb", customers_json_path="okapi_customers.json")
    r = client.get("/inventory/instances?limit=1")
    print(r.status_code, r.text[:200])

Requisitos
----------
pip install requests

Estructura esperada de okapi_customers.json
-------------------------------------------
{
  "okapi": [
    {
      "libraryName": "usb",
      "x_okapi_url": "https://api-btk.folio.ebsco.com",
      "x_okapi_tenant": "fs00000000",
      "userName": "usuarname",
      "password": "xxxxx",
      "content_type": "application/json",
    }
  ]
}

Notas
-----
- endpoint siempre debe empezar con "/" (ej: "/inventory/instances").
- este módulo NO imprime password ni lo loguea.
- retry/backoff: simple (2^attempt segundos) para 5xx.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests


class folio_login_module:
    """
    Cliente FOLIO con autenticación basada en cookies (Session) y auto-refresh.

    Flujo interno:
    1) __init__() recibe library_name y path a okapi_customers.json
    2) _load_customer() busca en el JSON el bloque correspondiente a library_name
    3) _login() ejecuta POST /authn/login-with-expiry y guarda cookies en Session
    4) _request() envía requests con headers base y:
       - si status_code == 401 => refresca (_refresh) y reintenta
       - si status_code >= 500 => reintenta con backoff (si quedan retries)
       - si OK o error no-retriable => retorna response

    Ventajas:
    - scripts externos llaman get/post/put/delete y listo
    - autenticación centralizada
    - menos duplicación y menos errores
    """

    def __init__(
        self,
        library_name: str,
        customers_json_path: str = "okapi_customers.json",
        timeout: int = 30,
        max_retries: int = 2,
        user_agent: str = "folio-login-module/1.0",
    ) -> None:
        """
        Inicializa el cliente y realiza login inmediatamente.

        Args:
            library_name: nombre del bloque "libraryName" dentro del okapi_customers.json.
                          Ej: "usb"
            customers_json_path: ruta al archivo JSON con credenciales.
            timeout: timeout (segundos) para requests HTTP.
            max_retries: cantidad de reintentos ante 5xx (y reintento por 401).
                         Nota: para 401, se refresca y se reintenta dentro del mismo loop.
            user_agent: User-Agent para requests (útil para trazabilidad en logs del servidor).

        Raises:
            FileNotFoundError: si no existe okapi_customers.json
            ValueError: si no existe library_name en el JSON
            RuntimeError: si falla el login
        """
        self.library_name = (library_name or "").strip()
        if not self.library_name:
            raise ValueError("library_name está vacío.")

        self.timeout = int(timeout)
        self.max_retries = int(max_retries)
        self.customers_json_path = Path(customers_json_path)

        # Session mantiene cookies y reusa conexiones TCP (más eficiente)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

        # Carga config + login
        self.customer = self._load_customer()
        self._login()

    # -------------------- CONFIG --------------------

    def _load_customer(self) -> Dict[str, Any]:
        """
        Lee okapi_customers.json y devuelve el diccionario del customer correspondiente.

        Returns:
            Dict con al menos:
                x_okapi_url, x_okapi_tenant, user, password

        Raises:
            FileNotFoundError: si no existe el archivo
            ValueError: si la estructura es inválida o no se encuentra el customer
        """
        if not self.customers_json_path.exists():
            raise FileNotFoundError(f"No existe: {self.customers_json_path}")

        with self.customers_json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        okapi_list = data.get("okapi")
        if not isinstance(okapi_list, list):
            raise ValueError("Estructura inválida: se esperaba {'okapi': [ ... ]}")

        for item in okapi_list:
            if not isinstance(item, dict):
                continue

            if str(item.get("libraryName", "")).strip().lower() == self.library_name.lower():
                # Validación mínima
                required = ("x_okapi_url", "x_okapi_tenant", "userName", "password")
                missing = [k for k in required if not item.get(k)]
                if missing:
                    raise ValueError(f"Customer '{self.library_name}' sin campos: {missing}")

                # normaliza URL (sin slash final)
                item["x_okapi_url"] = str(item["x_okapi_url"]).rstrip("/")
                # content_type opcional
                item.setdefault("content_type", "application/json")
                return item

        available = [str(x.get("name", "")).strip() for x in okapi_list if isinstance(x, dict)]
        raise ValueError(f"No se encontró library_name='{self.library_name}'. Disponibles: {available}")

    # -------------------- AUTH --------------------

    def _login(self) -> None:
        """
        Ejecuta login contra FOLIO usando cookies.

        Endpoint:
            POST {OKAPI_URL}/authn/login-with-expiry

        Efecto:
            - Si es exitoso, la Session() guarda cookies automáticamente.
            - Si falla, levanta RuntimeError.

        Raises:
            RuntimeError: si status != 200/201
        """
        okapi_url = self.customer["x_okapi_url"]
        tenant = self.customer["x_okapi_tenant"]
        user = self.customer["userName"]
        password = self.customer["password"]
        content_type = self.customer.get("content_type") or "application/json"

        url = f"{okapi_url}/authn/login-with-expiry"
        headers = {
            "x-okapi-tenant": tenant,
            "Content-Type": content_type,
            "Accept": "application/json",
        }
        payload = {"username": user, "password": password}

        resp = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
        print(resp.text)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Login failed: {resp.status_code} - {resp.text[:300]}")

        # NO logueamos cookies completas ni credenciales.
        logging.info("✅ Login OK (cookies cargadas en Session)")

    def _refresh(self) -> None:
        """
        Refresca sesión: en la práctica hace re-login.

        Se llama cuando _request() recibe 401 (sesión expirada/no autorizada).
        """
        logging.warning("🔄 Refreshing FOLIO session (re-login)...")
        self._login()

    # -------------------- HEADERS BASE --------------------

    def _base_headers(self) -> Dict[str, str]:
        """
        Headers base para cualquier request a FOLIO.

        Nota:
        - NO se incluye x-okapi-token porque estás usando cookies.
        - Si tu tenant en el futuro retorna token y lo quieres usar,
          aquí es donde lo agregarías.

        Returns:
            dict de headers base
        """
        return {
            "x-okapi-tenant": self.customer["x_okapi_tenant"],
            "Accept": "application/json",
            "Content-Type": self.customer.get("content_type") or "application/json",
        }

    # -------------------- CORE REQUEST --------------------

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """
        Método central para todas las requests.

        Implementa:
        - Construcción de URL: OKAPI_URL + endpoint
        - Inyección de headers base (tenant, content-type)
        - Auto-refresh cuando status_code == 401
        - Retry simple para status_code >= 500 con backoff exponencial

        Args:
            method: "GET", "POST", "PUT", "DELETE", ...
            endpoint: string que empieza con "/"
            **kwargs: se pasan a requests.Session.request():
                - params=...
                - json=...
                - data=...
                - headers=...  (se combinan con base_headers)
                - etc.

        Returns:
            requests.Response

        Raises:
            ValueError: endpoint inválido
            RuntimeError: si se agotan reintentos
        """
        if not endpoint.startswith("/"):
            raise ValueError("endpoint debe empezar con '/'. Ej: '/inventory/instances'")

        url = f"{self.customer['x_okapi_url']}{endpoint}"

        # Mezcla headers externos + base headers (base tiene prioridad para tenant)
        external_headers = kwargs.pop("headers", {}) or {}
        headers = {**external_headers, **self._base_headers()}

        # Loop de reintentos
        for attempt in range(self.max_retries + 1):
            resp = self.session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )

            # 1) Sesión expirada / no autorizada: refresca y reintenta inmediato
            if resp.status_code == 401:
                logging.warning("🔐 401 Unauthorized. Sesión expirada o inválida. Re-login y retry...")
                self._refresh()
                continue

            # 2) Errores de servidor: reintento con backoff si quedan intentos
            if resp.status_code >= 500 and attempt < self.max_retries:
                wait = 2 ** attempt
                logging.warning(f"⚠️ {resp.status_code} Server error. Retry en {wait}s (attempt {attempt+1})...")
                time.sleep(wait)
                continue

            # 3) OK o error no reintetable: devolvemos response tal cual
            return resp

        raise RuntimeError("Request failed after retries (401/5xx).")

    # -------------------- PUBLIC METHODS --------------------

    def get(self, endpoint: str, **kwargs) -> requests.Response:
        """GET wrapper."""
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs) -> requests.Response:
        """POST wrapper."""
        return self._request("POST", endpoint, **kwargs)

    def put(self, endpoint: str, **kwargs) -> requests.Response:
        """PUT wrapper."""
        return self._request("PUT", endpoint, **kwargs)

    def delete(self, endpoint: str, **kwargs) -> requests.Response:
        """DELETE wrapper."""
        return self._request("DELETE", endpoint, **kwargs)


# -------------------- EJEMPLO DE USO DIRECTO --------------------
'''if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    client = folio_login_module("namelibrary", customers_json_path="okapi_customers.json")

    # Ejemplo: health check proxy (puede variar según hosting)
    r = client.get("/_/proxy/health")
    print("status:", r.status_code)
    print("body:", r.text[:200])'''