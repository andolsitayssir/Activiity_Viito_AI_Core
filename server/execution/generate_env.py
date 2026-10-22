"""One-time generator: tools-secrets.txt -> .env.

Reads the local secrets file, maps each backend credential to a well-known
env-var name, and writes/merges them into the project root ``.env`` so that
``python-dotenv`` (``server/config.py::load_environment``) can load them at
startup. Runtime code NEVER reads ``tools-secrets.txt`` and never hardcodes a
secret value; it only reads ``os.environ`` via the names declared here.

Usage:
    & $env:PYTHON .\\server\\execution\\generate_env.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRETS_FILE = ROOT / "tools-secrets.txt"
ENV_FILE = ROOT / ".env"

ENV_KEYS: dict[str, str] = {
    "ODOO_DB": "Odoo database name",
    "ODOO_MASTER_PASSWORD": "Odoo database master password",
    "ODOO_LOGIN": "Odoo login email",
    "ODOO_PASSWORD": "Odoo login password",
    "ODOO_ADMIN": "Odoo admin user",
    "ODOO_URL": "Odoo base URL (with port)",
    "STALWART_ADMIN_EMAIL": "Stalwart admin email",
    "STALWART_ADMIN_PASSWORD": "Stalwart admin password",
    "STALWART_SENDER": "Stalwart From address (default sender)",
    "STALWART_URL": "Stalwart base URL (with port)",
    "GMAIL_CLIENT_ID": "Google Cloud OAuth Client ID",
    "GMAIL_CLIENT_SECRET": "Google Cloud OAuth Client Secret",
    "GMAIL_REDIRECT_URI": "OAuth redirect URI (default: http://localhost:8000/api/gmail/callback)",
    "VIITO_ENABLE_GMAIL": "Enable Gmail executor (1/true, default: 1)",
    "VIITO_ENABLE_STALWART": "Enable Stalwart executor as fallback (1/true, default: 0)",
    "VIITO_ENABLE_SEMANTIC_VALIDATION": "LLM semantic judge on execution results (1/true, default: 1; 0 disables for deterministic tests)",
    "NEXTCLOUD_URL": "Nextcloud base URL",
    "ONLYOFFICE_URL": "OnlyOffice Documentserver base URL",
    "ONLYOFFICE_JWT_SECRET": "OnlyOffice JWT secret",
    "VIKUNJA_URL": "Vikunja base URL",
    "VIKUNJA_SERVICE_SECRET": "Vikunja service secret",
    "VIKUNJA_DATABASE_HOST": "Vikunja db host",
    "VIKUNJA_DATABASE_USER": "Vikunja db user",
    "VIKUNJA_DATABASE_PASSWORD": "Vikunja db password",
    "VIKUNJA_DATABASE_DATABASE": "Vikunja db name",
    "CHATWOOT_URL": "Chatwoot base URL",
    "CHATWOOT_ACCESS_TOKEN": "Chatwoot API access token (account-scoped)",
    "CHATWOOT_ACCOUNT_ID": "Chatwoot account id",
    "TELEGRAM_API_ID": "Telegram api_id (my.telegram.org > API development tools)",
    "TELEGRAM_API_HASH": "Telegram api_hash (my.telegram.org > API development tools)",
    "TELEGRAM_PHONE": "Telegram phone number (international format, +336...)",
    "VIITO_ENABLE_TELEGRAM": "Enable Telegram executor for messaging (1/true, default: 1)",
    "RAG_URL": "Activiitykdb RAG base URL",
}

_SECTION_HEADERS = "oddo|stalwart|vikunja|onlyoffice|chatwoot|telegram"


def _section(text: str, header: str) -> str:
    """Return the body lines of a labeled section, up to the next section header."""
    pattern = re.compile(
        rf"^\s*{re.escape(header)}\s*:[^\r\n]*[\r\n]+"
        rf"((?:(?!^\s*(?:{_SECTION_HEADERS})\s*:).)*)",
        re.M | re.I | re.S,
    )
    match = pattern.search(text)
    return match.group(1) if match else ""


def _after(body: str, label: str) -> str | None:
    """Value that follows ``label`` on the same line (or, if empty, next line)."""
    match = re.search(
        rf"^\s*{re.escape(label)}\s*:?\s*(.*)$", body, re.M | re.I
    )
    if not match:
        return None
    value = match.group(1).strip()
    if value:
        return value
    # Label alone on its line: take the next non-empty line.
    rest = body[match.end():]
    nxt = re.search(r"^\s*([^\r\n]+)$", rest, re.M)
    return nxt.group(1).strip() if nxt else None


def extract_secrets(text: str) -> dict[str, str]:
    od = _section(text, "oddo")
    st = _section(text, "stalwart")
    vk = _section(text, "vikunja")
    oo = _section(text, "onlyoffice")

    out: dict[str, str] = {
        "ODOO_DB": _after(od, "data base"),
        "ODOO_MASTER_PASSWORD": _after(od, "master password"),
        "ODOO_LOGIN": _after(od, "login"),
        "ODOO_PASSWORD": _after(od, "password"),
        "ODOO_ADMIN": _after(od, "admin"),
        "ODOO_URL": "http://localhost:8069",
        "STALWART_ADMIN_EMAIL": _after(st, "administrator"),
        "STALWART_ADMIN_PASSWORD": _after(st, "password administrator"),
        "STALWART_URL": "http://localhost:8089",
        "NEXTCLOUD_URL": "http://localhost:8092",
        "ONLYOFFICE_URL": "http://localhost:8093",
        "ONLYOFFICE_JWT_SECRET": _after(oo, "only office jwt"),
        "VIKUNJA_URL": "http://localhost:3456",
        "VIKUNJA_SERVICE_SECRET": _after(vk, "VIKUNJA_SERVICE_SECRET"),
        "VIKUNJA_DATABASE_HOST": _after(vk, "VIKUNJA_DATABASE_HOST"),
        "VIKUNJA_DATABASE_USER": _after(vk, "VIKUNJA_DATABASE_USER"),
        "VIKUNJA_DATABASE_PASSWORD": _after(vk, "VIKUNJA_DATABASE_PASSWORD"),
        "VIKUNJA_DATABASE_DATABASE": _after(vk, "VIKUNJA_DATABASE_DATABASE"),
        "CHATWOOT_URL": "http://localhost:3000",
        "CHATWOOT_ACCOUNT_ID": "2",
        "RAG_URL": "http://localhost:8787",
    }
    return {k: v for k, v in out.items() if v}


def main() -> None:
    if not SECRETS_FILE.exists():
        raise SystemExit(f"missing secrets file: {SECRETS_FILE}")
    text = SECRETS_FILE.read_text(encoding="utf-8-sig")

    secrets = extract_secrets(text)
    expected = {
        "ODOO_DB": "oddo-db",
        "ODOO_MASTER_PASSWORD": "520ta520",
        "ODOO_LOGIN": "tayssirandolssi@gmail.com",
        "ODOO_PASSWORD": "520ta520",
        "ODOO_ADMIN": "activiity",
        "STALWART_ADMIN_EMAIL": "admin@localhost",
        "STALWART_ADMIN_PASSWORD": "94zTJewYY5arKUOT",
        "ONLYOFFICE_JWT_SECRET": "iDUuQxICGKiIMhQ4Tq50SkGumpelhvtQUj792TTuAOU",
    }
    bad = {k: (secrets.get(k), want) for k, want in expected.items() if secrets.get(k) != want}
    if bad:
        print("WARNING: parsed values differ from expectation:")
        for k, (got, want) in bad.items():
            print(f"  {k}: got={got!r} want={want!r}")

    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()

    merged = dict(existing)
    for key, value in secrets.items():
        if key not in existing or not existing[key]:
            merged[key] = value

    lines = [f"{k}={v}" for k, v in merged.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {ENV_FILE} ({len(lines)} entries)")


if __name__ == "__main__":
    main()