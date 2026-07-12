# EV Scanner — Trigger worker via GitHub Actions

Sostituisce cron-job.org (che su questo hosting non funziona: vedi sotto)
per richiamare periodicamente `worker/resolve_bets_web.php`.

## Perché cron-job.org non funziona

Il tuo hosting (`evscanner.free.je`) protegge le richieste esterne con una
JS-challenge anti-bot (script `slowAES` che imposta un cookie via
JavaScript e poi reindirizza). cron-job.org fa solo una richiesta HTTP
semplice: riceve la paginetta della sfida, non la esegue (non ha un motore
JavaScript), e non arriva mai al vero script PHP. **Nessun servizio di
"cron HTTP puro" può funzionare qui** — non è un problema di URL o token.

## La soluzione: un browser vero, gratis, programmato

Questo repository usa **GitHub Actions** (gratuito, incluso in qualsiasi
account GitHub per repository pubblici o privati entro il tier free) per
eseguire, ogni 15 minuti, un vero browser headless (Playwright + Chromium)
che apre l'URL, esegue il JavaScript della sfida come farebbe un browser
normale, ottiene il cookie, segue il redirect automatico e verifica che lo
script PHP abbia risposto con JSON valido.

## Setup (una tantum, ~5 minuti)

1. **Crea un repository GitHub** (va benissimo anche privato — GitHub
   Actions funziona comunque, con minuti gratuiti mensili più che
   sufficienti per un job di pochi secondi ogni 15 minuti).
2. Carica in quel repository **tutti i file di questa cartella**, mantenendo
   la struttura (`.github/workflows/trigger-worker.yml` deve restare in
   quel percorso esatto).
3. Nel repository: **Settings → Secrets and variables → Actions → New
   repository secret**:
   - Nome: `WORKER_URL`
   - Valore: l'URL completo con token, ad esempio
     `http://evscanner.free.je/ev-scanner/worker/resolve_bets_web.php?token=IL_TUO_TOKEN`

   ⚠️ Il token **non va mai scritto nel file YAML o in altri file del
   repo**: va sempre nei Secrets di GitHub (che non sono visibili nei log
   né nel codice), anche se il repository è privato — è una buona pratica
   indipendente dalla visibilità del repo.
4. Vai su **Actions** (tab in alto nel repository). Se GitHub chiede di
   abilitare le Actions per il repo, conferma.
5. Per testare subito senza aspettare 15 minuti: **Actions → "Trigger EV
   Scanner worker" (nella barra laterale) → Run workflow → Run workflow**.
   Dopo ~30-60 secondi dovresti vedere il job verde ✅, e nei log l'ultima
   riga con il JSON restituito dal worker (`{"success":true,"stats":{...},
   "clv_stats":{...}}`).

Da quel momento il workflow gira da solo secondo lo schedule (`*/15 * * * *`,
modificabile nel file YAML), senza bisogno di nessun servizio esterno.

## Verifica che stia funzionando

- **GitHub → Actions**: storico delle esecuzioni, verde = riuscito.
- **Pagina Sistema dell'app** (`system.php`): canale `worker_resolve_bets_web`
  nei log, e la card "Copertura CLV (30gg)" dovrebbe iniziare a salire nel
  giro di qualche ora/giorno.

## Se in futuro cambi hosting (niente più anti-bot)

Se in futuro passi a un hosting che espone un cron reale (SSH) o che non
blocca le richieste HTTP esterne, puoi tornare a un cron classico
(`worker/resolve_bets.php` da riga di comando, o cron-job.org su
`worker/resolve_bets_web.php` se l'hosting non ha l'anti-bot) e disattivare
questo workflow — nessuna modifica al codice PHP è necessaria, sono due
strade verso lo stesso endpoint.