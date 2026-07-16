"""
EV Scanner AI - Shadow Intelligence System
main/run_model_e_ciclo.py - Lancio manuale di un ciclo AutoML (Model E)
----------------------------------------------------------------
Script SEPARATO da main_shadow_engine.py e da lanciare SOLO manualmente
da CLI (mai da cron/GitHub Actions): un ciclo genetico completo
(dimensione_popolazione x numero_generazioni_per_ciclo backtest, ognuno
con walk-forward validation) puo' richiedere da decine di secondi a
diversi minuti a seconda di quanti eventi storici sono disponibili -
esattamente il tipo di durata che ha gia' causato il bug documentato in
worker/resolve_bets_web.php su InfinityFree. Vedi discussione completa
in models/model_e_automl.py e nel README.

USO:
    python3 main/run_model_e_ciclo.py

Legge lo storico da `scommesse` (produzione, esiti reali gia' noti) per
costruire gli EventoStorico necessari al backtest - NON da `eventi`
(che contiene anche eventi futuri senza esito). Scrive i risultati (sia
i 3 champion che TUTTE le configurazioni valutate, vedi schema.sql) in
shadow_automl_configs.
----------------------------------------------------------------
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from typing import List

from config import DEFAULT_CONFIG
from main.db_layer import ConfigDatabase, connessione
from models.model_e_automl import EventoStorico, esegui_ciclo_evolutivo, RisultatoBacktest
from models.model_a_conservative import EventoInput


def leggi_storico_per_backtest(cfg_db: ConfigDatabase) -> List[EventoStorico]:
    """
    Costruisce la lista di EventoStorico dal JOIN tra `scommesse`
    (esito reale, gia' concluso) ed `eventi` (probabilita'/quote al
    momento). A differenza di leggi_eventi_da_valutare() in db_layer.py
    (che espande OGNI selection possibile per l'inferenza live), qui
    serve solo la selection EFFETTIVAMENTE piazzata/valutata in
    produzione - il backtest walk-forward su Model A ha senso solo sulle
    combinazioni evento+selection che sappiamo essere realmente accadute
    e delle quali conosciamo l'esito, non su ipotetiche selection mai
    valutate.
    """
    query = """
        SELECT s.event_id, s.selection, s.probability, s.fair_odds, s.bookmaker_odds,
               s.result, e.sport, e.campionato, e.event_date
        FROM scommesse s
        INNER JOIN eventi e ON e.id = s.event_id
        WHERE s.result IN ('vinta', 'persa')
        ORDER BY e.event_date ASC
    """
    with connessione(cfg_db) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            righe = cur.fetchall()

    eventi_storici = []
    for r in righe:
        evento_input = EventoInput(
            event_id=r["event_id"], campionato=f"{r['sport']} - {r['campionato']}", mercato="1X2",
            selection=r["selection"], probability_pct=float(r["probability"]),
            bookmaker_odds=float(r["bookmaker_odds"]), fair_odds=float(r["fair_odds"]), clv_stimato_pct=None,
        )
        data_evento = r["event_date"].date() if isinstance(r["event_date"], datetime) else r["event_date"]
        eventi_storici.append(EventoStorico(
            evento_input=evento_input, data_evento=data_evento,
            esito_selection_vincente=(r["result"] == "vinta"),
        ))

    return eventi_storici


def scrivi_risultati_ciclo(cfg_db: ConfigDatabase, tutte_le_configurazioni: List[RisultatoBacktest], top_champion: List[RisultatoBacktest]) -> None:
    """
    Scrive OGNI configurazione valutata in shadow_automl_configs
    (is_champion=0 di default), poi aggiorna le sole righe corrispondenti
    ai top_champion con is_champion=1 e champion_rank - permette, come
    documentato in schema.sql, di analizzare a posteriori l'intero
    spazio di ricerca esplorato dal genetico, non solo il risultato finale.

    Nota su volume: con popolazione 30 x 20 generazioni (default
    config.py), un ciclo produce 600 righe. E' un volume gestibile per
    un INSERT batch, ma su esecuzioni ripetute nel tempo la tabella
    cresce rapidamente - vedi commento in schema.sql sul job di pulizia
    periodico non ancora implementato in questo step.
    """
    query_insert = """
        INSERT INTO shadow_automl_configs
            (generazione, parametri_json, backtest_roi_pct, backtest_sharpe,
             backtest_max_drawdown_pct, backtest_num_bet, backtest_fitness_score)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    with connessione(cfg_db) as conn:
        with conn.cursor() as cur:
            for idx, r in enumerate(tutte_le_configurazioni):
                if r.scartato_dati_insufficienti:
                    continue  # non ha senso persistere una valutazione fallita per mancanza dati
                cur.execute(query_insert, (
                    idx // DEFAULT_CONFIG.model_e.dimensione_popolazione,  # ricostruzione approssimata del numero di generazione
                    json.dumps(r.genoma.to_dict()), r.roi_medio_pct, r.sharpe_medio,
                    r.max_drawdown_medio_pct, r.numero_bet_totale_test, r.fitness_score,
                ))

            # Marca i champion: azzera prima eventuali vecchi champion (un
            # solo set di 3 alla volta), poi marca i nuovi in base al
            # genoma esatto (confronto sui parametri, essendo appena
            # stati inseriti non abbiamo ancora un id certo da riusare).
            cur.execute("UPDATE shadow_automl_configs SET is_champion = 0, champion_rank = NULL WHERE is_champion = 1")
            for rank, champ in enumerate(top_champion, start=1):
                cur.execute(
                    """
                    UPDATE shadow_automl_configs
                    SET is_champion = 1, champion_rank = %s
                    WHERE parametri_json = %s
                    ORDER BY id DESC LIMIT 1
                    """,
                    (rank, json.dumps(champ.genoma.to_dict())),
                )


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        cfg_db = ConfigDatabase.da_environment()
    except ValueError as exc:
        print(f"ERRORE CONFIGURAZIONE: {exc}")
        sys.exit(1)

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Lettura storico per backtest walk-forward...")
    t0 = time.time()
    eventi_storici = leggi_storico_per_backtest(cfg_db)
    print(f"  {len(eventi_storici)} eventi storici letti in {int((time.time()-t0)*1000)}ms")

    if len(eventi_storici) < DEFAULT_CONFIG.model_e.minimo_bet_per_backtest:
        print(
            f"ATTENZIONE: solo {len(eventi_storici)} eventi disponibili, sotto il minimo configurato "
            f"({DEFAULT_CONFIG.model_e.minimo_bet_per_backtest}). Il ciclo verra' comunque avviato ma "
            "e' pressoche' certo che tutte le configurazioni vengano scartate per dati insufficienti."
        )

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Avvio ciclo evolutivo "
          f"(popolazione={DEFAULT_CONFIG.model_e.dimensione_popolazione}, "
          f"generazioni={DEFAULT_CONFIG.model_e.numero_generazioni_per_ciclo})...")
    t0 = time.time()
    risultato = esegui_ciclo_evolutivo(eventi_storici, DEFAULT_CONFIG.model_e, seed=None)
    durata_s = time.time() - t0
    print(f"  Ciclo completato in {durata_s:.1f}s")

    if risultato.scartato_dati_insufficienti:
        print(f"CICLO SCARTATO: {risultato.motivo_scarto}")
        sys.exit(1)

    print(f"\nChampion trovati: {len(risultato.top_champion)}")
    for i, champ in enumerate(risultato.top_champion, 1):
        print(f"  #{i} fitness={champ.fitness_score}  ROI medio={champ.roi_medio_pct}%  "
              f"Sharpe={champ.sharpe_medio}  genoma={champ.genoma.to_dict()}")

    print(f"\nScrittura di {len(risultato.tutte_le_configurazioni_valutate)} configurazioni valutate in shadow_automl_configs...")
    scrivi_risultati_ciclo(cfg_db, risultato.tutte_le_configurazioni_valutate, risultato.top_champion)
    print("Fatto.")


if __name__ == "__main__":
    main()
