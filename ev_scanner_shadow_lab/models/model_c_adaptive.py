"""
EV Scanner AI - Shadow Intelligence System
Step 6/7: models/model_c_adaptive.py - Shadow Model C (Adaptive)
----------------------------------------------------------------
Questo e' il modello piu' delicato del progetto (vedi discussione
iniziale): a differenza di A/B/D, che sono combinazioni deterministiche
di regole/medie, qui c'e' vera matematica di machine learning online che
deve essere corretta, non solo plausibile. Ho scelto l'algoritmo piu'
semplice che soddisfa la progettazione (online logistic regression con
SGD) invece di qualcosa di piu' sofisticato ma piu' difficile da
validare - un modello lineare online e' interamenteispezionabile: ogni
peso ha un segno interpretabile, ogni predizione e' riproducibile a
mano con carta e penna, e la Feature Importance richiesta dalla
progettazione (sezione "Requisito di Sviluppo Avanzato") e' semplicemente
il vettore dei pesi stesso, senza bisogno di tecniche di post-hoc
explainability piu' complesse (SHAP, permutation importance) che
sarebbero over-engineering per un modello lineare.

----------------------------------------------------------------
FORMULAZIONE MATEMATICA
----------------------------------------------------------------

1. NORMALIZZAZIONE FEATURE (online, incrementale)
   ------------------------------------------------
   Ogni feature x_i viene standardizzata a z-score PRIMA di entrare nel
   modello: z_i = (x_i - mu_i) / sigma_i

   mu_i e sigma_i sono la media e deviazione standard CORRENTI di quella
   feature, calcolate incrementalmente con l'algoritmo di Welford (1962)
   per stabilita' numerica (evita di dover ricalcolare la varianza da
   zero su tutto lo storico ad ogni update, e non soffre di cancellazione
   catastrofica come la formula "naive" E[x^2] - E[x]^2):

       n_i     <- n_i + 1
       delta   <- x_i - mu_i
       mu_i    <- mu_i + delta / n_i
       delta2  <- x_i - mu_i
       M2_i    <- M2_i + delta * delta2
       sigma_i <- sqrt(M2_i / n_i)   (varianza di popolazione, n al denominatore)

   Se sigma_i == 0 (tutte le osservazioni finora identiche, tipico nei
   primissimi update), si usa sigma_i=1 per evitare una divisione per
   zero - equivale a non normalizzare quella feature finche' non c'e'
   abbastanza varianza osservata per farlo in modo sensato.

2. MODELLO: REGRESSIONE LOGISTICA
   --------------------------------
   p_vittoria = sigmoid(w . z + b) = 1 / (1 + exp(-(w . z + b)))

   dove w e' il vettore dei pesi (uno per feature, vedi feature_names in
   config.py), z il vettore delle feature normalizzate, b il bias.

3. AGGIORNAMENTO SGD CON REGOLARIZZAZIONE L2 (Ridge)
   ----------------------------------------------------
   Ad ogni bet settled disponibile per l'update (raggruppate in batch di
   aggiorna_ogni_n_bet_concluse, vedi config.py), per ciascun esempio
   (z, y) dove y in {0,1} e' l'esito reale (1=vinta, 0=persa; le bet
   'void' sono escluse dal training, vedi sotto):

       p_hat = sigmoid(w . z + b)
       errore = p_hat - y                          (gradiente della log-loss rispetto a w.z+b)

       gradiente_w = errore * z + lambda_l2 * w     (termine L2 aggiunto al gradiente pesi, MAI al bias)
       gradiente_b = errore

       w <- w - eta_t * gradiente_w
       b <- b - eta_t * gradiente_b

   Learning rate con decadimento 1/sqrt(t) (standard per SGD online, vedi
   config.py):
       eta_t = learning_rate_iniziale / sqrt(1 + t)
   dove t e' il contatore progressivo di AGGIORNAMENTI (batch), non di
   singole bet - cresce di 1 ad ogni chiamata ad aggiorna_pesi(), non ad
   ogni esempio dentro il batch (altrimenti un batch grande farebbe
   decadere il learning rate troppo in fretta rispetto a uno piccolo).

4. LOG-LOSS (per monitoraggio, salvata in shadow_model_c_weights.log_loss)
   -------------------------------------------------------------------------
   log_loss = -(1/N) * sum( y*log(p_hat) + (1-y)*log(1-p_hat) )
   con clipping di p_hat in [epsilon, 1-epsilon] per evitare log(0).

----------------------------------------------------------------
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from config import ModelCConfig
from models.model_a_conservative import EventoInput, ValutazioneShadow, calcola_kelly_full


EPSILON_LOG_LOSS = 1e-9  # clipping per evitare log(0) nella log-loss


# ==================================================================
# Normalizzazione online (Welford)
# ==================================================================
@dataclass
class NormalizzatoreOnline:
    """
    Uno stato di normalizzazione Welford PER FEATURE. L'intero
    NormalizzatoreOnline e' la collezione di questi stati per tutte le
    feature del modello (vedi StatoModelCAdaptive.normalizzatori sotto).

    Serializzabile 1:1 in shadow_model_c_weights.normalization_params_json
    (vedi to_dict/from_dict) - necessario per poter riapplicare
    correttamente i pesi in modo retrospettivo (stessa normalizzazione
    usata al momento del training, non quella corrente che sara' diversa
    dopo altri update).
    """
    n: int = 0
    media: float = 0.0
    m2: float = 0.0  # accumulatore Welford per la varianza

    @property
    def varianza(self) -> float:
        return self.m2 / self.n if self.n > 0 else 0.0

    @property
    def deviazione_standard(self) -> float:
        return math.sqrt(self.varianza)

    def aggiorna(self, x: float) -> None:
        """Un singolo step incrementale Welford su una nuova osservazione x."""
        self.n += 1
        delta = x - self.media
        self.media += delta / self.n
        delta2 = x - self.media
        self.m2 += delta * delta2

    def normalizza(self, x: float) -> float:
        """z-score di x secondo lo stato corrente. Vedi nota su sigma=0 nella docstring del modulo."""
        sigma = self.deviazione_standard
        if sigma == 0.0:
            return 0.0  # nessuna varianza osservata: la feature non discrimina ancora nulla, contributo neutro
        return (x - self.media) / sigma

    def to_dict(self) -> dict:
        return {"n": self.n, "media": self.media, "m2": self.m2}

    @staticmethod
    def from_dict(d: dict) -> "NormalizzatoreOnline":
        return NormalizzatoreOnline(n=d["n"], media=d["media"], m2=d["m2"])


# ==================================================================
# Stato completo del modello (pesi + normalizzatori + contatori)
# ==================================================================
@dataclass
class StatoModelCAdaptive:
    """
    Tutto cio' che serve per fare inferenza E per continuare il training
    da dove si era interrotto. Un'istanza di questo oggetto corrisponde
    1:1 a una riga di shadow_model_c_weights (weights_json +
    normalization_params_json + bet_concluse_totali_al_momento), piu' il
    contatore t di aggiornamenti SGD necessario per lo schedule 1/sqrt(t)
    (non persistito esplicitamente nello schema DB attuale, ma
    ricostruibile come bet_concluse_totali_al_momento //
    aggiorna_ogni_n_bet_concluse - vedi nota in aggiorna_pesi()).
    """
    feature_names: Tuple[str, ...]
    pesi: Dict[str, float]
    bias: float
    normalizzatori: Dict[str, NormalizzatoreOnline]
    bet_concluse_totali: int = 0
    numero_aggiornamenti_t: int = 0

    def to_weights_dict(self) -> dict:
        return dict(self.pesi)  # ordine feature_names garantito dal caller quando serve un vettore posizionale

    def to_normalization_dict(self) -> dict:
        return {nome: norm.to_dict() for nome, norm in self.normalizzatori.items()}

    @staticmethod
    def bootstrap(cfg: ModelCConfig) -> "StatoModelCAdaptive":
        """
        Stato iniziale prima di qualunque bet settled: pesi di bootstrap
        euristico da config.py, normalizzatori vuoti (n=0 su tutte le
        feature - la prima normalizzazione di ciascuna produrra' 0.0,
        vedi NormalizzatoreOnline.normalizza, finche' non si accumula
        varianza osservata).
        """
        pesi = dict(zip(cfg.feature_names, cfg.bootstrap_weights))
        normalizzatori = {nome: NormalizzatoreOnline() for nome in cfg.feature_names}
        return StatoModelCAdaptive(
            feature_names=cfg.feature_names, pesi=pesi, bias=cfg.bootstrap_bias,
            normalizzatori=normalizzatori, bet_concluse_totali=0, numero_aggiornamenti_t=0,
        )


# ==================================================================
# Estrazione feature da un evento
# ==================================================================
@dataclass
class EventoInputEsteso(EventoInput):
    """
    Estende EventoInput (definito in model_a_conservative.py) con i
    campi aggiuntivi che Model C usa e che A/B non usano: smart filter
    score gia' calcolato in produzione (Filtro Smart v7) e timestamp
    dell'evento per l'encoding ciclico orario/giorno settimana.

    Riusare EventoInput invece di duplicarlo da zero mantiene coerenza
    con A/B/D per i campi comuni (event_id, mercato, selection, ecc.) -
    stesso principio di riuso gia' visto in model_d_ensemble.py.
    """
    smart_filter_score: float = 0.5  # 0-1, gia' calcolato dal Filtro Smart v7 di produzione; 0.5 = neutro se non disponibile
    orario_evento: Optional[datetime] = None  # per encoding ciclico; se None, encoding orario/giorno = 0.0 (nessun segnale temporale)


def estrai_feature(evento: EventoInputEsteso, cfg: ModelCConfig) -> Dict[str, float]:
    """
    Costruisce il dict {nome_feature: valore_grezzo} nell'ordine e con i
    nomi esatti di cfg.feature_names. L'ordine e la presenza di TUTTE le
    feature qui e' un contratto rigido con StatoModelCAdaptive: un nome
    mancante o in piu' rispetto a cfg.feature_names fa fallire
    _vettorizza() esplicitamente (vedi sotto), invece di silenziosamente
    ignorare o azzerare una feature - un bug di questo tipo altererebbe
    la predizione senza errore visibile, il rischio peggiore possibile
    per un modello che decide quali bet piazzare.

    prob_stimata_vs_implicita_delta: differenza tra la probabilita'
    stimata dal proprio modello (evento.probability_pct) e la
    probabilita' implicita nella quota di mercato (1/quota) - un valore
    positivo alto indica che si pensa il mercato stia sottovalutando la
    selezione, il cuore stesso del value betting, quindi e' una feature
    particolarmente informativa da lasciare che il modello pesi da solo.

    Encoding ciclico ora/giorno: si usa seno+coseno invece del numero
    grezzo (es. ora=23) perche' un modello lineare interpreterebbe
    "ora=23" e "ora=0" come agli antipodi, quando invece sono adiacenti
    nel tempo (23:59 e 00:01 sono a 2 minuti di distanza, non a 23 ore) -
    l'encoding ciclico preserva questa adiacenza.
    """
    kelly_teorica = calcola_kelly_full(evento.probability_pct, evento.bookmaker_odds)
    prob_implicita = (1.0 / evento.bookmaker_odds) * 100.0 if evento.bookmaker_odds > 0 else 0.0
    delta_prob = evento.probability_pct - prob_implicita

    if evento.orario_evento is not None:
        ora_frazionaria = evento.orario_evento.hour + evento.orario_evento.minute / 60.0
        angolo_ora = 2 * math.pi * (ora_frazionaria / 24.0)
        ora_sin, ora_cos = math.sin(angolo_ora), math.cos(angolo_ora)

        giorno_settimana = evento.orario_evento.weekday()  # 0=lunedi ... 6=domenica
        angolo_giorno = 2 * math.pi * (giorno_settimana / 7.0)
        giorno_sin, giorno_cos = math.sin(angolo_giorno), math.cos(angolo_giorno)
    else:
        ora_sin = ora_cos = giorno_sin = giorno_cos = 0.0

    feature = {
        "ev_pct": calcola_ev_locale(evento.probability_pct, evento.bookmaker_odds),
        "kelly_fraction_teorica": kelly_teorica,
        "quota_bookmaker": evento.bookmaker_odds,
        "clv_stimato_pct": evento.clv_stimato_pct if evento.clv_stimato_pct is not None else 0.0,
        "smart_filter_score": evento.smart_filter_score,
        "prob_stimata_vs_implicita_delta": delta_prob,
        "ora_del_giorno_sin": ora_sin,
        "ora_del_giorno_cos": ora_cos,
        "giorno_settimana_sin": giorno_sin,
        "giorno_settimana_cos": giorno_cos,
    }

    # Sanity check: il contratto con cfg.feature_names deve essere
    # rispettato esattamente (stesso insieme di chiavi, vedi docstring).
    if set(feature.keys()) != set(cfg.feature_names):
        raise ValueError(
            f"estrai_feature() ha prodotto chiavi {sorted(feature.keys())} "
            f"ma cfg.feature_names attende {sorted(cfg.feature_names)}. "
            "Le due liste devono restare sincronizzate manualmente (vedi commento in config.py)."
        )

    return feature


def calcola_ev_locale(probability_pct: float, bookmaker_odds: float) -> float:
    """Stessa formula di model_a_conservative.calcola_ev(), duplicata qui SOLO per evitare un import circolare
    (model_a_conservative non deve dipendere da model_c_adaptive). Se in futuro le formule core EV/Kelly
    vengono spostate in un modulo condiviso (models/common.py o utils/formulas.py), questa duplicazione va
    eliminata a favore di un singolo import - annotato qui per non perderlo di vista."""
    p = probability_pct / 100.0
    return round((p * bookmaker_odds - 1) * 100, 3)


# ==================================================================
# Training example (per l'update SGD)
# ==================================================================
@dataclass
class EsempioTraining:
    """
    Una singola bet shadow di Model C gia' settled, pronta per un update
    SGD. `feature_grezze` e' il dict prodotto da estrai_feature() al
    MOMENTO in cui la bet fu generata (va quindi essere quello salvato
    in shadow_bets.features_snapshot_json, NON ricalcolato oggi con dati
    di mercato che nel frattempo sono cambiati - altrimenti si
    allenerebbe il modello su feature che non corrispondono a cio' che
    sapeva davvero in quel momento, un tipo di data leakage temporale).
    """
    feature_grezze: Dict[str, float]
    esito: int  # 1 = vinta, 0 = persa. Le bet 'void' NON devono generare un EsempioTraining (vedi filtro nel chiamante)


# ==================================================================
# Shadow Model C - Adaptive
# ==================================================================
class ModelCAdaptive:
    """
    Uso tipico da main_shadow_engine.py:

        stato = StatoModelCAdaptive.bootstrap(cfg.model_c)  # oppure ricostruito da un record shadow_model_c_weights
        model = ModelCAdaptive(cfg.model_c, stato)

        # --- inferenza ---
        for evento in eventi_del_batch:
            valutazione = model.valuta(evento)
            if valutazione.accepted:
                # costruisci la riga shadow_bets con model_source='model_c',
                # salvando ANCHE features_snapshot_json per il training futuro
                ...

        # --- training periodico, ogni aggiorna_ogni_n_bet_concluse bet settled ---
        nuovo_stato, diagnostica = model.aggiorna_pesi(esempi_training_batch)
        # nuovo_stato va persistito come nuova riga in shadow_model_c_weights
        # (mai un UPDATE, vedi commento nello schema.sql)
    """

    def __init__(self, cfg: ModelCConfig, stato: StatoModelCAdaptive):
        cfg.validate()
        if stato.feature_names != cfg.feature_names:
            raise ValueError(
                "Lo stato del modello e' stato costruito con un ordine/insieme di feature "
                "diverso da quello in config.py attuale: i pesi non sono piu' validi per "
                "questa configurazione (vedi commento su feature_names in config.py). "
                "Serve un nuovo bootstrap, non un caricamento diretto di questo stato."
            )
        self.cfg = cfg
        self.stato = stato

    # ------------------------------------------------------------
    # Inferenza
    # ------------------------------------------------------------
    def _vettorizza_normalizzato(self, feature_grezze: Dict[str, float]) -> List[float]:
        """Converte il dict di feature grezze in un vettore z-score, nell'ordine di feature_names."""
        if set(feature_grezze.keys()) != set(self.stato.feature_names):
            raise ValueError(
                f"feature_grezze ha chiavi {sorted(feature_grezze.keys())}, attese {sorted(self.stato.feature_names)}"
            )
        return [
            self.stato.normalizzatori[nome].normalizza(feature_grezze[nome])
            for nome in self.stato.feature_names
        ]

    def _predici_probabilita(self, feature_grezze: Dict[str, float]) -> float:
        z = self._vettorizza_normalizzato(feature_grezze)
        pesi_vettore = [self.stato.pesi[nome] for nome in self.stato.feature_names]
        logit = sum(w * zi for w, zi in zip(pesi_vettore, z)) + self.stato.bias
        return _sigmoid(logit)

    def valuta(self, evento: EventoInputEsteso) -> ValutazioneShadow:
        cfg = self.cfg

        if evento.bookmaker_odds <= 1.0:
            return ValutazioneShadow(accepted=False, motivo_scarto="quota bookmaker non valida (<= 1.0)")

        feature_grezze = estrai_feature(evento, cfg)
        probabilita_stimata_modello = self._predici_probabilita(feature_grezze)

        in_fase_bootstrap = self.stato.bet_concluse_totali < cfg.minimo_bet_per_pesi_appresi

        if probabilita_stimata_modello < cfg.soglia_probabilita_output:
            fase = "bootstrap" if in_fase_bootstrap else "appresa"
            return ValutazioneShadow(
                accepted=False,
                motivo_scarto=(
                    f"probabilita' stimata dal modello ({probabilita_stimata_modello:.4f}, fase {fase}) "
                    f"sotto soglia {cfg.soglia_probabilita_output}"
                ),
            )

        ev_pct = feature_grezze["ev_pct"]
        kelly_full = calcola_kelly_full(evento.probability_pct, evento.bookmaker_odds)
        # Kelly di Model C scalato dalla confidenza del modello stesso
        # (probabilita' di output, non la probabilita' di mercato usata
        # per il Kelly "teorico"): un modello piu' sicuro di se' investe
        # una frazione piu' vicina al Kelly pieno, coerente con lo spirito
        # "quantitativo dinamico" della progettazione - a differenza di A/B
        # che usano una kelly_fraction fissa indipendente dalla confidenza.
        kelly_fraction_effettiva = round(
            kelly_full * probabilita_stimata_modello, 6
        )

        return ValutazioneShadow(
            accepted=True,
            ev_pct=ev_pct,
            ev_penalizzato_pct=ev_pct,  # Model C non applica una penalizzazione esplicita come A: la "prudenza" e' gia' incorporata nel training sui dati storici
            kelly_fraction_usata=round(probabilita_stimata_modello, 4),  # qui il campo riflette la confidenza del modello, non una frazione fissa di config - per trasparenza in shadow_bets
            kelly_stake_frazione=kelly_fraction_effettiva,
            confidence_score=round(probabilita_stimata_modello * 100, 2),
        )

    # ------------------------------------------------------------
    # Training (SGD online)
    # ------------------------------------------------------------
    def aggiorna_pesi(self, esempi: List[EsempioTraining]) -> Tuple[StatoModelCAdaptive, "DiagnosticaTraining"]:
        """
        Esegue UN batch di update SGD (uno per esempio, in ordine, sullo
        stesso stato che si evolve esempio-dopo-esempio all'interno del
        batch - non e' un batch gradient descent con media dei gradienti,
        e' SGD "vero": ogni esempio aggiorna subito i pesi che il
        prossimo esempio nello stesso batch vedra' gia' aggiornati).

        Il learning rate eta_t usa t = stato.numero_aggiornamenti_t + 1
        per l'INTERO batch (non uno per esempio dentro il batch): la
        chiamata a questo metodo rappresenta "un aggiornamento" nello
        schedule 1/sqrt(t) descritto nella progettazione, coerente con
        aggiorna_ogni_n_bet_concluse che conta bet-per-batch, non
        bet-singole.

        Ritorna il NUOVO stato (il chiamante e' responsabile di
        persisterlo come nuova riga shadow_model_c_weights) piu' una
        diagnostica con log_loss e accuracy per il monitoraggio.
        """
        if not esempi:
            raise ValueError("aggiorna_pesi() chiamato con lista esempi vuota: nulla da apprendere")

        cfg = self.cfg
        # Copia profonda dello stato: non mutiamo self.stato in place,
        # per lasciare che il chiamante decida esplicitamente se e quando
        # sostituire lo stato corrente (es. potrebbe voler tenere il
        # vecchio stato attivo finche' il nuovo non e' stato validato).
        nuovi_pesi = dict(self.stato.pesi)
        nuovo_bias = self.stato.bias
        nuovi_normalizzatori = {
            nome: NormalizzatoreOnline(n=norm.n, media=norm.media, m2=norm.m2)
            for nome, norm in self.stato.normalizzatori.items()
        }

        nuovo_t = self.stato.numero_aggiornamenti_t + 1
        eta_t = cfg.learning_rate_iniziale / math.sqrt(1 + nuovo_t)

        # --- Diagnostica: calcolata SUL MODELLO PRIMA di questo update,
        # cosi' log_loss/accuracy riflettono "quanto bene il modello
        # prediceva questi esempi prima di vederli", una metrica di
        # generalizzazione onesta (non contaminata dal fatto di aver
        # appena visto la risposta giusta).
        predizioni_pre_update = []
        for esempio in esempi:
            z = [
                nuovi_normalizzatori[nome].normalizza(esempio.feature_grezze[nome])
                for nome in self.stato.feature_names
            ]
            pesi_vec = [nuovi_pesi[nome] for nome in self.stato.feature_names]
            logit = sum(w * zi for w, zi in zip(pesi_vec, z)) + nuovo_bias
            predizioni_pre_update.append(_sigmoid(logit))

        log_loss = _calcola_log_loss(predizioni_pre_update, [e.esito for e in esempi])
        accuracy = _calcola_accuracy(predizioni_pre_update, [e.esito for e in esempi], soglia=0.5)

        # --- Update SGD vero e proprio, un esempio alla volta ---
        for esempio in esempi:
            # 1. aggiorna i normalizzatori PRIMA di calcolare z per QUESTO
            #    esempio (cosi' l'esempio corrente contribuisce anche alla
            #    propria normalizzazione - comportamento standard per
            #    normalizzazione online, coerente con "online" nel nome).
            for nome in self.stato.feature_names:
                nuovi_normalizzatori[nome].aggiorna(esempio.feature_grezze[nome])

            z = [nuovi_normalizzatori[nome].normalizza(esempio.feature_grezze[nome]) for nome in self.stato.feature_names]
            pesi_vec = [nuovi_pesi[nome] for nome in self.stato.feature_names]
            logit = sum(w * zi for w, zi in zip(pesi_vec, z)) + nuovo_bias
            p_hat = _sigmoid(logit)

            errore = p_hat - esempio.esito

            for nome, zi in zip(self.stato.feature_names, z):
                gradiente_w = errore * zi + cfg.l2_regularization * nuovi_pesi[nome]
                nuovi_pesi[nome] -= eta_t * gradiente_w

            gradiente_b = errore  # nessuna regolarizzazione L2 sul bias, per convenzione standard (il bias non deve essere "contenuto" verso zero)
            nuovo_bias -= eta_t * gradiente_b

        nuovo_stato = StatoModelCAdaptive(
            feature_names=self.stato.feature_names,
            pesi=nuovi_pesi,
            bias=nuovo_bias,
            normalizzatori=nuovi_normalizzatori,
            bet_concluse_totali=self.stato.bet_concluse_totali + len(esempi),
            numero_aggiornamenti_t=nuovo_t,
        )

        diagnostica = DiagnosticaTraining(
            log_loss=log_loss, accuracy_pre_update=accuracy,
            numero_esempi=len(esempi), learning_rate_usato=eta_t,
            bet_concluse_totali_dopo_update=nuovo_stato.bet_concluse_totali,
        )

        return nuovo_stato, diagnostica

    # ------------------------------------------------------------
    # Feature Importance (per il requisito "Sviluppo Avanzato")
    # ------------------------------------------------------------
    def feature_importance(self) -> List[Tuple[str, float]]:
        """
        Per un modello lineare, la feature importance e' semplicemente il
        valore assoluto del peso (dato che le feature sono normalizzate a
        z-score, i pesi SONO gia' comparabili tra loro - non serve alcuna
        ulteriore standardizzazione, a differenza di un modello allenato
        su feature con scale diverse). Ordinata decrescente.

        Il segno del peso originale (non il valore assoluto) va mostrato
        separatamente all'utente per l'interpretazione direzionale (vedi
        dashboard, step 7): un peso positivo alto = "piu' alta questa
        feature, piu' il modello e' fiducioso"; negativo = l'opposto.
        """
        importanze = [(nome, abs(peso)) for nome, peso in self.stato.pesi.items()]
        importanze.sort(key=lambda t: t[1], reverse=True)
        return importanze


