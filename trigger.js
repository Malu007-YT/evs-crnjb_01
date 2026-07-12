// Trigger del worker EV Scanner tramite browser headless reale.
//
// Perche' un browser vero e non una semplice richiesta HTTP: il worker
// (worker/resolve_bets_web.php) vive su un hosting che protegge le
// richieste esterne con una JS-challenge anti-bot (slowAES): serve
// eseguire davvero il JavaScript della pagina di sfida, ottenere il
// cookie che genera, e seguire il redirect automatico che porta al vero
// script PHP — esattamente cio' che fa un browser normale, e cio' che
// nessun cron "HTTP puro" (cron-job.org, curl, wget...) puo' fare.
//
// Il worker risponde in JSON puro (Content-Type: application/json) quando
// la richiesta arriva davvero allo script PHP: lo verifichiamo esplicitamente
// per essere sicuri che il trigger sia riuscito e non ci si sia fermati
// sulla pagina della sfida (che risponde con text/html).

const { chromium } = require('playwright');

const WORKER_URL = process.env.WORKER_URL;

if (!WORKER_URL) {
    console.error('Variabile d\'ambiente WORKER_URL mancante: impostala come secret del repository GitHub (Settings -> Secrets and variables -> Actions -> New repository secret).');
    process.exit(1);
}

(async () => {
    const browser = await chromium.launch();
    const page = await browser.newPage({
        // User-Agent "normale": alcuni sistemi anti-bot trattano in modo
        // diverso gli user-agent che si dichiarano esplicitamente bot/headless.
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    });

    let finalResponseBody = null;
    let finalStatus = null;
    let finalContentType = null;

    // Ascolta TUTTE le risposte di rete della pagina: la sfida anti-bot fa
    // un redirect JS (location.href = ...&i=1) verso l'URL vero, quindi la
    // risposta che ci interessa non e' detto sia la prima ricevuta.
    page.on('response', async (response) => {
        try {
            const url = response.url();
            if (url.includes('resolve_bets_web.php')) {
                const contentType = response.headers()['content-type'] || '';
                if (contentType.includes('application/json')) {
                    finalStatus = response.status();
                    finalContentType = contentType;
                    finalResponseBody = await response.text();
                }
            }
        } catch (e) {
            // risposta non piu' leggibile (es. redirect concluso nel frattempo): ignora, non e' quella che cerchiamo
        }
    });

    try {
        await page.goto(WORKER_URL, { waitUntil: 'networkidle', timeout: 60000 });

        // Margine di sicurezza: la sfida anti-bot puo' impiegare qualche
        // secondo tra l'esecuzione del JS e il redirect effettivo.
        await page.waitForTimeout(4000);
    } catch (e) {
        console.error('Errore durante la navigazione:', e.message);
    }

    await browser.close();

    if (finalResponseBody && finalContentType && finalContentType.includes('application/json')) {
        console.log(`OK — worker raggiunto (HTTP ${finalStatus}):`);
        console.log(finalResponseBody);
        process.exit(0);
    }

    console.error('Il worker non ha risposto con JSON valido: probabile blocco anti-bot non superato o URL/token errati.');
    console.error('Ultimo status visto:', finalStatus, '- content-type:', finalContentType);
    process.exit(1);
})();