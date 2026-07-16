"""
EV Scanner AI - Shadow Intelligence System
main/main_shadow_engine.py - Il motore che collega tutto
----------------------------------------------------------------
Fino a questo file, ogni pezzo dello Shadow Lab esisteva ma non era
collegato a nulla: modelli richiamabili solo da CLI con dati finti,
tabelle DB vuote, dashboard con dati mock. Questo e' lo script che
rende tutto vivo: legge eventi reali da `eventi`, li fa valutare da
Model A, B, C e D in sequenza, scrive i risultati in `shadow_bets`.

NON include Model E: l'algoritmo genetico (Step 7) resta
DELIBERATAMENTE offline/manuale, va lanciato a parte con uno script
CLI dedicato (vedi run_model_e_ciclo.py) - mai da questo motore
ricorrente, per gli stessi motivi di durata processo gia' discussi.

STILE OPERATIVO: preso in prestito da worker/resolve_bets_web.php
(gia' in produzione) dopo aver letto il commento sul bug storico
documentato li' dentro:
  - NESSUN fire-and-forget: lo script gira per intero in modo
    sincrono, ogni fase e' cronometrata, un fallimento e' visibile
    SUBITO (eccezione + log), mai silenzioso.
  - Budget di tempo per fase con controllo prima di ogni fase, non a
    meta': se il tempo residuo non basta per la fase successiva, la
    si salta esplicitamente e lo si registra, invece di farla partire
    e rischiare un kill a meta' che lascerebbe dati inconsistenti.
  - Un blocco di log finale leggibile riassume cosa e' successo in
    ogni fase, in italiano, coerente con lo stile gia' visto lato PHP.

QUANDO LANCIARLO: pensato per essere invocato periodicamente (stesso
principio del cron-job.org/GitHub Actions gia' in uso per lo scan PHP),
tipicamente subito DOPO lo scan di produzione (worker/scan_web.php),
cosi' opera sugli eventi appena inseriti/aggiornati in `eventi`.
----------------------------------------------------------------
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional

# Questo script viene lanciato con cwd = ev_scanner_shadow_lab/main (sia
# dal workflow GitHub Actions "Shadow Engine", working-directory:
# ev_scanner_shadow_lab/main, sia da CLI seguendo il README: `cd
# ev_scanner_shadow_lab/main && python3 main_shadow_engine.py`). In
# quello scenario Python mette in sys.path SOLO la cartella dello
# script (main/), non la cartella padre (ev_scanner_shadow_lab/): gli
# import sotto - "models.*", "utils.*" e soprattutto "main.db_layer"
# (che tenta di importare se stesso come pacchetto "main" dall'esterno,
# impossibile visto da dentro main/ stesso) falliscono sempre con
# ModuleNotFoundError, PRIMA ancora di arrivare a leggere una singola
# riga da eventi. Aggiungendo esplicitamente la cartella padre a
# sys.path (idempotente: non duplica se gia' presente), tutti e tre i
# pacchetti diventano risolvibili indipendentemente da dove/come viene
# invocato lo script.
_PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

from config import DEFAULT_CONFIG
from main.db_layer import (
    ConfigDatabase, RigaShadowBetDaInserire,
    leggi_eventi_da_valutare, leggi_storico_shadow_settled,
    inserisci_shadow_bets, logga_run, leggi_versione_attiva,
)
from models.model_a_conservative import ModelAConservative, EventoInput, ValutazioneShadow
from models.model_b_pure_ev import ModelBPureEV
from models.model_d_ensemble import ModelDEnsemble, SegnaleModelloSorgente, calcola_pesi_sharpe

# Model C richiede uno stato persistente (pesi appresi) che oggi NON ha
# ancora un meccanismo di caricamento/salvataggio da shadow_model_c_weights
# implementato in db_layer.py (deliberatamente rimandato, vedi nota in
# fondo al file) - per questo primo motore funzionante, Model C viene
# fatto girare SEMPRE in bootstrap (pesi euristici, mai aggiornati). Il
# training online reale e' il prossimo pezzo da collegare, non e' nel
# perimetro di "far vedere qualcosa che si muove davvero" di oggi.
from models.model_c_adaptive import ModelCAdaptive, StatoModelCAdaptive


# Budget totale del ciclo, prudenzialmente sotto la soglia usuale di
# timeout HTTP (120s, vedi worker/resolve_bets_web.php) anche se questo
# script gira tipicamente da CLI/cron, non da endpoint web - lo stesso
# principio di budget esplicito evita comunque run che si allungano
# senza controllo se il volume di eventi cresce nel tempo.
BUDGET_TOTALE_SECONDI = 100
GIORNI_FINESTRA_EVENTI = 3
GIORNI_FINESTRA_SHARPE = 30


@dataclass
class RisultatoFase:
    nome: str
    riuscita: bool
    durata_ms: int
    dettaglio: str
    numero_elementi: int = 0


@dataclass
class ReportCiclo:
    fasi: List[RisultatoFase]
    successo_complessivo: bool
    durata_totale_ms: int
    diagnosi: str


def _tempo_residuo(inizio: float) -> float:
    return BUDGET_TOTALE_SECONDI - (time.time() - inizio)


def esegui_ciclo(cfg_db: ConfigDatabase) -> ReportCiclo:
    """
    Un ciclo completo: legge eventi -> fa valutare A/B/C -> combina con
    D -> scrive tutto in shadow_bets. Ogni fase controlla il tempo
    residuo PRIMA di partire (stesso pattern di
    worker/resolve_bets_web.php) e viene saltata, mai interrotta a
    meta', se il budget non basta.
    """
    inizio_ciclo = time.time()
    fasi: List[RisultatoFase] = []
    cfg_sistema = DEFAULT_CONFIG
    cfg_sistema.validate()

    # ------------------------------------------------------------
    # FASE 0: versione attiva (serve come FK per ogni riga shadow_bets)
    # ------------------------------------------------------------
    t0 = time.time()
    try:
        versione = leggi_versione_attiva(cfg_db)
        model_version_id = versione["id"]
        fasi.append(RisultatoFase(
            "versione_attiva", True, int((time.time() - t0) * 1000),
            f"versione attiva: {versione['versione']} (id={model_version_id})",
        ))
    except Exception as exc:
        fasi.append(RisultatoFase("versione_attiva", False, int((time.time() - t0) * 1000), f"ERRORE: {exc}"))
        return _chiudi_report(fasi, inizio_ciclo, successo=False)

    # ------------------------------------------------------------
    # FASE 1: lettura eventi da valutare
    # ------------------------------------------------------------
    if _tempo_residuo(inizio_ciclo) < 10:
        fasi.append(RisultatoFase("lettura_eventi", False, 0, "SALTATA: budget insufficiente prima di iniziare"))
        return _chiudi_report(fasi, inizio_ciclo, successo=False)

    t0 = time.time()
    try:
        candidate = leggi_eventi_da_valutare(cfg_db, giorni_finestra=GIORNI_FINESTRA_EVENTI)
        fasi.append(RisultatoFase(
            "lettura_eventi", True, int((time.time() - t0) * 1000),
            f"{len(candidate)} candidate (evento+selection) lette dalla finestra di {GIORNI_FINESTRA_EVENTI} giorni",
            numero_elementi=len(candidate),
        ))
    except Exception as exc:
        fasi.append(RisultatoFase("lettura_eventi", False, int((time.time() - t0) * 1000), f"ERRORE: {exc}\n{traceback.format_exc()}"))
        return _chiudi_report(fasi, inizio_ciclo, successo=False)

    if not candidate:
        fasi.append(RisultatoFase("valutazione_modelli", True, 0, "nessun evento da valutare in questa finestra, ciclo terminato senza lavoro"))
        return _chiudi_report(fasi, inizio_ciclo, successo=True)

    # ------------------------------------------------------------
    # FASE 2: pesi Sharpe per Model D (serve lo storico shadow settled)
    # ------------------------------------------------------------
    t0 = time.time()
    try:
        storico_per_modello = {
            m: leggi_storico_shadow_settled(cfg_db, m, giorni_finestra=GIORNI_FINESTRA_SHARPE)
            for m in ("model_a", "model_b", "model_c")
        }
        pesi_sharpe = calcola_pesi_sharpe(storico_per_modello, cfg_sistema.model_d)
        fasi.append(RisultatoFase(
            "calcolo_pesi_sharpe", True, int((time.time() - t0) * 1000),
            f"pesi: {pesi_sharpe} (storico: " + ", ".join(f"{m}={len(s)}bet" for m, s in storico_per_modello.items()) + ")",
        ))
    except Exception as exc:
        # Non fatale: si prosegue con pesi neutri, il consenso Model D
        # funziona comunque, solo senza la componente di qualita' Sharpe.
        pesi_sharpe = {"model_a": 1.0, "model_b": 1.0, "model_c": 1.0}
        fasi.append(RisultatoFase(
            "calcolo_pesi_sharpe", False, int((time.time() - t0) * 1000),
            f"fallita, uso pesi neutri di fallback: {exc}",
        ))

    # ------------------------------------------------------------
    # FASE 3: valutazione A/B/C/D per ogni candidata
    # ------------------------------------------------------------
    if _tempo_residuo(inizio_ciclo) < 15:
        fasi.append(RisultatoFase("valutazione_modelli", False, 0, "SALTATA: budget insufficiente"))
        return _chiudi_report(fasi, inizio_ciclo, successo=False)

    t0 = time.time()
    model_a = ModelAConservative(cfg_sistema.model_a)
    model_b = ModelBPureEV(cfg_sistema.model_b)
    model_c = ModelCAdaptive(cfg_sistema.model_c, StatoModelCAdaptive.bootstrap(cfg_sistema.model_c))
    model_d = ModelDEnsemble(cfg_sistema.model_d)

    righe_da_scrivere: List[RigaShadowBetDaInserire] = []
    conteggio_per_modello = {"model_a": 0, "model_b": 0, "model_c": 0, "model_d": 0}
    numero_valutate = 0

    for evento in candidate:
        # EventoInputEsteso e' un sottotipo di EventoInput (vedi
        # model_c_adaptive.py): passabile direttamente ad A/B che si
        # aspettano EventoInput, senza bisogno di conversione.
        valutazione_a = model_a.valuta(evento)
        valutazione_b = model_b.valuta(evento)
        valutazione_c = model_c.valuta(evento)
        numero_valutate += 1

        segnali_per_d: List[SegnaleModelloSorgente] = []
        for nome, val in [("model_a", valutazione_a), ("model_b", valutazione_b), ("model_c", valutazione_c)]:
            if val.accepted:
                segnali_per_d.append(SegnaleModelloSorgente(nome, val))
                conteggio_per_modello[nome] += 1
                righe_da_scrivere.append(_costruisci_riga(evento, nome, model_version_id, val))

        if segnali_per_d:
            risultato_d = model_d.valuta(segnali_per_d, pesi_sharpe)
            if risultato_d.accepted:
                conteggio_per_modello["model_d"] += 1
                righe_da_scrivere.append(_costruisci_riga_ensemble(evento, model_version_id, risultato_d))

        # Controllo periodico del budget anche DENTRO il loop: con
        # abbastanza eventi, la sola valutazione potrebbe avvicinarsi al
        # budget - se succede, si interrompe il loop (non a meta' di un
        # singolo evento, tra un evento e il successivo) e si scrive
        # comunque cio' che si e' accumulato finora, invece di perdere
        # tutto il lavoro fatto.
        if _tempo_residuo(inizio_ciclo) < 15:
            fasi.append(RisultatoFase(
                "valutazione_modelli", True, int((time.time() - t0) * 1000),
                f"budget esaurito a meta': valutate {numero_valutate}/{len(candidate)} candidate, "
                "il resto verra' ripreso al prossimo ciclo",
            ))
            break
    else:
        fasi.append(RisultatoFase(
            "valutazione_modelli", True, int((time.time() - t0) * 1000),
            f"tutte le {len(candidate)} candidate valutate. Accettate: " + ", ".join(f"{k}={v}" for k, v in conteggio_per_modello.items()),
        ))

    # ------------------------------------------------------------
    # FASE 4: scrittura shadow_bets
    # ------------------------------------------------------------
    if not righe_da_scrivere:
        fasi.append(RisultatoFase("scrittura_shadow_bets", True, 0, "nessuna bet shadow generata in questo ciclo (nessun modello ha accettato nulla)"))
        return _chiudi_report(fasi, inizio_ciclo, successo=True)

    t0 = time.time()
    try:
        righe_scritte = inserisci_shadow_bets(cfg_db, righe_da_scrivere)
        fasi.append(RisultatoFase(
            "scrittura_shadow_bets", True, int((time.time() - t0) * 1000),
            f"{righe_scritte} righe scritte/aggiornate in shadow_bets",
            numero_elementi=righe_scritte,
        ))
    except Exception as exc:
        fasi.append(RisultatoFase("scrittura_shadow_bets", False, int((time.time() - t0) * 1000), f"ERRORE: {exc}\n{traceback.format_exc()}"))
        return _chiudi_report(fasi, inizio_ciclo, successo=False)

    return _chiudi_report(fasi, inizio_ciclo, successo=True)


def _costruisci_riga(evento, model_source: str, model_version_id: int, valutazione: ValutazioneShadow) -> RigaShadowBetDaInserire:
    """Da una ValutazioneShadow accettata (A, B o C) a una riga pronta per l'INSERT."""
    return RigaShadowBetDaInserire(
        event_id=evento.event_id, model_source=model_source, model_version_id=model_version_id,
        selection=evento.selection, probability_stimata=evento.probability_pct, fair_odds=evento.fair_odds,
        bookmaker_odds=evento.bookmaker_odds, ev_pct=valutazione.ev_pct,
        kelly_fraction_usata=valutazione.kelly_fraction_usata, stake_shadow=round(
            DEFAULT_CONFIG.bankroll_shadow_iniziale * valutazione.kelly_stake_frazione, 2
        ),
        confidence_score=valutazione.confidence_score, clv_stimato_pct=evento.clv_stimato_pct,
        features_snapshot_json=json.dumps({
            "ev_pct": valutazione.ev_pct, "kelly_stake_frazione": valutazione.kelly_stake_frazione,
            "quota_bookmaker": evento.bookmaker_odds, "campionato": evento.campionato,
        }),
    )


