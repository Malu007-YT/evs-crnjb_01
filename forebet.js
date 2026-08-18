// EV Scanner — PONTE FOREBET (rotazione proxy con memoria)
// =============================================================================
// PERCHE' ESISTE, E PERCHE' HA I PROXY
//
// Forebet risponde HTTP 403 istantaneo a qualunque richiesta proveniente da un
// IP di datacenter. Due strade gia' provate e fallite:
//   1. richiesta diretta da InfinityFree (byetcluster)  -> 403
//   2. richiesta da un runner GitHub Actions (Azure)    -> 403
// In entrambi i casi il rifiuto arriva nello stesso secondo della richiesta: e'
// un blocco al bordo basato sulla reputazione dell'IP, non sugli header ne' sul
// comportamento. Nessuna modifica al codice puo' aggirarlo — serve un IP altro.
//
// Le liste pubbliche contengono migliaia di proxy aperti, tra cui parecchi
// residenziali e mobili: quelli hanno la reputazione che serve. In cambio sono
// inaffidabili per costruzione. La strategia non e' "trovare un buon proxy" ma
// "provarne tanti finche' uno passa".
//
// -----------------------------------------------------------------------------
// COME SI EVITA DI SPRECARE TENTATIVI (v3)
//
// 1. MEMORIA TRA UNA RUN E L'ALTRA. I proxy che hanno superato il WAF vengono
//    salvati e riprovati per PRIMI al giro successivo. Un IP che ieri non era
//    in lista nera quasi sempre non lo e' neanche oggi: e' l'informazione piu'
//    preziosa che questo script produce, e buttarla via ogni volta significava
//    ricominciare da zero ogni mattina. Si ricordano anche i BLOCCATI, per non
//    ripresentarsi a bussare a porte che hanno gia' detto no.
//
// 2. UN PROXY BUONO SI RIUSA PER TUTTE LE PAGINE. Prima ogni pagina ripartiva
//    dalla cima della lista mescolata: trovato l'IP buono per "oggi", lo si
//    buttava e si ricominciava a tentare per "domani". Ora il vincitore resta
//    in mano e serve tutte le pagine del giro.
//
// 3. LE ONDATE SI INTERROMPONO AL PRIMO SUCCESSO. Con Promise.all si aspettava
//    comunque la fine di tutti e 25 i tentativi, anche quando il primo aveva
//    gia' consegnato la pagina: fino a 12 secondi di attesa per niente, e 24
//    download inutili verso un sito che non ci deve nulla.
//
// 4. TESTA PRIMA, SCARICA DOPO. La verifica se un proxy passa si fa con una
//    richiesta leggera; solo il vincitore scarica le pagine intere. Evita di
//    tirare giu' centinaia di KB da venticinque proxy in parallelo per poi
//    tenerne uno.
// -----------------------------------------------------------------------------
// DUE REGOLE DI SICUREZZA, NON NEGOZIABILI
//
// 1. L'URL DI INGEST NON PASSA MAI DAL PROXY. Contiene il token del worker. I
//    proxy scaricano solo Forebet, che e' un sito pubblico; la POST verso il
//    nostro server esce diretta dal runner.
//
// 2. SOLO HTTPS, E LA VERIFICA DEL CERTIFICATO NON SI TOCCA. Su HTTPS il proxy
//    fa da tunnel cieco (CONNECT) e non puo' leggere ne' modificare l'HTML.
//    Mettere rejectUnauthorized:false farebbe passare piu' proxy e aprirebbe la
//    porta a un intermediario che inietta righe false — che il parser NON
//    distinguerebbe, perche' una partita inventata con percentuali che sommano
//    a 100 supera la cifra di controllo senza problemi. Se il tasso di successo
//    sembra basso si alza FOREBET_MAX_PROXY: non si tocca questa riga.
// -----------------------------------------------------------------------------
// RICHIEDE: undici in package.json. Playwright serve per la POST finale, che
// deve superare la JS-challenge di InfinityFree.
// =============================================================================

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { request, ProxyAgent } = require('undici');

