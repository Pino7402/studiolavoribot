"""
Google Sheets helper — foglio Lavori
Colonne: ID | Data | Descrizione | Prezzo | Nota | Tempo
"""
import os
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = ["ID", "Data", "Descrizione", "Prezzo", "Nota", "Tempo"]
SHEET_NAME = "Lavori"


def get_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON non impostato")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet():
    gc = get_client()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=2000, cols=len(HEADERS))
        ws.append_row(HEADERS)
    return ws


def _rows_to_lavori(rows):
    lavori = []
    for r in rows:
        r += [""] * (len(HEADERS) - len(r))
        lavori.append({
            "id":          r[0],
            "data":        r[1],
            "descrizione": r[2],
            "prezzo":      r[3],
            "nota":        r[4],
            "tempo":       r[5],
        })
    return lavori


def get_all_lavori():
    ws = get_sheet()
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    return _rows_to_lavori(rows[1:])


def add_lavoro(data_str: str, descrizione: str, prezzo: float, nota: str = "", tempo: str = ""):
    ws = get_sheet()
    record_id = str(int(datetime.now().timestamp() * 1000))
    row = [record_id, data_str, descrizione.strip(), str(prezzo), nota.strip(), tempo.strip()]
    ws.append_row(row)
    return _rows_to_lavori([row])[0]


def cerca_lavori(query: str):
    q = query.lower().strip()
    return [l for l in get_all_lavori() if q in l["descrizione"].lower()]


def get_lavori_oggi():
    oggi = date.today().strftime("%Y-%m-%d")
    return [l for l in get_all_lavori() if l["data"] == oggi]


def get_lavori_mese(anno: int, mese: int):
    prefix = f"{anno}-{mese:02d}"
    return [l for l in get_all_lavori() if l["data"].startswith(prefix)]


def get_totale_anno(anno: int):
    all_l = get_all_lavori()
    prefix = str(anno)
    total = 0.0
    for l in all_l:
        if l["data"].startswith(prefix):
            try:
                total += float(l["prezzo"])
            except ValueError:
                pass
    return total


def _lavoro_to_row(l: dict) -> list:
    """Normalizza un lavoro JSON dell'app web in una riga del foglio."""
    data_str = l.get("data", "")
    descrizione = l.get("descrizione", "")
    prezzo = l.get("prezzo", 0)
    nota = l.get("nota", "") or ""
    tempo_obj = l.get("tempo")
    tempo_str = ""
    if tempo_obj and isinstance(tempo_obj, dict):
        ore = tempo_obj.get("ore", 0)
        min_ = tempo_obj.get("min", 0)
        tempo_str = f"{ore}h{min_:02d}m" if ore or min_ else ""
    record_id = str(l.get("id", "")) or str(int(datetime.now().timestamp() * 1000))
    return [record_id, data_str, descrizione, str(prezzo), nota, tempo_str]


def upsert_lavoro(lavoro: dict):
    """Aggiunge o aggiorna un singolo lavoro per ID (sync real-time dall'app web)."""
    ws = get_sheet()
    row = _lavoro_to_row(lavoro)
    record_id = row[0]
    cell = ws.find(record_id, in_column=1)
    if cell:
        ws.update(range_name=f"A{cell.row}:F{cell.row}", values=[row])
    else:
        ws.append_row(row)


def delete_lavoro(record_id: str):
    ws = get_sheet()
    cell = ws.find(record_id, in_column=1)
    if not cell:
        return False
    ws.delete_rows(cell.row)
    return True


def preview_merge(lavori_list: list) -> dict:
    """
    Calcola quanti lavori sarebbero nuovi/aggiornati/invariati SENZA scrivere nulla.
    Usata per mostrare l'anteprima nel popup di conferma.
    """
    ws = get_sheet()
    all_rows = ws.get_all_values()
    existing_rows = all_rows[1:] if len(all_rows) > 1 else []

    id_to_row = {}
    for r in existing_rows:
        r = list(r) + [""] * (len(HEADERS) - len(r))
        rid = str(r[0])
        if rid:
            id_to_row[rid] = r[:len(HEADERS)]

    n_nuovi = n_agg = n_inv = 0
    for l in lavori_list:
        try:
            new_row = _lavoro_to_row(l)
            rid = new_row[0]
            if not rid:
                continue
            if rid in id_to_row:
                old = id_to_row[rid]
                if [str(x) for x in old] != [str(x) for x in new_row]:
                    n_agg += 1
                else:
                    n_inv += 1
            else:
                n_nuovi += 1
        except Exception:
            continue
    return {"nuovi": n_nuovi, "aggiornati": n_agg, "invariati": n_inv}


def merge_from_json(lavori_list: list) -> dict:
    """
    Merge incrementale: confronta i lavori del backup con quelli gia' su Sheets
    e applica solo le differenze (nuovi + modificati). Non cancella nulla.
    Ritorna {nuovi, aggiornati, invariati, totale_dopo}.
    """
    ws = get_sheet()
    all_rows = ws.get_all_values()
    existing_rows = all_rows[1:] if len(all_rows) > 1 else []

    # Mappa id -> (riga_1based, valori_normalizzati)
    id_to_row = {}
    for i, r in enumerate(existing_rows):
        r = list(r) + [""] * (len(HEADERS) - len(r))
        rid = str(r[0])
        if rid:
            id_to_row[rid] = (i + 2, r[:len(HEADERS)])  # +2 = 1-based + header

    nuovi_rows = []
    aggiornamenti = []  # [(range, [valori])]
    n_invariati = 0

    for l in lavori_list:
        try:
            new_row = _lavoro_to_row(l)
            record_id = new_row[0]
            if not record_id:
                continue
            if record_id in id_to_row:
                row_num, old_row = id_to_row[record_id]
                if [str(x) for x in old_row] != [str(x) for x in new_row]:
                    aggiornamenti.append((f"A{row_num}:F{row_num}", new_row))
                else:
                    n_invariati += 1
            else:
                nuovi_rows.append(new_row)
        except Exception:
            continue

    if aggiornamenti:
        body = [{"range": rng, "values": [vals]} for rng, vals in aggiornamenti]
        ws.batch_update(body, value_input_option="USER_ENTERED")

    if nuovi_rows:
        ws.append_rows(nuovi_rows, value_input_option="USER_ENTERED")

    return {
        "nuovi": len(nuovi_rows),
        "aggiornati": len(aggiornamenti),
        "invariati": n_invariati,
        "totale_dopo": len(existing_rows) + len(nuovi_rows),
    }


def import_from_json(lavori_list: list) -> int:
    """
    DEPRECATA — usa merge_from_json per backup incrementale.
    Mantenuta per compatibilita': sovrascrive tutto il foglio.
    """
    ws = get_sheet()
    ws.resize(1)
    rows = [_lavoro_to_row(l) for l in lavori_list if l]
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)


def export_to_json() -> list:
    """Esporta tutti i lavori in formato compatibile con l'app web."""
    lavori = get_all_lavori()
    result = []
    for l in lavori:
        tempo = None
        if l["tempo"]:
            try:
                parts = l["tempo"].replace("h", ":").replace("m", "").split(":")
                tempo = {"ore": int(parts[0]), "min": int(parts[1])}
            except Exception:
                pass
        result.append({
            "id": int(l["id"]) if l["id"].isdigit() else l["id"],
            "data": l["data"],
            "descrizione": l["descrizione"],
            "prezzo": float(l["prezzo"]) if l["prezzo"] else 0,
            "nota": l["nota"],
            "tempo": tempo,
        })
    return result
