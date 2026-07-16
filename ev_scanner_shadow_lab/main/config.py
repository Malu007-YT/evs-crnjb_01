"""
EV Scanner AI - Shadow Intelligence System
Step 2/7: config.py - Configurazione e parametri dei 5 Shadow Model
----------------------------------------------------------------
Segue lo stesso spirito di config/config.php nella produzione PHP:
costanti esplicite, commentate sul "perche'" oltre che sul "cosa", nessun
valore magico senza spiegazione. A differenza del PHP pero' qui i
parametri dei modelli sono raggruppati in dataclass (non costanti sparse),
perche' sono cio' che finisce congelato in
shadow_model_versions.parametri_snapshot_json ad ogni nuova versione, e
una struttura tipizzata rende quel serializzato meno soggetto a errori
rispetto a un dizionario libero scritto a mano ogni volta.

Isolamento dalla produzione: questo file NON legge/scrive mai
config/config.php o le sue costanti (DB_HOST, DB_NAME, ecc.) via
inclusione diretta. Le credenziali DB per lo Shadow Lab sono definite
qui sotto, separatamente, cosi' i due sistemi restano disaccoppiabili
anche se in pratica puntano allo stesso database in questa fase (vedi
README step 1).
----------------------------------------------------------------
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


# ==================================================================
# CONNESSIONE DATABASE
# ==================================================================
# Stesso database MySQL/MariaDB usato da EV Scanner in produzione (stesso
# DB_HOST/DB_NAME/DB_USER/DB_PASS di config/config.php), perche' lo Shadow
# Lab deve leggere `eventi` e `scommesse` in tempo reale. Le credenziali
# vanno SEMPRE lette da variabili d'ambiente in questo file Python (mai
# hardcoded come nel config.php originale, che le ha in chiaro perche'
# gira su hosting condiviso senza un vero meccanismo di secrets): lo
# Shadow Lab e' pensato per girare offline/in locale o su un host diverso
# da InfinityFree (vedi README step 1), dove un .env e' lo standard.
#
# Esempio .env:
#   EVSCANNER_DB_HOST=sql201.infinityfree.com
#   EVSCANNER_DB_NAME=if0_42322915_ev_scanner
#   EVSCANNER_DB_USER=if0_42322915
#   EVSCANNER_DB_PASS=...
#   EVSCANNER_DB_CHARSET=utf8mb4
# ------------------------------------------------------------------

@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    name: str
    user: str
    password: str
    charset: str = "utf8mb4"
    # Fuso orario di sessione MySQL, coerente con Database::connect() in
    # config/database.php (che imposta l'offset numerico di Europe/Rome
    # invece del nome, perche' molte installazioni condivise non hanno le
    # named timezones caricate).
    timezone: str = "Europe/Rome"
    connect_timeout_seconds: int = 5


def load_database_config() -> DatabaseConfig:
    """
    Carica la configurazione database da variabili d'ambiente. Solleva
    un errore esplicito e leggibile (non un generico KeyError) se manca
    qualcosa di obbligatorio, per evitare di scoprirlo a runtime nel
    mezzo di un ciclo di scansione shadow.
    """
    required = ["EVSCANNER_DB_HOST", "EVSCANNER_DB_NAME", "EVSCANNER_DB_USER", "EVSCANNER_DB_PASS"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "Configurazione database Shadow Lab incompleta. Variabili d'ambiente "
            f"mancanti: {', '.join(missing)}. Impostale (es. in un file .env caricato "
            "con python-dotenv) prima di avviare main_shadow_engine.py."
        )

    return DatabaseConfig(
        host=os.environ["EVSCANNER_DB_HOST"],
        name=os.environ["EVSCANNER_DB_NAME"],
        user=os.environ["EVSCANNER_DB_USER"],
        password=os.environ["EVSCANNER_DB_PASS"],
        charset=os.environ.get("EVSCANNER_DB_CHARSET", "utf8mb4"),
    )


# ==================================================================
# PARAMETRI SHADOW MODEL A - Conservative
# ==================================================================
@dataclass
class ModelAConfig:
    """
    Ottimizzazione del rischio: massimizzare il ROI storico riducendo
    drasticamente la varianza. Vedi progettazione originale, sezione
    Shadow Model A.
    """
    # Soglia EV minima per considerare l'evento (piu' alta di Model B:
    # Model A e' selettivo, non cerca ogni minimo edge).
    ev_minimo_pct: float = 4.0

    # Fattore di decadimento esponenziale applicato all'EV in funzione
    # della quota: quote alte vengono penalizzate anche a parita' di EV
    # nominale, perche' la varianza di un singolo evento a quota alta
    # e' proporzionalmente maggiore. Formula applicata in
    # models/model_a_conservative.py:
    #   ev_penalizzato = ev_pct * exp(-decadimento_quota_lambda * (quota - 1))
    # Con lambda=0.15: una quota di 2.00 penalizza l'EV di un fattore
    # ~0.86, una quota di 5.00 di un fattore ~0.51.
    decadimento_quota_lambda: float = 0.15

    # Quota massima accettata in assoluto, indipendentemente dall'EV
    # penalizzato: oltre questa soglia il modello scarta l'evento a
    # prescindere (troppa varianza per la filosofia "Conservative").
    quota_massima_assoluta: float = 4.00

    # Filtro di stabilita': solo mercati primari. Le chiavi devono
    # corrispondere ai valori usati in eventi/scommesse (vedi
    # scommesse.selection ENUM('1','X','2') - Model A al momento
    # ragiona solo su 1X2, l'estensione a altri mercati come Asian
    # Handicap/Over-Under principali e' predisposta qui ma richiede
    # che tali mercati esistano nello schema produzione, non presenti
    # ad oggi: vedi mercati_consentiti come lista aperta per quando
    # verranno aggiunti.
    mercati_consentiti: tuple = ("1X2",)

    # Leghe ad alta liquidità: lista di sottostringhe (case-insensitive,
    # stesso approccio di THE_ODDS_API_LEAGUE_MAP in config.php) su cui
    # eventi.campionato viene confrontato. Se vuota, nessun filtro lega
    # viene applicato (utile in fase di bootstrap quando c'e' poco
    # storico e restringere troppo lascerebbe il modello senza dati).
    leghe_alta_liquidita_keywords: tuple = (
        "serie a", "premier league", "la liga", "laliga", "bundesliga",
        "ligue 1", "champions league", "europa league",
    )

    # Peso minimo di CLV stimato (probabilita teorica in %% di battere la
    # quota di chiusura) richiesto per accettare la bet shadow. Se il
    # provider CLV non ha dato disponibile per l'evento, il modello
    # applica clv_fallback_policy (vedi model_a_conservative.py).
    clv_stimato_minimo_pct: float = 1.0
    clv_fallback_policy: str = "scarta"  # "scarta" | "ignora_filtro_clv"

    # Kelly Criterion estremamente conservativo.
    kelly_fraction: float = 0.05

    def validate(self) -> None:
        if not (0 < self.kelly_fraction <= 1):
            raise ValueError(f"Model A: kelly_fraction fuori range (0,1]: {self.kelly_fraction}")
        if self.quota_massima_assoluta <= 1:
            raise ValueError("Model A: quota_massima_assoluta deve essere > 1")
        if self.decadimento_quota_lambda < 0:
            raise ValueError("Model A: decadimento_quota_lambda non puo' essere negativo")
        if self.clv_fallback_policy not in ("scarta", "ignora_filtro_clv"):
            raise ValueError(f"Model A: clv_fallback_policy non valido: {self.clv_fallback_policy}")


# ==================================================================
# PARAMETRI SHADOW MODEL B - Pure EV
# ==================================================================
@dataclass
class ModelBConfig:
    """
    Edge matematico puro: nessun filtro euristico di stabilita', solo
    EV = (p * quota) - 1 sopra soglia. Vedi progettazione, Shadow Model B.
    """
    ev_minimo_pct: float = 2.0  # soglia piu' bassa di Model A: cattura piu' segnali

    # Kelly semi-aggressivo. Esposto come range possibile nella
    # progettazione (0.25 - 0.50): di default si parte dal piu'
    # prudente dei due, modificabile in shadow_model_versions al
    # prossimo aggiornamento se lo storico lo giustifica.
    kelly_fraction: float = 0.25

    # A differenza di Model A, nessun tetto sulla quota: e' parte della
    # filosofia "Pure EV" ignorare la varianza a breve termine. Questo
    # campo esiste comunque come valvola di sicurezza operativa (non
    # concettuale): una quota "infinita" e' quasi sempre un errore nei
    # dati di origine (es. quota fantasma pre-evento), non un vero
    # value bet, e va scartata per igiene dei dati, non per prudenza.
    quota_massima_sanity_check: float = 100.0

    def validate(self) -> None:
        if not (0 < self.kelly_fraction <= 1):
            raise ValueError(f"Model B: kelly_fraction fuori range (0,1]: {self.kelly_fraction}")
        if self.quota_massima_sanity_check <= 1:
            raise ValueError("Model B: quota_massima_sanity_check deve essere > 1")


# ==================================================================
# PARAMETRI SHADOW MODEL C - Adaptive
# ==================================================================
@dataclass
class ModelCConfig:
    """
    Modello quantitativo dinamico con apprendimento online. Vedi
    progettazione, Shadow Model C, e models/model_c_adaptive.py (step 6)
    per la formulazione matematica completa (online logistic regression
    con SGD + regolarizzazione L2).
    """
    # Feature usate dal modello, nell'ordine in cui vengono vettorizzate.
    # Deve rimanere sincronizzato con la logica di estrazione feature in
    # model_c_adaptive.py::extract_features(). Cambiare quest'ordine dopo
    # che il modello ha gia' pesi salvati in shadow_model_c_weights
    # invalida quei pesi (serve un nuovo bootstrap) - per questo e'
    # isolato qui come singola fonte di verita'.
    feature_names: tuple = (
        "ev_pct",
        "kelly_fraction_teorica",
        "quota_bookmaker",
        "clv_stimato_pct",
        "smart_filter_score",
        "prob_stimata_vs_implicita_delta",
        "ora_del_giorno_sin",  # encoding ciclico dell'orario evento
        "ora_del_giorno_cos",
        "giorno_settimana_sin",
        "giorno_settimana_cos",
    )

    # Learning rate iniziale per SGD online. Vedi model_c_adaptive.py per
    # lo schedule di decadimento (1/sqrt(t), standard per SGD online per
    # garantire convergenza).
    learning_rate_iniziale: float = 0.01

    # Regolarizzazione L2 (Ridge), per contenere overfitting su feature
    # rumorose con poco storico (specialmente nei primi mesi di vita del
    # modello, quando N e' piccolo).
    l2_regularization: float = 0.001

    # Il modello si riaddestra (aggiorna i pesi) ogni N bet shadow
    # concluse (settled). Valore piccolo = adattamento piu' rapido ma
    # piu' rumoroso; valore grande = piu' stabile ma piu' lento a
    # correggersi.
    aggiorna_ogni_n_bet_concluse: int = 10

    # Soglia di probabilita' di vittoria stimata dal modello (output
    # della sigmoid, 0-1) sopra la quale il modello genera una bet
    # shadow. Interpretabile come "confidence minima".
    soglia_probabilita_output: float = 0.55

    # Numero minimo di bet settled nello storico globale prima che
    # Model C inizi a operare con pesi appresi: sotto questa soglia usa
    # pesi di bootstrap ragionevoli ma non ancora "imparati" (vedi
    # bootstrap_weights sotto), per evitare decisioni premature basate
    # su 3-4 osservazioni.
    minimo_bet_per_pesi_appresi: int = 50

    # Pesi di bootstrap (pre-training euristico), usati finche' non si
    # raggiunge minimo_bet_per_pesi_appresi. Un peso positivo aumenta la
    # probabilita' di output all'aumentare della feature; ordine
    # allineato a feature_names.
    bootstrap_weights: tuple = (0.5, 0.2, -0.1, 0.4, 0.3, 0.2, 0.0, 0.0, 0.0, 0.0)
    bootstrap_bias: float = -1.0

    def validate(self) -> None:
        if len(self.bootstrap_weights) != len(self.feature_names):
            raise ValueError(
                f"Model C: bootstrap_weights ({len(self.bootstrap_weights)}) deve avere "
                f"la stessa lunghezza di feature_names ({len(self.feature_names)})"
            )
        if self.learning_rate_iniziale <= 0:
            raise ValueError("Model C: learning_rate_iniziale deve essere positivo")
        if self.aggiorna_ogni_n_bet_concluse < 1:
            raise ValueError("Model C: aggiorna_ogni_n_bet_concluse deve essere >= 1")
        if not (0 < self.soglia_probabilita_output < 1):
            raise ValueError("Model C: soglia_probabilita_output deve essere in (0,1)")


# ==================================================================
# PARAMETRI SHADOW MODEL D - Ensemble
# ==================================================================
@dataclass
class ModelDConfig:
    """
    Meta-modello di consenso su A/B/C. Vedi progettazione, Shadow Model D,
    e models/model_d_ensemble.py (step 5).
    """
    # Se A, B e C concordano sullo stesso event_id+selection, lo score
    # sale al massimo indipendentemente dai pesi Sharpe sottostanti.
    score_massimo_su_consenso_totale: float = 100.0

    # Finestra (giorni) su cui calcolare lo Sharpe Ratio recente di
    # ciascun modello sorgente, usato per pesare il contributo di
    # ciascuno quando NON c'e' consenso totale (es. solo A+B concordano,
    # C no: il peso di A e B nello score dipende da quanto sono stati
    # performanti di recente).
    finestra_sharpe_giorni: int = 30

    # Numero minimo di bet settled nella finestra sopra, sotto il quale
    # un modello sorgente riceve un peso neutro di default invece del suo
    # Sharpe calcolato (che sarebbe statisticamente inaffidabile con
    # pochi dati).
    minimo_bet_per_peso_sharpe: int = 15
    peso_neutro_default: float = 1.0

    # Soglia di score sotto la quale Model D NON genera comunque una bet
    # shadow, anche se almeno un modello sorgente ha segnalato l'evento
    # (lo scoring esiste per tutte le combinazioni, ma solo sopra soglia
    # diventa una riga in shadow_bets con model_source='model_d').
    score_minimo_per_generare_bet: float = 40.0

    # Kelly fraction usata da Model D per il proprio stake shadow
    # indipendente: nella filosofia ensemble ha senso posizionarla tra
    # quella conservativa di A e quella aggressiva di B, proporzionale
    # allo score di consenso (vedi model_d_ensemble.py per la formula
    # esatta di scaling).
    kelly_fraction_base: float = 0.15

    def validate(self) -> None:
        if self.finestra_sharpe_giorni < 1:
            raise ValueError("Model D: finestra_sharpe_giorni deve essere >= 1")
        if not (0 <= self.score_minimo_per_generare_bet <= self.score_massimo_su_consenso_totale):
            raise ValueError("Model D: score_minimo_per_generare_bet fuori range valido")


# ==================================================================
# PARAMETRI SHADOW MODEL E - AutoML
# ==================================================================
@dataclass
class ModelEConfig:
    """
    Ricerca genetica di iperparametri/strategie. Vedi progettazione,
    Shadow Model E, e models/model_e_automl.py (step 7). Eseguito
    OFFLINE (mai come endpoint web, vedi nota hosting nel README step 1).
    """
    dimensione_popolazione: int = 30
    numero_generazioni_per_ciclo: int = 20

    # Probabilita' di mutazione per gene ad ogni generazione (standard
    # per GA: valori piccoli, 0.05-0.15, per non degenerare in ricerca
    # casuale pura).
    tasso_mutazione: float = 0.10
    tasso_crossover: float = 0.70

    # Quanti individui migliori (elitismo) passano invariati alla
    # generazione successiva senza crossover/mutazione, per non perdere
    # mai la miglior soluzione trovata finora.
    elitismo_top_n: int = 2

    # Spazio di ricerca (min, max) per ciascun gene. Vedi
    # model_e_automl.py::genoma_random() per come vengono campionati.
    range_ev_minimo_pct: tuple = (1.0, 8.0)
    range_quota_massima: tuple = (1.5, 8.0)
    range_kelly_fraction: tuple = (0.05, 0.50)
    range_clv_minimo_pct: tuple = (0.0, 5.0)
    mercati_possibili: tuple = ("1X2",)  # estendibile in futuro come Model A

    # Walk-forward validation: numero di "fold" temporali in cui lo
    # storico viene diviso per il backtest (vedi model_e_automl.py per
    # la metodologia anti-overfitting completa).
    walk_forward_num_fold: int = 5
    # Percentuale di ciascun fold usata come test out-of-sample (il
    # resto e' training per quel fold).
    walk_forward_test_size_pct: float = 0.20

    # Numero minimo di bet nel dataset di backtest sotto il quale un
    # intero ciclo di ottimizzazione viene rifiutato a priori (troppo
    # pochi dati per qualunque conclusione statisticamente decente).
    minimo_bet_per_backtest: int = 200

    # Numero di configurazioni Champion/Challenger mantenute attive.
    numero_champion_mantenuti: int = 3

    # Pesi della funzione di fitness composita usata per il ranking
    # genetico (vedi model_e_automl.py::calcola_fitness()). Deve sommare
    # a 1.0 - normalizzato automaticamente se non lo fa, con warning.
    fitness_peso_roi: float = 0.45
    fitness_peso_sharpe: float = 0.35
    fitness_peso_drawdown: float = 0.20  # drawdown minore = fitness maggiore, peso su -max_drawdown

    def validate(self) -> None:
        if self.dimensione_popolazione < self.elitismo_top_n:
            raise ValueError("Model E: dimensione_popolazione deve essere >= elitismo_top_n")
        if not (0 <= self.tasso_mutazione <= 1) or not (0 <= self.tasso_crossover <= 1):
            raise ValueError("Model E: tasso_mutazione e tasso_crossover devono essere in [0,1]")
        pesi_sum = self.fitness_peso_roi + self.fitness_peso_sharpe + self.fitness_peso_drawdown
        if abs(pesi_sum - 1.0) > 1e-6:
            raise ValueError(
                f"Model E: i pesi della fitness devono sommare a 1.0, trovato {pesi_sum:.4f}. "
                "Correggi fitness_peso_roi/sharpe/drawdown in config.py."
            )
        if self.walk_forward_num_fold < 2:
            raise ValueError("Model E: walk_forward_num_fold deve essere >= 2 per avere un vero walk-forward")


# ==================================================================
# CONFIGURAZIONE GLOBALE DEL SISTEMA SHADOW
# ==================================================================
@dataclass
class ShadowSystemConfig:
    model_a: ModelAConfig = field(default_factory=ModelAConfig)
    model_b: ModelBConfig = field(default_factory=ModelBConfig)
    model_c: ModelCConfig = field(default_factory=ModelCConfig)
    model_d: ModelDConfig = field(default_factory=ModelDConfig)
    model_e: ModelEConfig = field(default_factory=ModelEConfig)

    # Bankroll shadow iniziale per ciascun modello (indipendente dal
    # bankroll reale di produzione, vedi shadow_bankroll_snapshots nello
    # schema). Stesso valore per tutti di default per rendere i confronti
    # ROI% direttamente comparabili fin dal primo giorno.
    bankroll_shadow_iniziale: float = 1000.0

    # Soglia N di bet out-of-sample richiesta dal Promotion Engine (step
    # finale) prima di considerare valido un test di significativita'.
    promotion_minimo_bet_out_of_sample: int = 1000
    promotion_p_value_soglia: float = 0.05

    def validate(self) -> None:
        """Valida ricorsivamente tutti i sotto-config. Chiamare sempre
        prima di persistere una nuova versione in shadow_model_versions."""
        self.model_a.validate()
        self.model_b.validate()
        self.model_c.validate()
        self.model_d.validate()
        self.model_e.validate()
        if self.bankroll_shadow_iniziale <= 0:
            raise ValueError("bankroll_shadow_iniziale deve essere positivo")
        if self.promotion_minimo_bet_out_of_sample < 1:
            raise ValueError("promotion_minimo_bet_out_of_sample deve essere >= 1")
        if not (0 < self.promotion_p_value_soglia < 1):
            raise ValueError("promotion_p_value_soglia deve essere in (0,1)")

    def to_snapshot_dict(self) -> dict:
        """
        Serializza la configurazione corrente nella stessa struttura
        attesa da shadow_model_versions.parametri_snapshot_json:
        {"model_a": {...}, "model_b": {...}, ...}. Le tuple diventano
        liste (JSON non ha un tipo tupla), coerente con come verranno
        rilette da json.loads() lato Python o da JSON_EXTRACT lato SQL.
        """
        return {
            "model_a": asdict(self.model_a),
            "model_b": asdict(self.model_b),
            "model_c": asdict(self.model_c),
            "model_d": asdict(self.model_d),
            "model_e": asdict(self.model_e),
            "bankroll_shadow_iniziale": self.bankroll_shadow_iniziale,
            "promotion_minimo_bet_out_of_sample": self.promotion_minimo_bet_out_of_sample,
            "promotion_p_value_soglia": self.promotion_p_value_soglia,
        }

    @staticmethod
    def from_snapshot_dict(data: dict) -> "ShadowSystemConfig":
        """
        Ricostruisce un ShadowSystemConfig da un
        parametri_snapshot_json letto da shadow_model_versions (es. per
        rieseguire un backtest con i parametri esatti di una versione
        passata). Ogni sotto-dataclass viene ricostruita filtrando solo
        le chiavi che riconosce, cosi' uno snapshot piu' vecchio con meno
        campi (schema evoluto nel tempo) non causa un TypeError su
        keyword arguments mancanti: i campi assenti prendono
        semplicemente il default della dataclass corrente.
        """

        def _build(cls, sub: dict):
            valid_fields = {f.name for f in dataclasses.fields(cls)}
            filtered = {k: v for k, v in sub.items() if k in valid_fields}
            for f in dataclasses.fields(cls):
                if f.name in filtered and isinstance(filtered[f.name], list):
                    filtered[f.name] = tuple(filtered[f.name])
            return cls(**filtered)

        return ShadowSystemConfig(
            model_a=_build(ModelAConfig, data.get("model_a", {})),
            model_b=_build(ModelBConfig, data.get("model_b", {})),
            model_c=_build(ModelCConfig, data.get("model_c", {})),
            model_d=_build(ModelDConfig, data.get("model_d", {})),
            model_e=_build(ModelEConfig, data.get("model_e", {})),
            bankroll_shadow_iniziale=data.get("bankroll_shadow_iniziale", 1000.0),
            promotion_minimo_bet_out_of_sample=data.get("promotion_minimo_bet_out_of_sample", 1000),
            promotion_p_value_soglia=data.get("promotion_p_value_soglia", 0.05),
        )


# ==================================================================
# GESTIONE VERSIONING (interazione con shadow_model_versions)
# ==================================================================
def export_current_params_snapshot(
    config: ShadowSystemConfig,
    versione: str,
    descrizione_modifiche: str,
    autore: str = "Malu",
    note: Optional[str] = None,
) -> dict:
    """
    Prepara il payload da inserire in shadow_model_versions per attivare
    una nuova versione del sistema. NON esegue l'INSERT/UPDATE SQL: quello
    e' responsabilita' del layer di persistenza (utils/db.py, step
    successivo), per mantenere config.py privo di dipendenze dirette dal
    driver database e quindi facilmente testabile in isolamento.

    Valida sempre la config PRIMA di produrre lo snapshot: una versione
    con parametri invalidi non deve mai poter finire nel DB.

    Ritorna un dict pronto per essere passato come parametri ad una query
    INSERT INTO shadow_model_versions (vedi schema.sql, step 1):
        {
            "versione": ...,
            "data_attivazione": ...,
            "descrizione_modifiche": ...,
            "autore": ...,
            "note": ...,
            "parametri_snapshot_json": ...,  # gia' serializzato in stringa JSON
        }
    """
    config.validate()

    return {
        "versione": versione,
        "data_attivazione": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "descrizione_modifiche": descrizione_modifiche,
        "autore": autore,
        "note": note,
        "parametri_snapshot_json": json.dumps(config.to_snapshot_dict(), ensure_ascii=False),
    }


# ==================================================================
# ISTANZA DI DEFAULT
# ==================================================================
# Punto di ingresso comodo per gli altri moduli: `from config import
# DEFAULT_CONFIG` da' subito una configurazione valida con i parametri di
# bootstrap descritti sopra. I moduli che devono usare i parametri di UNA
# VERSIONE SPECIFICA storica (es. per ricalcolare uno storico) useranno
# invece ShadowSystemConfig.from_snapshot_dict() leggendo la riga
# corrispondente da shadow_model_versions.
DEFAULT_CONFIG = ShadowSystemConfig()
DEFAULT_CONFIG.validate()


if __name__ == "__main__":
    # Self-test rapido eseguibile con `python3 config.py`: valida la
    # config di default e mostra lo snapshot che verrebbe scritto per la
    # versione di bootstrap v1.0, utile per verificare a colpo d'occhio
    # che tutto sia coerente prima di collegare il layer DB.
    payload = export_current_params_snapshot(
        DEFAULT_CONFIG,
        versione="v1.0",
        descrizione_modifiche="Bootstrap iniziale AI Research Lab - parametri di default.",
    )
    print("Config di default valida. Snapshot v1.0:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
