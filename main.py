import os
import json
import sqlite3
import math
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ==========================================
# CONFIGURAZIONE CLOUD & DATABASE
# ==========================================
DB_PATH = "/tmp/eventi_tattici.db" if os.path.exists("/tmp") else "eventi_tattici.db"
FILE_DISPOSITIVI = "/tmp/dispositivi.json" if os.path.exists("/tmp") else "dispositivi.json"

COORDINATE_CASA = {"lat": 40.8518, "lon": 14.2681}
RAGGIO_SICUREZZA_KM = 5.0

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
CLIENT_ID = "admin@sgmanagemets.net-api-client"
CLIENT_SECRET = "lHLykeQYkPTk1wmzcaorGM7K70nbbdPV"

def determina_categoria(icao24, callsign, type_code=""):
    icao24 = str(icao24 or "").lower().strip()
    cs = str(callsign or "").upper().strip()
    tc = str(type_code or "").upper().strip()
    
    tipi_elicotteri = ("EC35", "EC45", "EC55", "A109", "A139", "A169", "AW1", "B06", "B412", "H60", "H47", "R44", "R66", "AS35", "AS50", "BELL")
    if (tc.startswith(tipi_elicotteri) or 
        any(cs.startswith(p) for p in ("HELI", "HLE", "EMS", "PEGASO", "SAMU", "REGA", "POLICE", "POLIZIA", "CARAB", "CORPO"))):
        return "ELICOTTERO"

    if (icao24.startswith(('33f', '300', '301', '33e', 'ae', 'af', '43c', '3f')) or 
        any(cs.startswith(p) for p in ("IAM", "RCH", "GAF", "AME", "ASY", "RRR", "FORZA"))):
        return "MILITARE"

    if any(cs.startswith(p) for p in ("DHL", "FDX", "UPS", "CLX", "GTI", "BOX", "BCS")):
        return "CARGO"

    if (cs.startswith("N") and len(cs) <= 6 and cs[1:].isalnum()) or cs.startswith(("M-", "T7-", "VP-C", "3B-")):
        return "PRIVATO"

    return "LINEA"

class TokenManager:
    def __init__(self):
        self.token = None
        self.expires_at = None
        self.session = requests.Session()

    def get_token(self):
        if self.token and self.expires_at and datetime.now() < self.expires_at:
            return self.token
        try:
            r = self.session.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                timeout=5
            )
            r.raise_for_status()
            data = r.json()
            self.token = data["access_token"]
            expires_in = data.get("expires_in", 1800)
            self.expires_at = datetime.now() + timedelta(seconds=expires_in - 30)
            return self.token
        except Exception as e:
            print("Errore OAuth2 OpenSky:", e)
            return None

    def headers(self):
        token = self.get_token()
        h = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

