// EV Scanner - RELAY per le API bloccate da InfinityFree
// =============================================================================
// PERCHE' ESISTE QUESTO SCRIPT
// InfinityFree blocca a livello DNS le connessioni in uscita verso
// api.telegram.org (errore cURL #6 "Could not resolve host" nei log del server
// ad ogni ciclo). Non e' un limite generico dell'hosting — odds-api.io,
// api-sports.io e Betfair funzionano perfettamente dallo stesso account — ma un
// blocco MIRATO su Telegram e Discord, che InfinityFree applica perche' quei
// domini venivano usati in massa per spam bot. Lo staff ha dichiarato che e'
// "tecnicamente impossibile fare eccezioni per singoli account". Stesso
// trattamento sembra riguardare Sofascore.
//
// Questo runner GitHub, invece, ha internet senza restrizioni. Quindi:
//   PHP su InfinityFree = decide COSA fare (logica di business: quali bet
//                         notificare, con che testo, quali votare)
//   questo script       = esegue le CHIAMATE USCENTI vietate al server, e
//                         riferisce l'esito
//
// FLUSSO (3 passi):
//   1. Legge la coda (?action=relay_queue)          (navigazione stealth)
//   2. Chiama Telegram / Sofascore direttamente da qui
//   3. Conferma gli esiti (?action=relay_ack)        (navigazione stealth)
//
// PERCHE' "NAVIGAZIONE STEALTH" E NON UNA NORMALE fetch()
// InfinityFree ha anche un "Browser Security System" che blocca le richieste in
// INGRESSO non provenienti da un browser vero (cURL, bot, webhook). Una fetch()
// verso il nostro stesso hosting verrebbe respinta con una JS-challenge. Per
// questo si riusa lo stesso Chromium headless con spoofing gia' collaudato in
// trigger.js: e' l'unico modo per parlare col nostro PHP da qui.
//
// SEGRETI: token Telegram e Sofascore NON arrivano dal server, vivono come
// Secret del repository (vedi README). Cosi' non transitano in una risposta
// HTTP ne' rischiano di finire nei log pubblici della run.
// =============================================================================

const { chromium } = require('playwright');

// Entrambe puntano allo STESSO file (worker/resolve_bets_web.php), che su questo
// hosting e' l'unico endpoint del worker raggiungibile dall'esterno in modo
// affidabile: cambia solo il parametro action.
//   QUEUE: .../worker/resolve_bets_web.php?token=XXX&action=relay_queue
//   ACK:   .../worker/resolve_bets_web.php?token=XXX&action=relay_ack
const QUEUE_URL = process.env.RELAY_QUEUE_URL;
const ACK_URL = process.env.RELAY_ACK_URL;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID || '';
const SOFASCORE_TOKEN = process.env.SOFASCORE_TOKEN || '';

// Soglia di similarita' nomi squadra sotto la quale NON si vota: identica a
// SofascoreClient::MIN_MATCH_SCORE lato PHP. Meglio saltare una bet che votare
// la partita sbagliata — un voto su Sofascore non e' annullabile via API.
const MIN_MATCH_SCORE = 75.0;

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

function ts() {
    return new Date().toLocaleString('it-IT', { timeZone: 'Europe/Rome' });
}
function log(msg) { console.log(`[relay] [${ts()}] ${msg}`); }
function warn(msg) { console.warn(`[relay] [${ts()}] ${msg}`); }

// =============================================================================
// MATCHING NOMI SQUADRA
// Replica fedele di OddsMatcher::normalizeTeam()/teamSimilarity() lato PHP:
// deve dare gli STESSI risultati, altrimenti il relay voterebbe partite che il
// codice PHP avrebbe scartato (o viceversa), rendendo il comportamento
// imprevedibile a seconda di chi esegue il voto.
// =============================================================================
const ACCENTI = {
    'à': 'a', 'á': 'a', 'â': 'a', 'ä': 'a',
    'è': 'e', 'é': 'e', 'ê': 'e', 'ë': 'e',
    'ì': 'i', 'í': 'i', 'î': 'i', 'ï': 'i',
    'ò': 'o', 'ó': 'o', 'ô': 'o', 'ö': 'o',
    'ù': 'u', 'ú': 'u', 'û': 'u', 'ü': 'u',
    'ñ': 'n', 'ç': 'c',
};

