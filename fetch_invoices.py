"""
Google Ads Invoice Downloader
Downloads PDF invoices for the previous month from one or more Google Ads accounts
and sends them via Gmail API.
"""

import os
import io
import logging
from datetime import date, datetime
from dateutil.relativedelta import relativedelta

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
    Fetches invoices for the previous month for a single Google Ads customer.

    Returns a list of dicts with keys: invoice_id, invoice_date, pdf_url, amount
    """
    billing_service = client.get_service("InvoiceService")
    billing_setup_service = client.get_service("BillingSetupService")

    # Find active billing setup IDs for this customer
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT
            billing_setup.id,
            billing_setup.status,
            billing_setup.payments_account_info.payments_account_id
        FROM billing_setup
        WHERE billing_setup.status = 'APPROVED'
    """
    try:
        response = ga_service.search(customer_id=customer_id, query=query)
        billing_setups = list(response)
    except GoogleAdsException as ex:
        logger.error(
            "GoogleAdsException for customer %s: %s", customer_id, ex.error.message
        )
        return []

    if not billing_setups:
        logger.warning("No approved billing setups found for customer %s", customer_id)
        return []

    billing_setup_id = str(billing_setups[0].billing_setup.id)

    # Determine previous month
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - relativedelta(months=1)
    issue_year = str(last_month.year)
    issue_month = client.enums.MonthOfYearEnum.MonthOfYear(last_month.month).name

    try:
        invoice_response = billing_service.list_invoices(
            customer_id=customer_id,
            billing_setup=f"customers/{customer_id}/billingSetups/{billing_setup_id}",
            issue_year=issue_year,
            issue_month=issue_month,
        )
    except GoogleAdsException as ex:
        logger.error(
            "Failed to list invoices for customer %s: %s", customer_id, ex.error.message
        )
        return []

    invoices = []
    for invoice in invoice_response.invoices:
        invoices.append(
            {
                "invoice_id": invoice.id,
                "invoice_date": f"{issue_year}-{last_month.month:02d}",
                "pdf_url": invoice.pdf_download_url,
                "amount_micros": invoice.subtotal_amount_micros,
                "currency_code": invoice.currency_code,
                "customer_id": customer_id,
            }
        )
        logger.info(
            "Found invoice %s for customer %s (%s %s)",
            invoice.id,
            customer_id,
            invoice.subtotal_amount_micros / 1_000_000,
            invoice.currency_code,
        )

    return invoices


def download_invoice_pdf(pdf_url: str, session) -> bytes:
    """Downloads the PDF from the given URL and returns raw bytes."""
    response = session.get(pdf_url)
    response.raise_for_status()
    return response.content


def main():
    # --- Configuration from environment variables ---
    customer_ids_raw = os.environ.get("GOOGLE_ADS_CUSTOMER_IDS", "")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", "")

    if not customer_ids_raw:
        raise ValueError("GOOGLE_ADS_CUSTOMER_IDS environment variable is not set.")
    if not recipient_email:
        raise ValueError("RECIPIENT_EMAIL environment variable is not set.")

    # Support comma-separated list: "1234567890,9876543210"
    customer_ids = [cid.strip().replace("-", "") for cid in customer_ids_raw.split(",") if cid.strip()]
    logger.info("Processing %d customer account(s): %s", len(customer_ids), customer_ids)

    # Initialize Google Ads client (reads google-ads.yaml or env vars)
    os.environ.setdefault("GOOGLE_ADS_USE_PROTO_PLUS", "True")
    client = GoogleAdsClient.load_from_env()

    all_invoices = []
    for customer_id in customer_ids:
        logger.info("Fetching invoices for customer %s ...", customer_id)
        invoices = fetch_invoices_for_account(client, customer_id)
        all_invoices.extend(invoices)

    if not all_invoices:
        logger.warning("No invoices found for any account. Nothing to send.")
        return

    # Download PDFs
    import requests
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    # Use the Google Ads API credentials to authenticate the download requests
    credentials = client.oauth2_credentials
    if hasattr(credentials, "refresh"):
        credentials.refresh(Request())

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {credentials.token}"})

    attachments = []
    for inv in all_invoices:
        logger.info("Downloading PDF for invoice %s ...", inv["invoice_id"])
        try:
            pdf_bytes = download_invoice_pdf(inv["pdf_url"], session)
            filename = f"invoice_{inv['customer_id']}_{inv['invoice_date']}_{inv['invoice_id']}.pdf"
            attachments.append({"filename": filename, "data": pdf_bytes, "mimetype": "application/pdf"})
        except Exception as exc:
            logger.error("Failed to download PDF for invoice %s: %s", inv["invoice_id"], exc)

    if not attachments:
        logger.error("All PDF downloads failed. No email will be sent.")
        return

    start_date, end_date = get_last_month_range()
    subject = f"Google Ads Invoices – {datetime.strptime(start_date, '%Y-%m-%d').strftime('%B %Y')}"
    body = (
        f"Hi,\n\n"
        f"Please find attached the Google Ads invoice(s) for the period "
        f"{start_date} – {end_date}.\n\n"
        f"Accounts processed: {', '.join(customer_ids)}\n"
        f"Invoices attached: {len(attachments)}\n\n"
        f"This message was generated automatically.\n"
    )

    logger.info("Sending email to %s with %d attachment(s) ...", recipient_email, len(attachments))
    send_invoices_email(
        to=recipient_email,
        subject=subject,
        body=body,
        attachments=attachments,
    )
    logger.info("Done.")


if __name__ == "__main__":
    main()