token_manager = TokenManager()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS terremoti (
            id TEXT PRIMARY KEY, magnitudo REAL, luogo TEXT,
            lat REAL, lon REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS eventi_custom (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, categoria TEXT,
            lat REAL, lon REAL, colore TEXT DEFAULT '#a855f7', timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS storico_gps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT,
            lat REAL, lon REAL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_sessione (
            id INTEGER PRIMARY KEY CHECK (id = 1), ultimo_logout DATETIME
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO app_sessione (id, ultimo_logout) VALUES (1, CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

init_db()

def salva_timestamp_logout():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE app_sessione SET ultimo_logout = CURRENT_TIMESTAMP WHERE id = 1")
        conn.commit()
        conn.close()
    except Exception as e:
        print("Errore logout:", e)

def get_timestamp_logout():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ultimo_logout FROM app_sessione WHERE id = 1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as e:
        print("Errore lettura logout:", e)
    return "1970-01-01 00:00:00"

DISPOSITIVI_DEFAULT = {
    "PC_PRINCIPALE": {
        "nome": "PC Lavoro / Personale", "proprietario": "Tu", 
        "colore": "#00f2fe", "tipo": "IP", "attivo": True,
        "lat": 40.8518, "lon": 14.2681, "stato": "In casa"
    },
    "IPHONE_MARTINA": {
        "nome": "iPhone di Martina", "proprietario": "Figlia", 
        "colore": "#e11d48", "tipo": "GPS", "attivo": True,
        "lat": 40.8518, "lon": 14.2681, "stato": "In casa"
    }
}

def carica_dispositivi():
    if os.path.exists(FILE_DISPOSITIVI):
        try:
            with open(FILE_DISPOSITIVI, "r", encoding="utf-8") as f:
                dati = json.load(f)
                if dati:
                    if "IPHONE_MARTINA" not in dati:
                        dati["IPHONE_MARTINA"] = DISPOSITIVI_DEFAULT["IPHONE_MARTINA"]
                        salva_dispositivi(dati)
                    return dati
        except:
            pass
    salva_dispositivi(DISPOSITIVI_DEFAULT)
    return DISPOSITIVI_DEFAULT.copy()

def salva_dispositivi(db):
    try:
        with open(FILE_DISPOSITIVI, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=4)
    except Exception as e:
        print(f"Errore JSON: {e}")

DISPOSITIVI_DB = carica_dispositivi()

def calcola_distanza_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# ==========================================
# ROTTE API FLASK PER WEB INTERFACE
# ==========================================

@app.route('/api/dispositivi', methods=['GET'])
def get_dispositivi_api():
    return jsonify(DISPOSITIVI_DB)

@app.route('/api/aggiungi_dispositivo', methods=['POST'])
def aggiungi_dispositivo_api():
    data = request.json or {}
    dev_id = data.get("dev_id")
    prop = data.get("prop", "").strip()
    nome = data.get("nome", "").strip()
    colore = data.get("colore", "#f97316")
    base_lat = DISPOSITIVI_DB.get("PC_PRINCIPALE", {}).get("lat") or COORDINATE_CASA["lat"]
    base_lon = DISPOSITIVI_DB.get("PC_PRINCIPALE", {}).get("lon") or COORDINATE_CASA["lon"]

    DISPOSITIVI_DB[dev_id] = {
        "nome": nome, "proprietario": prop,
        "colore": colore, "tipo": "GPS", "attivo": True,
        "lat": base_lat, "lon": base_lon, "stato": "In casa"
    }
    salva_dispositivi(DISPOSITIVI_DB)
    return jsonify({"status": "ok"})

@app.route('/api/modifica_dispositivo', methods=['POST'])
def modifica_dispositivo_api():
    data = request.json or {}
    dev_id = data.get("dev_id")
    if dev_id in DISPOSITIVI_DB:
        DISPOSITIVI_DB[dev_id]["proprietario"] = data.get("prop", "").strip()
        DISPOSITIVI_DB[dev_id]["nome"] = data.get("nome", "").strip()
        DISPOSITIVI_DB[dev_id]["colore"] = data.get("colore")
        salva_dispositivi(DISPOSITIVI_DB)
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "msg": "Not found"})

@app.route('/api/rimuovi_dispositivo', methods=['POST'])
def rimuovi_dispositivo_api():
    data = request.json or {}
    dev_id = data.get("dev_id")
    if dev_id in DISPOSITIVI_DB:
        del DISPOSITIVI_DB[dev_id]
        salva_dispositivi(DISPOSITIVI_DB)
    return jsonify({"status": "ok"})

@app.route('/api/toggle_dispositivo', methods=['POST'])
def toggle_dispositivo_api():
    data = request.json or {}
    dev_id = data.get("dev_id")
    stato = data.get("stato")
    if dev_id in DISPOSITIVI_DB:
        DISPOSITIVI_DB[dev_id]["attivo"] = stato
        salva_dispositivi(DISPOSITIVI_DB)
    return jsonify({"status": "ok"})

@app.route('/api/radar_timestamp', methods=['GET'])
def get_latest_radar_timestamp_api():
    try:
        res = requests.get("https://api.rainviewer.com/public/weather-maps.json", timeout=5)
        if res.status_code == 200:
            data = res.json()
            radar_past = data.get("radar", {}).get("past", [])
            if radar_past:
                latest = radar_past[-1].get("time")
                return jsonify({"status": "ok", "time": latest, "host": data.get("host")})
    except Exception as e:
        print("Errore radar timestamp:", e)
    return jsonify({"status": "error"})

@app.route('/api/mcdonalds', methods=['GET'])
def fetch_mcdonalds_api():
    lamin = request.args.get("lamin")
    lomin = request.args.get("lomin")
    lamax = request.args.get("lamax")
    lomax = request.args.get("lomax")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) RadicaliumMap/1.0'}
    url_nom = f"https://nominatim.openstreetmap.org/search?q=McDonald's&format=json&addressdetails=1&bounded=1&viewbox={lomin},{lamax},{lomax},{lamin}&limit=50"
    
    try:
        res = requests.get(url_nom, headers=headers, timeout=6)
        if res.status_code == 200:
            data = res.json()
            locali = []
            for item in data:
                addr = item.get("address", {})
                citta = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("suburb") or "Città"
                regione = addr.get("state") or addr.get("county") or "Regione"
                stato = addr.get("country") or "Italia"
                
                via = addr.get("road") or addr.get("pedestrian") or addr.get("suburb") or "Indirizzo in Mappa"
                civico = addr.get("housenumber") or ""
                if civico: via = f"{via} {civico}"

                locali.append({
                    "id": item.get("place_id"),
                    "nome": "McDonald's",
                    "lat": float(item.get("lat")),
                    "lon": float(item.get("lon")),
                    "citta": citta,
                    "regione": regione,
                    "stato": stato,
                    "via": via,
                    "orario": "07:00 - 01:00",
                    "order_link": "https://www.mcdonalds.it/trova-il-ristorante"
                })
            return jsonify({"status": "OK", "count": len(locali), "data": locali})
    except Exception as e:
        print("Errore McD:", e)
    return jsonify({"status": "ERROR", "count": 0, "data": []})

@app.route('/api/navi_live', methods=['GET'])
def fetch_navi_live_api():
    latC = request.args.get("lat", 41.9)
    lonC = request.args.get("lon", 12.5)
    try:
        url = f"https://api.adsb.lol/v2/point/{latC}/{lonC}/350"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        navi = []
        if res.status_code == 200:
            ac = res.json().get("ac", []) or []
            for p in ac:
                if p.get("lat") is not None and p.get("lon") is not None:
                    alt = p.get("alt_baro", 0)
                    if alt == "ground" or (isinstance(alt, (int, float)) and alt <= 80):
                        navi.append({
                            "mmsi": str(p.get("hex", "AIS-SHIP")).upper(),
                            "nome": str(p.get("flight", "") or p.get("r", "")).strip() or "NAVE IN TRANSITO",
                            "tipo": "NAVE COMMERCIAL / FERRY",
                            "costruttore": "Cantiere Navale",
                            "tratta": "ROTTA MARITTIMA IN CORSO",
                            "lat": float(p["lat"]),
                            "lon": float(p["lon"]),
                            "velocita": round(float(p.get("gs", 0) or 0))
                        })
            return jsonify(navi)
    except Exception as e:
        print("Errore navi:", e)
    return jsonify([])

@app.route('/api/trasporti_campania', methods=['GET'])
def fetch_trasporti_campania_api():
    trasporti = [
        {"id": "ANM_L1_GARIBALDI", "nome": "ANM Metro L1 - Garibaldi", "rete": "ANM METRO", "modello": "AnsaldoBreda CAF MA 600", "costruttore": "CAF / AnsaldoBreda", "tratta": "Piscinola ➔ Garibaldi", "lat": 40.8525, "lon": 14.2721, "stato": "REGOLARE"},
        {"id": "ANM_L1_TOLEDO", "nome": "ANM Metro L1 - Toledo", "rete": "ANM METRO", "modello": "CAF Inneo Serie 100", "costruttore": "CAF Spagna", "tratta": "Garibaldi ➔ Piscinola", "lat": 40.8428, "lon": 14.2492, "stato": "REGOLARE"},
        {"id": "EAV_CIRCUM_SORRENTO", "nome": "EAV Circumvesuviana Express", "rete": "EAV FERROVIE", "modello": "ETR 211 Metrostar", "costruttore": "AnsaldoBreda / Firema", "tratta": "Napoli P. Nolana ➔ Sorrento", "lat": 40.8522, "lon": 14.2718, "stato": "ATTIVO"},
        {"id": "EAV_CUMANA_MONTESANTO", "nome": "EAV Ferrovia Cumana SEPSA", "rete": "EAV FERROVIE", "modello": "ET 500 Titano", "costruttore": "TFA Titano / Firema", "tratta": "Montesanto ➔ Torregaveta", "lat": 40.8465, "lon": 14.2461, "stato": "ATTIVO"},
        {"id": "ANM_FUNI_CENTRALE", "nome": "ANM Funicolare Centrale", "rete": "ANM FUNICOLARI", "modello": "Carrozza Vetrata Impianto Fisso", "costruttore": "Ceretti & Tanfani", "tratta": "Augusteo ➔ Piazza Fuga", "lat": 40.8398, "lon": 14.2468, "stato": "REGOLARE"},
        {"id": "ANM_BUS_ALIBUS", "nome": "ANM Alibus Navetta Aeroporto", "rete": "ANM BUS", "modello": "Iveco Bus Urbanway 12m Hybrid", "costruttore": "Iveco Bus France", "tratta": "Aeroporto Capodichino ➔ Molo Beverello", "lat": 40.8845, "lon": 14.2862, "stato": "IN TRANSITO"},
        {"id": "RFI_NAPOLI_CALE", "nome": "Trenitalia Regionale Metropolitano", "rete": "TRENITALIA", "modello": "ETR 104 Pop / Rock", "costruttore": "Alstom / Hitachi Rail", "tratta": "Napoli C.le ➔ Salerno / Caserta", "lat": 40.8530, "lon": 14.2735, "stato": "REGOLARE"}
    ]
    return jsonify(trasporti)

@app.route('/api/foto_aereo/<icao24>', methods=['GET'])
def fetch_foto_aereo_api(icao24):
    try:
        url = f"https://api.planespotters.net/pub/photos/hex/{icao24.lower()}"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        if res.status_code == 200:
            photos = res.json().get("photos", [])
            if photos:
                return jsonify({"status": "OK", "image_url": photos[0].get("thumbnail_large", {}).get("src")})
    except Exception as e:
        print("Errore foto:", e)
    return jsonify({"status": "NOT_FOUND"})

@app.route('/api/tratta_volo/<callsign>', methods=['GET'])
def fetch_tratta_volo_api(callsign):
    cs = callsign.strip().upper()
    if not cs or cs == "N/A":
        return jsonify({"status": "NOT_FOUND"})
    try:
        url = f"https://api.adsbdb.com/v0/callsign/{cs}"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        if res.status_code == 200:
            data = res.json().get("response", {}).get("flightroute", {})
            origin = data.get("origin", {})
            dest = data.get("destination", {})
            dep = origin.get("iata_code") or origin.get("icao_code") or origin.get("municipality") or ""
            arr = dest.get("iata_code") or dest.get("icao_code") or dest.get("municipality") or ""
            if dep and arr:
                return jsonify({"status": "OK", "tratta": f"{dep} ➔ {arr}"})
    except Exception as e:
        print("Errore tratta:", e)
    return jsonify({"status": "NOT_FOUND"})

@app.route('/api/voli_hybrid', methods=['GET'])
def fetch_voli_hybrid_api():
    lamin = request.args.get("lamin")
    lomin = request.args.get("lomin")
    lamax = request.args.get("lamax")
    lomax = request.args.get("lomax")
    latC = float(request.args.get("lat", 41.9))
    lonC = float(request.args.get("lon", 12.5))
    zoom = int(request.args.get("zoom", 6))

    voli = []
    try:
        url_os = f"https://opensky-network.org/api/states/all?lamin={lamin}&lomin={lomin}&lamax={lamax}&lomax={lomax}"
        res_os = token_manager.session.get(url_os, headers=token_manager.headers(), timeout=5)
        crediti = res_os.headers.get("X-Rate-Limit-Remaining", "OK")

        if res_os.status_code == 200:
            states = res_os.json().get("states", []) or []
            for p in states:
                if p[6] is not None and p[5] is not None:
                    icao24 = str(p[0] or "").upper()
                    cs = str(p[1] or "").strip() or icao24
                    cat_aereo = determina_categoria(icao24, cs)
                    p_lat, p_lon = float(p[6]), float(p[5])
                    hdg = round(float(p[10] or 0))
                    alt_ft = round(float(p[7] or 0) * 3.28084)
                    vel_kn = round(float(p[9] or 0) * 1.94384)
                    squawk = str(p[14] or "----")

                    rad = math.radians(hdg)
                    dep_lat = p_lat - 1.8 * math.cos(rad)
                    dep_lon = p_lon - 1.8 * math.sin(rad)

                    voli.append({
                        "icao24": icao24, "callsign": cs, "lat": p_lat, "lon": p_lon,
                        "heading": hdg, "alt_ft": alt_ft, "speed_kn": vel_kn, "squawk": squawk,
                        "categoria": cat_aereo, "type_code": "OpenSky Asset",
                        "dep_lat": dep_lat, "dep_lon": dep_lon
                    })
            return jsonify({"status": "200 OK (OpenSky)", "credits": f"{crediti} / 4000", "count": len(voli), "data": voli})
    except Exception as e:
        print("OpenSky fallback:", e)

    radius = 500 if zoom <= 4 else (350 if zoom <= 6 else 200)
    endpoints_fb = [
        f"https://api.adsb.fi/v2/point/{latC}/{lonC}/{radius}",
        f"https://api.adsb.lol/v2/point/{latC}/{lonC}/{radius}"
    ]

    for url in endpoints_fb:
        try:
            res_fb = token_manager.session.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
            if res_fb.status_code == 200:
                ac = res_fb.json().get("ac", []) or []
                for p in ac:
                    if p.get("lat") is not None and p.get("lon") is not None:
                        cs = str(p.get("flight", "") or p.get("r", "")).strip() or str(p.get("hex", "N/A")).upper()
                        icao24 = str(p.get("hex", "N/A")).upper()
                        type_code = str(p.get("t", "") or "")
                        cat_aereo = determina_categoria(icao24, cs, type_code)
                        p_lat, p_lon = float(p["lat"]), float(p["lon"])
                        hdg = round(float(p.get("track", 0) or 0))
                        alt_ft = round(float(p.get("alt_baro", 0) if isinstance(p.get("alt_baro"), (int, float)) else 0))
                        vel_kn = round(float(p.get("gs", 0) or 0))
                        squawk = str(p.get("squawk", "----") or "----")

                        rad = math.radians(hdg)
                        dep_lat = p_lat - 1.8 * math.cos(rad)
                        dep_lon = p_lon - 1.8 * math.sin(rad)

                        voli.append({
                            "icao24": icao24, "callsign": cs, "lat": p_lat, "lon": p_lon,
                            "heading": hdg, "alt_ft": alt_ft, "speed_kn": vel_kn, "squawk": squawk,
                            "categoria": cat_aereo, "type_code": type_code if type_code else "ND",
                            "dep_lat": dep_lat, "dep_lon": dep_lon
                        })
                return jsonify({"status": "Fallback Open Source", "credits": "Illimitati", "count": len(voli), "data": voli})
        except Exception:
            continue

    return jsonify({"status": "Zona senza stazioni", "credits": "0", "count": 0, "data": []})

@app.route('/api/lanci_spaziali', methods=['GET'])
def fetch_lanci_spaziali_api():
    try:
        url = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=10"
        res = requests.get(url, timeout=6)
        if res.status_code == 200:
            data = res.json()
            lanci = []
            for item in data.get("results", []):
                pad = item.get("pad", {})
                if pad.get("latitude") and pad.get("longitude"):
                    lanci.append({
                        "id": item.get("id"),
                        "nome": item.get("name"),
                        "stato": item.get("status", {}).get("name", "Programmato"),
                        "pad": pad.get("name"),
                        "location": pad.get("location", {}).get("name"),
                        "lat": float(pad.get("latitude")),
                        "lon": float(pad.get("longitude")),
                        "window_start": item.get("window_start")
                    })
            return jsonify(lanci)
    except Exception as e:
        print("Errore lanci:", e)
    return jsonify([])

@app.route('/api/sync_terremoti', methods=['POST'])
def sync_terremoti_db_api():
    data_in = request.json or {}
    items = data_in.get("items", [])
    user_lat = float(data_in.get("user_lat", COORDINATE_CASA["lat"]))
    user_lon = float(data_in.get("user_lon", COORDINATE_CASA["lon"]))

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        nuovi_eventi_trovati = []
        ora_corrente = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for item in items:
            c.execute("SELECT id FROM terremoti WHERE id = ?", (item["id"],))
            exists = c.fetchone()
            if not exists:
                luogo_raw = str(item["luogo"]).replace("EMSC RTS", "").replace("EMSC", "").strip()
                if not luogo_raw or luogo_raw.startswith("0x"):
                    luogo_raw = f"Zona Sismica (Coords {round(item['lat'],2)}°, {round(item['lon'],2)}°)"

                c.execute("""
                    INSERT INTO terremoti (id, magnitudo, luogo, lat, lon, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (item["id"], item["mag"], luogo_raw, item["lat"], item["lon"], ora_corrente))
                dist = calcola_distanza_km(user_lat, user_lon, item["lat"], item["lon"])

                continente = "Europa" if ("Italy" in luogo_raw or "Italia" in luogo_raw or "Greece" in luogo_raw or "EMSC" in str(item["id"])) else "Globale / Mondo"
                stato = "Italia" if ("Italy" in luogo_raw or "Italia" in luogo_raw) else "Internazionale"

                nuovi_eventi_trovati.append({
                    "id": item["id"], "mag": item["mag"], "luogo": luogo_raw,
                    "lat": item["lat"], "lon": item["lon"], "dist": round(dist, 1),
                    "ora": ora_corrente, "continente": continente, "stato": stato,
                    "tipo_evento": "TERREMOTO SISMICO LIVE"
                })
        conn.commit()

        ultimo_logout_ts = get_timestamp_logout()
        c.execute("SELECT id, magnitudo, luogo, lat, lon, timestamp FROM terremoti WHERE timestamp > ? ORDER BY timestamp DESC", (ultimo_logout_ts,))
        rows_dal_logout = c.fetchall()
        c.execute("SELECT id, magnitudo, luogo, lat, lon, timestamp FROM terremoti ORDER BY timestamp DESC LIMIT 60")
        rows_storico_completo = c.fetchall()
        conn.close()

        def formatta_lista(rows):
            res = []
            for r in rows:
                dist = calcola_distanza_km(user_lat, user_lon, r[3], r[4])
                res.append({
                    "id": r[0], "mag": r[1], "luogo": r[2],
                    "lat": r[3], "lon": r[4], "time": r[5], "distanza_km": round(dist, 1)
                })
            return res

        return jsonify({
            "lista_dal_logout": formatta_lista(rows_dal_logout),
            "lista_storico": formatta_lista(rows_storico_completo),
            "nuovi": nuovi_eventi_trovati,
            "ultimo_logout": ultimo_logout_ts
        })
    except Exception as e:
        return jsonify({"lista_dal_logout": [], "lista_storico": [], "nuovi": []})

@app.route('/api/salva_evento_custom', methods=['POST'])
def salva_evento_custom_api():
    data = request.json or {}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO eventi_custom (nome, categoria, lat, lon, colore) VALUES (?, ?, ?, ?, ?)",
                  (data.get("nome","").strip(), data.get("cat","").strip(), float(data.get("lat")), float(data.get("lon")), data.get("colore","#a855f7")))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/eventi_custom', methods=['GET'])
def get_eventi_custom_api():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, nome, categoria, lat, lon, colore, timestamp FROM eventi_custom ORDER BY timestamp DESC")
        rows = c.fetchall()
        conn.close()
        return jsonify([{"id": r[0], "nome": r[1], "categoria": r[2], "lat": r[3], "lon": r[4], "colore": r[5], "time": r[6]} for r in rows])
    except Exception as e:
        return jsonify([])

@app.route('/api/rimuovi_evento_custom', methods=['POST'])
def rimuovi_evento_custom_api():
    data = request.json or {}
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM eventi_custom WHERE id = ?", (data.get("id"),))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/gps', methods=['GET', 'POST'])
def receive_gps_traccar():
    if request.method == 'GET':
        device_id = request.args.get('id')
        lat = request.args.get('lat')
        lon = request.args.get('lon')
    else:
        data = request.get_json(silent=True) or {}
        device_id = data.get('id')
        lat = data.get('lat')
        lon = data.get('lon')

    if device_id and lat and lon:
        try:
            f_lat = float(lat)
            f_lon = float(lon)
            dev_id = str(device_id).strip()

            DISPOSITIVI_DB[dev_id] = {
                "nome": dev_id, "proprietario": "Tracker Mobile",
                "colore": "#f97316", "tipo": "GPS", "attivo": True,
                "lat": f_lat, "lon": f_lon, "stato": "Live Cloud"
            }
            salva_dispositivi(DISPOSITIVI_DB)

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO storico_gps (device_id, lat, lon) VALUES (?, ?, ?)", (dev_id, f_lat, f_lon))
            conn.commit()
            conn.close()
            return "OK", 200
        except Exception as e:
            return f"Error: {e}", 500

    return "RECEIVER ACTIVE", 200

@app.route('/api/posizioni', methods=['GET'])
def get_posizioni():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT device_id, lat, lon, MAX(timestamp) FROM storico_gps GROUP BY device_id")
        rows = c.fetchall()
        conn.close()
        return jsonify({r[0]: {"lat": r[1], "lon": r[2], "last_update": r[3]} for r in rows}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==========================================
# TEMPLATE PRINCIPALE WEB COMPLETO
# ==========================================
HTML_UI_COMPLETO = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <title>RADICALIUM - Tactical Multi-Hazard Control Center</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        body, html { width: 100%; height: 100%; overflow: hidden; background: #06101e; color: #fff; }
        #app-container { display: flex; width: 100vw; height: 100vh; flex-direction: column; position: relative; }

        #topbar { height: 48px; background: #0f172a; border-bottom: 1px solid #1e293b; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; z-index: 1100; }
        .brand { font-size: 15px; font-weight: bold; color: #00f2fe; letter-spacing: 1.5px; }
        .nav-buttons { display: flex; gap: 8px; }
        .btn-nav { background: #1e293b; color: white; border: 1px solid #334155; padding: 6px 12px; border-radius: 6px; font-size: 11px; font-weight: bold; cursor: pointer; transition: 0.2s; }
        .btn-nav:hover { background: #0284c7; border-color: #0284c7; }
        .btn-scan { background: #8b5cf6; border-color: #7c3aed; }
        .btn-scan:hover { background: #6d28d9; }
        .btn-custom-evt { background: #a855f7; border-color: #9333ea; }
        .btn-custom-evt:hover { background: #7e22ce; }

        #main-body { display: flex; flex: 1; height: calc(100vh - 48px); position: relative; }
        #map { flex: 1; height: 100%; background: #06101e; }
        #sidebar { width: 380px; background: #0f172a; border-left: 1px solid #1e293b; display: flex; flex-direction: column; padding: 14px; z-index: 1000; box-shadow: -5px 0 15px rgba(0,0,0,0.5); gap: 12px; overflow-y: auto; }

        #flightCardPanel {
            position: absolute; top: 15px; left: 15px; width: 320px; background: rgba(15, 23, 42, 0.96);
            border: 1.5px solid #00f2fe; border-radius: 10px; padding: 14px; z-index: 2500;
            box-shadow: 0 0 25px rgba(0,242,254,0.4); backdrop-filter: blur(6px); display: none;
            max-height: 92vh; overflow-y: auto;
        }
        .flight-card-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 8px; margin-bottom: 10px; }
        .flight-callsign { font-size: 18px; font-weight: bold; color: #00f2fe; }
        
        #planeImageContainer {
            width: 100%; height: 140px; background: #1e293b; border-radius: 6px;
            overflow: hidden; margin-bottom: 10px; border: 1px solid #334155;
            display: flex; align-items: center; justify-content: center; position: relative;
        }
        #planeImage { width: 100%; height: 100%; object-fit: cover; display: none; }
        #planeImagePlaceholder { font-size: 11px; color: #94a3b8; font-weight: bold; }

        .route-box { background: #1e293b; border: 1px solid #334155; padding: 8px; border-radius: 6px; text-align: center; margin-bottom: 10px; }
        .route-title { font-size: 10px; color: #94a3b8; font-weight: bold; }
        .route-val { font-size: 13px; color: #f59e0b; font-weight: bold; margin-top: 2px; }

        .flight-data-row { display: flex; justify-content: space-between; font-size: 11px; margin-bottom: 6px; border-bottom: 1px solid #1e293b; padding-bottom: 4px; }
        .flight-label { color: #94a3b8; font-weight: bold; }
        .flight-val { color: #fff; font-weight: bold; }

        .sidebar-accordion { background: #06101e; border: 1px solid #1e293b; border-radius: 6px; overflow: hidden; margin-bottom: 8px; }
        .sidebar-accordion-header { padding: 12px; background: #1e293b; cursor: pointer; display: flex; align-items: center; justify-content: space-between; font-weight: bold; font-size: 12px; color: #00f2fe; user-select: none; border-bottom: 1px solid #334155; }
        .sidebar-accordion-header:hover { background: #293548; }
        .sidebar-accordion-body { padding: 8px; max-height: 45vh; overflow-y: auto; }

        .filter-btn-group { display: flex; gap: 4px; margin-bottom: 8px; flex-wrap: wrap; }
        .btn-filter { flex: 1; min-width: 60px; padding: 6px; background: #0f172a; border: 1px solid #334155; color: #94a3b8; font-size: 10px; font-weight: bold; border-radius: 4px; cursor: pointer; text-align: center; }
        .btn-filter.active { background: #0284c7; color: white; border-color: #00f2fe; box-shadow: 0 0 8px #0284c7; }

        .group-container { margin-bottom: 8px; background: #1e293b; border-radius: 6px; overflow: hidden; border: 1px solid #334155; }
        .group-header { padding: 8px 10px; background: #0f172a; cursor: pointer; display: flex; align-items: center; justify-content: space-between; font-weight: bold; font-size: 12px; color: #00f2fe; user-select: none; }
        .group-header:hover { background: #1c2d42; }

        .device-card, .event-card { background: #0f172a; border-radius: 6px; margin-bottom: 6px; border-left: 4px solid #00f2fe; overflow: hidden; padding: 8px; }
        .event-header { display: flex; justify-content: space-between; font-weight: bold; font-size: 11px; margin-bottom: 4px; }
        .event-mag { padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; background: #f59e0b; color: black; }
        .event-place { font-size: 11px; color: #94a3b8; }
        .btn-order-online { display: block; width: 100%; margin-top: 6px; padding: 6px; background: #f59e0b; color: #000; font-weight: bold; font-size: 11px; text-align: center; border-radius: 4px; border: none; cursor: pointer; text-decoration: none; }

        .leaflet-popup-content-wrapper { background: #0f172a !important; color: #fff !important; border: 1px solid #00f2fe; border-radius: 8px; font-size: 12px; }
        .leaflet-popup-tip { background: #0f172a !important; }
    </style>
</head>
<body>
    <div id="app-container">
        <div id="topbar">
            <div class="brand">RADICALIUM // TACTICAL CONTROL CENTER CLOUD</div>
            <div class="nav-buttons">
                <button class="btn-nav" style="background:#0284c7;" onclick="volaACasa()">🏠 CASA</button>
                <button class="btn-nav btn-scan" onclick="forzaRilevamentoEventiConAnimazione()">🔄 RILEVA EVENTI</button>
                <button class="btn-nav" style="background:#0284c7;" onclick="toggleRighelloTattico()">📏 RIGHELLO</button>
            </div>
        </div>

        <div id="main-body">
            <div id="map"></div>

            <div id="flightCardPanel">
                <div class="flight-card-header">
                    <div>
                        <span class="flight-callsign" id="fcCallsign">-</span>
                        <span style="font-size:11px; color:#94a3b8; margin-left:6px;" id="fcIcao">-</span>
                    </div>
                    <button class="btn-nav" style="padding:2px 6px; font-size:10px; background:#ef4444;" onclick="chiudiSchedaVolo()">✖</button>
                </div>

                <div id="planeImageContainer">
                    <span id="planeImagePlaceholder">📷 Caricamento foto...</span>
                    <img id="planeImage" src="" alt="Foto Aereo" />
                </div>

                <div class="route-box">
                    <div class="route-title">TRATTA DI VOLO RILEVATA</div>
                    <div class="route-val" id="fcTratta">🔍 Ricerca origine/destinazione...</div>
                </div>
                
                <div class="flight-data-row"><span class="flight-label">CALLSIGN / VOLO:</span><span class="flight-val" id="fcCallsignVal" style="color:#00f2fe;">-</span></div>
                <div class="flight-data-row"><span class="flight-label">HEX TRANSPONDER:</span><span class="flight-val" id="fcIcaoVal" style="color:#f59e0b;">-</span></div>
                <div class="flight-data-row"><span class="flight-label">TIPO / ICAO:</span><span class="flight-val" id="fcType">-</span></div>
                <div class="flight-data-row"><span class="flight-label">CATEGORIA ASSETTO:</span><span class="flight-val" id="fcCategoria">-</span></div>
                <div class="flight-data-row"><span class="flight-label">ALTITUDINE CROCIERA:</span><span class="flight-val" id="fcAlt">-</span></div>
                <div class="flight-data-row"><span class="flight-label">VELOCITÀ AL SUOLO:</span><span class="flight-val" id="fcSpeed">-</span></div>
                <div class="flight-data-row"><span class="flight-label">HEADING / PRUA:</span><span class="flight-val" id="fcHeading">-</span></div>
                <div class="flight-data-row"><span class="flight-label">SQUAWK CODE:</span><span class="flight-val" id="fcSquawk" style="color:#a855f7;">-</span></div>
            </div>

            <div id="sidebar">
                <div class="sidebar-accordion">
                    <div class="sidebar-accordion-header" onclick="toggleMainSection('secDevice')">
                        <span>📍 LOCALIZZA DEVICE</span><span>▼</span>
                    </div>
                    <div class="sidebar-accordion-body" id="secDevice">
                        <div id="deviceList"></div>
                    </div>
                </div>

                <div class="sidebar-accordion" style="flex:1;">
                    <div class="sidebar-accordion-header" onclick="toggleMainSection('secLogList')">
                        <span>⚡ ULTIMI EVENTI RILEVATI (LIVE LOG)</span><span>▼</span>
                    </div>
                    <div class="sidebar-accordion-body" id="secLogList">
                        <div class="group-container">
                            <div class="group-header"><span>✈️ VOLI & AEREI LIVE</span></div>
                            <div class="group-content" style="display:block;" id="listaVoliContainer"></div>
                        </div>
                        <div class="group-container">
                            <div class="group-header"><span>🍔 MCDONALD'S REGISTRATI</span></div>
                            <div class="group-content" style="display:block;" id="listaMcDContainer"></div>
                        </div>
                        <div class="group-container">
                            <div class="group-header"><span>🚢 TRACCIAMENTO NAVALE LIVE</span></div>
                            <div class="group-content" style="display:block;" id="listaNaviContainer"></div>
                        </div>
                        <div class="group-container">
                            <div class="group-header"><span>🚆 TRASPORTI TPL CAMPANIA</span></div>
                            <div class="group-content" style="display:block;" id="listaTransitContainer"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let map, layerPlanes, layerShips, layerMcD, transitGroup;
        const casaLat = 40.8518, casaLon = 14.2681;

        document.addEventListener("DOMContentLoaded", () => {
            map = L.map('map').setView([41.9, 12.5], 6);

            const mapStreet = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19, attribution: 'Esri' }).addTo(map);
            const mapSatellitare = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 18, attribution: 'Esri, Maxar' });
            const mapDark = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', { maxZoom: 16, attribution: 'Esri' });

            layerPlanes = L.layerGroup().addTo(map);
            layerShips = L.layerGroup().addTo(map);
            layerMcD = L.layerGroup().addTo(map);
            transitGroup = L.layerGroup().addTo(map);

            const baseMaps = { "Stradale": mapStreet, "Satellitare": mapSatellitare, "Tattica Scura": mapDark };
            const overlayMaps = { "✈️ Voli Live": layerPlanes, "🚢 Navi AIS": layerShips, "🍔 McDonald's": layerMcD, "🚆 Trasporti Campania": transitGroup };

            L.control.layers(baseMaps, overlayMaps, { position: 'topleft', collapsed: false }).addTo(map);
            L.circle([casaLat, casaLon], { color: 'red', fillColor: '#ef4444', fillOpacity: 0.1, radius: 5000 }).addTo(map);

            forzaRilevamentoEventiConAnimazione();
            setInterval(forzaRilevamentoEventiConAnimazione, 10000);
        });

        function volaACasa() { map.flyTo([casaLat, casaLon], 12); }
        function toggleMainSection(id) {
            const el = document.getElementById(id);
            el.style.display = el.style.display === "none" ? "block" : "none";
        }

        function forzaRilevamentoEventiConAnimazione() {
            caricaVoli();
            caricaNavi();
            caricaMcD();
            caricaTrasporti();
            caricaDispositiviGPS();
        }

        function caricaDispositiviGPS() {
            fetch('/api/posizioni')
                .then(r => r.json())
                .then(data => {
                    const container = document.getElementById("deviceList");
                    container.innerHTML = "";
                    for (var id in data) {
                        var dev = data[id];
                        L.marker([dev.lat, dev.lon]).addTo(map).bindPopup("📱 <b>" + id + "</b>");
                        container.innerHTML += `<div class="device-card">📱 <b>${id}</b><br>Lat: ${dev.lat}, Lon: ${dev.lon}</div>`;
                    }
                });
        }

        function caricaVoli() {
            const bounds = map.getBounds();
            fetch(`/api/voli_hybrid?lamin=${bounds.getSouth()}&lomin=${bounds.getWest()}&lamax=${bounds.getNorth()}&lomax=${bounds.getEast()}&lat=${map.getCenter().lat}&lon=${map.getCenter().lng}&zoom=${map.getZoom()}`)
                .then(r => r.json())
                .then(res => {
                    layerPlanes.clearLayers();
                    const container = document.getElementById("listaVoliContainer");
                    container.innerHTML = "";
                    (res.data || []).forEach(p => {
                        const m = L.marker([p.lat, p.lon]).addTo(layerPlanes).bindPopup(`✈️ <b>${p.callsign}</b><br>Alt: ${p.alt_ft}ft`);
                        container.innerHTML += `<div class="event-card">✈️ <b>${p.callsign}</b> (${p.categoria})<br>Quota: ${p.alt_ft} ft</div>`;
                    });
                });
        }

        function caricaNavi() {
            fetch(`/api/navi_live?lat=${map.getCenter().lat}&lon=${map.getCenter().lng}`)
                .then(r => r.json())
                .then(data => {
                    layerShips.clearLayers();
                    const container = document.getElementById("listaNaviContainer");
                    container.innerHTML = "";
                    data.forEach(s => {
                        L.marker([s.lat, s.lon]).addTo(layerShips).bindPopup(`🚢 <b>${s.nome}</b>`);
                        container.innerHTML += `<div class="event-card">🚢 <b>${s.nome}</b><br>Velocità: ${s.velocita} kn</div>`;
                    });
                });
        }

        function caricaMcD() {
            const bounds = map.getBounds();
            fetch(`/api/mcdonalds?lamin=${bounds.getSouth()}&lomin=${bounds.getWest()}&lamax=${bounds.getNorth()}&lomax=${bounds.getEast()}`)
                .then(r => r.json())
                .then(res => {
                    layerMcD.clearLayers();
                    const container = document.getElementById("listaMcDContainer");
                    container.innerHTML = "";
                    (res.data || []).forEach(m => {
                        L.marker([m.lat, m.lon]).addTo(layerMcD).bindPopup(`🍔 <b>McDonald's ${m.citta}</b><br>${m.via}`);
                        container.innerHTML += `<div class="event-card">🍔 <b>McDonald's ${m.citta}</b><br>${m.via}</div>`;
                    });
                });
        }

        function caricaTrasporti() {
            fetch('/api/trasporti_campania')
                .then(r => r.json())
                .then(data => {
                    transitGroup.clearLayers();
                    const container = document.getElementById("listaTransitContainer");
                    container.innerHTML = "";
                    data.forEach(t => {
                        L.marker([t.lat, t.lon]).addTo(transitGroup).bindPopup(`🚆 <b>${t.nome}</b><br>${t.tratta}`);
                        container.innerHTML += `<div class="event-card">🚆 <b>${t.nome}</b><br>${t.tratta}</div>`;
                    });
                });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_UI_COMPLETO)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
