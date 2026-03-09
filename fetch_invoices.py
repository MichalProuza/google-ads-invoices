"""
Google Ads Invoice Downloader
Fetches actual invoices (PDF) for the previous month from one or more Google Ads
accounts via InvoiceService and sends them via Gmail API.
"""

import os
import logging
from datetime import date, datetime
from dateutil.relativedelta import relativedelta

import requests
from google.auth.transport.requests import Request
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from send_email import send_invoices_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def get_last_month_range() -> tuple[str, str]:
    """Returns (start_date, end_date) strings for the previous calendar month."""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - relativedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return last_month_start.strftime("%Y-%m-%d"), last_month_end.strftime("%Y-%m-%d")


def fetch_invoices_for_account(client: GoogleAdsClient, customer_id: str) -> list[dict]:
    """
    Fetches actual invoices for the previous month via InvoiceService.

    Returns a list of dicts with keys: invoice_id, pdf_url, amount_micros,
    currency_code, customer_id.
    """
    ga_service = client.get_service("GoogleAdsService")

    # Find active billing setup for this customer
    query = """
        SELECT
            billing_setup.id,
            billing_setup.status
        FROM billing_setup
        WHERE billing_setup.status = 'APPROVED'
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        billing_setups = list(response)
    except GoogleAdsException as ex:
        logger.error("Failed to query billing setups for customer %s: %s", customer_id, ex.failure)
        return []

    if not billing_setups:
        logger.warning("No approved billing setups found for customer %s", customer_id)
        return []

    billing_setup_id = str(billing_setups[0].billing_setup.id)
    logger.info("Using billing setup %s for customer %s", billing_setup_id, customer_id)

    # Determine previous month
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - relativedelta(months=1)
    issue_year = str(last_month.year)
    issue_month = client.enums.MonthOfYearEnum(last_month.month + 1)

    billing_service = client.get_service("InvoiceService")
    try:
        invoice_response = billing_service.list_invoices(
            customer_id=customer_id,
            billing_setup=f"customers/{customer_id}/billingSetups/{billing_setup_id}",
            issue_year=issue_year,
            issue_month=issue_month,
        )
    except GoogleAdsException as ex:
        logger.error("Failed to list invoices for customer %s: %s", customer_id, ex.failure)
        return []

    invoices = []
    for invoice in invoice_response.invoices:
        invoices.append({
            "invoice_id": invoice.id,
            "pdf_url": invoice.pdf_url,
            "amount_micros": invoice.subtotal_amount_micros,
            "currency_code": invoice.currency_code,
            "customer_id": customer_id,
        })
        logger.info(
            "Found invoice %s for customer %s (%.2f %s)",
            invoice.id, customer_id,
            invoice.subtotal_amount_micros / 1_000_000,
            invoice.currency_code,
        )

    return invoices


def download_invoice_pdf(pdf_url: str, credentials) -> bytes:
    """Downloads the invoice PDF from the given URL using OAuth2 credentials."""
    if hasattr(credentials, "refresh"):
        credentials.refresh(Request())

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {credentials.token}"
    response = session.get(pdf_url)
    response.raise_for_status()
    return response.content


def main():
    customer_ids_raw = os.environ.get("GOOGLE_ADS_CUSTOMER_IDS", "")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", "")

    if not customer_ids_raw:
        raise ValueError("GOOGLE_ADS_CUSTOMER_IDS environment variable is not set.")
    if not recipient_email:
        raise ValueError("RECIPIENT_EMAIL environment variable is not set.")

    customer_ids = [cid.strip().replace("-", "") for cid in customer_ids_raw.split(",") if cid.strip()]
    logger.info("Processing %d customer account(s): %s", len(customer_ids), customer_ids)

    os.environ.setdefault("GOOGLE_ADS_USE_PROTO_PLUS", "True")
    client = GoogleAdsClient.load_from_env()

    start_date, end_date = get_last_month_range()
    period_label = datetime.strptime(start_date, "%Y-%m-%d").strftime("%B %Y")

    attachments = []
    summary_lines = []

    for customer_id in customer_ids:
        logger.info("Fetching invoices for customer %s ...", customer_id)
        invoices = fetch_invoices_for_account(client, customer_id)

        if not invoices:
            logger.warning("No invoices found for customer %s", customer_id)
            continue

        credentials = client.oauth2_credentials
        for inv in invoices:
            try:
                logger.info("Downloading invoice PDF %s ...", inv["invoice_id"])
                pdf_bytes = download_invoice_pdf(inv["pdf_url"], credentials)
                filename = f"invoice_{customer_id}_{inv['invoice_id']}.pdf"
                attachments.append({
                    "filename": filename,
                    "data": pdf_bytes,
                    "mimetype": "application/pdf",
                })
                summary_lines.append(
                    f"  - Faktura {inv['invoice_id']} ({customer_id}): "
                    f"{inv['amount_micros'] / 1_000_000:,.2f} {inv['currency_code']}"
                )
            except Exception as ex:
                logger.error("Failed to download invoice %s: %s", inv["invoice_id"], ex)

    if not attachments:
        logger.warning("No invoices found for any account. Nothing to send.")
        return

    subject = f"Google Ads faktury – {period_label}"
    body = (
        f"Dobrý den,\n\n"
        f"v příloze naleznete faktury z Google Ads za období {period_label} "
        f"({start_date} – {end_date}).\n\n"
        f"Faktury:\n"
        + "\n".join(summary_lines)
        + f"\n\nCelkem příloh: {len(attachments)}\n\n"
        f"Tato zpráva byla vygenerována automaticky.\n"
    )

    logger.info("Sending email to %s with %d invoice(s) ...", recipient_email, len(attachments))
    send_invoices_email(
        to=recipient_email,
        subject=subject,
        body=body,
        attachments=attachments,
    )
    logger.info("Done.")


if __name__ == "__main__":
    main()
