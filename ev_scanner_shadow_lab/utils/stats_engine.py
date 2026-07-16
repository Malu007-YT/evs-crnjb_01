"""
EV Scanner AI - Shadow Intelligence System
Step 4/7: utils/stats_engine.py - Motore statistico e confronto A/B
----------------------------------------------------------------
A differenza di Model A/B (step 3), qui non c'e' nulla da "portare" da
functions.php: ROI/Yield/Win Rate hanno un equivalente informale sparso
nella dashboard PHP di produzione, ma Sharpe Ratio, Max Drawdown e
Profit Factor sono concetti quantitativi nuovi, introdotti solo dallo
Shadow Lab (vedi progettazione, sezione "AI Dashboard & Performance
Analytics"). Le formule seguono le definizioni standard usate in
letteratura quant/trading sistematico, non un'invenzione ad-hoc.

Questo modulo e' PURO: prende in input liste di bet gia' concluse
(settled) come semplici dict/dataclass, non fa mai query SQL da solo e
non importa nulla da models/ o config.py. Questo lo rende testabile in
isolamento con dati sintetici (vedi self-test in fondo) e riusabile sia
da main_shadow_engine.py (via il layer DB, step futuro) sia da un
notebook di analisi ad-hoc, senza portarsi dietro dipendenze pesanti.

Convenzione dati di input: una "BetSettled" e' un qualunque oggetto con
gli attributi elencati in BetSettled sotto. Funziona sia con istanze di
quella dataclass sia con righe DB gia' mappate (vedi
_as_bet_settled_list per la normalizzazione).
----------------------------------------------------------------
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional, Sequence, Union


# ==================================================================
# Struttura dati di input
# ==================================================================
@dataclass
class BetSettled:
    """
    Vista minima di una bet (reale o shadow) gia' conclusa, sufficiente
    per calcolare tutte le metriche di questo modulo. Il chiamante (layer
    DB, step futuro) la costruisce da una riga di `shadow_bets` (o
    `scommesse` per le metriche di produzione) filtrando result IN
    ('vinta','persa','void').
    """
    data_settlement: date  # per segmentazione temporale, vedi segmenta_per_periodo()
    stake: float
    profit_loss: float  # positivo se vinta, negativo se persa, 0.0 se void
    result: str  # 'vinta' | 'persa' | 'void'
    ev_teorico_pct: Optional[float] = None  # EV%% calcolato al momento della bet, per EV medio teorico vs reale
    clv_pct: Optional[float] = None  # CLV%% reale, se disponibile (None se non catturato)
    kelly_fraction_usata: Optional[float] = None
    campionato: Optional[str] = None  # per heatmap profitto per campionato/mercato (vedi funzione dedicata sotto)
    mercato: Optional[str] = None


def _normalizza_bets(bets: Iterable) -> list:
    """
    Accetta sia una lista di BetSettled sia una lista di dict/righe DB
    con le stesse chiavi (es. risultato di un fetchall PDO-style), e
    restituisce sempre una lista di BetSettled. Questo evita che ogni
    chiamante debba costruire esplicitamente l'oggetto se ha gia' un
    dict a portata di mano (tipico quando i dati arrivano da una query).
    """
    normalizzate = []
    for b in bets:
        if isinstance(b, BetSettled):
            normalizzate.append(b)
        elif isinstance(b, dict):
            normalizzate.append(BetSettled(
                data_settlement=b["data_settlement"],
                stake=float(b["stake"]),
                profit_loss=float(b["profit_loss"]),
                result=b["result"],
                ev_teorico_pct=b.get("ev_teorico_pct"),
                clv_pct=b.get("clv_pct"),
                kelly_fraction_usata=b.get("kelly_fraction_usata"),
                campionato=b.get("campionato"),
                mercato=b.get("mercato"),
            ))
        else:
            raise TypeError(f"Tipo bet non supportato: {type(b)}. Attesi BetSettled o dict.")
    return normalizzate


# ==================================================================
# Struttura dati di output
# ==================================================================
@dataclass
class MetrichePerformance:
    """
    Blocco completo di metriche per un set di bet in un dato periodo.
    Corrisponde 1:1 a quanto richiesto nella progettazione (sezione "AI
    Dashboard & Performance Analytics"): Metriche di Performance +
    Metriche di Rischio + Metriche di Modello.
    """
    numero_bet: int = 0
    numero_bet_vinte: int = 0
    numero_bet_perse: int = 0
    numero_bet_void: int = 0

    # --- Performance ---
    stake_totale: float = 0.0
    profitto_cumulativo: float = 0.0
    roi_pct: float = 0.0       # profitto_cumulativo / stake_totale * 100
    yield_pct: float = 0.0     # alias concettuale di ROI su stake medio - vedi nota in calcola_metriche()
    win_rate_pct: float = 0.0  # % vinte su (vinte+perse), void escluse dal denominatore

    # --- Rischio ---
    max_drawdown_pct: float = 0.0
    max_drawdown_durata_bet: int = 0  # numero di bet consecutive dal picco al punto piu' basso del drawdown
    profit_factor: Optional[float] = None  # None se non ci sono perdite (profit factor infinito, non rappresentabile)
    sharpe_ratio: Optional[float] = None   # None se varianza dei rendimenti e' zero o troppo pochi dati
    varianza_rendimenti: float = 0.0

    # --- Modello ---
    clv_medio_pct: Optional[float] = None
    ev_medio_teorico_pct: Optional[float] = None
    ev_medio_reale_pct: Optional[float] = None  # ROI% delle sole bet con EV teorico noto, per confronto diretto teorico-vs-reale
    kelly_medio_applicato: Optional[float] = None

    # --- Serie temporali per grafici ---
    curva_bankroll_cumulativo: list = field(default_factory=list)  # lista di (data, profitto_cumulativo_progressivo)
    drawdown_series: list = field(default_factory=list)  # lista di (data, drawdown_pct_in_quel_momento), per l'istogramma richiesto in progettazione


# ==================================================================
# Metriche core
# ==================================================================
def calcola_metriche(bets: Iterable, bankroll_iniziale: float = 1000.0) -> MetrichePerformance:
    """
    Calcola l'intero blocco di metriche su un set di bet gia' concluse.
    Le bet 'void' contano nel conteggio totale e nella curva bankroll
    (profit_loss=0.0, stake restituito) ma sono escluse dal denominatore
    del win rate (non e' ne' una vittoria ne' una sconfitta).

    ROI vs Yield: nella progettazione sono richiesti entrambi come
    metriche distinte. Qui si adotta la distinzione standard del betting
    quantitativo:
      - ROI%% = profitto_cumulativo / stake_totale_realmente_puntato * 100
                (rendimento sul capitale effettivamente rischiato)
      - Yield%% = stessa formula del ROI in questo contesto (nel value
                betting i due termini sono spesso usati come sinonimi,
                a differenza dell'investing tradizionale dove yield ha
                un significato diverso legato ai dividendi). Il campo
                resta distinto nello schema/output per aderenza 1:1 alla
                progettazione originale e per lasciare spazio a una
                futura divergenza (es. Yield calcolato solo sulle bet
                "Smart Filter" come nel Bankroll Verde di produzione),
                ma ad oggi i due valori coincidono numericamente.
    """
    bets_list = sorted(_normalizza_bets(bets), key=lambda b: b.data_settlement)

    m = MetrichePerformance()
    m.numero_bet = len(bets_list)
    if m.numero_bet == 0:
        return m

    m.numero_bet_vinte = sum(1 for b in bets_list if b.result == "vinta")
    m.numero_bet_perse = sum(1 for b in bets_list if b.result == "persa")
    m.numero_bet_void = sum(1 for b in bets_list if b.result == "void")

    m.stake_totale = round(sum(b.stake for b in bets_list), 2)
    m.profitto_cumulativo = round(sum(b.profit_loss for b in bets_list), 2)

    if m.stake_totale > 0:
        m.roi_pct = round((m.profitto_cumulativo / m.stake_totale) * 100, 3)
        m.yield_pct = m.roi_pct  # vedi nota nella docstring sopra

    decise = m.numero_bet_vinte + m.numero_bet_perse
    if decise > 0:
        m.win_rate_pct = round((m.numero_bet_vinte / decise) * 100, 2)

    # --- Profit Factor: somma vincite / somma perdite in valore assoluto ---
    somma_vincite = sum(b.profit_loss for b in bets_list if b.profit_loss > 0)
    somma_perdite_abs = abs(sum(b.profit_loss for b in bets_list if b.profit_loss < 0))
    if somma_perdite_abs > 0:
        m.profit_factor = round(somma_vincite / somma_perdite_abs, 3)
    # se somma_perdite_abs == 0 (nessuna bet persa), profit_factor resta
    # None: matematicamente sarebbe infinito, non rappresentabile in modo
    # utile, e va gestito esplicitamente dal chiamante (dashboard mostra
    # "∞" o "N/D" invece di un numero fuorviante)

    # --- Curva bankroll cumulativo + drawdown series ---
    cumulato = 0.0
    picco = 0.0
    picco_index = 0
    max_dd_pct = 0.0
    max_dd_durata = 0
    curva = []
    drawdown_series = []

    for idx, b in enumerate(bets_list):
        cumulato += b.profit_loss
        curva.append((b.data_settlement, round(cumulato, 2)))

        bankroll_corrente = bankroll_iniziale + cumulato
        bankroll_al_picco = bankroll_iniziale + picco

        if cumulato > picco:
            picco = cumulato
            picco_index = idx

        # Drawdown%% calcolato sul BANKROLL (iniziale + cumulato), non sul
        # solo profitto: e' la definizione standard (quanto sei sceso
        # rispetto al capitale disponibile al picco), coerente con come
        # un utente leggerebbe "sto in drawdown del 12%%" nella dashboard.
        if bankroll_al_picco > 0:
            dd_pct = round(((bankroll_al_picco - bankroll_corrente) / bankroll_al_picco) * 100, 3)
        else:
            dd_pct = 0.0
        dd_pct = max(0.0, dd_pct)  # sopra il picco il drawdown e' 0, non negativo

        drawdown_series.append((b.data_settlement, dd_pct))

        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_durata = idx - picco_index

    m.curva_bankroll_cumulativo = curva
    m.drawdown_series = drawdown_series
    m.max_drawdown_pct = max_dd_pct
    m.max_drawdown_durata_bet = max_dd_durata

    # --- Sharpe Ratio ---
    # Calcolato sui rendimenti PER-BET (profit_loss / stake di ciascuna
    # bet, non sul rendimento aggregato giornaliero): nel value betting
    # non esiste un "risk-free rate" di riferimento sensato come in
    # finanza tradizionale, quindi si usa la forma semplificata
    # Sharpe = media(rendimenti) / deviazione_standard(rendimenti),
    # rendimenti espressi come frazione dello stake (non %%, per avere
    # un numero adimensionale comparabile allo standard).
    # Richiede almeno 2 bet per una deviazione standard sensata.
    rendimenti_frazionali = [
        (b.profit_loss / b.stake) for b in bets_list if b.stake > 0
    ]
    if len(rendimenti_frazionali) >= 2:
        media_rend = statistics.mean(rendimenti_frazionali)
        std_rend = statistics.stdev(rendimenti_frazionali)  # stdev campionaria (n-1), standard per uno storico finito
        m.varianza_rendimenti = round(std_rend ** 2, 6)
        if std_rend > 0:
            # Annualizzazione approssimata: si assume ~250 bet/anno come
            # riferimento generico (mutuato dal numero di giorni di
            # trading in un anno finanziario), utile solo per rendere lo
            # Sharpe comparabile tra periodi di lunghezza diversa. Non e'
            # un vero calendario sportivo (che varia molto per volume di
            # bet piazzate), quindi va letto come indicatore relativo,
            # non come standard assoluto di settore.
            m.sharpe_ratio = round((media_rend / std_rend) * math.sqrt(250), 4)
    # con <2 bet o std=0 (rendimenti identici, rarissimo ma possibile),
    # sharpe_ratio resta None: non abbastanza segnale per un numero
    # significativo

    # --- Metriche di modello ---
    clv_validi = [b.clv_pct for b in bets_list if b.clv_pct is not None]
    if clv_validi:
        m.clv_medio_pct = round(statistics.mean(clv_validi), 3)

    ev_validi = [b for b in bets_list if b.ev_teorico_pct is not None]
    if ev_validi:
        m.ev_medio_teorico_pct = round(statistics.mean(b.ev_teorico_pct for b in ev_validi), 3)
        stake_ev_validi = sum(b.stake for b in ev_validi)
        if stake_ev_validi > 0:
            profitto_ev_validi = sum(b.profit_loss for b in ev_validi)
            m.ev_medio_reale_pct = round((profitto_ev_validi / stake_ev_validi) * 100, 3)

    kelly_validi = [b.kelly_fraction_usata for b in bets_list if b.kelly_fraction_usata is not None]
    if kelly_validi:
        m.kelly_medio_applicato = round(statistics.mean(kelly_validi), 4)

    return m


# ==================================================================
# Heatmap profitto per Campionato/Mercato
# ==================================================================
@dataclass
class CellaHeatmap:
    chiave: str  # es. nome campionato o mercato
    numero_bet: int
    profitto_cumulativo: float
    roi_pct: float


def heatmap_profitto(bets: Iterable, dimensione: str = "campionato") -> list:
    """
    Raggruppa le bet per campionato o mercato e calcola profitto/ROI per
    ciascun gruppo, ordinato per profitto decrescente. `dimensione` deve
    essere 'campionato' o 'mercato' (attributi di BetSettled).
    """
    if dimensione not in ("campionato", "mercato"):
        raise ValueError("dimensione deve essere 'campionato' o 'mercato'")

    bets_list = _normalizza_bets(bets)
    gruppi: dict = {}
    for b in bets_list:
        chiave = getattr(b, dimensione) or "N/D"
        gruppi.setdefault(chiave, []).append(b)

    risultato = []
    for chiave, bet_gruppo in gruppi.items():
        stake_tot = sum(b.stake for b in bet_gruppo)
        profitto_tot = round(sum(b.profit_loss for b in bet_gruppo), 2)
        roi = round((profitto_tot / stake_tot) * 100, 3) if stake_tot > 0 else 0.0
        risultato.append(CellaHeatmap(
            chiave=chiave, numero_bet=len(bet_gruppo),
            profitto_cumulativo=profitto_tot, roi_pct=roi,
        ))

    risultato.sort(key=lambda c: c.profitto_cumulativo, reverse=True)
    return risultato


# ==================================================================
# Segmentazione temporale
# ==================================================================
def segmenta_per_periodo(
    bets: Iterable,
    periodo: str,
    riferimento: Optional[date] = None,
    da: Optional[date] = None,
    a: Optional[date] = None,
) -> list:
    """
    Filtra le bet in base al periodo richiesto (vedi progettazione,
    "Motore di Segmentazione Temporale"). `periodo` accetta:
    'oggi', '7_giorni', '30_giorni', 'mese_corrente', 'mese_precedente',
    'anno_corrente', 'storico_totale', 'range_personalizzato'
    (quest'ultimo richiede `da`/`a`).

    `riferimento` e' la data "oggi" da usare per i calcoli relativi
    (default: date.today()) - parametrizzabile per rendere la funzione
    testabile in modo deterministico (vedi self-test in fondo) invece
    di dipendere implicitamente dalla data reale di esecuzione.
    """
    riferimento = riferimento or date.today()
    bets_list = _normalizza_bets(bets)

    if periodo == "storico_totale":
        return bets_list

    if periodo == "oggi":
        return [b for b in bets_list if b.data_settlement == riferimento]

    if periodo == "7_giorni":
        soglia = _sottrai_giorni(riferimento, 7)
        return [b for b in bets_list if soglia <= b.data_settlement <= riferimento]

    if periodo == "30_giorni":
        soglia = _sottrai_giorni(riferimento, 30)
        return [b for b in bets_list if soglia <= b.data_settlement <= riferimento]

    if periodo == "mese_corrente":
        return [
            b for b in bets_list
            if b.data_settlement.year == riferimento.year and b.data_settlement.month == riferimento.month
        ]

    if periodo == "mese_precedente":
        anno_prec, mese_prec = _mese_precedente(riferimento.year, riferimento.month)
        return [
            b for b in bets_list
            if b.data_settlement.year == anno_prec and b.data_settlement.month == mese_prec
        ]

    if periodo == "anno_corrente":
        return [b for b in bets_list if b.data_settlement.year == riferimento.year]

    if periodo == "range_personalizzato":
        if da is None or a is None:
            raise ValueError("periodo='range_personalizzato' richiede sia 'da' che 'a'")
        if da > a:
            raise ValueError(f"'da' ({da}) non puo' essere successivo a 'a' ({a})")
        return [b for b in bets_list if da <= b.data_settlement <= a]

    raise ValueError(
        f"periodo '{periodo}' non riconosciuto. Valori validi: oggi, 7_giorni, 30_giorni, "
        "mese_corrente, mese_precedente, anno_corrente, storico_totale, range_personalizzato"
    )


def _sottrai_giorni(d: date, n: int) -> date:
    from datetime import timedelta
    return d - timedelta(days=n)


def _mese_precedente(anno: int, mese: int) -> tuple:
    if mese == 1:
        return anno - 1, 12
    return anno, mese - 1


def segmenta_singolo_mese(bets: Iterable, anno: int, mese: int) -> list:
    """
    Variante esplicita per "Singolo Mese Storico" (es. Luglio 2026),
    richiesta separatamente nella progettazione rispetto a
    mese_corrente/mese_precedente perche' non e' relativa a 'oggi'.
    """
    bets_list = _normalizza_bets(bets)
    return [b for b in bets_list if b.data_settlement.year == anno and b.data_settlement.month == mese]


# ==================================================================
# Widget di Confronto Intelligente (A/B Testing)
# ==================================================================
@dataclass
class DifferenzaMetrica:
    """
    Una singola riga del confronto: nome metrica, valore A, valore B,
    differenza assoluta, e indicatore visuale. Il segno "buono" dipende
    dalla metrica (per il drawdown un valore piu' BASSO e' un
    miglioramento, per ROI/Sharpe/WinRate un valore piu' ALTO lo e') -
    gestito da `_migliora_se_aumenta` in CONFRONTO_METRICHE_CONFIG sotto.
    """
    nome: str
    valore_a: Optional[float]
    valore_b: Optional[float]
    differenza: Optional[float]  # valore_b - valore_a (B e' sempre il "nuovo"/candidato, A il "vecchio"/baseline)
    indicatore: str  # '▲' migliorato, '▼' peggiorato, '≈' invariato, '?' non calcolabile


@dataclass
class ConfrontoIntelligente:
    """Output completo del confronto tra due periodi o due versioni."""
    label_a: str
    label_b: str
    differenze: list  # lista di DifferenzaMetrica


# Soglia sotto la quale una differenza e' considerata "invariata" (≈)
# invece che un vero miglioramento/peggioramento. Percentuale RELATIVA
# al valore assoluto di A quando A!=0, altrimenti soglia assoluta -
# evita che un cambiamento di 0.01%% su un ROI del 50%% venga segnalato
# come "peggiorato" quando e' rumore statistico irrilevante.
SOGLIA_INVARIATO_RELATIVA_PCT = 3.0  # 3%% di variazione relativa
SOGLIA_INVARIATO_ASSOLUTA = 0.05     # usata solo quando valore_a == 0

# Per ciascuna metrica: True se un valore PIU' ALTO e' un miglioramento,
# False se un valore PIU' BASSO e' un miglioramento (es. drawdown).
_METRICHE_DIREZIONE = {
    "roi_pct": True,
    "yield_pct": True,
    "win_rate_pct": True,
    "profitto_cumulativo": True,
    "max_drawdown_pct": False,
    "sharpe_ratio": True,
    "profit_factor": True,
    "clv_medio_pct": True,
}


def _indicatore(valore_a: Optional[float], valore_b: Optional[float], migliora_se_aumenta: bool) -> tuple:
    """Ritorna (differenza, indicatore) per una singola metrica."""
    if valore_a is None or valore_b is None:
        return None, "?"

    differenza = round(valore_b - valore_a, 4)

    riferimento = abs(valore_a) if valore_a != 0 else None
    if riferimento is not None:
        variazione_relativa_pct = abs(differenza) / riferimento * 100
        invariato = variazione_relativa_pct < SOGLIA_INVARIATO_RELATIVA_PCT
    else:
        invariato = abs(differenza) < SOGLIA_INVARIATO_ASSOLUTA

    if invariato:
        return differenza, "≈"

    e_migliorato = (differenza > 0) if migliora_se_aumenta else (differenza < 0)
    return differenza, ("▲" if e_migliorato else "▼")


def confronta(
    metriche_a: MetrichePerformance,
    metriche_b: MetrichePerformance,
    label_a: str = "Periodo/Versione A",
    label_b: str = "Periodo/Versione B",
) -> ConfrontoIntelligente:
    """
    Confronta due blocchi di metriche gia' calcolati (tipicamente: stesso
    modello in due periodi diversi, oppure due versioni del sistema
    sullo stesso periodo - il chiamante decide cosa passare, questa
    funzione e' agnostica rispetto al significato di "A" e "B").
    """
    differenze = []
    for nome_metrica, migliora_se_aumenta in _METRICHE_DIREZIONE.items():
        valore_a = getattr(metriche_a, nome_metrica, None)
        valore_b = getattr(metriche_b, nome_metrica, None)
        differenza, indicatore = _indicatore(valore_a, valore_b, migliora_se_aumenta)
        differenze.append(DifferenzaMetrica(
            nome=nome_metrica, valore_a=valore_a, valore_b=valore_b,
            differenza=differenza, indicatore=indicatore,
        ))

    return ConfrontoIntelligente(label_a=label_a, label_b=label_b, differenze=differenze)


def formatta_confronto_testo(confronto: ConfrontoIntelligente) -> str:
    """
    Rappresentazione testuale leggibile del confronto, utile per log/CLI
    o come base per il testo che la dashboard mostrerebbe. La vera
    dashboard HTML/JS arriva nello step finale; qui serve principalmente
    per verificare a colpo d'occhio che gli indicatori abbiano senso.
    """
    righe = [f"Confronto: {confronto.label_a}  vs  {confronto.label_b}", "-" * 60]
    etichette_leggibili = {
        "roi_pct": "ROI %",
        "yield_pct": "Yield %",
        "win_rate_pct": "Win Rate %",
        "profitto_cumulativo": "Profitto Cumulativo",
        "max_drawdown_pct": "Max Drawdown %",
        "sharpe_ratio": "Sharpe Ratio",
        "profit_factor": "Profit Factor",
        "clv_medio_pct": "CLV Medio %",
    }
    for d in confronto.differenze:
        nome = etichette_leggibili.get(d.nome, d.nome)
        va = "N/D" if d.valore_a is None else f"{d.valore_a:.3f}"
        vb = "N/D" if d.valore_b is None else f"{d.valore_b:.3f}"
        diff = "N/D" if d.differenza is None else f"{d.differenza:+.3f}"
        righe.append(f"  {nome:22s} A={va:>10s}  B={vb:>10s}  Δ={diff:>10s}  {d.indicatore}")
    return "\n".join(righe)


# ==================================================================
# Self-test manuale rapido (python3 utils/stats_engine.py)
# ==================================================================
if __name__ == "__main__":
    from datetime import timedelta

    oggi = date(2026, 7, 15)

    def d(giorni_fa):
        return oggi - timedelta(days=giorni_fa)

    # Dataset sintetico: 10 bet, mix vinte/perse/void, con EV/CLV noti,
    # costruito per generare un drawdown riconoscibile a meta' serie.
    bets_a = [
        BetSettled(d(20), stake=50, profit_loss=45, result="vinta", ev_teorico_pct=5.0, clv_pct=1.2, kelly_fraction_usata=0.05, campionato="Italia - Serie A", mercato="1X2"),
        BetSettled(d(19), stake=50, profit_loss=60, result="vinta", ev_teorico_pct=6.0, clv_pct=2.0, kelly_fraction_usata=0.05, campionato="Italia - Serie A", mercato="1X2"),
        BetSettled(d(17), stake=50, profit_loss=-50, result="persa", ev_teorico_pct=4.5, clv_pct=-0.5, kelly_fraction_usata=0.05, campionato="Spagna - LaLiga", mercato="1X2"),
        BetSettled(d(15), stake=50, profit_loss=-50, result="persa", ev_teorico_pct=4.0, clv_pct=-1.0, kelly_fraction_usata=0.05, campionato="Spagna - LaLiga", mercato="1X2"),
        BetSettled(d(14), stake=50, profit_loss=-50, result="persa", ev_teorico_pct=3.8, clv_pct=-0.8, kelly_fraction_usata=0.05, campionato="Italia - Serie A", mercato="1X2"),
        BetSettled(d(10), stake=50, profit_loss=0.0, result="void", ev_teorico_pct=5.5, clv_pct=None, kelly_fraction_usata=0.05, campionato="Italia - Serie A", mercato="1X2"),
        BetSettled(d(8), stake=50, profit_loss=70, result="vinta", ev_teorico_pct=7.0, clv_pct=2.5, kelly_fraction_usata=0.06, campionato="Inghilterra - Premier League", mercato="1X2"),
        BetSettled(d(5), stake=50, profit_loss=55, result="vinta", ev_teorico_pct=5.2, clv_pct=1.5, kelly_fraction_usata=0.05, campionato="Italia - Serie A", mercato="1X2"),
        BetSettled(d(3), stake=50, profit_loss=48, result="vinta", ev_teorico_pct=4.8, clv_pct=1.0, kelly_fraction_usata=0.05, campionato="Inghilterra - Premier League", mercato="1X2"),
        BetSettled(d(1), stake=50, profit_loss=-50, result="persa", ev_teorico_pct=4.2, clv_pct=-0.3, kelly_fraction_usata=0.05, campionato="Italia - Serie A", mercato="1X2"),
    ]

    print("=" * 70)
    print("TEST 1: calcola_metriche() su dataset sintetico completo")
    print("=" * 70)
    m = calcola_metriche(bets_a, bankroll_iniziale=1000.0)
    print(f"Numero bet: {m.numero_bet} (V:{m.numero_bet_vinte} P:{m.numero_bet_perse} Void:{m.numero_bet_void})")
    print(f"Stake totale: {m.stake_totale}  Profitto: {m.profitto_cumulativo}")
    print(f"ROI: {m.roi_pct}%  Yield: {m.yield_pct}%  Win Rate: {m.win_rate_pct}%")
    print(f"Max Drawdown: {m.max_drawdown_pct}%  (durata {m.max_drawdown_durata_bet} bet)")
    print(f"Profit Factor: {m.profit_factor}")
    print(f"Sharpe Ratio: {m.sharpe_ratio}")
    print(f"CLV medio: {m.clv_medio_pct}%  EV teorico medio: {m.ev_medio_teorico_pct}%  EV reale medio: {m.ev_medio_reale_pct}%")
    print(f"Kelly medio applicato: {m.kelly_medio_applicato}")
    print(f"Punti curva bankroll: {len(m.curva_bankroll_cumulativo)}")

    # Verifica manuale indipendente di ROI e Win Rate
    stake_atteso = 50 * 10
    profitto_atteso = 45 + 60 - 50 - 50 - 50 + 0 + 70 + 55 + 48 - 50
    roi_atteso = round(profitto_atteso / stake_atteso * 100, 3)
    win_rate_atteso = round(5 / 8 * 100, 2)  # 5 vinte su 8 decise (10 - 2 void... in realta' 1 void)
    print(f"\n[verifica indipendente] stake atteso={stake_atteso} (match={m.stake_totale == stake_atteso})")
    print(f"[verifica indipendente] profitto atteso={profitto_atteso} (match={m.profitto_cumulativo == profitto_atteso})")
    print(f"[verifica indipendente] ROI atteso={roi_atteso}% (match={m.roi_pct == roi_atteso})")

    print()
    print("=" * 70)
    print("TEST 2: segmentazione temporale")
    print("=" * 70)
    ultimi_7 = segmenta_per_periodo(bets_a, "7_giorni", riferimento=oggi)
    print(f"Bet negli ultimi 7 giorni da {oggi}: {len(ultimi_7)} (attese 3: d(5,3,1))")
    range_custom = segmenta_per_periodo(bets_a, "range_personalizzato", da=d(20), a=d(15))
    print(f"Bet nel range personalizzato [{d(20)}, {d(15)}]: {len(range_custom)} (attese 4)")

    print()
    print("=" * 70)
    print("TEST 3: heatmap per campionato")
    print("=" * 70)
    heatmap = heatmap_profitto(bets_a, dimensione="campionato")
    for cella in heatmap:
        print(f"  {cella.chiave:35s} bet={cella.numero_bet:2d}  profitto={cella.profitto_cumulativo:+8.2f}  ROI={cella.roi_pct:+7.2f}%")

    print()
    print("=" * 70)
    print("TEST 4: confronto intelligente A/B (stesso dataset diviso in 2 meta')")
    print("=" * 70)
    prima_meta = calcola_metriche(bets_a[:5], bankroll_iniziale=1000.0)
    seconda_meta = calcola_metriche(bets_a[5:], bankroll_iniziale=1000.0)
    confronto = confronta(prima_meta, seconda_meta, label_a="Prima meta'", label_b="Seconda meta'")
    print(formatta_confronto_testo(confronto))

    print()
    print("=" * 70)
    print("TEST 5: caso limite - lista vuota")
    print("=" * 70)
    m_vuoto = calcola_metriche([])
    print(f"Metriche su lista vuota: numero_bet={m_vuoto.numero_bet}, roi_pct={m_vuoto.roi_pct}, sharpe={m_vuoto.sharpe_ratio} (nessun crash, valori di default)")

    print()
    print("=" * 70)
    print("TEST 6: caso limite - tutte bet vincenti (profit factor infinito -> None)")
    print("=" * 70)
    bets_solo_vincenti = [
        BetSettled(d(2), stake=50, profit_loss=40, result="vinta", clv_pct=1.0),
        BetSettled(d(1), stake=50, profit_loss=35, result="vinta", clv_pct=1.5),
    ]
    m_solo_vincenti = calcola_metriche(bets_solo_vincenti)
    print(f"Profit factor con zero perdite: {m_solo_vincenti.profit_factor} (atteso: None, non infinito)")

    print()
    print("Tutti i test completati senza eccezioni.")