@dataclass
class DiagnosticaTraining:
    log_loss: float
    accuracy_pre_update: float
    numero_esempi: int
    learning_rate_usato: float
    bet_concluse_totali_dopo_update: int


# ==================================================================
# Funzioni matematiche di supporto
# ==================================================================
def _sigmoid(x: float) -> float:
    """
    Sigmoid numericamente stabile: per x molto negativo, exp(-x) esplode
    e causerebbe un OverflowError in Python puro per x sufficientemente
    negativo (es. x=-1000). Si usa la forma equivalente per x<0 che
    evita l'overflow (identita' matematica: sigmoid(x) = exp(x)/(1+exp(x))
    per x<0, dove exp(x) con x negativo tende a 0 invece che a infinito).
    """
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    else:
        ex = math.exp(x)
        return ex / (1.0 + ex)


def _calcola_log_loss(predizioni: List[float], esiti: List[int]) -> float:
    n = len(predizioni)
    if n == 0:
        return 0.0
    somma = 0.0
    for p_hat, y in zip(predizioni, esiti):
        p_clip = min(max(p_hat, EPSILON_LOG_LOSS), 1 - EPSILON_LOG_LOSS)
        somma += y * math.log(p_clip) + (1 - y) * math.log(1 - p_clip)
    return round(-somma / n, 6)


