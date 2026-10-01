# Analisi del sito: gemcardinfinitycollection.it (1 ottobre 2026)

Piattaforma: **nopCommerce** (tema DefaultClean personalizzato) dietro nginx. Nessun WAF/Cloudflare rilevato,
`Cache-Control: private`, cookie `Nop.customer` impostato a ogni richiesta (non serve mantenerlo). Risposte in 0,5–2 s.

## Feed pubblici verificati

| URL | esito | uso |
|---|---|---|
| `/robots.txt` | 200. Blocchi solo per `Googlebot` e `Googlebot-image`, **nessun `User-agent: *`**: per il nostro UA tutto è permesso. `Disallow: /SmartSearch/`, `/addproducttocart/…`, `/backinstocksubscriptions/…`, checkout ecc. | letto e rispettato dal fetcher |
| `/sitemap.xml` → `/it/sitemap.xml` | 200, 400 KB, 2 045 URL con `lastmod`. Contiene già le pagine dei prodotti 30º Anniversario | troppo grande per il polling; utile per trovare URL prodotto una tantum |
| `/products.json` | 404 (non Shopify) | – |
| `/feed`, `/rss`, `/it/newproducts/rss/1` | 404 | – |
| `/newproducts/rss` | 200, RSS 2.0 con gli ultimi 10 nuovi prodotti (titolo, link, descrizione, guid `urn:store:1:newProducts:product:ID`). **Niente prezzo né stock** | sorgente di scoperta per prodotti nuovi |

## Pagine HTML

### Categoria `/it/pokemon-30%C2%BA-anniversario` (pagine 1–4, 6 prodotti/pagina)
* Ogni prodotto: `div.product-item[data-productid]`, `h2.product-title a`, `span.price.actual-price`, bottone `Acquista`
  (`AjaxCart.addproducttocart_catalog('/it/addproducttocart/catalog/ID/1/1')`, POST).
* Prodotto esaurito ⇒ il tema aggiunge un `div` con `position:absolute … >Esaurito<`. Il bottone "Acquista" c'è comunque.
* `?pagesize=100` e `viewmode=list` sono ignorati; la paginazione è `?pagenumber=N` nel `div.pager`.
* Al 1/10/2026: 20 prodotti "Pokemon 30 Anniversario", **tutti esauriti**, inclusi:
  * `3765` Mini Tin Case Sealed - ITA, €129,89 (pagina 2) — uscita 2 ottobre 2026, spedizioni dal 9 ottobre
  * `3766` Bundle 6 Buste - ITA, €36,89 (pagina 2)
  * `3822` Mini Tin **Casuale** (ITA), €12,89 — da escludere (`exclude: ["casuale"]`)

### Ricerca `/it/SmartSearch/Search?q=…&selectedPagingField=18&selectedPage=N`
* Fino a 18 risultati/pagina (3/6/9/18), stesso markup `product-item` ma **senza overlay "Esaurito"**: lo stock non è deducibile.
* `Disallow: /SmartSearch/` nel robots.txt (per Googlebot) + nessuna informazione di stock ⇒ non usata di default.
  Il parser la supporta comunque (`type: search`, availability `unknown`).

### Pagina prodotto `/it/pokemon-30-anniversario-mini-tin-case-sealed-ita`
* `div.product-name h1`, `div.product-price span#price-value-ID` + `<meta itemprop="price" content="129.89">`.
* **Esaurito** ⇔ presente `div.back-in-stock-subscription` ("Inviami una mail quando ritorna disponibile",
  popup `/it/backinstocksubscribe/ID`). Verificato per contrasto su un prodotto disponibile (`3757` Mega Darkrai tin): il blocco manca.
* Sempre presenti, quindi **ignorati**: bottone `#add-to-cart-button-ID` (POST `/it/addproducttocart/details/ID/1` con form
  `#product-details-form`), `<link itemprop="availability" href="schema.org/InStock">`.
* Nessun JSON-LD, nessun `div.stock` con quantità. SKU = id prodotto.
* Prodotto inesistente ⇒ 404 nopCommerce (pagina "Pagina non trovata").

## Strategia scelta

```
discovery: categoria p1 → p2 → p3 → p4 → RSS → (ricomincia)      1 richiesta per ciclo
hot:       pagina Mini Tin Case ↔ pagina Bundle 6 Buste           2 richieste hot ogni 1 discovery
```
Con intervallo 30–60 s ogni pagina prodotto viene controllata circa ogni 70 s e l'intera categoria ogni ~11 min.
Per reagire più in fretta a un drop annunciato: `min_seconds: 20`, `max_seconds: 35`, `hot_ratio: 3`.

## Scenari e rischi considerati

