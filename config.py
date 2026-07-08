"""Global settings for the generic scraper + dashboard.

DB env vars match the existing collector/dashboard so this drops straight into
the same docker-compose network (POSTGRES_HOST defaults to the service name
'postgres'; set it to 'localhost' when running on the host).
"""
from __future__ import annotations

import os

# ---- Postgres (same env contract as the old dashboard) -------------------
PG = dict(
    host=os.environ.get("POSTGRES_HOST", "postgres"),
    port=os.environ.get("POSTGRES_PORT", "5432"),
    dbname=os.environ.get("POSTGRES_DB", "scraper"),
    user=os.environ.get("POSTGRES_USER", "scraper"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
)

# ---- Polite HTTP defaults (ported from your harvester) -------------------
# A randomized gap in [MIN, MAX] seconds sits between every request so the
# request beat is never perfectly regular. Keep these gentle.
DEFAULT_MIN_DELAY = float(os.environ.get("SCRAPER_MIN_DELAY", "2.0"))
DEFAULT_MAX_DELAY = float(os.environ.get("SCRAPER_MAX_DELAY", "5.0"))
HTTP_TIMEOUT = int(os.environ.get("SCRAPER_HTTP_TIMEOUT", "30"))
HTTP_RETRIES = int(os.environ.get("SCRAPER_HTTP_RETRIES", "3"))
MAX_PAGES_GUARD = int(os.environ.get("SCRAPER_MAX_PAGES", "2000"))

USER_AGENT = os.environ.get(
    "SCRAPER_UA",
    "FortovaScraper/1.0 (config-driven harvest; contact: set SCRAPER_UA)",
)

# Where per-site JSON configs live (one file = one site).
SITES_DIR = os.environ.get("SCRAPER_SITES_DIR",
                           os.path.join(os.path.dirname(__file__), "sites"))

# Drift alert: a field whose fill-rate falls from >= HIGH to <= LOW versus its
# trailing average is flagged as a probably-broken selector.
DRIFT_HIGH = 0.6
DRIFT_LOW = 0.15

# ---- Custom scripts feature ----------------------------------------------
# Where browser-authored custom scripts are saved. Bind-mount this in compose
# (./scripts:/app/scripts) so the .py files survive image rebuilds, like sites/.
SCRIPTS_DIR = os.environ.get("SCRAPER_SCRIPTS_DIR",
                             os.path.join(os.path.dirname(__file__), "scripts"))

# Shared secret that gates the script editor/runner routes. If EMPTY, the whole
# scripts feature is DISABLED (fails safe) — nothing can write or run scripts.
# Set this to enable it. Critical because the dashboard is exposed publicly via
# ngrok with no other auth, and running scripts is arbitrary code execution.
SCRIPTS_TOKEN = os.environ.get("SCRIPTS_TOKEN", "")
