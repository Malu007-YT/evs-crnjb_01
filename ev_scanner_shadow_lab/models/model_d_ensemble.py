"""
EV Scanner AI - Shadow Intelligence System
Step 5/7: models/model_d_ensemble.py - Shadow Model D (Ensemble)
----------------------------------------------------------------
Obiettivo (vedi progettazione): consolidare i segnali di Model A, B e C
sullo STESSO evento+selection in un punteggio di confidenza unificato
0-100. A differenza di A/B (filtri deterministici puri) e di C (che
arriva nello step 6), D non guarda i dati grezzi dell'evento: guarda
SOLO le ValutazioneShadow gia' prodotte dagli altri modelli. E'
letteralmente un consumatore dei loro output, non un quarto modo
indipendente di valutare lo stesso evento.

Logica di scoring (dalla progettazione):
- Se A, B e C concordano tutti sullo stesso event_id+selection, lo score
  sale al MASSIMO (100 di default), indipendentemente dai pesi Sharpe.
- Altrimenti (consenso parziale, es. solo 2 su 3), lo score e' una
  combinazione di quanti modelli concordano (fattore di copertura) e
  quanto sono stati performanti di recente i modelli concordi (fattore
  di qualita', basato sui loro Sharpe Ratio nella finestra configurata,
  default 30 giorni). Se un modello ha troppo poche bet settled nella
  finestra per un Sharpe affidabile, riceve un peso neutro invece del
  suo Sharpe calcolato (vedi minimo_bet_per_peso_sharpe in config.py).

Kelly di Model D: non applica la propria logica di sizing basata su
probabilita' (non ha una probabilita' propria, e' un consenso), ma
scala kelly_fraction_base in proporzione allo score di consenso - piu'
alto il consenso, piu' vicino al Kelly "pieno" configurato; consenso
appena sopra soglia, Kelly ridotto a meta'. Vedi _scala_kelly_per_score().
----------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from config import ModelDConfig
from models.model_a_conservative import ValutazioneShadow
from utils.stats_engine import BetSettled, calcola_metriche


# ==================================================================
# Input: i segnali sorgente su cui Model D lavora
# ==================================================================
@dataclass
class SegnaleModelloSorgente:
    """
    Un singolo segnale in ingresso a Model D: la valutazione che UN
    modello (A, B o C) ha prodotto per un dato event_id+selection.
    Costruito dal chiamante (main_shadow_engine.py, step futuro) dopo
    aver gia' fatto girare A/B/C sullo stesso batch di eventi - Model D
    non rilancia i modelli, riceve solo i loro esiti gia' calcolati.

    `model_source` usa le stesse chiavi di shadow_bets.model_source
    ('model_a'/'model_b'/'model_c') per coerenza diretta con lo schema.
    Solo segnali con valutazione.accepted=True devono arrivare qui: il
    filtraggio va fatto dal chiamante prima di costruire la lista.
    """
    model_source: str  # 'model_a' | 'model_b' | 'model_c'
    valutazione: ValutazioneShadow


@dataclass
class RisultatoEnsemble:
    """
    Output di Model D per un singolo event_id+selection. A differenza di
    ValutazioneShadow (pass/fail secco), qui lo score e' sempre
    calcolato anche quando il risultato finale e' "non genera bet" (sotto
    soglia) - utile per debug/analisi di quanto vicino un evento sia
    stato dal generare un consenso forte, invece di uno scarto muto.
    """
    accepted: bool  # True solo se score >= score_minimo_per_generare_bet
    score_consenso: float  # 0-100
    modelli_concordi: List[str]  # es. ['model_a', 'model_b']
    consenso_totale: bool  # True se concordano TUTTI e 3 i modelli (A, B, C)
    pesi_usati: Dict[str, float]  # peso Sharpe-based applicato a ciascun modello concorde, per trasparenza/debug
    motivo_scarto: Optional[str] = None

    # Popolati solo se accepted=True, per un eventuale INSERT in
    # shadow_bets con model_source='model_d'. EV e' la MEDIA PESATA
    # (stessi pesi del consenso) dei modelli concordi - non ha senso
    # "inventare" un EV proprio per un meta-modello che non guarda i
    # dati grezzi dell'evento.
    ev_pct_medio: Optional[float] = None
    kelly_fraction_usata: Optional[float] = None
    kelly_stake_frazione: Optional[float] = None


# Insieme fisso dei modelli sorgente che Model D conosce. Model C non
# esiste ancora in questo step (arriva allo step 6): se non compare mai
# tra i segnali ricevuti in questa fase del progetto e' normale, non un
# bug - il consenso_totale nella pratica odierna richiede solo A+B
# finche' C non e' online, ma la logica resta corretta e pronta per
# quando lo sara' (nessuna modifica necessaria a questo file).
MODELLI_SORGENTE_NOTI = {"model_a", "model_b", "model_c"}


# ==================================================================
# Calcolo pesi Sharpe-based
# ==================================================================
def calcola_pesi_sharpe(
    storico_bet_per_modello: Dict[str, List[BetSettled]],
    cfg: ModelDConfig,
) -> Dict[str, float]:
    """
    Calcola il peso di ciascun modello sorgente in base al suo Sharpe
    Ratio nella finestra configurata (cfg.finestra_sharpe_giorni),
    riusando direttamente utils.stats_engine.calcola_metriche() invece
    di reimplementare il calcolo Sharpe qui - un solo punto di verita'
    per quella formula in tutto il progetto.

    `storico_bet_per_modello` e' un dict {model_source: [bet settled
    shadow di quel modello negli ultimi N giorni]}, gia' filtrato per
    finestra temporale dal chiamante (main_shadow_engine.py, che ha
    accesso al DB - questa funzione resta pura e non fa query).

    Se un modello ha meno di cfg.minimo_bet_per_peso_sharpe bet nella
    finestra, o uno Sharpe non calcolabile (es. varianza zero), riceve
    cfg.peso_neutro_default invece dello Sharpe vero - uno Sharpe
    calcolato su 3 bet e' rumore, non segnale.

    I pesi vengono troncati a un minimo di 0.05 (mai zero o negativi):
    un modello con Sharpe negativo (in perdita di recente) deve pesare
    molto poco nel consenso, ma un peso zero o negativo romperebbe la
    media pesata (negativo capovolgerebbe il contributo nella direzione
    sbagliata, zero escluderebbe completamente un modello che ha
    comunque concordato sulla direzione dell'evento, il che resta
    informazione valida anche se il modello sta attraversando un
    periodo no).
    """
    PESO_MINIMO_TRONCAMENTO = 0.05
    pesi = {}

    for model_source, storico in storico_bet_per_modello.items():
        if len(storico) < cfg.minimo_bet_per_peso_sharpe:
            pesi[model_source] = cfg.peso_neutro_default
            continue

        metriche = calcola_metriche(storico)
        if metriche.sharpe_ratio is None:
            pesi[model_source] = cfg.peso_neutro_default
        else:
            pesi[model_source] = max(PESO_MINIMO_TRONCAMENTO, metriche.sharpe_ratio)

    return pesi


# ==================================================================
# Shadow Model D - Ensemble
# ==================================================================
class ModelDEnsemble:
    """
    Uso tipico da main_shadow_engine.py:

        pesi = calcola_pesi_sharpe(storico_ultimi_30_giorni_per_modello, cfg.model_d)
        model_d = ModelDEnsemble(cfg.model_d)

        for event_id, selection in eventi_del_batch:
            segnali = [s for s in tutti_i_segnali_del_batch
                       if s_evento(s) == event_id and s_selection(s) == selection]
            risultato = model_d.valuta(segnali, pesi)
            if risultato.accepted:
                # costruisci la riga shadow_bets con model_source='model_d'
                ...

    Nota: i pesi Sharpe vanno ricalcolati periodicamente (es. una volta
    al giorno) da calcola_pesi_sharpe(), non ad ogni singola valutazione
    - main_shadow_engine.py li calcola una volta per batch e li passa
    qui; questa classe non fa query e non mantiene stato tra chiamate.
    """

    def __init__(self, cfg: ModelDConfig):
        cfg.validate()
        self.cfg = cfg

    def valuta(
        self,
        segnali: List[SegnaleModelloSorgente],
        pesi_sharpe: Dict[str, float],
    ) -> RisultatoEnsemble:
        cfg = self.cfg

        if not segnali:
            return RisultatoEnsemble(
                accepted=False, score_consenso=0.0, modelli_concordi=[],
                consenso_totale=False, pesi_usati={},
                motivo_scarto="nessun segnale sorgente ricevuto per questo evento+selection",
            )

        for s in segnali:
            if not s.valutazione.accepted:
                raise ValueError(
                    f"Model D ha ricevuto un segnale non accettato da {s.model_source}: "
                    "solo segnali con accepted=True devono arrivare a valuta(). "
                    "Il filtraggio va fatto dal chiamante prima di costruire la lista segnali."
                )
            if s.model_source not in MODELLI_SORGENTE_NOTI:
                raise ValueError(f"model_source sconosciuto: '{s.model_source}'. Attesi: {MODELLI_SORGENTE_NOTI}")

        modelli_concordi = sorted({s.model_source for s in segnali})
        # Se lo stesso modello compare due volte (bug a monte nel
        # chiamante: non dovrebbe mai succedere che A valuti due volte lo
        # stesso event_id+selection, vedi UNIQUE KEY in shadow_bets), lo
        # segnaliamo esplicitamente invece di ignorarlo silenziosamente -
        # meglio un errore rumoroso qui che uno score doppiamente pesato
        # in modo silenzioso a valle.
        if len(modelli_concordi) != len(segnali):
            raise ValueError(
                "Model D ha ricevuto piu' segnali dallo stesso model_source per lo stesso "
                "evento+selection: probabile bug nel chiamante (main_shadow_engine.py) nella "
                "costruzione della lista segnali."
            )

        consenso_totale = set(modelli_concordi) == MODELLI_SORGENTE_NOTI

        # ------------------------------------------------------------
        # Score di consenso.
        # ------------------------------------------------------------
        pesi_usati = {m: pesi_sharpe.get(m, cfg.peso_neutro_default) for m in modelli_concordi}

        if consenso_totale:
            score = cfg.score_massimo_su_consenso_totale
        else:
            # Score = fattore di copertura (quanti modelli su 3 concordano)
            # x fattore di qualita' (quanto sono stati performanti di
            # recente i modelli concordi, via pesi Sharpe normalizzati
            # rispetto al peso neutro 1.0). La componente dominante resta
            # sempre la copertura: 2 modelli concordi con Sharpe pessimo
            # devono comunque pesare piu' di 1 modello concorde con
            # Sharpe ottimo, perche' il CONSENSO in se' e' il segnale
            # primario del meta-modello (vedi progettazione).
            fattore_copertura = len(modelli_concordi) / len(MODELLI_SORGENTE_NOTI)
            peso_medio_normalizzato = sum(pesi_usati.values()) / len(pesi_usati)
            # Il fattore qualita' oscilla tra 0.7 e 1.0 (mai sotto 0.7, per
            # non lasciare che un pessimo Sharpe recente da solo affossi
            # uno score altrimenti supportato da un buon consenso):
            # peso_medio_normalizzato=1.0 (neutro) -> qualita'=0.85 (meta' via)
            # peso_medio_normalizzato>=2.0 (ottimo) -> qualita'=1.0 (tetto)
            # peso_medio_normalizzato->0 (pessimo)  -> qualita'=0.7 (pavimento)
            fattore_qualita = 0.7 + 0.3 * min(1.0, peso_medio_normalizzato / 2.0)

            score = round(cfg.score_massimo_su_consenso_totale * fattore_copertura * fattore_qualita, 2)
            # Mai raggiungere (o superare per arrotondamento) il punteggio
            # massimo senza consenso totale: sarebbe concettualmente
            # sbagliato che un 2-su-3 "colpisca" lo stesso score del 3-su-3.
            score = min(score, cfg.score_massimo_su_consenso_totale - 0.01)

        if score < cfg.score_minimo_per_generare_bet:
            return RisultatoEnsemble(
                accepted=False, score_consenso=score, modelli_concordi=modelli_concordi,
                consenso_totale=consenso_totale, pesi_usati=pesi_usati,
                motivo_scarto=f"score consenso {score} sotto soglia minima {cfg.score_minimo_per_generare_bet}",
            )

        # ------------------------------------------------------------
        # EV medio pesato (media pesata degli EV dei modelli concordi,
        # con gli stessi pesi usati per lo score) e Kelly scalato.
        # ------------------------------------------------------------
        somma_pesi = sum(pesi_usati[s.model_source] for s in segnali)
        ev_pct_medio = round(
            sum(s.valutazione.ev_pct * pesi_usati[s.model_source] for s in segnali) / somma_pesi, 3
        )

        # Base per il Kelly di Model D: la media pesata (stessi pesi) del
        # kelly_stake_frazione gia' calcolato dai modelli concordi, poi
        # riscalata secondo lo score di consenso - Model D non ha una
        # propria coppia (probabilita, quota) univoca da cui derivare un
        # Kelly indipendente, essendo un consenso su segnali eterogenei
        # (A e B possono avere probabilita' stimate diverse sullo stesso
        # evento). Vedi _scala_kelly_per_score() per il fattore di scaling.
        kelly_stake_base_media = sum(
            s.valutazione.kelly_stake_frazione * pesi_usati[s.model_source] for s in segnali
        ) / somma_pesi

        kelly_fraction_scalata = self._scala_kelly_per_score(score)
        fattore_scaling_kelly = (kelly_fraction_scalata / cfg.kelly_fraction_base) if cfg.kelly_fraction_base > 0 else 0.0
        kelly_stake_frazione = round(kelly_stake_base_media * fattore_scaling_kelly, 6)

        return RisultatoEnsemble(
            accepted=True, score_consenso=score, modelli_concordi=modelli_concordi,
            consenso_totale=consenso_totale, pesi_usati=pesi_usati,
            ev_pct_medio=ev_pct_medio,
            kelly_fraction_usata=round(kelly_fraction_scalata, 4),
            kelly_stake_frazione=kelly_stake_frazione,
        )

    def _scala_kelly_per_score(self, score: float) -> float:
        """
        Scala kelly_fraction_base linearmente tra
        [score_minimo_per_generare_bet, score_massimo_su_consenso_totale]:
        al minimo score accettabile, Kelly e' al 50%% della base (non
        zero: il segnale e' comunque valido, solo meno forte); al
        massimo score (consenso totale), Kelly e' al 100%% della base.
        """
        cfg = self.cfg
        range_score = cfg.score_massimo_su_consenso_totale - cfg.score_minimo_per_generare_bet
        if range_score <= 0:
            return cfg.kelly_fraction_base

        posizione = (score - cfg.score_minimo_per_generare_bet) / range_score
        posizione = min(1.0, max(0.0, posizione))
        moltiplicatore = 0.5 + 0.5 * posizione  # da 0.5x a 1.0x
        return round(cfg.kelly_fraction_base * moltiplicatore, 4)


# ==================================================================
# Self-test manuale rapido (python3 models/model_d_ensemble.py)
# ==================================================================
if __name__ == "__main__":
    from config import DEFAULT_CONFIG
    from datetime import date, timedelta

    cfg = DEFAULT_CONFIG.model_d
    model_d = ModelDEnsemble(cfg)

    def valutazione_fittizia(ev_pct, kelly_stake):
        return ValutazioneShadow(
            accepted=True, ev_pct=ev_pct, ev_penalizzato_pct=ev_pct,
            kelly_fraction_usata=0.1, kelly_stake_frazione=kelly_stake, confidence_score=100.0,
        )

    print("=" * 70)
    print("TEST 1: consenso totale (A+B+C concordano) -> score max")
    print("=" * 70)
    segnali_totale = [
        SegnaleModelloSorgente("model_a", valutazione_fittizia(6.0, 0.01)),
        SegnaleModelloSorgente("model_b", valutazione_fittizia(15.0, 0.03)),
        SegnaleModelloSorgente("model_c", valutazione_fittizia(9.0, 0.02)),
    ]
    pesi_neutri = {"model_a": 1.0, "model_b": 1.0, "model_c": 1.0}
    r1 = model_d.valuta(segnali_totale, pesi_neutri)
    print(f"accepted={r1.accepted} score={r1.score_consenso} consenso_totale={r1.consenso_totale}")
    print(f"EV medio pesato={r1.ev_pct_medio}%  kelly_fraction_usata={r1.kelly_fraction_usata}  kelly_stake_frazione={r1.kelly_stake_frazione}")
    assert r1.score_consenso == cfg.score_massimo_su_consenso_totale, "score deve essere il massimo su consenso totale"
    assert r1.accepted
    assert r1.kelly_fraction_usata == cfg.kelly_fraction_base, "su consenso totale il Kelly deve essere al 100% della base"

    print()
    print("=" * 70)
    print("TEST 2: consenso parziale (solo A+B), pesi Sharpe neutri")
    print("=" * 70)
    segnali_parziale = [
        SegnaleModelloSorgente("model_a", valutazione_fittizia(6.0, 0.01)),
        SegnaleModelloSorgente("model_b", valutazione_fittizia(15.0, 0.03)),
    ]
    r2 = model_d.valuta(segnali_parziale, pesi_neutri)
    print(f"accepted={r2.accepted} score={r2.score_consenso} consenso_totale={r2.consenso_totale}")
    print(f"(atteso: score < {cfg.score_massimo_su_consenso_totale}, sopra soglia {cfg.score_minimo_per_generare_bet})")
    assert r2.score_consenso < cfg.score_massimo_su_consenso_totale
    assert not r2.consenso_totale

    print()
    print("=" * 70)
    print("TEST 3: singolo modello (solo A) -> score piu' basso di 2 concordi")
    print("=" * 70)
    segnali_singolo = [SegnaleModelloSorgente("model_a", valutazione_fittizia(6.0, 0.01))]
    r3 = model_d.valuta(segnali_singolo, pesi_neutri)
    print(f"accepted={r3.accepted} score={r3.score_consenso} motivo_scarto={r3.motivo_scarto}")
    assert r3.score_consenso < r2.score_consenso, "1 modello deve pesare meno di 2 concordi"

    print()
    print("=" * 70)
    print("TEST 4: copertura domina sempre sulla qualita' (2 concordi con Sharpe")
    print("        pessimo devono comunque battere 1 concorde con Sharpe ottimo)")
    print("=" * 70)
    pesi_pessimi = {"model_a": 0.05, "model_b": 0.05, "model_c": 0.05}
    pesi_ottimi = {"model_a": 5.0, "model_b": 5.0, "model_c": 5.0}
    r4a = model_d.valuta(segnali_parziale, pesi_pessimi)  # 2 concordi, Sharpe pessimo
    r4b = model_d.valuta(segnali_singolo, pesi_ottimi)    # 1 concorde, Sharpe ottimo
    print(f"2 concordi (Sharpe pessimo): score={r4a.score_consenso}")
    print(f"1 concorde (Sharpe ottimo):  score={r4b.score_consenso}")
    assert r4a.score_consenso > r4b.score_consenso, "la copertura deve dominare sulla qualita'"

    print()
    print("=" * 70)
    print("TEST 5: EV medio pesato pende verso il modello con Sharpe piu' alto")
    print("=" * 70)
    pesi_sbilanciati = {"model_a": 3.0, "model_b": 0.3, "model_c": 1.0}
    r5 = model_d.valuta(segnali_parziale, pesi_sbilanciati)
    print(f"pesi_usati={r5.pesi_usati}  EV medio={r5.ev_pct_medio}% (EV_A=6.0, EV_B=15.0)")
    assert r5.ev_pct_medio < 10.5, "con A pesato 10x piu' di B, l'EV medio deve pendere verso A (6.0)"

    print()
    print("=" * 70)
    print("TEST 6: nessun segnale -> scarto pulito, nessuna eccezione")
    print("=" * 70)
    r6 = model_d.valuta([], pesi_neutri)
    print(f"accepted={r6.accepted} motivo_scarto={r6.motivo_scarto}")
    assert not r6.accepted

    print()
    print("=" * 70)
    print("TEST 7: calcola_pesi_sharpe() con storico insufficiente -> peso neutro")
    print("=" * 70)
    oggi = date(2026, 7, 15)
    storico_pochi = {
        "model_a": [
            BetSettled(oggi - timedelta(days=i), stake=50, profit_loss=10, result="vinta")
            for i in range(3)  # solo 3 bet, sotto il minimo di default (15)
        ],
    }
    pesi_calcolati = calcola_pesi_sharpe(storico_pochi, cfg)
    print(f"peso model_a con solo 3 bet nella finestra: {pesi_calcolati['model_a']} (atteso: peso neutro {cfg.peso_neutro_default})")
    assert pesi_calcolati["model_a"] == cfg.peso_neutro_default

    print()
    print("=" * 70)
    print("TEST 8: calcola_pesi_sharpe() con storico sufficiente e Sharpe positivo")
    print("=" * 70)
    storico_sufficiente = {
        "model_b": [
            BetSettled(oggi - timedelta(days=i), stake=50,
                       profit_loss=(20 if i % 3 != 0 else -30), result=("vinta" if i % 3 != 0 else "persa"))
            for i in range(20)
        ],
    }
    pesi_calcolati_2 = calcola_pesi_sharpe(storico_sufficiente, cfg)
    print(f"peso model_b con 20 bet, per lo piu' vincenti: {pesi_calcolati_2['model_b']}")
    assert pesi_calcolati_2["model_b"] != cfg.peso_neutro_default, "con dati sufficienti il peso deve riflettere lo Sharpe reale, non il neutro"

    print()
    print("=" * 70)
    print("TEST 9: robustezza - segnale con accepted=False deve sollevare ValueError")
    print("=" * 70)
    try:
        segnale_invalido = SegnaleModelloSorgente(
            "model_a", ValutazioneShadow(accepted=False, motivo_scarto="test")
        )
        model_d.valuta([segnale_invalido], pesi_neutri)
        print("ERRORE: doveva sollevare ValueError e non l'ha fatto")
        raise SystemExit(1)
    except ValueError as e:
        print(f"ValueError sollevato correttamente: {e}")

    print()
    print("=" * 70)
    print("TEST 10: robustezza - stesso model_source duplicato deve sollevare ValueError")
    print("=" * 70)
    try:
        segnali_duplicati = [
            SegnaleModelloSorgente("model_a", valutazione_fittizia(6.0, 0.01)),
            SegnaleModelloSorgente("model_a", valutazione_fittizia(7.0, 0.01)),
        ]
        model_d.valuta(segnali_duplicati, pesi_neutri)
        print("ERRORE: doveva sollevare ValueError e non l'ha fatto")
        raise SystemExit(1)
    except ValueError as e:
        print(f"ValueError sollevato correttamente: {e}")

    print()
    print("Tutti i test completati.")
