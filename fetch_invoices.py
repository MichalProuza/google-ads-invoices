"""
Google Ads – monthly spending reminder.

Queries the previous month's spending per campaign from one or more Google Ads
accounts and sends a summary email with a direct link to the billing documents
page so the accountant can download the official tax receipts.
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

BILLING_DOCS_URL = "https://ads.google.com/aw/billing/documents"


def get_last_month_range() -> tuple[str, str]:
    """Returns (start_date, end_date) strings for the previous calendar month."""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - relativedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return last_month_start.strftime("%Y-%m-%d"), last_month_end.strftime("%Y-%m-%d")


def fetch_spending_for_account(
    client: GoogleAdsClient, customer_id: str, start_date: str, end_date: str
) -> dict:
    """
    Queries total and per-campaign spending for the given date range.

    Returns a dict:
        {
            "customer_id": str,
            "account_name": str,
            "total_micros": int,
            "currency": str,
            "campaigns": [{"name": str, "cost_micros": int}, ...],
        }
    """
    ga_service = client.get_service("GoogleAdsService")

    # Account-level total
    account_query = f"""
        SELECT
            customer.descriptive_name,
            metrics.cost_micros
        FROM customer
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    """
    total_micros = 0
    account_name = customer_id
    currency = "CZK"

    try:
        response = ga_service.search(customer_id=customer_id, query=account_query)
        for row in response:
            account_name = row.customer.descriptive_name or customer_id
            total_micros += row.metrics.cost_micros
    except GoogleAdsException as ex:
        logger.error("Failed to query account spending for %s: %s", customer_id, ex.failure)
        return {}

    # Per-campaign breakdown
    campaign_query = f"""
        SELECT
            campaign.name,
            metrics.cost_micros
        FROM campaign
        WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND metrics.cost_micros > 0
        ORDER BY metrics.cost_micros DESC
    """
    campaigns = []
    try:
        response = ga_service.search(customer_id=customer_id, query=campaign_query)
        for row in response:
            campaigns.append({
                "name": row.campaign.name,
                "cost_micros": row.metrics.cost_micros,
            })
            currency = "CZK"  # Google Ads API returns cost in account currency
    except GoogleAdsException as ex:
        logger.warning("Failed to query campaign spending for %s: %s", customer_id, ex.failure)

    # Try to get actual currency from account settings
    try:
        currency_query = "SELECT customer.currency_code FROM customer LIMIT 1"
        response = ga_service.search(customer_id=customer_id, query=currency_query)
        for row in response:
            currency = row.customer.currency_code
    except GoogleAdsException:
        pass

    return {
        "customer_id": customer_id,
        "account_name": account_name,
        "total_micros": total_micros,
        "currency": currency,
        "campaigns": campaigns,
    }


def format_czk(micros: int) -> str:
    """Formats micros amount as a human-readable string with CZK-style formatting."""
    amount = micros / 1_000_000
    return f"{amount:,.2f}".replace(",", " ")


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

    account_sections = []
    has_spending = False

    for customer_id in customer_ids:
        logger.info("Fetching spending for customer %s ...", customer_id)
        spending = fetch_spending_for_account(client, customer_id, start_date, end_date)

        if not spending:
            logger.warning("Could not retrieve spending for customer %s", customer_id)
            continue

        cid_formatted = f"{customer_id[:3]}-{customer_id[3:6]}-{customer_id[6:]}"
        docs_link = f"{BILLING_DOCS_URL}?ocid={customer_id}"

        section = (
            f"Účet: {spending['account_name']} ({cid_formatted})\n"
            f"  Celková útrata: {format_czk(spending['total_micros'])} {spending['currency']}\n"
        )

        if spending["campaigns"]:
            section += "  Kampaně:\n"
            for c in spending["campaigns"]:
                section += f"    - {c['name']}: {format_czk(c['cost_micros'])} {spending['currency']}\n"

        section += f"\n  Daňové doklady ke stažení:\n  {docs_link}\n"

        account_sections.append(section)
        if spending["total_micros"] > 0:
            has_spending = True

    if not account_sections:
        logger.warning("No spending data for any account. Nothing to send.")
        return

    subject = f"Google Ads – přehled útrat za {period_label}"
    body = (
        f"Dobrý den,\n\n"
        f"přehled útrat z Google Ads za období {period_label} "
        f"({start_date} – {end_date}):\n\n"
        + "\n".join(account_sections)
        + "\n"
        + "─" * 50 + "\n"
        + "Připomínka: Nezapomeňte stáhnout oficiální daňové doklady\n"
        + "z Google Ads (odkazy výše) a předat je do účetnictví.\n\n"
        + "Tato zpráva byla vygenerována automaticky.\n"
    )

    logger.info("Sending reminder email to %s ...", recipient_email)
    send_invoices_email(
        to=recipient_email,
        subject=subject,
        body=body,
        attachments=[],
    )
    logger.info("Done.")


if __name__ == "__main__":
    main()