const INGEST_URL = process.env.FOREBET_INGEST_URL;
const GIORNI = parseInt(process.env.FOREBET_GIORNI || '2', 10);
const MAX_PROXY = parseInt(process.env.FOREBET_MAX_PROXY || '300', 10);

// Cartella messa in cache da GitHub Actions fra una run e l'altra.
const CARTELLA_MEMORIA = process.env.FOREBET_CACHE_DIR || '.proxy-cache';
const FILE_MEMORIA = path.join(CARTELLA_MEMORIA, 'proxy-memoria.json');

const MAX_BUONI_RICORDATI = 40;
const MAX_BLOCCATI_RICORDATI = 800;
// Dopo qualche giorno un IP pubblico cambia mano o cambia reputazione: tenerlo
// in memoria oltre questa soglia significherebbe fidarsi di un'informazione
// scaduta, sia in positivo che in negativo.
const GIORNI_VALIDITA_MEMORIA = 5;

const LISTA_PROXY = 'https://api.proxyscrape.com/v4/free-proxy-list/get'
    + '?request=display_proxies&proxy_format=protocolipport&format=text'
    + '&protocol=http&timeout=5000';

// URL leggera usata SOLO per capire se il proxy supera il WAF. E' una pagina
// del sito, non un asset statico: le immagini spesso stanno dietro una CDN con
// regole diverse, e risponderebbero 200 anche da un IP che sulle pagine viene
// bloccato — un test che dice sempre di si' non e' un test.
const URL_SONDA = 'https://www.forebet.com/it/cosa-e-il-forebet';

const CONCORRENZA = 25;
const TIMEOUT_SONDA_MS = 9000;
const TIMEOUT_DOWNLOAD_MS = 25000;
const MIN_BYTE_HTML = 20000;

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
         + '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

const ts = () => new Date().toLocaleString('it-IT');
const log = (m) => console.log(`[${ts()}] ${m}`);

const INTESTAZIONI = {
    'user-agent': UA,
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'accept-language': 'it-IT,it;q=0.9,en;q=0.8',
    'upgrade-insecure-requests': '1',
};

function agentePer(proxyUrl) {
    return new ProxyAgent({
        uri: proxyUrl,
        requestTls: {
            // NON si tocca: vedi regola 2 in testa al file.
            rejectUnauthorized: true,
            servername: 'www.forebet.com',
        },
    });
}

// ============================================================================
// MEMORIA
// ============================================================================

function caricaMemoria() {
    try {
        const m = JSON.parse(fs.readFileSync(FILE_MEMORIA, 'utf8'));
        const limite = Date.now() - GIORNI_VALIDITA_MEMORIA * 86400000;

        const buoni = (m.buoni || []).filter(b => b.visto > limite);
        const bloccati = (m.bloccati || []).filter(b => b.visto > limite);

        log(`Memoria: ${buoni.length} proxy buoni, ${bloccati.length} bloccati noti `
            + `(scartati quelli piu' vecchi di ${GIORNI_VALIDITA_MEMORIA} giorni).`);
        return { buoni, bloccati };
    } catch {
        log('Memoria assente: prima run, oppure la cache e scaduta.');
        return { buoni: [], bloccati: [] };
    }
}

function salvaMemoria(mem) {
    try {
        fs.mkdirSync(CARTELLA_MEMORIA, { recursive: true });

        // I buoni si ordinano per successi e poi per data: se la lista va
        // tagliata, a cadere sono quelli che hanno funzionato meno.
        const buoni = mem.buoni
            .sort((a, b) => (b.successi - a.successi) || (b.visto - a.visto))
            .slice(0, MAX_BUONI_RICORDATI);

        const bloccati = mem.bloccati
            .sort((a, b) => b.visto - a.visto)
            .slice(0, MAX_BLOCCATI_RICORDATI);

        fs.writeFileSync(FILE_MEMORIA, JSON.stringify({ buoni, bloccati }, null, 1));
        log(`Memoria salvata: ${buoni.length} buoni, ${bloccati.length} bloccati.`);
    } catch (e) {
        log(`Memoria non salvata (${e.message}). Non e grave: la prossima run riparte dalla lista.`);
    }
}

