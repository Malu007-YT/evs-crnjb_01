// Trigger dei worker EV Scanner tramite browser headless reale con spoofing avanzato.
//
// Mantiene la struttura originale a ciclo sequenziale, ma applica modifiche stealth
// per bypassare i blocchi anti-bot (slowAES) degli hosting gratuiti che rilevano Playwright.

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
 * Visita un URL protetto dalla JS-challenge anti-bot applicando tecniche stealth.
 * Legge direttamente il contenuto del body a fine caricamento per estrarre il JSON.
 */
/** Timestamp leggibile per ogni riga di log (il log GitHub Actions ha gia' un suo timestamp, ma comodo per correlare con logs/*.log lato server, che usa Europe/Rome). */
function ts() {
    return new Date().toLocaleString('it-IT', { timeZone: 'Europe/Rome' });
}

/**
 * Prova a estrarre e stampare in modo leggibile le statistiche del JSON
 * restituito dal worker PHP (es. {"stats":{"controllate":21,"risolte":0,...}}
 * o {"success":true,...}), cosi' nei log di GitHub Actions si vede subito
 * l'esito del ciclo senza dover aprire il JSON grezzo.
 */
function logParsedSummary(name, content) {
    let parsed;
    try {
        parsed = JSON.parse(content.trim());
    } catch (e) {
        console.log(`[${name}] [${ts()}] Risposta non e' JSON valido, impossibile riassumerla (verra' comunque stampato il contenuto grezzo sopra).`);
        return;
    }

    if (parsed.skipped) {
        console.log(`[${name}] [${ts()}] Ciclo SALTATO lato server (skipped:true) — probabilmente un altro worker era gia' in esecuzione o il gate anti-doppia-esecuzione era attivo.`);
    }

    const stats = parsed.stats || parsed;
    if (stats && typeof stats === 'object') {
        const campi = ['controllate', 'risolte', 'void', 'saltate', 'fuori_finestra', 'errori', 'void_ricontrollate', 'void_corrette', 'eventi_trovati', 'eventi_nuovi', 'eventi_rimossi', 'candidate_bets', 'catturate', 'totale'];
        const presenti = campi.filter(c => stats[c] !== undefined);
        if (presenti.length > 0) {
            console.log(`[${name}] [${ts()}] Riepilogo: ` + presenti.map(c => `${c}=${stats[c]}`).join(', '));
        }
        if ((stats.errori ?? 0) > 0) {
            console.warn(`[${name}] [${ts()}] ATTENZIONE: ${stats.errori} errori segnalati dal worker in questo ciclo — controlla logs/app.log sul server per il dettaglio.`);
        }
        if ((stats.fuori_finestra ?? 0) > 0) {
            console.log(`[${name}] [${ts()}] Nota: ${stats.fuori_finestra} bet sono fuori dalla finestra di 3 giorni del piano gratuito API-Sports, restano in attesa (comportamento atteso, non un errore).`);
        }
    }
}

