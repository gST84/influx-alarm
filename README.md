# Influx Alarm

Alarmsysteem voor InfluxDB 2, gebouwd op de [influx browser](https://github.com/gST84/influx_browser):
kies met de browser welke waarden bewaakt worden, stel per waarde grenzen in, en laat het systeem
mailen, sms'en en bellen (via Asterisk, met "druk 1 om te bevestigen").

📖 **[Handleiding met screenshots](docs/HANDLEIDING.md)**

## Wat het doet

- **Organisaties**: elke organisatie heeft eigen contactpersonen, regels en historiek; de instellingen
  (InfluxDB, mail, sms, telefonie) zijn gedeeld. Beheerders zien alles, gewone gebruikers alleen de
  organisaties waaraan ze gekoppeld zijn.
- **Contactpersonen** met mail en telefoon (testknoppen voor mail, sms en oproep).
- **Zonder aanmelden** het overzicht en de status van de waarden bekijken (alleen-lezen, uit te zetten).
- **Regels**: per regel kies je met de browser (bucket → measurements → values & tags) de reeksen
  die bewaakt worden, wie verwittigd wordt en hoe:
  - **mail** en **sms** gaan meteen naar iedereen met een vinkje;
  - **bellen** gebeurt als cascade in de gekozen volgorde: wie niet opneemt of geen 1 drukt, wordt
    overgeslagen; bevestigt niemand, dan wordt de lijst na de wachttijd nog één keer afgelopen.
- Per waarde: **ondergrens**, **bovengrens**, **vertraging** (zo lang moet de waarde buiten de grens
  blijven) en **geen data na** (alarm als het laatste punt ouder is dan dit).
- **Eén bericht per regel**: meerdere waarden in alarm komen samen in één melding. Zolang het niet
  opgelost is, volgt **maximaal één herinnering per 24 uur**.
- **Negeren na** een instelbare tijd (uren, dagen of weken): daarna geen herinneringen meer.
- **Opgelost**: mail naar iedereen die verwittigd werd, plus sms naar wie een sms of oproep kreeg.
- **Bevestigen** (toets 1 aan de telefoon, of de knop op de pagina) stopt de belronde; de
  herinnering na 24 uur blijft zolang het probleem niet opgelost is.
- **Stille uren** per regel: 's nachts mail en sms, het telefoontje volgt na de stille uren.
- **Pauzeren** van een hele regel of van individuele waarden.
- De mail bevat alle tags, de grenzen, het tijdstip van de laatste data en een link naar de regel.
- Het telefoonbericht wordt voorgelezen met een Vlaamse stem (Piper TTS, volledig offline).

## Installeren

Nodig: een server met Docker en Docker Compose, bereikbaar InfluxDB 2, en (voor echte oproepen)
een SIP-account bij een VoIP-provider.

```bash
cp .env.example .env
# vul .env in: minstens AMI_PASSWORD, en INFLUX_* en SIP_* zodra je ze hebt
docker compose up -d --build
```

Open daarna `http://<server>:8080`, maak de eerste organisatie en beheerder aan, en vul onder **Instellingen**
InfluxDB, mail (SMTP) en sms in. Elke sectie heeft een testknop.

### Telefonie

De compose bevat een vooraf geconfigureerde Asterisk. Vul in `.env`:

| Variabele | Betekenis |
|---|---|
| `SIP_HOST`, `SIP_PORT` | SIP-server van je provider |
| `SIP_USERNAME`, `SIP_PASSWORD` | SIP-account (registratie) |
| `SIP_AUTH_USERNAME` | alleen als de provider een aparte authenticatiegebruiker gebruikt |
| `CALLER_ID` | getoond nummer; meestal verplicht een nummer van je provider |
| `EXTERNAL_IP` | publiek IP als de server achter NAT zit (tegen eenrichtingsgeluid) |
| `RTP_START`, `RTP_END` | poorten voor het geluid (UDP, standaard 10000–10100) |

Poorten `5060/udp` en de RTP-reeks moeten van de provider bereikbaar zijn (of gebruik
`network_mode: host` voor de asterisk-service op Linux). Zonder SIP-gegevens werken alleen de
**testnummers**, handig om alles uit te proberen:

| Nummer | Gedrag |
|---|---|
| `test:1001` | neemt op en drukt 1 |
| `test:1002` | neemt op maar drukt niets (zoals een voicemail) |
| `test:1003` | neemt niet op |

Telefoonnummers vul je in zoals je provider ze verwacht (meestal `+32…` of `0032…`).

### SMS

Te kiezen onder Instellingen → SMS:

- **HTTP-API**: werkt met de meeste providers. URL en body met plaatshouders `{to}`, `{to_digits}`
  en `{message}`, en extra headers (bv. `Authorization: Bearer …`).
- **Twilio**.
- **Via Asterisk**: een USB GSM-dongle (`chan_dongle`, zie de commentaar in `docker-compose.yml`)
  of SIP MESSAGE als je provider dat ondersteunt.

## Hoe een alarm bepaald wordt

Elke minuut (instelbaar) haalt de app per waarde het laatste punt op:

1. **Geen data**: is het laatste punt ouder dan *geen data na*, dan is het alarm.
2. **Grenzen**: met vertraging 0 telt de laatste waarde. Met een vertraging moet de waarde de hele
   periode buiten de grens liggen. Dat wordt in InfluxDB zelf nagekeken (de waarde vlak vóór de
   periode plus min/max erbinnen), zodat het ook klopt na een herstart en bij bronnen die alleen
   bij een wijziging schrijven, zoals Home Assistant.
3. Tekst- en aan/uit-waarden worden alleen op *geen data* gecontroleerd.

## Ontwikkelen

```bash
python3 -m unittest discover -s tests   # logica: beoordeling, berichten, stille uren, alarmflow
docker compose up -d --build            # volledige stack
```

| Pad | Inhoud |
|---|---|
| `alarm/checker.py` | controlelus: waarden beoordelen, alarmen openen/sluiten, meldingen starten |
| `alarm/messages.py` | beoordeling (`evaluate`) en teksten voor mail, sms en spraak |
| `alarm/notify.py` | verzenden, bel-cascade, stille uren, opgelost-berichten |
| `alarm/influx.py` | InfluxDB-queries (browser en controle) |
| `alarm/channels/` | AMI (Asterisk), mail, sms, tekst-naar-spraak |
| `alarm/web.py` | webserver, login, rollen, organisaties en API |
| `alarm/static/` | webinterface |
| `asterisk/` | Asterisk-container: config en dialplan (`alarm-call`, `alarm-test`) |
