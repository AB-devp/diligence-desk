"""Generates a small fictional deal room: python sample_data/make_samples.py"""
import csv, random
from pathlib import Path

import docx
import openpyxl

out = Path(__file__).parent
random.seed(7)

# Loan tape: 80 loans, one oversized borrower, a tail of delinquent/defaulted loans
names = ["Northwind Traders", "Harbor Logistics", "Pinecrest Farms", "Delta Textiles", "Orbit Retail", "Summit Clinics",
         "Blue Mesa Foods", "Ironbridge Steel", "Lakeside Hotels", "Cobalt Software", "Redwood Timber", "Atlas Transport"]
with open(out / "loan_tape.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["Loan ID", "Borrower", "Outstanding Balance", "Days Past Due", "Interest Rate", "Status", "Collateral"])
    for i in range(1, 81):
        b = "Ironbridge Steel" if i % 5 == 0 else random.choice(names)
        bal = random.randint(80, 400) * 1000 * (3 if b == "Ironbridge Steel" else 1)
        dpd = random.choices([0, 12, 38, 67, 104, 190], [62, 12, 8, 6, 7, 5])[0]
        status = "Default" if dpd > 90 else "Delinquent" if dpd > 30 else "Current"
        w.writerow([f"L{i:04d}", b, bal, dpd, round(random.uniform(.07, .16), 4), status, random.choice(["Real estate", "Equipment", "Unsecured"])])

# Financial statements
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Income statement"
ws.append(["Meridian Credit Partners - Income statement (USD thousands, FY2024 unaudited)"]); ws.append([])
ws.append(["Line item", "FY2022", "FY2023", "FY2024"])
for row in [("Interest income", 41200, 47900, 52600), ("Interest expense", 15100, 19800, 26400), ("Net interest income", 26100, 28100, 26200),
            ("Fee income", 4300, 4100, 3200), ("Operating expenses", 14800, 16900, 19100), ("Provisions for loan losses", 3900, 5200, 9800),
            ("Pre-tax profit", 11700, 10100, 500)]:
    ws.append(row)
bs = wb.create_sheet("Balance sheet"); bs.append(["Line item", "FY2022", "FY2023", "FY2024"])
for row in [("Gross loans", 402000, 455000, 498000), ("Loan loss reserve", 9200, 12100, 17400), ("Cash", 38000, 21000, 9500),
            ("Borrowings", 310000, 378000, 441000), ("Equity", 96000, 99000, 91000)]:
    bs.append(row)
wb.save(out / "financials.xlsx")

# Term sheet
d = docx.Document(); d.add_heading("Indicative Term Sheet - Acquisition of Meridian Credit Partners", 1)
for t in ["Purchase price: USD 62 million on a cash-free, debt-free basis, subject to confirmatory due diligence.",
          "Change of control: the senior facility with Harbor Bank (USD 250 million) may be accelerated on a change of control unless waived.",
          "Seller indemnity: capped at 8% of the purchase price, limited to 18 months. Loan portfolio quality is warranted as of 31 December 2024 only.",
          "Key person: the CEO and CRO must remain for 24 months; no retention terms have been agreed.",
          "Covenant: net debt to equity must remain below 5.0x under the Harbor Bank facility."]:
    d.add_paragraph(t, style="List Bullet")
d.save(out / "term_sheet.docx")
print("wrote", sorted(p.name for p in out.iterdir() if p.name != "make_samples.py"))
