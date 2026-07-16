import React, { useState, useMemo } from 'react';
import { LineChart, Line, AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine, Cell } from 'recharts';

// ============================================================
// EV Scanner AI - Shadow Intelligence System
// Step 7/7 (parte C): Dashboard mockup concettuale
// ------------------------------------------------------------
// Direzione visuale: terminal quantitativo, non un dashboard SaaS
// generico. Sfondo quasi-nero, monospace per ogni numero (i numeri
// sono il prodotto qui, non un'illustrazione), un solo accento colore
// per segnale positivo/negativo coerente in tutta l'interfaccia
// (verde/rosso finanziario standard, riconoscibile a colpo d'occhio
// da chiunque guardi dati di trading), tipografia sans discreta per
// le etichette. La griglia densa e i bordi sottili richiamano un
// vero terminal Bloomberg/trading desk, coerente col fatto che
// questo e' letteralmente un laboratorio di ricerca quantitativa,
// non un prodotto consumer.
//
// Dati: tutti MOCK, generati qui sotto in modo deterministico (seed
// fisso) per rappresentare in modo plausibile 5 modelli su ~90 giorni
// di storico shadow. In produzione ogni sezione verrebbe alimentata
// da utils/stats_engine.py (step 4) via un layer API/DB non ancora
// scritto in questo progetto.
// ============================================================

const PALETTE = {
  bg: '#0a0e14',
  bgPanel: '#0f1420',
  bgPanelAlt: '#131a29',
  border: '#1f2937',
  borderBright: '#2d3b52',
  textPrimary: '#e2e8f0',
  textSecondary: '#8b96a8',
  textMuted: '#5a6478',
  positive: '#3ddc84',
  positiveDim: '#1f6b46',
  negative: '#ff5c5c',
  negativeDim: '#7a2828',
  accent: '#4a9eff',
  accentDim: '#1d3a5c',
  gold: '#e0b341',
};

const MODELLI_META = {
  model_a: { nome: 'Model A · Conservative', colore: '#4a9eff' },
  model_b: { nome: 'Model B · Pure EV', colore: '#e0b341' },
  model_c: { nome: 'Model C · Adaptive', colore: '#3ddc84' },
  model_d: { nome: 'Model D · Ensemble', colore: '#c77dff' },
  model_e: { nome: 'Model E · AutoML', colore: '#ff8b5c' },
  produzione: { nome: 'Filtro Ufficiale (Produzione)', colore: '#8b96a8' },
};

// ------------------------------------------------------------
// Generatore dati mock deterministico (seed fisso, PRNG semplice)
// ------------------------------------------------------------
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function generaCurvaBankroll(seed, giorni, drift, volatilita, bankrollIniziale, versioniMarker) {
  const rng = mulberry32(seed);
  let bankroll = bankrollIniziale;
  const punti = [];
  for (let i = 0; i < giorni; i++) {
    const rumore = (rng() - 0.5) * volatilita;
    bankroll += drift + rumore;
    punti.push({
      giorno: i,
      bankroll: Math.round(bankroll * 100) / 100,
      versione: versioniMarker.find(v => v.giorno === i)?.versione || null,
    });
  }
  return punti;
}

const VERSIONI = [
  { giorno: 0, versione: 'v1.0', data: '17 Apr 2026', descrizione: 'Bootstrap iniziale — 5 Shadow Model attivi con parametri di default' },
  { giorno: 22, versione: 'v1.1', data: '9 Mag 2026', descrizione: 'Model A: aggiunta penalizzazione esponenziale sulla quota' },
  { giorno: 41, versione: 'v1.2', data: '28 Mag 2026', descrizione: 'Model C: prima attivazione pesi appresi (soglia 50 bet raggiunta)' },
  { giorno: 63, versione: 'v1.3', data: '19 Giu 2026', descrizione: 'Model D: ricalibrata soglia minima consenso 62→65' },
  { giorno: 78, versione: 'v1.4', data: '4 Lug 2026', descrizione: 'Model E: primo ciclo evolutivo completato, 3 champion pubblicati' },
];

