"""
EV Scanner AI - Shadow Intelligence System
Step 3/7: models/model_a_conservative.py - Shadow Model A (Conservative)
----------------------------------------------------------------
Obiettivo (vedi progettazione): massimizzare il ROI storico riducendo
drasticamente la varianza. Non e' un modello che "impara" (a differenza
di Model C): e' un filtro euristico deterministico, gli stessi input
producono sempre lo stesso output - per questo e' pass/fail, non uno
score continuo (coerente con confidence_score=100 di default in
shadow_bets, vedi schema.sql).

Le formule EV/Kelly sono DELIBERATAMENTE identiche a quelle di
produzione (includes/functions.php::calcola_ev / calcola_kelly), non
una reinvenzione: uno Shadow Model deve valutare "cosa avrebbe fatto un
filtro con questa filosofia sugli stessi dati", non introdurre una
matematica diversa da quella già validata in produzione. Le uniche
differenze rispetto al filtro reale sono le SOGLIE e i filtri aggiuntivi
(decadimento quota, CLV, leghe), che sono esattamente cio' che rende
Model A "un modello diverso" e non un duplicato del filtro ufficiale.
----------------------------------------------------------------
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from config import ModelAConfig


# ==================================================================
# Input/Output tipizzati
# ==================================================================
@dataclass
class EventoInput:
    """
    Vista minima di un evento+selezione candidata, cosi' come la
    costruisce main_shadow_engine.py leggendo `eventi` (e opzionalmente
    `scommesse` per il CLV storico). Un singolo evento produce fino a 3
    EventoInput (uno per selection 1/X/2), ciascuno valutato
    indipendentemente dal modello.
    """
    event_id: int
    campionato: str
    mercato: str  # es. "1X2" - allineato a scommesse.selection nello schema attuale
    selection: str  # '1' | 'X' | '2'
    probability_pct: float  # probabilita stimata per QUESTA selection
    bookmaker_odds: float
    fair_odds: float
    # CLV stimato (probabilita teorica %% di battere la quota di chiusura),
    # None se il provider non ha dato disponibile per questo evento (vedi
    # SharpApiClient/CLV surrogato in produzione - lo Shadow Lab riusa lo
    # stesso dato quando presente, non lo ricalcola).
    clv_stimato_pct: Optional[float] = None


@dataclass
class ValutazioneShadow:
    """
    Output di un modello shadow per un singolo EventoInput: o il modello
    "passa" l'evento (accepted=True, con tutti i campi calcolati pronti
    per un INSERT in shadow_bets) o lo scarta (accepted=False, con un
    motivo leggibile per debug/log - MAI silenzioso, coerente con lo
    stile di log dettagliato visto in config.php/SchemaSync.php).
    """
    accepted: bool
    motivo_scarto: Optional[str] = None

    # Popolati solo se accepted=True. Ricalcano 1:1 le colonne di
    # shadow_bets che questo modello e' responsabile di produrre (il
    # resto, es. model_source/model_version_id/event_id, viene aggiunto
    # dal chiamante in main_shadow_engine.py, che e' l'unico punto che
    # conosce quella metadata).
    ev_pct: Optional[float] = None
    ev_penalizzato_pct: Optional[float] = None  # solo Model A: EV dopo decadimento quota
    kelly_fraction_usata: Optional[float] = None
    kelly_stake_frazione: Optional[float] = None  # frazione di bankroll (kelly_full * kelly_fraction)
    confidence_score: float = 100.0


# ==================================================================
# Funzioni di calcolo core (identiche a includes/functions.php)
# ==================================================================
def calcola_ev(probability_pct: float, bookmaker_odds: float) -> float:
    """
    EV%% = (probabilita * quota - 1) * 100
    Porting 1:1 di calcola_ev() in includes/functions.php.
    """
    p = probability_pct / 100.0
    return round((p * bookmaker_odds - 1) * 100, 3)


def calcola_kelly_full(probability_pct: float, bookmaker_odds: float) -> float:
    """
    Kelly Criterion pieno (f*), stesso porting di calcola_kelly()
    (solo il ramo 'full': le frazioni half/quarter di produzione non
    servono qui, ogni shadow model applica la PROPRIA kelly_fraction
    da config.py sopra questo valore, vedi kelly_stake_frazione).
    """
    p = probability_pct / 100.0
    b = bookmaker_odds - 1
    if b <= 0:
        return 0.0
    f = ((p * b) - (1 - p)) / b
    return max(0.0, round(f, 4))


# ==================================================================
# Shadow Model A - Conservative
# ==================================================================
class ModelAConservative:
    """
    Uso tipico da main_shadow_engine.py:

        model = ModelAConservative(DEFAULT_CONFIG.model_a)
        for evento in eventi_del_batch:
            valutazione = model.valuta(evento)
            if valutazione.accepted:
                # costruisci la riga shadow_bets con model_source='model_a'
                ...
    """

    def __init__(self, cfg: ModelAConfig):
        cfg.validate()  # non fidarsi mai di una config non validata, anche se gia' validata a monte
        self.cfg = cfg

    def valuta(self, evento: EventoInput) -> ValutazioneShadow:
        cfg = self.cfg

        # ------------------------------------------------------------
        # 1. Filtro mercato: Model A ragiona solo sui mercati consentiti
        #    (di default solo "1X2", vedi commento in config.py sul
        #    perche' altri mercati non sono ancora rappresentabili nello
        #    schema produzione attuale).
        # ------------------------------------------------------------
        if evento.mercato not in cfg.mercati_consentiti:
            return ValutazioneShadow(
                accepted=False,
                motivo_scarto=f"mercato '{evento.mercato}' non in mercati_consentiti {cfg.mercati_consentiti}",
            )

        # ------------------------------------------------------------
        # 2. Filtro lega ad alta liquidita' (se la lista non e' vuota).
        #    Confronto case-insensitive per sottostringa, stesso
        #    approccio pragmatico di THE_ODDS_API_LEAGUE_MAP in
        #    config.php: eventi.campionato e' testo libero "Paese - Lega",
        #    non una chiave normalizzata.
        # ------------------------------------------------------------
        if cfg.leghe_alta_liquidita_keywords:
            campionato_lower = evento.campionato.lower()
            match_lega = any(kw in campionato_lower for kw in cfg.leghe_alta_liquidita_keywords)
            if not match_lega:
                return ValutazioneShadow(
                    accepted=False,
                    motivo_scarto=f"campionato '{evento.campionato}' non tra le leghe ad alta liquidita' configurate",
                )

        # ------------------------------------------------------------
        # 3. Quota massima assoluta: oltre questa soglia, scarto a
        #    prescindere dall'EV (troppa varianza per la filosofia
        #    Conservative, vedi config.py).
        # ------------------------------------------------------------
        if evento.bookmaker_odds > cfg.quota_massima_assoluta:
            return ValutazioneShadow(
                accepted=False,
                motivo_scarto=(
                    f"quota {evento.bookmaker_odds:.2f} oltre il tetto assoluto "
                    f"{cfg.quota_massima_assoluta:.2f}"
                ),
            )
        if evento.bookmaker_odds <= 1.0:
            # Sanity check sui dati sorgente: una quota <=1 e' un dato
            # corrotto (probabilita implicita >=100%%), mai un vero value
            # bet - va scartata come igiene dati, non come giudizio di
            # rischio (stesso spirito del sanity check in Model B).
            return ValutazioneShadow(accepted=False, motivo_scarto="quota bookmaker non valida (<= 1.0)")

        # ------------------------------------------------------------
        # 4. EV grezzo (stessa formula di produzione).
        # ------------------------------------------------------------
        ev_pct = calcola_ev(evento.probability_pct, evento.bookmaker_odds)

        # ------------------------------------------------------------
        # 5. Penalizzazione esponenziale sulla quota (cuore della
        #    filosofia Conservative, vedi config.py per la formula e
        #    l'intuizione: a parita' di EV nominale, una quota piu' alta
        #    ha varianza maggiore sul singolo evento, quindi l'EV
        #    "effettivo" ai fini della decisione viene scontato.
        #
        #    ev_penalizzato = ev_pct * exp(-lambda * (quota - 1))
        #
        #    Nota: si applica il fattore di decadimento SOLO se ev_pct e'
        #    positivo. Su un EV negativo scontarlo ulteriormente lo
        #    renderebbe "meno negativo" in valore assoluto, il che
        #    invertirebbe l'intento del filtro (un EV negativo va sempre
        #    scartato al passo successivo, non reso piu' accettabile).
        # ------------------------------------------------------------
        if ev_pct > 0:
            fattore_decadimento = math.exp(-cfg.decadimento_quota_lambda * (evento.bookmaker_odds - 1))
            ev_penalizzato_pct = round(ev_pct * fattore_decadimento, 3)
        else:
            ev_penalizzato_pct = ev_pct

        if ev_penalizzato_pct < cfg.ev_minimo_pct:
            return ValutazioneShadow(
                accepted=False,
                motivo_scarto=(
                    f"EV penalizzato {ev_penalizzato_pct:.3f}%% sotto soglia minima "
                    f"{cfg.ev_minimo_pct:.3f}%% (EV grezzo era {ev_pct:.3f}%%)"
                ),
            )

        # ------------------------------------------------------------
        # 6. Filtro CLV stimato. Se il dato manca, applica la policy
        #    configurata (scarta per prudenza di default, coerente con
        #    la filosofia Conservative - un modello aggressivo come B
        #    non ha questo filtro affatto).
        # ------------------------------------------------------------
        if evento.clv_stimato_pct is None:
            if cfg.clv_fallback_policy == "scarta":
                return ValutazioneShadow(
                    accepted=False,
                    motivo_scarto="CLV stimato non disponibile per l'evento e clv_fallback_policy='scarta'",
                )
            # "ignora_filtro_clv": prosegue senza applicare il filtro CLV
        elif evento.clv_stimato_pct < cfg.clv_stimato_minimo_pct:
            return ValutazioneShadow(
                accepted=False,
                motivo_scarto=(
                    f"CLV stimato {evento.clv_stimato_pct:.3f}%% sotto soglia minima "
                    f"{cfg.clv_stimato_minimo_pct:.3f}%%"
                ),
            )

        # ------------------------------------------------------------
        # 7. Tutti i filtri superati: calcola Kelly (sull'EV/probabilita'
        #    REALI, non su quelli penalizzati - la penalizzazione e' solo
        #    un criterio di AMMISSIONE, non deve distorcere il sizing
        #    matematico dello stake, che deve restare corretto secondo la
        #    formula standard).
        # ------------------------------------------------------------
        kelly_full = calcola_kelly_full(evento.probability_pct, evento.bookmaker_odds)
        kelly_stake_frazione = round(kelly_full * cfg.kelly_fraction, 6)

        return ValutazioneShadow(
            accepted=True,
            ev_pct=ev_pct,
            ev_penalizzato_pct=ev_penalizzato_pct,
            kelly_fraction_usata=cfg.kelly_fraction,
            kelly_stake_frazione=kelly_stake_frazione,
            confidence_score=100.0,
        )


# ==================================================================
# Self-test manuale rapido (python3 models/model_a_conservative.py)
# ==================================================================
if __name__ == "__main__":
    from config import DEFAULT_CONFIG

    model = ModelAConservative(DEFAULT_CONFIG.model_a)

    casi_test = [
        EventoInput(
            event_id=1, campionato="Italia - Serie A", mercato="1X2", selection="1",
            probability_pct=55.0, bookmaker_odds=2.10, fair_odds=1.90, clv_stimato_pct=1.5,
        ),
        EventoInput(
            event_id=2, campionato="Islanda - 1. Deild", mercato="1X2", selection="2",
            probability_pct=60.0, bookmaker_odds=2.50, fair_odds=1.65, clv_stimato_pct=2.0,
        ),  # scartato: lega non ad alta liquidita'
        EventoInput(
            event_id=3, campionato="Italia - Serie A", mercato="1X2", selection="X",
            probability_pct=40.0, bookmaker_odds=5.50, fair_odds=2.50, clv_stimato_pct=3.0,
        ),  # scartato: quota oltre tetto assoluto
        EventoInput(
            event_id=4, campionato="Spagna - LaLiga", mercato="1X2", selection="1",
            probability_pct=52.0, bookmaker_odds=2.00, fair_odds=1.92, clv_stimato_pct=None,
        ),  # scartato: CLV mancante, policy default 'scarta'
    ]

    for evt in casi_test:
        v = model.valuta(evt)
        stato = "ACCETTATA" if v.accepted else f"scartata ({v.motivo_scarto})"
        print(f"evento#{evt.event_id} [{evt.selection}] -> {stato}"
              + (f" | EV grezzo={v.ev_pct}%% EV pen.={v.ev_penalizzato_pct}%% "
                 f"kelly_stake_frazione={v.kelly_stake_frazione}" if v.accepted else ""))
