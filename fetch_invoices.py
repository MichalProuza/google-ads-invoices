"""
Google Ads Invoice Downloader
Fetches actual invoices (PDF) for the previous month from one or more Google Ads
accounts via InvoiceService, plus generates spending reports, and sends them via
Gmail API.
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

# Suppress verbose font subsetting logs from fpdf2/fonttools
logging.getLogger("fontTools.subset").setLevel(logging.WARNING)
logging.getLogger("fontTools").setLevel(logging.WARNING)


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


def fetch_spending_for_account(
    client: GoogleAdsClient, customer_id: str, start_date: str, end_date: str
) -> dict | None:
    """
    Fetches account-level and campaign-level spending for a date range.

    Returns a dict with account info and campaign breakdown, or None on error.
    """
    ga_service = client.get_service("GoogleAdsService")

    account_query = f"""
        SELECT
            customer.descriptive_name,
            customer.id,
            customer.currency_code,
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.conversions
        FROM customer
        WHERE segments.date >= '{start_date}'
            AND segments.date <= '{end_date}'
    """

    try:
        account_response = ga_service.search(customer_id=customer_id, query=account_query)
        account_rows = list(account_response)
    except GoogleAdsException as ex:
        logger.error("Failed to fetch account data for %s: %s", customer_id, ex.failure)
        return None

    if not account_rows:
        logger.warning("No spending data for customer %s in %s – %s", customer_id, start_date, end_date)
        return None

    row = account_rows[0]
    account_name = row.customer.descriptive_name or f"Account {customer_id}"
    currency = row.customer.currency_code
    total_cost_micros = row.metrics.cost_micros
    total_impressions = row.metrics.impressions
    total_clicks = row.metrics.clicks
    total_conversions = row.metrics.conversions

    campaign_query = f"""
        SELECT
            campaign.name,
            campaign.id,
            metrics.cost_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.conversions
        FROM campaign
        WHERE segments.date >= '{start_date}'
            AND segments.date <= '{end_date}'
            AND metrics.cost_micros > 0
        ORDER BY metrics.cost_micros DESC
    """

    campaigns = []
    try:
        campaign_response = ga_service.search(customer_id=customer_id, query=campaign_query)
        for crow in campaign_response:
            campaigns.append({
                "name": crow.campaign.name,
                "id": crow.campaign.id,
                "cost_micros": crow.metrics.cost_micros,
                "impressions": crow.metrics.impressions,
                "clicks": crow.metrics.clicks,
                "conversions": crow.metrics.conversions,
            })
    except GoogleAdsException as ex:
        logger.warning("Failed to fetch campaign data for %s: %s", customer_id, ex.failure)

    return {
        "customer_id": customer_id,
        "account_name": account_name,
        "currency": currency,
        "total_cost_micros": total_cost_micros,
        "total_impressions": total_impressions,
        "total_clicks": total_clicks,
        "total_conversions": total_conversions,
        "campaigns": campaigns,
    }


def _find_font_path() -> str | None:
    """Find DejaVu Sans TTF font on the system."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return os.path.dirname(path)
    return None


