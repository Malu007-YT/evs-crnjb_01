# EV Scanner AI - Shadow Intelligence System

## Stato avanzamento

- [x] Step 1/7 - Schema DB + Versioning
- [x] Step 2/7 - config.py (parametri modelli + snapshot versioning)
- [x] Step 3/7 - Model A (Conservative) + Model B (Pure EV)
- [x] Step 4/7 - Stats Engine (ROI, Sharpe, Drawdown, confronto A/B)
- [x] Step 5/7 - Model D (Ensemble)
- [x] Step 6/7 - Model C (Adaptive, online learning)
- [x] Step 7/7 - Model E (AutoML genetico) + Promotion Engine + Dashboard
- [x] **Motore collegato (`main/`)** ← sei qui: DB layer + orchestratore + CLI Model E

Ad ogni step ricevi lo zip completo con tutto il lavoro fatto finora,
non solo il pezzo nuovo.

## Step 1: Schema Database

File: `schema.sql`

### Cosa fa

Crea 7 tabelle, tutte con prefisso `shadow_` per essere immediatamente
riconoscibili come separate dalla produzione:

| Tabella | Scopo |
|---|---|
| `shadow_model_versions` | Versioning del sistema (v1.0, v1.1...), con snapshot JSON dei parametri di tutti i modelli attivi in quel momento |
| `shadow_bets` | Il cuore: una riga per ogni valutazione shadow di un modello su un evento |
| `shadow_bankroll_snapshots` | Bankroll simulato per-modello (ogni modello ha il suo, indipendente) |
| `shadow_model_c_weights` | Storico dei pesi del Model C (Adaptive), un nuovo record ad ogni aggiornamento online |
| `shadow_automl_configs` | Configurazioni testate/scoperte dal Model E (algoritmo genetico), incluse le 3 Champion/Challenger |
| `shadow_promotion_tests` | Storico dei test statistici per promuovere uno shadow model a produzione |
| `shadow_run_log` | Log diagnostico delle esecuzioni del motore shadow |

### Isolamento dalla produzione

- **Lettura**: solo da `eventi` e `scommesse` (per input dati e per
  verificare risultati reali a fine giornata).
- **Scrittura**: mai verso tabelle di produzione. Solo verso le tabelle
  `shadow_*`.
- Le FK verso `eventi.id` usano `ON DELETE CASCADE` (se un evento viene
  eliminato dalla produzione, le valutazioni shadow associate decadono
  con lui — comportamento identico a `scommesse` esistente).

### Come installarlo

Sullo stesso database MySQL/MariaDB di EV Scanner (stesso DB_NAME):

```bash
mysql -u TUO_USER -p TUO_DATABASE < schema.sql
```

Oppure, se preferisci mantenerlo su un database separato (consigliato
se in futuro vuoi hostare lo Shadow Lab su un server diverso da
InfinityFree per via dei limiti sui processi lunghi — vedi nota in
fondo): crea prima un database dedicato, es. `evscanner_shadow`, e
importa lì. In quel caso `eventi`/`scommesse` andranno referenziate
tramite un meccanismo di sync separato (non trattato in questo step:
lo affrontiamo quando arriviamo al motore di ingestione dati in
`main_shadow_engine.py`).

È sicuro rieseguirlo più volte: tutte le `CREATE TABLE` usano
`IF NOT EXISTS` e l'unico `INSERT` iniziale (riga v1.0) usa una guardia
`WHERE NOT EXISTS`.

### Nota su AutoML (Model E) e hosting

Come discusso: gli algoritmi genetici con backtest walk-forward del
Model E **non gireranno come endpoint web su InfinityFree** — stesso
problema di kill dei processi lunghi che affligge già
`worker/scan_web.php`. Li progetteremo come script Python offline che
scrivono i risultati in `shadow_automl_configs`, eseguibili in locale o
altrove, con il solo consumo dei risultati (lettura) esposto lato web
se/quando serve.

---

## Step 2: config.py

File: `config.py`

### Cosa fa

Centralizza tutti i parametri configurabili dei 5 modelli in dataclass
Python tipizzate (una per modello: `ModelAConfig`, `ModelBConfig`,
`ModelCConfig`, `ModelDConfig`, `ModelEConfig`), più una
`ShadowSystemConfig` che le aggrega tutte insieme al bankroll shadow
iniziale e alle soglie del Promotion Engine.

Ogni dataclass ha un metodo `.validate()` che controlla che i valori
abbiano senso (es. `kelly_fraction` in (0,1], pesi della fitness di
Model E che sommano a 1.0) — pensato per essere chiamato **sempre**
prima di scrivere una nuova versione in `shadow_model_versions`, così
un errore di configurazione non può mai finire silenziosamente nel DB.

### Perché parametri così nel dettaglio

Ogni valore ha un commento che spiega il "perché", non solo il "cosa" —
stesso stile che hai già in `config/config.php`. Alcuni punti degni di
nota:

- **Model A**: il decadimento esponenziale della quota è parametrizzato
  con un `lambda=0.15` — a quota 2.00 penalizza l'EV di un fattore
  ~0.86, a quota 5.00 di un fattore ~0.51. Modificabile senza toccare
  il codice del modello (arriverà nello Step 3).
