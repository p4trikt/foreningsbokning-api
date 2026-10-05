"""
Föreningsbokningssystem - API-backend (Railway).

Kör lokalt med:  python app.py
Serverar enbart /api/* - frontend och adminpanel driftsätts separat
(Netlify) och anropar detta API över nätet.
"""
import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, request, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

from database import init_db, get_db, ensure_admin
import email_utils

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-nyckel-byt-i-produktion")
# Sessionskaka - håller inloggningen i 30 dagar
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Frontend och API körs på olika domäner (Netlify + Railway), så
# sessionskakan måste tillåtas skickas cross-site.
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True

# Vilka ursprung (frontend-domäner) som får anropa API:t med kakor.
# Sätts via miljövariabeln ALLOWED_ORIGINS (kommaseparerad), t.ex.
# "https://foreningsbokning.netlify.app,https://sdskanebutiken.se"
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
CORS(app, supports_credentials=True, origins=_allowed_origins or "*")

GILTIGA_STATUSAR = {"vantande", "bekraftad", "nekad", "avbokad", "raderad"}


# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

def parse_datum(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def forening_till_dict(row):
    return {
        "id": row["id"],
        "namn": row["namn"],
        "telefonnr": row["telefonnr"],
        "email": row["email"],
        "is_admin": bool(row["is_admin"]),
    }


def objekt_till_dict(row):
    return {
        "id": row["id"],
        "namn": row["namn"],
        "typ": row["typ"],
        "max_dagar": row["max_dagar"],
        "aktiv": bool(row["aktiv"]),
    }


def bokning_till_dict(row, med_forening=False):
    d = {
        "id": row["id"],
        "objekt_id": row["objekt_id"],
        "objekt_namn": row["objekt_namn"] if "objekt_namn" in row.keys() else None,
        "forening_id": row["forening_id"],
        "start_datum": row["start_datum"],
        "slut_datum": row["slut_datum"],
        "status": row["status"],
        "kommentar": row["kommentar"],
        "admin_kommentar": row["admin_kommentar"] if "admin_kommentar" in row.keys() else None,
        "skapad_at": row["skapad_at"],
    }
    if med_forening and "forening_namn" in row.keys():
        d["forening_namn"] = row["forening_namn"]
        d["forening_email"] = row["forening_email"]
        d["forening_telefonnr"] = row["forening_telefonnr"]
    return d


def inloggad_krav(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "forening_id" not in session:
            return jsonify({"fel": "Du måste logga in."}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_krav(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "forening_id" not in session:
            return jsonify({"fel": "Du måste logga in."}), 401
        if not session.get("is_admin"):
            return jsonify({"fel": "Endast administratörer har åtkomst."}), 403
        return f(*args, **kwargs)
    return wrapper


def dagar_i_bokning(start, slut):
    return (slut - start).days + 1


def overlapp_finns(conn, objekt_id, start, slut, undanta_bokning_id=None):
    query = (
        "SELECT id FROM bokningar WHERE objekt_id = ? AND status IN ('vantande', 'bekraftad') "
        "AND NOT (slut_datum < ? OR start_datum > ?)"
    )
    params = [objekt_id, start.isoformat(), slut.isoformat()]
    if undanta_bokning_id is not None:
        query += " AND id != ?"
        params.append(undanta_bokning_id)
    rad = conn.execute(query, params).fetchone()
    return rad is not None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/api/registrera", methods=["POST"])
def registrera():
    data = request.get_json(force=True, silent=True) or {}
    namn = (data.get("namn") or "").strip()
    telefonnr = (data.get("telefonnr") or "").strip()
    epost = (data.get("email") or "").strip().lower()
    losenord = data.get("losenord") or ""
    kontaktpersoner = data.get("kontaktpersoner") or []

    if not namn or not telefonnr or not epost or not losenord:
        return jsonify({"fel": "Föreningens namn, telefonnummer, e-post och lösenord krävs."}), 400
    if "@" not in epost:
        return jsonify({"fel": "Ange en giltig e-postadress."}), 400
    if len(losenord) < 6:
        return jsonify({"fel": "Lösenordet måste vara minst 6 tecken."}), 400
    if not isinstance(kontaktpersoner, list) or len(kontaktpersoner) == 0:
        return jsonify({"fel": "Minst en kontaktperson krävs."}), 400
    for kp in kontaktpersoner:
        if not (kp.get("namn") or "").strip():
            return jsonify({"fel": "Varje kontaktperson måste ha ett namn."}), 400

    with get_db() as conn:
        finns = conn.execute("SELECT id FROM foreningar WHERE email = ?", (epost,)).fetchone()
        if finns:
            return jsonify({"fel": "Det finns redan en profil med den e-postadressen."}), 409

        cur = conn.execute(
            "INSERT INTO foreningar (namn, telefonnr, email, losenord_hash) VALUES (?, ?, ?, ?)",
            (namn, telefonnr, epost, generate_password_hash(losenord)),
        )
        forening_id = cur.lastrowid
        for kp in kontaktpersoner:
            conn.execute(
                "INSERT INTO kontaktpersoner (forening_id, namn, telefon, email) VALUES (?, ?, ?, ?)",
                (forening_id, kp.get("namn", "").strip(), (kp.get("telefon") or "").strip(),
                 (kp.get("email") or "").strip()),
            )

    return jsonify({"ok": True, "meddelande": "Profil skapad. Du kan nu logga in."})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True, silent=True) or {}
    epost = (data.get("email") or "").strip().lower()
    losenord = data.get("losenord") or ""

    with get_db() as conn:
        row = conn.execute("SELECT * FROM foreningar WHERE email = ?", (epost,)).fetchone()

    if row is None or not check_password_hash(row["losenord_hash"], losenord):
        return jsonify({"fel": "Fel e-postadress eller lösenord."}), 401

    session.permanent = True
    session["forening_id"] = row["id"]
    session["is_admin"] = bool(row["is_admin"])
    return jsonify({"ok": True, "forening": forening_till_dict(row)})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
def me():
    if "forening_id" not in session:
        return jsonify({"inloggad": False})
    with get_db() as conn:
        row = conn.execute("SELECT * FROM foreningar WHERE id = ?", (session["forening_id"],)).fetchone()
    if row is None:
        session.clear()
        return jsonify({"inloggad": False})
    return jsonify({"inloggad": True, "forening": forening_till_dict(row)})


# ---------------------------------------------------------------------------
# Objekt (publikt, för att visa vad som kan bokas)
# ---------------------------------------------------------------------------

@app.route("/api/objekt", methods=["GET"])
def lista_objekt():
    with get_db() as conn:
        rader = conn.execute(
            "SELECT * FROM objekt WHERE aktiv = 1 ORDER BY ordning, id"
        ).fetchall()
    return jsonify([objekt_till_dict(r) for r in rader])


@app.route("/api/objekt/<int:objekt_id>/bokade-datum", methods=["GET"])
def bokade_datum(objekt_id):
    """Visar vilka datumintervall som redan är upptagna (utan personuppgifter),
    så att den som bokar kan se vad som är ledigt."""
    with get_db() as conn:
        rader = conn.execute(
            "SELECT start_datum, slut_datum, status FROM bokningar "
            "WHERE objekt_id = ? AND status IN ('vantande', 'bekraftad') "
            "AND slut_datum >= ? ORDER BY start_datum",
            (objekt_id, date.today().isoformat()),
        ).fetchall()
    return jsonify([dict(r) for r in rader])


# ---------------------------------------------------------------------------
# Bokningar (kund/förening)
# ---------------------------------------------------------------------------

@app.route("/api/bokningar", methods=["POST"])
@inloggad_krav
def skapa_bokning():
    data = request.get_json(force=True, silent=True) or {}
    objekt_id = data.get("objekt_id")
    start = parse_datum(data.get("start_datum"))
    slut = parse_datum(data.get("slut_datum"))
    kommentar = (data.get("kommentar") or "").strip()

    if not objekt_id or not start or not slut:
        return jsonify({"fel": "Objekt, startdatum och slutdatum krävs."}), 400
    if slut < start:
        return jsonify({"fel": "Slutdatum kan inte vara före startdatum."}), 400
    if start < date.today():
        return jsonify({"fel": "Startdatum kan inte vara i det förflutna."}), 400

    with get_db() as conn:
        objekt = conn.execute("SELECT * FROM objekt WHERE id = ? AND aktiv = 1", (objekt_id,)).fetchone()
        if objekt is None:
            return jsonify({"fel": "Objektet finns inte eller är inte längre bokningsbart."}), 404

        antal_dagar = dagar_i_bokning(start, slut)
        if antal_dagar < 1 or antal_dagar > objekt["max_dagar"]:
            return jsonify({
                "fel": f"{objekt['namn']} kan bokas i 1 till {objekt['max_dagar']} dagar."
            }), 400

        if overlapp_finns(conn, objekt_id, start, slut):
            return jsonify({"fel": "Valda datum är redan bokade eller väntar på godkännande. Välj andra datum."}), 409

        cur = conn.execute(
            "INSERT INTO bokningar (objekt_id, forening_id, start_datum, slut_datum, kommentar, status) "
            "VALUES (?, ?, ?, ?, ?, 'vantande')",
            (objekt_id, session["forening_id"], start.isoformat(), slut.isoformat(), kommentar),
        )
        booking_id = cur.lastrowid
        forening = conn.execute("SELECT namn FROM foreningar WHERE id = ?", (session["forening_id"],)).fetchone()
        objekt_namn = objekt["namn"]

    email_utils.notify_admin_ny_bokning(objekt_namn, forening["namn"], start.isoformat(), slut.isoformat(), booking_id)

    return jsonify({"ok": True, "meddelande": "Bokningsförfrågan skickad. Den väntar nu på godkännande.", "id": booking_id})


@app.route("/api/mina-bokningar", methods=["GET"])
@inloggad_krav
def mina_bokningar():
    with get_db() as conn:
        rader = conn.execute(
            "SELECT b.*, o.namn AS objekt_namn FROM bokningar b "
            "JOIN objekt o ON o.id = b.objekt_id "
            "WHERE b.forening_id = ? AND b.status != 'raderad' "
            "ORDER BY b.start_datum DESC",
            (session["forening_id"],),
        ).fetchall()
    return jsonify([bokning_till_dict(r) for r in rader])


@app.route("/api/bokningar/<int:booking_id>/avboka", methods=["POST"])
@inloggad_krav
def avboka(booking_id):
    with get_db() as conn:
        rad = conn.execute("SELECT * FROM bokningar WHERE id = ?", (booking_id,)).fetchone()
        if rad is None or rad["forening_id"] != session["forening_id"]:
            return jsonify({"fel": "Bokningen hittades inte."}), 404
        if rad["status"] not in ("vantande", "bekraftad"):
            return jsonify({"fel": "Bokningen kan inte avbokas."}), 400
        conn.execute(
            "UPDATE bokningar SET status = 'avbokad', andrad_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = ?",
            (booking_id,),
        )
    return jsonify({"ok": True, "meddelande": "Bokningen är avbokad."})


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/api/admin/bokningar", methods=["GET"])
@admin_krav
def admin_lista_bokningar():
    status_filter = request.args.get("status")
    query = (
        "SELECT b.*, o.namn AS objekt_namn, f.namn AS forening_namn, "
        "f.email AS forening_email, f.telefonnr AS forening_telefonnr "
        "FROM bokningar b "
        "JOIN objekt o ON o.id = b.objekt_id "
        "JOIN foreningar f ON f.id = b.forening_id "
        "WHERE b.status != 'raderad'"
    )
    params = []
    if status_filter and status_filter in GILTIGA_STATUSAR:
        query += " AND b.status = ?"
        params.append(status_filter)
    query += " ORDER BY b.skapad_at DESC"
    with get_db() as conn:
        rader = conn.execute(query, params).fetchall()
    return jsonify([bokning_till_dict(r, med_forening=True) for r in rader])


@app.route("/api/admin/bokningar/<int:booking_id>/godkann", methods=["POST"])
@admin_krav
def admin_godkann(booking_id):
    with get_db() as conn:
        rad = conn.execute(
            "SELECT b.*, o.namn AS objekt_namn, f.email AS forening_email "
            "FROM bokningar b JOIN objekt o ON o.id = b.objekt_id "
            "JOIN foreningar f ON f.id = b.forening_id WHERE b.id = ?",
            (booking_id,),
        ).fetchone()
        if rad is None:
            return jsonify({"fel": "Bokningen hittades inte."}), 404
        conn.execute(
            "UPDATE bokningar SET status = 'bekraftad', andrad_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = ?",
            (booking_id,),
        )
    email_utils.notify_forening_bekraftad(rad["forening_email"], rad["objekt_namn"], rad["start_datum"], rad["slut_datum"])
    return jsonify({"ok": True})


@app.route("/api/admin/bokningar/<int:booking_id>/neka", methods=["POST"])
@admin_krav
def admin_neka(booking_id):
    data = request.get_json(force=True, silent=True) or {}
    anledning = (data.get("anledning") or "").strip()
    with get_db() as conn:
        rad = conn.execute(
            "SELECT b.*, o.namn AS objekt_namn, f.email AS forening_email "
            "FROM bokningar b JOIN objekt o ON o.id = b.objekt_id "
            "JOIN foreningar f ON f.id = b.forening_id WHERE b.id = ?",
            (booking_id,),
        ).fetchone()
        if rad is None:
            return jsonify({"fel": "Bokningen hittades inte."}), 404
        conn.execute(
            "UPDATE bokningar SET status = 'nekad', admin_kommentar = ?, andrad_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = ?",
            (anledning, booking_id),
        )
    email_utils.notify_forening_nekad(rad["forening_email"], rad["objekt_namn"], rad["start_datum"], rad["slut_datum"], anledning)
    return jsonify({"ok": True})


@app.route("/api/admin/bokningar/<int:booking_id>", methods=["PUT"])
@admin_krav
def admin_redigera_bokning(booking_id):
    data = request.get_json(force=True, silent=True) or {}
    with get_db() as conn:
        rad = conn.execute("SELECT * FROM bokningar WHERE id = ?", (booking_id,)).fetchone()
        if rad is None:
            return jsonify({"fel": "Bokningen hittades inte."}), 404

        start = parse_datum(data.get("start_datum", rad["start_datum"]))
        slut = parse_datum(data.get("slut_datum", rad["slut_datum"]))
        status = data.get("status", rad["status"])
        admin_kommentar = data.get("admin_kommentar", rad["admin_kommentar"])

        if status not in GILTIGA_STATUSAR:
            return jsonify({"fel": "Ogiltig status."}), 400
        if start is None or slut is None or slut < start:
            return jsonify({"fel": "Ogiltiga datum."}), 400

        if status in ("vantande", "bekraftad") and overlapp_finns(conn, rad["objekt_id"], start, slut, undanta_bokning_id=booking_id):
            return jsonify({"fel": "Datumen krockar med en annan bokning för samma objekt."}), 409

        conn.execute(
            "UPDATE bokningar SET start_datum = ?, slut_datum = ?, status = ?, admin_kommentar = ?, "
            "andrad_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = ?",
            (start.isoformat(), slut.isoformat(), status, admin_kommentar, booking_id),
        )
    return jsonify({"ok": True})


@app.route("/api/admin/bokningar/<int:booking_id>", methods=["DELETE"])
@admin_krav
def admin_radera_bokning(booking_id):
    # Enligt önskad rutin: raderar aldrig direkt, "karantän" via statusen 'raderad'.
    with get_db() as conn:
        rad = conn.execute("SELECT id FROM bokningar WHERE id = ?", (booking_id,)).fetchone()
        if rad is None:
            return jsonify({"fel": "Bokningen hittades inte."}), 404
        conn.execute(
            "UPDATE bokningar SET status = 'raderad', andrad_at = to_char(now(), 'YYYY-MM-DD HH24:MI:SS') WHERE id = ?",
            (booking_id,),
        )
    return jsonify({"ok": True, "meddelande": "Bokningen är flyttad till karantän (dold, men inte permanent borttagen)."})


@app.route("/api/admin/bokningar", methods=["POST"])
@admin_krav
def admin_skapa_bokning():
    """Admin kan lägga in en bokning manuellt (t.ex. telefonbokning), direkt bekräftad."""
    data = request.get_json(force=True, silent=True) or {}
    objekt_id = data.get("objekt_id")
    forening_id = data.get("forening_id")
    start = parse_datum(data.get("start_datum"))
    slut = parse_datum(data.get("slut_datum"))
    kommentar = (data.get("kommentar") or "").strip()

    if not objekt_id or not forening_id or not start or not slut:
        return jsonify({"fel": "Objekt, förening, startdatum och slutdatum krävs."}), 400
    if slut < start:
        return jsonify({"fel": "Slutdatum kan inte vara före startdatum."}), 400

    with get_db() as conn:
        objekt = conn.execute("SELECT * FROM objekt WHERE id = ?", (objekt_id,)).fetchone()
        forening = conn.execute("SELECT * FROM foreningar WHERE id = ?", (forening_id,)).fetchone()
        if objekt is None or forening is None:
            return jsonify({"fel": "Objekt eller förening hittades inte."}), 404
        if overlapp_finns(conn, objekt_id, start, slut):
            return jsonify({"fel": "Datumen krockar med en annan bokning för samma objekt."}), 409
        cur = conn.execute(
            "INSERT INTO bokningar (objekt_id, forening_id, start_datum, slut_datum, kommentar, status) "
            "VALUES (?, ?, ?, ?, ?, 'bekraftad')",
            (objekt_id, forening_id, start.isoformat(), slut.isoformat(), kommentar),
        )
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/admin/objekt", methods=["GET"])
@admin_krav
def admin_lista_objekt():
    with get_db() as conn:
        rader = conn.execute("SELECT * FROM objekt ORDER BY ordning, id").fetchall()
    return jsonify([objekt_till_dict(r) for r in rader])


@app.route("/api/admin/objekt", methods=["POST"])
@admin_krav
def admin_skapa_objekt():
    data = request.get_json(force=True, silent=True) or {}
    namn = (data.get("namn") or "").strip()
    typ = (data.get("typ") or "").strip()
    max_dagar = data.get("max_dagar", 7)
    if not namn or not typ:
        return jsonify({"fel": "Namn och typ krävs."}), 400
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO objekt (namn, typ, max_dagar, ordning) VALUES (?, ?, ?, "
            "(SELECT COALESCE(MAX(ordning), 0) + 1 FROM objekt))",
            (namn, typ, max_dagar),
        )
    return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/admin/objekt/<int:objekt_id>", methods=["PUT"])
@admin_krav
def admin_redigera_objekt(objekt_id):
    data = request.get_json(force=True, silent=True) or {}
    with get_db() as conn:
        rad = conn.execute("SELECT * FROM objekt WHERE id = ?", (objekt_id,)).fetchone()
        if rad is None:
            return jsonify({"fel": "Objektet hittades inte."}), 404
        namn = data.get("namn", rad["namn"])
        typ = data.get("typ", rad["typ"])
        max_dagar = data.get("max_dagar", rad["max_dagar"])
        aktiv = data.get("aktiv", rad["aktiv"])
        conn.execute(
            "UPDATE objekt SET namn = ?, typ = ?, max_dagar = ?, aktiv = ? WHERE id = ?",
            (namn, typ, max_dagar, int(bool(aktiv)), objekt_id),
        )
    return jsonify({"ok": True})


@app.route("/api/admin/objekt/<int:objekt_id>", methods=["DELETE"])
@admin_krav
def admin_ta_bort_objekt(objekt_id):
    # Avaktiverar istället för att radera, så historiska bokningar bevaras.
    with get_db() as conn:
        conn.execute("UPDATE objekt SET aktiv = 0 WHERE id = ?", (objekt_id,))
    return jsonify({"ok": True})


@app.route("/api/admin/objekt/<int:objekt_id>/radera-permanent", methods=["DELETE"])
@admin_krav
def admin_radera_objekt_permanent(objekt_id):
    """Tar bort objektet helt och hållet. Tillåts bara om det aldrig haft
    några bokningar, så att ingen bokningshistorik kan försvinna av misstag."""
    with get_db() as conn:
        objekt = conn.execute("SELECT * FROM objekt WHERE id = ?", (objekt_id,)).fetchone()
        if objekt is None:
            return jsonify({"fel": "Objektet hittades inte."}), 404
        antal_bokningar = conn.execute(
            "SELECT COUNT(*) AS c FROM bokningar WHERE objekt_id = ?", (objekt_id,)
        ).fetchone()["c"]
        if antal_bokningar > 0:
            return jsonify({
                "fel": "Objektet har bokningar kopplade till sig och kan inte raderas permanent. "
                       "Avaktivera det istället så döljs det från bokningssidan."
            }), 409
        conn.execute("DELETE FROM objekt WHERE id = ?", (objekt_id,))
    return jsonify({"ok": True, "meddelande": "Objektet är permanent borttaget."})


@app.route("/api/admin/foreningar", methods=["GET"])
@admin_krav
def admin_lista_foreningar():
    with get_db() as conn:
        foreningar = conn.execute("SELECT * FROM foreningar WHERE is_admin = 0 ORDER BY namn").fetchall()
        resultat = []
        for f in foreningar:
            kontakter = conn.execute(
                "SELECT * FROM kontaktpersoner WHERE forening_id = ?", (f["id"],)
            ).fetchall()
            d = forening_till_dict(f)
            d["kontaktpersoner"] = [
                {"namn": k["namn"], "telefon": k["telefon"], "email": k["email"]} for k in kontakter
            ]
            resultat.append(d)
    return jsonify(resultat)


# ---------------------------------------------------------------------------
# Hälsokontroll (för Railway)
# ---------------------------------------------------------------------------

@app.route("/")
@app.route("/health")
def halsokontroll():
    return jsonify({"status": "ok", "tjanst": "foreningsbokning-api"})


# ---------------------------------------------------------------------------
# Uppstart
# ---------------------------------------------------------------------------

def skapa_admin_fran_env():
    admin_email = os.environ.get("ADMIN_EMAIL")
    admin_losenord = os.environ.get("ADMIN_PASSWORD")
    if admin_email and admin_losenord:
        ensure_admin(admin_email.strip().lower(), generate_password_hash(admin_losenord))
        print(f"[OK] Adminkonto klart för {admin_email}")
    else:
        print("[VARNING] ADMIN_EMAIL / ADMIN_PASSWORD är inte satta - inget adminkonto skapat/uppdaterat.")


# Körs vid varje appstart (även under gunicorn på Railway, inte bara
# "python app.py" lokalt), så att schema/admin alltid är i synk.
init_db()
skapa_admin_fran_env()


if __name__ == "__main__":
    # Läs in .env manuellt för lokal körning (utan extra beroenden)
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            for rad in f:
                rad = rad.strip()
                if not rad or rad.startswith("#") or "=" not in rad:
                    continue
                key, _, value = rad.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
