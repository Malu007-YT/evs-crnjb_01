"""
EV Scanner AI - Shadow Intelligence System
Step 7/7 (parte B): utils/promotion_engine.py - Promotion Engine
----------------------------------------------------------------
Logica di validazione per promuovere manualmente uno Shadow Model a
nuovo Filtro Ufficiale (vedi progettazione, "Logica di Promozione in
Produzione"). Il candidato deve:
  1. avere un numero minimo di bet out-of-sample (default 1000, vedi
     ShadowSystemConfig.promotion_minimo_bet_out_of_sample)
  2. superare un test di significativita' statistica (p-value < soglia,
     default 0.05) sulla differenza di ROI rispetto al Filtro Ufficiale
  3. avere ROI e Sharpe Ratio superiori al Filtro Ufficiale corrente

Il test statistico usato e' uno Z-TEST A DUE CAMPIONI sulla differenza
tra le medie dei rendimenti per-bet (candidato vs produzione) - la
scelta standard quando si confrontano due campioni indipendenti di
dimensione sufficientemente grande (qui N>=1000 per costruzione, quindi
l'approssimazione normale della distribuzione campionaria della media,
garantita dal Teorema del Limite Centrale, e' pienamente giustificata;
un t-test sarebbe piu' indicato per campioni piccoli, che qui non si
presentano mai per via della soglia minima).

IMPORTANTE - onestà statistica: un p-value basso NON dimostra che il
modello candidato sia "davvero" migliore in un senso causale forte,
dimostra solo che la differenza osservata e' improbabile sotto l'ipotesi
nulla (nessuna vera differenza) dato IL CAMPIONE osservato. Rimangono
tutti i limiti standard di un test di ipotesi su dati storici di betting
(non stazionarieta' del mercato, drift dei bookmaker, multiple-testing se
si testano molti modelli/periodi in sequenza) - questo motore fornisce
uno strumento rigoroso di supporto alla decisione, non un oracolo.
----------------------------------------------------------------
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from utils.stats_engine import BetSettled, MetrichePerformance, calcola_metriche


# ==================================================================
# Z-test a due campioni indipendenti
# ==================================================================
@dataclass
class RisultatoTestStatistico:
    z_score: float
    p_value: float  # two-tailed
    differenza_media_rendimenti: float  # rendimento medio candidato - rendimento medio produzione (frazione di stake, non %)
    significativo: bool  # p_value < soglia configurata


def _rendimenti_frazionali(bets: List[BetSettled]) -> List[float]:
    """Rendimento per-bet come frazione dello stake (profit_loss/stake), stessa convenzione di stats_engine.calcola_metriche() per lo Sharpe."""
    return [b.profit_loss / b.stake for b in bets if b.stake > 0]


def _cdf_normale_standard(x: float) -> float:
    """
    CDF della normale standard, calcolata tramite la funzione erf della
    libreria standard math (nessuna dipendenza da scipy/numpy, per
    restare coerenti con lo stile "solo standard library" gia' visto in
    stats_engine.py). Identita' matematica standard:
        Phi(x) = 0.5 * (1 + erf(x / sqrt(2)))
    """
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def z_test_due_campioni(rendimenti_a: List[float], rendimenti_b: List[float], soglia_p_value: float) -> RisultatoTestStatistico:
    """
    Test Z a due campioni indipendenti sulla differenza delle medie.
    `rendimenti_a` = candidato (shadow), `rendimenti_b` = produzione
    (filtro ufficiale corrente). Formula standard (varianze NON assunte
    uguali - Welch, piu' robusto della versione pooled quando le
    varianze dei due campioni possono ragionevolmente differire, come
    tipicamente accade tra un modello sperimentale e uno consolidato):

        z = (media_a - media_b) / sqrt(var_a/n_a + var_b/n_b)

    p-value two-tailed: p = 2 * (1 - Phi(|z|))
    """
    n_a, n_b = len(rendimenti_a), len(rendimenti_b)
    if n_a < 2 or n_b < 2:
        raise ValueError("z_test_due_campioni richiede almeno 2 osservazioni per campione (per calcolare una varianza)")

    media_a = sum(rendimenti_a) / n_a
    media_b = sum(rendimenti_b) / n_b
    var_a = sum((r - media_a) ** 2 for r in rendimenti_a) / (n_a - 1)  # varianza campionaria (n-1), stesso standard di stats_engine
    var_b = sum((r - media_b) ** 2 for r in rendimenti_b) / (n_b - 1)

    errore_standard = math.sqrt(var_a / n_a + var_b / n_b)
    if errore_standard == 0:
        # Caso degenere: entrambi i campioni hanno varianza zero (tutti i
        # rendimenti identici in ciascun campione) - evita una divisione
        # per zero. Se le medie sono anche uguali, non c'e' differenza da
        # testare (z=0, p=1); se sono diverse nonostante varianza zero, e'
        # una situazione talmente anomala nei dati reali (mai osservata
        # con N>=1000 bet vere) che un z artificialmente enorme
        # sarebbe fuorviante piu' che informativo - si tratta comunque
        # come "non significativo" per prudenza, aspettando dati piu' sani.
        z = 0.0
    else:
        z = (media_a - media_b) / errore_standard

    p_value = round(2 * (1 - _cdf_normale_standard(abs(z))), 8)
    # Clamp esplicito: per |z| molto grandi, 1-Phi(|z|) puo' risultare in
    # un valore floating point leggermente negativo per errori di
    # arrotondamento di erf - va riportato a 0 invece di un p-value negativo,
    # che non avrebbe senso statistico.
    p_value = max(0.0, min(1.0, p_value))

    return RisultatoTestStatistico(
        z_score=round(z, 4), p_value=p_value,
        differenza_media_rendimenti=round(media_a - media_b, 6),
        significativo=p_value < soglia_p_value,
    )


# ==================================================================
# Promotion Engine
# ==================================================================
@dataclass
class EsitoPromozione:
    """
    Corrisponde 1:1 alle colonne di shadow_promotion_tests (vedi
    schema.sql) - pronto per un INSERT diretto una volta che il
    chiamante (main_shadow_engine.py, o uno script manuale lanciato da
    Malu) ha raccolto le due liste di bet.
    """
    esito: str  # 'promosso' | 'respinto_dati_insufficienti' | 'respinto_non_significativo' | 'respinto_metriche_inferiori'
    p_value: Optional[float]
    roi_candidato_pct: float
    roi_produzione_pct: float
    sharpe_candidato: float
    sharpe_produzione: float
    num_bet_out_of_sample: int
    dettaglio: dict  # breakdown completo per audit (shadow_promotion_tests.dettaglio_json)


def valuta_promozione(
    bet_candidato: List[BetSettled],
    bet_produzione: List[BetSettled],
    minimo_bet_out_of_sample: int,
    soglia_p_value: float,
) -> EsitoPromozione:
    """
    Applica in sequenza le 3 condizioni richieste dalla progettazione:

    1. Numero minimo di bet OUT-OF-SAMPLE del candidato (bet_produzione
       non e' soggetto allo stesso minimo: il Filtro Ufficiale ha quasi
       certamente gia' uno storico ben piu' ampio, essendo in produzione
       da piu' tempo - il vincolo di N minimo esiste per proteggere la
       decisione di promozione da un campione shadow troppo piccolo, non
       il contrario).
    2. Test di significativita' statistica sulla differenza di ROI.
    3. Metriche (ROI E Sharpe) del candidato superiori alla produzione -
       la significativita' statistica da sola non basta: un candidato
       potrebbe essere "significativamente diverso" ma PEGGIORE (z
       negativo, p-value comunque basso) - questo terzo controllo lo
       esclude esplicitamente, e va verificato ANCHE quando il test e'
       significativo, non solo come alternativa ad esso.

    Le condizioni sono verificate in un ordine che fa terminare la
    valutazione al primo fallimento (nessun costo computazionale sprecato
    a calcolare un test statistico se i dati sono gia' insufficienti).
    """
    num_bet_candidato = len(bet_candidato)

    if num_bet_candidato < minimo_bet_out_of_sample:
        return EsitoPromozione(
            esito="respinto_dati_insufficienti", p_value=None,
            roi_candidato_pct=0.0, roi_produzione_pct=0.0, sharpe_candidato=0.0, sharpe_produzione=0.0,
            num_bet_out_of_sample=num_bet_candidato,
            dettaglio={
                "motivo": f"bet out-of-sample del candidato ({num_bet_candidato}) sotto il minimo richiesto ({minimo_bet_out_of_sample})",
            },
        )

    metriche_candidato = calcola_metriche(bet_candidato)
    metriche_produzione = calcola_metriche(bet_produzione)

    rendimenti_candidato = _rendimenti_frazionali(bet_candidato)
    rendimenti_produzione = _rendimenti_frazionali(bet_produzione)

    if len(rendimenti_produzione) < 2:
        return EsitoPromozione(
            esito="respinto_dati_insufficienti", p_value=None,
            roi_candidato_pct=metriche_candidato.roi_pct, roi_produzione_pct=metriche_produzione.roi_pct,
            sharpe_candidato=metriche_candidato.sharpe_ratio or 0.0, sharpe_produzione=metriche_produzione.sharpe_ratio or 0.0,
            num_bet_out_of_sample=num_bet_candidato,
            dettaglio={"motivo": "storico di produzione insufficiente (< 2 bet) per un test statistico valido"},
        )

    test_statistico = z_test_due_campioni(rendimenti_candidato, rendimenti_produzione, soglia_p_value)

    dettaglio_base = {
        "z_score": test_statistico.z_score,
        "p_value": test_statistico.p_value,
        "differenza_media_rendimenti_frazionali": test_statistico.differenza_media_rendimenti,
        "num_bet_candidato": num_bet_candidato,
        "num_bet_produzione": len(bet_produzione),
    }

    if not test_statistico.significativo:
        return EsitoPromozione(
            esito="respinto_non_significativo", p_value=test_statistico.p_value,
            roi_candidato_pct=metriche_candidato.roi_pct, roi_produzione_pct=metriche_produzione.roi_pct,
            sharpe_candidato=metriche_candidato.sharpe_ratio or 0.0, sharpe_produzione=metriche_produzione.sharpe_ratio or 0.0,
            num_bet_out_of_sample=num_bet_candidato,
            dettaglio={**dettaglio_base, "motivo": f"p-value {test_statistico.p_value} non sotto la soglia {soglia_p_value}"},
        )

    roi_migliore = metriche_candidato.roi_pct > metriche_produzione.roi_pct
    sharpe_candidato_val = metriche_candidato.sharpe_ratio if metriche_candidato.sharpe_ratio is not None else float("-inf")
    sharpe_produzione_val = metriche_produzione.sharpe_ratio if metriche_produzione.sharpe_ratio is not None else float("-inf")
    sharpe_migliore = sharpe_candidato_val > sharpe_produzione_val

    if not (roi_migliore and sharpe_migliore):
        return EsitoPromozione(
            esito="respinto_metriche_inferiori", p_value=test_statistico.p_value,
            roi_candidato_pct=metriche_candidato.roi_pct, roi_produzione_pct=metriche_produzione.roi_pct,
            sharpe_candidato=metriche_candidato.sharpe_ratio or 0.0, sharpe_produzione=metriche_produzione.sharpe_ratio or 0.0,
            num_bet_out_of_sample=num_bet_candidato,
            dettaglio={
                **dettaglio_base,
                "motivo": (
                    "differenza statisticamente significativa, ma il candidato non supera la produzione "
                    f"su ENTRAMBE le metriche richieste (ROI migliore={roi_migliore}, Sharpe migliore={sharpe_migliore})"
                ),
            },
        )

    return EsitoPromozione(
        esito="promosso", p_value=test_statistico.p_value,
        roi_candidato_pct=metriche_candidato.roi_pct, roi_produzione_pct=metriche_produzione.roi_pct,
        sharpe_candidato=metriche_candidato.sharpe_ratio or 0.0, sharpe_produzione=metriche_produzione.sharpe_ratio or 0.0,
        num_bet_out_of_sample=num_bet_candidato,
        dettaglio={**dettaglio_base, "motivo": "tutte le condizioni soddisfatte: dati sufficienti, differenza significativa, ROI e Sharpe superiori"},
    )


# ==================================================================
# Calibrazione probabilita' (Platt Scaling) - requisito "Sviluppo Avanzato"
# ==================================================================
# Non e' usata direttamente dal Promotion Engine, ma e' collocata in
# questo modulo perche' concettualmente appartiene alla stessa categoria
# di "utility statistiche di supporto alla decisione" - la progettazione
# la richiede esplicitamente nella sezione "Requisito di Sviluppo
# Avanzato" come tecnica per calibrare le probabilita' stimate dei
# bookmaker. Platt Scaling (invece di Isotonic Regression) e' scelto
# perche' e' parametrico (2 soli parametri A,B) e quindi allenabile con
# lo stesso stile "SGD-friendly" gia' visto in Model C, mentre
# l'Isotonic Regression richiederebbe una struttura dati piu' complessa
# (funzione a gradini monotona) fuori scope per questo step; se in
# futuro servisse una calibrazione non-parametrica, va aggiunta come
# funzione indipendente qui accanto, non in sostituzione di questa.
@dataclass
class ModelloCalibrazionePlatt:
    """
    p_calibrata = sigmoid(A * logit(p_grezza) + B)

    Un modello di calibrazione Platt e' semplicemente una regressione
    logistica 1D allenata sui LOGIT delle probabilita' grezze (qui: le
    probabilita' implicite dalle quote bookmaker) contro gli esiti reali
    osservati - la stessa idea del sigmoid(w.z+b) di Model C, ma con una
    sola feature di input (il logit della probabilita' grezza) invece di
    10.
    """
    A: float = 1.0  # inizializzazione neutra: A=1, B=0 equivale a NON calibrare affatto
    B: float = 0.0

    def calibra(self, probabilita_grezza_pct: float) -> float:
        p = max(1e-9, min(1 - 1e-9, probabilita_grezza_pct / 100.0))
        logit_p = math.log(p / (1 - p))
        logit_calibrato = self.A * logit_p + self.B
        return _sigmoid_locale(logit_calibrato) * 100.0


def _sigmoid_locale(x: float) -> float:
    """Stessa implementazione numericamente stabile di model_c_adaptive._sigmoid, duplicata qui per evitare un import incrociato tra utils/ e models/ (utils non deve dipendere da models/)."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    return math.exp(x) / (1.0 + math.exp(x))


def allena_platt_scaling(
    probabilita_grezze_pct: List[float],
    esiti: List[int],
    learning_rate: float = 0.01,
    iterazioni: int = 200,
) -> ModelloCalibrazionePlatt:
    """
    Allena A e B via gradient descent a batch pieno (non online come
    Model C: la calibrazione Platt e' tipicamente ri-allenata
    periodicamente su un batch di storico, non aggiornata bet-per-bet -
    non ha lo stesso bisogno di reattivita' immediata di Model C, che
    deve adattarsi rapidamente perche' guida decisioni di piazzamento
    in tempo reale).
    """
    if len(probabilita_grezze_pct) != len(esiti):
        raise ValueError("probabilita_grezze_pct ed esiti devono avere la stessa lunghezza")
    if len(esiti) < 10:
        raise ValueError("allena_platt_scaling richiede almeno 10 osservazioni per una calibrazione sensata")

    logit_grezzi = []
    for p_pct in probabilita_grezze_pct:
        p = max(1e-9, min(1 - 1e-9, p_pct / 100.0))
        logit_grezzi.append(math.log(p / (1 - p)))

    A, B = 1.0, 0.0
    n = len(esiti)
    for _ in range(iterazioni):
        grad_A, grad_B = 0.0, 0.0
        for logit_p, y in zip(logit_grezzi, esiti):
            p_hat = _sigmoid_locale(A * logit_p + B)
            errore = p_hat - y
            grad_A += errore * logit_p
            grad_B += errore
        A -= learning_rate * (grad_A / n)
        B -= learning_rate * (grad_B / n)

    return ModelloCalibrazionePlatt(A=round(A, 6), B=round(B, 6))


# ==================================================================
# Self-test manuale rapido (python3 utils/promotion_engine.py)
# ==================================================================
if __name__ == "__main__":
    import random
    from datetime import date, timedelta

    oggi = date(2026, 7, 15)

    print("=" * 70)
    print("TEST 1: z_test_due_campioni() - verifica con valori noti (SciPy-equivalenti)")
    print("=" * 70)
    # Due campioni con media e varianza semplici, calcolabili a mano.
    campione_a = [0.10, 0.12, 0.08, 0.11, 0.09, 0.13, 0.07, 0.10, 0.12, 0.09] * 20  # media~0.101, n=200
    campione_b = [0.01, -0.02, 0.03, 0.00, -0.01, 0.02, 0.01, -0.03, 0.02, 0.00] * 20  # media~0.003, n=200
    risultato_test = z_test_due_campioni(campione_a, campione_b, soglia_p_value=0.05)
    print(f"z={risultato_test.z_score}  p={risultato_test.p_value}  differenza_media={risultato_test.differenza_media_rendimenti}  significativo={risultato_test.significativo}")

    # Verifica manuale indipendente
    n_a, n_b = len(campione_a), len(campione_b)
    media_a_manuale = sum(campione_a) / n_a
    media_b_manuale = sum(campione_b) / n_b
    var_a_manuale = sum((x - media_a_manuale) ** 2 for x in campione_a) / (n_a - 1)
    var_b_manuale = sum((x - media_b_manuale) ** 2 for x in campione_b) / (n_b - 1)
    se_manuale = math.sqrt(var_a_manuale / n_a + var_b_manuale / n_b)
    z_manuale = (media_a_manuale - media_b_manuale) / se_manuale
    print(f"[verifica indipendente] z atteso={z_manuale:.4f}  (match={abs(z_manuale - risultato_test.z_score) < 0.001})")
    assert abs(z_manuale - risultato_test.z_score) < 0.001
    assert risultato_test.significativo, "con una differenza cosi' marcata e n=200 per campione, il test deve risultare significativo"

    print()
    print("=" * 70)
    print("TEST 2: p-value noto da tabella statistica standard (z=1.96 -> p~0.05)")
    print("=" * 70)
    # Costruzione diretta: due campioni la cui differenza produce z=1.96 esatto e' complessa
    # da ottenere per costruzione discreta, quindi verifichiamo la funzione CDF/p-value
    # direttamente contro valori noti da tabella statistica standard.
    p_per_z_196 = round(2 * (1 - _cdf_normale_standard(1.96)), 4)
    p_per_z_0 = round(2 * (1 - _cdf_normale_standard(0.0)), 4)
    p_per_z_258 = round(2 * (1 - _cdf_normale_standard(2.576)), 4)
    print(f"p-value per z=1.96:  {p_per_z_196}  (atteso ~0.05, valore da tabella standard)")
    print(f"p-value per z=0.0:   {p_per_z_0}  (atteso 1.0, nessuna differenza)")
    print(f"p-value per z=2.576: {p_per_z_258}  (atteso ~0.01, valore da tabella standard)")
    assert abs(p_per_z_196 - 0.05) < 0.001
    assert abs(p_per_z_0 - 1.0) < 0.001
    assert abs(p_per_z_258 - 0.01) < 0.001

    print()
    print("=" * 70)
    print("TEST 3: valuta_promozione() - scarto per dati insufficienti")
    print("=" * 70)
    poche_bet_candidato = [BetSettled(oggi, 50, 10, "vinta") for _ in range(100)]  # sotto 1000
    molte_bet_produzione = [BetSettled(oggi, 50, 5, "vinta") for _ in range(2000)]
    esito1 = valuta_promozione(poche_bet_candidato, molte_bet_produzione, minimo_bet_out_of_sample=1000, soglia_p_value=0.05)
    print(f"esito={esito1.esito}  dettaglio={esito1.dettaglio}")
    assert esito1.esito == "respinto_dati_insufficienti"

    print()
    print("=" * 70)
    print("TEST 4: valuta_promozione() - candidato chiaramente migliore -> promosso")
    print("=" * 70)
    rng = random.Random(1)

    def genera_bet_shadow(n, stake, prob_vittoria, quota):
        risultato = []
        for i in range(n):
            vince = rng.random() < prob_vittoria
            pl = round(stake * (quota - 1), 2) if vince else -stake
            risultato.append(BetSettled(oggi - timedelta(days=n - i), stake, pl, "vinta" if vince else "persa"))
        return risultato

    bet_candidato_forte = genera_bet_shadow(1200, 50, prob_vittoria=0.58, quota=2.00)  # EV chiaramente positivo
    bet_produzione_debole = genera_bet_shadow(3000, 50, prob_vittoria=0.50, quota=2.00)  # EV neutro

    esito2 = valuta_promozione(bet_candidato_forte, bet_produzione_debole, minimo_bet_out_of_sample=1000, soglia_p_value=0.05)
    print(f"esito={esito2.esito}")
    print(f"ROI candidato={esito2.roi_candidato_pct}%  ROI produzione={esito2.roi_produzione_pct}%")
    print(f"Sharpe candidato={esito2.sharpe_candidato}  Sharpe produzione={esito2.sharpe_produzione}")
    print(f"p-value={esito2.p_value}")
    assert esito2.esito == "promosso", "con un vantaggio cosi' netto (58% vs 50% win rate, stessa quota) e N>1000, il candidato deve essere promosso"

    print()
    print("=" * 70)
    print("TEST 5: valuta_promozione() - differenza NON significativa -> respinto")
    print("=" * 70)
    bet_candidato_pari = genera_bet_shadow(1200, 50, prob_vittoria=0.501, quota=2.00)
    bet_produzione_pari = genera_bet_shadow(1200, 50, prob_vittoria=0.499, quota=2.00)
    esito3 = valuta_promozione(bet_candidato_pari, bet_produzione_pari, minimo_bet_out_of_sample=1000, soglia_p_value=0.05)
    print(f"esito={esito3.esito}  p-value={esito3.p_value}")
    assert esito3.esito == "respinto_non_significativo", "una differenza dello 0.2% di win rate su questi campioni non deve essere statisticamente significativa"

    print()
    print("=" * 70)
    print("TEST 6: valuta_promozione() - significativo ma candidato PEGGIORE -> respinto_metriche_inferiori")
    print("=" * 70)
    bet_candidato_scarso = genera_bet_shadow(1200, 50, prob_vittoria=0.42, quota=2.00)  # chiaramente sotto EV
    bet_produzione_buona = genera_bet_shadow(3000, 50, prob_vittoria=0.55, quota=2.00)
    esito4 = valuta_promozione(bet_candidato_scarso, bet_produzione_buona, minimo_bet_out_of_sample=1000, soglia_p_value=0.05)
    print(f"esito={esito4.esito}  ROI candidato={esito4.roi_candidato_pct}%  ROI produzione={esito4.roi_produzione_pct}%")
    assert esito4.esito == "respinto_metriche_inferiori", "un candidato con win rate 42%% vs quota 2.00 (EV negativo) non deve mai essere promosso, anche se la differenza e' significativa"

    print()
    print("=" * 70)
    print("TEST 7: Platt Scaling - il modello di calibrazione impara a correggere un bias sistematico")
    print("=" * 70)
    # Dataset sintetico: le probabilita' "grezze" sono sistematicamente
    # SOVRASTIMATE del 15% rispetto alla vera probabilita' di vittoria -
    # dopo la calibrazione, le probabilita' calibrate devono avvicinarsi
    # alla vera frequenza di vittoria osservata.
    rng2 = random.Random(5)
    prob_grezze = []
    esiti_calibrazione = []
    for _ in range(500):
        prob_vera = rng2.uniform(0.20, 0.80)
        prob_grezza_sovrastimata = min(99.0, (prob_vera + 0.15) * 100)
        vince = 1 if rng2.random() < prob_vera else 0
        prob_grezze.append(prob_grezza_sovrastimata)
        esiti_calibrazione.append(vince)

    modello_platt = allena_platt_scaling(prob_grezze, esiti_calibrazione, learning_rate=0.1, iterazioni=300)
    print(f"Parametri appresi: A={modello_platt.A}  B={modello_platt.B}")

    # Verifica: su un campione di probabilita' grezze note, la media
    # calibrata deve essere piu' vicina alla vera frequenza di vittoria
    # osservata rispetto alla media grezza (non calibrata).
    media_grezza = sum(prob_grezze) / len(prob_grezze)
    media_calibrata = sum(modello_platt.calibra(p) for p in prob_grezze) / len(prob_grezze)
    media_vera_frequenza = sum(esiti_calibrazione) / len(esiti_calibrazione) * 100
    print(f"Media probabilita' grezza (sovrastimata): {media_grezza:.2f}%")
    print(f"Media probabilita' calibrata:              {media_calibrata:.2f}%")
    print(f"Vera frequenza di vittoria osservata:       {media_vera_frequenza:.2f}%")
    distanza_grezza = abs(media_grezza - media_vera_frequenza)
    distanza_calibrata = abs(media_calibrata - media_vera_frequenza)
    print(f"Distanza grezza dalla verita': {distanza_grezza:.2f}  |  Distanza calibrata dalla verita': {distanza_calibrata:.2f}")
    assert distanza_calibrata < distanza_grezza, "la calibrazione Platt deve ridurre la distanza dalla vera frequenza di vittoria, non aumentarla"

    print()
    print("=" * 70)
    print("TEST 8: robustezza - varianza zero in entrambi i campioni non causa crash")
    print("=" * 70)
    campione_costante_a = [0.05] * 50
    campione_costante_b = [0.05] * 50
    risultato_degenere = z_test_due_campioni(campione_costante_a, campione_costante_b, soglia_p_value=0.05)
    print(f"z={risultato_degenere.z_score}  p={risultato_degenere.p_value}  significativo={risultato_degenere.significativo} (atteso: nessun crash, z=0, non significativo)")
    assert risultato_degenere.z_score == 0.0
    assert not risultato_degenere.significativo

    print()
    print("Tutti i test completati.")
