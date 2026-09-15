import os
import json
import sqlite3
import math
import requests
import threading
import time
import random
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import webview

COORDINATE_CASA = {"lat": 40.8518, "lon": 14.2681}
RAGGIO_SICUREZZA_KM = 5.0
FILE_DISPOSITIVI = "dispositivi.json"
DB_PATH = "eventi_tattici.db"

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

class MultiDeviceHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        device_id = query.get("id", [None])[0]
        lat = query.get("lat", [None])[0]
        lon = query.get("lon", [None])[0]

        if device_id and lat and lon:
            if device_id in DISPOSITIVI_DB:
                f_lat = float(lat)
                f_lon = float(lon)
                DISPOSITIVI_DB[device_id]["lat"] = f_lat
                DISPOSITIVI_DB[device_id]["lon"] = f_lon
                salva_dispositivi(DISPOSITIVI_DB)

                try:
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("INSERT INTO storico_gps (device_id, lat, lon) VALUES (?, ?, ?)", (device_id, f_lat, f_lon))
                    conn.commit()
                    conn.close()
                except Exception as db_err:
                    print(f"Errore DB GPS: {db_err}")

                if window_ref:
                    window_ref.evaluate_js("aggiornaMappaFromPython();")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def avvia_server_traccar():
    try:
        server = HTTPServer(("0.0.0.0", 5055), MultiDeviceHTTPHandler)
        server.serve_forever()
    except Exception as e:
        print(f"Errore Server: {e}")