function normalizeText(name) {
    let s = String(name || '').toLowerCase();
    s = s.replace(/[àáâäèéêëìíîïòóôöùúûüñç]/g, (c) => ACCENTI[c] || c);
    s = s.replace(/[^a-z0-9 ]/g, '');
    s = s.replace(/\s+/g, ' ');
    return s.trim();
}

function normalizeTeam(name) {
    let s = normalizeText(name);
    // Come lato PHP: NON si rimuovono "united"/"city" ecc., per molti club fanno
    // parte del nome distintivo (Manchester United, Leeds United...).
    s = s.replace(/\b(fc|cf|sc|cd|ac|afc|cfc)\b/g, '');
    s = s.replace(/\s+/g, ' ');
    return s.trim();
}

/**
 * Equivalente di similar_text() di PHP: piu' lunga sottostringa comune, poi
 * ricorsione sulle due code. Restituisce il numero di caratteri "in comune".
 */
function similarTextChars(a, b) {
    if (!a.length || !b.length) return 0;

    let max = 0, posA = 0, posB = 0;
    for (let i = 0; i < a.length; i++) {
        for (let j = 0; j < b.length; j++) {
            let k = 0;
            while (i + k < a.length && j + k < b.length && a[i + k] === b[j + k]) k++;
            if (k > max) { max = k; posA = i; posB = j; }
        }
    }
    if (max === 0) return 0;

    return max
        + similarTextChars(a.slice(0, posA), b.slice(0, posB))
        + similarTextChars(a.slice(posA + max), b.slice(posB + max));
}

function levenshtein(a, b) {
    if (a === b) return 0;
    if (!a.length) return b.length;
    if (!b.length) return a.length;

    let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
        const cur = [i];
        for (let j = 1; j <= b.length; j++) {
            cur[j] = Math.min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)
            );
        }
        prev = cur;
    }
    return prev[b.length];
}

function hybridSimilarity(a, b) {
    const common = similarTextChars(a, b);
    const simPct = (a.length + b.length) > 0 ? (common * 2 / (a.length + b.length)) * 100 : 0;

    const maxLen = Math.max(a.length, b.length);
    let levPct = 0;
    if (maxLen > 0) {
        const lev = levenshtein(a, b);
        levPct = (1 - Math.min(lev, maxLen) / maxLen) * 100;
    }
    return (simPct + levPct) / 2;
}

function teamSimilarity(a, b) {
    if (!a || !b) return 0;
    if (a === b) return 100;
    let score = hybridSimilarity(a, b);
    if (a.length >= 3 && b.length >= 3 && (a.includes(b) || b.includes(a))) {
        score = Math.max(score, 90);
    }
    return score;
}

// =============================================================================
// NAVIGAZIONE STEALTH verso il nostro PHP
// =============================================================================
async function stealthGetJson(browser, url, label) {
    const context = await browser.newContext({
        userAgent: UA,
        viewport: { width: 1920, height: 1080 },
        locale: 'it-IT',
        timezoneId: 'Europe/Rome',
        extraHTTPHeaders: {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
            'Upgrade-Insecure-Requests': '1',
        },
    });
    const page = await context.newPage();
    // Cancella la traccia "navigator.webdriver" PRIMA che parta la JS-challenge.
    await page.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    });

    try {
        const resp = await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
        const status = resp ? resp.status() : 0;
        // Micro-movimento + pausa: lascia stabilizzare i cookie generati dalla
        // challenge anti-bot prima di leggere il contenuto finale.
        await page.mouse.move(200, 200);
        await page.waitForTimeout(4000);

        const body = (await page.textContent('body')) || '';
        log(`${label}: HTTP ${status}, ${body.length} byte ricevuti.`);

        try {
            return JSON.parse(body.trim());
        } catch (e) {
            warn(`${label}: risposta non e' JSON valido. Probabile challenge anti-bot non superata o URL/token errato. Contenuto:\n${body.trim().slice(0, 800)}`);
            return null;
        }
    } catch (e) {
        warn(`${label}: navigazione fallita — ${e.message}`);
        return null;
    } finally {
        await context.close();
    }
}

