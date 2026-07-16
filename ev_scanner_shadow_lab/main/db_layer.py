"""
EV Scanner AI - Shadow Intelligence System
main/db_layer.py - Layer di persistenza (il pezzo che mancava)
----------------------------------------------------------------
Fino a questo punto ogni modulo di ev_scanner_shadow_lab lavorava SOLO
su strutture dati Python pure (EventoInput, BetSettled, ecc.), mai su
una vera connessione al database - era la scelta giusta per testare la
logica in isolamento, ma significa che nessuno di quei moduli puo'
funzionare da solo su dati reali. Questo file e' il ponte.

Usa pymysql (pure Python, nessuna dipendenza da compilazione nativa a
differenza di mysqlclient - piu' adatto a un ambiente come XAMPP/locale
dove Malu potrebbe non avere un compilatore C configurato). Installare
con: pip install pymysql

CREDENZIALI: MAI hardcoded qui o altrove. Lette da variabili
d'ambiente (vedi .env.example in questa stessa cartella) tramite
python-dotenv. NOTA IMPORTANTE vista in config/config.php del progetto
PHP: le credenziali del database sono attualmente scritte IN CHIARO in
quel file (DB_HOST/DB_USER/DB_PASS/DB_NAME). Non e' un problema di
questo modulo Python, ma vale la pena spostarle anche li' in variabili
d'ambiente appena possibile - lo stesso file php potrebbe leggere da un
.env con una libreria come vlucas/phpdotenv.

MAPPING SCHEMA PRODUZIONE -> SHADOW LAB
----------------------------------------
La tabella `eventi` di produzione ha probabilita'/quote per TUTTE E 3
le selection insieme (prob_1/x/2, quota_1/x/2), mentre ogni modello
Shadow lavora su un EventoInput per singola selection. Ogni riga di
`eventi` viene quindi espansa in FINO A 3 EventoInputEsteso (una per
ogni selection con probabilita' valorizzata - alcuni sport non hanno
pareggio, quindi prob_x puo' essere NULL, vedi espandi_evento_in_candidate()).

smart_filter_score (0-1, atteso da Model C) non esiste come colonna
diretta: si deriva dal `voto` 1-10 gia' calcolato in produzione
(includes/functions.php::calcola_voto_bet()) con normalizzazione
lineare (voto-1)/9. Il voto pero' e' calcolato SOLO al momento del
tracciamento di una bet reale (scommesse.voto), non esiste per un
evento che non e' mai stato tracciato - per gli eventi ancora da
valutare (nessuna bet reale piazzata) si usa 0.5 (neutro), la stessa
convenzione di default gia' documentata in EventoInputEsteso.
----------------------------------------------------------------
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

try:
    import pymysql
    import pymysql.cursors
except ImportError as exc:
    raise ImportError(
        "pymysql non installato. Esegui: pip install pymysql "
        "(o pip install pymysql --break-system-packages su alcuni sistemi)"
    ) from exc

from models.model_a_conservative import EventoInput
from models.model_c_adaptive import EventoInputEsteso
from utils.stats_engine import BetSettled


# ==================================================================
# Configurazione connessione (da variabili d'ambiente, MAI hardcoded)
# ==================================================================
@dataclass
class ConfigDatabase:
    host: str
    user: str
    password: str
    database: str
    port: int = 3306
    charset: str = "utf8mb4"

    @staticmethod
    def da_environment() -> "ConfigDatabase":
        """
        Legge da variabili d'ambiente (DB_HOST, DB_USER, DB_PASSWORD,
        DB_NAME, DB_PORT opzionale). Se python-dotenv e' installato e un
        file .env e' presente nella working directory, load_dotenv()
        (chiamata dal chiamante, es. run_shadow_cycle.py, PRIMA di
        costruire questo oggetto - non e' responsabilita' di questa
        funzione caricare il .env, solo leggere os.environ) lo popola
        automaticamente in os.environ.
        """
        host = os.environ.get("DB_HOST")
        user = os.environ.get("DB_USER")
        password = os.environ.get("DB_PASSWORD")
        database = os.environ.get("DB_NAME")
        missing = [n for n, v in [("DB_HOST", host), ("DB_USER", user), ("DB_PASSWORD", password), ("DB_NAME", database)] if not v]
        if missing:
            raise ValueError(
                f"Variabili d'ambiente mancanti per la connessione DB: {missing}. "
                "Copia .env.example in .env e valorizzale, poi assicurati che il "
                "chiamante esegua load_dotenv() prima di ConfigDatabase.da_environment()."
            )
        return ConfigDatabase(
            host=host, user=user, password=password, database=database,
            port=int(os.environ.get("DB_PORT", "3306")),
        )


@contextmanager
def connessione(cfg: ConfigDatabase):
    """
    Context manager per una connessione pymysql con DictCursor (righe
    come dict invece di tuple posizionali - piu' leggibile e meno
    fragile a riordinamenti futuri delle colonne). Commit esplicito solo
    se il blocco `with` completa senza eccezioni; rollback automatico
    altrimenti - stesso principio di sicurezza delle transazioni gia'
    visto nel PDO di produzione (config/database.php).
    """
    conn = pymysql.connect(
        host=cfg.host, user=cfg.user, password=cfg.password, database=cfg.database,
        port=cfg.port, charset=cfg.charset, cursorclass=pymysql.cursors.DictCursor,
        # Timezone Europe/Rome coerente con config/config.php di
        # produzione (date_default_timezone_set('Europe/Rome') +
        # SET time_zone su PDO) - senza questo, i timestamp scritti da
        # Python potrebbero non allinearsi con quelli scritti da PHP.
        init_command="SET time_zone = '+02:00'",
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==================================================================
# Lettura: eventi da valutare (per l'inferenza dei modelli)
# ==================================================================
def _normalizza_voto_a_score(voto: Optional[float]) -> float:
    """voto 1-10 -> smart_filter_score 0-1. None (evento mai tracciato) -> 0.5 neutro."""
    if voto is None:
        return 0.5
    return round(max(0.0, min(1.0, (float(voto) - 1) / 9)), 4)


def espandi_evento_in_candidate(riga_evento: dict) -> List[EventoInputEsteso]:
    """
    Da una riga della tabella `eventi` (dict, come restituito da
    DictCursor), produce fino a 3 EventoInputEsteso (uno per selection
    1/X/2), saltando le selection senza probabilita' valorizzata
    (sport senza pareggio: prob_x sara' NULL).

    Priorita' quota, replicando la stessa logica gia' in uso lato PHP
    (vedi commento su quota_real_* in sql/schema.sql): quota_real_N
    (odds-api.io, quota reale di mercato) ha priorita' su quota_N
    (Sbancobet, valore di partenza pre-compilato) quando disponibile.
    """
    candidate = []
    mappa_selection = [
        ("1", "prob_1", "fair_1", "quota_1", "quota_real_1"),
        ("X", "prob_x", "fair_x", "quota_x", "quota_real_x"),
        ("2", "prob_2", "fair_2", "quota_2", "quota_real_2"),
    ]

    for selection, campo_prob, campo_fair, campo_quota, campo_quota_real in mappa_selection:
        prob = riga_evento.get(campo_prob)
        if prob is None:
            continue  # sport senza questo esito (es. basket senza X), o dato non ancora calcolato

        quota = riga_evento.get(campo_quota_real) or riga_evento.get(campo_quota)
        if quota is None or float(quota) <= 1.0:
            continue  # nessuna quota utilizzabile per questa selection

        fair = riga_evento.get(campo_fair)

        candidate.append(EventoInputEsteso(
            event_id=riga_evento["id"],
            campionato=f"{riga_evento['sport']} - {riga_evento['campionato']}",
            mercato="1X2",
            selection=selection,
            probability_pct=float(prob),
            bookmaker_odds=float(quota),
            fair_odds=float(fair) if fair is not None else (100.0 / float(prob) if prob else 0.0),
            clv_stimato_pct=None,  # il CLV STIMATO (pre-bet) non e' oggi calcolato in produzione, solo il CLV REALE post-settlement (scommesse.clv_pct) - vedi nota in README
            smart_filter_score=0.5,  # nessun voto ancora esistente per un evento non tracciato: neutro di default
            orario_evento=riga_evento["event_date"] if isinstance(riga_evento["event_date"], datetime) else None,
        ))

    return candidate


def leggi_eventi_da_valutare(cfg: ConfigDatabase, giorni_finestra: int = 3) -> List[EventoInputEsteso]:
    """
    Legge dalla tabella `eventi` di produzione gli eventi futuri entro
    `giorni_finestra` giorni (stessa finestra temporale ragionevole di
    uno scan reale: non ha senso valutare eventi troppo lontani nel
    tempo, le quote cambieranno comunque prima del fischio d'inizio),
    e li espande in candidate per-selection.

    SOLA LETTURA da `eventi` - coerente con l'isolamento dichiarato fin
    dallo Step 1 (schema.sql): lo Shadow Lab non scrive mai su tabelle
    di produzione.
    """
    query = """
        SELECT id, sport, campionato, home_team, away_team, event_date,
               prob_1, prob_x, prob_2, fair_1, fair_x, fair_2,
               quota_1, quota_x, quota_2, quota_real_1, quota_real_x, quota_real_2
        FROM eventi
        WHERE event_date >= NOW() AND event_date <= DATE_ADD(NOW(), INTERVAL %s DAY)
        ORDER BY event_date ASC
    """
    with connessione(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (giorni_finestra,))
            righe = cur.fetchall()

    candidate_totali = []
    for riga in righe:
        candidate_totali.extend(espandi_evento_in_candidate(riga))
    return candidate_totali


# ==================================================================
# Lettura: storico shadow_bets settled (per Sharpe pesi Model D e per i backtest)
# ==================================================================
def leggi_storico_shadow_settled(
    cfg: ConfigDatabase,
    model_source: str,
    giorni_finestra: Optional[int] = None,
) -> List[BetSettled]:
    """
    Legge da shadow_bets le bet gia' concluse (result IN vinta/persa/void)
    per UN modello specifico, opzionalmente limitate a una finestra
    temporale (usata da calcola_pesi_sharpe(), Step 5, che vuole solo gli
    ultimi N giorni - vedi ModelDConfig.finestra_sharpe_giorni).
    """
    query = """
        SELECT settled_at, stake_shadow, profit_loss_shadow, result,
               ev_pct, clv_stimato_pct, kelly_fraction_usata
        FROM shadow_bets
        WHERE model_source = %s AND result IN ('vinta', 'persa', 'void') AND settled_at IS NOT NULL
    """
    parametri = [model_source]
    if giorni_finestra is not None:
        query += " AND settled_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        parametri.append(giorni_finestra)
    query += " ORDER BY settled_at ASC"

    with connessione(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(parametri))
            righe = cur.fetchall()

    return [
        BetSettled(
            data_settlement=r["settled_at"].date() if isinstance(r["settled_at"], datetime) else r["settled_at"],
            stake=float(r["stake_shadow"]), profit_loss=float(r["profit_loss_shadow"]), result=r["result"],
            ev_teorico_pct=float(r["ev_pct"]) if r["ev_pct"] is not None else None,
            clv_pct=float(r["clv_stimato_pct"]) if r["clv_stimato_pct"] is not None else None,
            kelly_fraction_usata=float(r["kelly_fraction_usata"]) if r["kelly_fraction_usata"] is not None else None,
        )
        for r in righe
    ]


def leggi_storico_produzione_settled(cfg: ConfigDatabase, giorni_finestra: Optional[int] = None) -> List[BetSettled]:
    """
    Equivalente di leggi_storico_shadow_settled() ma per la tabella
    `scommesse` REALE di produzione - serve al Promotion Engine (Step 7)
    per confrontare un candidato shadow con quello che il Filtro
    Ufficiale ha realmente fatto.
    """
    query = """
        SELECT created_at, stake, profit_loss, result, ev, clv_pct
        FROM scommesse
        WHERE result IN ('vinta', 'persa', 'void') AND stake IS NOT NULL
    """
    parametri = []
    if giorni_finestra is not None:
        query += " AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        parametri.append(giorni_finestra)
    query += " ORDER BY created_at ASC"

    with connessione(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(query, tuple(parametri))
            righe = cur.fetchall()

    return [
        BetSettled(
            data_settlement=r["created_at"].date() if isinstance(r["created_at"], datetime) else r["created_at"],
            stake=float(r["stake"]), profit_loss=float(r["profit_loss"]) if r["profit_loss"] is not None else 0.0,
            result=r["result"], ev_teorico_pct=float(r["ev"]) if r["ev"] is not None else None,
            clv_pct=float(r["clv_pct"]) if r["clv_pct"] is not None else None,
        )
        for r in righe
    ]


# ==================================================================
# Scrittura: shadow_bets (INSERT con idempotenza)
# ==================================================================
@dataclass
class RigaShadowBetDaInserire:
    """Corrisponde 1:1 alle colonne INSERT-abili di shadow_bets (vedi schema.sql)."""
    event_id: int
    model_source: str
    model_version_id: int
    selection: str
    probability_stimata: float
    fair_odds: float
    bookmaker_odds: float
    ev_pct: float
    kelly_fraction_usata: float
    stake_shadow: float
    confidence_score: float
    clv_stimato_pct: Optional[float] = None
    features_snapshot_json: Optional[str] = None  # gia' serializzato JSON dal chiamante
    automl_config_id: Optional[int] = None


def inserisci_shadow_bets(cfg: ConfigDatabase, righe: List[RigaShadowBetDaInserire]) -> int:
    """
    INSERT ... ON DUPLICATE KEY UPDATE su (model_source, event_id,
    selection, automl_config_id) - la UNIQUE KEY di shadow_bets (vedi
    schema.sql) garantisce idempotenza: se main_shadow_engine.py viene
    rilanciato sullo stesso batch di eventi (es. dopo un crash a meta'
    ciclo), non duplica le righe, aggiorna quella esistente. Ritorna il
    numero di righe effettivamente scritte.
    """
    if not righe:
        return 0

    query = """
        INSERT INTO shadow_bets
            (event_id, model_source, model_version_id, automl_config_id, selection,
             probability_stimata, fair_odds, bookmaker_odds, ev_pct,
             kelly_fraction_usata, stake_shadow, confidence_score,
             clv_stimato_pct, features_snapshot_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            probability_stimata = VALUES(probability_stimata),
            fair_odds = VALUES(fair_odds),
            bookmaker_odds = VALUES(bookmaker_odds),
            ev_pct = VALUES(ev_pct),
            kelly_fraction_usata = VALUES(kelly_fraction_usata),
            stake_shadow = VALUES(stake_shadow),
            confidence_score = VALUES(confidence_score),
            clv_stimato_pct = VALUES(clv_stimato_pct),
            features_snapshot_json = VALUES(features_snapshot_json)
    """
    valori = [
        (
            r.event_id, r.model_source, r.model_version_id, r.automl_config_id, r.selection,
            r.probability_stimata, r.fair_odds, r.bookmaker_odds, r.ev_pct,
            r.kelly_fraction_usata, r.stake_shadow, r.confidence_score,
            r.clv_stimato_pct, r.features_snapshot_json,
        )
        for r in righe
    ]

    with connessione(cfg) as conn:
        with conn.cursor() as cur:
            cur.executemany(query, valori)
            return cur.rowcount


# ==================================================================
# Scrittura: shadow_run_log (diagnostica di ogni esecuzione)
# ==================================================================
def logga_run(
    cfg: ConfigDatabase, channel: str, level: str, message: str,
    eventi_processati: Optional[int] = None, bet_shadow_generate: Optional[int] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Scrive una riga in shadow_run_log (vedi schema.sql) - stesso ruolo diagnostico dello scheduler_log gia' in uso lato PHP."""
    query = """
        INSERT INTO shadow_run_log (channel, level, message, eventi_processati, bet_shadow_generate, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s)
    """
    with connessione(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (channel, level, message[:1000], eventi_processati, bet_shadow_generate, duration_ms))


# ==================================================================
# Lettura: versione attiva del sistema (per model_version_id)
# ==================================================================
def leggi_versione_attiva(cfg: ConfigDatabase) -> dict:
    """Ritorna la riga di shadow_model_versions con is_attiva=1 (vedi schema.sql, dovrebbe essere sempre esattamente una)."""
    query = "SELECT id, versione, parametri_snapshot_json FROM shadow_model_versions WHERE is_attiva = 1 LIMIT 1"
    with connessione(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            riga = cur.fetchone()
    if riga is None:
        raise RuntimeError(
            "Nessuna versione attiva trovata in shadow_model_versions. "
            "Verifica che schema.sql sia stato importato correttamente (il bootstrap v1.0 dovrebbe già esistere)."
        )
    return riga