def _costruisci_riga_ensemble(evento, model_version_id: int, risultato_d) -> RigaShadowBetDaInserire:
    """Da un RisultatoEnsemble accettato (Model D) a una riga pronta per l'INSERT."""
    return RigaShadowBetDaInserire(
        event_id=evento.event_id, model_source="model_d", model_version_id=model_version_id,
        selection=evento.selection, probability_stimata=evento.probability_pct, fair_odds=evento.fair_odds,
        bookmaker_odds=evento.bookmaker_odds, ev_pct=risultato_d.ev_pct_medio,
        kelly_fraction_usata=risultato_d.kelly_fraction_usata,
        stake_shadow=round(DEFAULT_CONFIG.bankroll_shadow_iniziale * risultato_d.kelly_stake_frazione, 2),
        confidence_score=risultato_d.score_consenso, clv_stimato_pct=evento.clv_stimato_pct,
        features_snapshot_json=json.dumps({
            "score_consenso": risultato_d.score_consenso, "modelli_concordi": risultato_d.modelli_concordi,
            "consenso_totale": risultato_d.consenso_totale, "pesi_usati": risultato_d.pesi_usati,
        }),
    )


def _chiudi_report(fasi: List[RisultatoFase], inizio_ciclo: float, successo: bool) -> ReportCiclo:
    durata_totale_ms = int((time.time() - inizio_ciclo) * 1000)
    righe_diagnosi = [f"{'✓' if f.riuscita else '✗'} {f.nome} ({f.durata_ms}ms): {f.dettaglio}" for f in fasi]
    diagnosi = "\n".join(righe_diagnosi)
    return ReportCiclo(fasi=fasi, successo_complessivo=successo, durata_totale_ms=durata_totale_ms, diagnosi=diagnosi)


