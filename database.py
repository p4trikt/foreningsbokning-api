"""
Databaslager för föreningsbokningssystemet.
Ansluter till Postgres (Supabase) via psycopg2, i ett eget schema
("foreningsbokning") så att tabellerna inte krockar med andra appar
som delar samma Supabase-projekt.

Ett tunt kompatibilitetslager gör att resten av koden (skriven mot
sqlite3-stil med "?"-platshållare och cur.lastrowid) kan köras mot
Postgres med minimala ändringar i app.py.
"""
import os
import re
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")
DB_SCHEMA = os.environ.get("DB_SCHEMA", "foreningsbokning")

# Återanvänd en enda uppkoppling mot databasen per processt (gunicorn-worker)
# istället för att öppna en ny anslutning (med TCP/TLS-handskakning och
# autentisering mot Supabase) för varje enskild förfrågan. Det var den stora
# anledningen till att sidan kändes seg - en ny anslutning tog ofta
# 1-3 sekunder att sätta upp. SET search_path körs ändå i varje request,
# eftersom Supabase Transaction Pooler kan ge olika bakomliggande
# databas-sessioner för olika transaktioner på samma klientuppkoppling.
_conn = None
_conn_lock = threading.Lock()


def _ny_anslutning():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def _hamta_anslutning():
    global _conn
    with _conn_lock:
        if _conn is None or _conn.closed:
            _conn = _ny_anslutning()
        return _conn

_PLACEHOLDER = re.compile(r"\?")

# Postgres-kompatibelt schema (motsvarar den gamla SQLite-SCHEMA-strängen).
# Skapas idempotent vid uppstart som en extra säkerhet, utöver migrationen
# som redan är körd i Supabase.
SCHEMA = f"""
CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA};

CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.foreningar (
    id SERIAL PRIMARY KEY,
    namn TEXT NOT NULL,
    telefonnr TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    losenord_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    skapad_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.kontaktpersoner (
    id SERIAL PRIMARY KEY,
    forening_id INTEGER NOT NULL REFERENCES {DB_SCHEMA}.foreningar(id) ON DELETE CASCADE,
    namn TEXT NOT NULL,
    telefon TEXT,
    email TEXT
);

CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.objekt (
    id SERIAL PRIMARY KEY,
    namn TEXT NOT NULL,
    typ TEXT NOT NULL,
    max_dagar INTEGER NOT NULL DEFAULT 7,
    aktiv INTEGER NOT NULL DEFAULT 1,
    ordning INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.bokningar (
    id SERIAL PRIMARY KEY,
    objekt_id INTEGER NOT NULL REFERENCES {DB_SCHEMA}.objekt(id),
    forening_id INTEGER NOT NULL REFERENCES {DB_SCHEMA}.foreningar(id),
    start_datum TEXT NOT NULL,
    slut_datum TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'vantande',
    kommentar TEXT,
    admin_kommentar TEXT,
    skapad_at TEXT NOT NULL DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
    andrad_at TEXT
);
"""

# Fasta bokningsbara objekt enligt kravet.
SEED_OBJEKT = [
    ("Bil 1", "bil", 7),
    ("Bil 2", "bil", 7),
    ("Bil 3", "bil", 7),
    ("Släp", "slap", 7),
    ("Popcornmaskin", "popcornmaskin", 7),
    ("Elefantdräkt 1", "elefantdrakt", 7),
    ("Elefantdräkt 2", "elefantdrakt", 7),
    ("Skånebutiken", "skanebutiken", 2),
]


class _CompatCursor:
    """Wrapper runt en psycopg2-cursor som efterliknar sqlite3:
    - översätter "?" till "%s"
    - sätter .lastrowid efter INSERT (via RETURNING id)
    - raderna beter sig som dict (RealDictCursor), så rad["kolumn"] och
      "kolumn" in rad.keys() fungerar precis som med sqlite3.Row.
    """

    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    def execute(self, query, params=None):
        q = _PLACEHOLDER.sub("%s", query)
        stripped = q.strip().upper()
        is_insert = stripped.startswith("INSERT")
        if is_insert and "RETURNING" not in stripped:
            q = q.rstrip().rstrip(";") + " RETURNING id"
        self._cur.execute(q, params if params is not None else [])
        if is_insert:
            try:
                row = self._cur.fetchone()
                self.lastrowid = row["id"] if row else None
            except psycopg2.ProgrammingError:
                self.lastrowid = None
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _CompatConnection:
    """Wrapper runt en psycopg2-connection som exponerar conn.execute(...)
    likt sqlite3.Connection, för att undvika att skriva om alla anrop i app.py."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, query, params=None):
        cur = _CompatCursor(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))
        cur.execute(query, params)
        return cur

    def executescript(self, script):
        with self._conn.cursor() as cur:
            cur.execute(script)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


@contextmanager
def get_db():
    global _conn
    conn = _hamta_anslutning()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {DB_SCHEMA}, public")
        wrapped = _CompatConnection(conn)
        yield wrapped
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # Anslutningen är trasig (t.ex. timeout eller avbruten av poolern).
        # Stäng den och tvinga fram en ny anslutning nästa gång get_db() anropas.
        with _conn_lock:
            try:
                conn.close()
            except Exception:
                pass
            if _conn is conn:
                _conn = None
        raise
    except Exception:
        conn.rollback()
        raise


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        existing = conn.execute(f"SELECT COUNT(*) AS c FROM {DB_SCHEMA}.objekt").fetchone()["c"]
        if existing == 0:
            for i, (namn, typ, max_dagar) in enumerate(SEED_OBJEKT):
                conn.execute(
                    "INSERT INTO objekt (namn, typ, max_dagar, ordning) VALUES (?, ?, ?, ?)",
                    (namn, typ, max_dagar, i),
                )


def ensure_admin(email: str, password_hash: str, namn: str = "Administratör", telefonnr: str = ""):
    """Skapar (eller uppgraderar till) admin-kontot om det inte redan finns."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM foreningar WHERE email = ?", (email,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO foreningar (namn, telefonnr, email, losenord_hash, is_admin) "
                "VALUES (?, ?, ?, ?, 1)",
                (namn, telefonnr, email, password_hash),
            )
        else:
            conn.execute("UPDATE foreningar SET is_admin = 1 WHERE id = ?", (row["id"],))
