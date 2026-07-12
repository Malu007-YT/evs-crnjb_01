// Trigger dei worker EV Scanner tramite browser headless reale.
//
// Perche' un browser vero e non una semplice richiesta HTTP: gli endpoint
// vivono su un hosting che protegge le richieste esterne con una
// JS-challenge anti-bot (slowAES): serve eseguire davvero il JavaScript
// della pagina di sfida, ottenere il cookie che genera, e seguire il
// redirect automatico che porta al vero script PHP — esattamente cio' che
// fa un browser normale, e cio' che nessun cron "HTTP puro" (cron-job.org,
// curl, wget...) puo' fare.
//
// Richiama IN SEQUENZA due endpoint (mai in parallelo: lo scan e' gia' di
// per se' un'operazione lenta — login + parsing + matching quote — meglio
// non sommarci il carico di un'altra richiesta pesante nello stesso istante):
//   1) SCAN_URL   -> worker/scan_web.php: scansiona Sbancobet, aggiorna gli
//      eventi e le candidate/value bet (Auto-Tracking se attivo).
//   2) WORKER_URL -> worker/resolve_bets_web.php: risolve gli esiti delle
//      bet scadute, ricontrolla le void recenti, cattura il CLV.
// Ha senso in quest'ordine: prima si cerca cosa c'e' di nuovo da
// scommettere, poi si sistema cosa e' concluso nel frattempo.

const { chromium } = require('playwright');

const URLS = [
    { name: 'scan (Sbancobet + quote)', url: process.env.SCAN_URL },
    { name: 'resolve esiti + CLV', url: process.env.WORKER_URL },
].filter(u => !!u.url);

if (URLS.length === 0) {
    console.error('Nessuna variabile d\'ambiente SCAN_URL/WORKER_URL impostata: configura almeno una come secret del repository GitHub.');
    process.exit(1);
}

/**
 * Visita un URL protetto dalla JS-challenge anti-bot con un browser
 * headless reale, e verifica che la risposta finale sia il JSON del
 * worker (non la pagina della sfida, che risponde con text/html).
 */
async function triggerUrl(browser, name, url) {
    const page = await browser.newPage({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    });

    let finalResponseBody = null;
    let finalStatus = null;
    let finalContentType = null;

    page.on('response', async (response) => {
        try {
            const contentType = response.headers()['content-type'] || '';
            if (contentType.includes('application/json')) {
                finalStatus = response.status();
                finalContentType = contentType;
                finalResponseBody = await response.text();
            }
        } catch (e) {
            // risposta non piu' leggibile (es. redirect concluso nel frattempo): ignora
        }
    });

    try {
        await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
        await page.waitForTimeout(4000); // margine per il redirect della sfida anti-bot
    } catch (e) {
        console.error(`[${name}] Errore durante la navigazione:`, e.message);
    }

    await page.close();

    if (finalResponseBody && finalContentType && finalContentType.includes('application/json')) {
        console.log(`[${name}] OK (HTTP ${finalStatus}):`);
        console.log(finalResponseBody);
        return true;
    }

    console.error(`[${name}] Nessuna risposta JSON valida: probabile blocco anti-bot non superato o URL/token errati.`);
    console.error(`[${name}] Ultimo status visto:`, finalStatus, '- content-type:', finalContentType);
    return false;
}

(async () => {
    const browser = await chromium.launch();
    let allOk = true;

    for (const { name, url } of URLS) {
        const ok = await triggerUrl(browser, name, url);
        allOk = allOk && ok;
    }

    await browser.close();
    process.exit(allOk ? 0 : 1);
})();