function registraBuono(mem, proxy) {
    const e = mem.buoni.find(b => b.proxy === proxy);
    if (e) { e.successi++; e.visto = Date.now(); }
    else mem.buoni.push({ proxy, successi: 1, visto: Date.now() });

    // Se aveva funzionato, non e' bloccato: si toglie dall'altra lista.
    mem.bloccati = mem.bloccati.filter(b => b.proxy !== proxy);
}

function registraBloccato(mem, proxy) {
    if (!mem.bloccati.some(b => b.proxy === proxy)) {
        mem.bloccati.push({ proxy, visto: Date.now() });
    }
    // Un proxy che ora riceve 403 non e' piu' buono, per quanto lo sia stato.
    mem.buoni = mem.buoni.filter(b => b.proxy !== proxy);
}

// ============================================================================
// SONDA E DOWNLOAD
// ============================================================================

/**
 * Verifica se il proxy supera il WAF. Richiesta leggera: si guarda solo lo
 * stato, il corpo viene scartato senza leggerlo.
 */
async function sonda(proxyUrl, esiti, segnale) {
    let agent;
    try {
        agent = agentePer(proxyUrl);
        const r = await request(URL_SONDA, {
            dispatcher: agent,
            headers: INTESTAZIONI,
            headersTimeout: TIMEOUT_SONDA_MS,
            bodyTimeout: TIMEOUT_SONDA_MS,
            maxRedirections: 3,
            signal: segnale,
        });
        await r.body.dump();

        if (r.statusCode === 403) { esiti.bloccati++; return 'bloccato'; }
        if (r.statusCode !== 200)  { esiti.altri++;    return null; }

        esiti.ok++;
        return 'ok';
    } catch (e) {
        // AbortError = un altro proxy della stessa ondata ha gia' vinto: non e'
        // un fallimento e non va contato come tale, altrimenti la diagnosi
        // finale conterebbe come "morti" decine di proxy mai realmente provati.
        if (e.name !== 'AbortError') esiti.morti++;
        return null;
    } finally {
        if (agent) await agent.close().catch(() => {});
    }
}

/** Scarica una pagina intera attraverso un proxy gia' promosso dalla sonda. */
async function scaricaPagina(proxyUrl, url) {
    let agent;
    try {
        agent = agentePer(proxyUrl);
        const r = await request(url, {
            dispatcher: agent,
            headers: INTESTAZIONI,
            headersTimeout: TIMEOUT_DOWNLOAD_MS,
            bodyTimeout: TIMEOUT_DOWNLOAD_MS,
            maxRedirections: 3,
        });

        if (r.statusCode !== 200) { await r.body.dump(); return { errore: `HTTP ${r.statusCode}` }; }

        const html = await r.body.text();
        if (html.length < MIN_BYTE_HTML) return { errore: `pagina troppo corta (${html.length} byte)` };

        return { html };
    } catch (e) {
        return { errore: e.message };
    } finally {
        if (agent) await agent.close().catch(() => {});
    }
}

/**
 * Cerca UN proxy che superi il WAF. Prima quelli in memoria (uno alla volta:
 * sono pochi e hanno alta probabilita' di funzionare, tentarli in parallelo
 * sprecherebbe i piu' promettenti), poi la lista fresca a ondate parallele con
 * interruzione al primo successo.
 */
