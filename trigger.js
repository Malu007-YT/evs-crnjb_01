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
async function triggerUrl(browser, name, url) {
    console.log(`[${name}] Avvio richiesta stealth per: ${url}`);

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
        // Navigazione: 'networkidle' attende che la sfida JS abbia finito i redirect e la rete sia ferma
        const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
        
        // Simula un micro-movimento del mouse e una pausa per far stabilizzare i cookie generati da slowAES
        await page.mouse.move(200, 200);
        await page.waitForTimeout(4000); 

        // Estrae il testo contenuto nella pagina (il JSON finale generato dal PHP)
        const content = await page.textContent('body');
        const status = response ? response.status() : 'Sconosciuto';

        if (content && (content.includes('"success":true') || content.includes('success'))) {
            console.log(`[${name}] OK (HTTP ${status}):`);
            console.log(content.trim());
            success = true;
        } else {
            console.error(`[${name}] Nessuna risposta JSON valida ricevuta dall'output.`);
            console.error(`[${name}] Ultimo status HTTP visto: ${status}`);
            console.error(`[${name}] Contenuto della pagina recuperato:\n`, content ? content.trim() : 'VUOTO');
        }
    } catch (e) {
        console.error(`[${name}] Errore durante la navigazione/sblocco:`, e.message);
        // Scatta uno screenshot di errore se si pianta, utile per fare debug su GitHub
        await page.screenshot({ path: `error-${name.replace(/\s+/g, '_')}.png` }).catch(() => {});
    }

    await context.close(); // Chiude il contesto della pagina corrente liberando memoria
    return success;
}

(async () => {
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

    // Esecuzione rigorosamente sequenziale come nel tuo script originale
    for (const { name, url } of URLS) {
        const ok = await triggerUrl(browser, name, url);
        allOk = allOk && ok;
        
        if (ok && URLS.indexOf({ name, url }) !== URLS.length - 1) {
            console.log('Attesa precauzionale di 5 secondi prima del prossimo endpoint...');
            await new Promise(resolve => setTimeout(resolve, 5000));
        }
    }

    await browser.close();
    process.exit(allOk ? 0 : 1);
})();
