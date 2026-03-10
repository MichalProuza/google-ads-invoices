# Google Ads – měsíční přehled útrat

Automaticky odesílá měsíční email s přehledem útrat z Google Ads (celkově i po kampaních) a odkazem na stránku s oficiálními daňovými doklady. Spouští se každý měsíc přes GitHub Actions.

---

## Proč přehled útrat a ne PDF faktury?

Původní záměr byl automaticky stahovat a posílat oficiální PDF faktury (daňové doklady) přímo z Google Ads API pomocí `InvoiceService`.

**Zjistili jsme, že to není možné.** `InvoiceService` vrací faktury pouze pro účty nastavené na **měsíční fakturaci** (monthly invoicing). Naše účty používají **automatické platby** (platba 1. v měsíci + při dosažení limitu), a přepnutí na měsíční fakturaci není v našem případě dostupné.

Pro účty s automatickými platbami neexistuje žádný způsob, jak oficiální daňové doklady získat přes Google Ads API. Tyto doklady jsou dostupné pouze ručně v Google Ads UI v sekci **Fakturace → Dokumenty**.

**Aktuální řešení:** Skript posílá měsíční email s přehledem útrat a přímým odkazem do sekce dokumentů, aby účtárna věděla, že je čas doklady stáhnout.

---

## Jak to funguje

1. Každý 5. v měsíci spustí GitHub Actions skript `fetch_invoices.py`
2. Skript se připojí k Google Ads API a dotáže se na útraty za předchozí měsíc (celkem + po kampaních)
3. Sestaví email s přehledem a odkazem na daňové doklady pro každý účet:
   `https://ads.google.com/aw/billing/documents?ocid={customer_id}`
4. Email odešle přes Gmail API

---

## Nastavení

### 1. Přihlašovací údaje Google Ads API

1. Aktivuj **Google Ads API** v [Google Cloud Console](https://console.cloud.google.com/)
2. Vytvoř **OAuth 2.0 Client ID** (typ Desktop app)
3. Požádej o **Developer Token** ve svém Google Ads účtu → Nástroje → API Center
4. Vygeneruj refresh token pomocí [OAuth Playground](https://developers.google.com/oauthplayground/) nebo lokálního skriptu

### 2. Přihlašovací údaje Gmail API

1. Aktivuj **Gmail API** v Google Cloud Console
2. Vytvoř **OAuth 2.0 Client ID** (typ Desktop app) a stáhni JSON soubor
3. Spusť generátor tokenu lokálně:

```bash
pip install -r requirements.txt google-auth-oauthlib
GMAIL_CREDENTIALS_FILE=client_secret.json python generate_token.py
```

4. Zkopíruj vytištěný JSON do GitHub Secretu `GMAIL_TOKEN_JSON`
5. Zkopíruj obsah souboru `client_secret.json` do GitHub Secretu `GMAIL_CREDENTIALS_JSON`

### 3. GitHub Secrets

Jdi do repozitáře → **Settings → Secrets and variables → Actions** a přidej:

| Secret | Popis |
|---|---|
| `GOOGLE_ADS_CUSTOMER_IDS` | Customer ID oddělená čárkou, např. `1234567890,9876543210` |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Developer token z Google Ads |
| `GOOGLE_ADS_CLIENT_ID` | OAuth2 Client ID |
| `GOOGLE_ADS_CLIENT_SECRET` | OAuth2 Client Secret |
| `GOOGLE_ADS_REFRESH_TOKEN` | OAuth2 refresh token pro Ads API |
| `GMAIL_CREDENTIALS_JSON` | Obsah JSON souboru s Gmail OAuth2 přihlašovacími údaji |
| `GMAIL_TOKEN_JSON` | Token JSON vygenerovaný skriptem `generate_token.py` |
| `SENDER_EMAIL` | Gmail adresa odesílatele |
| `RECIPIENT_EMAIL` | Email adresa příjemce |

---

## Ruční spuštění

Workflow lze spustit ručně přes **GitHub Actions → Monthly Google Ads Invoices → Run workflow**.

## Struktura souborů

```
├── fetch_invoices.py          # Hlavní skript – přehled útrat + reminder email
├── send_email.py              # Gmail API modul pro odesílání
├── generate_token.py          # Jednorázový lokální helper pro získání Gmail tokenu
├── requirements.txt
└── .github/
    └── workflows/
        └── monthly-invoices.yml   # GitHub Actions cron workflow
```