async function trovaProxyBuono(mem, esiti) {
    // --- 1. i ricordati ---
    for (const b of mem.buoni.sort((x, y) => (y.successi - x.successi) || (y.visto - x.visto))) {
        const esito = await sonda(b.proxy, esiti, undefined);
        if (esito === 'ok') {
            log(`Proxy dalla memoria: ${b.proxy} (gia' riuscito ${b.successi} volte). `
                + `Zero tentativi sprecati.`);
            return b.proxy;
        }
        if (esito === 'bloccato') registraBloccato(mem, b.proxy);
    }
    if (mem.buoni.length) log(`Nessuno dei ${mem.buoni.length} proxy in memoria funziona piu'.`);

    // --- 2. la lista fresca ---
    log('Scarico la lista proxy...');
    const r = await request(LISTA_PROXY, { headersTimeout: 20000, bodyTimeout: 20000 });
    if (r.statusCode !== 200) throw new Error(`lista proxy HTTP ${r.statusCode}`);
    const testo = await r.body.text();

    const noti = new Set([
        ...mem.bloccati.map(b => b.proxy),
        ...mem.buoni.map(b => b.proxy),   // gia' provati sopra
    ]);

    let lista = testo.split('\n')
        .map(s => s.trim())
        .filter(s => /^http:\/\/[\d.]+:\d+$/.test(s))
        .filter(s => !noti.has(s));

    // Mescolata: arriva sempre nello stesso ordine, e senza shuffle si
    // riproverebbero ogni giorno i proxy in cima — gli stessi che tutti gli
    // altri utenti della lista stanno martellando.
    for (let i = lista.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [lista[i], lista[j]] = [lista[j], lista[i]];
    }
    lista = lista.slice(0, MAX_PROXY);

    log(`Lista: ${lista.length} proxy nuovi da provare (esclusi ${noti.size} gia' noti).`);

    for (let i = 0; i < lista.length; i += CONCORRENZA) {
        const ondata = lista.slice(i, i + CONCORRENZA);
        const ac = new AbortController();

        const vincente = await new Promise((risolvi) => {
            let rimasti = ondata.length;
            for (const p of ondata) {
                sonda(p, esiti, ac.signal).then(esito => {
                    if (esito === 'ok') { ac.abort(); risolvi(p); return; }
                    if (esito === 'bloccato') registraBloccato(mem, p);
                    if (--rimasti === 0) risolvi(null);
                });
            }
        });

        if (vincente) {
            log(`Proxy trovato: ${vincente} dopo ${i + ondata.length} tentativi. `
                + `Ondata interrotta, gli altri tentativi annullati.`);
            return vincente;
        }
        log(`${i + ondata.length}/${lista.length} tentati — ${JSON.stringify(esiti)}`);
    }

    return null;
}

function alleggerisci(html) {
    // Regex e non parser DOM di proposito: NON e' estrazione di dati (quella
    // resta tutta lato PHP, in un posto solo), e' solo alleggerire il payload
    // verso un hosting gratuito togliendo cio' che non contiene partite.
    return html
        .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
        .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '')
        .replace(/<noscript\b[^>]*>[\s\S]*?<\/noscript>/gi, '');
}

function urlForebet(offset) {
    if (offset === 0) {
        return {
            etichetta: 'oggi',
            url: 'https://www.forebet.com/it/pronostici-calcistici-per-oggi/pronostici-1x2',
        };
    }
    const d = new Date();
    d.setDate(d.getDate() + offset);
    const iso = d.toISOString().slice(0, 10);
    return {
        etichetta: iso,
        url: `https://www.forebet.com/it/pronostici-calcistici/pronostici-1x2/${iso}`,
    };
}