const GIORNI_STORICO = 90;
const curveBankroll = {
  model_a: generaCurvaBankroll(101, GIORNI_STORICO, 1.8, 14, 1000, VERSIONI),
  model_b: generaCurvaBankroll(202, GIORNI_STORICO, 0.6, 26, 1000, VERSIONI),
  model_c: generaCurvaBankroll(303, GIORNI_STORICO, 2.4, 16, 1000, VERSIONI),
  model_d: generaCurvaBankroll(404, GIORNI_STORICO, 2.9, 11, 1000, VERSIONI),
  model_e: generaCurvaBankroll(505, GIORNI_STORICO, 1.1, 19, 1000, VERSIONI),
  produzione: generaCurvaBankroll(606, GIORNI_STORICO, 1.3, 12, 1000, VERSIONI),
};

function calcolaMetricheDaCurva(curva) {
  const iniziale = curva[0].bankroll;
  const finale = curva[curva.length - 1].bankroll;
  const profitto = finale - iniziale;
  const roi = (profitto / iniziale) * 100;

  let picco = iniziale;
  let maxDrawdown = 0;
  for (const p of curva) {
    if (p.bankroll > picco) picco = p.bankroll;
    const dd = ((picco - p.bankroll) / picco) * 100;
    if (dd > maxDrawdown) maxDrawdown = dd;
  }

  const rendimenti = [];
  for (let i = 1; i < curva.length; i++) {
    rendimenti.push((curva[i].bankroll - curva[i - 1].bankroll) / curva[i - 1].bankroll);
  }
  const media = rendimenti.reduce((a, b) => a + b, 0) / rendimenti.length;
  const varianza = rendimenti.reduce((a, b) => a + (b - media) ** 2, 0) / rendimenti.length;
  const sharpe = varianza > 0 ? (media / Math.sqrt(varianza)) * Math.sqrt(250) : 0;

  return {
    roi: Math.round(roi * 100) / 100,
    profitto: Math.round(profitto * 100) / 100,
    maxDrawdown: Math.round(maxDrawdown * 100) / 100,
    sharpe: Math.round(sharpe * 100) / 100,
    winRate: Math.round((48 + (roi > 0 ? Math.min(roi * 0.4, 14) : roi * 0.3)) * 10) / 10,
    numeroBet: Math.round(180 + Math.abs(roi) * 6),
  };
}

const METRICHE_MODELLI = Object.fromEntries(
  Object.entries(curveBankroll).map(([k, v]) => [k, calcolaMetricheDaCurva(v)])
);

const HEATMAP_CAMPIONATI = [
  { campionato: 'Serie A', bet: 84, profitto: 612.3 },
  { campionato: 'Premier League', bet: 71, profitto: 448.9 },
  { campionato: 'LaLiga', bet: 66, profitto: -128.4 },
  { campionato: 'Bundesliga', bet: 52, profitto: 291.6 },
  { campionato: 'Ligue 1', bet: 44, profitto: -84.2 },
  { campionato: 'Eredivisie', bet: 31, profitto: 156.8 },
  { campionato: 'Championship', bet: 38, profitto: -211.5 },
  { campionato: 'Serie B', bet: 27, profitto: 62.1 },
];

const FEATURE_IMPORTANCE_MODEL_C = [
  { feature: 'ev_pct', peso: 0.412, segno: '+' },
  { feature: 'clv_stimato_pct', peso: 0.318, segno: '+' },
  { feature: 'prob_stimata_vs_implicita_delta', peso: 0.276, segno: '+' },
  { feature: 'smart_filter_score', peso: 0.194, segno: '+' },
  { feature: 'kelly_fraction_teorica', peso: 0.151, segno: '+' },
  { feature: 'quota_bookmaker', peso: 0.098, segno: '−' },
  { feature: 'giorno_settimana_cos', peso: 0.041, segno: '−' },
  { feature: 'ora_del_giorno_sin', peso: 0.033, segno: '+' },
  { feature: 'ora_del_giorno_cos', peso: 0.021, segno: '+' },
  { feature: 'giorno_settimana_sin', peso: 0.014, segno: '−' },
];

