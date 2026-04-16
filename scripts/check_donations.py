#!/usr/bin/env python3
"""
check_donations.py
Runs on a daily GitHub Actions schedule. Checks the Contra Costa NetFile
campaign finance portal for new FPPC Form 497 contributions filed by
'Safe and Healthy Contra Costa County' (Yes on Measure B), parses any
new PDFs, updates scripts/donations.json, and regenerates the auto-update
sections of yes-on-b-funding.html using HTML comment markers.
"""

import io
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR     = Path(__file__).parent
REPO_ROOT      = SCRIPT_DIR.parent
DONATIONS_JSON = SCRIPT_DIR / "donations.json"
FUNDING_HTML   = REPO_ROOT  / "yes-on-b-funding.html"

# ---------------------------------------------------------------------------
# NetFile config
# ---------------------------------------------------------------------------
NETFILE_BASE = "https://public.netfile.com/Pub2"
FILER_URL    = NETFILE_BASE + "/AllFilingsByFiler.aspx?id=215686834&aid=CCC"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
def fetch_filing_list():
    """Return a list of Form 497 filings that have electronic PDFs."""
    print("Fetching filing list from NetFile...")
    session = requests.Session()

    # First GET to establish session cookies (ASP.NET WebForms sites need this)
    resp = session.get(FILER_URL, headers=HEADERS, timeout=30)
    print("  HTTP status: %d" % resp.status_code)
    resp.raise_for_status()

    # Debug: print first 500 chars of response to help diagnose parse issues
    preview = resp.text[:500].replace("\n", " ").replace("\r", "")
    print("  Response preview: %s" % preview)

    soup = BeautifulSoup(resp.text, "lxml")
    all_rows = soup.select("table tr")
    print("  Total table rows found: %d" % len(all_rows))

    filings = []

    for row in all_rows:
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        filing_id = cells[0].get_text(strip=True)
        form_type = cells[3].get_text(strip=True)

        # Only process Form 497 (large contribution reports)
        if "497" not in form_type:
            continue

        # Paper filings have no PDF link; skip them
        view_link = row.find("a", string=re.compile(r"^View$", re.I))
        if not view_link:
            continue

        pdf_href = view_link.get("href", "")
        if not pdf_href.startswith("http"):
            pdf_href = NETFILE_BASE + "/" + pdf_href.lstrip("/")

        filings.append({
            "filing_id":   filing_id,
            "form_type":   form_type,
            "filing_date": cells[2].get_text(strip=True),
            "pdf_url":     pdf_href,
        })

    print("  Found %d Form 497 filings." % len(filings))
    return filings


def extract_pdf_text(pdf_url):
    """Download a PDF and return its extracted plain text."""
    resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    buf = io.StringIO()
    extract_text_to_fp(io.BytesIO(resp.content), buf, laparams=LAParams())
    return buf.getvalue()