- **Model C**: `feature_names` è dichiarato come "singola fonte di
  verità" — se lo cambi dopo che il modello ha già pesi salvati in
  `shadow_model_c_weights`, quei pesi vanno considerati invalidati
  (serve un nuovo bootstrap). Ho aggiunto un commento esplicito su
  questo per evitare un bug subdolo in futuro.
- **Model E**: i pesi della funzione di fitness (`fitness_peso_roi` +
  `fitness_peso_sharpe` + `fitness_peso_drawdown`) devono sommare
  esattamente a 1.0, altrimenti `validate()` solleva un errore con il
  messaggio esatto di cosa correggere.

### Versioning: come si collega a shadow_model_versions

La funzione chiave è `export_current_params_snapshot()`:

```python
from config import DEFAULT_CONFIG, export_current_params_snapshot

payload = export_current_params_snapshot(
    DEFAULT_CONFIG,
    versione="v1.1",
    descrizione_modifiche="Aumentata soglia EV Model B da 2%% a 3%%",
)
# payload è pronto per un INSERT INTO shadow_model_versions
# (il layer DB arriverà in uno step successivo)
```

Per rileggere/riapplicare i parametri esatti di una versione storica
(es. per rieseguire un backtest con le impostazioni della v1.1 di 3
mesi fa):

```python
from config import ShadowSystemConfig
import json

row = ...  # riga letta da shadow_model_versions
snapshot_dict = json.loads(row["parametri_snapshot_json"])
config_storica = ShadowSystemConfig.from_snapshot_dict(snapshot_dict)
```

`from_snapshot_dict()` è scritto per essere **retrocompatibile**: se in
futuro aggiungi un nuovo parametro a una dataclass, gli snapshot vecchi
che non lo contengono non causano un errore — il campo mancante prende
semplicemente il default corrente. Testato esplicitamente (vedi sotto).

### Come testarlo

```bash
python3 config.py
```

Stampa la configurazione di default validata e lo snapshot JSON che
verrebbe scritto per la versione di bootstrap `v1.0`.