const AUTOML_CHAMPION = [
  { rank: 1, evMin: 4.6, quotaMax: 5.3, kelly: 0.08, clvMin: 0.03, fitness: 1.154, roi: 3.33, sharpe: 1.82 },
  { rank: 2, evMin: 2.5, quotaMax: 5.3, kelly: 0.37, clvMin: 0.03, fitness: -0.585, roi: 3.33, sharpe: 0.44 },
  { rank: 3, evMin: 2.7, quotaMax: 5.3, kelly: 0.37, clvMin: 0.03, fitness: -0.585, roi: 3.33, sharpe: 0.44 },
];

// ------------------------------------------------------------
// Componenti primitivi
// ------------------------------------------------------------
function fmt(n, decimali = 2) {
  const sign = n > 0 ? '+' : '';
  return sign + n.toFixed(decimali);
}

function Indicatore({ valore, invertito = false }) {
  const positivo = invertito ? valore < 0 : valore > 0;
  const soglia = Math.abs(valore) < 0.15;
  if (soglia) return <span style={{ color: PALETTE.textMuted }}>≈</span>;
  return (
    <span style={{ color: positivo ? PALETTE.positive : PALETTE.negative, fontWeight: 600 }}>
      {positivo ? '▲' : '▼'}
    </span>
  );
}

function Pannello({ titolo, sottotitolo, children, style = {} }) {
  return (
    <div style={{
      background: PALETTE.bgPanel,
      border: `1px solid ${PALETTE.border}`,
      borderRadius: 3,
      padding: '18px 20px',
      ...style,
    }}>
      <div style={{ marginBottom: 14 }}>
        <div style={{
          fontSize: 11, fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase',
          color: PALETTE.textSecondary, fontFamily: "'JetBrains Mono', monospace",
        }}>
          {titolo}
        </div>
        {sottotitolo && (
          <div style={{ fontSize: 11, color: PALETTE.textMuted, marginTop: 2 }}>{sottotitolo}</div>
        )}
      </div>
      {children}
    </div>
  );
}

