// EV Scanner — PONTE FOREBET
// =============================================================================
// PERCHE' ESISTE QUESTO SCRIPT
//
// Forebet risponde HTTP 403 a qualunque richiesta partita da InfinityFree:
// l'IP condiviso di byetcluster e' filtrato dal loro WAF. Nei log del server si
// vede il fallimento istantaneo su entrambe le URL, "oggi" e per-data. Non e'
// un problema di header o user-agent, e nessuna modifica al PHP puo' aggirarlo:
// la pagina non arriva proprio.
//
// Stessa forma del relay Telegram (relay.js), stessa divisione dei compiti:
//   questo runner  -> SCARICA le pagine (internet senza restrizioni)
//   PHP sul server -> INTERPRETA e SALVA (dove vivono dati e logica)
//
// IL PARSING NON E' DUPLICATO QUI. Sarebbe stato piu' comodo estrarre le
// percentuali dentro il browser, che ha gia' il DOM in mano — ed e'
// esattamente per questo che non si fa: due parser in due linguaggi
// divergono in silenzio, e quello sbagliato continuerebbe a produrre numeri
// dall'aria credibile. L'HTML grezzo viene spedito al server, dove gira
// l'unico parser esistente con le sue validazioni (somma a 100, rifiuto
// delle righe ambigue, guardia anti-leakage sul post-inizio).
//
// PERCHE' LA POST PARTE DA DENTRO LA PAGINA
// InfinityFree respinge le richieste in INGRESSO che non vengono da un browser
// vero. Il trucco gia' collaudato e': aprire con Chromium una pagina del NOSTRO
// dominio (che supera la JS-challenge e riceve i cookie), e da li' fare una
// fetch() same-origin. La POST eredita quei cookie e passa.
//
// SEGRETI: FOREBET_INGEST_URL vive come Secret del repository, non nel codice.
// =============================================================================

const { chromium } = require('playwright');

// Stesso file di tutti gli altri: su questo hosting e' l'unico endpoint
// raggiungibile dall'esterno in modo affidabile.
//   .../worker/resolve_bets_web.php?token=XXX&action=forebet_ingest
const INGEST_URL = process.env.FOREBET_INGEST_URL;

// Quanti giorni raccogliere: oggi + i successivi. Due bastano — le partite
// piu' lontane verranno prese dai cicli dei prossimi giorni, comunque prima
// del calcio d'inizio, che e' l'unica cosa che conta per la calibrazione.
const GIORNI = parseInt(process.env.FOREBET_GIORNI || '2', 10);

const NAV_TIMEOUT_MS = 90000;
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
           '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

const ts = () => new Date().toLocaleString('it-IT');
const log = (m) => console.log(`[${ts()}] ${m}`);

function urlForebet(offsetGiorni) {
    if (offsetGiorni === 0) {
        return {
            etichetta: 'oggi',
            url: 'https://www.forebet.com/it/pronostici-calcistici-per-oggi/pronostici-1x2',
        };
    }
    const d = new Date();
    d.setDate(d.getDate() + offsetGiorni);
    const iso = d.toISOString().slice(0, 10);
    return {
        etichetta: iso,
        url: `https://www.forebet.com/it/pronostici-calcistici/pronostici-1x2/${iso}`,
    };
}

async function scaricaPagina(context, etichetta, url) {
    const page = await context.newPage();
    try {
        log(`[${etichetta}] Apertura ${url}`);
        const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
        const status = resp ? resp.status() : 0;
        log(`[${etichetta}] HTTP ${status}`);

        if (status !== 200) {
            log(`[${etichetta}] SALTATA: risposta non valida.`);
            return null;
        }

        // Le righe sono nell'HTML servito dal server, ma un paio di secondi
        // di respiro evitano di catturare la pagina a meta' render.
        await page.waitForTimeout(3000);

        // Si rimuovono script e stili PRIMA di prendere l'HTML: sono la
        // maggior parte del peso e non contengono nessuna riga partita.
        // Serve a non spedire un megabyte inutile a un hosting gratuito.
        const html = await page.evaluate(() => {
            document.querySelectorAll('script, style, noscript, iframe, svg').forEach(n => n.remove());
            return document.documentElement.outerHTML;
        });

        log(`[${etichetta}] HTML catturato: ${html.length} byte`);

        // Una pagina di blocco e' corta. Meglio accorgersene qui che
        // spedire spazzatura e vedere "zero righe" dall'altra parte.
        if (html.length < 20000) {
            log(`[${etichetta}] ATTENZIONE: HTML sospettosamente corto, probabile pagina di blocco.`);
        }

        return html;
    } catch (e) {
        log(`[${etichetta}] ERRORE nel download: ${e.message}`);
        return null;
    } finally {
        await page.close().catch(() => {});
    }
}

async function spedisci(context, etichetta, html) {
    const page = await context.newPage();
    try {
        // 1) Si apre l'endpoint in GET: serve a superare la JS-challenge di
        //    InfinityFree e a ottenere i cookie di sessione.
        log(`[${etichetta}] Apertura endpoint per la challenge anti-bot...`);
        await page.goto(INGEST_URL, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
        await page.waitForTimeout(4000);

        // 2) POST same-origin da DENTRO la pagina: eredita i cookie appena
        //    ottenuti, quindi non viene respinta.
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
        } catch (e) {
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

(async () => {
    if (!INGEST_URL) {
        console.error('FOREBET_INGEST_URL non configurata: aggiungila ai Secret del repository.');
        process.exit(1);
    }

    log(`Avvio ponte Forebet — ${GIORNI} giorno/i da raccogliere`);

    const browser = await chromium.launch({
        args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
    });
    const context = await browser.newContext({
        userAgent: UA,
        locale: 'it-IT',
        timezoneId: 'Europe/Rome',
        viewport: { width: 1920, height: 1080 },
    });

    // Nasconde navigator.webdriver, il controllo piu' banale fatto dai WAF.
    await context.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    });

    let ok = 0, falliti = 0;

    try {
        for (let g = 0; g < GIORNI; g++) {
            const { etichetta, url } = urlForebet(g);

            const html = await scaricaPagina(context, etichetta, url);
            if (!html) { falliti++; continue; }

            const esito = await spedisci(context, etichetta, html);
            esito ? ok++ : falliti++;

            // Pausa fra una pagina e l'altra: non serve fare i molesti.
            if (g < GIORNI - 1) await new Promise(r => setTimeout(r, 3000));
        }
    } finally {
        await context.close().catch(() => {});
        await browser.close().catch(() => {});
    }

    log(`Ponte Forebet completato. Riuscite: ${ok}, fallite: ${falliti}.`);

    // Si esce con errore solo se NON e' passato proprio niente: una singola
    // data mancante non deve far diventare rossa tutta la run.
    process.exit(ok > 0 ? 0 : 1);
})();