// =============================================================================
// TELEGRAM
// =============================================================================
async function sendTelegram(text) {
    const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            chat_id: TELEGRAM_CHAT_ID,
            text,
            parse_mode: 'HTML',
            disable_web_page_preview: true,
        }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok !== true) {
        throw new Error(`HTTP ${res.status} — ${data.description || 'risposta non valida'}`);
    }
    return true;
}

// =============================================================================
// SOFASCORE
// =============================================================================
const sofascoreCache = new Map();

// -----------------------------------------------------------------------------
// PERCHE' SOFASCORE PASSA DAL BROWSER E NON DA fetch()
// Il primo tentativo usava una normale fetch() dal runner: Sofascore rispondeva
// HTTP 403 su OGNI richiesta (0 candidati esaminati su tutte le 24 bet). Non e'
// un problema di configurazione ne' di token: e' l'anti-bot di Sofascore che
// rifiuta gli IP dei runner GitHub (datacenter Azure) e le richieste prive di
// un fingerprint TLS/browser credibile.
//
// Qui si riusa il Chromium headless che gia' serve per parlare col nostro PHP:
// si apre una vera pagina sofascore.com e le chiamate all'API partono da DENTRO
// quella pagina (page.evaluate). Cosi' la richiesta ha fingerprint TLS reale,
// cookie di sessione autentici e Origin same-origin — indistinguibile da quella
// che farebbe il sito stesso mentre lo usi.
// -----------------------------------------------------------------------------
let sofascorePage = null;
// Diventa true se l'API risponde con un errore di accesso (403/429): in quel
// caso NON abbiamo davvero cercato le partite, quindi sarebbe sbagliato
// marcarle come "nessun match trovato" — meglio interrompere e ritentare al
// ciclo successivo, lasciandole in coda intatte.
let sofascoreBloccato = false;

async function getSofascorePage(browser) {
    if (sofascorePage) return sofascorePage;

    const context = await browser.newContext({
        userAgent: UA,
        viewport: { width: 1920, height: 1080 },
        locale: 'it-IT',
        timezoneId: 'Europe/Rome',
    });
    const page = await context.newPage();
    await page.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    });

    log('Sofascore: apro una sessione browser reale su sofascore.com...');
    await page.goto('https://www.sofascore.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
    // Lascia che partano gli script del sito e si depositino i cookie: e'
    // proprio quello che rende la sessione credibile per l'anti-bot.
    await page.waitForTimeout(4000);

    sofascorePage = page;
    return page;
}

/** GET dell'API Sofascore eseguita da dentro la pagina (same-origin). */
async function sofascoreApiGet(browser, path) {
    const page = await getSofascorePage(browser);
    return await page.evaluate(async (p) => {
        try {
            const r = await fetch(p, { headers: { 'Accept': '*/*' } });
            if (!r.ok) return { ok: false, status: r.status };
            return { ok: true, status: r.status, data: await r.json() };
        } catch (e) {
            return { ok: false, status: 0, error: String(e && e.message) };
        }
    }, path);
}

async function sofascoreEventsForDate(browser, date) {
    if (sofascoreCache.has(date)) return sofascoreCache.get(date);

    let events = [];
    const res = await sofascoreApiGet(browser, `/api/v1/sport/football/scheduled-events/${date}`);
    if (res.ok && res.data && Array.isArray(res.data.events)) {
        events = res.data.events;
        log(`Sofascore: ${events.length} partite in palinsesto per ${date}.`);
    } else {
        if (res.status === 403 || res.status === 429) sofascoreBloccato = true;
        warn(`Sofascore scheduled-events(${date}): HTTP ${res.status}${res.error ? ' — ' + res.error : ''}. Se resta 403 anche da dentro il browser, Sofascore sta bloccando l'intero range di IP dei runner GitHub e il voto automatico non e' praticabile da qui.`);
    }

    sofascoreCache.set(date, events);
    return events;
}

