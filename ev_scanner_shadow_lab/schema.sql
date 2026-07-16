-- ============================================================
-- EV SCANNER - AI Research Lab (Shadow Intelligence System)
-- Schema MySQL - Step 1/7: Versioning + Shadow Bets + Model Weights
-- ============================================================
-- NOTA IMPORTANTE (isolamento dalla produzione):
-- Nessuna di queste tabelle viene letta/scritta dal Filtro Ufficiale
-- (classes/, worker/, api/ della root EV Scanner). Lo Shadow Lab legge
-- SOLO in lettura da `eventi` e `scommesse` (per costruire il flusso dati
-- di input e per confrontare l'esito reale a fine giornata), ma scrive
-- esclusivamente nelle tabelle `shadow_*` definite qui sotto. Questo
-- garantisce che un bug nei modelli shadow non possa mai corrompere lo
-- stato del filtro reale o piazzare una scommessa vera.
--
-- Convenzioni ereditate dallo schema esistente (sql/schema.sql):
-- - CHARSET/COLLATE esplicito utf8mb4/utf8mb4_unicode_ci su ogni tabella
--   (necessario su hosting condivisi come InfinityFree, vedi commento
--   originale in sql/schema.sql).
-- - Timestamp gestiti da MySQL (CURRENT_TIMESTAMP), coerenti col fuso
--   Europe/Rome impostato a livello di sessione da config/database.php.
-- - Colonne aggiuntive vanno gestite in stile SchemaSync (additive,
--   mai ALTER distruttivi) - vedi utils/schema_sync_shadow.py nello
--   step successivo per l'equivalente Python di classes/SchemaSync.php.
-- ============================================================


-- ------------------------------------------------------------
-- 1. shadow_model_versions
-- ------------------------------------------------------------
-- Ogni riga è una "release" del sistema Shadow Lab nel suo complesso
-- (non del singolo modello: i parametri di TUTTI i modelli attivi in
-- quel momento vengono congelati in `parametri_snapshot_json`). Questo
-- è ciò che permette, mesi dopo, di rispondere alla domanda "con quali
-- impostazioni esatte il Model C ha generato questa bet shadow?" senza
-- dover ricostruire lo stato da un changelog testuale.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_model_versions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    versione VARCHAR(20) NOT NULL COMMENT 'es. v1.0, v1.1, v2.0 - libera ma consigliata semver-like',
    data_attivazione DATETIME NOT NULL COMMENT 'quando questa versione diventa la "corrente" per le nuove bet shadow',
    data_disattivazione DATETIME NULL DEFAULT NULL COMMENT 'valorizzato automaticamente quando viene attivata la versione successiva; NULL = versione correntemente attiva',
    descrizione_modifiche TEXT NOT NULL COMMENT 'changelog libero, es. "Aumentata soglia EV Model B da 3%% a 4%%"',
    autore VARCHAR(100) NOT NULL DEFAULT 'Malu',
    note TEXT DEFAULT NULL,
    -- Snapshot completo dei parametri di TUTTI i modelli (A/B/C/D/E) attivi
    -- al momento dell'attivazione. Struttura libera JSON, tipicamente:
    -- {"model_a": {...}, "model_b": {...}, "model_c": {...}, "model_d": {...}, "model_e": {...}}
    -- Vedi config.py::export_current_params_snapshot() nello step 2.
    parametri_snapshot_json JSON NOT NULL,
    is_attiva TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'ridondante rispetto a data_disattivazione IS NULL, ma piu comodo/veloce per query e indice',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_versione (versione),
    INDEX idx_is_attiva (is_attiva),
    INDEX idx_data_attivazione (data_attivazione)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ------------------------------------------------------------
-- 2. shadow_bets
-- ------------------------------------------------------------
-- Il cuore del sistema: una riga per ogni valutazione shadow generata da
-- UNO dei 5 modelli su UN evento. Un singolo evento reale può quindi
-- generare fino a 5 righe qui (una per modello, se tutti lo valutano
-- positivamente), più eventualmente una riga aggiuntiva per Model D
-- (Ensemble), che è un consumatore dei segnali A/B/C più che un
-- generatore indipendente - vedi commento su model_source sotto.
--
-- IMPORTANTE: questa tabella NON ha alcuna relazione di scrittura verso
-- `scommesse` (la tabella delle bet reali). Il collegamento a
-- `eventi.id` è di sola lettura per poter recuperare risultato/quote.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_bets (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_id INT NOT NULL COMMENT 'FK verso eventi.id (tabella produzione, sola lettura)',
    -- Quale modello ha generato questa valutazione. 'ensemble' è il Model D
    -- e non genera una selezione propria: consolida quelle di a/b/c sullo
    -- stesso event_id+selection in un unico score - vedi model_d_ensemble.py.
    model_source ENUM('model_a', 'model_b', 'model_c', 'model_d', 'model_e') NOT NULL,
    -- FK verso la versione del sistema attiva al momento della generazione.
    -- Permette di rispondere a "come si comportava il Model C nella v1.2?"
    -- anche dopo che i parametri sono cambiati molte volte.
    model_version_id INT NOT NULL,
    -- Per Model E (AutoML): quale configurazione candidata (tra le 3
    -- Champion/Challenger mantenute) ha generato questa bet. NULL per gli
    -- altri modelli. FK verso shadow_automl_configs.id (step successivo).
    automl_config_id INT NULL DEFAULT NULL,

    selection ENUM('1', 'X', '2') NOT NULL,
    probability_stimata DECIMAL(6,3) NOT NULL COMMENT 'probabilita pct stimata dal modello per questa selection (puo differire da eventi.prob_* se il modello applica una propria calibrazione)',
    fair_odds DECIMAL(8,3) NOT NULL,
    bookmaker_odds DECIMAL(8,3) NOT NULL COMMENT 'quota usata nel calcolo EV al momento della valutazione shadow',
    ev_pct DECIMAL(8,3) NOT NULL COMMENT 'expected value %, formula (probabilita_stimata/100 * bookmaker_odds) - 1, espresso in %',

    -- Sizing shadow: ogni modello ha la propria logica Kelly (frazione
    -- diversa per A vs B, vedi progettazione), lo stake è SIMULATO su un
    -- bankroll shadow indipendente (vedi shadow_bankroll_snapshots sotto).
    kelly_fraction_usata DECIMAL(4,3) NOT NULL COMMENT 'frazione di Kelly applicata da questo modello (es 0.05 per Model A, 0.25 per Model B)',
    stake_shadow DECIMAL(10,2) NOT NULL COMMENT 'stake simulato in valuta, calcolato su shadow_bankroll_snapshots del modello a quella data',

    -- Confidence/score: per i modelli A/B è quasi sempre 100 (pass/fail
    -- binario sui filtri), per C è l'output continuo del modello, per D è
    -- il punteggio di consenso 0-100 descritto nella progettazione.
    confidence_score DECIMAL(5,2) NOT NULL DEFAULT 100.00 COMMENT '0-100, per Model D e' il vero score di consenso; per gli altri modelli e un pass/fail (100) salvo estensioni future',

    -- CLV: stimato al momento della bet shadow (probabilita teorica di
    -- battere la chiusura), poi confrontato col CLV REALE una volta che
    -- worker/capture_closing_odds.php (produzione) ha catturato la quota
    -- di chiusura per lo stesso evento. Il join avviene in fase di
    -- calcolo statistiche, non con una FK diretta.
    clv_stimato_pct DECIMAL(6,3) DEFAULT NULL,

    -- Snapshot delle feature usate per la decisione, principalmente per
    -- Model C (Adaptive) e Model D (Ensemble) - permette la Feature
    -- Importance retrospettiva richiesta nella progettazione, anche se i
    -- pesi del modello sono cambiati da allora.
    -- Struttura tipica: {"ev": 4.2, "kelly": 0.08, "quota": 2.10,
    --   "campionato": "Serie A", "mercato": "1X2", "clv_stimato": 1.8,
    --   "smart_filter_score": 0.73, "prob_stimata_vs_implicita": 0.05, ...}
    features_snapshot_json JSON DEFAULT NULL,

    -- Esito: popolato da utils/shadow_settlement.py leggendo il risultato
    -- reale da `scommesse`/`eventi` una volta che la partita è conclusa.
    -- Indipendente dal fatto che la bet REALE su quell'evento sia stata
    -- effettivamente piazzata: lo shadow valuta comunque il "cosa
    -- sarebbe successo se avessi seguito questo modello".
    result ENUM('pending', 'vinta', 'persa', 'void') NOT NULL DEFAULT 'pending',
    profit_loss_shadow DECIMAL(10,2) DEFAULT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    settled_at TIMESTAMP NULL DEFAULT NULL,

    CONSTRAINT fk_shadow_bets_evento FOREIGN KEY (event_id) REFERENCES eventi(id) ON DELETE CASCADE,
    CONSTRAINT fk_shadow_bets_versione FOREIGN KEY (model_version_id) REFERENCES shadow_model_versions(id) ON DELETE RESTRICT,

    -- Un modello non deve poter generare due valutazioni per lo stesso
    -- evento+selection (idempotenza: se lo scanner shadow gira più volte
    -- sullo stesso batch di eventi, non deve duplicare le righe).
    UNIQUE KEY uniq_model_event_selection (model_source, event_id, selection, automl_config_id),

    INDEX idx_model_result (model_source, result),
    INDEX idx_event (event_id),
    INDEX idx_result_created (result, created_at),
    INDEX idx_model_version (model_version_id),
    INDEX idx_automl_config (automl_config_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ------------------------------------------------------------
-- 3. shadow_bankroll_snapshots
-- ------------------------------------------------------------
-- Ogni modello (A/B/C/D + le 3 configurazioni Champion/Challenger di E)
-- ha un proprio bankroll shadow simulato, indipendente sia dal bankroll
-- reale (impostazioni.bankroll) sia dagli altri modelli. Necessario per
-- calcolare correttamente lo stake Kelly simulato (dipende dal bankroll
-- CORRENTE del modello, non da quello iniziale) e per disegnare la
-- curva-bankroll per-modello nella dashboard.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_bankroll_snapshots (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    model_source ENUM('model_a', 'model_b', 'model_c', 'model_d', 'model_e') NOT NULL,
    automl_config_id INT NULL DEFAULT NULL COMMENT 'NULL per A/B/C/D; valorizzato per E, uno snapshot per ciascuna delle 3 configurazioni Champion/Challenger',
    snapshot_date DATE NOT NULL,
    bankroll_shadow DECIMAL(12,2) NOT NULL,
    bankroll_iniziale_shadow DECIMAL(12,2) NOT NULL COMMENT 'ripetuto su ogni riga per semplicita di calcolo ROI senza self-join',
    UNIQUE KEY uniq_model_config_day (model_source, automl_config_id, snapshot_date),
    INDEX idx_model_date (model_source, snapshot_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ------------------------------------------------------------
-- 4. shadow_model_c_weights
-- ------------------------------------------------------------
-- Storico dei pesi del modello Adaptive (Model C). Ogni volta che il
-- meccanismo di apprendimento online (vedi utils/stats_engine.py e
-- model_c_adaptive.py, step 5) aggiorna i pesi dopo N bet settled, viene
-- inserita una NUOVA riga (mai un UPDATE) - questo è ciò che rende
-- possibile disegnare "l'evoluzione dei pesi nel tempo" e fare debug
-- retrospettivo se il modello inizia a comportarsi in modo anomalo.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_model_c_weights (
    id INT AUTO_INCREMENT PRIMARY KEY,
    model_version_id INT NOT NULL,
    -- Pesi correnti per ciascuna feature, dopo l'ultimo aggiornamento.
    -- Struttura: {"ev": 0.42, "kelly": 0.18, "clv_stimato": 0.31,
    --   "smart_filter_score": 0.25, "quota_inv": -0.12, "bias": 0.05, ...}
    weights_json JSON NOT NULL,
    -- Parametri di normalizzazione (media/std per feature) usati al
    -- momento di QUESTO aggiornamento, necessari per riapplicare
    -- correttamente i pesi in modo retrospettivo (vedi model_c_adaptive.py).
    normalization_params_json JSON NOT NULL,
    bet_concluse_totali_al_momento INT NOT NULL COMMENT 'quante bet settled avevano contribuito a questo update (contatore progressivo)',
    learning_rate_usato DECIMAL(8,6) NOT NULL,
    -- Metriche di fit al momento dell'update, utili per monitorare se il
    -- modello sta effettivamente migliorando o divergendo.
    log_loss DECIMAL(10,6) DEFAULT NULL,
    accuracy_ultime_50 DECIMAL(5,2) DEFAULT NULL COMMENT 'accuracy pct sulle ultime 50 bet valutate prima di questo update',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_weights_versione FOREIGN KEY (model_version_id) REFERENCES shadow_model_versions(id) ON DELETE RESTRICT,
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ------------------------------------------------------------
-- 5. shadow_automl_configs
-- ------------------------------------------------------------
-- Le configurazioni scoperte/valutate dal Model E (algoritmo genetico).
-- Mantiene SOLO le migliori 3 (Champion/Challenger) come da progettazione,
-- ma questa tabella logga OGNI configurazione testata (anche quelle
-- scartate) con is_champion=0, per poter analizzare a posteriori lo
-- spazio di ricerca esplorato dall'algoritmo genetico. Un job di pulizia
-- periodico (vedi model_e_automl.py) può eventualmente potare le righe
-- più vecchie non-champion per contenere la crescita della tabella.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_automl_configs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    generazione INT NOT NULL COMMENT 'numero di generazione dell algoritmo genetico in cui questa config e comparsa',
    -- Genoma della configurazione: soglie EV, limiti quota, mercati
    -- consentiti, coefficiente Kelly, soglia CLV - vedi progettazione
    -- Model E. Struttura libera per permettere di aggiungere geni futuri
    -- senza ALTER TABLE.
    parametri_json JSON NOT NULL,
    -- Risultati del backtest walk-forward su questa configurazione (vedi
    -- model_e_automl.py per la metodologia anti-overfitting).
    backtest_roi_pct DECIMAL(8,3) DEFAULT NULL,
    backtest_sharpe DECIMAL(8,4) DEFAULT NULL,
    backtest_max_drawdown_pct DECIMAL(8,3) DEFAULT NULL,
    backtest_num_bet INT DEFAULT NULL,
    backtest_fitness_score DECIMAL(10,4) DEFAULT NULL COMMENT 'score composito usato dall algoritmo genetico per il ranking (vedi formula in model_e_automl.py)',
    is_champion TINYINT(1) NOT NULL DEFAULT 0 COMMENT '1 se attualmente tra le 3 migliori configurazioni mantenute in produzione shadow',
    champion_rank TINYINT DEFAULT NULL COMMENT '1, 2 o 3 se is_champion=1, altrimenti NULL',
    evaluated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_champion (is_champion, champion_rank),
    INDEX idx_generazione (generazione),
    INDEX idx_fitness (backtest_fitness_score)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- shadow_bets.automl_config_id punta qui; aggiunta come ALTER separato
-- (invece che nella CREATE TABLE sopra) perche' shadow_automl_configs
-- deve esistere prima - MySQL non supporta forward-reference nelle FK
-- all'interno dello stesso script in ordine lineare senza questo trucco.
ALTER TABLE shadow_bets
    ADD CONSTRAINT fk_shadow_bets_automl_config
    FOREIGN KEY (automl_config_id) REFERENCES shadow_automl_configs(id) ON DELETE SET NULL;


-- ------------------------------------------------------------
-- 6. shadow_promotion_tests
-- ------------------------------------------------------------
-- Storico dei test di significativita statistica eseguiti dal Promotion
-- Engine (step finale) quando l'utente richiede di valutare se uno
-- Shadow Model e pronto per sostituire il Filtro Ufficiale. Una riga per
-- ogni test eseguito, anche quelli falliti - serve a costruire una
-- timeline di "quante volte abbiamo provato a promuovere Model X".
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_promotion_tests (
    id INT AUTO_INCREMENT PRIMARY KEY,
    model_source ENUM('model_a', 'model_b', 'model_c', 'model_d', 'model_e') NOT NULL,
    automl_config_id INT NULL DEFAULT NULL,
    model_version_id INT NOT NULL,
    -- Periodo out-of-sample valutato per il test.
    periodo_da DATE NOT NULL,
    periodo_a DATE NOT NULL,
    num_bet_out_of_sample INT NOT NULL,
    -- Risultato del test statistico (vedi utils/promotion_engine.py).
    p_value DECIMAL(10,8) DEFAULT NULL,
    roi_candidato_pct DECIMAL(8,3) NOT NULL,
    roi_produzione_pct DECIMAL(8,3) NOT NULL,
    sharpe_candidato DECIMAL(8,4) NOT NULL,
    sharpe_produzione DECIMAL(8,4) NOT NULL,
    esito ENUM('promosso', 'respinto_dati_insufficienti', 'respinto_non_significativo', 'respinto_metriche_inferiori') NOT NULL,
    dettaglio_json JSON DEFAULT NULL COMMENT 'breakdown completo del test per audit, vedi promotion_engine.py',
    eseguito_da VARCHAR(100) NOT NULL DEFAULT 'Malu',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_promotion_versione FOREIGN KEY (model_version_id) REFERENCES shadow_model_versions(id) ON DELETE RESTRICT,
    CONSTRAINT fk_promotion_automl_config FOREIGN KEY (automl_config_id) REFERENCES shadow_automl_configs(id) ON DELETE SET NULL,
    INDEX idx_model_esito (model_source, esito),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ------------------------------------------------------------
-- 7. shadow_run_log
-- ------------------------------------------------------------
-- Equivalente shadow di `scheduler_log` (produzione): log diagnostico di
-- ogni esecuzione del motore shadow (main_shadow_engine.py), per capire
-- se/quando/perche un ciclo di valutazione e fallito senza dover
-- guardare i log PHP di produzione che non sanno nulla dello shadow lab.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_run_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    channel VARCHAR(64) NOT NULL COMMENT 'es. main_engine, model_c_training, model_e_genetic, settlement',
    level ENUM('info', 'warning', 'error') NOT NULL DEFAULT 'info',
    message VARCHAR(1000) NOT NULL,
    eventi_processati INT DEFAULT NULL,
    bet_shadow_generate INT DEFAULT NULL,
    duration_ms INT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_channel_created (channel, created_at),
    INDEX idx_level_created (level, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ------------------------------------------------------------
-- Seed iniziale: versione v1.0 vuota, da valorizzare con i parametri
-- reali al primo avvio di main_shadow_engine.py (vedi config.py,
-- step 2 - la funzione export_current_params_snapshot() aggiorna questa
-- riga invece di crearne una nuova al primissimo bootstrap).
-- ------------------------------------------------------------
INSERT INTO shadow_model_versions
    (versione, data_attivazione, descrizione_modifiche, autore, parametri_snapshot_json, is_attiva)
SELECT
    'v1.0',
    NOW(),
    'Bootstrap iniziale AI Research Lab - Shadow Model A/B/C/D/E con parametri di default.',
    'Malu',
    JSON_OBJECT(),
    1
WHERE NOT EXISTS (SELECT 1 FROM shadow_model_versions WHERE versione = 'v1.0');
