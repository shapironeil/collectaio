# Fase 2 rivisitata: motore ordini ("come Cyber AIO, ma meglio")

## Modello

| concetto | in Cyber AIO | in collectaio |
|---|---|---|
| Profile | dati di spedizione/pagamento riusabili | `personal/order-profile.yaml` + `personal/profiles/*.yaml`, password solo in `.env`, sessione (cookie) per profilo in `personal/sessions/` |
| Task | sito + prodotto + taglia + profilo + modalità | `tasks[]` in `config.yaml`: prodotto (= voce di `products`), quantità, lista di profili, `monitor` o `auto_checkout`, limite di spesa, metodo spedizione/pagamento preferito |
| Monitor | polling del prodotto | il monitor della fase 1: una richiesta per ciclo, backoff, pagine prodotto in coda hot |
| Captcha harvester | risoluzione captcha | solo in registrazione account, mostrato alla persona nella finestra; mai risolto da macchine |
| Quick task / checkout | checkout automatico, spesso senza freni | sempre: limite di spesa, riepilogo letto dal sito, conferma umana su Telegram (bottoni), un ordine per profilo in sequenza |

"Meglio" significa: nessuna raffica di richieste, nessun aggiramento di captcha, ogni ordine è
visto e confermato da una persona (disattivabile per profilo con `confirm_on_telegram: false`,
ma il limite di spesa resta sempre).

## Flusso (nopCommerce 3.x, verificato dove possibile sul sito)

```
AVAILABLE  ──►  per ogni profilo del task:
  1. login            POST /it/login (token anti-forgery, nessun captcha) — cookie salvati
  2. carrello         POST /it/addproducttocart/catalog/{id}/1/{qty} (JSON success/message)
                      GET  /it/cart → articoli, subtotale, token, checkbox termini
                      POST /it/cart {checkout, termsofservice} → /it/onepagecheckout
  3. checkout OPC     GET  /it/onepagecheckout → saveUrl dai `X.init(...)`, indirizzi salvati
                      POST OpcSaveBilling (indirizzo esistente che combacia col profilo, altrimenti nuovo; ShipToSameAddress)
                      POST OpcSaveShippingMethod {shippingoption}   ← scelta per nome
                      POST OpcSavePaymentMethod  {paymentmethod}    ← scelta per nome (PayPal/bonifico; carte rifiutate)
                      POST OpcSavePaymentInfo
                      → sezione confirm-order: righe, subtotale, spedizione, TOTALE
  4. controlli        totale ≤ max_total_eur, nessun avviso, quantità giusta
  5. conferma         Telegram: "✅ Conferma ordine / ❌ Annulla" (timeout → non inviato)
  6. invio            POST OpcConfirmOrder → redirect a /checkout/completed (ordine piazzato)
                      oppure a PayPal (ordine creato, pagamento da completare al link inviato su Telegram)
```

Il **dry-run** esegue 1-4 e si ferma: `drop-monitor order --task NOME` (default), `--live` per inviare davvero.
`--probe` salva ogni pagina della corsa in `data/probes/<data-ora>/` (token oscurati) per studiare
le pagine del checkout che, senza account, non ho potuto vedere.

## Cosa è verificato e cosa no

Verificato sul sito (anonimo): add-to-cart JSON, pagina carrello (fixture reale nei test), checkout
che richiede il login (niente guest checkout), script OPC standard di nopCommerce 3.x
(`/Scripts/public.onepagecheckout.js`, con sezioni e `goto_section`), redirect di `/it/onepagecheckout`.

Non verificato (serve un account): markup esatto delle sezioni spedizione/pagamento/riepilogo e
i metodi di pagamento offerti. Il driver legge le opzioni dai radio `shippingoption`/`paymentmethod`
e i totali dalla tabella `.cart-total`, come nel tema DefaultClean; i test usano fixture sintetiche
nello stile nop 3.9. Il primo `order --task X --probe` con un account vero servirà a confermare.

## Comandi

```
drop-monitor login --profile default            # prova il login del profilo
drop-monitor order --task minitin               # dry-run fino al riepilogo (conferma Telegram non richiesta)
drop-monitor order --task minitin --probe       # idem, salvando le pagine per lo studio
drop-monitor order --task minitin --live        # ordine vero: limite + conferma Telegram + invio
drop-monitor order --task minitin --live --console-confirm   # conferma da tastiera invece che Telegram
drop-monitor orders                             # storico ordini (anche in /orders sul bot e nella finestra)
```

Con `mode: auto_checkout` e il monitor in esecuzione (`run`), il task parte da solo al primo
passaggio a "disponibile"; con `run --dry-run` si ferma al riepilogo. Un task non viene mai
avviato due volte in parallelo per lo stesso prodotto.

## Limiti e rischi

* Pagamento con carta nel checkout: non supportato (non salviamo dati di carta). Usa PayPal
  o un metodo che non chiede dati nel sito; con PayPal l'ordine viene creato e il link di
  pagamento arriva su Telegram.
* Quantità massima per ordine, limiti per cliente, cambio prezzo tra carrello e riepilogo:
  il riepilogo è letto dal sito subito prima della conferma e il limite si applica al totale.
* Sessione scaduta o password cambiata: `LoginError`, nessun ordine; il task fallisce con
  messaggio e il monitor continua.
* Più profili = più account reali, ciascuno con i propri dati: la fase 2 non crea account
  automaticamente (la registrazione resta assistita, col captcha visto da una persona).