class ApiBridge:
    def get_dispositivi(self):
        return json.dumps(DISPOSITIVI_DB)

    def aggiungi_dispositivo(self, dev_id, prop, nome, colore):
        prop_pulito = prop.strip()
        base_lat = DISPOSITIVI_DB.get("PC_PRINCIPALE", {}).get("lat") or COORDINATE_CASA["lat"]
        base_lon = DISPOSITIVI_DB.get("PC_PRINCIPALE", {}).get("lon") or COORDINATE_CASA["lon"]

        DISPOSITIVI_DB[dev_id] = {
            "nome": nome.strip(), "proprietario": prop_pulito,
            "colore": colore, "tipo": "GPS", "attivo": True,
            "lat": base_lat, "lon": base_lon, "stato": "In casa"
        }
        salva_dispositivi(DISPOSITIVI_DB)
        return json.dumps({"status": "ok"})

    def modifica_dispositivo(self, dev_id, prop, nome, colore):
        if dev_id in DISPOSITIVI_DB:
            DISPOSITIVI_DB[dev_id]["proprietario"] = prop.strip()
            DISPOSITIVI_DB[dev_id]["nome"] = nome.strip()
            DISPOSITIVI_DB[dev_id]["colore"] = colore
            salva_dispositivi(DISPOSITIVI_DB)
            return json.dumps({"status": "ok"})
        return json.dumps({"status": "error", "msg": "Dispositivo non trovato"})

    def rimuovi_dispositivo(self, dev_id):
        if dev_id in DISPOSITIVI_DB:
            del DISPOSITIVI_DB[dev_id]
            salva_dispositivi(DISPOSITIVI_DB)
        return json.dumps({"status": "ok"})

    def toggle_dispositivo(self, dev_id, stato):
        if dev_id in DISPOSITIVI_DB:
            DISPOSITIVI_DB[dev_id]["attivo"] = stato
            salva_dispositivi(DISPOSITIVI_DB)
        return json.dumps({"status": "ok"})

    def get_latest_radar_timestamp(self):
        try:
            res = requests.get("https://api.rainviewer.com/public/weather-maps.json", timeout=5)
            if res.status_code == 200:
                data = res.json()
                radar_past = data.get("radar", {}).get("past", [])
                if radar_past:
                    latest = radar_past[-1].get("time")
                    return json.dumps({"status": "ok", "time": latest, "host": data.get("host")})
        except Exception as e:
            print("Errore timestamp radar:", e)
        return json.dumps({"status": "error"})

    def fetch_mcdonalds(self, lamin, lomin, lamax, lomax):
        """Ricerca avanzata Nominatim con geocodifica di Stato e Regione per McDonald's"""
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

                    f_lat = float(item.get("lat"))
                    f_lon = float(item.get("lon"))

                    locali.append({
                        "id": item.get("place_id"),
                        "nome": "McDonald's",
                        "lat": f_lat,
                        "lon": f_lon,
                        "citta": citta,
                        "regione": regione,
                        "stato": stato,
                        "via": via,
                        "orario": "07:00 - 01:00",
                        "order_link": "https://www.mcdonalds.it/trova-il-ristorante"
                    })
                if locali:
                    return json.dumps({"status": "OK", "count": len(locali), "data": locali})
        except Exception as e:
            print("[McDonalds] Errore Nominatim:", e)

        return json.dumps({"status": "ERROR", "count": 0, "data": []})

    def fetch_navi_live(self, lat_center=41.9, lon_center=12.5):
        try:
            url = f"https://api.adsb.lol/v2/point/{lat_center}/{lon_center}/350"
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
                return json.dumps(navi)
        except Exception as e:
            print("Errore fetch navi:", e)

        return json.dumps([])

    def fetch_trasporti_campania(self):
        trasporti_campania = [
            {"id": "ANM_L1_GARIBALDI", "nome": "ANM Metro L1 - Garibaldi", "rete": "ANM METRO", "modello": "AnsaldoBreda CAF MA 600", "costruttore": "CAF / AnsaldoBreda", "tratta": "Piscinola ➔ Garibaldi", "lat": 40.8525, "lon": 14.2721, "stato": "REGOLARE"},
            {"id": "ANM_L1_TOLEDO", "nome": "ANM Metro L1 - Toledo", "rete": "ANM METRO", "modello": "CAF Inneo Serie 100", "costruttore": "CAF Spagna", "tratta": "Garibaldi ➔ Piscinola", "lat": 40.8428, "lon": 14.2492, "stato": "REGOLARE"},
            {"id": "EAV_CIRCUM_SORRENTO", "nome": "EAV Circumvesuviana Express", "rete": "EAV FERROVIE", "modello": "ETR 211 Metrostar", "costruttore": "AnsaldoBreda / Firema", "tratta": "Napoli P. Nolana ➔ Sorrento", "lat": 40.8522, "lon": 14.2718, "stato": "ATTIVO"},
            {"id": "EAV_CUMANA_MONTESANTO", "nome": "EAV Ferrovia Cumana SEPSA", "rete": "EAV FERROVIE", "modello": "ET 500 Titano", "costruttore": "TFA Titano / Firema", "tratta": "Montesanto ➔ Torregaveta", "lat": 40.8465, "lon": 14.2461, "stato": "ATTIVO"},
            {"id": "ANM_FUNI_CENTRALE", "nome": "ANM Funicolare Centrale", "rete": "ANM FUNICOLARI", "modello": "Carrozza Vetrata Impianto Fisso", "costruttore": "Ceretti & Tanfani", "tratta": "Augusteo ➔ Piazza Fuga", "lat": 40.8398, "lon": 14.2468, "stato": "REGOLARE"},
            {"id": "ANM_BUS_ALIBUS", "nome": "ANM Alibus Navetta Aeroporto", "rete": "ANM BUS", "modello": "Iveco Bus Urbanway 12m Hybrid", "costruttore": "Iveco Bus France", "tratta": "Aeroporto Capodichino ➔ Molo Beverello", "lat": 40.8845, "lon": 14.2862, "stato": "IN TRANSITO"},
            {"id": "RFI_NAPOLI_CALE", "nome": "Trenitalia Regionale Metropolitano", "rete": "TRENITALIA", "modello": "ETR 104 Pop / Rock", "costruttore": "Alstom / Hitachi Rail", "tratta": "Napoli C.le ➔ Salerno / Caserta", "lat": 40.8530, "lon": 14.2735, "stato": "REGOLARE"}
        ]
        return json.dumps(trasporti_campania)

    def fetch_foto_aereo(self, icao24):
        try:
            url = f"https://api.planespotters.net/pub/photos/hex/{icao24.lower()}"
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
            if res.status_code == 200:
                photos = res.json().get("photos", [])
                if photos:
                    return json.dumps({"status": "OK", "image_url": photos[0].get("thumbnail_large", {}).get("src")})
        except Exception as e:
            print("Errore foto:", e)
        return json.dumps({"status": "NOT_FOUND"})

    def fetch_tratta_volo(self, callsign):
        cs = callsign.strip().upper()
        if not cs or cs == "N/A":
            return json.dumps({"status": "NOT_FOUND"})

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
                    return json.dumps({"status": "OK", "tratta": f"{dep} ➔ {arr}"})
        except Exception as e:
            print("Errore recupero tratta:", e)
        return json.dumps({"status": "NOT_FOUND"})

    def fetch_voli_hybrid(self, lamin, lomin, lamax, lomax, lat_center, lon_center, zoom_level):
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

                return json.dumps({"status": "200 OK (OpenSky)", "credits": f"{crediti} / 4000", "count": len(voli), "data": voli})
        except Exception as e:
            print("OpenSky non disponibile, avvio fallback open:", e)

        radius = 500 if zoom_level <= 4 else (350 if zoom_level <= 6 else 200)
        endpoints_fb = [
            f"https://api.adsb.fi/v2/point/{lat_center}/{lon_center}/{radius}",
            f"https://api.adsb.lol/v2/point/{lat_center}/{lon_center}/{radius}"
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

                    return json.dumps({"status": "Fallback Open Source", "credits": "Illimitati", "count": len(voli), "data": voli})
            except Exception:
                continue

        return json.dumps({"status": "Zona senza stazioni", "credits": "0", "count": 0, "data": []})

    def fetch_lanci_spaziali(self):
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
                return json.dumps(lanci)
        except Exception as e:
            print("Errore fetch lanci spaziali:", e)
        return json.dumps([])

    def sync_terremoti_db(self, terremoti_json, user_lat=COORDINATE_CASA["lat"], user_lon=COORDINATE_CASA["lon"]):
        try:
            items = json.loads(terremoti_json)
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

                    continente = "Europa" if ("Italy" in luogo_raw or "Italia" in luogo_raw or "Greece" in luogo_raw or "EMSC" in item["id"]) else "Globale / Mondo"
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

            return json.dumps({
                "lista_dal_logout": formatta_lista(rows_dal_logout),
                "lista_storico": formatta_lista(rows_storico_completo),
                "nuovi": nuovi_eventi_trovati,
                "ultimo_logout": ultimo_logout_ts
            })
        except Exception as e:
            return json.dumps({"lista_dal_logout": [], "lista_storico": [], "nuovi": []})

    def salva_evento_custom(self, nome, cat, lat, lon, colore):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO eventi_custom (nome, categoria, lat, lon, colore) VALUES (?, ?, ?, ?, ?)",
                      (nome.strip(), cat.strip(), float(lat), float(lon), colore))
            conn.commit()
            conn.close()
            return json.dumps({"status": "ok"})
        except Exception as e:
            return json.dumps({"status": "error", "msg": str(e)})

    def get_eventi_custom(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT id, nome, categoria, lat, lon, colore, timestamp FROM eventi_custom ORDER BY timestamp DESC")
            rows = c.fetchall()
            conn.close()
            res = []
            for r in rows:
                res.append({"id": r[0], "nome": r[1], "categoria": r[2], "lat": r[3], "lon": r[4], "colore": r[5], "time": r[6]})
            return json.dumps(res)
        except Exception as e:
            return json.dumps([])

    def rimuovi_evento_custom(self, evt_id):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM eventi_custom WHERE id = ?", (evt_id,))
            conn.commit()
            conn.close()
            return json.dumps({"status": "ok"})
        except Exception as e:
            return json.dumps({"status": "error", "msg": str(e)})

    def get_storico_rotta_device(self, dev_id):
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT lat, lon, timestamp FROM storico_gps WHERE device_id = ? ORDER BY id ASC LIMIT 100", (dev_id,))
            rows = c.fetchall()
            conn.close()
            res = []
            for r in rows:
                res.append({"lat": r[0], "lon": r[1], "time": r[2]})
            return json.dumps(res)
        except Exception as e:
            return json.dumps([])

    def chiudi_app(self):
        salva_timestamp_logout()
        if window_ref:
            window_ref.destroy()

# ---------------------------------------------------------
# INTERFACCIA WEB LEAFLET - CONTROL CENTER
# ---------------------------------------------------------
HTML_UI = """
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
        .btn-close { background: #ef4444; border-color: #dc2626; }
        .btn-close:hover { background: #b91c1c; }

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

        .btn-toggle-all-layers {
            width: 100%; padding: 6px; background: #1e293b; color: #00f2fe; 
            border: 1px solid #00f2fe; border-radius: 4px; font-size: 10px; 
            font-weight: bold; cursor: pointer; margin-bottom: 8px; text-align: center;
        }
        .btn-toggle-all-layers:hover { background: #0284c7; color: white; }

        .group-container { margin-bottom: 8px; background: #1e293b; border-radius: 6px; overflow: hidden; border: 1px solid #334155; }
        .group-header { padding: 8px 10px; background: #0f172a; cursor: pointer; display: flex; align-items: center; justify-content: space-between; font-weight: bold; font-size: 12px; color: #00f2fe; user-select: none; }
        .group-header:hover { background: #1c2d42; }
        .group-arrow { transition: transform 0.2s ease; font-size: 11px; }
        .group-arrow.open { transform: rotate(180deg); }
        .group-content { display: none; padding: 6px; }
        .group-content.open { display: block; }

        .device-card { background: #0f172a; border-radius: 6px; margin-bottom: 6px; border-left: 4px solid #00f2fe; overflow: hidden; }
        .device-main { padding: 8px; display: flex; align-items: center; justify-content: space-between; }
        .device-info { display: flex; align-items: center; gap: 8px; cursor: pointer; flex: 1; }
        .device-name { font-size: 12px; font-weight: bold; }

        .device-actions { background: #1e293b; padding: 5px 8px; display: flex; gap: 4px; border-top: 1px solid #334155; }
        .btn-action { background: #334155; color: white; border: none; padding: 6px; border-radius: 4px; font-size: 10px; font-weight: bold; cursor: pointer; flex: 1; text-align: center; }
        .btn-action:hover { background: #0284c7; }

        .event-card { background: #0f172a; border-radius: 5px; padding: 8px; margin-bottom: 6px; border-left: 4px solid #f59e0b; cursor: pointer; transition: 0.2s; }
        .event-card:hover { background: #1e293b; }
        .event-card.strong { border-left-color: #ef4444; }
        .event-card.custom { border-left-color: #a855f7; }
        .event-card.space { border-left-color: #3b82f6; }
        .event-card.transit { border-left-color: #10b981; }
        .event-header { display: flex; justify-content: space-between; font-weight: bold; font-size: 11px; margin-bottom: 4px; }
        .event-mag { padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; background: #f59e0b; color: black; }
        .event-mag.red { background: #ef4444; color: white; }
        .event-mag.purple { background: #a855f7; color: white; }
        .event-mag.blue { background: #3b82f6; color: white; }
        .event-mag.green { background: #10b981; color: white; }
        .event-place { font-size: 11px; color: #94a3b8; }

        .btn-more-events { background: #334155; color: #00f2fe; border: 1px dashed #00f2fe; width: 100%; padding: 8px; border-radius: 5px; font-size: 11px; font-weight: bold; cursor: pointer; margin-top: 6px; }
        .btn-more-events:hover { background: #0284c7; color: white; }

        #notificationContainer { position: fixed; top: 60px; right: 400px; z-index: 5000; display: flex; flex-direction: column; gap: 8px; pointer-events: none; }
        .toast-notification { background: #0f172a; border: 1.5px solid #ef4444; color: white; padding: 14px; border-radius: 8px; box-shadow: 0 0 25px rgba(239, 68, 68, 0.7); font-size: 12px; pointer-events: auto; cursor: pointer; animation: slideIn 0.3s ease-out; transition: 0.2s; width: 340px; }
        .toast-notification:hover { background: #1e293b; border-color: #00f2fe; box-shadow: 0 0 30px rgba(0, 242, 254, 0.8); }
        .toast-row { display: flex; margin-bottom: 3px; font-size: 11px; }
        .toast-label { width: 110px; color: #94a3b8; font-weight: bold; }
        .toast-val { flex: 1; color: #fff; font-weight: bold; }
        @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

        .radar-pulse-ring {
            border: 3px solid #22c55e;
            border-radius: 50%;
            box-shadow: 0 0 25px #22c55e, inset 0 0 25px #22c55e;
            animation: tacticalScanPulse 2.5s ease-out infinite;
        }
        @keyframes tacticalScanPulse {
            0% { transform: scale(0.1); opacity: 1; }
            80% { transform: scale(1.2); opacity: 0.4; }
            100% { transform: scale(1.4); opacity: 0; }
        }

        .plane-wrapper { display: flex; justify-content: center; align-items: center; cursor: pointer; }
        .plane-svg { width: 22px; height: 22px; fill: #00f2fe; filter: drop-shadow(0 0 3px #000); transition: 0.2s; }
        .plane-svg.mil { fill: #ef4444 !important; }
        .plane-svg.cargo { fill: #a855f7 !important; }
        .plane-svg.priv { fill: #38bdf8 !important; }
        .plane-svg.eli { fill: #22c55e !important; }
        .plane-wrapper:hover .plane-svg { fill: #f59e0b !important; transform: scale(1.4) !important; }

        .btn-order-online {
            display: block; width: 100%; margin-top: 6px; padding: 6px;
            background: #f59e0b; color: #000; font-weight: bold; font-size: 11px;
            text-align: center; border-radius: 4px; border: none; cursor: pointer;
            text-decoration: none; transition: 0.2s;
        }
        .btn-order-online:hover { background: #d97706; color: #fff; }

        .leaflet-tooltip.custom-tooltip {
            background: rgba(15, 23, 42, 0.95) !important;
            border: 1px solid #00f2fe !important;
            color: #fff !important; font-size: 11px !important;
            padding: 5px 8px !important; border-radius: 6px !important;
            box-shadow: 0 0 10px rgba(0,242,254,0.4) !important;
        }

        .modal { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.75); z-index: 2000; align-items: center; justify-content: center; backdrop-filter: blur(4px); }
        #modalAdd { z-index: 3000; }
        #modalCustomEvent { z-index: 3500; }
        .modal-content { background: #0f172a; width: 500px; padding: 26px; border-radius: 10px; border: 1px solid #00f2fe; box-shadow: 0 10px 30px rgba(0,0,0,0.6); max-height: 90vh; overflow-y: auto; }
        .modal-title { font-size: 16px; font-weight: bold; color: #00f2fe; margin-bottom: 16px; text-align: center; }

        #modalMedia { z-index: 4000; }
        .modal-media-content { background: #06101e; width: 85vw; height: 85vh; border-radius: 12px; border: 1.5px solid #00f2fe; box-shadow: 0 0 35px rgba(0, 242, 254, 0.3); display: flex; flex-direction: column; overflow: hidden; }
        .modal-media-header { height: 44px; background: #0f172a; border-bottom: 1px solid #1e293b; display: flex; align-items: center; justify-content: space-between; padding: 0 16px; font-weight: bold; font-size: 13px; color: #00f2fe; }
        .modal-media-body { flex: 1; width: 100%; height: calc(100% - 44px); background: #000; }
        .modal-media-body iframe { width: 100%; height: 100%; border: none; }

        .form-group { margin-bottom: 14px; }
        .form-group label { display: block; font-size: 12px; font-weight: bold; margin-bottom: 6px; color: #94a3b8; }
        .form-group input, .form-group select { width: 100%; padding: 10px; background: #1e293b; border: 1px solid #334155; color: white; border-radius: 6px; font-size: 13px; }
        .modal-buttons { display: flex; gap: 12px; margin-top: 16px; }

        .btn-success { background: #16a34a; color: white; border: none; padding: 11px; border-radius: 6px; font-weight: bold; cursor: pointer; flex: 1; font-size: 13px; }
        .btn-danger { background: #ef4444; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; cursor: pointer; font-size: 11px; }
        .btn-edit { background: #f59e0b; color: black; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; cursor: pointer; font-size: 11px; margin-right: 6px; }
        .btn-cancel { background: #475569; color: white; border: none; padding: 11px; border-radius: 6px; font-weight: bold; cursor: pointer; flex: 1; font-size: 13px; }

        .manage-table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        .manage-table th, .manage-table td { padding: 10px; text-align: left; font-size: 12px; border-bottom: 1px solid #1e293b; }
        .manage-table th { color: #00f2fe; background: #1e293b; }

        .leaflet-popup-content-wrapper { background: #0f172a !important; color: #fff !important; border: 1px solid #00f2fe; border-radius: 8px; font-size: 12px; }
        .leaflet-popup-tip { background: #0f172a !important; }
    </style>
</head>
<body>
    <div id="app-container">
        <div id="topbar">
            <div class="brand">RADICALIUM // TACTICAL CONTROL CENTER</div>
            <div class="nav-buttons">
                <button class="btn-nav" style="background:#0284c7;" onclick="volaACasa()">🏠 CASA</button>
                <button class="btn-nav btn-scan" onclick="forzaRilevamentoEventiConAnimazione()">🔄 RILEVA EVENTI</button>
                <button class="btn-nav btn-custom-evt" onclick="apriModalCustomEvent()">🎪 GESTIONE EVENTI</button>
                <button class="btn-nav" style="background:#0284c7;" onclick="toggleRighelloTattico()">📏 RIGHELLO</button>
                <button class="btn-nav" onclick="apriModalAggiungi()">➕ AGGIUNGI DISPOSITIVO</button>
                <button class="btn-nav" style="background:#0f766e;" onclick="apriPannelloGestione()">⚙️ GESTIONE</button>
                <button class="btn-nav btn-close" onclick="chiudiAppCompleta()">❌ ESCI</button>
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
                    <button class="btn-nav btn-close" style="padding:2px 6px; font-size:10px;" onclick="chiudiSchedaVolo()">✖</button>
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
                <div class="flight-data-row"><span class="flight-label">SQUAWK CODE (RADAR):</span><span class="flight-val" id="fcSquawk" style="color:#a855f7;">-</span></div>
            </div>

            <div id="notificationContainer"></div>

            <div id="sidebar">
                <div class="sidebar-accordion">
                    <div class="sidebar-accordion-header" onclick="toggleMainSection('secDevice', 'arrowDev')">
                        <span>📍 LOCALIZZA DEVICE</span>
                        <span id="arrowDev">▼</span>
                    </div>
                    <div class="sidebar-accordion-body" id="secDevice" style="display:none;">
                        <div class="device-list" id="deviceList"></div>
                    </div>
                </div>

                <div class="sidebar-accordion" style="flex:1; display:flex; flex-direction:column;">
                    <div class="sidebar-accordion-header" onclick="toggleMainSection('secLogList', 'arrowLog')">
                        <span>⚡ ULTIMI EVENTI RILEVATI (LIVE LOG)</span>
                        <span id="arrowLog">▼</span>
                    </div>
                    <div class="sidebar-accordion-body" id="secLogList" style="flex:1; display:none;">

                        <div class="group-container" id="subPnlMeteo">
                            <div class="group-header" onclick="toggleSubSection('secSubMeteo', 'arrowSubMeteo')">
                                <span>🌧️ RADAR PIOGGIA & TEMPORALI</span>
                                <span class="group-arrow" id="arrowSubMeteo">▲</span>
                            </div>
                            <div class="group-content open" id="secSubMeteo" style="display:block;">
                                <div style="font-size:11px; color:#cbd5e1; padding:4px;">
                                    ⚡ <b>Radar Live Attivo:</b> Focolai temporaleschi e precipitazioni attive.
                                </div>
                            </div>
                        </div>

                        <div class="group-container" id="subPnlVoli" style="display:none;">
                            <div class="group-header" onclick="toggleSubSection('secSubVoli', 'arrowSubVoli')">
                                <span id="lblVoliCount">✈️ VOLI & AEREI LIVE (ADS-B) (0)</span>
                                <span class="group-arrow" id="arrowSubVoli">▼</span>
                            </div>
                            <div class="group-content" id="secSubVoli" style="display:none;">
                                <div class="filter-btn-group">
                                    <button class="btn-filter active" id="fltVoliTutti" onclick="impostaFiltroVoli('TUTTI')">🌐 TUTTI</button>
                                    <button class="btn-filter" id="fltVoliLinea" onclick="impostaFiltroVoli('LINEA')">✈️ LINEA</button>
                                    <button class="btn-filter" id="fltVoliEli" onclick="impostaFiltroVoli('ELICOTTERO')">🚁 ELI</button>
                                    <button class="btn-filter" id="fltVoliMil" onclick="impostaFiltroVoli('MILITARE')">🪖 MIL</button>
                                    <button class="btn-filter" id="fltVoliCargo" onclick="impostaFiltroVoli('CARGO')">📦 CARGO</button>
                                    <button class="btn-filter" id="fltVoliPriv" onclick="impostaFiltroVoli('PRIVATO')">🛩️ PRIV</button>
                                </div>
                                <div id="listaVoliContainer"></div>
                            </div>
                        </div>

                        <!-- CATEGORIA MCDONALD'S (CON FILTRI E ORDINAMENTO PER VICINANZA) -->
                        <div class="group-container" id="subPnlMcD">
                            <div class="group-header" onclick="toggleSubSection('secSubMcD', 'arrowSubMcD')">
                                <span id="lblMcDCount">🍔 MCDONALD'S REGISTRATI (0)</span>
                                <span class="group-arrow" id="arrowSubMcD">▲</span>
                            </div>
                            <div class="group-content open" id="secSubMcD" style="display:block;">
                                <div class="filter-btn-group">
                                    <button class="btn-filter active" id="fltMcDTutti" onclick="impostaFiltroMcD('TUTTI')">🌐 TUTTI</button>
                                    <button class="btn-filter" id="fltMcDAperti" onclick="impostaFiltroMcD('APERTI')">🟢 APERTI</button>
                                    <button class="btn-filter" id="fltMcDChiusi" onclick="impostaFiltroMcD('CHIUSI')">🔴 CHIUSI</button>
                                </div>
                                <div id="listaMcDContainer"></div>
                            </div>
                        </div>

                        <div class="group-container" id="subPnlNavi" style="display:none;">
                            <div class="group-header" onclick="toggleSubSection('secSubNavi', 'arrowSubNavi')">
                                <span id="lblNaviCount">🚢 TRACCIAMENTO NAVALE LIVE (AIS) (0)</span>
                                <span class="group-arrow" id="arrowSubNavi">▼</span>
                            </div>
                            <div class="group-content" id="secSubNavi" style="display:none;">
                                <div class="filter-btn-group">
                                    <button class="btn-filter active" id="fltNaviTutti" onclick="impostaFiltroNavi('TUTTE')">TUTTE</button>
                                    <button class="btn-filter" id="fltNaviPass" onclick="impostaFiltroNavi('PASS')">PASSEGGERI</button>
                                    <button class="btn-filter" id="fltNaviCargo" onclick="impostaFiltroNavi('CARGO')">CARGO/TANKER</button>
                                </div>
                                <div id="listaNaviContainer"></div>
                            </div>
                        </div>

                        <div class="group-container" id="subPnlTransit" style="display:none;">
                            <div class="group-header" onclick="toggleSubSection('secSubTransit', 'arrowSubTransit')">
                                <span id="lblTransitCount">🚆 TRASPORTI TPL CAMPANIA (ANM/EAV) (0)</span>
                                <span class="group-arrow" id="arrowSubTransit">▼</span>
                            </div>
                            <div class="group-content" id="secSubTransit" style="display:none;">
                                <div id="listaTransitContainer"></div>
                            </div>
                        </div>

                        <div class="group-container" id="evt-group-TERREMOTI" style="display:none;">
                            <div class="group-header" onclick="toggleEventGroup('TERREMOTI')">
                                <span id="lblQuakes">🌋 TERREMOTI NUOVI DAL LOGOUT (0)</span>
                                <span class="group-arrow" id="evt-arrow-TERREMOTI">▼</span>
                            </div>
                            <div class="group-content" id="evt-content-TERREMOTI" style="display:none;">
                                <div id="listQuakes"></div>
                            </div>
                        </div>

                        <div class="group-container" id="evt-group-VULCANI" style="display:none;">
                            <div class="group-header" onclick="toggleEventGroup('VULCANI')">
                                <span id="lblVolcanoes">🌋 VULCANI & ERUZIONI ATTIVE (0)</span>
                                <span class="group-arrow" id="evt-arrow-VULCANI">▼</span>
                            </div>
                            <div class="group-content" id="evt-content-VULCANI" style="display:none;">
                                <div id="listVolcanoes"></div>
                            </div>
                        </div>

                        <div class="group-container" id="evt-group-CUSTOM" style="display:none;">
                            <div class="group-header" onclick="toggleEventGroup('CUSTOM')">
                                <span id="lblCustomEvts">🎪 SAGRE & EVENTI LOCALI (0)</span>
                                <span class="group-arrow" id="evt-arrow-CUSTOM">▼</span>
                            </div>
                            <div class="group-content" id="evt-content-CUSTOM" style="display:none;">
                                <div id="listCustomEvts"></div>
                            </div>
                        </div>

                        <div class="group-container" id="subPnlSpace" style="display:none;">
                            <div class="group-header" onclick="toggleSubSection('secSubSpace', 'arrowSubSpace')">
                                <span id="lblSpaceLaunches">🚀 LANCI SPAZIALI & ROCKETS (0)</span>
                                <span class="group-arrow" id="arrowSubSpace">▼</span>
                            </div>
                            <div class="group-content" id="secSubSpace" style="display:none;">
                                <div id="listSpaceLaunches"></div>
                            </div>
                        </div>

                        <div class="group-container" id="subPnlFires" style="display:none;">
                            <div class="group-header" onclick="toggleSubSection('secSubFires', 'arrowSubFires')">
                                <span>🔥 INCENDI & ANOMALIE (NASA)</span>
                                <span class="group-arrow" id="arrowSubFires">▼</span>
                            </div>
                            <div class="group-content" id="secSubFires" style="display:none;">
                                <div style="font-size:11px; color:#cbd5e1; padding:4px;">
                                    🔥 <b>Anomalie Termiche NASA:</b> Monitoraggio satellitare focolai e punti caldi.
                                </div>
                            </div>
                        </div>

                        <div class="group-container" id="subPnlThermal" style="display:none;">
                            <div class="group-header" onclick="toggleSubSection('secSubThermal', 'arrowSubThermal')">
                                <span>🌡️ MAPPA TERMICA GEOTERMICA</span>
                                <span class="group-arrow" id="arrowSubThermal">▼</span>
                            </div>
                            <div class="group-content" id="secSubThermal" style="display:none;">
                                <div style="font-size:11px; color:#cbd5e1; padding:4px;">
                                    🌡️ <b>Mappa Superficie Terrestre:</b> Gradienti di temperatura infrarossi attivi.
                                </div>
                            </div>
                        </div>

                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="modal" id="modalCustomEvent">
        <div class="modal-content" style="width: 580px;">
            <div class="modal-title">🎪 GESTIONE EVENTI LOCALI & SAGRE</div>
            <div class="form-group">
                <label>NOME EVENTO / SAGRA:</label>
                <input type="text" id="custEvtNome" placeholder="es. Sagra del Fungo Porcino">
            </div>
            <div class="form-group">
                <label>CATEGORIA:</label>
                <input type="text" id="custEvtCat" placeholder="es. Sagra / Festa Popolare">
            </div>
            <div style="display:flex; gap:10px;">
                <div class="form-group" style="flex:1;">
                    <label>LATITUDINE:</label>
                    <input type="number" step="any" id="custEvtLat" placeholder="es. 40.8518">
                </div>
                <div class="form-group" style="flex:1;">
                    <label>LONGITUDINE:</label>
                    <input type="number" step="any" id="custEvtLon" placeholder="es. 14.2681">
                </div>
            </div>
            <div class="form-group">
                <label>COLORE SEGNALATORE:</label>
                <input type="color" id="custEvtColore" value="#a855f7" style="height:40px; cursor:pointer;">
            </div>
            <button class="btn-success" onclick="salvaNuovoEventoCustom()" style="width:100%; margin-bottom:12px;">➕ SALVA EVENTO NELLA MAPPA</button>

            <div class="modal-title" style="font-size:13px; margin-top:14px;">EVENTI PERSONALIZZATI SALVATI</div>
            <table class="manage-table">
                <thead>
                    <tr>
                        <th>NOME EVENTO</th>
                        <th>CATEGORIA</th>
                        <th>COORDS</th>
                        <th>AZIONI</th>
                    </tr>
                </thead>
                <tbody id="customEvtTableBody"></tbody>
            </table>

            <div class="modal-buttons" style="margin-top:14px;">
                <button class="btn-cancel" onclick="chiudiModalCustomEvent()">CHIUDI</button>
            </div>
        </div>
    </div>

    <div class="modal" id="modalMedia">
        <div class="modal-media-content" id="mediaContainer">
            <div class="modal-media-header">
                <span id="mediaTitle">🌐 VISUALIZZATORE TATTICO 3D</span>
                <div style="display:flex; gap:8px;">
                    <button class="btn-nav" onclick="toggleMediaFullscreen()">⛶ INGRANDISCI</button>
                    <button class="btn-nav btn-close" onclick="chiudiVistaMedia()">✖ CHIUDI</button>
                </div>
            </div>
            <div class="modal-media-body">
                <iframe id="mediaIframe" src="about:blank"></iframe>
            </div>
        </div>
    </div>

    <div class="modal" id="modalAdd">
        <div class="modal-content">
            <div class="modal-title" id="modalAddTitle">AGGIUNGI NUOVO DISPOSITIVO</div>
            <input type="hidden" id="editDevId">
            <div class="form-group">
                <label>ID TRACKER (Traccar Client Code):</label>
                <input type="text" id="addId" placeholder="es. 849201">
            </div>
            <div class="form-group">
                <label>PROPRIETARIO:</label>
                <select id="selectProp" onchange="gestisciCambioProprietario(this.value)"></select>
            </div>
            <div class="form-group" id="groupNewProp" style="display: none;">
                <label>NOME NUOVO PROPRIETARIO:</label>
                <input type="text" id="addPropCustom" placeholder="es. Marco">
            </div>
            <div class="form-group">
                <label>NOME DISPOSITIVO:</label>
                <input type="text" id="addNome" placeholder="es. iPhone di Martina">
            </div>
            <div class="form-group">
                <label>COLORE MARCATORE:</label>
                <input type="color" id="addColore" value="#f97316" style="height:42px; cursor:pointer;">
            </div>
            <div class="modal-buttons">
                <button class="btn-success" onclick="salvaDispositivo()">💾 SALVA DISPOSITIVO</button>
                <button class="btn-cancel" onclick="chiudiModalAdd()">ANNULLA</button>
            </div>
        </div>
    </div>

    <div class="modal" id="modalManage">
        <div class="modal-content" style="width: 650px;">
            <div class="modal-title">⚙️ GESTIONE DISPOSITIVI REGISTRATI</div>
            <table class="manage-table">
                <thead>
                    <tr>
                        <th>ID TRACKER</th>
                        <th>PROPRIETARIO</th>
                        <th>NOME DISPOSITIVO</th>
                        <th>AZIONI</th>
                    </tr>
                </thead>
                <tbody id="manageTableBody"></tbody>
            </table>
            <div class="modal-buttons">
                <button class="btn-cancel" onclick="chiudiPannelloGestione()">CHIUDI PANNELLO</button>
            </div>
        </div>
    </div>

    <script>
        let map, markersGroup, terremotiGroup, vulcaniGroup, customEventsGroup, spaceGroup, transitGroup, layerFiresNasa, layerGeotermico, layerRadarRain, layerPlanes, layerShips, layerMcD, rotteGroup, flightPathPolyline;
        let globalDb = {};
        let openGroupsState = {};
        let openEventGroupsState = { "TERREMOTI": false, "VULCANI": false, "CUSTOM": false };
        let mostraSoloDalLogout = true;
        let allLayersActiveState = true;
        let pulseMarkerRadar = null;
        let moveTimer = null;

        let filtroVoliCorrente = "TUTTI";
        let filtroNaviCorrente = "TUTTE";
        let filtroMcDCorrente = "TUTTI";

        let listaVoliGrezzi = [];
        let listaNaviGrezze = [];
        let shipMarkersMap = {};

        let listaLanciGrezzi = [];
        let listaTransitGrezzi = [];
        
        let dictMcDMemoria = {};

        let righelloAttivo = false;
        let righelloPunti = [];
        let righelloLine = null;

        const casaLat = 40.8518, casaLon = 14.2681, raggioKm = 5.0;

        document.addEventListener("DOMContentLoaded", () => {
            map = L.map('map', { zoomControl: false, maxZoom: 18 }).setView([41.9, 12.5], 6);

            const mapStreet = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19, attribution: 'Esri' });
            const mapSatellitare = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 18, attribution: 'Esri, Maxar' });
            const mapDark = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', { maxZoom: 16, attribution: 'Esri' });

            layerFiresNasa = L.tileLayer('https://firms.modaps.eosdis.nasa.gov/mapserver/wms/fires/{z}/{x}/{y}', { opacity: 0.75, maxZoom: 18, maxNativeZoom: 14 });
            layerGeotermico = L.tileLayer('https://tile.openweathermap.org/map/temp_new/{z}/{x}/{y}.png?appid=b1b15e88fa797225412429c1c50c122a', { opacity: 0.55, maxZoom: 18, maxNativeZoom: 12 });
            layerRadarRain = L.tileLayer('https://tilecache.rainviewer.com/v2/radar/nowcast_0/256/{z}/{x}/{y}/2/1_1.png', { opacity: 0.7, maxZoom: 18, maxNativeZoom: 6 }).addTo(map);

            mapStreet.addTo(map);

            const baseMaps = {
                "🗺️ Stradale Chiara (Esri)": mapStreet,
                "🛰️ Satellitare HD (Esri)": mapSatellitare,
                "🌙 Tattica Scura (Esri)": mapDark
            };

            terremotiGroup = L.layerGroup().addTo(map);
            vulcaniGroup = L.layerGroup().addTo(map);
            customEventsGroup = L.layerGroup().addTo(map);
            spaceGroup = L.layerGroup().addTo(map);
            transitGroup = L.layerGroup().addTo(map);
            rotteGroup = L.layerGroup().addTo(map);
            layerPlanes = L.layerGroup().addTo(map);
            layerShips = L.layerGroup().addTo(map);
            layerMcD = L.layerGroup().addTo(map);

            layerFiresNasa.addTo(map);
            layerGeotermico.addTo(map);

            const overlayMaps = {
                "🌧️ Radar Pioggia & Temporali": layerRadarRain,
                "✈️ Voli & Aerei Live (ADS-B)": layerPlanes,
                "🍔 McDonald's Mondo (Overpass)": layerMcD,
                "🚢 Tracciamento Navale Live (AIS)": layerShips,
                "🚆 Trasporti TPL Campania (ANM/EAV)": transitGroup,
                "🌋 Terremoti Live (USGS/EMSC)": terremotiGroup,
                "🌋 Vulcani & Eruzioni Attive": vulcaniGroup,
                "🎪 Sagre & Eventi Locali": customEventsGroup,
                "🚀 Lanci Spaziali & Rockets": spaceGroup,
                "🔥 Incendi & Anomalie (NASA)": layerFiresNasa,
                "🌡️ Mappa Termica Geotermica": layerGeotermico
            };

            L.control.layers(baseMaps, overlayMaps, { position: 'topleft', collapsed: true }).addTo(map);
            L.control.zoom({ position: 'topleft' }).addTo(map);

            setInterval(() => {
                const ctrlContainer = document.querySelector('.leaflet-control-layers-overlays');
                if (ctrlContainer && !document.getElementById("btnToggleAllOverlay")) {
                    const btnToggleAll = document.createElement("button");
                    btnToggleAll.className = "btn-toggle-all-layers";
                    btnToggleAll.id = "btnToggleAllOverlay";
                    btnToggleAll.innerText = allLayersActiveState ? "🔲 DESELEZIONA TUTTI" : "☑️ SELEZIONA TUTTI";
                    btnToggleAll.onclick = toggleTuttiILayerLegenda;
                    ctrlContainer.insertBefore(btnToggleAll, ctrlContainer.firstChild);
                }
            }, 500);

            map.on('overlayadd', (e) => sincronizzaPannelliDestra(e.name, true));
            map.on('overlayremove', (e) => sincronizzaPannelliDestra(e.name, false));

            map.on('moveend', () => {
                clearTimeout(moveTimer);
                moveTimer = setTimeout(() => {
                    caricaVoliENaviLive();
                    caricaMcDonaldsLive();
                }, 400);
            });

            setTimeout(() => {
                sincronizzaPannelliDestra("🌧️ Radar Pioggia & Temporali", map.hasLayer(layerRadarRain));
                sincronizzaPannelliDestra("✈️ Voli & Aerei Live (ADS-B)", map.hasLayer(layerPlanes));
                sincronizzaPannelliDestra("🍔 McDonald's Mondo (Overpass)", map.hasLayer(layerMcD));
                sincronizzaPannelliDestra("🚢 Tracciamento Navale Live (AIS)", map.hasLayer(layerShips));
                sincronizzaPannelliDestra("🚆 Trasporti TPL Campania (ANM/EAV)", map.hasLayer(transitGroup));
                sincronizzaPannelliDestra("🌋 Terremoti Live (USGS/EMSC)", map.hasLayer(terremotiGroup));
                sincronizzaPannelliDestra("🌋 Vulcani & Eruzioni Attive", map.hasLayer(vulcaniGroup));
                sincronizzaPannelliDestra("🎪 Sagre & Eventi Locali", map.hasLayer(customEventsGroup));
                sincronizzaPannelliDestra("🚀 Lanci Spaziali & Rockets", map.hasLayer(spaceGroup));
                sincronizzaPannelliDestra("🔥 Incendi & Anomalie (NASA)", map.hasLayer(layerFiresNasa));
                sincronizzaPannelliDestra("🌡️ Mappa Termica Geotermica", map.hasLayer(layerGeotermico));
            }, 300);

            let checkApiInterval = setInterval(() => {
                if (window.pywebview && window.pywebview.api) {
                    clearInterval(checkApiInterval);
                    aggiornaTimestampRadarLive();
                    forzaRilevamentoEventiConAnimazione();
                    caricaEventiCustomMappa();
                    caricaVoliENaviLive();
                    caricaMcDonaldsLive();
                    caricaLanciSpazialiLive();
                    caricaTrasportiCampaniaLive();
                }
            }, 200);

            map.on('click', async (e) => {
                if (righelloAttivo) {
                    gestisciClickRighello(e.latlng);
                    return;
                }

                const lat = e.latlng.lat;
                const lon = e.latlng.lng;

                L.popup().setLatLng(e.latlng).setContent("🌐 Rilevamento coordinate in corso...").openOn(map);

                try {
                    const response = await fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lon}&zoom=10`);
                    const data = await response.json();

                    let stato = "Acque Internazionali", regione = "Settore Marittimo", citta = "Coordinate Tattiche";
                    if (data && data.address) {
                        stato = data.address.country || stato;
                        regione = data.address.state || data.address.region || regione;
                        citta = data.address.city || data.address.town || data.address.village || citta;
                    }

                    const safeCitta = citta.replace(/'/g, "\\'");

                    L.popup()
                        .setLatLng(e.latlng)
                        .setContent(`
                            <div style="padding:4px;">
                                <b style="color:#00f2fe; font-size:13px;">🌍 STATO: ${stato.toUpperCase()}</b><br>
                                📍 <b>Regione:</b> ${regione}<br>
                                🏛 <b>Città/Zona:</b> ${citta}<br>
                                🌐 <b>Coords:</b> ${lat.toFixed(4)}°, ${lon.toFixed(4)}°<br>
                                <button class="btn-popup-opt btn-earth-3d" onclick="apriVista3DEarth(${lat}, ${lon}, '${safeCitta}')">🌐 VISTA 3D EARTH</button>
                                <button class="btn-popup-opt btn-nasa-live" onclick="apriLinkEsterno('https://worldview.earthdata.nasa.gov/?v=${lon-1},${lat-1},${lon+1},${lat+1}')">🛰️ SATELLITE NASA (LIVE ESTERNO)</button>
                            </div>
                        `)
                        .openOn(map);
                } catch (err) {
                    L.popup()
                        .setLatLng(e.latlng)
                        .setContent(`
                            📍 <b>Coordinate Tattiche:</b><br>${lat.toFixed(4)}°, ${lon.toFixed(4)}°<br>
                            <button class="btn-popup-opt btn-earth-3d" onclick="apriVista3DEarth(${lat}, ${lon}, 'Settore Tattico')">🌐 VISTA 3D EARTH</button>
                            <button class="btn-popup-opt btn-nasa-live" onclick="apriLinkEsterno('https://worldview.earthdata.nasa.gov/?v=${lon-1},${lat-1},${lon+1},${lat+1}')">🛰️ SATELLITE NASA (LIVE ESTERNO)</button>
                        `)
                        .openOn(map);
                }
            });

            L.circle([casaLat, casaLon], {
                color: '#ef4444', fillColor: '#ef4444', fillOpacity: 0.1, radius: raggioKm * 1000, dashArray: '5, 5'
            }).addTo(map);

            markersGroup = L.layerGroup().addTo(map);

            setInterval(aggiornaMappaFromPython, 1000);
            setInterval(forzaRilevamentoEventiConAnimazione, 60000);
            setInterval(caricaLanciSpazialiLive, 120000);
            setInterval(caricaTrasportiCampaniaLive, 45000);
        });

        async function aggiornaTimestampRadarLive() {
            if (!window.pywebview) return;
            try {
                const resStr = await window.pywebview.api.get_latest_radar_timestamp();
                const res = JSON.parse(resStr);
                if (res.status === "ok" && res.time) {
                    const host = res.host || "https://tilecache.rainviewer.com";
                    const nuovoUrl = `${host}/v2/radar/${res.time}/256/{z}/{x}/{y}/2/1_1.png`;
                    layerRadarRain.setUrl(nuovoUrl);
                }
            } catch(e) { console.log("Errore radar:", e); }
        }

        function toggleTuttiILayerLegenda() {
            allLayersActiveState = !allLayersActiveState;
            const btn = document.getElementById("btnToggleAllOverlay");
            if (btn) btn.innerText = allLayersActiveState ? "🔲 DESELEZIONA TUTTI" : "☑️ SELEZIONA TUTTI";

            const checkboxes = document.querySelectorAll('.leaflet-control-layers-overlays input[type="checkbox"]');
            checkboxes.forEach(chk => {
                if (chk.checked !== allLayersActiveState) {
                    chk.click();
                }
            });
        }

        function riproduciSuonoSonar() {
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();

                osc.type = 'sine';
                osc.frequency.setValueAtTime(1200, ctx.currentTime);
                osc.frequency.exponentialRampToValueAtTime(400, ctx.currentTime + 0.4);

                gain.gain.setValueAtTime(0.3, ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.8);

                osc.connect(gain);
                gain.connect(ctx.destination);

                osc.start(ctx.currentTime);
                osc.stop(ctx.currentTime + 0.8);
            } catch(e) { console.log("Errore audio sonar:", e); }
        }

        function avviaAnimazioneRadarPianeta() {
            riproduciSuonoSonar();

            if (pulseMarkerRadar) map.removeLayer(pulseMarkerRadar);

            const pulseIcon = L.divIcon({
                className: 'custom-radar-pulse',
                html: `<div class="radar-pulse-ring" style="width:320px; height:320px; margin-left:-160px; margin-top:-160px;"></div>`,
                iconSize: [0, 0]
            });

            pulseMarkerRadar = L.marker([casaLat, casaLon], { icon: pulseIcon }).addTo(map);

            setTimeout(() => {
                if (pulseMarkerRadar) {
                    map.removeLayer(pulseMarkerRadar);
                    pulseMarkerRadar = null;
                }
            }, 5000);
        }

        function forzaRilevamentoEventiConAnimazione() {
            avviaAnimazioneRadarPianeta();
            caricaTerremotiLive();
            caricaVulcaniLive();
            caricaEventiCustomMappa();
            caricaLanciSpazialiLive();
            caricaVoliENaviLive();
            caricaMcDonaldsLive();
            caricaTrasportiCampaniaLive();
            aggiornaTimestampRadarLive();
        }

        function mostraNotificaNuovoEvento(titolo, eventoObj, lat, lon) {
            riproduciSuonoSonar();

            const container = document.getElementById("notificationContainer");
            const toast = document.createElement("div");
            toast.className = "toast-notification";

            const distCasa = (eventoObj.dist !== undefined) ? eventoObj.dist : 'N/D';
            const nomeLuogo = eventoObj.luogo || "Sconosciuto";

            toast.innerHTML = `
                <div style="font-weight:bold; font-size:12px; color:#ef4444; border-bottom:1px solid #334155; padding-bottom:4px; margin-bottom:6px;">
                    🚨 ${titolo.toUpperCase()} - TERREMOTO (M${eventoObj.mag})
                </div>
                <div class="toast-row"><span class="toast-label">📍 CITTA / ZONA:</span><span class="toast-val" style="color:#00f2fe;">${nomeLuogo}</span></div>
                <div class="toast-row"><span class="toast-label">🇮🇹 STATO:</span><span class="toast-val">${eventoObj.stato || 'Italia'}</span></div>
                <div class="toast-row"><span class="toast-label">🌐 CONTINENTE:</span><span class="toast-val">${eventoObj.continente || 'Europa'}</span></div>
                <div class="toast-row"><span class="toast-label">🎯 DISTANZA BASE:</span><span class="toast-val" style="color:#22c55e;">${distCasa} KM DA CASA</span></div>
                <div style="font-size:10px; color:#94a3b8; display:flex; justify-content:space-between; align-items:center; border-top:1px dashed #334155; padding-top:6px; margin-top:6px;">
                    <span>🕒 ${eventoObj.ora}</span>
                    <span style="color:#00f2fe; font-weight:bold;">🎯 CLICCA PER LOCALIZZARE</span>
                </div>
            `;

            toast.onclick = () => {
                if (lat && lon) {
                    map.flyTo([lat, lon], 11, { animate: true, duration: 2.0 });
                }
            };

            container.appendChild(toast);

            setTimeout(() => { toast.remove(); }, 9000);
        }

        function sincronizzaPannelliDestra(layerName, visibile) {
            if (layerName.includes("Radar")) {
                const el = document.getElementById("subPnlMeteo");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Voli")) {
                const el = document.getElementById("subPnlVoli");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("McDonald")) {
                const el = document.getElementById("subPnlMcD");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Navale")) {
                const el = document.getElementById("subPnlNavi");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Trasporti") || layerName.includes("TPL")) {
                const el = document.getElementById("subPnlTransit");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Terremoti")) {
                const el = document.getElementById("evt-group-TERREMOTI");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Vulcani")) {
                const el = document.getElementById("evt-group-VULCANI");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Sagre") || layerName.includes("Locali")) {
                const el = document.getElementById("evt-group-CUSTOM");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Lanci") || layerName.includes("Spaziali")) {
                const el = document.getElementById("subPnlSpace");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Incendi")) {
                const el = document.getElementById("subPnlFires");
                if (el) el.style.display = visibile ? "block" : "none";
            } else if (layerName.includes("Termica") || layerName.includes("Geotermica")) {
                const el = document.getElementById("subPnlThermal");
                if (el) el.style.display = visibile ? "block" : "none";
            }
        }

        function toggleMainSection(secId, arrowId) {
            const body = document.getElementById(secId);
            const arrow = document.getElementById(arrowId);
            if (body.style.display === "none") {
                body.style.display = "block"; arrow.innerText = "▲";
            } else {
                body.style.display = "none"; arrow.innerText = "▼";
            }
        }

        function toggleSubSection(secId, arrowId) {
            const body = document.getElementById(secId);
            const arrow = document.getElementById(arrowId);
            if (body.style.display === "none") {
                body.style.display = "block"; arrow.innerText = "▲";
            } else {
                body.style.display = "none"; arrow.innerText = "▼";
            }
        }

        function volaACasa() {
            map.flyTo([casaLat, casaLon], 12, { animate: true, duration: 2.0 });
        }

        // APERTURA INTERATTIVA DI LINK ESTERNI (PER IL TASTO ORDINA ONLINE)
        function apriLinkEsterno(url) {
            window.open(url, '_blank');
        }

        function toggleRighelloTattico() {
            righelloAttivo = !righelloAttivo;
            if (righelloAttivo) {
                alert("📏 MODALITÀ RIGHELLO ATTIVATA: Clicca sulla mappa in due o più punti per misurare la distanza esatta.");
                righelloPunti = [];
            } else {
                if (righelloLine) map.removeLayer(righelloLine);
                righelloPunti = [];
                alert("📏 Righello Tattico Disattivato.");
            }
        }

        function gestisciClickRighello(latlng) {
            righelloPunti.push(latlng);
            if (righelloPunti.length > 1) {
                if (righelloLine) map.removeLayer(righelloLine);
                righelloLine = L.polyline(righelloPunti, { color: '#00f2fe', weight: 4, dashArray: '5, 10' }).addTo(map);

                let distTotaleMetri = 0;
                for (let i = 0; i < righelloPunti.length - 1; i++) {
                    distTotaleMetri += righelloPunti[i].distanceTo(righelloPunti[i+1]);
                }
                const distKm = (distTotaleMetri / 1000).toFixed(2);

                L.popup()
                    .setLatLng(latlng)
                    .setContent(`📏 <b>DISTANZA TRACCIATA:</b><br><b style="color:#00f2fe; font-size:14px;">${distKm} KM</b> (${Math.round(distTotaleMetri)} metri)`)
                    .openOn(map);
            }
        }

        function impostaFiltroVoli(tipo) {
            filtroVoliCorrente = tipo;
            document.querySelectorAll("#subPnlVoli .btn-filter").forEach(b => b.classList.remove("active"));
            
            if (tipo === "TUTTI") document.getElementById("fltVoliTutti").classList.add("active");
            if (tipo === "LINEA") document.getElementById("fltVoliLinea").classList.add("active");
            if (tipo === "ELICOTTERO") document.getElementById("fltVoliEli").classList.add("active");
            if (tipo === "MILITARE") document.getElementById("fltVoliMil").classList.add("active");
            if (tipo === "CARGO") document.getElementById("fltVoliCargo").classList.add("active");
            if (tipo === "PRIVATO") document.getElementById("fltVoliPriv").classList.add("active");

            renderizzaListaVoli();
        }

        function impostaFiltroNavi(tipo) {
            filtroNaviCorrente = tipo;
            document.querySelectorAll("#subPnlNavi .btn-filter").forEach(b => b.classList.remove("active"));
            if (tipo === "TUTTE") document.getElementById("fltNaviTutti").classList.add("active");
            if (tipo === "PASS") document.getElementById("fltNaviPass").classList.add("active");
            if (tipo === "CARGO") document.getElementById("fltNaviCargo").classList.add("active");
            renderizzaListaNavi();
        }

        function impostaFiltroMcD(tipo) {
            filtroMcDCorrente = tipo;
            document.querySelectorAll("#subPnlMcD .btn-filter").forEach(b => b.classList.remove("active"));
            if (tipo === "TUTTI") document.getElementById("fltMcDTutti").classList.add("active");
            if (tipo === "APERTI") document.getElementById("fltMcDAperti").classList.add("active");
            if (tipo === "CHIUSI") document.getElementById("fltMcDChiusi").classList.add("active");
            renderizzaListaMcD();
        }

        // CALCOLO DISTANZA HAVERSINE IN JAVASCRIPT
        function calcolaDistanzaKmJs(lat1, lon1, lat2, lon2) {
            const R = 6371;
            const dLat = (lat2 - lat1) * Math.PI / 180;
            const dLon = (lon2 - lon1) * Math.PI / 180;
            const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                      Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                      Math.sin(dLon/2) * Math.sin(dLon/2);
            const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
            return R * c;
        }

        // VALUTAZIONE STATO ORARI (APERTO / CHIUSO / H24)
        function valutaStatoApertura(orarioStr) {
            if (!orarioStr) return { stato: "APERTO", etichetta: "APERTO" };
            const str = orarioStr.toLowerCase();
            if (str.includes("24/7") || str.includes("24 hours") || str.includes("00:00-24:00")) {
                return { stato: "APERTO", etichetta: "H24" };
            }

            const oraCorrente = new Date().getHours();
            if (oraCorrente >= 7 || oraCorrente < 1) {
                return { stato: "APERTO", etichetta: "APERTO" };
            } else {
                return { stato: "CHIUSO", etichetta: "CHIUSO" };
            }
        }

        async function caricaMcDonaldsLive() {
            if (!window.pywebview) return;
            const bounds = map.getBounds();

            let lamin, lomin, lamax, lomax;

            if (map.getZoom() < 9) {
                const c = map.getCenter();
                lamin = (c.lat - 0.8).toFixed(4);
                lomin = (c.lng - 0.8).toFixed(4);
                lamax = (c.lat + 0.8).toFixed(4);
                lomax = (c.lng + 0.8).toFixed(4);
            } else {
                lamin = Math.max(-85, bounds.getSouth()).toFixed(4);
                lomin = Math.max(-180, bounds.getWest()).toFixed(4);
                lamax = Math.min(85, bounds.getNorth()).toFixed(4);
                lomax = Math.min(180, bounds.getEast()).toFixed(4);
            }

            try {
                const resStr = await window.pywebview.api.fetch_mcdonalds(lamin, lomin, lamax, lomax);
                const res = JSON.parse(resStr);
                const nuovi = res.data || [];

                nuovi.forEach(item => {
                    item.distanza_km = calcolaDistanzaKmJs(casaLat, casaLon, item.lat, item.lon);
                    item.infoApertura = valutaStatoApertura(item.orario);
                    dictMcDMemoria[item.id] = item;
                });

                renderizzaListaMcD();
            } catch(e) { console.log("Errore caricamento McDonald's:", e); }
        }

        function renderizzaListaMcD() {
            layerMcD.clearLayers();
            const container = document.getElementById("listaMcDContainer");
            const lbl = document.getElementById("lblMcDCount");
            if (container) container.innerHTML = "";

            let listaLocali = Object.values(dictMcDMemoria);

            // ORDINAMENTO PER VICINANZA DA CASA
            listaLocali.sort((a, b) => a.distanza_km - b.distanza_km);

            // FILTRAGGIO APERTI / CHIUSI
            if (filtroMcDCorrente === "APERTI") {
                listaLocali = listaLocali.filter(m => m.infoApertura.stato === "APERTO");
            } else if (filtroMcDCorrente === "CHIUSI") {
                listaLocali = listaLocali.filter(m => m.infoApertura.stato === "CHIUSI");
            }

            if (lbl) lbl.innerText = `🍔 MCDONALD'S REGISTRATI (${listaLocali.length})`;

            listaLocali.forEach(mcd => {
                const markerMcD = L.marker([mcd.lat, mcd.lon], {
                    icon: L.divIcon({
                        className: 'mcd-custom',
                        html: `<div style="background:#f59e0b; color:black; font-weight:bold; width:22px; height:22px; border-radius:50%; border:2px solid white; display:flex; align-items:center; justify-content:center; font-size:12px; box-shadow:0 0 10px #f59e0b;">M</div>`,
                        iconSize: [22, 22], iconAnchor: [11, 11]
                    })
                });

                const safeUrl = mcd.order_link.replace(/'/g, "\\'");

                const popupContent = `
                    <b style="color:#f59e0b; font-size:13px;">🌍 ${mcd.citta.toUpperCase()} - ${mcd.regione}, ${mcd.stato}</b><br>
                    📍 Indirizzo: <b>${mcd.via}</b><br>
                    🕒 Orario: <b>${mcd.orario}</b><br>
                    📏 Distanza: <b style="color:#22c55e;">${mcd.distanza_km.toFixed(1)} KM DA TE</b><br>
                    <button class="btn-order-online" onclick="apriLinkEsterno('${safeUrl}')">📲 ORDINA ONLINE / DELIVERY</button>
                `;

                markerMcD.bindPopup(popupContent);
                layerMcD.addLayer(markerMcD);

                if (container) {
                    const card = document.createElement("div");
                    card.className = "event-card";

                    const badgeColor = mcd.infoApertura.etichetta === "CHIUSO" ? "#ef4444" : "#22c55e";

                    card.innerHTML = `
                        <div class="event-header" onclick="map.flyTo([${mcd.lat}, ${mcd.lon}], 15, { animate: true, duration: 1.5 });">
                            <span>🌍 ${mcd.citta.toUpperCase()} - ${mcd.regione}, ${mcd.stato}</span>
                            <span class="event-mag" style="background:${badgeColor}; color:${mcd.infoApertura.etichetta === 'CHIUSO' ? 'white' : 'black'};">${mcd.infoApertura.etichetta}</span>
                        </div>
                        <div class="event-place" onclick="map.flyTo([${mcd.lat}, ${mcd.lon}], 15, { animate: true, duration: 1.5 });">
                            🛣 <b>Indirizzo:</b> ${mcd.via}<br>
                            📏 <b>Distanza:</b> <span style="color:#22c55e; font-weight:bold;">${mcd.distanza_km.toFixed(1)} KM DA TE</span><br>
                            🕒 <b>Orari:</b> ${mcd.orario}
                        </div>
                        <button class="btn-order-online" onclick="apriLinkEsterno('${safeUrl}')">📲 ORDINA ONLINE / DELIVERY</button>
                    `;
                    container.appendChild(card);
                }
            });
        }

        async function caricaVoliENaviLive() {
            if (!window.pywebview) return;
            const bounds = map.getBounds();
            const lamin = Math.max(-85, bounds.getSouth()).toFixed(2);
            const lomin = Math.max(-180, bounds.getWest()).toFixed(2);
            const lamax = Math.min(85, bounds.getNorth()).toFixed(2);
            const lomax = Math.min(180, bounds.getEast()).toFixed(2);

            const center = map.getCenter();
            const latC = center.lat.toFixed(4);
            const lonC = center.lng.toFixed(4);
            const zoom = map.getZoom();

            try {
                const resVoliStr = await window.pywebview.api.fetch_voli_hybrid(lamin, lomin, lamax, lomax, latC, lonC, zoom);
                const resVoli = JSON.parse(resVoliStr);
                listaVoliGrezzi = resVoli.data || [];
                renderizzaListaVoli();

                const resNaviStr = await window.pywebview.api.fetch_navi_live(latC, lonC);
                listaNaviGrezze = JSON.parse(resNaviStr);
                renderizzaListaNavi();
            } catch(e) { console.log("Errore carica voli/navi:", e); }
        }

        function renderizzaListaVoli() {
            layerPlanes.clearLayers();
            const container = document.getElementById("listaVoliContainer");
            const lbl = document.getElementById("lblVoliCount");
            if (container) container.innerHTML = "";

            const voliFiltrati = listaVoliGrezzi.filter(plane => {
                if (filtroVoliCorrente === "TUTTI") return true;
                return plane.categoria === filtroVoliCorrente;
            });

            if (lbl) lbl.innerText = `✈️ VOLI & AEREI LIVE (${voliFiltrati.length})`;

            voliFiltrati.forEach(plane => {
                let catClass = "";
                let svgPath = "M21,16V14L13,9V3.5A1.5,1.5 0 0,0 11.5,2A1.5,1.5 0 0,0 10,3.5V9L2,14V16L10,13.5V19L8,20.5V22L11.5,21L15,22V20.5L13,19V13.5L21,16Z";

                if (plane.categoria === "ELICOTTERO") {
                    catClass = "eli";
                    svgPath = "M12,2A1,1 0 0,0 11,3V7H4A1,1 0 0,0 3,8V10A1,1 0 0,0 4,11H11V14.17L7.5,16.27A1,1 0 0,0 7,17.14V19A1,1 0 0,0 8.24,19.95L12,17.7L15.76,19.95A1,1 0 0,0 17,19V17.14A1,1 0 0,0 16.5,16.27L13,14.17V11H20A1,1 0 0,0 21,10V8A1,1 0 0,0 20,7H13V3A1,1 0 0,0 12,2Z";
                } else if (plane.categoria === "MILITARE") catClass = "mil";
                else if (plane.categoria === "CARGO") catClass = "cargo";
                else if (plane.categoria === "PRIVATO") catClass = "priv";

                const iconHtml = `
                    <div class="plane-wrapper">
                        <svg class="plane-svg ${catClass}" style="transform: rotate(${plane.heading}deg);" viewBox="0 0 24 24">
                            <path d="${svgPath}"/>
                        </svg>
                    </div>
                `;

                const marker = L.marker([plane.lat, plane.lon], {
                    icon: L.divIcon({
                        className: 'plane-custom', html: iconHtml,
                        iconSize: [22, 22], iconAnchor: [11, 11]
                    })
                });

                marker.bindTooltip(`
                    <b style="color:#00f2fe;">${plane.callsign}</b> (${plane.icao24})<br>
                    Tipo: <b>${plane.type_code}</b> | Cat: <b>${plane.categoria}</b><br>
                    Quota: <b>${plane.alt_ft} ft</b> | Vel: <b>${plane.speed_kn} kt</b>
                `, { permanent: false, direction: 'top', offset: [0, -8], className: 'custom-tooltip' });

                marker.on('click', () => apriDettaglioVolo(plane));
                layerPlanes.addLayer(marker);

                if (container) {
                    const card = document.createElement("div");
                    card.className = "event-card";
                    card.onclick = () => apriDettaglioVolo(plane);
                    card.innerHTML = `
                        <div class="event-header">
                            <span style="color:#00f2fe;">🛫 ROTTA LIVE // ${plane.callsign}</span>
                            <span class="event-mag">${plane.alt_ft} ft</span>
                        </div>
                        <div class="event-place">
                            🆔 <b>Callsign:</b> ${plane.callsign} (${plane.type_code})<br>
                            ⚡ <b>Velocità:</b> ${plane.speed_kn} kt | Prua: ${plane.heading}°
                        </div>
                    `;
                    container.appendChild(card);
                }
            });
        }

        async function apriDettaglioVolo(plane) {
            document.getElementById("fcCallsign").innerText = plane.callsign;
            document.getElementById("fcIcao").innerText = "HEX: " + plane.icao24;
            document.getElementById("fcCallsignVal").innerText = plane.callsign;
            document.getElementById("fcIcaoVal").innerText = plane.icao24;
            document.getElementById("fcType").innerText = plane.type_code;
            document.getElementById("fcCategoria").innerText = plane.categoria;
            document.getElementById("fcAlt").innerText = plane.alt_ft + " ft";
            document.getElementById("fcSpeed").innerText = plane.speed_kn + " kt";
            document.getElementById("fcHeading").innerText = plane.heading + "°";
            document.getElementById("fcSquawk").innerText = plane.squawk;

            document.getElementById("fcTratta").innerText = "🔍 Ricerca tratta in corso...";
            document.getElementById("flightCardPanel").style.display = "block";

            try {
                const trattaResStr = await window.pywebview.api.fetch_tratta_volo(plane.callsign);
                const trattaData = JSON.parse(trattaResStr);
                if (trattaData.status === "OK" && trattaData.tratta) {
                    document.getElementById("fcTratta").innerText = trattaData.tratta;
                } else {
                    document.getElementById("fcTratta").innerText = plane.callsign + " (Rotta Vettoriale In Corso)";
                }
            } catch(e) {
                document.getElementById("fcTratta").innerText = plane.callsign + " (Rotta Vettoriale In Corso)";
            }

            const imgEl = document.getElementById("planeImage");
            const placeholderEl = document.getElementById("planeImagePlaceholder");
            imgEl.style.display = "none";
            placeholderEl.style.display = "block";

            try {
                const fotoResStr = await window.pywebview.api.fetch_foto_aereo(plane.icao24);
                const fotoData = JSON.parse(fotoResStr);
                if (fotoData.status === "OK" && fotoData.image_url) {
                    imgEl.src = fotoData.image_url;
                    imgEl.onload = () => { imgEl.style.display = "block"; placeholderEl.style.display = "none"; };
                } else { placeholderEl.innerText = "📷 Foto non disponibile"; }
            } catch(e) { placeholderEl.innerText = "📷 Foto non disponibile"; }

            if (flightPathPolyline) map.removeLayer(flightPathPolyline);
            flightPathPolyline = L.polyline([[plane.dep_lat, plane.dep_lon], [plane.lat, plane.lon]], {
                color: '#00f2fe', weight: 3, dashArray: '5, 5', opacity: 0.9
            }).addTo(map);
        }

        function chiudiSchedaVolo() {
            document.getElementById("flightCardPanel").style.display = "none";
            if (flightPathPolyline) map.removeLayer(flightPathPolyline);
        }

        function renderizzaListaNavi() {
            const container = document.getElementById("listaNaviContainer");
            const lbl = document.getElementById("lblNaviCount");
            if (container) container.innerHTML = "";

            let attiviOra = new Set();
            let conteggio = 0;

            listaNaviGrezze.forEach(ship => {
                const mmsi = ship.mmsi || "SHIP";
                const nome = ship.nome || "NAVE";
                const lat = ship.lat;
                const lon = ship.lon;
                const tipo = ship.tipo;

                let passaFiltro = true;
                if (filtroNaviCorrente === "PASS" && !tipo.includes("PASSEGGERI")) passaFiltro = false;
                if (filtroNaviCorrente === "CARGO" && tipo.includes("PASSEGGERI")) passaFiltro = false;

                if (lat && lon && passaFiltro) {
                    conteggio++;
                    attiviOra.add(mmsi);

                    const iconHtml = `<div style="font-size:16px;">🚢</div>`;

                    if (shipMarkersMap[mmsi]) {
                        shipMarkersMap[mmsi].setLatLng([lat, lon]);
                    } else {
                        const markerNave = L.marker([lat, lon], {
                            icon: L.divIcon({
                                className: 'custom-div-icon',
                                html: iconHtml,
                                iconSize: [18, 18], iconAnchor: [9, 9]
                            })
                        });
                        markerNave.bindTooltip(`🚢 <b>${nome}</b><br>MMSI: ${ship.mmsi}<br>Velocità: <b>${ship.velocita} kt</b>`);
                        layerShips.addLayer(markerNave);
                        shipMarkersMap[mmsi] = markerNave;
                    }

                    if (container) {
                        const card = document.createElement("div");
                        card.className = "event-card";
                        card.onclick = () => map.flyTo([lat, lon], 12, { animate: true });
                        card.innerHTML = `
                            <div class="event-header">
                                <span>🚢 ${nome}</span>
                                <span class="event-mag blue">${tipo}</span>
                            </div>
                            <div class="event-place">🚢 <b>MMSI:</b> ${ship.mmsi}<br>⚡ <b>Velocità:</b> ${ship.velocita || 0} kn</div>
                        `;
                        container.appendChild(card);
                    }
                }
            });

            for (let id in shipMarkersMap) {
                if (!attiviOra.has(id)) {
                    layerShips.removeLayer(shipMarkersMap[id]);
                    delete shipMarkersMap[id];
                }
            }

            if (lbl) lbl.innerText = `🚢 TRACCIAMENTO NAVALE LIVE (AIS) (${conteggio})`;
        }

        async function caricaTrasportiCampaniaLive() {
            if (!window.pywebview) return;
            try {
                const resTransitStr = await window.pywebview.api.fetch_trasporti_campania();
                listaTransitGrezzi = JSON.parse(resTransitStr);

                transitGroup.clearLayers();
                const container = document.getElementById("listaTransitContainer");
                const lbl = document.getElementById("lblTransitCount");
                if (container) container.innerHTML = "";
                if (lbl) lbl.innerText = `🚆 TRASPORTI TPL CAMPANIA (ANM/EAV) (${listaTransitGrezzi.length})`;

                listaTransitGrezzi.forEach(t => {
                    const markerTransit = L.marker([t.lat, t.lon], {
                        icon: L.divIcon({
                            className: 'custom-div-icon',
                            html: `<div style="font-size:18px;">🚆</div>`,
                            iconSize: [20, 20], iconAnchor: [10, 10]
                        })
                    });
                    markerTransit.bindTooltip(`🚆 <b>${t.nome}</b><br>Modello: ${t.modello}<br>Costruttore: ${t.costruttore}<br>Tratta: ${t.tratta}`);
                    transitGroup.addLayer(markerTransit);

                    if (container) {
                        const card = document.createElement("div");
                        card.className = "event-card transit";
                        card.onclick = () => map.flyTo([t.lat, t.lon], 14, { animate: true, duration: 1.8 });
                        card.innerHTML = `
                            <div class="event-header">
                                <span>🚆 ${t.nome}</span>
                                <span class="event-mag green">${t.stato}</span>
                            </div>
                            <div class="event-place">⚙️ <b>Mezzo:</b> ${t.modello} (${t.costruttore})<br>🛣 <b>Tratta:</b> ${t.tratta}</div>
                        `;
                        container.appendChild(card);
                    }
                });
            } catch(e) { console.log("Errore carica TPL Campania:", e); }
        }

        async function caricaLanciSpazialiLive() {
            if (!window.pywebview) return;
            try {
                const resStr = await window.pywebview.api.fetch_lanci_spaziali();
                listaLanciGrezzi = JSON.parse(resStr);

                spaceGroup.clearLayers();
                const container = document.getElementById("listSpaceLaunches");
                const lbl = document.getElementById("lblSpaceLaunches");
                if (container) container.innerHTML = "";
                if (lbl) lbl.innerText = `🚀 LANCI SPAZIALI & ROCKETS (${listaLanciGrezzi.length})`;

                if (listaLanciGrezzi && listaLanciGrezzi.length > 0) {
                    listaLanciGrezzi.forEach(launch => {
                        const markerRocket = L.marker([launch.lat, launch.lon], {
                            icon: L.divIcon({
                                className: 'custom-div-icon',
                                html: `<div style="font-size:18px;">🚀</div>`,
                                iconSize: [20, 20], iconAnchor: [10, 10]
                            })
                        });
                        markerRocket.bindTooltip(`🚀 <b>${launch.nome}</b><br>Pad: ${launch.pad}<br>Stato: ${launch.stato}`);
                        spaceGroup.addLayer(markerRocket);

                        if (container) {
                            const card = document.createElement("div");
                            card.className = "event-card space";
                            card.onclick = () => map.flyTo([launch.lat, launch.lon], 11, { animate: true, duration: 1.8 });
                            card.innerHTML = `
                                <div class="event-header">
                                    <span>🚀 ${launch.nome}</span>
                                    <span class="event-mag blue">${launch.stato.toUpperCase()}</span>
                                </div>
                                <div class="event-place">📍 Pad: ${launch.pad} | ${launch.location || ''}</div>
                            `;
                            container.appendChild(card);
                        }
                    });
                } else if (container) {
                    container.innerHTML = "<div style='color:#64748b; font-size:11px; padding:6px;'>Nessun lancio imminente nel feed.</div>";
                }
            } catch(e) { console.log("Errore lanci spaziali:", e); }
        }

        async function caricaTerremotiLive() {
            try {
                const resUsgs = await fetch("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson");
                const dataUsgs = await resUsgs.json();

                const resEmsc = await fetch("https://www.seismicportal.eu/fdsnws/event/1/query?format=json&limit=50");
                const dataEmsc = await resEmsc.json();

                terremotiGroup.clearLayers();
                const eventiPerDb = [];

                dataUsgs.features.forEach(eq => {
                    const coords = eq.geometry.coordinates;
                    const mag = eq.properties.mag || 0;
                    const luogo = eq.properties.place || "Sconosciuto";
                    eventiPerDb.push({ id: eq.id, mag: mag, luogo: luogo, lat: coords[1], lon: coords[0] });

                    const markerCircle = L.circleMarker([coords[1], coords[0]], {
                        radius: Math.max(mag * 3.5, 3),
                        color: mag >= 4.5 ? '#ef4444' : '#f59e0b',
                        fillColor: mag >= 4.5 ? '#dc2626' : '#fbbf24',
                        fillOpacity: 0.6, weight: 1.5
                    });
                    markerCircle.bindTooltip(`🌋 <b>TERREMOTO M${mag}</b><br>${luogo}`);
                    terremotiGroup.addLayer(markerCircle);
                });

                if (dataEmsc && dataEmsc.features) {
                    dataEmsc.features.forEach(eq => {
                        const props = eq.properties;
                        const coords = eq.geometry.coordinates;
                        const mag = props.mag || 0;
                        const luogo = props.flynn_region || props.source_catalog || "Europa/Mediterraneo";
                        const eqId = props.unid || `emsc_${coords[1]}_${coords[0]}`;

                        eventiPerDb.push({ id: eqId, mag: mag, luogo: luogo, lat: coords[1], lon: coords[0] });

                        const markerCircle = L.circleMarker([coords[1], coords[0]], {
                            radius: Math.max(mag * 4.0, 3),
                            color: mag >= 4.0 ? '#ef4444' : '#0284c7',
                            fillColor: mag >= 4.0 ? '#dc2626' : '#38bdf8',
                            fillOpacity: 0.6, weight: 1.5
                        });
                        markerCircle.bindTooltip(`🌋 <b>EUROPA/ITALIA M${mag}</b><br>${luogo}`);
                        terremotiGroup.addLayer(markerCircle);
                    });
                }

                if (window.pywebview) {
                    const dbResStr = await window.pywebview.api.sync_terremoti_db(JSON.stringify(eventiPerDb), casaLat, casaLon);
                    const dbRes = JSON.parse(dbResStr);

                    if (dbRes.nuovi && dbRes.nuovi.length > 0) {
                        dbRes.nuovi.forEach(n => {
                            mostraNotificaNuovoEvento("NUOVO EVENTO RILEVATO", n, n.lat, n.lon);
                        });
                    }

                    renderizzaSidebarEventiPerCategoria(dbRes.lista_dal_logout, dbRes.lista_storico, listaVulcaniGlobal, listaCustomGlobal);
                }
            } catch(e) { console.log("Errore sismico:", e); }
        }

        async function caricaVulcaniLive() {
            try {
                const res = await fetch("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&eventtype=volcanic%20eruption&minmagnitude=0");
                const data = await res.json();
                vulcaniGroup.clearLayers();
                listaVulcaniGlobal = [];

                data.features.forEach(v => {
                    const coords = v.geometry.coordinates;
                    const titolo = v.properties.place || "Eruzione Vulcanica";

                    listaVulcaniGlobal.push({ id: v.id, luogo: titolo, lat: coords[1], lon: coords[0] });

                    const markerVulcano = L.marker([coords[1], coords[0]], {
                        icon: L.divIcon({
                            className: 'custom-div-icon',
                            html: `<div style="font-size:18px;">🌋</div>`,
                            iconSize: [20, 20], iconAnchor: [10, 10]
                        })
                    });
                    markerVulcano.bindTooltip(`🌋 <b>ATTIVITÀ VULCANICA / ERUZIONE</b><br>${titolo}`);
                    vulcaniGroup.addLayer(markerVulcano);
                });
            } catch(e) { console.log("Errore vulcani:", e); }
        }

        async function caricaEventiCustomMappa() {
            if (!window.pywebview) return;
            const res = await window.pywebview.api.get_eventi_custom();
            listaCustomGlobal = JSON.parse(res);

            customEventsGroup.clearLayers();
            listaCustomGlobal.forEach(evt => {
                const markerCustom = L.marker([evt.lat, evt.lon], {
                    icon: L.divIcon({
                        className: 'custom-div-icon',
                        html: `<div style="background-color:${evt.colore}; width:20px; height:20px; border-radius:50%; border:2px solid white; display:flex; align-items:center; justify-content:center; font-size:11px; box-shadow:0 0 10px ${evt.colore};">🎪</div>`,
                        iconSize: [20, 20], iconAnchor: [10, 10]
                    })
                });
                markerCustom.bindTooltip(`🎪 <b>${evt.nome}</b><br>Categoria: ${evt.categoria}`);
                customEventsGroup.addLayer(markerCustom);
            });
        }

        function toggleEventGroup(key) {
            openEventGroupsState[key] = !openEventGroupsState[key];
            const content = document.getElementById(`evt-content-${key}`);
            const arrow = document.getElementById(`evt-arrow-${key}`);
            if (content && arrow) {
                if (openEventGroupsState[key]) { content.classList.add("open"); arrow.innerText = "▲"; content.style.display="block"; }
                else { content.classList.remove("open"); arrow.innerText = "▼"; content.style.display="none"; }
            }
        }

        function toggleFiltroStoricoTerremoti() {
            mostraSoloDalLogout = !mostraSoloDalLogout;
            caricaTerremotiLive();
        }

        function renderizzaSidebarEventiPerCategoria(terremotiLogout, terremotiStorico, vulcani, customEvts) {
            const containerQuakes = document.getElementById("listQuakes");
            const listaTerremotiDaMostrare = mostraSoloDalLogout ? terremotiLogout : terremotiStorico;

            if (document.getElementById("lblQuakes")) {
                document.getElementById("lblQuakes").innerText = `🌋 TERREMOTI (${mostraSoloDalLogout ? 'DAL LOGOUT' : 'STORICO'}) (${listaTerremotiDaMostrare ? listaTerremotiDaMostrare.length : 0})`;
            }
            if (containerQuakes) {
                containerQuakes.innerHTML = "";

                const btnToggleFilter = document.createElement("button");
                btnToggleFilter.className = "btn-more-events";
                btnToggleFilter.style.marginBottom = "8px";
                btnToggleFilter.innerText = mostraSoloDalLogout ? "📜 MOSTRA TUTTO LO STORICO COMPLETO" : "⏳ MOSTRA SOLO EVENTI DAL LOGOUT";
                btnToggleFilter.onclick = toggleFiltroStoricoTerremoti;
                containerQuakes.appendChild(btnToggleFilter);

                if (listaTerremotiDaMostrare && listaTerremotiDaMostrare.length > 0) {
                    listaTerremotiDaMostrare.forEach(eq => {
                        const isStrong = eq.mag >= 4.5;
                        const card = document.createElement("div");
                        card.className = `event-card ${isStrong ? 'strong' : ''}`;
                        card.onclick = () => map.flyTo([eq.lat, eq.lon], 10, {animate:true, duration:1.8});
                        card.innerHTML = `
                            <div class="event-header">
                                <span>🌋 ${eq.luogo}</span>
                                <span class="event-mag ${isStrong ? 'red' : ''}">M${eq.mag}</span>
                            </div>
                            <div class="event-place">🕒 ${eq.time || ''}<br>📍 Distanza: <b>${eq.distanza_km} km</b> | Coords: ${eq.lat.toFixed(2)}°, ${eq.lon.toFixed(2)}°</div>
                        `;
                        containerQuakes.appendChild(card);
                    });
                } else {
                    const emptyMsg = document.createElement("div");
                    emptyMsg.style.cssText = "color:#64748b; font-size:11px; padding:6px;";
                    emptyMsg.innerText = mostraSoloDalLogout ? "Nessun nuovo evento rilevato dal tuo ultimo logout." : "Nessun evento registrato.";
                    containerQuakes.appendChild(emptyMsg);
                }
            }

            const containerVolcanoes = document.getElementById("listVolcanoes");
            if (document.getElementById("lblVolcanoes")) {
                document.getElementById("lblVolcanoes").innerText = `🌋 VULCANI & ERUZIONI ATTIVE (${vulcani ? vulcani.length : 0})`;
            }
            if (containerVolcanoes) {
                containerVolcanoes.innerHTML = "";
                if (vulcani && vulcani.length > 0) {
                    vulcani.forEach(v => {
                        const card = document.createElement("div");
                        card.className = "event-card strong";
                        card.onclick = () => map.flyTo([v.lat, v.lon], 10, {animate:true, duration:1.8});
                        card.innerHTML = `
                            <div class="event-header">
                                <span>🌋 ${v.luogo}</span>
                                <span class="event-mag red">ATTIVO</span>
                            </div>
                            <div class="event-place">📍 ${v.lat.toFixed(2)}°, ${v.lon.toFixed(2)}°</div>
                        `;
                        containerVolcanoes.appendChild(card);
                    });
                } else {
                    containerVolcanoes.innerHTML = "<div style='color:#64748b; font-size:11px; padding:6px;'>Nessuna eruzione attiva registrata.</div>";
                }
            }

            const containerCustom = document.getElementById("listCustomEvts");
            if (document.getElementById("lblCustomEvts")) {
                document.getElementById("lblCustomEvts").innerText = `🎪 SAGRE & EVENTI LOCALI (${customEvts ? customEvts.length : 0})`;
            }
            if (containerCustom) {
                containerCustom.innerHTML = "";
                if (customEvts && customEvts.length > 0) {
                    customEvts.forEach(evt => {
                        const card = document.createElement("div");
                        card.className = "event-card custom";
                        card.onclick = () => map.flyTo([evt.lat, evt.lon], 13, {animate:true, duration:1.8});
                        card.innerHTML = `
                            <div class="event-header">
                                <span>🎪 ${evt.nome}</span>
                                <span class="event-mag purple">${evt.categoria.toUpperCase()}</span>
                            </div>
                            <div class="event-place">📍 ${evt.lat.toFixed(2)}°, ${evt.lon.toFixed(2)}°</div>
                        `;
                        containerCustom.appendChild(card);
                    });
                } else {
                    containerCustom.innerHTML = "<div style='color:#64748b; font-size:11px; padding:6px;'>Nessuna sagra/evento salvato.</div>";
                }
            }

            sincronizzaPannelliDestra("🌋 Terremoti Live (USGS/EMSC)", map.hasLayer(terremotiGroup));
            sincronizzaPannelliDestra("🌋 Vulcani & Eruzioni Attive", map.hasLayer(vulcaniGroup));
            sincronizzaPannelliDestra("🎪 Sagre & Eventi Locali", map.hasLayer(customEventsGroup));
            sincronizzaPannelliDestra("🚀 Lanci Spaziali & Rockets", map.hasLayer(spaceGroup));
            sincronizzaPannelliDestra("🚆 Trasporti TPL Campania (ANM/EAV)", map.hasLayer(transitGroup));
        }

        async function apriModalCustomEvent() {
            caricaTabellaCustomEventi();
            document.getElementById("modalCustomEvent").style.display = "flex";
        }

        function chiudiModalCustomEvent() {
            document.getElementById("modalCustomEvent").style.display = "none";
        }

        async function salvaNuovoEventoCustom() {
            const nome = document.getElementById("custEvtNome").value.trim();
            const cat = document.getElementById("custEvtCat").value.trim();
            const lat = document.getElementById("custEvtLat").value;
            const lon = document.getElementById("custEvtLon").value;
            const col = document.getElementById("custEvtColore").value;

            if (!nome || !cat || !lat || !lon) {
                alert("Compila tutti i campi dell'evento!");
                return;
            }

            await window.pywebview.api.salva_evento_custom(nome, cat, lat, lon, col);
            document.getElementById("custEvtNome").value = "";
            document.getElementById("custEvtCat").value = "";
            document.getElementById("custEvtLat").value = "";
            document.getElementById("custEvtLon").value = "";

            caricaTabellaCustomEventi();
            forzaRilevamentoEventiConAnimazione();
        }

        async function caricaTabellaCustomEventi() {
            const res = await window.pywebview.api.get_eventi_custom();
            const lista = JSON.parse(res);
            const tbody = document.getElementById("customEvtTableBody");
            if (tbody) {
                tbody.innerHTML = "";
                lista.forEach(e => {
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td style="color:${e.colore}; font-weight:bold;">${e.nome}</td>
                        <td>${e.categoria}</td>
                        <td>${e.lat.toFixed(2)}, ${e.lon.toFixed(2)}</td>
                        <td><button class="btn-danger" onclick="eliminaEventoCustom(${e.id})">CANCELLA</button></td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        }

        async function eliminaEventoCustom(id) {
            if (confirm("Eliminare questo evento dalla mappa?")) {
                await window.pywebview.api.rimuovi_evento_custom(id);
                caricaTabellaCustomEventi();
                forzaRilevamentoEventiConAnimazione();
            }
        }

        function apriVista3DEarth(lat, lon, titoloZona) {
            document.getElementById("mediaTitle").innerText = `🌐 VISTA SATELLITARE TATTICA 3D // ${titoloZona.toUpperCase()}`;
            const iframe = document.getElementById("mediaIframe");
            iframe.src = `https://maps.google.com/maps?q=${lat},${lon}&t=k&z=17&ie=UTF8&iwloc=&output=embed`;
            document.getElementById("modalMedia").style.display = "flex";
        }

        function chiudiVistaMedia() {
            document.getElementById("modalMedia").style.display = "none";
            document.getElementById("mediaIframe").src = "about:blank";
        }

        function toggleMediaFullscreen() {
            const container = document.getElementById("mediaContainer");
            if (container.style.width === "100vw") {
                container.style.width = "85vw"; container.style.height = "85vh"; container.style.borderRadius = "12px";
            } else {
                container.style.width = "100vw"; container.style.height = "100vh"; container.style.borderRadius = "0px";
            }
        }

        function toggleGroupVisibility(key, visible) {
            for (let [id, dev] of Object.entries(globalDb)) {
                if (dev.proprietario && dev.proprietario.trim().toUpperCase() === key) {
                    toggleDev(id, visible);
                }
            }
        }

        function toggleGroup(propKey) {
            openGroupsState[propKey] = !openGroupsState[propKey];
            const content = document.getElementById(`content-${propKey}`);
            const arrow = document.getElementById(`arrow-${propKey}`);

            if (content && arrow) {
                if (openGroupsState[propKey]) { content.classList.add("open"); arrow.innerText = "▲"; content.style.display="block"; } 
                else { content.classList.remove("open"); arrow.innerText = "▼"; content.style.display="none"; }
            }
        }

        async function aggiornaMappaFromPython() {
            if (!window.pywebview) return;
            const res = await window.pywebview.api.get_dispositivi();
            const newDb = JSON.parse(res);

            const dbChanged = JSON.stringify(newDb) !== JSON.stringify(globalDb);
            globalDb = newDb;

            markersGroup.clearLayers();

            if (dbChanged || document.getElementById("deviceList").children.length === 0) {
                const listContainer = document.getElementById("deviceList");
                if (listContainer) {
                    listContainer.innerHTML = "";

                    const gruppi = {};
                    for (let [id, dev] of Object.entries(globalDb)) {
                        let prop = (dev.proprietario || "Altri").trim();
                        let key = prop.toUpperCase();
                        if (!gruppi[key]) { gruppi[key] = { nomeFormattato: prop, lista: [] }; }
                        gruppi[key].lista.push({ id, ...dev });
                    }

                    for (let [key, gruppo] of Object.entries(gruppi)) {
                        if (openGroupsState[key] === undefined) openGroupsState[key] = false;

                        const isOpen = openGroupsState[key];
                        const tuttiAttivi = gruppo.lista.every(d => d.attivo);

                        const groupDiv = document.createElement("div");
                        groupDiv.className = "group-container";

                        groupDiv.innerHTML = `
                            <div class="group-header" onclick="toggleGroup('${key}')">
                                <span style="display:flex; align-items:center; gap:8px;">
                                    <input type="checkbox" ${tuttiAttivi ? 'checked' : ''} onclick="event.stopPropagation(); toggleGroupVisibility('${key}', this.checked)">
                                    👤 ${gruppo.nomeFormattato.toUpperCase()} (${gruppo.lista.length})
                                </span>
                                <span class="group-arrow" id="arrow-${key}">${isOpen ? '▲' : '▼'}</span>
                            </div>
                            <div class="group-content" id="content-${key}" style="display:${isOpen ? 'block' : 'none'};"></div>
                        `;

                        const content = groupDiv.querySelector(`.group-content`);
                        gruppo.lista.forEach(dev => {
                            const card = document.createElement("div");
                            card.className = "device-card";
                            card.style.borderLeftColor = dev.colore;
                            card.innerHTML = `
                                <div class="device-main">
                                    <div class="device-info" onclick="volaSuDispositivo('${dev.id}')">
                                        <input type="checkbox" ${dev.attivo ? 'checked' : ''} onclick="event.stopPropagation();" onchange="toggleDev('${dev.id}', this.checked)">
                                        <span class="device-name" style="color:${dev.colore}">${dev.nome}</span>
                                    </div>
                                </div>
                                <div class="device-actions">
                                    <button class="btn-action" onclick="volaSuDispositivo('${dev.id}')">🎯 LOCALIZZA</button>
                                    <button class="btn-action" style="background:#f59e0b; color:black;" onclick="mostraStoricoRotta('${dev.id}')">📍 ROTTA GPS</button>
                                </div>
                            `;
                            content.appendChild(card);
                        });

                        listContainer.appendChild(groupDiv);
                    }
                }
            }

            for (let [id, dev] of Object.entries(globalDb)) {
                if (dev.attivo && dev.lat && dev.lon) {
                    const customIcon = L.divIcon({
                        className: 'custom-div-icon',
                        html: `<div style="background-color:${dev.colore}; width:18px; height:18px; border-radius:50%; border:2px solid white; box-shadow:0 0 10px ${dev.colore};"></div>`,
                        iconSize: [18, 18], iconAnchor: [9, 9]
                    });

                    const marker = L.marker([dev.lat, dev.lon], { icon: customIcon }).addTo(markersGroup);
                    marker.bindTooltip(`📍 <b>${dev.proprietario}</b> - ${dev.nome}`, { permanent: false, direction: 'right' });
                }
            }
        }

        function popolaSelectProprietari(selectedVal = "") {
            const select = document.getElementById("selectProp");
            if (!select) return;
            select.innerHTML = "";

            const proprietariSet = new Set();
            for (let dev of Object.values(globalDb)) {
                if (dev.proprietario) proprietariSet.add(dev.proprietario.trim());
            }

            proprietariSet.forEach(p => {
                const opt = document.createElement("option");
                opt.value = p; opt.innerText = p;
                select.appendChild(opt);
            });

            const optNew = document.createElement("option");
            optNew.value = "__NEW__"; optNew.innerText = "➕ NUOVO PROPRIETARIO...";
            select.appendChild(optNew);

            if (selectedVal && proprietariSet.has(selectedVal)) {
                select.value = selectedVal; gestisciCambioProprietario(selectedVal);
            } else if (selectedVal) {
                select.value = "__NEW__"; document.getElementById("addPropCustom").value = selectedVal; gestisciCambioProprietario("__NEW__");
            } else {
                select.value = select.options[0].value; gestisciCambioProprietario(select.value);
            }
        }

        function gestisciCambioProprietario(val) {
            const groupCustom = document.getElementById("groupNewProp");
            if (groupCustom) groupCustom.style.display = (val === "__NEW__") ? "block" : "none";
        }

        function volaSuDispositivo(id) {
            const dev = globalDb[id];
            if (dev && dev.lat && dev.lon) {
                map.flyTo([dev.lat, dev.lon], 14, { animate: true, duration: 1.8 });
            } else {
                alert("Posizione GPS del dispositivo non ancora ricevuta!");
            }
        }

        async function toggleDev(id, checked) {
            await window.pywebview.api.toggle_dispositivo(id, checked);
            aggiornaMappaFromPython();
        }

        function chiudiAppCompleta() { if (window.pywebview) window.pywebview.api.chiudi_app(); }

        function apriModalAggiungi() { 
            chiudiPannelloGestione();
            popolaSelectProprietari();
            document.getElementById("modalAddTitle").innerText = "AGGIUNGI NUOVO DISPOSITIVO";
            document.getElementById("editDevId").value = "";
            document.getElementById("addId").value = "";
            document.getElementById("addId").disabled = false;
            document.getElementById("addNome").value = "";
            document.getElementById("modalAdd").style.display = "flex"; 
        }
        function chiudiModalAdd() { document.getElementById("modalAdd").style.display = "none"; }

        function apriModalModifica(id) {
            const dev = globalDb[id];
            if (!dev) return;
            chiudiPannelloGestione();
            popolaSelectProprietari(dev.proprietario);
            document.getElementById("modalAddTitle").innerText = `MODIFICA DISPOSITIVO (${id})`;
            document.getElementById("editDevId").value = id;
            document.getElementById("addId").value = id;
            document.getElementById("addId").disabled = true;
            document.getElementById("addNome").value = dev.nome;
            document.getElementById("addColore").value = dev.colore;
            document.getElementById("modalAdd").style.display = "flex";
        }

        async function salvaDispositivo() {
            const editId = document.getElementById("editDevId").value;
            const id = document.getElementById("addId").value.trim();
            let prop = document.getElementById("selectProp").value;
            if (prop === "__NEW__") prop = document.getElementById("addPropCustom").value.trim();
            const nome = document.getElementById("addNome").value.trim();
            const col = document.getElementById("addColore").value;

            if (!id || !prop || !nome) { alert("Compila tutti i campi obbligatori!"); return; }

            if (editId) {
                await window.pywebview.api.modifica_dispositivo(editId, prop, nome, col);
            } else {
                await window.pywebview.api.aggiungi_dispositivo(id, prop, nome, col);
            }
            chiudiModalAdd();
            aggiornaMappaFromPython();
        }

        function apriPannelloGestione() {
            const tbody = document.getElementById("manageTableBody");
            if (tbody) {
                tbody.innerHTML = "";
                for (let [id, dev] of Object.entries(globalDb)) {
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td style="color:#00f2fe; font-weight:bold;">${id}</td>
                        <td>${dev.proprietario}</td>
                        <td><span style="color:${dev.colore}">●</span> ${dev.nome}</td>
                        <td>
                            <button class="btn-edit" onclick="apriModalModifica('${id}')">✏️ EDIT</button>
                            ${id !== 'PC_PRINCIPALE' ? `<button class="btn-danger" onclick="confermaElimina('${id}')">CANCELLA</button>` : '<span style="color:#64748b; font-size:10px;">BASE</span>'}
                        </td>
                    `;
                    tbody.appendChild(tr);
                }
            }
            document.getElementById("modalManage").style.display = "flex";
        }

        function chiudiPannelloGestione() { document.getElementById("modalManage").style.display = "none"; }

        async function confermaElimina(id) {
            if (confirm(`Sei sicuro di voler rimuovere il dispositivo ${id}?`)) {
                await window.pywebview.api.rimuovi_dispositivo(id);
                apriPannelloGestione();
                aggiornaMappaFromPython();
            }
        }
    </script>
</body>
</html>
"""

window_ref = None

def avvia_massimizzato():
    if window_ref:
        window_ref.maximize()

def al_chiusura_finestra():
    salva_timestamp_logout()

if __name__ == "__main__":
    threading.Thread(target=avvia_server_traccar, daemon=True).start()

    api = ApiBridge()
    window_ref = webview.create_window(
        title="RADICALIUM - Tactical Multi-Hazard Control Center by Salvatore Gison",
        html=HTML_UI,
        js_api=api,
        fullscreen=False
    )
    window_ref.events.closed += al_chiusura_finestra
    webview.start(avvia_massimizzato)