/** POST al nostro server: DIRETTA, mai attraverso un proxy (regola 1). */
async function spedisci(context, etichetta, html) {
    const page = await context.newPage();
    try {
        await page.goto(INGEST_URL, { waitUntil: 'domcontentloaded', timeout: 90000 });
        await page.waitForTimeout(4000);

        log(`[${etichetta}] Invio ${html.length} byte al server...`);
        const risposta = await page.evaluate(async ({ url, html, etichetta }) => {
            const body = new URLSearchParams();
            body.set('html', html);
            body.set('data', etichetta);
            const r = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: body.toString(),
                credentials: 'same-origin',
            });
            return { status: r.status, testo: (await r.text()).slice(0, 4000) };
        }, { url: INGEST_URL, html, etichetta });

        log(`[${etichetta}] Risposta HTTP ${risposta.status}: ${risposta.testo}`);

        try {
            const j = JSON.parse(risposta.testo);
            return !!j.success && (j.righe || 0) > 0;
        } catch {
            log(`[${etichetta}] Risposta non JSON: il server ha risposto con una pagina di errore.`);
            return false;
        }
    } catch (e) {
        log(`[${etichetta}] ERRORE nell'invio: ${e.message}`);
        return false;
    } finally {
        await page.close().catch(() => {});
    }
}

// ============================================================================
(async () => {
    if (!INGEST_URL) {
        console.error('FOREBET_INGEST_URL non configurata: aggiungila ai Secret del repository.');
        process.exit(1);
    }

    log(`Avvio ponte Forebet — ${GIORNI} giorno/i`);

    const mem = caricaMemoria();
    const esiti = { ok: 0, bloccati: 0, morti: 0, altri: 0 };

    let proxy;
    try {
        proxy = await trovaProxyBuono(mem, esiti);
    } catch (e) {
        console.error(`Ricerca proxy fallita: ${e.message}`);
        salvaMemoria(mem);
        process.exit(1);
    }

    if (!proxy) {
        log(`NESSUN proxy e passato. Esiti: ${JSON.stringify(esiti)}`);
        // I contatori sono separati proprio per distinguere due fallimenti che
        // richiedono risposte opposte.
        if (esiti.bloccati > esiti.morti) {
            log(`Diagnosi: la maggioranza dei proxy VIVI ha ricevuto 403. Forebet blocca anche `
                + `questi IP: i proxy pubblici non bastano, servirebbe un pool residenziale a `
                + `pagamento o lo userscript dal tuo browser.`);
        } else {
            log(`Diagnosi: la maggioranza dei proxy era morta o irraggiungibile. E' qualita' `
                + `della lista, non un blocco: conviene rilanciare la run o alzare `
                + `FOREBET_MAX_PROXY.`);
        }
        salvaMemoria(mem);
        process.exit(1);
    }

    // Lo stesso proxy serve TUTTE le pagine: e' passato una volta, non c'e'
    // nessun motivo di rimettersi a cercarne un altro per la pagina seguente.
    const pagine = [];
    for (let g = 0; g < GIORNI; g++) {
        const { etichetta, url } = urlForebet(g);
        const r = await scaricaPagina(proxy, url);

        if (r.html) {
            log(`[${etichetta}] scaricata: ${r.html.length} byte`);
            pagine.push({ etichetta, html: alleggerisci(r.html) });
        } else {
            log(`[${etichetta}] fallita (${r.errore}). Le altre pagine proseguono.`);
        }
    }

    if (pagine.length > 0) registraBuono(mem, proxy);
    salvaMemoria(mem);

    if (pagine.length === 0) {
        log('Nessuna pagina scaricata: niente da spedire.');
        process.exit(1);
    }

    // Chromium si apre solo ora, e solo perche' c'e' davvero qualcosa da
    // spedire: avviarlo prima per poi scoprire che non era passato nessun
    // proxy sarebbe stato tempo e memoria buttati.
    const browser = await chromium.launch({
        args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
    });
    const context = await browser.newContext({
        userAgent: UA, locale: 'it-IT', timezoneId: 'Europe/Rome',
        viewport: { width: 1920, height: 1080 },
    });
    await context.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    });

    let ok = 0;
    try {
        for (const p of pagine) {
            if (await spedisci(context, p.etichetta, p.html)) ok++;
        }
    } finally {
        await context.close().catch(() => {});
        await browser.close().catch(() => {});
    }

    log(`Completato. Pagine consegnate: ${ok}/${pagine.length}. Proxy usato: ${proxy}`);
    process.exit(ok > 0 ? 0 : 1);
})();
