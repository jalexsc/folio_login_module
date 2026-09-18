# folio_login_module

A lightweight Python client for authenticating against a FOLIO library services platform (LSP) via the POST /authn/login-with-expiry endpoint, with automatic session-cookie handling, 401 re-login, and simple retry/backoff for server errors.

Why this module exists
Some FOLIO tenants don't return an x-okapi-token header on login — the effective authentication lives entirely in session cookies. folio_login_module wraps a requests.Session() so your scripts don't have to worry about:

Reading credentials from a JSON config file
Performing the login request
Refreshing the session automatically when it expires (401)
Repeating base headers (x-okapi-tenant, Content-Type) on every call
Retrying transient server errors (5xx) with exponential backoff
Requirements
pip install requests
Python 3.9+ (uses from __future__ import annotations and standard typing).

Configuration file
Credentials are read from a JSON file (default name: okapi_customers.json), structured as a list of library/tenant blocks:

{
  "okapi": [
    {
      "libraryName": "yourlibraryName",
      "x_okapi_url": "https://api-btk.folio.ebsco.com",
      "x_okapi_tenant": "fs00000000",
      "userName": "username",
      "password": "xxxxx",
      "content_type": "application/json"
    }
  ]
}
Field	Required	Description
libraryName	Yes	Identifier used to select this block (case-insensitive).
x_okapi_url	Yes	Base Okapi/FOLIO URL (trailing slash is stripped automatically).
x_okapi_tenant	Yes	Tenant ID sent as the x-okapi-tenant header.
userName	Yes	FOLIO username.
password	Yes	FOLIO password.
content_type	No	Defaults to application/json.
Never commit real credentials. Keep okapi_customers.json out of version control (e.g. via .gitignore) and use a template/example file instead.

Usage
from folio_login_module import folio_login_module

client = folio_login_module("yourlibraryName", customers_json_path="okapi_customers.json")

r = client.get("/inventory/instances?limit=1")
print(r.status_code, r.text[:200])
Constructor parameters:

Parameter	Default	Description
library_name	—	Matches libraryName in the JSON config.
customers_json_path	"okapi_customers.json"	Path to the credentials file.
timeout	30	Per-request timeout, in seconds.
max_retries	2	Retries on 5xx (also bounds the 401 re-login retry loop).
user_agent	"folio-login-module/1.0"	User-Agent header sent with every request.
Login happens immediately in __init__. If it fails, a RuntimeError is raised.

Available HTTP methods
client.get("/inventory/instances")
client.post("/inventory/instances", json={...})
client.put("/inventory/instances/<id>", json={...})
client.delete("/inventory/instances/<id>")
All of them accept the same keyword arguments as requests.Session.request (params, json, data, headers, etc.). Endpoints must start with /.

How it works
_load_customer() reads the JSON file and finds the block matching library_name.
_login() sends POST {x_okapi_url}/authn/login-with-expiry with the tenant header and credentials; on success, cookies are stored in the requests.Session.
_request() is the core method used by get/post/put/delete. For every call it:
Merges base headers (x-okapi-tenant, Content-Type, Accept) with any headers you pass in.
Retries automatically on 401 by calling _refresh() (which re-runs _login()).
Retries on 5xx with exponential backoff (2^attempt seconds) until max_retries is exhausted.
Returns the requests.Response as-is for success or non-retriable errors.
Error handling
Exception	Raised when
ValueError	Empty library_name, missing required fields in the config, or an endpoint not starting with /.
FileNotFoundError	customers_json_path does not exist.
RuntimeError	Login fails, or a request keeps failing after all retries.
Security notes
Passwords are never logged or printed.
The module logs high-level events only (e.g. login success, refresh attempts, retry warnings) via the standard logging module — configure a handler in your own script if you want to see them:
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
License
Internal migration tooling — add a license here if this repository will be made public.

