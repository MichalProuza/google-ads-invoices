"""
Google Ads Billing Report
Fetches spending data for the previous month from one or more Google Ads accounts,
generates PDF reports, and sends them via Gmail API.
"""

import os
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


def fetch_spending_for_account(
    client: GoogleAdsClient, customer_id: str, start_date: str, end_date: str
) -> dict | None:
    """
    Fetches account-level and campaign-level spending for a date range.

    Returns a dict with account info and campaign breakdown, or None on error.
    """
    ga_service = client.get_service("GoogleAdsService")

    # Account-level totals
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

    # Campaign-level breakdown
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

    # Use DejaVu Sans for Unicode (Czech characters) support
    font_dir = _find_font_path()
    if font_dir:
        pdf.add_font("DejaVu", "", os.path.join(font_dir, "DejaVuSans.ttf"))
        pdf.add_font("DejaVu", "B", os.path.join(font_dir, "DejaVuSans-Bold.ttf"))
        font_family = "DejaVu"
    else:
        font_family = "Helvetica"

    currency = account_data["currency"]
    total_cost = account_data["total_cost_micros"] / 1_000_000

    # Title
    pdf.set_font(font_family, "B", 16)
    pdf.cell(0, 10, "Google Ads Billing Report", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)

    # Account info
    pdf.set_font(font_family, "", 11)
    pdf.cell(0, 7, f"Account: {account_data['account_name']} ({account_data['customer_id']})", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Period: {start_date}  -  {end_date}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Summary
    pdf.set_font(font_family, "B", 13)
    pdf.cell(0, 9, "Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font_family, "", 11)
    pdf.cell(0, 7, f"Total Cost: {total_cost:,.2f} {currency}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Impressions: {account_data['total_impressions']:,}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Clicks: {account_data['total_clicks']:,}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Conversions: {account_data['total_conversions']:,.1f}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Campaign breakdown table
    campaigns = account_data["campaigns"]
    if campaigns:
        pdf.set_font(font_family, "B", 13)
        pdf.cell(0, 9, "Campaign Breakdown", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        # Table header
        col_widths = [70, 30, 25, 25, 25]
        headers = ["Campaign", f"Cost ({currency})", "Impr.", "Clicks", "Conv."]
        pdf.set_font(font_family, "B", 9)
        for w, h in zip(col_widths, headers):
            pdf.cell(w, 7, h, border=1)
        pdf.ln()

        # Table rows
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

    # Footer
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

    attachments = []
    summary_lines = []

    for customer_id in customer_ids:
        logger.info("Fetching spending data for customer %s ...", customer_id)
        data = fetch_spending_for_account(client, customer_id, start_date, end_date)
        if data is None:
            continue

        total_cost = data["total_cost_micros"] / 1_000_000
        logger.info(
            "Customer %s (%s): %.2f %s",
            customer_id, data["account_name"], total_cost, data["currency"],
        )

        summary_lines.append(
            f"  - {data['account_name']} ({customer_id}): "
            f"{total_cost:,.2f} {data['currency']}"
        )

        # Generate PDF report
        logger.info("Generating PDF report for customer %s ...", customer_id)
        pdf_bytes = generate_pdf_report(data, start_date, end_date)
        filename = f"google_ads_report_{customer_id}_{start_date[:7]}.pdf"
        attachments.append({"filename": filename, "data": pdf_bytes, "mimetype": "application/pdf"})

    if not attachments:
        logger.warning("No spending data found for any account. Nothing to send.")
        return

    subject = f"Google Ads Billing Report – {period_label}"
    body = (
        f"Hi,\n\n"
        f"Please find attached the Google Ads billing report(s) for {period_label} "
        f"({start_date} – {end_date}).\n\n"
        f"Accounts:\n"
        + "\n".join(summary_lines)
        + f"\n\nReports attached: {len(attachments)}\n\n"
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