| scenario | comportamento |
|---|---|
| Prodotto non ancora in catalogo, poi compare nella categoria o nell'RSS | `absent → present_unavailable` (o `available`), notifica; da quel momento la sua pagina entra nella coda hot |
| Restock: overlay "Esaurito" sparisce / blocco back-in-stock sparisce | `present_unavailable → available`, notifica con prezzo e link |
| Si riesaurisce | `available → present_unavailable`, notifica |
| Pagina prodotto 404 / rimossa | `→ absent`, notifica; se torna, nuova notifica |
| Prodotto sparito dalla categoria ma pagina ancora viva | lo stato lo decide la pagina (hot); la categoria marca `absent` solo se non c'è una pagina hot e la scansione completa è andata a buon fine |
| Cambio prezzo senza cambio stato | evento registrato; notifica solo con `notify.on_price_change: true` |
| 429 / 5xx / 403 / timeout | backoff esponenziale 60 s → 15 min (×1–1,25 jitter), rispetta `Retry-After`; avviso Telegram dopo N errori consecutivi e al ripristino |
| robots.txt cambia e vieta un URL | la sorgente viene disabilitata (o il polling pagine prodotto spento) con avviso Telegram |
| Scansione paginata interrotta da errore | la scansione viene annullata, nessun prodotto marcato `absent` su dati parziali |
| Titolo riscritto dal negozio | matching per token tollerante; opzione `url:` per agganciare l'URL esatto; `fuzzy` per refusi |
| Omonimi ("Mini Tin Casuale") | `exclude` |
| Crash del processo | Docker `restart: unless-stopped`; heartbeat `health.json` + `HEALTHCHECK`; stato in SQLite ⇒ nessuna rinotifica al riavvio |
| Comandi da chat sconosciute | ignorati e loggati |

## Registrazione account (`/it/register?returnurl=%2fit%2fcart`)

Form `POST` sulla stessa URL, protetto da `__RequestVerificationToken` (campo hidden + cookie `__RequestVerificationToken`;
la sessione imposta anche `ASP.NET_SessionId` e `Nop.customer`). `returnurl` riporta al carrello a registrazione avvenuta.

| sezione | campo sito | obbligatorio | nel profilo (`personal/`) |
|---|---|---|---|
| Dettagli personali | `FirstName`, `LastName`, `Email` | sì | `shipping.first_name`, `shipping.last_name`, `account.email` |
| Dettagli azienda | `Company`, `customer_attribute_1` Codice Fiscale, `_2` Partita IVA, `_6` Codice SDI, `_7` PEC | no | `shipping.company`, `billing.*` |
| Indirizzo | `StreetAddress`, `ZipPostalCode`, `City`, `CountryId` (46 = Italy), `StateProvinceId` | sì | `shipping.*`, `country_id`, `province_id` |
| Recapiti | `Phone` | sì | `shipping.phone` |
| Opzioni | `Newsletter` (true/false), `customer_attribute_3` "Come ci hai conosciuto?" radio 1 Google, 2 Facebook, 3 Instagram, 4 TikTok, 5 Passaparola, 7 Giornali, 8 Clienti | no | `preferences.newsletter`, `preferences.referral` |
| Password | `Password`, `ConfirmPassword` (min 6, max 999) | sì | `.env` → `ORDER_PASSWORD[_NOME]` |
| Captcha | `CaptchaDeText` (hash hidden) + `CaptchaInputText` (risposta); immagine GIF 200×70 da `/DefaultCaptcha/Generate?t=<hash>`, refresh `POST /DefaultCaptcha/Refresh` | sì | risolto a mano nella finestra |
| Consensi | `accept-privacy-policy`, `accept-privacy-termini`, `accept-privacy-registrazione` | solo lato client (alert JS se mancano) | inviati come `on` dopo spunta esplicita nella finestra |

* Le province arrivano via AJAX: `GET /country/getstatesbycountryid?countryId=46&addSelectStateItem=true` → 107 province (`{"id":130,"name":"Milano"}`). La finestra le carica e salva `province_id`.
* Il captcha è di tipo **CaptchaMvc** (immagine distorta, nessun reCAPTCHA/hCaptcha/Turnstile): serve una persona. drop-monitor mostra l'immagine nella finestra "Account" e inoltra la risposta digitata, senza alcuna risoluzione automatica.
  Attenzione: `Generate` restituisce una **X rossa su nero** (GIF da 1 237 byte) se la richiesta non porta l'header `Referer` della pagina di registrazione; con il Referer arriva l'immagine vera (~3,5 KB, diversa a ogni sessione): una **domanda aritmetica** su sfondo rumoroso, es. `74-29=?`, a cui si risponde con il numero (`45`). La finestra la mostra e la persona digita il risultato; nessuna risoluzione automatica, per scelta. `POST /DefaultCaptcha/Refresh` risponde con codice jQuery che contiene il nuovo token e il nuovo `src`.
* Esito atteso (nopCommerce): redirect a `/it/registerresult/1` (attivo), `/2` (approvazione negozio), `/3` (conferma e-mail) oppure direttamente al `returnurl`; in caso di errore il form viene rirenderizzato con `div.message-error li` / `span.field-validation-error` / `.captcha-box p.Error`. Il classificatore gestisce tutti i casi; l'esito reale di questo negozio (1, 2 o 3) si saprà alla prima registrazione.
* **Login** `/it/login`: solo `Email`, `Password`, `RememberMe` + token anti-forgery, **senza captcha**: utilizzabile in automatico nella fase 2 con account già creati.
* Cosa non si sa ancora: struttura del carrello/checkout (`/it/cart`, `/onepagecheckout`, in `Disallow` per i bot), metodi di pagamento disponibili, eventuale limite di quantità per ordine.

## Limiti noti

* L'aggiunta al carrello su nopCommerce è una POST AJAX legata a sessione: non esiste un deep link GET. La notifica riporta l'endpoint per la fase di automazione.
* Lo stock reale è visibile solo a stock 0/≠0: non si conosce la quantità.
* La pagina prodotto è l'unico segnale certo: con molti prodotti osservati e `hot_ratio` alto, il tempo di reazione per singolo prodotto cresce linearmente (n prodotti × intervallo).
