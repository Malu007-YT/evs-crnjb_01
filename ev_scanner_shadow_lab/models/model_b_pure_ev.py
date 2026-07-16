"""
EV Scanner AI - Shadow Intelligence System
Step 3/7: models/model_b_pure_ev.py - Shadow Model B (Pure EV)
----------------------------------------------------------------
Obiettivo (vedi progettazione): sfruttare puramente l'Expected Value
matematico, EV = (p * quota) - 1 > soglia, senza filtri euristici di
stabilita' (nessun tetto quota "morbido", nessun filtro lega, nessun
CLV). L'unico "filtro" oltre alla soglia EV e' un sanity check sui dati
sorgente (quota_massima_sanity_check), che non e' un giudizio di
rischio ma pulizia dati - vedi config.py per la distinzione esplicita.

Stessa formula EV/Kelly di produzione (includes/functions.php), stesso
principio dello Shadow Model A: qui cambiano SOLO soglia e assenza di
filtri aggiuntivi, non la matematica di base.
----------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import ModelBConfig
from models.model_a_conservative import EventoInput, ValutazioneShadow, calcola_ev, calcola_kelly_full

# EventoInput e ValutazioneShadow sono definiti in model_a_conservative.py
# e riusati qui invece che duplicati: sono strutture dati generiche (non
# specifiche del modello A), spostarle in un modulo comune dedicato
# (es. models/common.py) e' rimandato a quando un terzo modello ne avra'
# bisogno con una variazione reale, per non introdurre un file in piu'
# senza necessita' concreta al momento - model_d_ensemble.py (step
# successivo) le importera' dallo stesso posto.


class ModelBPureEV:
    """
    Uso tipico da main_shadow_engine.py:

        model = ModelBPureEV(DEFAULT_CONFIG.model_b)
        for evento in eventi_del_batch:
            valutazione = model.valuta(evento)
            if valutazione.accepted:
                # costruisci la riga shadow_bets con model_source='model_b'
                ...
    """

    def __init__(self, cfg: ModelBConfig):
        cfg.validate()
        self.cfg = cfg

    def valuta(self, evento: EventoInput) -> ValutazioneShadow:
        cfg = self.cfg

        # ------------------------------------------------------------
        # 1. Sanity check dati sorgente (non un filtro di rischio, vedi
        #    docstring config.py: una quota fuori da un range ragionevole
        #    e' quasi sempre un dato corrotto, non un vero value bet).
        # ------------------------------------------------------------
        if evento.bookmaker_odds <= 1.0:
            return ValutazioneShadow(accepted=False, motivo_scarto="quota bookmaker non valida (<= 1.0)")
        if evento.bookmaker_odds > cfg.quota_massima_sanity_check:
            return ValutazioneShadow(
                accepted=False,
                motivo_scarto=(
                    f"quota {evento.bookmaker_odds:.2f} oltre il sanity check "
                    f"{cfg.quota_massima_sanity_check:.2f} (probabile dato corrotto, non un vero filtro di rischio)"
                ),
            )
        if not (0 < evento.probability_pct <= 100):
            return ValutazioneShadow(
                accepted=False,
                motivo_scarto=f"probability_pct fuori range valido (0,100]: {evento.probability_pct}",
            )

        # ------------------------------------------------------------
        # 2. Unico vero criterio di selezione: EV puro sopra soglia.
        #    Nessuna penalizzazione, nessun filtro lega/mercato/CLV -
        #    e' esattamente la filosofia "Pure EV" della progettazione.
        # ------------------------------------------------------------
        ev_pct = calcola_ev(evento.probability_pct, evento.bookmaker_odds)

        if ev_pct < cfg.ev_minimo_pct:
            return ValutazioneShadow(
                accepted=False,
                motivo_scarto=f"EV {ev_pct:.3f}%% sotto soglia minima {cfg.ev_minimo_pct:.3f}%%",
            )

        # ------------------------------------------------------------
        # 3. Kelly semi-aggressivo (frazione piu' alta di Model A, vedi
        #    config.py default 0.25).
        # ------------------------------------------------------------
        kelly_full = calcola_kelly_full(evento.probability_pct, evento.bookmaker_odds)
        kelly_stake_frazione = round(kelly_full * cfg.kelly_fraction, 6)

        return ValutazioneShadow(
            accepted=True,
            ev_pct=ev_pct,
            ev_penalizzato_pct=ev_pct,  # Model B non penalizza: penalizzato == grezzo, per uniformita' di schema con Model A
            kelly_fraction_usata=cfg.kelly_fraction,
            kelly_stake_frazione=kelly_stake_frazione,
            confidence_score=100.0,
        )


# ==================================================================
# Self-test manuale rapido (python3 models/model_b_pure_ev.py)
# ==================================================================
if __name__ == "__main__":
    from config import DEFAULT_CONFIG

    model = ModelBPureEV(DEFAULT_CONFIG.model_b)

    casi_test = [
        EventoInput(
            event_id=1, campionato="Italia - Serie A", mercato="1X2", selection="1",
            probability_pct=55.0, bookmaker_odds=2.10, fair_odds=1.90,
        ),  # accettata, stessa dell'esempio Model A ma con soglia piu' bassa
        EventoInput(
            event_id=2, campionato="Islanda - 1. Deild", mercato="1X2", selection="2",
            probability_pct=60.0, bookmaker_odds=2.50, fair_odds=1.65,
        ),  # accettata: Model B NON filtra per lega, a differenza di A
        EventoInput(
            event_id=3, campionato="Italia - Serie A", mercato="1X2", selection="X",
            probability_pct=40.0, bookmaker_odds=5.50, fair_odds=2.50,
        ),  # accettata: Model B NON ha un tetto quota "morbido" come A (5.50 < sanity check 100.0)
        EventoInput(
            event_id=4, campionato="Mondo - Torneo minore", mercato="1X2", selection="1",
            probability_pct=15.0, bookmaker_odds=200.0, fair_odds=6.5,
        ),  # scartata: quota oltre il sanity check (probabile dato corrotto)
    ]

    for evt in casi_test:
        v = model.valuta(evt)
        stato = "ACCETTATA" if v.accepted else f"scartata ({v.motivo_scarto})"
        print(f"evento#{evt.event_id} [{evt.selection}] -> {stato}"
              + (f" | EV={v.ev_pct}%% kelly_stake_frazione={v.kelly_stake_frazione}" if v.accepted else ""))
