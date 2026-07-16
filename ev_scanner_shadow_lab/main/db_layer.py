"""
EV Scanner AI - Shadow Intelligence System
main/db_layer.py - Layer di persistenza HTTP-based (sostituisce pymysql)
------------------------------------------------------------
QUESTO FILE SOSTITUISCE COMPLETAMENTE la versione pymysql originale.
L'interfaccia pubblica (firme delle funzioni, nomi, tipi di ritorno)
RESTA IDENTICA, quindi main_shadow_engine.py NON VA MODIFICATO.

Architettura:
- Le chiamate DB vanno all'endpoint PHP shadow_db_proxy.php su InfinityFree
- Autenticazione: Bearer Token (SHADOW_API_TOKEN da .env)
- Trasporto: HTTPS + requests con timeout e retry
- Stessa logica di transazione "simulata": ogni operazione è atomica lato PHP
-------------------------------------------------------------
"""

from __future__ import annotations

import os
import json
import time
import logging
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Dict, List, Optional, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        pass

# Import dei tipi usati dall'interfaccia pubblica (devono restare invariati)
from models.model_a_conservative import EventoInput
from models.model_c_adaptive import EventoInputEsteso
from utils.stats_engine import BetSettled

# Carica .env all'import (così ConfigDatabase.da_environment() trova le variabili)
load_dotenv()

# ==================================================================
# Configurazione connessione HTTP (sostituisce ConfigDatabase pymysql)
# ==================================================================

@dataclass
class ConfigDatabase:
    """
    Configurazione per il client HTTP. Non contiene credenziali DB dirette,
    ma URL endpoint e token API. Le credenziali DB reali restano SOLO
    nel config.php lato server (InfinityFree).
    """
    api_url: str
    api_token: str
    timeout_seconds: int = 30
    max_retries: int = 3
    backoff_factor: float = 0.5

    @staticmethod
    def da_environment() -> "ConfigDatabase":
        """
        Legge da variabili d'ambiente:
        - SHADOW_API_URL: es. https://dash.infinityfree.com/worker/shadow_db_proxy.php
        - SHADOW_API_TOKEN: deve coincidere con WORKER_SECRET_TOKEN in config.php
        - SHADOW_TIMEOUT_SECONDS (opzionale, default 30)
        - SHADOW_MAX_RETRIES (opzionale, default 3)
        """
        api_url = os.environ.get("SHADOW_API_URL")
        api_token = os.environ.get("SHADOW_API_TOKEN")
        timeout = int(os.environ.get("SHADOW_TIMEOUT_SECONDS", "30"))
        max_retries = int(os.environ.get("SHADOW_MAX_RETRIES", "3"))

        missing = [n for n, v in [("SHADOW_API_URL", api_url), ("SHADOW_API_TOKEN", api_token)] if not v]
        if missing:
            raise ValueError(
                f"Variabili d'ambiente mancanti per Shadow DB Proxy: {missing}. "
                "Impostale nel file .env (vedi .env.example)."
            )

        return ConfigDatabase(
            api_url=api_url.rstrip('/'),
            api_token=api_token,
            timeout_seconds=timeout,
            max_retries=max_retries,
        )


# ==================================================================
# Client HTTP con retry automatico (sostituisce contextmanager pymysql)
# ==================================================================