def generate_pdf_report(account_data: dict, start_date: str, end_date: str) -> bytes:
    """Generates a PDF spending report for a single account."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    font_dir = _find_font_path()
    if font_dir:
        pdf.add_font("DejaVu", "", os.path.join(font_dir, "DejaVuSans.ttf"))
        pdf.add_font("DejaVu", "B", os.path.join(font_dir, "DejaVuSans-Bold.ttf"))
        font_family = "DejaVu"
    else:
        font_family = "Helvetica"

    currency = account_data["currency"]
    total_cost = account_data["total_cost_micros"] / 1_000_000

    pdf.set_font(font_family, "B", 16)
    pdf.cell(0, 10, "Google Ads Billing Report", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)

    pdf.set_font(font_family, "", 11)
    pdf.cell(0, 7, f"Account: {account_data['account_name']} ({account_data['customer_id']})", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Period: {start_date}  -  {end_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font(font_family, "B", 13)
    pdf.cell(0, 9, "Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font_family, "", 11)
    pdf.cell(0, 7, f"Total Cost: {total_cost:,.2f} {currency}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Impressions: {account_data['total_impressions']:,}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Clicks: {account_data['total_clicks']:,}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Conversions: {account_data['total_conversions']:,.1f}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    campaigns = account_data["campaigns"]
    if campaigns:
        pdf.set_font(font_family, "B", 13)
        pdf.cell(0, 9, "Campaign Breakdown", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        col_widths = [70, 30, 25, 25, 25]
        headers = ["Campaign", f"Cost ({currency})", "Impr.", "Clicks", "Conv."]
        pdf.set_font(font_family, "B", 9)
        for w, h in zip(col_widths, headers):
            pdf.cell(w, 7, h, border=1)
        pdf.ln()

        pdf.set_font(font_family, "", 9)
        for c in campaigns:
            name = c["name"][:40] + ("..." if len(c["name"]) > 40 else "")
            cost = f"{c['cost_micros'] / 1_000_000:,.2f}"
            pdf.cell(col_widths[0], 7, name, border=1)
            pdf.cell(col_widths[1], 7, cost, border=1, align="R")
            pdf.cell(col_widths[2], 7, f"{c['impressions']:,}", border=1, align="R")
            pdf.cell(col_widths[3], 7, f"{c['clicks']:,}", border=1, align="R")
            pdf.cell(col_widths[4], 7, f"{c['conversions']:,.1f}", border=1, align="R")
            pdf.ln()

    pdf.ln(8)
    pdf.set_font(font_family, "", 8)
    pdf.cell(0, 5, f"Generated automatically on {date.today().isoformat()}", new_x="LMARGIN", new_y="NEXT", align="C")

    return pdf.output()


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

    invoice_attachments = []
    report_attachments = []
    summary_lines = []

    for customer_id in customer_ids:
        # 1) Try to fetch actual invoices via InvoiceService
        logger.info("Fetching invoices for customer %s ...", customer_id)
        invoices = fetch_invoices_for_account(client, customer_id)

        if invoices:
            credentials = client.oauth2_credentials
            for inv in invoices:
                try:
                    logger.info("Downloading invoice PDF %s ...", inv["invoice_id"])
                    pdf_bytes = download_invoice_pdf(inv["pdf_url"], credentials)
                    filename = f"invoice_{customer_id}_{inv['invoice_id']}.pdf"
                    invoice_attachments.append({
                        "filename": filename,
                        "data": pdf_bytes,
                        "mimetype": "application/pdf",
                    })
                    summary_lines.append(
                        f"  - Invoice {inv['invoice_id']} ({customer_id}): "
                        f"{inv['amount_micros'] / 1_000_000:,.2f} {inv['currency_code']}"
                    )
                except Exception as ex:
                    logger.error("Failed to download invoice %s: %s", inv["invoice_id"], ex)
        else:
            logger.info("No invoices found for customer %s via InvoiceService", customer_id)

        # 2) Also generate spending report
        logger.info("Fetching spending data for customer %s ...", customer_id)
        data = fetch_spending_for_account(client, customer_id, start_date, end_date)
        if data is not None:
            total_cost = data["total_cost_micros"] / 1_000_000
            logger.info(
                "Customer %s (%s): %.2f %s",
                customer_id, data["account_name"], total_cost, data["currency"],
            )
            if not invoices:
                summary_lines.append(
                    f"  - {data['account_name']} ({customer_id}): "
                    f"{total_cost:,.2f} {data['currency']} (spending report)"
                )

            logger.info("Generating PDF report for customer %s ...", customer_id)
            pdf_bytes = generate_pdf_report(data, start_date, end_date)
            filename = f"google_ads_report_{customer_id}_{start_date[:7]}.pdf"
            report_attachments.append({
                "filename": filename,
                "data": pdf_bytes,
                "mimetype": "application/pdf",
            })

    # Invoices go first, then spending reports
    attachments = invoice_attachments + report_attachments

    if not attachments:
        logger.warning("No invoices or spending data found for any account. Nothing to send.")
        return

    invoice_count = len(invoice_attachments)
    report_count = len(report_attachments)

    subject = f"Google Ads faktury – {period_label}"
    body = (
        f"Dobrý den,\n\n"
        f"v příloze naleznete faktury a reporty z Google Ads za období {period_label} "
        f"({start_date} – {end_date}).\n\n"
    )
    if invoice_count:
        body += f"Faktury: {invoice_count}\n"
    if report_count:
        body += f"Reporty útrat: {report_count}\n"
    body += (
        f"\nÚčty:\n"
        + "\n".join(summary_lines)
        + f"\n\nCelkem příloh: {len(attachments)}\n\n"
        f"Tato zpráva byla vygenerována automaticky.\n"
    )

    logger.info("Sending email to %s with %d attachment(s) (%d invoices, %d reports) ...",
                recipient_email, len(attachments), invoice_count, report_count)
    send_invoices_email(
        to=recipient_email,
        subject=subject,
        body=body,
        attachments=attachments,
    )
    logger.info("Done.")


if __name__ == "__main__":
    main()
