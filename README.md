# drop-monitor

Avvisa su Telegram appena un prodotto **compare**, **torna disponibile** o **si esaurisce** su un sito e-commerce.
Pensato per i drop Pokémon su [gemcardinfinitycollection.it](https://www.gemcardinfinitycollection.it) (nopCommerce),
ma il parser è a plugin: Shopify `products.json`, feed RSS/Atom e un fallback generico (JSON-LD) sono inclusi.

```
config.yaml  ──►  scheduler (1 richiesta per ciclo, jitter, backoff)  ──►  parser  ──►  matching parole chiave
                                                                                          │
                       Telegram  ◄──  notifica solo sui cambi di stato  ◄──  SQLite (stato per prodotto)
```

## Come funziona

**Stati per prodotto** (persistiti in SQLite, notifica solo sulle transizioni):

| stato | significato |
|---|---|
| `absent` | mai visto, oppure sparito dal sito (404 sulla pagina prodotto, o assente da una scansione completa della categoria) |
| `present_unavailable` | in catalogo ma non acquistabile ("Esaurito") |
| `available` | in catalogo e acquistabile |

**Sorgenti** (`site.sources`): ogni ciclo fa **una sola richiesta HTTP**, scelta a rotazione fra:

* *discovery*: pagina categoria (paginata: una pagina per ciclo), ricerca, feed RSS, Shopify `products.json`. Serve a scoprire prodotti nuovi.
* *hot*: la pagina prodotto di ogni prodotto osservato di cui conosciamo l'URL (da config o perché già visto in una lista). Su nopCommerce è l'unico segnale affidabile di disponibilità. `polling.hot_ratio` = quante richieste "hot" per ogni richiesta "discovery" (default 2).

**Matching** (`products[].keywords`): case-insensitive, senza accenti, ignora punteggiatura, `º`/`°` e stopword; tutte le parole della chiave devono comparire nel titolo (in qualsiasi ordine). `exclude` scarta titoli con certe parole (es. "casuale"), `url` forza un match esatto, `fuzzy: 0.85` abilita un fallback tollerante ai refusi.

**Rispetto del sito**: User-Agent onesto e configurabile, `robots.txt` letto e rispettato (`Crawl-delay` incluso), intervallo casuale fra `min_seconds` e `max_seconds`, backoff esponenziale (con `Retry-After`) su 429/5xx/403/errori di rete, mai più di una richiesta in volo.

## Deploy con Docker Compose

```bash
git clone https://github.com/shapironeil/collectaio.git drop-monitor && cd drop-monitor
cp config.example.yaml config.yaml      # modifica sorgenti, prodotti, intervallo
cp .env.example .env                    # TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID
mkdir -p data
docker compose up -d --build
docker compose logs -f                  # oppure: tail -f data/drop-monitor.log
```

* **Token bot**: crea il bot con [@BotFather](https://t.me/BotFather) e copia il token.
* **Chat ID**: scrivi al bot, poi apri `https://api.telegram.org/bot<TOKEN>/getUpdates` e leggi `message.chat.id`
  (per un gruppo l'id è negativo). Verifica con `docker compose run --rm drop-monitor test-telegram`.
* I file persistenti stanno in `./data` (db SQLite, log ruotato 5 MB x 3, heartbeat `health.json`). Il container gira
  con l'utente `PUID:PGID` (default 1000:1000) perché `./data` resti scrivibile: se `id -u` dà un altro valore, impostalo in `.env`.
* `HEALTHCHECK` Docker: `drop-monitor healthcheck` fallisce se non c'è heartbeat da `storage.health_max_age_seconds`
  (`docker inspect --format '{{.State.Health.Status}}' drop-monitor`). Con `restart: unless-stopped` un crash viene riavviato.

### Senza Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && pip install -e .
drop-monitor -c config.yaml run
```

## Comandi

| comando | cosa fa |
|---|---|
| `drop-monitor run [--dry-run]` | loop infinito; `--dry-run` logga le notifiche invece di inviarle |
| `drop-monitor once [--dry-run] [--delay 3]` | una scansione completa (ogni sorgente + ogni pagina prodotto una volta), stampa stato ed esce. Ideale per i test |
| `drop-monitor probe URL [--type …] [--file saved.html]` | scarica (o legge da file) e mostra cosa vede il parser e quali prodotti matchano |
| `drop-monitor status` | stato e ultimi eventi dal database |
| `drop-monitor healthcheck` | exit 0 se l'heartbeat è fresco |
| `drop-monitor test-telegram` | invia un messaggio di prova |

Sul bot Telegram: `/status` (stato prodotti, ciclo, errori, backoff), `/ping`, `/help`. Risponde solo alla chat configurata.

## Esempio di notifica

```
🟢 DISPONIBILE ORA — Pokemon 30 Anniversario - Mini Tin Case Sealed - ITA
💶 Prezzo: €129,89
📦 Stato: DISPONIBILE (prima: presente, non acquistabile)
🔗 Pagina prodotto
🛒 Add-to-cart endpoint (POST): https://www.gemcardinfinitycollection.it/it/addproducttocart/details/3765/1
🔎 Chiave: Pokemon 30 Anniversario Mini Tin Case ITA · fonte: product
🕒 2026-10-02 10:00:05 CEST
```

Nota sul "link aggiungi al carrello": nopCommerce non espone un link GET; l'aggiunta è una `POST` AJAX
(`/it/addproducttocart/details/{id}/1` dalla pagina prodotto, `/it/addproducttocart/catalog/{id}/1/1` dalle liste).
Il messaggio riporta l'endpoint per la fase successiva (automazione dell'ordine); il link cliccabile è la pagina prodotto.
Su Shopify invece il link `/cart/{variant}:1` è un vero deep link e viene usato.

## Configurazione

Vedi [`config.example.yaml`](config.example.yaml), commentato riga per riga. Le chiavi principali:

```yaml
site:
  sources:
    - {type: category, url: https://www.gemcardinfinitycollection.it/it/pokemon-30%C2%BA-anniversario}
    - {type: rss,      url: https://www.gemcardinfinitycollection.it/newproducts/rss}
  track_product_pages: true
products:
  - keywords: "Pokemon 30 Anniversario Mini Tin Case ITA"
    exclude: ["casuale"]
  - keywords: "Pokemon 30 Anniversario Bundle 6 Buste ITA"
polling: {min_seconds: 30, max_seconds: 60, hot_ratio: 2}
telegram: {bot_token: "${TELEGRAM_BOT_TOKEN}", chat_id: "${TELEGRAM_CHAT_ID}"}
```

I segreti si possono scrivere inline o iniettare dall'ambiente con `${VAR}` / `${VAR:-default}`.

### Perché queste sorgenti per gemcardinfinitycollection.it

L'analisi completa è in [`docs/site-analysis.md`](docs/site-analysis.md). In breve:

* non c'è `products.json` (non è Shopify); c'è una `sitemap.xml` (2 000+ URL, utile solo una tantum) e il feed
  nopCommerce `/newproducts/rss` (solo i nuovi inserimenti, senza prezzo né stock);
* la **pagina categoria** espone titolo, prezzo e l'overlay "Esaurito": è la sorgente di scoperta migliore (6 prodotti/pagina, `?pagesize=` ignorato);
* la **ricerca SmartSearch** è in `Disallow` nel robots.txt (per Googlebot) e non mostra lo stock: non la usiamo;
* la **pagina prodotto** è l'unico segnale certo: il blocco "Inviami una mail quando ritorna disponibile" compare solo a stock 0.
  Il bottone "Acquista" e `schema.org/InStock` sono presenti anche sugli esauriti e vanno ignorati.

## Test

```bash
pip install -r requirements-dev.txt
pytest -q
```

I test coprono parser (fixture ricavate dalle pagine reali del sito, ottobre 2026), matching, macchina a stati,
scheduler end-to-end con fetcher finto (scoperta → restock → 404 → ritorno, backoff 429/5xx, robots, sweep paginato), config e healthcheck.

## Struttura

```
drop_monitor/
  cli.py            comandi run / once / probe / status / healthcheck / test-telegram
  config.py         caricamento e validazione config.yaml (+ espansione ${ENV})
  fetcher.py        httpx, User-Agent, robots.txt, classificazione errori / Retry-After
  scheduler.py      loop: 1 richiesta/ciclo, code hot/discovery, paginazione, backoff, heartbeat
  parsers/          nopcommerce.py, shopify.py, rss.py, generic.py (JSON-LD), dispatcher
  matching.py       normalizzazione e matching parole chiave
  state.py          transizioni di stato ed eventi
  store.py          SQLite (tracked, events, seen_products, meta)
  notifier.py       Telegram sendMessage/getUpdates + formattazione messaggi
  telegram_bot.py   thread /status /ping /help
  health.py         heartbeat file + healthcheck
tests/              pytest + fixtures reali
docs/site-analysis.md
```

## Roadmap (fase 2)

Il monitor è la base per il "sistema loop" multi-ordine: la pagina prodotto espone già id prodotto ed endpoint
add-to-cart; la fase successiva (sessione, carrello, checkout) va progettata a parte valutando i termini del sito.