class ShadowDBClient:
    """
    Client HTTP thread-safe per worker/resolve_bets_web.php (azioni shadow_*).

    NOTA STORICA (bug corretto): questo client parlava in origine con
    shadow_db_proxy.php (auth via header 'Authorization: Bearer ...', payload
    sempre come body JSON). Quell'endpoint, chiamato da GitHub Actions
    (client non-browser, IP esterno "nuovo" ad ogni run), faceva scattare
    quasi sempre la JS-challenge anti-bot di InfinityFree: la risposta era
    una paginetta HTML/JS (slowAES) invece del JSON atteso.
    worker/resolve_bets_web.php espone le STESSE azioni shadow_* (vedi fondo
    di quel file) ma è già l'endpoint "collaudato" per le chiamate esterne
    di GitHub Actions e usa un protocollo diverso:
      - autenticazione: parametro di query string 'token' (confrontato con
        WORKER_SECRET_TOKEN), NON un header Authorization Bearer;
      - per le azioni di SOLA LETTURA, il payload va in query string
        (il PHP legge $_GET), non nel body JSON;
      - solo le azioni di SCRITTURA (shadow_insert_shadow_bets,
        shadow_update_shadow_bets, shadow_log_run) leggono il payload dal
        body JSON (php://input).
    """

    # Azioni che il PHP legge dal body JSON (php://input); tutte le altre
    # sono lette da $_GET e vanno quindi in query string.
    _WRITE_ACTIONS = {'shadow_insert_shadow_bets', 'shadow_update_shadow_bets', 'shadow_log_run'}

    def __init__(self, config: ConfigDatabase):
        self.config = config
        self._session = requests.Session()
        self._session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': 'EVScanner-ShadowEngine/1.0',
        })

        # Strategia retry per errori transienti (5xx, timeout, connection error)
        retry_strategy = Retry(
            total=config.max_retries,
            backoff_factor=config.backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _call(self, action: str, payload: dict) -> dict:
        """
        Esegue una chiamata POST all'endpoint resolve_bets_web.php.
        Restituisce il campo 'data' della risposta JSON se success=True.
        Solleva eccezione con messaggio dettagliato altrimenti.
        """
        url = self.config.api_url
        is_write = action in self._WRITE_ACTIONS

        # token + action vanno sempre in query string (il PHP li legge da
        # $_GET['token']/$_GET['action'] con fallback a $_POST).
        query_params = {'action': action, 'token': self.config.api_token}

        if not is_write:
            # Azioni di lettura: il PHP fa $payload = $_GET, quindi ogni
            # parametro deve stare in query string, non nel body.
            for key, value in payload.items():
                if value is not None:
                    query_params[key] = value

        try:
            if is_write:
                response = self._session.post(
                    url,
                    params=query_params,
                    json=payload,
                    timeout=self.config.timeout_seconds,
                )
            else:
                response = self._session.post(
                    url,
                    params=query_params,
                    timeout=self.config.timeout_seconds,
                )
        except requests.exceptions.Timeout:
            raise TimeoutError(f"Timeout ({self.config.timeout_seconds}s) chiamando {action} su {url}")
        except requests.exceptions.ConnectionError as e:
            raise ConnectionError(f"Errore di connessione a {url}: {e}")

        # Parsing risposta
        try:
            data = response.json()
        except json.JSONDecodeError:
            raise RuntimeError(f"Risposta non-JSON da {action} (HTTP {response.status_code}): {response.text[:200]}")

        if not response.ok:
            error_msg = data.get('error', f'HTTP {response.status_code}')
            if response.status_code == 401:
                raise PermissionError(f"Token non valido o scaduto per {action}: {error_msg}")
            if response.status_code == 403:
                raise PermissionError(f"Accesso negato per {action}: {error_msg}")
            raise RuntimeError(f"Errore API {action}: {error_msg}")

        if not data.get('success', False):
            raise RuntimeError(f"API {action} ha restituito success=false: {data.get('error', 'sconosciuto')}")

        return data.get('data', {})

    # ------------------------------------------------------------
    # Metodi pubblici: interfaccia identica alle funzioni originali
    # ------------------------------------------------------------

    def leggi_storico_shadow_settled(
        self, model_source: str, giorni_finestra: Optional[int] = None
    ) -> List[BetSettled]:
        data = self._call('shadow_read_shadow_settled', {
            'model_source': model_source,
            'giorni_finestra': giorni_finestra,
        })
        rows = data.get('rows', [])
        return [self._row_to_bet_settled(r) for r in rows]

    def leggi_storico_produzione_settled(
        self, giorni_finestra: Optional[int] = None
    ) -> List[BetSettled]:
        data = self._call('shadow_read_production_settled', {
            'giorni_finestra': giorni_finestra,
        })
        rows = data.get('rows', [])
        return [self._row_to_bet_settled_produzione(r) for r in rows]

    def inserisci_shadow_bets(self, righe: List["RigaShadowBetDaInserire"]) -> int:
        if not righe:
            return 0
        # Converti dataclass in dict serializzabili
        righe_serializzabili = [asdict(r) for r in righe]
        # features_snapshot_json è già stringa JSON nel dataclass
        data = self._call('shadow_insert_shadow_bets', {'righe': righe_serializzabili})
        return data.get('inserted', 0)

    def aggiorna_stato_shadow_bets(self, aggiornamenti: List[dict]) -> int:
        """
        aggiornamenti: lista di dict con chiavi id, result, profit_loss_shadow,
        closing_odds (opzionale), beating_closing_line (opzionale)
        """
        if not aggiornamenti:
            return 0
        data = self._call('shadow_update_shadow_bets', {'aggiornamenti': aggiornamenti})
        return data.get('updated', 0)

    def logga_run(
        self,
        channel: str,
        level: str,
        message: str,
        eventi_processati: Optional[int] = None,
        bet_shadow_generate: Optional[int] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        self._call('shadow_log_run', {
            'channel': channel,
            'level': level,
            'message': message,
            'eventi_processati': eventi_processati,
            'bet_shadow_generate': bet_shadow_generate,
            'duration_ms': duration_ms,
        })

    def leggi_versione_attiva(self) -> dict:
        return self._call('shadow_read_version', {})

    # ------------------------------------------------------------
    # Helpers conversione righe
    # ------------------------------------------------------------

    def _row_to_bet_settled(self, row: dict) -> BetSettled:
        """Converte riga shadow_bets in BetSettled (stesso formato pymysql)"""
        settled_at = row.get('settled_at')
        if isinstance(settled_at, str):
            try:
                settled_dt = datetime.fromisoformat(settled_at.replace('Z', '+00:00'))
                data_settlement = settled_dt.date()
            except Exception:
                data_settlement = date.today()
        else:
            data_settlement = date.today()

        return BetSettled(
            data_settlement=data_settlement,
            stake=float(row['stake_shadow']),
            profit_loss=float(row['profit_loss_shadow']),
            result=row['result'],
            ev_teorico_pct=float(row['ev_pct']) if row.get('ev_pct') is not None else None,
            clv_pct=float(row['clv_stimato_pct']) if row.get('clv_stimato_pct') is not None else None,
            kelly_fraction_usata=float(row['kelly_fraction_usata']) if row.get('kelly_fraction_usata') is not None else None,
        )

    def _row_to_bet_settled_produzione(self, row: dict) -> BetSettled:
        created_at = row.get('created_at')
        if isinstance(created_at, str):
            try:
                created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                data_settlement = created_dt.date()
            except Exception:
                data_settlement = date.today()
        else:
            data_settlement = date.today()

        return BetSettled(
            data_settlement=data_settlement,
            stake=float(row['stake']),
            profit_loss=float(row['profit_loss']) if row.get('profit_loss') is not None else 0.0,
            result=row['result'],
            ev_teorico_pct=float(row['ev']) if row.get('ev') is not None else None,
            clv_pct=float(row['clv_pct']) if row.get('clv_pct') is not None else None,
        )


# ==================================================================
# Dataclass per INSERT (invariato rispetto versione pymysql)
# ==================================================================

@dataclass
class RigaShadowBetDaInserire:
    """Corrisponde 1:1 alle colonne INSERT-abili di shadow_bets."""
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
    features_snapshot_json: Optional[str] = None
    automl_config_id: Optional[int] = None


# ==================================================================
# Funzioni di comodo a livello modulo (stessa firma del vecchio db_layer)
# ==================================================================

# Client singleton (inizializzato lazy al primo uso)
_DB_CLIENT: Optional[ShadowDBClient] = None
_DB_CONFIG: Optional[ConfigDatabase] = None


def _get_client() -> ShadowDBClient:
    global _DB_CLIENT, _DB_CONFIG
    if _DB_CLIENT is None:
        if _DB_CONFIG is None:
            _DB_CONFIG = ConfigDatabase.da_environment()
        _DB_CLIENT = ShadowDBClient(_DB_CONFIG)
    return _DB_CLIENT


def _reset_client_for_testing() -> None:
    """Solo per test: forza reinizializzazione client."""
    global _DB_CLIENT, _DB_CONFIG
    _DB_CLIENT = None
    _DB_CONFIG = None


# ------------------------------------------------------------
# Lettura: eventi da valutare (da tabella `eventi` produzione)
# ------------------------------------------------------------

def _normalizza_voto_a_score(voto: Optional[float]) -> float:
    if voto is None:
        return 0.5
    return round(max(0.0, min(1.0, (float(voto) - 1) / 9)), 4)


def espandi_evento_in_candidate(riga_evento: dict) -> List[EventoInputEsteso]:
    """
    Da una riga della tabella `eventi` (dict), produce fino a 3 EventoInputEsteso.
    Logica identica alla versione pymysql.
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
            continue

        quota = riga_evento.get(campo_quota_real) or riga_evento.get(campo_quota)
        if quota is None or float(quota) <= 1.0:
            continue

        fair = riga_evento.get(campo_fair)

        candidate.append(EventoInputEsteso(
            event_id=riga_evento["id"],
            campionato=f"{riga_evento['sport']} - {riga_evento['campionato']}",
            mercato="1X2",
            selection=selection,
            probability_pct=float(prob),
            bookmaker_odds=float(quota),
            fair_odds=float(fair) if fair is not None else (100.0 / float(prob) if prob else 0.0),
            clv_stimato_pct=None,
            smart_filter_score=0.5,
            orario_evento=riga_evento["event_date"] if isinstance(riga_evento["event_date"], datetime) else None,
        ))

    return candidate


def leggi_eventi_da_valutare(cfg: ConfigDatabase, giorni_finestra: int = 3) -> List[EventoInputEsteso]:
    """
    Legge dalla tabella `eventi` di PRODUZIONE gli eventi futuri entro
    `giorni_finestra` giorni e li espande in candidate per-selection.
    Ora supportata via HTTP proxy (action: shadow_read_events).
    """
    data = _get_client()._call('shadow_read_events', {'giorni_finestra': giorni_finestra})
    rows = data.get('rows', [])

    candidate_totali = []
    for riga in rows:
        candidate_totali.extend(espandi_evento_in_candidate(riga))
    return candidate_totali


# ------------------------------------------------------------
# Lettura: storico shadow_bets settled (per Sharpe Model D)
# ------------------------------------------------------------

def leggi_storico_shadow_settled(
    cfg: ConfigDatabase,
    model_source: str,
    giorni_finestra: Optional[int] = None,
) -> List[BetSettled]:
    return _get_client().leggi_storico_shadow_settled(model_source, giorni_finestra)


def leggi_storico_produzione_settled(
    cfg: ConfigDatabase,
    giorni_finestra: Optional[int] = None,
) -> List[BetSettled]:
    return _get_client().leggi_storico_produzione_settled(giorni_finestra)


# ------------------------------------------------------------
# Scrittura: shadow_bets (INSERT idempotente)
# ------------------------------------------------------------

def inserisci_shadow_bets(cfg: ConfigDatabase, righe: List[RigaShadowBetDaInserire]) -> int:
    return _get_client().inserisci_shadow_bets(righe)


# ------------------------------------------------------------
# Scrittura: shadow_run_log (diagnostica)
# ------------------------------------------------------------

def logga_run(
    cfg: ConfigDatabase,
    channel: str,
    level: str,
    message: str,
    eventi_processati: Optional[int] = None,
    bet_shadow_generate: Optional[int] = None,
    duration_ms: Optional[int] = None,
) -> None:
    _get_client().logga_run(channel, level, message, eventi_processati, bet_shadow_generate, duration_ms)


# ------------------------------------------------------------
# Aggiornamento stato shadow_bets (settlement)
# ------------------------------------------------------------

def aggiorna_stato_shadow_bets(cfg: ConfigDatabase, aggiornamenti: List[dict]) -> int:
    """
    aggiornamenti: lista di dict con chiavi:
    - id (int): PK shadow_bets.id
    - result (str): 'vinta'|'persa'|'void'
    - profit_loss_shadow (float)
    - closing_odds (float, opzionale)
    - beating_closing_line (bool, opzionale)
    """
    return _get_client().aggiorna_stato_shadow_bets(aggiornamenti)


# ------------------------------------------------------------
# Versione attiva (ORA supportata via HTTP - action: shadow_read_version)
# ------------------------------------------------------------

def leggi_versione_attiva(cfg: ConfigDatabase) -> dict:
    return _get_client().leggi_versione_attiva()


# ==================================================================
# Costruzione righe per INSERT (helper usati da main_shadow_engine.py)
# ==================================================================

def _costruisci_riga(evento, model_source: str, model_version_id: int, valutazione) -> RigaShadowBetDaInserire:
    from config import DEFAULT_CONFIG
    import json as _json

    return RigaShadowBetDaInserire(
        event_id=evento.event_id,
        model_source=model_source,
        model_version_id=model_version_id,
        selection=evento.selection,
        probability_stimata=evento.probability_pct,
        fair_odds=evento.fair_odds,
        bookmaker_odds=evento.bookmaker_odds,
        ev_pct=valutazione.ev_pct,
        kelly_fraction_usata=valutazione.kelly_fraction_usata,
        stake_shadow=round(
            DEFAULT_CONFIG.bankroll_shadow_iniziale * valutazione.kelly_stake_frazione, 2
        ),
        confidence_score=valutazione.confidence_score,
        clv_stimato_pct=evento.clv_stimato_pct,
        features_snapshot_json=_json.dumps({
            "ev_pct": valutazione.ev_pct,
            "kelly_stake_frazione": valutazione.kelly_stake_frazione,
            "quota_bookmaker": evento.bookmaker_odds,
            "campionato": evento.campionato,
        }),
    )


def _costruisci_riga_ensemble(evento, model_version_id: int, risultato_d) -> RigaShadowBetDaInserire:
    from config import DEFAULT_CONFIG
    import json as _json

    return RigaShadowBetDaInserire(
        event_id=evento.event_id,
        model_source="model_d",
        model_version_id=model_version_id,
        selection=evento.selection,
        probability_stimata=evento.probability_pct,
        fair_odds=evento.fair_odds,
        bookmaker_odds=evento.bookmaker_odds,
        ev_pct=risultato_d.ev_pct_medio,
        kelly_fraction_usata=risultato_d.kelly_fraction_usata,
        stake_shadow=round(
            DEFAULT_CONFIG.bankroll_shadow_iniziale * risultato_d.kelly_stake_frazione, 2
        ),
        confidence_score=risultato_d.score_consenso,
        clv_stimato_pct=evento.clv_stimato_pct,
        features_snapshot_json=_json.dumps({
            "score_consenso": risultato_d.score_consenso,
            "modelli_concordi": risultato_d.modelli_concordi,
            "consenso_totale": risultato_d.consenso_totale,
            "pesi_usati": risultato_d.pesi_usati,
        }),
    )


# ==================================================================
# Self-test
# ==================================================================

if __name__ == "__main__":
    print("=== TEST db_layer.py (HTTP mode) ===")
    print()

    # Test 1: Config da environment
    print("1. Test ConfigDatabase.da_environment()...")
    try:
        # Imposta variabili per test (in produzione vengono da .env)
        os.environ.setdefault("SHADOW_API_URL", "https://dash.infinityfree.com/worker/shadow_db_proxy.php")
        os.environ.setdefault("SHADOW_API_TOKEN", "test-token-123")
        cfg = ConfigDatabase.da_environment()
        print(f"   OK: api_url={cfg.api_url}, token={'***' if cfg.api_token else 'MANCANTE'}")
    except Exception as e:
        print(f"   ERRORE: {e}")

    # Test 2: Client initialization
    print("\n2. Test ShadowDBClient init...")
    try:
        client = ShadowDBClient(cfg)
        print(f"   OK: session creata, headers={list(client._session.headers.keys())}")
    except Exception as e:
        print(f"   ERRORE: {e}")

    # Test 3: Funzioni che richiedono endpoint non implementati
    print("\n3. Test chiamate che richiedono endpoint non ancora nel proxy...")
    for func_name in ['leggi_storico_shadow_settled', 'leggi_storico_produzione_settled', 'inserisci_shadow_bets']:
        try:
            func = getattr(client, func_name)
            func("model_a")  # tipo argomento sbagliato apposta per vedere errore connessione
            print(f"   {func_name}: inaspettatamente riuscito")
        except NotImplementedError as e:
            print(f"   {func_name}: NotImplementedError (atteso) - {e}")
        except ConnectionError as e:
            print(f"   {func_name}: ConnectionError (endpoint non raggiungibile in test) - OK")
        except Exception as e:
            print(f"   {func_name}: {type(e).__name__}: {e}")

    # Test 4: Dataclass RigaShadowBetDaInserire
    print("\n4. Test serializzazione RigaShadowBetDaInserire...")
    from dataclasses import asdict
    riga = RigaShadowBetDaInserire(
        event_id=1, model_source="model_a", model_version_id=1, selection="1",
        probability_stimata=55.0, fair_odds=1.82, bookmaker_odds=2.10,
        ev_pct=5.5, kelly_fraction_usata=0.10, stake_shadow=12.50,
        confidence_score=100.0, clv_stimato_pct=1.2,
        features_snapshot_json='{"ev":5.5}', automl_config_id=None
    )
    d = asdict(riga)
    print(f"   OK: {len(d)} campi, features_snapshot_json è stringa: {isinstance(d['features_snapshot_json'], str)}")

    print("\n=== Test completati ===")
    print("NOTA: Per test reali serve shadow_db_proxy.php deployato su InfinityFree")
    print("      e variabili .env corrette (SHADOW_API_URL, SHADOW_API_TOKEN).")