async function triggerUrl(browser, name, url) {
    const startedAt = Date.now();
    console.log(`[${name}] [${ts()}] Avvio richiesta stealth per: ${url}`);

    // Configura un contesto con impronta digitale umana realistica (Windows + Chrome)
    const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        viewport: { width: 1920, height: 1080 },
        locale: 'it-IT',
        timezoneId: 'Europe/Rome',
        extraHTTPHeaders: {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
            'Cache-Control': 'max-age=0',
            'Upgrade-Insecure-Requests': '1'
        }
    });

    const page = await context.newPage();

    // INIEZIONE STEALTH: Elimina la traccia "navigator.webdriver" prima che gli script della sfida partano
    await page.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
    });

    let success = false;

    try {
        console.log(`[${name}] [${ts()}] Navigazione in corso (timeout 60s, attesa networkidle)...`);
        // Navigazione: 'networkidle' attende che la sfida JS abbia finito i redirect e la rete sia ferma
        const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
        const navMs = Date.now() - startedAt;
        const status = response ? response.status() : 'Sconosciuto';
        console.log(`[${name}] [${ts()}] Navigazione completata in ${navMs}ms, HTTP ${status}, URL finale: ${page.url()}`);

        // Simula un micro-movimento del mouse e una pausa per far stabilizzare i cookie generati da slowAES
        await page.mouse.move(200, 200);
        console.log(`[${name}] [${ts()}] Attesa stabilizzazione anti-bot (4s)...`);
        await page.waitForTimeout(4000);

        // Estrae il testo contenuto nella pagina (il JSON finale generato dal PHP)
        const content = await page.textContent('body');
        const totalMs = Date.now() - startedAt;

        if (content && (content.includes('"success":true') || content.includes('success'))) {
            console.log(`[${name}] [${ts()}] OK (HTTP ${status}, durata totale ${totalMs}ms, ${content.length} byte ricevuti):`);
            console.log(content.trim());
            logParsedSummary(name, content);
            success = true;
        } else {
            console.error(`[${name}] [${ts()}] Nessuna risposta JSON valida ricevuta dall'output (HTTP ${status}, durata ${totalMs}ms).`);
            console.error(`[${name}] [${ts()}] Possibili cause: la JS-challenge anti-bot non si e' sbloccata, l'URL/token e' errato, oppure il server ha risposto con una pagina di errore invece del worker.`);
            console.error(`[${name}] Contenuto della pagina recuperato (${content ? content.length : 0} byte):\n`, content ? content.trim() : 'VUOTO');
        }
    } catch (e) {
        const totalMs = Date.now() - startedAt;
        console.error(`[${name}] [${ts()}] Errore durante la navigazione/sblocco dopo ${totalMs}ms:`, e.message);
        console.error(`[${name}] Stack:`, e.stack);
        // Scatta uno screenshot di errore se si pianta, utile per fare debug su GitHub
        const shotPath = `error-${name.replace(/\s+/g, '_')}.png`;
        await page.screenshot({ path: shotPath }).catch((shotErr) => {
            console.error(`[${name}] Impossibile salvare lo screenshot di debug (${shotPath}):`, shotErr.message);
        });
        console.error(`[${name}] Screenshot di debug salvato in: ${shotPath} (visibile tra gli artifact/log della run GitHub Actions, se configurato).`);
    }

    await context.close(); // Chiude il contesto della pagina corrente liberando memoria
    console.log(`[${name}] [${ts()}] Contesto chiuso. Esito: ${success ? 'SUCCESSO' : 'FALLITO'}.`);
    return success;
}

(async () => {
    const runStartedAt = Date.now();
    console.log(`[${ts()}] Avvio ciclo trigger — ${URLS.length} endpoint da chiamare: ${URLS.map(u => u.name).join(', ')}`);

    // Lancio di Chromium disabilitando i flag di automazione nativi che i sistemi anti-bot controllano
    const browser = await chromium.launch({
        headless: true,
        args: [
            '--disable-blink-features=AutomationControlled',
            '--disable-infobars',
            '--no-sandbox'
        ]
    });

    let allOk = true;
    const risultati = [];

    // Esecuzione rigorosamente sequenziale come nel tuo script originale
    for (let i = 0; i < URLS.length; i++) {
        const { name, url } = URLS[i];
        const ok = await triggerUrl(browser, name, url);
        allOk = allOk && ok;
        risultati.push({ name, ok });

        if (i !== URLS.length - 1) {
            console.log(`[${ts()}] Attesa precauzionale di 5 secondi prima del prossimo endpoint...`);
            await new Promise(resolve => setTimeout(resolve, 5000));
        }
    }

    await browser.close();

    const totalMs = Date.now() - runStartedAt;
    console.log(`[${ts()}] Ciclo trigger completato in ${totalMs}ms. Esito per endpoint: ` +
        risultati.map(r => `${r.name}=${r.ok ? 'OK' : 'FALLITO'}`).join(', '));

    process.exit(allOk ? 0 : 1);
})();