function MetricaBox({ label, valore, unita = '', colore, mono = true }) {
  return (
    <div>
      <div style={{ fontSize: 10, color: PALETTE.textMuted, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>
        {label}
      </div>
      <div style={{
        fontSize: 20, fontWeight: 700, color: colore || PALETTE.textPrimary,
        fontFamily: mono ? "'JetBrains Mono', monospace" : 'inherit',
      }}>
        {valore}{unita}
      </div>
    </div>
  );
}

function CustomTooltip({ active, payload, label }) {
  if (!active || !payload || !payload.length) return null;
  return (
    <div style={{
      background: PALETTE.bgPanelAlt, border: `1px solid ${PALETTE.borderBright}`,
      borderRadius: 3, padding: '8px 12px', fontSize: 12, fontFamily: "'JetBrains Mono', monospace",
    }}>
      <div style={{ color: PALETTE.textMuted, marginBottom: 4 }}>Giorno {label}</div>
      {payload.map((p, i) => (
        <div key={i} style={{ color: p.color }}>
          {p.name}: {p.value.toFixed(2)}
        </div>
      ))}
    </div>
  );
}

// ------------------------------------------------------------
// Sezione: Curva Bankroll con marker versioni
// ------------------------------------------------------------
function GraficoBankroll({ modelliVisibili }) {
  const datiCombinati = useMemo(() => {
    const out = [];
    for (let i = 0; i < GIORNI_STORICO; i++) {
      const riga = { giorno: i };
      for (const m of modelliVisibili) {
        riga[m] = curveBankroll[m][i].bankroll;
      }
      out.push(riga);
    }
    return out;
  }, [modelliVisibili]);

  return (
    <ResponsiveContainer width="100%" height={340}>
      <LineChart data={datiCombinati} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
        <CartesianGrid stroke={PALETTE.border} strokeDasharray="2 4" vertical={false} />
        <XAxis
          dataKey="giorno" tick={{ fill: PALETTE.textMuted, fontSize: 10, fontFamily: 'monospace' }}
          axisLine={{ stroke: PALETTE.border }} tickLine={false}
        />
        <YAxis
          tick={{ fill: PALETTE.textMuted, fontSize: 10, fontFamily: 'monospace' }}
          axisLine={{ stroke: PALETTE.border }} tickLine={false} domain={['auto', 'auto']}
        />
        <Tooltip content={<CustomTooltip />} />
        {VERSIONI.map(v => (
          <ReferenceLine
            key={v.versione} x={v.giorno} stroke={PALETTE.gold} strokeDasharray="3 3" strokeOpacity={0.5}
            label={{ value: v.versione, position: 'top', fill: PALETTE.gold, fontSize: 10, fontFamily: 'monospace' }}
          />
        ))}
        {modelliVisibili.map(m => (
          <Line
            key={m} type="monotone" dataKey={m} name={MODELLI_META[m].nome}
            stroke={MODELLI_META[m].colore} strokeWidth={m === 'produzione' ? 1.5 : 2}
            strokeDasharray={m === 'produzione' ? '4 3' : undefined}
            dot={false} activeDot={{ r: 3 }}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

// ------------------------------------------------------------
// Sezione: Heatmap profitto per campionato
// ------------------------------------------------------------
function HeatmapCampionati() {
  const max = Math.max(...HEATMAP_CAMPIONATI.map(h => Math.abs(h.profitto)));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {HEATMAP_CAMPIONATI.sort((a, b) => b.profitto - a.profitto).map(h => {
        const larghezza = (Math.abs(h.profitto) / max) * 100;
        const positivo = h.profitto >= 0;
        return (
          <div key={h.campionato} style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12 }}>
            <div style={{ width: 132, color: PALETTE.textSecondary, flexShrink: 0 }}>{h.campionato}</div>
            <div style={{ flex: 1, height: 16, background: PALETTE.bgPanelAlt, borderRadius: 2, position: 'relative' }}>
              <div style={{
                width: `${larghezza}%`, height: '100%', borderRadius: 2,
                background: positivo ? PALETTE.positiveDim : PALETTE.negativeDim,
                border: `1px solid ${positivo ? PALETTE.positive : PALETTE.negative}`,
              }} />
            </div>
            <div style={{
              width: 76, textAlign: 'right', fontFamily: "'JetBrains Mono', monospace", fontWeight: 600,
              color: positivo ? PALETTE.positive : PALETTE.negative,
            }}>
              {fmt(h.profitto)}€
            </div>
            <div style={{ width: 44, textAlign: 'right', color: PALETTE.textMuted, fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }}>
              {h.bet}n
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------
// Sezione: Feature Importance Model C
// ------------------------------------------------------------
function FeatureImportance() {
  const max = Math.max(...FEATURE_IMPORTANCE_MODEL_C.map(f => f.peso));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
      {FEATURE_IMPORTANCE_MODEL_C.map(f => (
        <div key={f.feature} style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 11.5 }}>
          <div style={{ width: 220, color: PALETTE.textSecondary, fontFamily: "'JetBrains Mono', monospace", flexShrink: 0 }}>
            {f.feature}
          </div>
          <div style={{ flex: 1, height: 12, background: PALETTE.bgPanelAlt, borderRadius: 2 }}>
            <div style={{
              width: `${(f.peso / max) * 100}%`, height: '100%', borderRadius: 2,
              background: f.segno === '+' ? PALETTE.accent : PALETTE.gold, opacity: 0.85,
            }} />
          </div>
          <div style={{ width: 46, textAlign: 'right', fontFamily: "'JetBrains Mono', monospace", color: PALETTE.textPrimary }}>
            {f.segno}{f.peso.toFixed(3)}
          </div>
        </div>
      ))}
    </div>
  );
}

// ------------------------------------------------------------
// Sezione: Widget Confronto Intelligente A/B
// ------------------------------------------------------------
function ConfrontoAB() {
  const [modelloA, setModelloA] = useState('model_a');
  const [modelloB, setModelloB] = useState('model_c');

  const mA = METRICHE_MODELLI[modelloA];
  const mB = METRICHE_MODELLI[modelloB];

  const righe = [
    { label: 'ROI', a: mA.roi, b: mB.roi, unita: '%', invertito: false },
    { label: 'Win Rate', a: mA.winRate, b: mB.winRate, unita: '%', invertito: false },
    { label: 'Max Drawdown', a: mA.maxDrawdown, b: mB.maxDrawdown, unita: '%', invertito: true },
    { label: 'Sharpe Ratio', a: mA.sharpe, b: mB.sharpe, unita: '', invertito: false },
    { label: 'Profitto Cum.', a: mA.profitto, b: mB.profitto, unita: '€', invertito: false },
  ];

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
        <SelettoreModello label="Baseline (A)" valore={modelloA} onChange={setModelloA} />
        <div style={{ display: 'flex', alignItems: 'center', color: PALETTE.textMuted, fontSize: 12 }}>vs</div>
        <SelettoreModello label="Candidato (B)" valore={modelloB} onChange={setModelloB} />
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <div style={{
          display: 'grid', gridTemplateColumns: '1fr 90px 90px 70px 30px', fontSize: 10,
          color: PALETTE.textMuted, textTransform: 'uppercase', letterSpacing: '0.04em',
          padding: '4px 10px', borderBottom: `1px solid ${PALETTE.border}`, marginBottom: 4,
        }}>
          <span>Metrica</span><span style={{ textAlign: 'right' }}>A</span><span style={{ textAlign: 'right' }}>B</span>
          <span style={{ textAlign: 'right' }}>Δ</span><span></span>
        </div>
        {righe.map(r => {
          const delta = r.b - r.a;
          return (
            <div key={r.label} style={{
              display: 'grid', gridTemplateColumns: '1fr 90px 90px 70px 30px', fontSize: 13,
              padding: '7px 10px', fontFamily: "'JetBrains Mono', monospace", borderRadius: 2,
              background: PALETTE.bgPanelAlt,
            }}>
              <span style={{ color: PALETTE.textSecondary, fontFamily: 'inherit' }}>{r.label}</span>
              <span style={{ textAlign: 'right', color: PALETTE.textMuted }}>{r.a.toFixed(2)}{r.unita}</span>
              <span style={{ textAlign: 'right', color: PALETTE.textPrimary }}>{r.b.toFixed(2)}{r.unita}</span>
              <span style={{ textAlign: 'right', color: PALETTE.textMuted }}>{fmt(delta)}{r.unita}</span>
              <span style={{ textAlign: 'right' }}><Indicatore valore={delta} invertito={r.invertito} /></span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SelettoreModello({ label, valore, onChange }) {
  return (
    <div style={{ flex: 1 }}>
      <div style={{ fontSize: 10, color: PALETTE.textMuted, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
        {label}
      </div>
      <select
        value={valore} onChange={e => onChange(e.target.value)}
        style={{
          width: '100%', background: PALETTE.bgPanelAlt, border: `1px solid ${PALETTE.border}`,
          color: PALETTE.textPrimary, borderRadius: 3, padding: '6px 8px', fontSize: 12,
          fontFamily: "'JetBrains Mono', monospace", outline: 'none',
        }}
      >
        {Object.entries(MODELLI_META).map(([k, v]) => (
          <option key={k} value={k}>{v.nome}</option>
        ))}
      </select>
    </div>
  );
}

// ------------------------------------------------------------
// Sezione: Timeline versioni
// ------------------------------------------------------------
function TimelineVersioni() {
  return (
    <div style={{ position: 'relative', paddingLeft: 18 }}>
      <div style={{ position: 'absolute', left: 5, top: 6, bottom: 6, width: 1, background: PALETTE.border }} />
      {VERSIONI.slice().reverse().map((v, i) => (
        <div key={v.versione} style={{ position: 'relative', paddingBottom: i === VERSIONI.length - 1 ? 0 : 18 }}>
          <div style={{
            position: 'absolute', left: -18, top: 3, width: 9, height: 9, borderRadius: '50%',
            background: i === 0 ? PALETTE.gold : PALETTE.bgPanel, border: `2px solid ${PALETTE.gold}`,
          }} />
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color: PALETTE.gold, fontSize: 13 }}>
              {v.versione}
            </span>
            <span style={{ fontSize: 11, color: PALETTE.textMuted }}>{v.data}</span>
          </div>
          <div style={{ fontSize: 12.5, color: PALETTE.textSecondary, marginTop: 3, lineHeight: 1.4 }}>
            {v.descrizione}
          </div>
        </div>
      ))}
    </div>
  );
}

// ------------------------------------------------------------
// Sezione: Model E Champion/Challenger
// ------------------------------------------------------------
function TabellaAutoML() {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5, fontFamily: "'JetBrains Mono', monospace" }}>
        <thead>
          <tr style={{ color: PALETTE.textMuted, textTransform: 'uppercase', fontSize: 10, letterSpacing: '0.04em' }}>
            <th style={{ textAlign: 'left', padding: '4px 8px', borderBottom: `1px solid ${PALETTE.border}` }}>#</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', borderBottom: `1px solid ${PALETTE.border}` }}>EV min%</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', borderBottom: `1px solid ${PALETTE.border}` }}>Quota max</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', borderBottom: `1px solid ${PALETTE.border}` }}>Kelly f.</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', borderBottom: `1px solid ${PALETTE.border}` }}>CLV min%</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', borderBottom: `1px solid ${PALETTE.border}` }}>Fitness</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', borderBottom: `1px solid ${PALETTE.border}` }}>ROI%</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', borderBottom: `1px solid ${PALETTE.border}` }}>Sharpe</th>
          </tr>
        </thead>
        <tbody>
          {AUTOML_CHAMPION.map(c => (
            <tr key={c.rank}>
              <td style={{ padding: '7px 8px', color: c.rank === 1 ? PALETTE.gold : PALETTE.textPrimary, fontWeight: c.rank === 1 ? 700 : 400 }}>
                {c.rank === 1 ? '★' : c.rank}
              </td>
              <td style={{ textAlign: 'right', padding: '7px 8px', color: PALETTE.textSecondary }}>{c.evMin.toFixed(2)}</td>
              <td style={{ textAlign: 'right', padding: '7px 8px', color: PALETTE.textSecondary }}>{c.quotaMax.toFixed(2)}</td>
              <td style={{ textAlign: 'right', padding: '7px 8px', color: PALETTE.textSecondary }}>{c.kelly.toFixed(3)}</td>
              <td style={{ textAlign: 'right', padding: '7px 8px', color: PALETTE.textSecondary }}>{c.clvMin.toFixed(2)}</td>
              <td style={{ textAlign: 'right', padding: '7px 8px', color: c.fitness >= 0 ? PALETTE.positive : PALETTE.negative, fontWeight: 600 }}>
                {fmt(c.fitness, 3)}
              </td>
              <td style={{ textAlign: 'right', padding: '7px 8px', color: PALETTE.positive }}>{fmt(c.roi)}</td>
              <td style={{ textAlign: 'right', padding: '7px 8px', color: PALETTE.textPrimary }}>{c.sharpe.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------
// App principale
// ------------------------------------------------------------
export default function ShadowLabDashboard() {
  const [modelliVisibili, setModelliVisibili] = useState(['model_a', 'model_b', 'model_c', 'model_d', 'produzione']);
  const [modelloSelezionato, setModelloSelezionato] = useState('model_c');

  const toggleModello = (m) => {
    setModelliVisibili(prev => prev.includes(m) ? prev.filter(x => x !== m) : [...prev, m]);
  };

  const metricheSelezionate = METRICHE_MODELLI[modelloSelezionato];

  return (
    <div style={{
      minHeight: '100vh', background: PALETTE.bg, color: PALETTE.textPrimary,
      fontFamily: "'Inter', -apple-system, sans-serif", padding: '24px 28px',
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');
        * { box-sizing: border-box; }
        select { cursor: pointer; }
        ::-webkit-scrollbar { height: 6px; width: 6px; }
        ::-webkit-scrollbar-thumb { background: ${PALETTE.border}; border-radius: 3px; }
      `}</style>

      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', marginBottom: 22, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <div style={{ fontSize: 10, color: PALETTE.gold, letterSpacing: '0.12em', fontWeight: 700, marginBottom: 4 }}>
            EV SCANNER · AI RESEARCH LAB
          </div>
          <div style={{ fontSize: 22, fontWeight: 700, letterSpacing: '-0.01em' }}>
            Shadow Intelligence System
          </div>
        </div>
        <div style={{ display: 'flex', gap: 18, fontSize: 11, color: PALETTE.textMuted, fontFamily: "'JetBrains Mono', monospace" }}>
          <span>versione attiva <b style={{ color: PALETTE.gold }}>v1.4</b></span>
          <span>storico <b style={{ color: PALETTE.textPrimary }}>{GIORNI_STORICO}g</b></span>
          <span>ultimo aggiornamento <b style={{ color: PALETTE.textPrimary }}>oggi 06:12</b></span>
        </div>
      </div>

      {/* Riga metriche modello selezionato */}
      <Pannello titolo="Shadow Model in Dettaglio" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 18, flexWrap: 'wrap' }}>
          {Object.entries(MODELLI_META).map(([k, v]) => (
            <button
              key={k} onClick={() => setModelloSelezionato(k)}
              style={{
                padding: '6px 12px', borderRadius: 3, fontSize: 11.5, fontFamily: "'JetBrains Mono', monospace",
                cursor: 'pointer', background: modelloSelezionato === k ? v.colore + '22' : 'transparent',
                border: `1px solid ${modelloSelezionato === k ? v.colore : PALETTE.border}`,
                color: modelloSelezionato === k ? v.colore : PALETTE.textMuted,
                fontWeight: modelloSelezionato === k ? 700 : 400,
              }}
            >
              {v.nome}
            </button>
          ))}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 20 }}>
          <MetricaBox label="ROI" valore={fmt(metricheSelezionate.roi)} unita="%" colore={metricheSelezionate.roi >= 0 ? PALETTE.positive : PALETTE.negative} />
          <MetricaBox label="Profitto Cum." valore={fmt(metricheSelezionate.profitto)} unita="€" colore={metricheSelezionate.profitto >= 0 ? PALETTE.positive : PALETTE.negative} />
          <MetricaBox label="Win Rate" valore={metricheSelezionate.winRate.toFixed(1)} unita="%" />
          <MetricaBox label="N° Bet" valore={metricheSelezionate.numeroBet} />
          <MetricaBox label="Max Drawdown" valore={metricheSelezionate.maxDrawdown.toFixed(2)} unita="%" colore={PALETTE.negative} />
          <MetricaBox label="Sharpe Ratio" valore={metricheSelezionate.sharpe.toFixed(2)} colore={metricheSelezionate.sharpe >= 1 ? PALETTE.positive : PALETTE.textPrimary} />
        </div>
      </Pannello>

      {/* Curva bankroll */}
      <Pannello
        titolo="Curva Bankroll Cumulativo"
        sottotitolo="Marker verticali oro = rilascio nuova versione del sistema"
        style={{ marginBottom: 16 }}
      >
        <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
          {Object.entries(MODELLI_META).map(([k, v]) => (
            <label key={k} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, color: PALETTE.textSecondary, cursor: 'pointer' }}>
              <input type="checkbox" checked={modelliVisibili.includes(k)} onChange={() => toggleModello(k)} style={{ accentColor: v.colore }} />
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: v.colore, display: 'inline-block' }} />
              {v.nome}
            </label>
          ))}
        </div>
        <GraficoBankroll modelliVisibili={modelliVisibili} />
      </Pannello>

      <div style={{ display: 'grid', gridTemplateColumns: '1.1fr 0.9fr', gap: 16, marginBottom: 16 }}>
        {/* Confronto A/B */}
        <Pannello titolo="Widget di Confronto Intelligente" sottotitolo="A/B testing tra modelli o versioni">
          <ConfrontoAB />
        </Pannello>

        {/* Timeline versioni */}
        <Pannello titolo="Timeline Aggiornamenti">
          <TimelineVersioni />
        </Pannello>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16 }}>
        {/* Heatmap campionati */}
        <Pannello titolo="Heatmap Profitto per Campionato" sottotitolo="Model C · ultimi 90 giorni">
          <HeatmapCampionati />
        </Pannello>

        {/* Feature importance */}
        <Pannello titolo="Feature Importance" sottotitolo="Model C (Adaptive) · |peso| dopo normalizzazione z-score">
          <FeatureImportance />
        </Pannello>
      </div>

      {/* AutoML Champion */}
      <Pannello titolo="Model E · Champion / Challenger" sottotitolo="Configurazioni attive dall'ultimo ciclo evolutivo">
        <TabellaAutoML />
      </Pannello>

      <div style={{ marginTop: 20, fontSize: 10.5, color: PALETTE.textMuted, textAlign: 'center' }}>
        Dati dimostrativi generati deterministicamente per questo mockup — in produzione alimentati da utils/stats_engine.py
      </div>
    </div>
  );
}