function ymd(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/**
 * Cerca l'id evento Sofascore per data + nomi squadra. Guarda anche il giorno
 * prima e dopo: Sofascore ragiona in UTC mentre le nostre date sono
 * Europe/Rome, quindi una partita in tarda serata puo' "scivolare" di giorno.
 */
async function findSofascoreEventId(browser, home, away, dateStr) {
    const dt = new Date(dateStr.replace(' ', 'T'));
    if (isNaN(dt.getTime())) return { id: null, candidates: 0, best: 0 };

    const nHome = normalizeTeam(home);
    const nAway = normalizeTeam(away);
    if (!nHome || !nAway) return { id: null, candidates: 0, best: 0 };

    let best = 0, bestId = null, candidates = 0;

    for (const offset of [0, -1, 1]) {
        const d = new Date(dt.getTime() + offset * 86400000);
        const events = await sofascoreEventsForDate(browser, ymd(d));
        for (const ev of events) {
            const evHome = ev?.homeTeam?.name || '';
            const evAway = ev?.awayTeam?.name || '';
            if (!evHome || !evAway) continue;
            candidates++;

            const score = (teamSimilarity(nHome, normalizeTeam(evHome)) + teamSimilarity(nAway, normalizeTeam(evAway))) / 2;
            if (score > best) { best = score; bestId = ev.id ? Number(ev.id) : null; }
        }
    }

    return { id: (bestId && best >= MIN_MATCH_SCORE) ? bestId : null, candidates, best: Math.round(best) };
}

/** POST del voto, anch'esso da dentro la pagina sofascore.com (vedi sopra). */
async function sofascoreVote(browser, eventId, vote) {
    const page = await getSofascorePage(browser);
    const res = await page.evaluate(async ({ id, v, token }) => {
        try {
            const r = await fetch(`/api/v1/event/${id}/vote`, {
                method: 'POST',
                headers: {
                    'Accept': '*/*',
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${token}`,
                },
                body: JSON.stringify({ vote: String(v).toUpperCase(), type: 1 }),
            });
            return { ok: r.status >= 200 && r.status < 300, status: r.status, body: (await r.text()).slice(0, 150) };
        } catch (e) {
            return { ok: false, status: 0, body: String(e && e.message) };
        }
    }, { id: eventId, v: vote, token: SOFASCORE_TOKEN });

    if (!res.ok) throw new Error(`HTTP ${res.status} ${res.body}`);
    return true;
}

// =============================================================================
// MAIN
// =============================================================================
(async () => {
    if (!QUEUE_URL || !ACK_URL) {
        console.error('RELAY_QUEUE_URL e RELAY_ACK_URL devono essere impostati come secret del repository. Interrompo.');
        process.exit(1);
    }

    const browser = await chromium.launch({
        headless: true,
        args: ['--disable-blink-features=AutomationControlled', '--disable-infobars', '--no-sandbox'],
    });

    const tgOk = [], sfOk = [], sfNo = [], sfErr = [];

    try {
        const queue = await stealthGetJson(browser, QUEUE_URL, 'Lettura coda');
        if (!queue || queue.success !== true) {
            console.error('Coda non leggibile o risposta non valida dal server. Interrompo senza modificare nulla.');
            process.exit(1);
        }

        // ----- TELEGRAM -----
        const messages = queue.telegram?.messages || [];
        if (!queue.telegram?.enabled) {
            log('Telegram: disattivato nelle impostazioni, niente da fare.');
        } else if (messages.length === 0) {
            log(`Telegram: nessun messaggio in coda (${queue.telegram.scartate || 0} bet scartate dai filtri lato server).`);
        } else if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
            warn(`Telegram: ${messages.length} messaggi in coda ma i secret TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID non sono configurati. Salto (le bet restano in coda per il prossimo ciclo).`);
        } else {
            log(`Telegram: invio ${messages.length} messaggi...`);
            for (const m of messages) {
                try {
                    await sendTelegram(m.text);
                    tgOk.push(m.id);
                    // Telegram limita a ~30 messaggi/secondo; 350ms e' molto
                    // sotto la soglia e tiene il bot lontano da qualunque
                    // rate-limit anche con code lunghe.
                    await new Promise(r => setTimeout(r, 350));
                } catch (e) {
                    // Non si prosegue: se Telegram rifiuta, quasi sempre e' un
                    // problema che riguardera' anche i messaggi successivi
                    // (token errato, chat sbagliata, rate-limit). Le bet non
                    // confermate restano in coda e si ritenta al prossimo ciclo.
                    warn(`Telegram: invio bet #${m.id} fallito — ${e.message}. Interrompo l'invio, le restanti restano in coda.`);
                    break;
                }
            }
            log(`Telegram: ${tgOk.length}/${messages.length} inviati con successo.`);
        }

        // ----- SOFASCORE -----
        const sfBets = queue.sofascore?.bets || [];
        if (!queue.sofascore?.enabled) {
            log('Sofascore: disattivato nelle impostazioni, niente da fare.');
        } else if (sfBets.length === 0) {
            log('Sofascore: nessuna bet da votare.');
        } else if (!SOFASCORE_TOKEN) {
            warn(`Sofascore: ${sfBets.length} bet da votare ma il secret SOFASCORE_TOKEN non e' configurato. Salto.`);
        } else {
            log(`Sofascore: elaboro ${sfBets.length} bet...`);
            for (const b of sfBets) {
                if (sofascoreBloccato) {
                    warn('Sofascore: accesso bloccato (403/429), interrompo. Le bet restanti restano in coda intatte e verranno ritentate al prossimo ciclo — non le marco come "nessun match" perche\' non sono mai state davvero cercate.');
                    break;
                }
                try {
                    const { id, candidates, best } = await findSofascoreEventId(browser, b.home, b.away, b.date);
                    if (!id) {
                        log(`Sofascore: nessun match per "${b.home} - ${b.away}" (${candidates} candidati esaminati, miglior punteggio ${best}/100, soglia ${MIN_MATCH_SCORE}).`);
                        sfNo.push(b.id);
                        continue;
                    }
                    await sofascoreVote(browser, id, b.selection);
                    log(`Sofascore: votato "${b.selection}" su ${b.home} - ${b.away} (evento ${id}, match ${best}/100).`);
                    sfOk.push(b.id);
                } catch (e) {
                    warn(`Sofascore: voto bet #${b.id} fallito — ${e.message}`);
                    sfErr.push(b.id);
                }
                // Endpoint non ufficiale: pausa fra un voto e l'altro per non
                // martellarlo con richieste a raffica (rischio blocco).
                await new Promise(r => setTimeout(r, 600));
            }
            log(`Sofascore: ${sfOk.length} votate, ${sfNo.length} senza match, ${sfErr.length} errori.`);
        }

        // ----- ACK -----
        if (tgOk.length || sfOk.length || sfNo.length || sfErr.length) {
            const sep = ACK_URL.includes('?') ? '&' : '?';
            const params = [];
            if (tgOk.length) params.push(`tg_ok=${tgOk.join(',')}`);
            if (sfOk.length) params.push(`sf_ok=${sfOk.join(',')}`);
            if (sfNo.length) params.push(`sf_no=${sfNo.join(',')}`);
            if (sfErr.length) params.push(`sf_err=${sfErr.join(',')}`);

            const ackResp = await stealthGetJson(browser, `${ACK_URL}${sep}${params.join('&')}`, 'Conferma esiti');
            if (ackResp && ackResp.success) {
                log(`ACK registrato: ${JSON.stringify(ackResp)}`);
            } else {
                // Situazione da conoscere: il lavoro E' stato fatto (messaggi
                // inviati, voti registrati su Sofascore) ma il server non l'ha
                // saputo. Al prossimo ciclo quelle bet torneranno in coda e
                // verranno rifatte — per Telegram significa un messaggio
                // doppio, per Sofascore un voto gia' espresso (innocuo).
                warn('ACK NON registrato: il lavoro e\' stato eseguito ma il server non ne e\' stato informato. Al prossimo ciclo verra\' ritentato (possibili duplicati su Telegram).');
            }
        } else {
            log('Nessun esito da confermare, ACK non necessario.');
        }
    } finally {
        await browser.close();
    }

    log('Relay completato.');
})();