def _calcola_accuracy(predizioni: List[float], esiti: List[int], soglia: float = 0.5) -> float:
    n = len(predizioni)
    if n == 0:
        return 0.0
    corrette = sum(1 for p_hat, y in zip(predizioni, esiti) if (p_hat >= soglia) == bool(y))
    return round((corrette / n) * 100, 2)


# ==================================================================
# Self-test manuale rapido (python3 models/model_c_adaptive.py)
# ==================================================================
if __name__ == "__main__":
    import random
    from config import DEFAULT_CONFIG

    cfg = DEFAULT_CONFIG.model_c

    print("=" * 70)
    print("TEST 1: Welford normalizzatore - confronto con formula diretta")
    print("=" * 70)
    valori = [10.0, 12.0, 23.0, 21.0, 15.0, 18.0, 9.0, 14.0]
    norm = NormalizzatoreOnline()
    for v in valori:
        norm.aggiorna(v)
    media_diretta = sum(valori) / len(valori)
    varianza_diretta = sum((v - media_diretta) ** 2 for v in valori) / len(valori)
    print(f"Welford:  media={norm.media:.6f}  varianza={norm.varianza:.6f}")
    print(f"Diretta:  media={media_diretta:.6f}  varianza={varianza_diretta:.6f}")
    assert abs(norm.media - media_diretta) < 1e-9, "media Welford deve coincidere con formula diretta"
    assert abs(norm.varianza - varianza_diretta) < 1e-9, "varianza Welford deve coincidere con formula diretta"
    print("MATCH ESATTO confermato.")

    print()
    print("=" * 70)
    print("TEST 2: sigmoid - valori noti e stabilita' numerica su input estremi")
    print("=" * 70)
    assert abs(_sigmoid(0.0) - 0.5) < 1e-9, "sigmoid(0) deve essere 0.5"
    assert _sigmoid(1000.0) > 0.999999, "sigmoid di un numero molto positivo deve saturare vicino a 1"
    assert _sigmoid(-1000.0) < 0.000001, "sigmoid di un numero molto negativo deve saturare vicino a 0 SENZA overflow"
    print(f"sigmoid(0)     = {_sigmoid(0.0)}")
    print(f"sigmoid(1000)  = {_sigmoid(1000.0)} (nessun overflow)")
    print(f"sigmoid(-1000) = {_sigmoid(-1000.0)} (nessun overflow)")

    print()
    print("=" * 70)
    print("TEST 3: bootstrap + inferenza a freddo (zero storico)")
    print("=" * 70)
    stato = StatoModelCAdaptive.bootstrap(cfg)
    model = ModelCAdaptive(cfg, stato)
    evento_test = EventoInputEsteso(
        event_id=1, campionato="Italia - Serie A", mercato="1X2", selection="1",
        probability_pct=58.0, bookmaker_odds=2.00, fair_odds=1.72, clv_stimato_pct=1.5,
        smart_filter_score=0.7, orario_evento=datetime(2026, 7, 18, 20, 45),  # sabato sera
    )
    v = model.valuta(evento_test)
    print(f"accepted={v.accepted}  " + (f"prob_modello(confidence)={v.confidence_score}%  ev={v.ev_pct}%" if v.accepted else f"motivo={v.motivo_scarto}"))

    print()
    print("=" * 70)
    print("TEST 4: training - il modello impara a distinguere due gruppi separabili")
    print("=" * 70)
    # Dataset sintetico: EV alto + CLV alto -> quasi sempre vinta; EV
    # basso/negativo + CLV negativo -> quasi sempre persa. Costruito per
    # essere facilmente separabile, cosi' l'accuracy dopo training deve
    # salire chiaramente sopra il 50% casuale.
    random.seed(42)

    def genera_esempio(buono: bool) -> EsempioTraining:
        if buono:
            ev = random.uniform(8.0, 20.0)
            clv = random.uniform(1.0, 3.0)
            esito = 1 if random.random() < 0.75 else 0  # segnale forte ma non deterministico (realistico)
        else:
            ev = random.uniform(-10.0, 2.0)
            clv = random.uniform(-3.0, -0.5)
            esito = 1 if random.random() < 0.25 else 0
        feature = {
            "ev_pct": ev, "kelly_fraction_teorica": max(0.0, ev / 100),
            "quota_bookmaker": 2.0, "clv_stimato_pct": clv, "smart_filter_score": 0.6 if buono else 0.3,
            "prob_stimata_vs_implicita_delta": ev / 5, "ora_del_giorno_sin": 0.0, "ora_del_giorno_cos": 1.0,
            "giorno_settimana_sin": 0.0, "giorno_settimana_cos": 1.0,
        }
        return EsempioTraining(feature_grezze=feature, esito=esito)

    esempi_training = [genera_esempio(i % 2 == 0) for i in range(200)]

    stato2 = StatoModelCAdaptive.bootstrap(cfg)
    model2 = ModelCAdaptive(cfg, stato2)

    log_loss_storico = []
    accuracy_storico = []
    batch_size = cfg.aggiorna_ogni_n_bet_concluse
    for i in range(0, len(esempi_training), batch_size):
        batch = esempi_training[i:i + batch_size]
        if len(batch) < batch_size:
            break
        nuovo_stato, diag = model2.aggiorna_pesi(batch)
        model2 = ModelCAdaptive(cfg, nuovo_stato)
        log_loss_storico.append(diag.log_loss)
        accuracy_storico.append(diag.accuracy_pre_update)

    print(f"Log-loss primi 3 batch:  {log_loss_storico[:3]}")
    print(f"Log-loss ultimi 3 batch: {log_loss_storico[-3:]}")
    print(f"Accuracy primi 3 batch:  {accuracy_storico[:3]}")
    print(f"Accuracy ultimi 3 batch: {accuracy_storico[-3:]}")
    media_accuracy_iniziale = sum(accuracy_storico[:3]) / 3
    media_accuracy_finale = sum(accuracy_storico[-3:]) / 3
    print(f"Accuracy media primi 3 batch: {media_accuracy_iniziale:.2f}%  |  ultimi 3 batch: {media_accuracy_finale:.2f}%")
    assert media_accuracy_finale > 55.0, "su un dataset chiaramente separabile, l'accuracy finale deve superare nettamente il 50% casuale"
    print(f"Bet concluse totali nello stato finale: {model2.stato.bet_concluse_totali} (atteso: multiplo di {batch_size} <= 200)")

    print()
    print("=" * 70)
    print("TEST 5: feature importance dopo training")
    print("=" * 70)
    importanze = model2.feature_importance()
    for nome, imp in importanze[:5]:
        segno = "+" if model2.stato.pesi[nome] >= 0 else "-"
        print(f"  {nome:35s} |peso|={imp:.4f}  (segno originale: {segno})")
    nomi_top5 = [n for n, _ in importanze[:5]]
    print(f"\nTop feature attese tra le piu' rilevanti (ev_pct, clv_stimato_pct, "
          f"prob_stimata_vs_implicita_delta, smart_filter_score): {nomi_top5}")

    print()
    print("=" * 70)
    print("TEST 6: robustezza - stato con feature_names diverse deve fallire al costruttore")
    print("=" * 70)
    try:
        stato_corrotto = StatoModelCAdaptive.bootstrap(cfg)
        stato_corrotto.feature_names = ("ev_pct",)  # deliberatamente disallineato
        ModelCAdaptive(cfg, stato_corrotto)
        print("ERRORE: doveva sollevare ValueError e non l'ha fatto")
        raise SystemExit(1)
    except ValueError as e:
        print(f"ValueError sollevato correttamente: {e}")

    print()
    print("=" * 70)
    print("TEST 7: robustezza - aggiorna_pesi() con lista vuota deve fallire")
    print("=" * 70)
    try:
        model2.aggiorna_pesi([])
        print("ERRORE: doveva sollevare ValueError e non l'ha fatto")
        raise SystemExit(1)
    except ValueError as e:
        print(f"ValueError sollevato correttamente: {e}")

    print()
    print("=" * 70)
    print("TEST 8: schedule learning rate 1/sqrt(t) - decadimento verificato numericamente")
    print("=" * 70)
    stato3 = StatoModelCAdaptive.bootstrap(cfg)
    model3 = ModelCAdaptive(cfg, stato3)
    eta_attesi = []
    for t in range(1, 4):
        eta_atteso = cfg.learning_rate_iniziale / math.sqrt(1 + t)
        eta_attesi.append(eta_atteso)
        batch = esempi_training[(t - 1) * batch_size: t * batch_size]
        nuovo_stato, diag = model3.aggiorna_pesi(batch)
        model3 = ModelCAdaptive(cfg, nuovo_stato)
        print(f"batch t={t}: eta_usato={diag.learning_rate_usato:.9f}  atteso={eta_attesi[-1]:.9f}  match={abs(diag.learning_rate_usato - eta_attesi[-1]) < 1e-9}")
        assert abs(diag.learning_rate_usato - eta_attesi[-1]) < 1e-9

    print()
    print("Tutti i test completati.")
