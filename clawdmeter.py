"""
Clawdmeter-Anbindung fuer den Claude Session Browser.

Ersetzt den separaten Clawdmeter-Daemon: der Session Browser pollt selbst
die Anthropic-API nach den Ratelimit-Headern und schickt die Werte per BLE
an das Geraet.

Protokoll (aus der Clawdmeter-Firmware):
  Service  4c41555a-4465-7669-6365-000000000001
    ...0002  write     -> JSON-Payload mit den Usage-Werten
    ...0003  read/notify
    ...0004  notify    -> Geraet bittet um frische Daten

Das Geraet muss einmalig ueber die Windows-Bluetooth-Einstellungen gekoppelt
werden (es ist zugleich eine BLE-HID-Tastatur). Danach findet dieses Modul
die Adresse selbst ueber die PnP-Tabelle.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from i18n import t

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"
# Erstes Byte einer Meldung auf REQ: 0x02 = Konto wechseln (Byte 1 = Index wie
# in den "ac"-Meldungen, ab Byte 2 der Spitzname, so wie er geschickt wurde).
# 0x03 = das Geraet zeigt jetzt diesen PC, 0x04 = es zeigt einen anderen PC
# (bis zu drei gekoppelte PCs sind gleichzeitig verbunden). 0x05 = Fenster
# dieser Session nach vorn holen (langer Druck auf den Clawd-Bildschirm; Byte 1
# = Index in der letzten "se"-Liste, ab Byte 2 ihr Name, so wie er geschickt
# wurde). Alles andere (0x01, leer) heisst "bitte alles neu schicken".
REQ_SWITCH = 0x02
REQ_ACTIVE = 0x03
REQ_INACTIVE = 0x04
REQ_FOCUS = 0x05
# Name dieses PCs in der "hi"-Meldung: so viele Zeichen zeigt die Auswahl
# auf dem Geraet (Windows-Computernamen sind ohnehin hoechstens 15 lang).
PC_NAME_MAX = 15
# Lesbar ab Firmware-Protokoll 2: "2". Fehlt sie, versteht das Geraet nur die
# einfache Nutzungsmeldung - Sessions- und Kontenlisten gehen dann nicht raus.
CAPS_CHAR_UUID = "4c41555a-4465-7669-6365-000000000005"
# Standard-Batteriedienst. Die Firmware haengt ihn ueber NimBLEs HID-Geraet
# mit an (ble_set_battery_level -> hid_dev->setBatteryLevel), also nichts
# Eigenes - jedes Werkzeug kann den Ladestand lesen.
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

POLL_INTERVAL = 60      # Sekunden zwischen zwei API-Abfragen
RETRY_INTERVAL = 15     # Obergrenze der Wartezeit nach einem Fehlschlag
# Erster Versuch kurz: ein BLE-Geraet, das gerade wirklich erreichbar ist,
# antwortet in wenigen Sekunden. Wartet man stattdessen 20 s in einen
# Fehlversuch hinein und legt danach 15 s Pause ein, vergehen bis zum
# zweiten Versuch mehr als 30 Sekunden - obwohl das Geraet vielleicht schon
# nach fuenf wieder da war. Deshalb kurz anklopfen und zuegig nachfassen.
CONNECT_TIMEOUT_FIRST = 8
CONNECT_TIMEOUT = 20    # Abbruch wenn das Geraet nicht antwortet
RETRY_BACKOFF = (2, 4, 8, 15)   # Wartezeit nach dem 1., 2., 3., n-ten Fehlschlag
# Offene Sessions gehen als eigene "se"-Meldung je Session raus, hoechstens
# SE_MAX Stueck. Sessionnamen: hoechstens SE_NAME_MAX Zeichen; passt eine
# Meldung nicht in ein BLE-Paket, wird ihr Name bis auf SE_NAME_MIN gekuerzt.
SE_MAX = 8
SE_NAME_MAX = 20
SE_NAME_MIN = 4
# PC-Systemwerte ({"k":"sy",...}, ab Protokoll 2) gehen alle SY_INTERVAL
# Sekunden raus, unabhaengig von jeder Aenderungserkennung.
SY_INTERVAL = 2.0


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------

def _credential_candidates() -> list[Path]:
    if override := os.environ.get("CLAUDE_CREDENTIALS_PATH"):
        return [Path(override)]
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return [Path(config_dir) / ".credentials.json"]
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        home / ".claude" / ".credentials.json",
        local / "Claude" / ".credentials.json",
        roaming / "Claude" / ".credentials.json",
    ]


def _extract_token(blob: str) -> str | None:
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        tok = data.get("accessToken")
        if isinstance(tok, str) and tok.strip():
            return tok
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    return None


def read_token() -> str | None:
    """Liest den Claude-OAuth-Token aus der ersten gefundenen Credentials-Datei."""
    for path in _credential_candidates():
        try:
            tok = _extract_token(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if tok:
            return tok
    return None


# --------------------------------------------------------------------------
# API-Abfrage
# --------------------------------------------------------------------------

def _pct(value: str) -> int:
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def _reset_epoch(reset_ts: str) -> float:
    """Reset-Header (Epoch-Sekunden) -> float. 0.0 wenn unlesbar."""
    try:
        return float(reset_ts)
    except (TypeError, ValueError):
        return 0.0


def _reset_minutes(reset_ts: str, now: float) -> int:
    """Reset-Header (Epoch-Sekunden) -> verbleibende Minuten."""
    ts = _reset_epoch(reset_ts)
    mins = (ts - now) / 60.0
    return int(round(mins)) if mins > 0 else 0


def _ascii(v, limit: int) -> str:
    """Fuer das Display: nur ASCII (die Schriften des Geraets haben keine
    Umlaute oder Emoji), Leerraum zusammengezogen, gekuerzt."""
    text = str(v or "").encode("ascii", "ignore").decode()
    return " ".join(text.split())[:limit]


def _halve(values) -> list:
    """Je zwei benachbarte Werte mitteln (ganzzahlig); bei ungerader Anzahl
    bleibt der letzte allein. Fuer "cc", wenn "sy" nicht ins Paket passt."""
    out = []
    for i in range(0, len(values), 2):
        grp = values[i:i + 2]
        out.append(int(sum(grp) / len(grp) + 0.5))
    return out


def parse_request(data) -> tuple | None:
    """Meldung des Geraets auf REQ auswerten.

    ("refresh",) fuer 0x01, leer oder unbekannt (alte Bedeutung);
    ("switch", index, spitzname) fuer 0x02 mit Index; None fuer ein 0x02
    ohne Index - das wird verworfen, statt irgendein Konto zu raten.
    ("active",) fuer 0x03 (Geraet zeigt diesen PC), ("inactive",) fuer 0x04
    (Geraet zeigt einen anderen PC). ("focus", index, name) fuer 0x05 mit
    Index; ein 0x05 ohne Index wird zu ("focus", None, "") und verworfen."""
    raw = bytes(data or b"")
    if raw and raw[0] == REQ_ACTIVE:
        return ("active",)
    if raw and raw[0] == REQ_INACTIVE:
        return ("inactive",)
    if raw and raw[0] == REQ_FOCUS:
        if len(raw) < 2:
            return ("focus", None, "")
        name = raw[2:].split(b"\0", 1)[0].decode("ascii", "ignore").strip()
        return ("focus", raw[1], name)
    if not raw or raw[0] != REQ_SWITCH:
        return ("refresh",)
    if len(raw) < 2:
        return None
    nick = raw[2:].split(b"\0", 1)[0].decode("ascii", "ignore").strip()
    return ("switch", raw[1], nick)


def pc_name() -> str:
    """Name dieses PCs fuer die "hi"-Meldung: der Windows-Computername, nur
    ASCII (die Schriften des Geraets haben keine Umlaute), hoechstens
    PC_NAME_MAX Zeichen. Bleibt davon nichts uebrig, der Hostname, sonst "PC"."""
    name = _ascii(os.environ.get("COMPUTERNAME"), PC_NAME_MAX).strip()
    if not name:
        try:
            import socket
            name = _ascii(socket.gethostname(), PC_NAME_MAX).strip()
        except Exception:
            name = ""
    return name or "PC"


def payload_from_usage(u: dict, now: float | None = None) -> tuple[dict, dict]:
    """Nutzung aus accounts.py (kostenlose /api/oauth/usage-Abfrage) als
    BLE-Payload + Meta, im selben Format wie poll_usage_meta. Zusaetzlich:
    "x5": 1 = dieses Konto hat kein 5-Stunden-Limit (Ultra); "f"/"fr" =
    Fable-Wochenlimit in Prozent / Minuten bis zum Reset. Aeltere Firmware
    ignoriert die neuen Felder."""
    now = time.time() if now is None else now

    def mins(at) -> int:
        at = float(at or 0)
        return int(round((at - now) / 60.0)) if at > now else 0

    s = u.get("s")
    payload = {
        "s": int(s) if s is not None else 0,
        "sr": mins(u.get("s_reset_at")),
        "w": int(u.get("w") or 0),
        "wr": mins(u.get("w_reset_at")),
        "st": "rejected" if u.get("limited") else "allowed",
        "acct": "pro",
        "ok": True,
    }
    if s is None:
        payload["x5"] = 1
    if u.get("f") is not None:
        payload["f"] = int(u["f"])
        payload["fr"] = mins(u.get("f_reset_at"))
    payload["t"] = int(now) + time.localtime(now).tm_gmtoff
    payload["tf"] = 24
    meta = {"session_reset_at": float(u.get("s_reset_at") or 0),
            "weekly_reset_at": float(u.get("w_reset_at") or 0),
            "session_pct": payload["s"], "weekly_pct": payload["w"],
            "status": payload["st"], "at": float(u.get("at") or now)}
    return payload, meta


def poll_usage(token: str) -> dict | None:
    """Wie poll_usage_meta, aber nur der BLE-Payload."""
    payload, _meta = poll_usage_meta(token)
    return payload


def poll_usage_meta(token: str) -> tuple[dict | None, dict]:
    """Fragt die Ratelimit-Header bei der Anthropic-API ab.

    Schickt eine Mini-Anfrage (1 Token) -- die Antwort interessiert nicht,
    nur die Header. Gibt (BLE-Payload, Meta) zurueck; Payload ist None wenn
    die Abfrage fehlschlug.

    Meta traegt die rohen Reset-Zeitpunkte als Epoch. Der Payload kann die
    nicht liefern: der schickt dem Geraet nur noch verbleibende MINUTEN, und
    aus einer gerundeten Minutenzahl laesst sich kein stabiler Zeitpunkt
    zurueckrechnen -- fuer den Reset-Timer brauchen wir aber genau den."""
    headers = dict(API_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(API_BODY).encode()
    req = urllib.request.Request(API_URL, data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            hdrs = resp.headers
    except urllib.error.HTTPError as e:
        # 429 & Co. liefern die Header trotzdem mit
        hdrs = e.headers
        if e.code in (401, 403):
            return None, {}
    except Exception:
        return None, {}

    def hdr(name: str, default: str = "") -> str:
        return hdrs.get(name) or default

    now = time.time()
    meta = {}
    if hdr("anthropic-ratelimit-unified-5h-utilization"):
        meta = {
            "session_reset_at": _reset_epoch(
                hdr("anthropic-ratelimit-unified-5h-reset")),
            "weekly_reset_at": _reset_epoch(
                hdr("anthropic-ratelimit-unified-7d-reset")),
        }
        payload = {
            "s": _pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
            "sr": _reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset"), now),
            "w": _pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
            "wr": _reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset"), now),
            "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
            "acct": "pro",
            "ok": True,
        }
    elif hdr("anthropic-ratelimit-unified-overage-utilization"):
        meta = {
            "session_reset_at": _reset_epoch(
                hdr("anthropic-ratelimit-unified-overage-reset")),
            "weekly_reset_at": 0.0,
        }
        payload = {
            "s": _pct(hdr("anthropic-ratelimit-unified-overage-utilization")),
            "sr": _reset_minutes(hdr("anthropic-ratelimit-unified-overage-reset"), now),
            "w": 0,
            "wr": 0,
            "st": hdr("anthropic-ratelimit-unified-status", "unknown"),
            "acct": "ent",
            "ok": True,
        }
    else:
        return None, {}

    # Uhrzeit fuers Display (lokale Wall-Clock als Epoch)
    payload["t"] = int(time.time()) + time.localtime().tm_gmtoff
    payload["tf"] = 24
    meta["session_pct"] = payload["s"]
    meta["weekly_pct"] = payload["w"]
    meta["status"] = payload["st"]
    meta["at"] = now
    return payload, meta


# --------------------------------------------------------------------------
# Geraeteadresse
# --------------------------------------------------------------------------

def _mac_from_instance_id(instance_id: str) -> str | None:
    # Gross-/Kleinschreibung variiert: PnP liefert "DEV_", die Registry "Dev_".
    m = re.search(r"DEV_([0-9A-Fa-f]{12})(?![0-9A-Fa-f])", instance_id,
                  re.IGNORECASE)
    if not m:
        return None
    h = m.group(1).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


# Kurz zwischenspeichern -- die Liste aendert sich praktisch nie.
_devices_cache: dict = {"at": 0.0, "value": []}
_DEVICES_TTL = 30.0

# Hier fuehrt Windows jedes gekoppelte BLE-Geraet mit Namen und Adresse.
_BTHLE_KEY = r"SYSTEM\CurrentControlSet\Enum\BTHLE"


def list_paired_devices(force: bool = False) -> list[dict]:
    """Alle in Windows gekoppelten BLE-Geraete: [{name, address}, ...].

    Liest die Registry direkt. Frueher lief das ueber PowerShell -- das
    riss aber jedes Mal ein Konsolenfenster auf, egal welche Flags gesetzt
    waren. Die Registry-Variante startet keinen Prozess und ist sofort da.

    Ein gekoppeltes UND verbundenes Geraet sendet keine Advertisements mehr,
    ein normaler BLE-Scan findet es also nicht -- Windows kennt es aber."""
    if sys.platform != "win32":
        return []
    now = time.time()
    if not force and now - _devices_cache["at"] < _DEVICES_TTL:
        return list(_devices_cache["value"])

    import winreg
    out, seen = [], set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _BTHLE_KEY) as root:
            for i in range(4096):
                try:
                    dev = winreg.EnumKey(root, i)     # z.B. "Dev_98a316a5d60e"
                except OSError:
                    break
                mac = _mac_from_instance_id(dev)
                if not mac or mac in seen:
                    continue

                # Der Anzeigename haengt eine Ebene tiefer an der Instanz.
                name = ""
                try:
                    with winreg.OpenKey(root, dev) as devkey:
                        for j in range(64):
                            try:
                                inst = winreg.EnumKey(devkey, j)
                            except OSError:
                                break
                            try:
                                with winreg.OpenKey(devkey, inst) as ik:
                                    name = winreg.QueryValueEx(
                                        ik, "FriendlyName")[0]
                                if name:
                                    break
                            except OSError:
                                continue
                except OSError:
                    pass

                seen.add(mac)
                out.append({"name": (name or "").strip() or mac,
                            "address": mac})
    except OSError:
        return list(_devices_cache["value"])

    _devices_cache["at"] = now
    _devices_cache["value"] = out
    return list(out)


def _looks_like_clawdmeter(name: str) -> bool:
    return "clawd" in (name or "").lower()


def discover_address(preferred: str | None = None) -> str | None:
    """Ermittelt die zu benutzende BLE-Adresse.

    Reihenfolge:
      1. ausdruecklich gewaehltes Geraet (Einstellungen)
      2. CLAWDMETER_BLE_ADDRESS aus der Umgebung
      3. gekoppeltes Geraet dessen Name nach Clawdmeter aussieht
      4. wenn genau EIN BLE-Geraet gekoppelt ist: dieses
    """
    if preferred and preferred.strip():
        return preferred.strip().upper()
    if override := os.environ.get("CLAWDMETER_BLE_ADDRESS"):
        return override.strip().upper()

    devices = list_paired_devices()
    for d in devices:
        if _looks_like_clawdmeter(d["name"]):
            return d["address"]
    if len(devices) == 1:
        return devices[0]["address"]
    return None


# Als Funktionen, damit t() erst beim Melden uebersetzt - in der Sprache, die
# dann eingestellt ist, nicht in der vom Programmstart.
_BT_NICHT_VERFUEGBAR = {
    "POWERED_OFF": lambda: t("Bluetooth ist ausgeschaltet"),
    "NO_BLUETOOTH": lambda: t("Kein Bluetooth-Adapter gefunden"),
    "NO_BLE_CENTRAL_ROLE":
        lambda: t("Der Bluetooth-Adapter unterstützt kein Bluetooth LE"),
    "DENIED_BY_USER": lambda: t("Bluetooth-Zugriff wurde verweigert"),
    "DENIED_BY_SYSTEM": lambda: t("Bluetooth-Zugriff ist vom System gesperrt"),
    "DENIED_BY_UNKNOWN": lambda: t("Bluetooth-Zugriff wurde verweigert"),
}


def _fehlertext(e: Exception) -> str:
    """Lesbarer Grund fuer die Statuszeile.

    bleak legt bei fehlendem Bluetooth Meldung und Grund zusammen in die
    Exception. str(e) zeigt dann das rohe Tupel:
    ('Bluetooth radio is not powered on. ...', <...Reason.POWERED_OFF: 3>)
    """
    grund = getattr(getattr(e, "reason", None), "name", None)
    if grund in _BT_NICHT_VERFUEGBAR:
        return _BT_NICHT_VERFUEGBAR[grund]()
    if len(e.args) > 1 and isinstance(e.args[0], str):
        return e.args[0]
    return str(e) or type(e).__name__


def _ble_target(address: str):
    """Was BleakClient zum Verbinden bekommt.

    Mit einer blossen Adresse sucht bleak unter Windows das Geraet erst per
    Scan. Ein gekoppelter Clawdmeter haengt aber als HID-Tastatur schon an
    Windows - und ein verbundenes BLE-Geraet wirbt nicht mehr. Der Scan fand
    es dann nie ("Device ... was not found"), obwohl die Verbindung stand.
    Windows kann ein gekoppeltes Geraet auch direkt ueber die Adresse
    ansprechen; dafuer bekommt bleak ein BLEDevice mit der Adresse als Zahl
    und laesst den Scan weg. Faellt das weg (anderes System, andere bleak-
    Version), bleibt es bei der Adresse und damit beim Scan.
    """
    if sys.platform != "win32":
        return address
    try:
        from types import SimpleNamespace
        from bleak.backends.device import BLEDevice
        from bleak.backends.winrt.scanner import RawAdvData
        nummer = int(address.replace(":", ""), 16)
        return BLEDevice(address, DEVICE_NAME, RawAdvData(
            adv=SimpleNamespace(bluetooth_address=nummer), scan=None))
    except Exception:
        return address


# --------------------------------------------------------------------------
# Hintergrund-Link
# --------------------------------------------------------------------------

class ClawdmeterLink:
    """Haelt die BLE-Verbindung zum Clawdmeter und schickt zyklisch Updates.

    Laeuft in einem eigenen Thread mit eigener asyncio-Loop, damit die
    pywebview-GUI davon nichts mitbekommt."""

    def __init__(self, log=None, address_provider=None, on_usage=None,
                 anim_provider=None, on_battery=None, session_provider=None,
                 usage_provider=None, extras_provider=None,
                 on_switch_request=None, system_provider=None,
                 on_focus_request=None):
        """address_provider: Funktion die die gewuenschte Geraeteadresse liefert
        (leer/None = automatisch suchen). Wird bei jedem Versuch neu gefragt,
        damit eine Aenderung in den Einstellungen sofort greift.

        on_usage: wird nach jeder erfolgreichen API-Abfrage mit dem Meta-Dict
        gerufen (siehe poll_usage_meta).

        anim_provider: liefert den Namen der Animation die das Geraet zeigen
        soll (= was der Desktop-Buddy gerade zeigt), oder "" wenn das Geraet
        selbst entscheiden soll. Wird oft gefragt, muss also billig sein."""
        self._log = log or (lambda msg: None)
        self._address_provider = address_provider or (lambda: None)
        self._on_usage = on_usage
        self._anim_provider = anim_provider or (lambda: "")
        # session_provider: {"n": Name der Session, die die Animation meint,
        # "h": Hinweis auf die anderen, z.B. "+2 working", optional "cx":
        # belegter Kontext dieser Session in Prozent, "cw": Fenstergroesse
        # als Text ("1M", "200K")}. Geht unter denselben Schluesseln mit;
        # aeltere Firmware ignoriert unbekannte Felder.
        self._session_provider = session_provider or (lambda: {"n": "", "h": ""})
        self._last_session = ("", "", None, "")
        # usage_provider: Nutzung des aktiven Kontos aus accounts.py (oder None)
        # - bevorzugt vor der Header-Abfrage: kostet kein Kontingent und
        # funktioniert auch fuer Tarife ohne 5-Stunden-Limit.
        self._usage_provider = usage_provider or (lambda: None)
        # extras_provider: {"se": [...Sessions...], "ac": [...Konten...]} fuer
        # Firmware ab Protokoll 2 (CAPS_CHAR_UUID).
        self._extras_provider = extras_provider or (lambda: {})
        # on_switch_request(index, spitzname): das Geraet bittet, das aktive
        # Konto zu wechseln (REQ 0x02). Laeuft in einem eigenen Thread, damit
        # ein langsamer Wechsel (Token erneuern, Dateien schreiben) die
        # BLE-Loop nicht aufhaelt; danach gehen frische Daten raus.
        self._on_switch_request = on_switch_request
        # on_focus_request(index, name): das Geraet bittet, das Fenster dieser
        # Session nach vorn zu holen (REQ 0x05). Eigener kurzer Thread, damit
        # Fenstersuche und UI Automation die BLE-Loop nicht aufhalten; danach
        # wird nichts neu geschickt (am Geraet aendert sich nichts).
        self._on_focus_request = on_focus_request
        # system_provider: PC-Systemwerte (sysstats.SystemStats.sample(), ohne
        # "k") fuer den System-Bildschirm, ab Protokoll 2 alle SY_INTERVAL s.
        # Fehler darin duerfen die Verbindung nie stoeren.
        self._system_provider = system_provider
        self._last_sy = None         # monotonic() der letzten "sy"-Meldung
        self._sy_failed = False      # Fehler schon gemeldet (nicht alle 2 s)
        self._proto = 1
        self._mtu = 23
        self._last_extras = {}
        # Wird nur bei einer Aenderung gerufen, nicht bei jedem Nachlesen -
        # sonst haengt an jedem Poll eine Auswertung.
        self._on_battery = on_battery
        self._last_payload = None    # letzte API-Antwort, fuer Anim-Updates
        self._last_anim = ""         # "" = nichts vorgegeben, wie beim Geraet
        self._thread = None
        self._stop = threading.Event()
        self._status = {"connected": False, "last_send": None,
                        "last_error": None, "address": None,
                        "battery": None,   # Ladestand in %, None = unbekannt
                        "attempt": 0,      # laufender Verbindungsversuch
                        # Zeigt das Geraet diesen PC? True/False nach REQ
                        # 0x03/0x04, None = unbekannt (keine Verbindung oder
                        # aeltere Firmware, die nur einen PC kennt).
                        "shown": None}
        # Beim naechsten Durchlauf alles neu schicken: "hi", Nutzung, alle
        # "se"/"ac" (auch unveraenderte) und sofort "sy". Gesetzt von REQ
        # 0x01 und wenn das Geraet wieder diesen PC zeigt.
        self._resend_all = False
        # (Loop, Client) der laufenden Verbindung, fuer request_show() aus
        # anderen Threads. None ohne Verbindung.
        self._live = None
        self._lock = threading.Lock()
        # Bricht eine laufende Wartezeit ab ("Jetzt verbinden").
        self._wake = threading.Event()

    # -- oeffentliche API --------------------------------------------------

    def reconnect(self) -> bool:
        """Sofort einen neuen Verbindungsversuch anstossen.

        Laeuft der Thread schon, wird nur die Wartezeit abgekuerzt - ein
        Neustart waere unnoetig und wuerde eine gerade aufgebaute Verbindung
        wegwerfen. Laeuft er nicht, wird er gestartet.
        """
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            self._wake.set()
            return True
        return self.start()

    def start(self) -> bool:
        old = self._thread
        if old and old.is_alive():
            if not self._stop.is_set():
                return True          # laeuft schon
            # Ein stop() ist noch im Gange: kurz auf das Ende warten, damit
            # nicht zwei Threads parallel auf dasselbe Geraet zugreifen.
            old.join(timeout=CONNECT_TIMEOUT + 5)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="clawdmeter")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def request_show(self, timeout: float = 3.0) -> bool:
        """Das Geraet bitten, diesen PC zu zeigen ({"k":"use"}). Darf aus
        jedem Thread kommen (Oberflaeche, Tray), nur nicht aus der BLE-Loop
        selbst: die Meldung geht ueber die asyncio-Loop der Verbindung raus.
        False ohne Verbindung, bei Firmware unter Protokoll 2 oder wenn das
        Schreiben scheitert."""
        import asyncio
        live = self._live
        if not live or self._proto < 2:
            return False
        loop, client = live
        coro = self._write_json(client, {"k": "use"})
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()             # Loop schon beendet
            return False
        try:
            fut.result(timeout=timeout)
        except Exception as e:
            fut.cancel()
            self._log(f"Clawdmeter: show request failed: {e}")
            return False
        self._log("Clawdmeter: asked the gadget to show this PC")
        return True

    # -- intern ------------------------------------------------------------

    def _set(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    def _active(self) -> bool:
        """Zeigt das Geraet diesen PC? Solange es nichts dazu gesagt hat
        (aeltere Firmware), ja - dann laeuft alles wie bisher."""
        with self._lock:
            return self._status.get("shown") is not False

    async def _write_json(self, client, obj) -> None:
        data = json.dumps(obj, separators=(",", ":")).encode()
        await client.write_gatt_char(RX_CHAR_UUID, data, response=False)

    def _hi_message(self) -> dict:
        """{"k":"hi","n":Name dieses PCs}; der Name wird gekuerzt, bis die
        Meldung in ein BLE-Paket passt (hoechstens bis auf 1 Zeichen)."""
        limit = max(20, self._mtu - 3)
        msg = {"k": "hi", "n": pc_name()}
        while (len(json.dumps(msg, separators=(",", ":"))) > limit
               and len(msg["n"]) > 1):
            msg["n"] = msg["n"][:-1].rstrip() or msg["n"][:1]
        return msg

    async def _send_hi(self, client) -> None:
        """Diesen PC beim Geraet vorstellen. Nur ab Protokoll 2 - aeltere
        Firmware liest jede Meldung ohne bekanntes "k" als Nutzung."""
        if self._proto < 2:
            return
        await self._write_json(client, self._hi_message())

    async def _sleep(self, seconds: float) -> None:
        """Wartet, bricht aber sofort ab wenn stop() oder reconnect() kam."""
        import asyncio
        waited = 0.0
        while waited < seconds and not self._stop.is_set():
            if self._wake.is_set():
                self._wake.clear()
                return
            await asyncio.sleep(0.5)
            waited += 0.5

    def _run(self) -> None:
        try:
            import asyncio
            asyncio.run(self._loop())
        except Exception as e:
            self._log(f"Clawdmeter thread ended: {e}")
            self._set(connected=False, last_error=_fehlertext(e))

    async def _loop(self) -> None:
        import asyncio
        fails = 0
        while not self._stop.is_set():
            try:
                preferred = self._address_provider()
            except Exception:
                preferred = None
            address = discover_address(preferred)
            if not address:
                self._set(connected=False,
                          last_error=t("Kein Gerät ausgewählt"))
                await self._sleep(RETRY_INTERVAL)
                continue
            self._set(address=address, attempt=fails + 1)
            try:
                await self._session(address)
                fails = 0
                # Sauber beendete Sitzung (Geraet weg / abgeschaltet):
                # kurz warten, damit kein Reconnect-Karussell entsteht.
                await self._sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fails += 1
                self._set(connected=False, last_error=_fehlertext(e))
                self._log(f"Clawdmeter connection lost: {e}")
                wait = RETRY_BACKOFF[min(fails - 1, len(RETRY_BACKOFF) - 1)]
                await self._sleep(wait)

    async def _session(self, address: str) -> None:
        import asyncio
        from bleak import BleakClient

        refresh = asyncio.Event()
        loop = asyncio.get_running_loop()

        def wake() -> None:
            # Darf aus jedem Thread kommen (der Kontowechsel laeuft getrennt).
            try:
                loop.call_soon_threadsafe(refresh.set)
            except RuntimeError:
                pass  # Loop schon beendet: Sitzung vorbei, nichts zu tun

        def on_refresh(_char, data) -> None:
            self._handle_request(data, wake)

        # Beim ersten Anlauf kurz anklopfen, danach laenger Geduld haben.
        with self._lock:
            erster = int(self._status.get("attempt") or 1) <= 1
        timeout = CONNECT_TIMEOUT_FIRST if erster else CONNECT_TIMEOUT
        async with BleakClient(_ble_target(address), timeout=timeout) as client:
            # Fehlt der Service, hat das zwei moegliche Gruende: falsches
            # Geraet gewaehlt -- oder das Geraet haengt schon an einem anderen
            # Programm, dann liefert Windows eine unvollstaendige Service-Liste.
            # Beides ist kein Grund aufzugeben, aber ein Schreibversuch waere
            # zwecklos. Also melden und es spaeter nochmal probieren.
            if not any(s.uuid.lower() == SERVICE_UUID
                       for s in client.services):
                self._set(connected=False, last_error=t(
                    "Gerät meldet keinen Clawdmeter-Dienst — falsches Gerät, "
                    "oder es ist von einem anderen Programm belegt"))
                self._log(f"{address}: no Clawdmeter service found")
                await self._sleep(RETRY_INTERVAL)
                return

            self._set(connected=True, last_error=None)
            self._log(f"Clawdmeter connected ({address})")
            try:
                caps = await client.read_gatt_char(CAPS_CHAR_UUID)
                self._proto = int(bytes(caps).decode().strip() or "1")
            except Exception:
                self._proto = 1          # aeltere Firmware
            self._mtu = int(getattr(client, "mtu_size", 23) or 23)
            self._last_extras = {}       # neue Sitzung: alles neu schicken
            self._log(f"Clawdmeter: protocol {self._proto}, MTU {self._mtu}")
            # Neue Verbindung: bis das Geraet etwas anderes sagt, gilt dieser
            # PC als angezeigt (so verhaelt sich auch aeltere Firmware).
            self._set(shown=None)
            self._resend_all = False
            try:
                await client.start_notify(REQ_CHAR_UUID, on_refresh)
            except Exception:
                pass  # Refresh-Kanal ist optional, der Poll-Loop reicht
            await self._send_hi(client)

            def on_battery(_char, data) -> None:
                if data:
                    self._battery(int(data[0]))

            # Ladestand: einmal lesen, danach auf Meldungen horchen. Das
            # Abonnement ist nicht garantiert (aeltere Firmware, oder Windows
            # gibt den Dienst nicht frei), deshalb wird im Poll-Loop zusaetzlich
            # nachgelesen.
            await self._read_battery(client)
            try:
                await client.start_notify(BATTERY_CHAR_UUID, on_battery)
            except Exception:
                pass

            self._last_payload = None    # frische Sitzung, nichts zwischenlagern
            self._last_anim = ""
            self._last_sy = None
            self._sy_failed = False
            self._live = (loop, client)
            try:
                while not self._stop.is_set() and client.is_connected:
                    # Vor dem Senden zuruecksetzen: eine Bitte, die waehrend
                    # des Sendens kommt, loest so gleich den naechsten
                    # Durchlauf aus, statt bis zum naechsten Poll zu warten.
                    refresh.clear()
                    await self._cycle(client)
                    # Auf den naechsten Poll warten -- oder frueher, wenn das
                    # Geraet selbst um Daten bittet.
                    # In kleinen Scheiben warten, damit stop() sofort greift
                    # und damit ein Buddy-Wechsel schnell durchkommt.
                    waited = 0.0
                    while (waited < POLL_INTERVAL and not self._stop.is_set()
                           and not refresh.is_set()):
                        try:
                            await asyncio.wait_for(refresh.wait(), 1.0)
                        except asyncio.TimeoutError:
                            waited += 1.0
                            # Der Buddy wechselt viel haeufiger als die
                            # Nutzungszahlen. Solche Wechsel gehen ohne neue
                            # API-Abfrage raus -- sonst wuerde jedes Blinzeln
                            # einen echten Request kosten.
                            if self._anim_changed():
                                await self._send_once(client, poll=False)
                            elif self._extras_changed():
                                # Noch keine Nutzungszahlen (Abfrage
                                # gescheitert): die Sessions trotzdem.
                                await self._send_extras(client)
                            await self._maybe_send_system(client)
            finally:
                self._live = None
        # Ohne Verbindung ist der Ladestand unbekannt, nicht "wie zuletzt" -
        # ein stehengebliebener Wert waere schlimmer als gar keiner. Welchen
        # PC das Geraet zeigt, ebenso.
        self._set(connected=False, battery=None, shown=None)

    async def _cycle(self, client) -> None:
        """Ein Durchlauf: Nutzung abfragen und schicken, Ladestand lesen,
        Systemwerte schicken. Steht ein Neuschicken an (REQ 0x01 oder das
        Geraet zeigt wieder diesen PC), zuerst "hi" und alles ohne
        Aenderungserkennung. Zeigt das Geraet einen anderen PC, geht ausser
        "hi" nichts raus; abgefragt wird trotzdem, damit die Limit-Meldungen
        weiter Zahlen bekommen."""
        if self._resend_all:
            self._resend_all = False
            await self._send_hi(client)
            self._last_extras = {}       # nichts als "schon da" ueberspringen
            self._last_sy = None         # "sy" sofort, nicht erst nach 2 s
        await self._send_once(client)
        await self._read_battery(client)
        await self._maybe_send_system(client)

    def _handle_request(self, data, wake) -> None:
        """Eine Meldung auf REQ. wake() stoesst einen sofortigen Durchlauf an
        (Abfrage + Senden) und muss aus jedem Thread gehen.

        Kontowechsel: der Rueckruf laeuft in einem eigenen Thread, erst danach
        kommt der Durchlauf - so gehen Zahlen und Kontenliste des neuen Kontos
        raus, nicht noch einmal die alten."""
        req = parse_request(data)
        if req is None:
            self._log("Clawdmeter: account switch without an index dropped")
            return
        if req[0] == "inactive":
            # Das Geraet zeigt einen anderen PC: ab jetzt still bis auf "hi".
            self._set(shown=False)
            self._log("Clawdmeter: the gadget shows another PC")
            return
        if req[0] == "active":
            with self._lock:
                war = self._status.get("shown")
                self._status["shown"] = True
            if war is not True:
                # Wieder angezeigt: alles neu, das Geraet hat nichts von uns.
                self._log("Clawdmeter: the gadget shows this PC")
                self._resend_all = True
                wake()
            return
        if req[0] == "refresh":
            self._resend_all = True      # 0x01: "hi" und alles neu
            wake()
            return
        if req[0] == "focus":
            self._focus(req[1], req[2])
            return
        if not self._on_switch_request:
            wake()
            return
        _, index, nick = req
        cb = self._on_switch_request

        def _go():
            try:
                cb(index, nick)
            except Exception as e:
                self._log(f"Clawdmeter: account switch failed: {e}")
            finally:
                wake()

        threading.Thread(target=_go, daemon=True,
                         name="clawdmeter-switch").start()

    def _focus(self, index, name) -> None:
        """REQ 0x05: Rueckruf in einem eigenen kurzen Thread, ohne Durchlauf
        danach."""
        if index is None:
            self._log("Clawdmeter: focus request without an index dropped")
            return
        cb = self._on_focus_request
        if not cb:
            return

        def _go():
            try:
                cb(index, name)
            except Exception as e:
                self._log(f"Clawdmeter: focus request failed: {e}")

        threading.Thread(target=_go, daemon=True, name="clawdmeter-focus").start()

    async def _read_battery(self, client) -> None:
        """Ladestand lesen. Fehlt der Dienst, bleibt der Wert None - das ist
        kein Fehlerfall, aeltere Firmware meldet ihn schlicht nicht."""
        try:
            data = await client.read_gatt_char(BATTERY_CHAR_UUID)
        except Exception:
            return
        if data:
            self._battery(int(data[0]))

    def _battery(self, pct: int) -> None:
        """Neuen Ladestand uebernehmen und nur bei Aenderung weitermelden."""
        pct = max(0, min(100, int(pct)))
        with self._lock:
            if self._status.get("battery") == pct:
                return
            self._status["battery"] = pct
        if self._on_battery:
            try:
                self._on_battery(pct)
            except Exception:
                pass

    def _anim_changed(self) -> bool:
        """Animation ODER angezeigte Session geaendert -> ohne neue Abfrage
        nachschicken."""
        if self._last_payload is None:
            return False            # noch keine Zahlen -> nichts zu ergaenzen
        if not self._active():
            return False            # Geraet zeigt einen anderen PC
        try:
            return (self._current_anim() != self._last_anim
                    or self._current_session() != self._last_session
                    or (self._proto >= 2 and self._extras_messages() != self._last_extras))
        except Exception:
            return False

    def _extras_changed(self) -> bool:
        """Sessions- oder Kontenliste geaendert, unabhaengig von den
        Nutzungszahlen. Die Sessions kommen aus der lokalen Registry, nicht
        aus der API: scheitert die Nutzungsabfrage (kein Token, API weg),
        muessen sie trotzdem aktuell bleiben."""
        if self._proto < 2 or not self._active():
            return False
        try:
            return self._extras_messages() != self._last_extras
        except Exception:
            return False

    async def _send_extras_without_usage(self, client) -> None:
        """Nutzungsabfrage gescheitert: die Listen trotzdem schicken."""
        if self._proto >= 2 and self._active():
            await self._send_extras(client)

    def _extras_messages(self) -> dict:
        """Zusatzmeldungen fuer Protokoll 2, je Schluessel ein fertiger
        JSON-Text: "se<i>" = offene Session i (Name, Animation, Zustand,
        Kontext, dringendste zuerst, hoechstens SE_MAX), "t" = Anzahl der
        geschickten Sessions; ohne offene Session eine einzige Meldung "se"
        mit t=0. "ac<i>" = Konto i. Jede passt in ein BLE-Paket (MTU - 3),
        sonst wird ihr Name gekuerzt (Sessions bis SE_NAME_MIN, Konten bis 4).
        Weil jede "se"-Meldung "t" traegt, aendert eine neue Anzahl alle
        Session-Texte: sie gehen dann alle neu raus, und das Geraet kuerzt
        seine Liste schon an der ersten."""
        try:
            ex = self._extras_provider() or {}
        except Exception:
            ex = {}
        limit = max(20, self._mtu - 3)
        out = {}

        def enc(obj):
            return json.dumps(obj, separators=(",", ":"))

        sessions = (ex.get("se") or [])[:SE_MAX]
        if not sessions:
            out["se"] = enc({"k": "se", "t": 0})
        for i, x in enumerate(sessions):
            msg = {"k": "se", "i": i, "t": len(sessions), **x}
            msg["n"] = _ascii(msg.get("n"), SE_NAME_MAX).rstrip()
            # Gekuerzt endet der Name sonst auf " "; ein Schnitt, der danach
            # unter SE_NAME_MIN laege, wird uebersprungen.
            name = msg["n"]
            while len(enc(msg)) > limit and len(name) > SE_NAME_MIN:
                name = name[:-1]
                if len(name.rstrip()) >= SE_NAME_MIN:
                    msg["n"] = name.rstrip()
            out[f"se{i}"] = enc(msg)

        accounts = (ex.get("ac") or [])[:6]
        for i, a in enumerate(accounts):
            msg = dict(a, k="ac", i=i, c=len(accounts))
            msg["n"] = _ascii(msg.get("n"), 14)
            while len(enc(msg)) > limit and len(msg["n"]) > 4:
                msg["n"] = msg["n"][:-1]
            out[f"ac{i}"] = enc(msg)
        return out

    async def _send_extras(self, client) -> None:
        msgs = self._extras_messages()
        for key, text in msgs.items():
            if self._last_extras.get(key) == text:
                continue
            await client.write_gatt_char(RX_CHAR_UUID, text.encode(), response=False)
        self._last_extras = msgs

    def _system_message(self) -> bytes | None:
        """{"k":"sy",...} als ein BLE-Paket, oder None (kein Anbieter, Firmware
        unter Protokoll 2, Anbieter liefert nichts oder scheitert). Passt die
        Meldung nicht in MTU - 3, wird nur "cc" schrittweise auf die halbe
        Anzahl gemittelt; die anderen Felder bleiben immer. Reicht selbst ein
        einziger Kernwert nicht, geht diesmal nichts raus."""
        if self._system_provider is None or self._proto < 2:
            return None
        try:
            stats = self._system_provider()
            if not stats:
                return None
            msg = {"k": "sy", **stats}
            limit = max(20, self._mtu - 3)
            data = json.dumps(msg, separators=(",", ":")).encode()
            cc = list(msg.get("cc") or [])
            while len(data) > limit and len(cc) > 1:
                cc = _halve(cc)
                msg["cc"] = cc
                data = json.dumps(msg, separators=(",", ":")).encode()
        except Exception as e:
            if not self._sy_failed:
                self._sy_failed = True
                self._log(f"Clawdmeter: system stats failed: {e}")
            return None
        if len(data) > limit:
            if not self._sy_failed:
                self._sy_failed = True
                self._log(f"Clawdmeter: system stats do not fit MTU {self._mtu}")
            return None
        return data

    async def _maybe_send_system(self, client) -> None:
        """Systemwerte schicken, wenn SY_INTERVAL seit dem letzten Mal um ist.
        Etwas Spielraum, weil der Loop in 1-s-Scheiben aufwacht."""
        if not self._active():
            return                   # Geraet zeigt einen anderen PC
        now = time.monotonic()
        if self._last_sy is not None and now - self._last_sy < SY_INTERVAL - 0.2:
            return
        data = self._system_message()
        if data is None:
            return
        self._last_sy = now
        await client.write_gatt_char(RX_CHAR_UUID, data, response=False)

    def _current_session(self) -> tuple:
        """(Name, Hinweis, Kontext-%, Fenster), Name und Hinweis fuer das
        Display auf ASCII gebracht und gekuerzt: die Schriften des Geraets
        haben keine Umlaute oder Emoji. Kontext-% ist ein ganzzahliger Wert
        0-100 oder None (unbekannt), Fenster dann "" - so loest erst ein
        neuer ganzer Prozentpunkt ein Nachschicken aus."""
        try:
            d = self._session_provider() or {}
        except Exception:
            d = {}
        cx, cw = d.get("cx"), _ascii(d.get("cw"), 6)
        try:
            cx = max(0, min(100, int(cx))) if cx is not None and cw else None
        except (TypeError, ValueError):
            cx = None
        return _ascii(d.get("n"), 24), _ascii(d.get("h"), 24), cx, cw if cx is not None else ""

    def _current_anim(self) -> str:
        try:
            return str(self._anim_provider() or "")
        except Exception:
            return ""

    async def _send_once(self, client, poll: bool = True) -> None:
        import asyncio
        if poll or self._last_payload is None:
            payload = meta = None
            try:
                u = self._usage_provider()
            except Exception:
                u = None
            if u:
                payload, meta = payload_from_usage(u)
            else:
                token = read_token()
                if not token:
                    await self._send_extras_without_usage(client)
                    self._set(last_error=t("Kein Claude-Token gefunden"))
                    return
                payload, meta = await asyncio.to_thread(poll_usage_meta, token)
            if not payload:
                await self._send_extras_without_usage(client)
                self._set(last_error=t("API-Abfrage fehlgeschlagen"))
                return
            self._last_payload = payload
            # Die Limit-Ueberwachung mitfuettern, damit sie nicht dieselben
            # Header ein zweites Mal abfragen muss.
            if self._on_usage and meta:
                try:
                    self._on_usage(meta)
                except Exception:
                    pass

        if not self._active():
            # Das Geraet zeigt einen anderen PC: abgefragt ist (fuer die
            # Limit-Meldungen), geschickt wird nichts. Wird dieser PC wieder
            # angezeigt, geht ohnehin alles neu raus.
            return

        payload = dict(self._last_payload)
        anim = self._current_anim()
        payload["a"] = anim          # "" = Geraet entscheidet selbst
        self._last_anim = anim
        session = self._current_session()
        name, hint, cx, cw = session
        payload["n"] = name          # Session, die die Animation meint
        payload["h"] = hint          # z.B. "+2 working"
        if cx is not None:           # Kontext dieser Session, z.B. 33 % von "1M"
            payload["cx"] = cx
            payload["cw"] = cw
        self._last_session = session

        data = json.dumps(payload, separators=(",", ":")).encode()
        await client.write_gatt_char(RX_CHAR_UUID, data, response=False)
        if self._proto >= 2:
            await self._send_extras(client)
        self._set(last_send=time.time(), last_error=None)
        if poll:
            self._log(f"Clawdmeter: {payload['s']}% / {payload['w']}%"
                      + (f" [{anim}]" if anim else ""))


class UsageWatcher:
    """Haelt die Ratelimit-Werte aktuell, auch ohne Clawdmeter-Hardware.

    Der ClawdmeterLink fragt die Header nur ab solange eine BLE-Verbindung
    steht. Fuer die Limit-Benachrichtigungen brauchen wir die Werte aber
    unabhaengig davon, also pollt dieser Thread selbst -- allerdings nur
    wenn `is_covered()` False meldet, sonst wuerden zwei Poller dieselben
    Header holen und jeder Aufruf kostet echte Requests.

    Bewusst traege getaktet: den genauen Reset-Zeitpunkt liefert die API als
    Zeitstempel mit, den Rest erledigt ein Timer. Haeufiges Pollen wuerde
    daran nichts verbessern.
    """

    INTERVAL = 300.0        # 5 Minuten
    RETRY_INTERVAL = 60.0   # nach Fehlschlag frueher nochmal

    def __init__(self, on_usage, log=None, is_covered=None):
        self._on_usage = on_usage
        self._log = log or (lambda msg: None)
        self._is_covered = is_covered or (lambda: False)
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="usage-watcher")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            delay = self.INTERVAL
            if not self._is_covered():
                token = read_token()
                if token:
                    try:
                        _payload, meta = poll_usage_meta(token)
                    except Exception:
                        meta = {}
                    if meta:
                        try:
                            self._on_usage(meta)
                        except Exception as exc:
                            self._log(f"UsageWatcher: {exc}")
                    else:
                        delay = self.RETRY_INTERVAL
                else:
                    delay = self.RETRY_INTERVAL
            # In Scheiben warten, damit stop() sofort greift.
            waited = 0.0
            while waited < delay and not self._stop.is_set():
                self._stop.wait(1.0)
                waited += 1.0


# --------------------------------------------------------------------------
# Standalone-Test:  python clawdmeter.py
# --------------------------------------------------------------------------

if __name__ == "__main__":
    def _p(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    _p("Looking for the address...")
    addr = discover_address()
    _p(f"Address: {addr}")
    _p("Reading the token...")
    tok = read_token()
    _p("Token: " + ("found" if tok else "NOT found"))
    if tok:
        _p("Querying the API...")
        _pl, _mt = poll_usage_meta(tok)
        _p(f"Payload: {_pl}")
        _p(f"Meta:    {_mt}")

    link = ClawdmeterLink(log=_p)
    link.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        link.stop()