def parse_form_497(text, filing_id):
    """
    Extract contribution fields from an FPPC Form 497 PDF text.
    """
    # --- Amount -----------------------------------------------------------
    amounts = re.findall(r"(\d{1,3}(?:,\d{3})+\.\d{2})", text)
    eligible = [
        float(a.replace(",", ""))
        for a in amounts
        if float(a.replace(",", "")) >= 100
    ]
    if not eligible:
        print("  WARNING: no dollar amount in filing %s" % filing_id)
        return None
    amount = max(eligible)

    # --- Contribution date ------------------------------------------------
    all_dates = re.findall(r"(\d{2}/\d{2}/\d{4})", text)
    raw_date = all_dates[1] if len(all_dates) >= 2 else (all_dates[0] if all_dates else None)
    if not raw_date:
        print("  WARNING: no date in filing %s" % filing_id)
        return None
    try:
        contrib_date = datetime.strptime(raw_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        contrib_date = raw_date

    # --- Contributor name and committee ID --------------------------------
    m = re.search(
        r"\d{2}/\d{2}/\d{4}\s+(.+?)Committee ID #\s*(\d+)",
        text,
        re.DOTALL,
    )
    contributor  = "Unknown"
    location     = ""
    committee_id = ""

    if m:
        raw_name     = re.sub(r"\s+X\s*$", "", m.group(1).strip())
        committee_id = m.group(2).strip()
        combined = " ".join(raw_name.split())

        city_m = re.search(
            r"(.+?)\s+([A-Za-z][a-zA-Z\s]+,\s*[A-Z]{2})\s+\d{5}",
            combined,
        )
        if city_m:
            contributor = city_m.group(1).strip()
            location    = city_m.group(2).strip()
        else:
            contributor = combined

    return {
        "filing_id":    filing_id,
        "contributor":  contributor,
        "location":     location,
        "committee_id": committee_id,
        "date":         contrib_date,
        "amount":       amount,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_currency(amount):
    return "$%s" % "{:,.0f}".format(amount)


def fmt_currency_short(amount):
    if amount >= 1_000_000:
        return "$%.1fM" % (amount / 1_000_000)
    k = amount / 1_000
    if k == int(k):
        return "$%dK" % int(k)
    return "$%.1fK" % k


def fmt_date_long(iso):
    """'2026-03-16' -> 'March 16, 2026'"""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        try:
            return d.strftime("%B %-d, %Y")
        except ValueError:
            return d.strftime("%B %d, %Y").replace(" 0", " ")
    except (ValueError, AttributeError):
        return iso


def count_unique_donors(donations):
    return len({d["contributor"] for d in donations})


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------
def build_table_html(donations, total):
    rows = []
    for d in donations:
        name   = d.get("contributor", "Unknown")
        loc    = d.get("location", "")
        cid    = d.get("committee_id", "")
        dt_str = fmt_date_long(d.get("date", ""))
        amt    = fmt_currency(d.get("amount", 0))

        sub = ""
        if cid and loc:
            sub = ('<br><small style="color:var(--muted);">'
                   "Committee ID #%s &mdash; %s</small>" % (cid, loc))
        elif cid:
            sub = ('<br><small style="color:var(--muted);">'
                   "Committee ID #%s</small>" % cid)

        rows.append(
            "            <tr>\n"
            '              <td>%s%s</td>\n'
            '              <td class="col-date">%s</td>\n'
            '              <td class="col-amount">%s</td>\n'
            "            </tr>" % (name, sub, dt_str, amt)
        )

    rows.append(
        '            <tr class="total-row">\n'
        "              <td colspan=\"2\"><strong>Total raised</strong></td>\n"
        '              <td class="col-amount"><strong>%s</strong></td>\n'
        "            </tr>" % fmt_currency(total)
    )

    return (
        '        <table class="donor-table">\n'
        "          <thead>\n"
        "            <tr>\n"
        "              <th>Contributor</th>\n"
        '              <th class="col-date">Date</th>\n'
        '              <th class="col-amount">Amount</th>\n'
        "            </tr>\n"
        "          </thead>\n"
        "          <tbody>\n"
        + "\n".join(rows) + "\n"
        "          </tbody>\n"
        "        </table>"
    )


def build_stats_html(total, donors, checked_date):
    total_short = fmt_currency_short(total)
    date_long   = fmt_date_long(checked_date)
    return (
        '      <div class="stat-row">\n'
        '        <div class="stat-box">\n'
        '          <div class="num"><!-- AUTO:TOTAL -->%s<!-- /AUTO:TOTAL --></div>\n'
        '          <div class="label">Total raised by Yes on B as of '
        "<!-- AUTO:DATE -->%s<!-- /AUTO:DATE --></div>\n"
        "        </div>\n"
        '        <div class="stat-box">\n'
        '          <div class="num">100%%</div>\n'
        '          <div class="label">Share from public employee union PACs</div>\n'
        "        </div>\n"
        '        <div class="stat-box">\n'
        '          <div class="num"><!-- AUTO:DONORS -->%d<!-- /AUTO:DONORS --></div>\n'
        '          <div class="label">Donors on record &mdash; both representing county workers</div>\n'
        "        </div>\n"
        "      </div>"
    ) % (total_short, date_long, donors)


def update_html(donations, total, donors, checked_date):
    html = FUNDING_HTML.read_text(encoding="utf-8")

    new_table = build_table_html(donations, total)
    new_stats = build_stats_html(total, donors, checked_date)
    date_long = fmt_date_long(checked_date)

    html = re.sub(
        r"<!-- AUTO:TABLE-START -->.*?<!-- AUTO:TABLE-END -->",
        "<!-- AUTO:TABLE-START -->\n%s\n        <!-- AUTO:TABLE-END -->" % new_table,
        html,
        flags=re.DOTALL,
    )

    html = re.sub(
        r"<!-- AUTO:STATS-START -->.*?<!-- AUTO:STATS-END -->",
        "<!-- AUTO:STATS-START -->\n%s\n      <!-- AUTO:STATS-END -->" % new_stats,
        html,
        flags=re.DOTALL,
    )

    html = re.sub(
        r"<!-- AUTO:AS-OF -->.*?<!-- /AUTO:AS-OF -->",
        "<!-- AUTO:AS-OF -->%s<!-- /AUTO:AS-OF -->" % date_long,
        html,
    )
    html = re.sub(
        r"<!-- AUTO:AS-OF-2 -->.*?<!-- /AUTO:AS-OF-2 -->",
        "<!-- AUTO:AS-OF-2 -->%s<!-- /AUTO:AS-OF-2 -->" % date_long,
        html,
    )

    FUNDING_HTML.write_text(html, encoding="utf-8")
    print("  Updated %s" % FUNDING_HTML.name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    state     = json.loads(DONATIONS_JSON.read_text())
    known_ids = set(state.get("filings_seen", []))
    donations = state.get("donations", [])

    try:
        filings = fetch_filing_list()
    except Exception as exc:
        # Print the error but exit 0 so the workflow doesn't show as failed
        # when the issue is a transient network problem with NetFile
        print("ERROR fetching filing list: %s" % exc, file=sys.stderr)
        print("Exiting without changes.")
        # Write false to GITHUB_OUTPUT so no commit is attempted
        gho = os.environ.get("GITHUB_OUTPUT")
        if gho:
            with open(gho, "a") as f:
                f.write("new_donations=false\n")
        sys.exit(0)

    today     = date.today().isoformat()
    new_found = False

    for filing in filings:
        fid = filing["filing_id"]
        if fid in known_ids:
            print("  Already seen filing %s -- skipping." % fid)
            continue

        print("  New filing: %s (%s)" % (fid, filing["filing_date"]))
        known_ids.add(fid)

        try:
            text   = extract_pdf_text(filing["pdf_url"])
            parsed = parse_form_497(text, fid)
            if parsed:
                donations.append(parsed)
                print("    %s: %s on %s"
                      % (parsed["contributor"],
                         fmt_currency(parsed["amount"]),
                         parsed["date"]))
                new_found = True
            else:
                print("    Could not parse filing %s -- skipping." % fid)
        except Exception as exc:
            print("    ERROR on %s: %s" % (fid, exc), file=sys.stderr)

    donations.sort(key=lambda d: d.get("date", ""))

    total  = sum(d.get("amount", 0) for d in donations)
    donors = count_unique_donors(donations)

    state.update({
        "donations":     donations,
        "total":         total,
        "unique_donors": donors,
        "filings_seen":  list(known_ids),
        "last_checked":  today,
    })
    if new_found:
        state["last_updated"] = today

    DONATIONS_JSON.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print("Saved donations.json -- %d donations, total %s"
          % (len(donations), fmt_currency(total)))

    if new_found:
        print("Updating yes-on-b-funding.html...")
        update_html(donations, total, donors, today)

    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as f:
            f.write("new_donations=%s\n" % ("true" if new_found else "false"))

    print("Done.")


if __name__ == "__main__":
    main()
