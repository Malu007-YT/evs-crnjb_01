"""
EV Scanner AI - Shadow Intelligence System
Step 7/7 (parte A): models/model_e_automl.py - Shadow Model E (AutoML)
----------------------------------------------------------------
ESEGUITO SOLO OFFLINE. Come discusso fin dall'inizio del progetto: un
algoritmo genetico con backtest walk-forward su centinaia di
configurazioni x generazioni non puo' girare come endpoint web su
InfinityFree (stesso problema di kill dei processi lunghi che affligge
gia' worker/scan_web.php in produzione). Questo modulo e' pensato per
essere lanciato da CLI/cron locale o su un host con processi lunghi
disponibili, che scrive i risultati in shadow_automl_configs - la sola
LETTURA di quei risultati (non l'esecuzione del genetico) puo' stare
dietro un endpoint web, se/quando servira'.

----------------------------------------------------------------
GENOMA E SPAZIO DI RICERCA
----------------------------------------------------------------
Un "individuo" della popolazione e' una configurazione candidata con 5
geni (vedi ModelEConfig in config.py per i range):
    ev_minimo_pct, quota_massima, kelly_fraction, clv_minimo_pct, mercato

Il fenotipo (come si comporta una configurazione sui dati) e' valutato
riusando ESATTAMENTE la stessa logica di Model A (models/model_a_conservative.py):
un individuo del genetico non e' altro che un ModelAConfig con parametri
diversi, applicato in backtest. Questo e' deliberato: Model E cerca
varianti della filosofia "filtro a soglie" di A/B, non inventa una
logica di scoring parallela - single source of truth per "cosa succede
dato un set di soglie" (stessa idea gia' vista con calcola_pesi_sharpe
che riusa stats_engine invece di reimplementare Sharpe).

----------------------------------------------------------------
METODOLOGIA ANTI-OVERFITTING (walk-forward validation)
----------------------------------------------------------------
Il rischio concreto di un algoritmo genetico su dati storici e' il
"backtest overfitting": con abbastanza generazioni, il genetico trovera'
SEMPRE una configurazione che ha performato benissimo sui dati passati
per puro rumore statistico, non per un vero edge. Contromisure adottate:

1. WALK-FORWARD invece di un singolo backtest su tutto lo storico: lo
   storico ordinato temporalmente viene diviso in walk_forward_num_fold
   segmenti consecutivi. Per ciascun fold, l'ultima
   walk_forward_test_size_pct% e' test out-of-sample, il resto e'
   "visibile" per quel fold. La fitness finale di un individuo e' la
   MEDIA delle fitness sui soli segmenti di TEST di ciascun fold, mai
   sui dati di training - un individuo che performa bene solo su
   training e male su ogni test fold viene penalizzato correttamente,
   invece di premiare la semplice memorizzazione del passato.

2. MINIMO_BET_PER_BACKTEST: un intero ciclo di ottimizzazione viene
   rifiutato a priori se lo storico disponibile e' troppo piccolo (vedi
   config.py) - nessuna conclusione statistica affidabile e' possibile
   sotto quella soglia, quindi e' meglio non generare nessuna
   configurazione piuttosto che generarne una basata su rumore.

3. FITNESS COMPOSITA (non solo ROI): pesata su ROI, Sharpe Ratio e
   Max Drawdown (vedi config.py, pesi sommano a 1.0). Un individuo che
   ottiene un ROI alto ma con un drawdown devastante o uno Sharpe
   pessimo (varianza altissima) NON vince: questo scoraggia il
   genetico dal convergere su configurazioni fragili che hanno
   funzionato "per fortuna" su un singolo run di dati fortunati.

4. ELITISMO LIMITATO (elitismo_top_n, default 2 su popolazione 30): solo
   una minima parte della popolazione passa invariata; il resto viene
   sempre rigenerato via crossover/mutazione, per continuare a esplorare
   invece di convergere prematuramente su un ottimo locale di rumore.

Nessuna di queste misure elimina il rischio di overfitting al 100% (e'
strutturalmente impossibile con dati storici finiti), ma lo riduce in
modo sostanziale rispetto a un genetico "naive" ottimizzato su un solo
ROI aggregato senza validazione out-of-sample.
----------------------------------------------------------------
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, List, Optional, Tuple

from config import ModelEConfig
from models.model_a_conservative import ModelAConservative, EventoInput
from utils.stats_engine import BetSettled, calcola_metriche, MetrichePerformance


# ==================================================================
# Genoma
# ==================================================================
@dataclass
class Genoma:
    """
    Una singola configurazione candidata. I 5 geni corrispondono 1:1 ai
    campi che servono per costruire un ModelAConfig equivalente (vedi
    a_config_da_genoma() sotto) - il genetico non introduce parametri
    che Model A non capirebbe gia'.

    decadimento_quota_lambda e leghe_alta_liquidita_keywords di Model A
    NON sono geni qui: la progettazione limita esplicitamente lo spazio
    di ricerca del genetico a
    "soglie di EV, limiti di quota, mercati consentiti, coefficienti
    Kelly, e soglie di CLV" - il genetico esplora quei 5 assi, non
    reinventa la filosofia di penalizzazione quota o i filtri di lega,
    che restano fissi ai default di Model A durante il backtest.
    """
    ev_minimo_pct: float
    quota_massima: float
    kelly_fraction: float
    clv_minimo_pct: float
    mercato: str

    def a_tuple_ordinata(self) -> tuple:
        """Rappresentazione posizionale stabile, utile per crossover/confronti."""
        return (self.ev_minimo_pct, self.quota_massima, self.kelly_fraction, self.clv_minimo_pct, self.mercato)

    def to_dict(self) -> dict:
        """Per la colonna shadow_automl_configs.parametri_json."""
        return {
            "ev_minimo_pct": self.ev_minimo_pct,
            "quota_massima": self.quota_massima,
            "kelly_fraction": self.kelly_fraction,
            "clv_minimo_pct": self.clv_minimo_pct,
            "mercato": self.mercato,
        }

    @staticmethod
    def from_dict(d: dict) -> "Genoma":
        return Genoma(
            ev_minimo_pct=d["ev_minimo_pct"], quota_massima=d["quota_massima"],
            kelly_fraction=d["kelly_fraction"], clv_minimo_pct=d["clv_minimo_pct"], mercato=d["mercato"],
        )


def genoma_random(cfg: ModelEConfig, rng: random.Random) -> Genoma:
    """Campiona un individuo uniformemente nei range configurati."""
    return Genoma(
        ev_minimo_pct=round(rng.uniform(*cfg.range_ev_minimo_pct), 3),
        quota_massima=round(rng.uniform(*cfg.range_quota_massima), 3),
        kelly_fraction=round(rng.uniform(*cfg.range_kelly_fraction), 4),
        clv_minimo_pct=round(rng.uniform(*cfg.range_clv_minimo_pct), 3),
        mercato=rng.choice(cfg.mercati_possibili),
    )


# ==================================================================
# Ponte Genoma -> Model A (fenotipo)
# ==================================================================
def a_config_da_genoma(genoma: Genoma):
    """
    Costruisce un ModelAConfig equivalente a questo genoma, riusando i
    default di Model A per tutto cio' che il genetico non esplora
    (decadimento_quota_lambda, leghe_alta_liquidita_keywords, clv_fallback_policy).
    Import locale di ModelAConfig per evitare un ciclo di import a livello
    modulo (config.py non dipende da models/, ma qui serve costruirne
    un'istanza modificata).
    """
    from config import ModelAConfig
    return ModelAConfig(
        ev_minimo_pct=genoma.ev_minimo_pct,
        quota_massima_assoluta=genoma.quota_massima,
        kelly_fraction=genoma.kelly_fraction,
        clv_stimato_minimo_pct=genoma.clv_minimo_pct,
        mercati_consentiti=(genoma.mercato,),
        # leghe_alta_liquidita_keywords lasciato al default di ModelAConfig
        # (tuple vuota = nessun filtro lega): il genetico esplora soglie
        # numeriche, non la lista di leghe, che resta una decisione umana.
    )


# ==================================================================
# Dataset di backtest
# ==================================================================
@dataclass
class EventoStorico:
    """
    Un evento storico con esito noto, sufficiente per rigiocare
    Model A/il genoma su dati passati e sapere se avrebbe vinto/perso.
    Combina EventoInput (per far girare ModelAConservative.valuta) con
    l'esito reale e la data, che servono al backtest ma non
    all'inferenza live di Model A.
    """
    evento_input: EventoInput
    data_evento: date
    esito_selection_vincente: bool  # True se evento_input.selection e' risultata la selection vincente


def backtest_genoma_su_eventi(
    genoma: Genoma,
    eventi_storici: List[EventoStorico],
    bankroll_iniziale: float = 1000.0,
) -> MetrichePerformance:
    """
    Rigioca un genoma (via il suo ModelAConfig equivalente) su una lista
    di eventi storici GIA' ORDINATI TEMPORALMENTE dal chiamante, e
    ritorna le metriche di performance risultanti (riusa
    utils.stats_engine.calcola_metriche(), stesso principio di single
    source of truth gia' visto altrove nel progetto).

    Per ogni evento: se il genoma (Model A con quei parametri) avrebbe
    accettato la bet, si simula lo stake Kelly su quell'evento e si
    registra vinta/persa in base a esito_selection_vincente.
    """
    model_a_equivalente = ModelAConservative(a_config_da_genoma(genoma))
    bet_simulate: List[BetSettled] = []

    for evt_storico in eventi_storici:
        valutazione = model_a_equivalente.valuta(evt_storico.evento_input)
        if not valutazione.accepted:
            continue

        stake = round(bankroll_iniziale * valutazione.kelly_stake_frazione, 2)
        if stake <= 0:
            continue

        if evt_storico.esito_selection_vincente:
            profit_loss = round(stake * (evt_storico.evento_input.bookmaker_odds - 1), 2)
            result = "vinta"
        else:
            profit_loss = -stake
            result = "persa"

        bet_simulate.append(BetSettled(
            data_settlement=evt_storico.data_evento, stake=stake, profit_loss=profit_loss,
            result=result, ev_teorico_pct=valutazione.ev_pct, clv_pct=evt_storico.evento_input.clv_stimato_pct,
            kelly_fraction_usata=valutazione.kelly_fraction_usata,
        ))

    return calcola_metriche(bet_simulate, bankroll_iniziale=bankroll_iniziale)


# ==================================================================
# Walk-forward validation
# ==================================================================
def dividi_walk_forward(
    eventi_storici: List[EventoStorico],
    num_fold: int,
    test_size_pct: float,
) -> List[Tuple[List[EventoStorico], List[EventoStorico]]]:
    """
    Divide gli eventi (gia' ordinati temporalmente) in num_fold segmenti
    CONSECUTIVI e non sovrapposti, ciascuno diviso a sua volta in
    training (visibile) + test out-of-sample (le ultime test_size_pct%
    osservazioni di quel segmento). Ritorna una lista di tuple
    (training, test) - la fitness verra' calcolata SOLO sui test set
    (vedi calcola_fitness_walk_forward sotto).

    Esempio con 100 eventi, num_fold=5: 5 segmenti da 20 eventi
    ciascuno, di cui l'ultimo 20% (4 eventi) e' test in ciascun segmento.
    Questo e' walk-forward "a blocchi" (non espansivo): ogni fold vede
    SOLO i propri dati, non un training set cumulativo crescente -
    scelta deliberata per tenere il codice semplice e i fold
    statisticamente comparabili tra loro (stesso volume di training in
    ciascuno), al costo di non sfruttare tutto lo storico disponibile
    nei fold piu' recenti. Una variante espansiva e' possibile in
    futuro se servisse maggiore realismo, ma aggiunge complessita' che
    oggi non e' giustificata dal volume di dati disponibile.
    """
    if num_fold < 1:
        raise ValueError("num_fold deve essere >= 1")
    if not (0 < test_size_pct < 1):
        raise ValueError("test_size_pct deve essere in (0,1)")

    n_totale = len(eventi_storici)
    dimensione_fold = n_totale // num_fold
    fold_risultanti = []

    for i in range(num_fold):
        inizio = i * dimensione_fold
        fine = (i + 1) * dimensione_fold if i < num_fold - 1 else n_totale
        segmento = eventi_storici[inizio:fine]
        if len(segmento) < 2:
            continue  # segmento troppo piccolo per uno split train/test sensato, viene saltato

        dimensione_test = max(1, round(len(segmento) * test_size_pct))
        training = segmento[:-dimensione_test]
        test = segmento[-dimensione_test:]
        if training and test:
            fold_risultanti.append((training, test))

    return fold_risultanti


@dataclass
class RisultatoBacktest:
    """Esito completo del backtest walk-forward per un genoma, pronto per shadow_automl_configs."""
    genoma: Genoma
    fitness_score: float
    roi_medio_pct: float
    sharpe_medio: float
    max_drawdown_medio_pct: float
    numero_bet_totale_test: int
    numero_fold_validi: int
    scartato_dati_insufficienti: bool = False
    motivo_scarto: Optional[str] = None


def calcola_fitness(metriche: MetrichePerformance, cfg: ModelEConfig) -> float:
    """
    Fitness composita (vedi progettazione + config.py per i pesi
    default 0.45/0.35/0.20 su ROI/Sharpe/Drawdown). Il drawdown entra
    con segno invertito (piu' basso e' meglio, ma la fitness deve
    crescere quando le cose migliorano) e viene troncato a un massimo
    "punibile" di 50 punti percentuali per evitare che un singolo
    drawdown estremo (es. 95%) domini numericamente la formula rispetto
    a ROI/Sharpe che vivono su scale diverse.

    Sharpe None (varianza zero o troppo pochi dati nel fold) viene
    trattato come 0.0 ai fini della fitness: ne' un bonus ne' una
    penalita', semplice assenza di segnale in quel fold.
    """
    roi_component = metriche.roi_pct
    sharpe_component = metriche.sharpe_ratio if metriche.sharpe_ratio is not None else 0.0
    drawdown_troncato = min(metriche.max_drawdown_pct, 50.0)
    drawdown_component = -drawdown_troncato

    return round(
        cfg.fitness_peso_roi * roi_component
        + cfg.fitness_peso_sharpe * sharpe_component
        + cfg.fitness_peso_drawdown * drawdown_component,
        4,
    )


def valuta_genoma_walk_forward(
    genoma: Genoma,
    eventi_storici: List[EventoStorico],
    cfg: ModelEConfig,
) -> RisultatoBacktest:
    """
    Il cuore anti-overfitting: valuta un genoma SOLO sui segmenti di
    test out-of-sample di ciascun fold walk-forward, mai sul training.
    La fitness finale e' la media delle fitness per-fold.
    """
    if len(eventi_storici) < cfg.minimo_bet_per_backtest:
        return RisultatoBacktest(
            genoma=genoma, fitness_score=float("-inf"), roi_medio_pct=0.0, sharpe_medio=0.0,
            max_drawdown_medio_pct=0.0, numero_bet_totale_test=0, numero_fold_validi=0,
            scartato_dati_insufficienti=True,
            motivo_scarto=(
                f"storico disponibile ({len(eventi_storici)} eventi) sotto il minimo "
                f"richiesto per backtest ({cfg.minimo_bet_per_backtest})"
            ),
        )

    fold = dividi_walk_forward(eventi_storici, cfg.walk_forward_num_fold, cfg.walk_forward_test_size_pct)
    if not fold:
        return RisultatoBacktest(
            genoma=genoma, fitness_score=float("-inf"), roi_medio_pct=0.0, sharpe_medio=0.0,
            max_drawdown_medio_pct=0.0, numero_bet_totale_test=0, numero_fold_validi=0,
            scartato_dati_insufficienti=True,
            motivo_scarto="nessun fold walk-forward valido generato (segmenti troppo piccoli)",
        )

    fitness_per_fold = []
    roi_per_fold = []
    sharpe_per_fold = []
    drawdown_per_fold = []
    numero_bet_totale_test = 0

    for training, test in fold:
        # Il training set qui NON viene usato per adattare il genoma
        # (Model A e' un filtro a soglie fisse, non un modello che si
        # allena come Model C) - e' incluso nella tupla per coerenza
        # concettuale con un vero walk-forward e per eventuali estensioni
        # future (es. un genetico che ottimizzasse anche parametri
        # allenabili), ma nel caso concreto di Model E su Model A la
        # valutazione avviene interamente sul segmento di test.
        metriche_test = backtest_genoma_su_eventi(genoma, test)
        if metriche_test.numero_bet == 0:
            continue  # questo genoma non ha generato nessuna bet in questo fold di test: non contribuisce alla fitness (ne' bene ne' male)

        fitness_fold = calcola_fitness(metriche_test, cfg)
        fitness_per_fold.append(fitness_fold)
        roi_per_fold.append(metriche_test.roi_pct)
        sharpe_per_fold.append(metriche_test.sharpe_ratio if metriche_test.sharpe_ratio is not None else 0.0)
        drawdown_per_fold.append(metriche_test.max_drawdown_pct)
        numero_bet_totale_test += metriche_test.numero_bet

    if not fitness_per_fold:
        return RisultatoBacktest(
            genoma=genoma, fitness_score=float("-inf"), roi_medio_pct=0.0, sharpe_medio=0.0,
            max_drawdown_medio_pct=0.0, numero_bet_totale_test=0, numero_fold_validi=0,
            scartato_dati_insufficienti=True,
            motivo_scarto="il genoma non ha generato nessuna bet in nessun fold di test (soglie troppo restrittive per questo dataset)",
        )

    return RisultatoBacktest(
        genoma=genoma,
        fitness_score=round(sum(fitness_per_fold) / len(fitness_per_fold), 4),
        roi_medio_pct=round(sum(roi_per_fold) / len(roi_per_fold), 3),
        sharpe_medio=round(sum(sharpe_per_fold) / len(sharpe_per_fold), 4),
        max_drawdown_medio_pct=round(sum(drawdown_per_fold) / len(drawdown_per_fold), 3),
        numero_bet_totale_test=numero_bet_totale_test,
        numero_fold_validi=len(fitness_per_fold),
    )


# ==================================================================
# Operatori genetici
# ==================================================================
def crossover(genoma_a: Genoma, genoma_b: Genoma, rng: random.Random) -> Genoma:
    """
    Crossover uniforme: ciascun gene del figlio viene ereditato a caso
    da uno dei due genitori (50/50 per gene, indipendentemente dagli
    altri geni) - piu' semplice ed efficace del single-point crossover
    per un genoma corto come questo (5 geni), dove single-point
    introdurrebbe un bias posizionale arbitrario senza benefici reali.
    """
    return Genoma(
        ev_minimo_pct=rng.choice([genoma_a.ev_minimo_pct, genoma_b.ev_minimo_pct]),
        quota_massima=rng.choice([genoma_a.quota_massima, genoma_b.quota_massima]),
        kelly_fraction=rng.choice([genoma_a.kelly_fraction, genoma_b.kelly_fraction]),
        clv_minimo_pct=rng.choice([genoma_a.clv_minimo_pct, genoma_b.clv_minimo_pct]),
        mercato=rng.choice([genoma_a.mercato, genoma_b.mercato]),
    )


def muta(genoma: Genoma, cfg: ModelEConfig, rng: random.Random) -> Genoma:
    """
    Mutazione indipendente per gene con probabilita' cfg.tasso_mutazione:
    ciascun gene, SE mutato, viene ri-campionato uniformemente nel suo
    range (non una perturbazione incrementale attorno al valore attuale)
    - piu' semplice e sufficiente per uno spazio di ricerca a 5 dimensioni
    di questa scala, evita di dover progettare una deviazione standard di
    mutazione per ciascun gene con range molto diversi tra loro (es.
    kelly_fraction in [0.05,0.5] vs quota_massima in [1.5,8.0]).
    """
    nuovo = Genoma(**genoma.to_dict())
    if rng.random() < cfg.tasso_mutazione:
        nuovo.ev_minimo_pct = round(rng.uniform(*cfg.range_ev_minimo_pct), 3)
    if rng.random() < cfg.tasso_mutazione:
        nuovo.quota_massima = round(rng.uniform(*cfg.range_quota_massima), 3)
    if rng.random() < cfg.tasso_mutazione:
        nuovo.kelly_fraction = round(rng.uniform(*cfg.range_kelly_fraction), 4)
    if rng.random() < cfg.tasso_mutazione:
        nuovo.clv_minimo_pct = round(rng.uniform(*cfg.range_clv_minimo_pct), 3)
    if rng.random() < cfg.tasso_mutazione:
        nuovo.mercato = rng.choice(cfg.mercati_possibili)
    return nuovo


def selezione_torneo(popolazione_valutata: List[RisultatoBacktest], rng: random.Random, k: int = 3) -> Genoma:
    """
    Selezione a torneo: pesca k individui a caso dalla popolazione
    valutata e ritorna il migliore (fitness piu' alta) tra quelli
    pescati. Preferita alla roulette-wheel classica perche' non richiede
    che la fitness sia sempre positiva (qui puo' essere negativa, es. un
    ROI negativo) - la roulette-wheel con fitness negative richiederebbe
    un offset arbitrario che il torneo evita del tutto.
    """
    candidati = rng.sample(popolazione_valutata, min(k, len(popolazione_valutata)))
    return max(candidati, key=lambda r: r.fitness_score).genoma


# ==================================================================
# Ciclo evolutivo completo
# ==================================================================
@dataclass
class RisultatoCicloEvolutivo:
    top_champion: List[RisultatoBacktest]  # ordinati per fitness decrescente, lunghezza = cfg.numero_champion_mantenuti
    tutte_le_configurazioni_valutate: List[RisultatoBacktest]  # per shadow_automl_configs, incluse quelle NON champion (vedi schema.sql)
    generazioni_eseguite: int
    scartato_dati_insufficienti: bool = False
    motivo_scarto: Optional[str] = None


def esegui_ciclo_evolutivo(
    eventi_storici: List[EventoStorico],
    cfg: ModelEConfig,
    seed: Optional[int] = None,
) -> RisultatoCicloEvolutivo:
    """
    Ciclo genetico completo: genera popolazione iniziale casuale, poi per
    numero_generazioni_per_ciclo generazioni applica elitismo + selezione
    a torneo + crossover + mutazione, valutando ogni individuo con
    valuta_genoma_walk_forward() (quindi SEMPRE su dati out-of-sample,
    mai training puro - vedi discussione anti-overfitting in testa al
    modulo).

    `seed` e' opzionale e serve SOLO per riproducibilita' nei test (vedi
    self-test in fondo) - in produzione va lasciato None per una vera
    esplorazione casuale ad ogni ciclo.
    """
    rng = random.Random(seed)

    if len(eventi_storici) < cfg.minimo_bet_per_backtest:
        return RisultatoCicloEvolutivo(
            top_champion=[], tutte_le_configurazioni_valutate=[], generazioni_eseguite=0,
            scartato_dati_insufficienti=True,
            motivo_scarto=(
                f"storico disponibile ({len(eventi_storici)} eventi) sotto il minimo "
                f"richiesto per backtest ({cfg.minimo_bet_per_backtest}): nessun ciclo evolutivo eseguito"
            ),
        )

    popolazione = [genoma_random(cfg, rng) for _ in range(cfg.dimensione_popolazione)]
    tutte_le_valutazioni: List[RisultatoBacktest] = []

    for generazione in range(cfg.numero_generazioni_per_ciclo):
        valutazioni = [valuta_genoma_walk_forward(g, eventi_storici, cfg) for g in popolazione]
        tutte_le_valutazioni.extend(valutazioni)

        # Solo individui con fitness finita (non scartati per dati
        # insufficienti/nessuna bet generata) partecipano a selezione ed
        # elitismo - un individuo con fitness -inf non deve mai essere
        # scelto ne' come genitore ne' come elite, altrimenti il genetico
        # convergerebbe su configurazioni che semplicemente non fanno nulla.
        valutazioni_valide = [v for v in valutazioni if v.fitness_score != float("-inf")]

        if not valutazioni_valide:
            # Intera generazione senza individui validi: rigenera popolazione
            # casuale da zero per la prossima generazione invece di bloccarsi.
            popolazione = [genoma_random(cfg, rng) for _ in range(cfg.dimensione_popolazione)]
            continue

        valutazioni_valide.sort(key=lambda r: r.fitness_score, reverse=True)
        elite = [v.genoma for v in valutazioni_valide[:cfg.elitismo_top_n]]

        nuova_popolazione = list(elite)
        while len(nuova_popolazione) < cfg.dimensione_popolazione:
            genitore_a = selezione_torneo(valutazioni_valide, rng)
            if rng.random() < cfg.tasso_crossover and len(valutazioni_valide) > 1:
                genitore_b = selezione_torneo(valutazioni_valide, rng)
                figlio = crossover(genitore_a, genitore_b, rng)
            else:
                figlio = genitore_a
            figlio = muta(figlio, cfg, rng)
            nuova_popolazione.append(figlio)

        popolazione = nuova_popolazione

    valutazioni_finali_valide = [v for v in tutte_le_valutazioni if v.fitness_score != float("-inf")]
    valutazioni_finali_valide.sort(key=lambda r: r.fitness_score, reverse=True)

    # Deduplicazione dei top champion per genoma (lo stesso genoma puo'
    # essere stato valutato piu' volte tra generazioni diverse, specie
    # per via dell'elitismo): si tiene solo la valutazione con fitness
    # piu' alta per ciascun genoma distinto, cosi' i 3 champion non
    # rischiano di essere in realta' varianti quasi identiche dello
    # stesso individuo elitario.
    visti = set()
    top_champion = []
    for v in valutazioni_finali_valide:
        chiave = v.genoma.a_tuple_ordinata()
        if chiave in visti:
            continue
        visti.add(chiave)
        top_champion.append(v)
        if len(top_champion) >= cfg.numero_champion_mantenuti:
            break

    return RisultatoCicloEvolutivo(
        top_champion=top_champion,
        tutte_le_configurazioni_valutate=tutte_le_valutazioni,
        generazioni_eseguite=cfg.numero_generazioni_per_ciclo,
    )


# ==================================================================
# Self-test manuale rapido (python3 models/model_e_automl.py)
# ==================================================================
if __name__ == "__main__":
    import time
    from datetime import timedelta
    from config import DEFAULT_CONFIG

    cfg = DEFAULT_CONFIG.model_e

    print("=" * 70)
    print("TEST 1: genoma_random() rispetta i range configurati")
    print("=" * 70)
    rng_test = random.Random(123)
    for _ in range(50):
        g = genoma_random(cfg, rng_test)
        assert cfg.range_ev_minimo_pct[0] <= g.ev_minimo_pct <= cfg.range_ev_minimo_pct[1]
        assert cfg.range_quota_massima[0] <= g.quota_massima <= cfg.range_quota_massima[1]
        assert cfg.range_kelly_fraction[0] <= g.kelly_fraction <= cfg.range_kelly_fraction[1]
        assert cfg.range_clv_minimo_pct[0] <= g.clv_minimo_pct <= cfg.range_clv_minimo_pct[1]
        assert g.mercato in cfg.mercati_possibili
    print("50 genomi campionati, tutti i geni entro i range configurati: OK")

    print()
    print("=" * 70)
    print("TEST 2: dividi_walk_forward() - copertura e non sovrapposizione dei fold")
    print("=" * 70)
    oggi = date(2026, 7, 15)
    eventi_fittizi_semplici = list(range(100))  # placeholder, testiamo solo la logica di slicing
    fold_test = dividi_walk_forward(eventi_fittizi_semplici, num_fold=5, test_size_pct=0.20)
    print(f"Fold generati: {len(fold_test)} (attesi 5 su 100 eventi)")
    totale_coperto = sum(len(tr) + len(te) for tr, te in fold_test)
    print(f"Eventi totali coperti dai fold: {totale_coperto} (atteso 100, nessuna perdita/duplicazione)")
    assert totale_coperto == 100
    for i, (tr, te) in enumerate(fold_test):
        print(f"  fold {i}: training={len(tr)} test={len(te)}")
        assert len(te) >= 1

    print()
    print("=" * 70)
    print("TEST 3: backtest_genoma_su_eventi() su un dataset sintetico noto")
    print("=" * 70)
    # Costruzione dataset: eventi con EV alto (8%) e bassa quota (2.0),
    # esito vincente forzato al 60% - un genoma permissivo (soglia EV
    # bassa) deve produrre un ROI positivo su questo dataset per costruzione.
    rng_data = random.Random(7)
    eventi_storici_test = []
    for i in range(300):
        ev_target = 8.0
        quota = 2.0
        prob = ((ev_target / 100) + 1) / quota * 100  # inverte calcola_ev per ottenere la prob che produce esattamente ev_target
        evt_input = EventoInput(
            event_id=i, campionato="Italia - Serie A", mercato="1X2", selection="1",
            probability_pct=prob, bookmaker_odds=quota, fair_odds=100 / prob, clv_stimato_pct=1.0,
        )
        vince = rng_data.random() < 0.60
        eventi_storici_test.append(EventoStorico(
            evento_input=evt_input, data_evento=oggi - timedelta(days=300 - i), esito_selection_vincente=vince,
        ))

    genoma_permissivo = Genoma(ev_minimo_pct=2.0, quota_massima=5.0, kelly_fraction=0.10, clv_minimo_pct=0.0, mercato="1X2")
    metriche_backtest = backtest_genoma_su_eventi(genoma_permissivo, eventi_storici_test)
    print(f"Bet generate: {metriche_backtest.numero_bet} (atteso: vicino a 300, il genoma e' permissivo)")
    print(f"ROI: {metriche_backtest.roi_pct}%  Win Rate: {metriche_backtest.win_rate_pct}%")
    assert metriche_backtest.numero_bet > 250, "un genoma permissivo su 300 eventi tutti validi deve accettarne la maggior parte"
    assert metriche_backtest.roi_pct > 0, "con EV reale 8%% e prob di vincita reale 60%%, il ROI atteso e' positivo per costruzione"

    genoma_restrittivo = Genoma(ev_minimo_pct=7.99, quota_massima=1.9, kelly_fraction=0.05, clv_minimo_pct=0.0, mercato="1X2")
    metriche_restrittivo = backtest_genoma_su_eventi(genoma_restrittivo, eventi_storici_test)
    print(f"Genoma restrittivo (quota_massima 1.9 < quota reale 2.0): bet generate={metriche_restrittivo.numero_bet} (atteso: 0)")
    assert metriche_restrittivo.numero_bet == 0, "un genoma con quota_massima sotto la quota reale non deve accettare nulla"

    print()
    print("=" * 70)
    print("TEST 4: calcola_fitness() - ranking coerente con l'intuizione")
    print("=" * 70)
    m_buona = calcola_metriche([
        BetSettled(oggi, 50, 40, "vinta"), BetSettled(oggi, 50, 35, "vinta"), BetSettled(oggi, 50, -20, "persa"),
    ])
    m_cattiva = calcola_metriche([
        BetSettled(oggi, 50, -40, "persa"), BetSettled(oggi, 50, -35, "persa"), BetSettled(oggi, 50, 10, "vinta"),
    ])
    fit_buona = calcola_fitness(m_buona, cfg)
    fit_cattiva = calcola_fitness(m_cattiva, cfg)
    print(f"Fitness scenario buono: {fit_buona}  |  Fitness scenario cattivo: {fit_cattiva}")
    assert fit_buona > fit_cattiva, "uno scenario con ROI/Sharpe migliori deve avere fitness piu' alta"

    print()
    print("=" * 70)
    print("TEST 5: valuta_genoma_walk_forward() - scarto per dati insufficienti")
    print("=" * 70)
    pochi_eventi = eventi_storici_test[:50]  # sotto minimo_bet_per_backtest (default 200)
    risultato_pochi = valuta_genoma_walk_forward(genoma_permissivo, pochi_eventi, cfg)
    print(f"scartato_dati_insufficienti={risultato_pochi.scartato_dati_insufficienti}  motivo={risultato_pochi.motivo_scarto}")
    assert risultato_pochi.scartato_dati_insufficienti

    print()
    print("=" * 70)
    print("TEST 6: valuta_genoma_walk_forward() - genoma valido su dataset sufficiente")
    print("=" * 70)
    risultato_valido = valuta_genoma_walk_forward(genoma_permissivo, eventi_storici_test, cfg)
    print(f"fitness={risultato_valido.fitness_score}  fold_validi={risultato_valido.numero_fold_validi}  "
          f"ROI medio={risultato_valido.roi_medio_pct}%  bet totali nei test set={risultato_valido.numero_bet_totale_test}")
    assert not risultato_valido.scartato_dati_insufficienti
    assert risultato_valido.numero_fold_validi > 0

    print()
    print("=" * 70)
    print("TEST 7: crossover() e muta() producono sempre geni entro i range")
    print("=" * 70)
    rng_ops = random.Random(99)
    g1 = genoma_random(cfg, rng_ops)
    g2 = genoma_random(cfg, rng_ops)
    for _ in range(100):
        figlio = crossover(g1, g2, rng_ops)
        figlio_mutato = muta(figlio, cfg, rng_ops)
        assert cfg.range_ev_minimo_pct[0] <= figlio_mutato.ev_minimo_pct <= cfg.range_ev_minimo_pct[1]
        assert cfg.range_kelly_fraction[0] <= figlio_mutato.kelly_fraction <= cfg.range_kelly_fraction[1]
    print("100 cicli crossover+mutazione, tutti i figli entro i range: OK")

    print()
    print("=" * 70)
    print("TEST 8: ciclo evolutivo completo end-to-end (popolazione/generazioni ridotte per velocita' test)")
    print("=" * 70)
    from dataclasses import replace
    cfg_test_veloce = replace(cfg, dimensione_popolazione=10, numero_generazioni_per_ciclo=5, minimo_bet_per_backtest=200)

    t0 = time.time()
    risultato_ciclo = esegui_ciclo_evolutivo(eventi_storici_test, cfg_test_veloce, seed=42)
    durata = time.time() - t0
    print(f"Ciclo completato in {durata:.2f}s")
    print(f"Generazioni eseguite: {risultato_ciclo.generazioni_eseguite}")
    print(f"Configurazioni totali valutate: {len(risultato_ciclo.tutte_le_configurazioni_valutate)} "
          f"(atteso: popolazione {cfg_test_veloce.dimensione_popolazione} x generazioni {cfg_test_veloce.numero_generazioni_per_ciclo} = "
          f"{cfg_test_veloce.dimensione_popolazione * cfg_test_veloce.numero_generazioni_per_ciclo})")
    print(f"Champion trovati: {len(risultato_ciclo.top_champion)} (atteso: fino a {cfg_test_veloce.numero_champion_mantenuti})")
    for i, champ in enumerate(risultato_ciclo.top_champion, 1):
        print(f"  #{i} fitness={champ.fitness_score}  ROI medio={champ.roi_medio_pct}%  genoma={champ.genoma.to_dict()}")

    assert not risultato_ciclo.scartato_dati_insufficienti
    assert len(risultato_ciclo.tutte_le_configurazioni_valutate) == cfg_test_veloce.dimensione_popolazione * cfg_test_veloce.numero_generazioni_per_ciclo
    assert len(risultato_ciclo.top_champion) >= 1, "su un dataset con edge positivo per costruzione, il genetico deve trovare almeno un champion valido"
    # I champion devono essere ordinati per fitness decrescente
    fitness_champion = [c.fitness_score for c in risultato_ciclo.top_champion]
    assert fitness_champion == sorted(fitness_champion, reverse=True), "i champion devono essere ordinati per fitness decrescente"

    print()
    print("=" * 70)
    print("TEST 9: riproducibilita' - stesso seed produce lo stesso risultato")
    print("=" * 70)
    risultato_ciclo_2 = esegui_ciclo_evolutivo(eventi_storici_test, cfg_test_veloce, seed=42)
    champion_1 = [c.genoma.to_dict() for c in risultato_ciclo.top_champion]
    champion_2 = [c.genoma.to_dict() for c in risultato_ciclo_2.top_champion]
    print(f"Champion run 1: {champion_1}")
    print(f"Champion run 2: {champion_2}")
    assert champion_1 == champion_2, "con lo stesso seed, il ciclo evolutivo deve essere deterministico"
    print("Riproducibilita' confermata: stesso seed -> stesso risultato esatto.")

    print()
    print("Tutti i test completati.")