# ==================================================================
# Entry point CLI
# ==================================================================
def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        print("[avviso] python-dotenv non installato: le variabili d'ambiente vanno impostate manualmente (pip install python-dotenv per usare un file .env)")

    try:
        cfg_db = ConfigDatabase.da_environment()
    except ValueError as exc:
        print(f"ERRORE CONFIGURAZIONE: {exc}")
        sys.exit(1)

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Avvio ciclo Shadow Engine...")
    report = esegui_ciclo(cfg_db)
    print()
    print(report.diagnosi)
    print()
    print(f"Ciclo {'COMPLETATO' if report.successo_complessivo else 'FALLITO'} in {report.durata_totale_ms}ms")

    try:
        eventi_processati = next((f.numero_elementi for f in report.fasi if f.nome == "lettura_eventi"), None)
        bet_generate = next((f.numero_elementi for f in report.fasi if f.nome == "scrittura_shadow_bets"), None)
        logga_run(
            cfg_db, channel="main_engine", level="info" if report.successo_complessivo else "error",
            message=report.diagnosi[:1000], eventi_processati=eventi_processati,
            bet_shadow_generate=bet_generate, duration_ms=report.durata_totale_ms,
        )
    except Exception as exc:
        print(f"[avviso] impossibile scrivere il log su shadow_run_log: {exc}")

    sys.exit(0 if report.successo_complessivo else 1)


if __name__ == "__main__":
    main()