Ho anche fatto girare una suite di 5 test funzionali (round-trip
snapshot, retrocompatibilità con snapshot "vecchi", validazione errori
su Kelly fuori range, validazione errori su pesi fitness non a somma 1,
errore chiaro se mancano le variabili d'ambiente del DB) — tutti
passati. Non è incluso come file di test formale in questo step (lo
strutturiamo meglio quando arriva il layer DB), ma se vuoi te lo giro
subito.

### Connessione database

Le credenziali NON sono hardcoded (a differenza di `config/config.php`,
che gira su hosting condiviso senza un vero meccanismo di secrets).
Vanno impostate come variabili d'ambiente:

```bash
export EVSCANNER_DB_HOST=sql201.infinityfree.com
export EVSCANNER_DB_NAME=if0_42322915_ev_scanner
export EVSCANNER_DB_USER=if0_42322915
export EVSCANNER_DB_PASS=...
```

Oppure con un file `.env` + `python-dotenv` quando arriviamo al motore
di esecuzione vero e proprio (`main_shadow_engine.py`).

---

## Step 3: Model A (Conservative) + Model B (Pure EV)

File: `models/model_a_conservative.py`, `models/model_b_pure_ev.py`

### Cosa fanno

Entrambi i modelli sono **filtri euristici deterministici**, non
modelli che imparano: stessi input → stesso output sempre. Ricevono un
`EventoInput` (evento + una selection 1/X/2 candidata) e restituiscono
una `ValutazioneShadow` con `accepted=True/False`. Se accettata,
contiene già EV, Kelly e tutto il necessario per un `INSERT` in
`shadow_bets`. Se scartata, `motivo_scarto` spiega esattamente perché —
niente scarti silenziosi, stesso spirito di log dettagliato che hai già
in `SchemaSync.php`/`config.php`.

**Le formule EV e Kelly sono un porting 1:1** di
`includes/functions.php::calcola_ev()` e `calcola_kelly()` — stessa
matematica di produzione, non una reinvenzione. L'ho verificato con un
confronto numerico incrociato (5 casi test, implementazione
indipendente vs modulo): coincidenza esatta su tutti.

### Model A — filtri applicati in ordine

1. Mercato consentito (`mercati_consentiti`, di default solo `1X2`)
2. Lega ad alta liquidità (sottostringa case-insensitive su
   `campionato`, stesso approccio pragmatico di
   `THE_ODDS_API_LEAGUE_MAP` in `config.php`)
3. Quota massima assoluta (tetto duro, sopra il quale scarta sempre)
4. **EV penalizzato** con decadimento esponenziale sulla quota:
   `ev_penalizzato = ev_pct * exp(-lambda * (quota - 1))` — la
   penalizzazione si applica solo se l'EV grezzo è positivo (altrimenti
   scontare un EV negativo lo renderebbe "meno negativo", invertendo
   l'intento del filtro)
5. CLV stimato minimo (con policy configurabile se il dato manca:
   `scarta` di default, o `ignora_filtro_clv`)

Il sizing Kelly finale usa però **probabilità/quote reali**, non quelle
penalizzate: la penalizzazione è solo un criterio di ammissione, non
deve distorcere il calcolo matematico dello stake.

### Model B — filtro unico

Solo sanity check sui dati sorgente (quota `<= 1.0` o sopra
`quota_massima_sanity_check=100.0`, che è pulizia dati non giudizio di
rischio) + soglia EV puro. Nessun filtro lega, nessun tetto quota
"morbido", nessun CLV — esattamente la filosofia "Pure EV" della
progettazione.

### Test fatti

- Compilazione pulita (`py_compile`) di entrambi i moduli.
- Self-test eseguibili direttamente (`python3 models/model_a_conservative.py`
  e stesso per B) con 4 casi ciascuno, output verificato a mano.
- Confronto numerico incrociato EV/Kelly con implementazione
  indipendente (non riusa le funzioni del modulo) su 5 coppie
  probabilità/quota: **coincidenza esatta**.
- Verificato che Kelly vada correttamente a `0.0` quando l'EV è
  negativo (mai puntare contro il proprio edge).
- Verificato numericamente il fattore di decadimento di Model A contro
  i valori documentati nel commento di `config.py` (quota 2.00 → ~0.86,
  quota 5.00 → ~0.51): coincidono.
- Verificato con un caso mirato che il filtro CLV mancante scarti
  correttamente quando raggiunto (nei casi test "di vetrina" l'EV
  scartava già prima nella pipeline, quindi ho aggiunto un test
  dedicato per isolare quel ramo specifico).

### Come provarli

```bash
cd ev_scanner_shadow_lab
PYTHONPATH=. python3 models/model_a_conservative.py
PYTHONPATH=. python3 models/model_b_pure_ev.py
```

(Il `PYTHONPATH=.` serve perché entrambi i moduli fanno
`from config import ...` come import assoluto dalla root del pacchetto
— lo stesso varrà per `main_shadow_engine.py` quando lo eseguirai
normalmente dalla root.)

### Nota di design: EventoInput/ValutazioneShadow condivisi

Le due dataclass di input/output sono definite in
`model_a_conservative.py` e importate da `model_b_pure_ev.py` invece di
essere duplicate o già spostate in un file comune dedicato
(`models/common.py`). Con solo 2 modelli scritti finora spostarle
sarebbe stata un'astrazione prematura; quando arriva Model D
(Ensemble, step 5) — che consuma gli output di A/B/C insieme — verrà
naturale deciderlo con più informazione su come vengono davvero usate.

---

## Step 4: Stats Engine

File: `utils/stats_engine.py`

### Cosa fa

A differenza degli step precedenti, qui non c'era nulla da portare da
`functions.php`: Sharpe Ratio, Max Drawdown e Profit Factor sono
metriche quantitative nuove, non presenti in produzione. Le ho
implementate seguendo le definizioni standard del betting/trading
sistematico, non un'invenzione ad-hoc.

Il modulo è **puro**: prende in input liste di bet già concluse
(`BetSettled`, o dict equivalenti da una query DB) e non fa mai query
SQL da solo, non importa nulla da `models/` o `config.py`. Questo lo
rende testabile in isolamento e riusabile sia dal motore principale sia
da un notebook di analisi ad-hoc.

**Funzioni principali:**

- `calcola_metriche(bets, bankroll_iniziale)` → blocco completo:
  Performance (ROI, Yield, Win Rate, profitto), Rischio (Max Drawdown
  con profondità *e* durata in numero di bet, Profit Factor, Sharpe
  Ratio, varianza), Modello (CLV medio, EV teorico vs reale, Kelly
  medio), più le serie temporali per i grafici (curva bankroll
  cumulativo, drawdown series per l'istogramma).
- `heatmap_profitto(bets, dimensione)` → profitto/ROI raggruppati per
  campionato o mercato, ordinati per profitto decrescente.
- `segmenta_per_periodo(bets, periodo, riferimento, da, a)` → filtra le
  bet secondo tutti i periodi richiesti dalla progettazione (oggi, 7/30
  giorni, mese corrente/precedente, anno corrente, storico totale,
  range personalizzato). `segmenta_singolo_mese()` a parte per "Luglio
  2026" e simili, che non sono relativi a "oggi".
- `confronta(metriche_a, metriche_b, label_a, label_b)` → il Widget di
  Confronto Intelligente: per ogni metrica calcola differenza e
  indicatore ▲/▼/≈. Sa quali metriche sono "meglio se più alte" (ROI,
  Sharpe...) e quali "meglio se più basse" (Max Drawdown), e applica una
  soglia di variazione relativa (3%%) sotto la quale considera il
  risultato invariato invece di segnalare rumore statistico come un
  peggioramento/miglioramento reale.

### Decisioni di design degne di nota

- **ROI vs Yield**: nel value betting sono spesso sinonimi (a
  differenza dell'investing tradizionale). Ho tenuto i due campi
  distinti nello schema output — coincidono numericamente oggi, ma
  lasciano spazio a una futura divergenza (es. Yield calcolato solo
  sulle bet "Smart Filter", come già fai col Bankroll Verde in
  produzione) senza dover rifare lo schema.
- **Profit Factor** resta `None` (non "infinito") quando non ci sono
  perdite — un valore infinito non è rappresentabile in modo utile in
  dashboard, meglio che il chiamante lo gestisca esplicitamente
  mostrando "∞" o "N/D".
- **Sharpe Ratio** calcolato sui rendimenti per-singola-bet (non
  aggregati per giorno), annualizzato con un fattore √250 come
  riferimento generico per renderlo comparabile tra periodi di
  lunghezza diversa — è un indicatore relativo, non uno standard
  assoluto di settore (il volume di bet piazzate varia molto rispetto a
  un vero calendario di trading).
- **Max Drawdown** calcolato sul bankroll (iniziale + cumulato), non
  sul solo profitto — è quello che un utente si aspetta leggendo "sono
  in drawdown del 12%%" in dashboard.

### Test fatti

- Compilazione pulita e self-test eseguibile
  (`PYTHONPATH=. python3 utils/stats_engine.py`), 6 blocchi di test:
  1. Metriche complete su un dataset sintetico di 10 bet costruito ad
     hoc per generare un drawdown riconoscibile.
  2. **Verifica indipendente**: stake totale, profitto e ROI ricalcolati
     a mano fuori dal modulo e confrontati — coincidenza esatta.
  3. Segmentazione temporale (ultimi 7 giorni, range personalizzato).
  4. Heatmap per campionato.
  5. Confronto A/B tra due metà dello stesso dataset (la seconda metà è
     nettamente migliore — tutti gli indicatori risultano ▲, coerente).
  6. **Casi limite**: lista vuota (nessun crash, valori di default) e
     dataset con zero bet perse (Profit Factor → `None`, non un errore
     di divisione per zero).

### Come provarlo

```bash
cd ev_scanner_shadow_lab
PYTHONPATH=. python3 utils/stats_engine.py
```

---

## Step 5: Model D (Ensemble)

File: `models/model_d_ensemble.py`

### Cosa fa

A differenza di A/B (filtri deterministici che guardano i dati grezzi
dell'evento), Model D **non valuta l'evento**: consuma le
`ValutazioneShadow` già prodotte da A, B e (quando arriverà, step 6) C
sullo stesso `event_id`+`selection`, e le combina in un punteggio di
consenso 0-100.

**Logica di scoring:**

- Se **tutti e 3** i modelli concordano → score al massimo (100 di
  default), indipendentemente da qualunque Sharpe.
- Con consenso **parziale** (es. solo 2 su 3), lo score è
  `fattore_copertura × fattore_qualità`, dove:
  - `fattore_copertura` = quanti modelli concordano sul totale possibile
    (2/3 pesa più di 1/3) — è la componente **dominante** per design:
    ho verificato esplicitamente (Test 4) che 2 modelli concordi con
    Sharpe pessimo battano comunque 1 modello concorde con Sharpe
    ottimo, perché il consenso in sé è il segnale primario richiesto
    dalla progettazione, non la performance del singolo modello.
  - `fattore_qualità` = quanto sono stati performanti di recente i
    modelli concordi (Sharpe Ratio nella finestra configurata, default
    30 giorni), ma **troncato tra 0.7 e 1.0** — un pessimo Sharpe
    recente da solo non può affossare uno score altrimenti supportato
    da un buon consenso.
- Lo score con consenso parziale non raggiunge **mai** il massimo
  (troncato a `max - 0.01`): un 2-su-3 non deve mai "colpire" lo stesso
  punteggio del 3-su-3 per arrotondamento.

**EV e Kelly** sono medie pesate (stessi pesi Sharpe) dei modelli
concordi — Model D non ha una propria coppia probabilità/quota da cui
derivare un EV indipendente, essendo un consenso su segnali eterogenei.
Il Kelly finale scala tra il 50% e il 100% della base configurata in
base a quanto lo score è vicino alla soglia minima o al massimo.

### `calcola_pesi_sharpe()`

Riusa direttamente `utils.stats_engine.calcola_metriche()` per il
calcolo Sharpe — un solo punto di verità per quella formula in tutto il
progetto, invece di reimplementarla qui. Se un modello ha meno bet
settled del minimo configurato nella finestra, riceve un peso neutro
(1.0) invece del suo Sharpe vero: uno Sharpe calcolato su 3 bet è
rumore, non segnale. I pesi non scendono mai sotto 0.05 (mai zero o
negativi): un peso zero escluderebbe del tutto un modello che ha
comunque concordato, un peso negativo capovolgerebbe il suo contributo
nella direzione sbagliata nella media pesata.

### Robustezza

Il modulo solleva `ValueError` esplicito (non ignora silenziosamente)
in due casi che indicherebbero un bug nel chiamante:
- un segnale con `accepted=False` arrivato a `valuta()` (il filtraggio
  deve avvenire prima, a monte)
- due segnali dallo stesso `model_source` per lo stesso evento+selection
  (non dovrebbe mai succedere data la `UNIQUE KEY` su `shadow_bets`)

### Test fatti

10 blocchi di test, tutti con assert automatici oltre alla stampa
leggibile:

1. Consenso totale → score esattamente al massimo, Kelly al 100% base
2. Consenso parziale (2/3) → score sotto il massimo, sopra soglia
3. Singolo modello → score più basso del consenso parziale
4. **Copertura vs qualità**: 2 concordi con Sharpe pessimo (score
   47.17) battono 1 concorde con Sharpe ottimo (score 33.33) — verifica
   diretta del comportamento chiave richiesto dalla progettazione
5. EV medio pesato pende verso il modello con Sharpe più alto
6. Nessun segnale → scarto pulito, nessuna eccezione
7. `calcola_pesi_sharpe()` con storico insufficiente → peso neutro
8. `calcola_pesi_sharpe()` con storico sufficiente → Sharpe reale
9. Segnale non accettato in input → `ValueError`
10. Model source duplicato in input → `ValueError`

### Come provarlo

```bash
cd ev_scanner_shadow_lab
PYTHONPATH=. python3 models/model_d_ensemble.py
```

### Nota per lo Step 6

Quando arriverà Model C, non serviranno modifiche a questo file: la
costante `MODELLI_SORGENTE_NOTI` include già `'model_c'`, e la logica
di consenso/scoring è già scritta per gestire correttamente tutti e 3 i
modelli (i test con solo A+B "simulano" oggi lo scenario che sarà reale
finché C non è ancora online).

---

## Step 6: Model C (Adaptive)

File: `models/model_c_adaptive.py`

Questo è il modello più delicato del progetto, come discusso all'inizio
(vera matematica ML online, non solo regole/medie). Ho scelto
**deliberatamente** l'algoritmo più semplice che soddisfa la
progettazione — **online logistic regression con SGD** — invece di
qualcosa di più sofisticato: un modello lineare è interamente
ispezionabile, ogni peso ha un segno interpretabile, ogni predizione è
riproducibile a mano con carta e penna, e la Feature Importance
richiesta dalla progettazione è semplicemente il vettore dei pesi
stesso (le feature sono già a z-score, quindi già comparabili tra
loro — non serve SHAP o altre tecniche di post-hoc explainability, che
sarebbero over-engineering qui).

### Come funziona

1. **Normalizzazione online (Welford, 1962)**: ogni feature viene
   standardizzata a z-score con media/varianza calcolate
   incrementalmente, senza mai dover ricalcolare da zero su tutto lo
   storico e senza soffrire di cancellazione catastrofica (a differenza
   della formula "naive" E[x²]-E[x]²).
2. **Modello**: regressione logistica standard,
   `p = sigmoid(w·z + b)`.
3. **Update SGD** con regolarizzazione L2 (mai sul bias, per
   convenzione), learning rate con decadimento `1/√t` (`t` = numero di
   *batch* di aggiornamento, non di singole bet — coerente con
   `aggiorna_ogni_n_bet_concluse` che conta bet-per-batch).
4. **10 feature** (`config.py::feature_names`), incluso un **encoding
   ciclico seno/coseno** per ora del giorno e giorno della settimana —
   necessario perché un modello lineare tratterebbe "ora=23" e "ora=0"
   come agli antipodi, quando sono adiacenti nel tempo.
5. **Bootstrap**: sotto `minimo_bet_per_pesi_appresi` (default 50), il
   modello opera con pesi euristici pre-impostati invece di decisioni
   premature su 3-4 osservazioni.

### Decisioni di design degne di nota

- **Sigmoid numericamente stabile**: per input molto negativi la forma
  naive `1/(1+exp(-x))` causerebbe overflow in Python puro; uso la
  forma equivalente `exp(x)/(1+exp(x))` per x negativi, verificata fino
  a x=-1000 senza errori.
- **SGD vero, non batch gradient descent**: dentro un batch, ogni
  esempio aggiorna subito i pesi che il prossimo esempio nello stesso
  batch vedrà già aggiornati (incluso il normalizzatore, che evolve
  esempio-per-esempio all'interno del batch stesso).
- **Diagnostica pre-update**: log-loss e accuracy sono calcolate sul
  modello *prima* di vedere gli esempi di quel batch — una metrica di
  generalizzazione onesta, non contaminata dall'aver appena visto la
  risposta giusta.
- **`aggiorna_pesi()` non muta lo stato in place**: ritorna un nuovo
  `StatoModelCAdaptive`, coerente con `shadow_model_c_weights` che
  richiede una nuova riga ad ogni update (mai un `UPDATE`, vedi
  `schema.sql`).
- **Kelly di Model C** è scalato dalla confidenza del modello stesso
  (la probabilità di output), non da una frazione fissa come A/B — un
  modello più sicuro di sé investe più vicino al Kelly pieno, coerente
  con lo spirito "quantitativo dinamico" della progettazione.
- **Guardia esplicita sull'ordine delle feature**: se lo stato del
  modello è stato costruito con un `feature_names` diverso da quello
  attuale in `config.py`, il costruttore solleva subito `ValueError`
  invece di produrre predizioni silenziosamente sbagliate — coerente
  col commento in `config.py` su cosa succede se si cambia quell'ordine
  dopo che il modello ha già pesi salvati.

### Test fatti

8 blocchi di test, con particolare attenzione alla correttezza
matematica visto che è la parte più rischiosa del progetto:

1. **Welford vs formula diretta**: media e varianza calcolate
   incrementalmente confrontate con la formula "a libro" su 8 valori —
   coincidenza esatta.
2. Sigmoid su valori noti (0 → 0.5) e input estremi (±1000) — nessun
   overflow.
3. Bootstrap + inferenza a freddo (zero storico).
4. **Training end-to-end**: dataset sintetico di 200 esempi
   chiaramente separabile in due gruppi (EV/CLV alti → quasi sempre
   vinta, bassi → quasi sempre persa). L'accuracy media sale dal 66.7%
   dei primi 3 batch al 73.3% degli ultimi 3, ben sopra il 50% casuale
   — il modello impara davvero, non solo "gira senza errori".
5. Feature importance dopo training — `ev_pct` e `clv_stimato_pct`
   emergono correttamente come le più rilevanti, coerente col dataset
   sintetico costruito apposta su quelle due dimensioni.
6. Robustezza: `feature_names` disallineate → `ValueError` esplicito.
7. Robustezza: batch vuoto → `ValueError` esplicito.
8. Schedule del learning rate `1/√t` verificato numericamente su 3
   batch consecutivi — coincidenza esatta.

**Oltre al self-test**, ho fatto **due verifiche indipendenti
aggiuntive** (non incluse nel file, eseguite a parte) per validare
l'update SGD a mano, cifra per cifra:
- Un singolo esempio nel caso limite di varianza zero (primo dato mai
  visto da un normalizzatore): bias e tutti i 10 pesi ricalcolati a
  mano con la formula del gradiente — coincidenza esatta.
- Due esempi con varianza reale, per validare anche il ramo normale
  della normalizzazione Welford *mentre* evolve dentro il batch SGD
  (non prima o dopo) — coincidenza esatta anche qui.

Un bug è emerso durante lo sviluppo (non nel modulo, in un confronto
del test 8 troppo stretto rispetto a un arrotondamento nella stampa) —
corretto e verificato.

### Come provarlo

```bash
cd ev_scanner_shadow_lab
PYTHONPATH=. python3 models/model_c_adaptive.py
```

---

## Step 7: Model E (AutoML) + Promotion Engine + Dashboard

Ultimo step, tre pezzi. File: `models/model_e_automl.py`,
`utils/promotion_engine.py`, `dashboard/ShadowLabDashboard.jsx`.

### Model E — AutoML genetico

**Eseguito SOLO offline**, come discusso fin dall'inizio: un genetico
con backtest walk-forward su decine di configurazioni × generazioni non
può girare su InfinityFree (stesso problema di kill dei processi lunghi
di `worker/scan_web.php`). Va lanciato da CLI/cron locale o un host con
processi lunghi; solo la *lettura* dei risultati (non l'esecuzione)
potrà stare dietro un endpoint web.

**Deliberatamente riusa Model A** come fenotipo: un individuo del
genetico non è altro che un `ModelAConfig` con parametri diversi. Non
inventa una logica di scoring parallela — stesso principio di single
source of truth già visto con `calcola_pesi_sharpe` che riusa lo Stats
Engine invece di reimplementare Sharpe.

**Anti-overfitting**, il rischio concreto di qualunque genetico su dati
storici (con abbastanza generazioni, trova sempre qualcosa che ha
performato bene per puro rumore):
- **Walk-forward validation a blocchi**: storico diviso in fold
  temporali consecutivi, fitness calcolata **solo sui segmenti di test
  out-of-sample** di ciascun fold, mai sul training.
- **Fitness composita** (ROI 45% + Sharpe 35% − Drawdown 20%, pesi
  configurabili): un ROI alto con drawdown devastante o Sharpe pessimo
  non vince.
- **Elitismo limitato** (2 su 30): il resto della popolazione viene
  sempre rigenerato, per non convergere prematuramente su un ottimo
  locale di rumore.
- Un intero ciclo viene rifiutato a priori sotto la soglia minima di
  dati (`minimo_bet_per_backtest`, default 200).

**Operatori genetici**: crossover uniforme (ogni gene ereditato a caso
da uno dei due genitori — più adatto di single-point per un genoma
corto a 5 geni), mutazione per ri-campionamento nel range (non
perturbazione incrementale, per non dover tarare una deviazione
standard diversa per ogni gene con scale molto diverse tra loro),
selezione a torneo (robusta a fitness negative, a differenza della
roulette-wheel classica).

**Test**: 9 blocchi, incluso un **test di riproducibilità** cruciale
per il debug in produzione (stesso seed → stesso risultato esatto,
verificato bit a bit sui 3 champion), e un dataset sintetico con edge
positivo costruito ad hoc per verificare che il genetico trovi
effettivamente configurazioni profittevoli (non solo "gira senza
errori").

### Promotion Engine

Implementa le 3 condizioni della progettazione in sequenza (si ferma al
primo fallimento): (1) minimo bet out-of-sample, (2) **Z-test a due
campioni indipendenti** (Welch, varianze non assunte uguali) sulla
differenza dei rendimenti medi, (3) ROI **e** Sharpe del candidato
superiori alla produzione — la sola significatività statistica non
basta, un candidato potrebbe essere "significativamente diverso" ma
*peggiore*.

**Onestà statistica esplicita nel modulo**: un p-value basso non
dimostra superiorità causale, dimostra solo che la differenza osservata
è improbabile sotto l'ipotesi nulla dato *quel* campione. Restano tutti
i limiti standard di un test su dati storici di betting (non
stazionarietà del mercato, drift dei bookmaker, multiple-testing se si
testano molti modelli in sequenza).

**Bonus — Platt Scaling** (calibrazione probabilità, richiesto dalla
progettazione in "Sviluppo Avanzato"): parametrico a 2 soli parametri
(A, B), allenato con lo stesso stile SGD già visto in Model C ma a
batch pieno (non online — la calibrazione va ri-allenata
periodicamente su uno storico, non bet-per-bet). Preferito a Isotonic
Regression per semplicità, coerenza di stile col resto del progetto, e
perché una funzione a gradini monotona sarebbe fuori scope per questo
step.

**Test**: 8 blocchi. Il più convincente è la verifica che la
calibrazione riduca concretamente il bias — su un dataset con
probabilità grezze sistematicamente sovrastimate del 15%, la distanza
dalla vera frequenza di vittoria scende da 15.22 punti percentuali a
0.08 dopo la calibrazione. I p-value ai punti di riferimento standard
(z=1.96→p≈0.05, z=2.576→p≈0.01) coincidono esattamente con le tabelle
statistiche note.

### Dashboard

`dashboard/ShadowLabDashboard.jsx` — mockup React funzionante (usa
`recharts`, disponibile nell'ambiente artifact). Direzione visiva:
**terminal quantitativo**, non un dashboard SaaS generico — sfondo
quasi-nero, monospace per ogni numero (i numeri sono il prodotto qui),
verde/rosso finanziario standard per positivo/negativo, oro per i
marker di versione. Dati mock generati con un PRNG deterministico (seed
fisso), quindi lo stesso mockup è riproducibile identico ad ogni
apertura.

Copre tutte le sezioni richieste dalla progettazione:
- Metriche Performance/Rischio/Modello per modello selezionato
- **Curva bankroll cumulativo** con marker verticali oro sulle date di
  rilascio versione (toggle per mostrare/nascondere ogni modello)
- **Widget di Confronto Intelligente**: selettori A/B liberi tra
  qualunque coppia di modelli, indicatori ▲▼≈ coerenti con la logica di
  `stats_engine.confronta()` (Step 4)
- **Timeline interattiva** degli aggiornamenti versione
- Heatmap profitto per campionato
- Feature Importance di Model C (coerente con lo Step 6: EV e CLV
  emergono come le feature più rilevanti)
- Tabella Champion/Challenger di Model E

### Note finali di progetto

Tutti e 5 gli Shadow Model sono ora implementati e testati
individualmente. Quello che **non** è incluso in questi 7 step, perché
esula da "architettura + algoritmi + schema + logica di confronto +
mockup dashboard" richiesto dalla progettazione originale, e
richiederebbe la sua iterazione dedicata:

- `main_shadow_engine.py` (il motore che orchestra A→B→C→D, chiama
  `calcola_pesi_sharpe()`, ingesta da `eventi`, scrive su `shadow_bets`)
- Il layer di persistenza DB reale (oggi ogni modulo lavora su
  strutture dati Python pure, mai su una vera connessione MySQL)
- L'integrazione della Dashboard con dati live invece di quelli mock
- Lo script CLI per lanciare `esegui_ciclo_evolutivo()` offline

Quando sarai pronto a cablare questi pezzi insieme, ha senso ripartire
da lì con la stessa logica step-by-step.

---

## Il motore collegato: `main/`

Fino a qui ogni pezzo esisteva ma non era attaccato a niente: modelli
richiamabili solo da CLI con dati finti, tabelle DB vuote, dashboard con
dati mock. Questi 4 file sono il collegamento vero.

⚠️ **Prima di tutto**: ho notato che `config/config.php` del progetto
PHP ha le credenziali del database **scritte in chiaro nel codice**
(`DB_HOST`/`DB_USER`/`DB_PASS`). Non le ho usate né scritte da nessuna
parte, ma vale la pena spostarle in variabili d'ambiente appena hai un
attimo — se quel file finisce mai in un repo pubblico o in uno zip
condiviso con qualcun altro, è accesso completo al tuo database.

### File

- **`main/db_layer.py`** — il ponte tra il mondo Python (strutture dati
  pure, `EventoInput`/`BetSettled`) e MySQL vero. Legge `eventi` e
  `scommesse` (sola lettura, mai scrittura — stesso isolamento
  dichiarato fin dallo Step 1), scrive `shadow_bets`/`shadow_run_log`.
  Usa `pymysql` (pure Python, nessuna compilazione nativa richiesta) e
  legge le credenziali **solo** da variabili d'ambiente via
  `python-dotenv`.
- **`main/main_shadow_engine.py`** — l'orchestratore: legge eventi
  imminenti, li fa valutare da A/B/C, combina con D, scrive tutto.
  **Non include Model E** (resta deliberatamente offline/manuale, vedi
  sotto).
- **`main/run_model_e_ciclo.py`** — script CLI **separato**, da lanciare
  **solo manualmente**, mai da cron. Un ciclo genetico completo può
  richiedere minuti — esattamente il tipo di durata che ha già causato
  il bug documentato nei commenti di `worker/resolve_bets_web.php` su
  InfinityFree.
- **`main/.env.example`** — copialo in `.env` (mai da committare) e
  valorizzalo con le tue credenziali reali.

### Decisioni di design degne di nota

- **Mapping schema reale → Shadow Lab**: `eventi` ha probabilità/quote
  per tutte e 3 le selection insieme (`prob_1/x/2`, `quota_1/x/2`);
  ogni riga viene espansa in fino a 3 `EventoInputEsteso` (una per
  selection valorizzata — gli sport senza pareggio, es. basket, saltano
  automaticamente la `X`). Stessa priorità quota già in uso lato PHP:
  `quota_real_N` (odds-api.io) batte `quota_N` (Sbancobet) quando
  disponibile.
- **`smart_filter_score`** (0–1, atteso da Model C) non esiste come
  colonna diretta: deriva dal `voto` 1–10 già calcolato in produzione
  (`calcola_voto_bet()`) con normalizzazione lineare `(voto-1)/9`. Un
  evento mai tracciato come bet reale non ha un voto: usa `0.5`
  (neutro), stessa convenzione già documentata in `EventoInputEsteso`.
- **Idempotenza**: `inserisci_shadow_bets()` usa
  `INSERT ... ON DUPLICATE KEY UPDATE` sulla `UNIQUE KEY` di
  `shadow_bets` — se il motore viene rilanciato sullo stesso batch
  (es. dopo un crash a metà), aggiorna invece di duplicare.
- **Budget di tempo per fase**, stesso pattern già in uso in
  `worker/resolve_bets_web.php` dopo aver letto il commento sul bug
  storico documentato lì dentro: nessun fire-and-forget, ogni fase
  controlla il tempo residuo *prima* di partire (mai a metà), un
  fallimento è visibile subito invece che dedotto indirettamente da
  dati mancanti.
- **Model C sempre in bootstrap** in questa prima versione del motore:
  non c'è ancora un meccanismo di caricamento/salvataggio dei pesi
  appresi da/verso `shadow_model_c_weights`. È il prossimo pezzo
  naturale da collegare — oggi Model C gira, ma non impara ancora
  davvero da un ciclo all'altro.

### Test fatti

- Compilazione pulita di tutti e 3 i moduli.
- **Test end-to-end del motore** con tutte le funzioni `db_layer`
  sostituite da mock in-memory (nessun vero DB necessario per
  verificare la *logica*, non solo la sintassi): 5 eventi fittizi → il
  motore legge, calcola pesi Sharpe, fa valutare A/B/C, combina con D,
  produce 9 righe `shadow_bets` pronte per la scrittura.
- **Verifica della logica di espansione eventi**: evento calcio
  completo (3 candidate, priorità `quota_real` confermata), evento
  senza pareggio tipo basket (2 candidate, `X` correttamente saltata),
  quota non valida (`<=1.0`, selection scartata).
- **Verifica normalizzazione voto→score**: 1→0.0, 10→1.0, 5.5→0.5,
  `None`→0.5 (neutro).
- Durante il test end-to-end è emerso un comportamento che sembrava un
  bug ma non lo era: i pesi Sharpe di un dataset di test sintetico
  uscivano troncati al minimo (0.05). Verificato che fosse corretto —
  quel dataset aveva davvero Sharpe negativo per come l'avevo costruito
  io. Confermato con un secondo dataset a edge positivo reale che il
  peso riflette correttamente lo Sharpe quando è positivo.

### Come usarlo

```bash
cd ev_scanner_shadow_lab/main
pip install -r requirements.txt
cp .env.example .env
# modifica .env con le tue credenziali reali (stesse di config/config.php)

# Ciclo ricorrente (A/B/C/D) — da schedulare periodicamente, es. dopo lo scan PHP:
python3 main_shadow_engine.py

# Ciclo AutoML — SOLO manuale, mai da cron:
python3 run_model_e_ciclo.py
```

### Cosa manca ancora

- **Persistenza dei pesi di Model C** tra un ciclo e l'altro (oggi
  riparte sempre da bootstrap) — serve un `carica_stato_model_c()` /
  `salva_stato_model_c()` in `db_layer.py` che legga/scriva
  `shadow_model_c_weights`, più la logica di trigger
  (`aggiorna_ogni_n_bet_concluse`) nel motore.
- **Settlement delle bet shadow**: oggi nulla popola mai
  `shadow_bets.result`/`profit_loss_shadow`/`settled_at` — serve un
  equivalente Python di `ResultResolver` (o un riuso via lettura da
  `scommesse`/`eventi` quando l'evento è concluso).
- **Integrazione Dashboard con dati reali** invece dei mock.
- Un vero **scheduler** (cron/GitHub Actions) che invochi
  `main_shadow_engine.py` periodicamente, coerente con quello già in
  uso per lo scan PHP.
