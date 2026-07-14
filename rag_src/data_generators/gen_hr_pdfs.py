"""Generate the Kai Components HR-policy PDF corpus (UNSTRUCTURED RAG source).

Kai Components is a FICTIONAL employee-owned Hawaiian electronics-components retailer
(Arduino boards, sensors, MOSFETs, marine electronics, solar, tools) with stores across
the Hawaiian islands — the same business the structured sales RAG answers about. These
six policy PDFs are the source documents for the HR (semantic / unstructured) scenario.

Content is DETERMINISTIC (no randomness) so the golden Q&A set in
`eval/golden/hr_qa.jsonl` stays stable across regenerations. Every figure here is
fictional. The six files map 1:1 onto the S3 Vectors `hr-policies` index schema; the
filename `HR-LEAVE-001_leave.pdf` encodes doc_id + policy_category for
`arbiter_rag.loaders.iter_hr_documents`.

Requires the `data` extra (reportlab).  Run:
    python rag_src/data_generators/gen_hr_pdfs.py
    # or, offline-friendly path resolution regardless of cwd:
    python -m data_generators.gen_hr_pdfs   # from inside rag_src/
"""
from __future__ import annotations

import sys
from pathlib import Path

# --- Bootstrap: make `arbiter_rag` importable without an editable install ---------
_RAG_SRC = Path(__file__).resolve().parents[1]  # rag_src/
if str(_RAG_SRC) not in sys.path:
    sys.path.insert(0, str(_RAG_SRC))

from arbiter_rag.config import DATA_ROOT  # noqa: E402

OUT_DIR = DATA_ROOT / "Hawaii_HR_Policies"
COMPANY = "Kai Components"

# Each policy -> one PDF. `category` becomes the S3 Vectors filterable metadata
# `policy_category`. `sections` is a list of (heading, [body paragraphs]) or
# (heading, [body paragraphs], table_rows). Keep numbers in sync with the golden set.
HR_POLICIES: list[dict] = [
    {
        "doc_id": "HR-LEAVE-001",
        "title": "Paid Time Off and Leave Policy",
        "category": "leave",
        "effective_date": "2025-01-01",
        "sections": [
            (
                "1. Purpose and Scope",
                [
                    f"This policy describes paid time off (PTO), sick leave, and other leaves of "
                    f"absence available to {COMPANY} retail associates and store leadership across "
                    f"all Hawaiian island store locations. It applies to regular full-time and "
                    f"part-time employees. Seasonal and temporary employees are not eligible for "
                    f"PTO accrual.",
                ],
            ),
            (
                "2. PTO Accrual",
                [
                    "Full-time retail associates accrue paid time off based on length of continuous "
                    "service, as shown in the table below. PTO accrues each pay period and may be "
                    "used for vacation, personal time, or unplanned absences.",
                    "Part-time associates who work at least 20 hours per week accrue PTO at 50 "
                    "percent of the full-time rate for their service tier.",
                ],
                [
                    ["Years of Service", "Annual PTO (Full-Time)", "Accrual per Pay Period"],
                    ["0 to 2 years", "15 days (120 hours)", "4.62 hours"],
                    ["3 to 4 years", "20 days (160 hours)", "6.15 hours"],
                    ["5+ years", "25 days (200 hours)", "7.69 hours"],
                ],
            ),
            (
                "3. Carryover and Payout",
                [
                    "Employees may carry over a maximum of 40 hours of unused PTO into the following "
                    "calendar year. Any balance above 40 hours on December 31 is forfeited. Upon "
                    "voluntary separation with two weeks notice, accrued and unused PTO up to 80 "
                    "hours is paid out in the final paycheck.",
                ],
            ),
            (
                "4. Sick Leave",
                [
                    "In addition to PTO, full-time employees receive 7 paid sick days (56 hours) per "
                    "calendar year, available on the first day of employment. Sick leave does not "
                    "carry over and is not paid out at separation. A doctor's note is required for "
                    "absences of three or more consecutive scheduled shifts.",
                ],
            ),
            (
                "5. Parental and Family Leave",
                [
                    "Eligible employees receive 12 weeks of paid parental leave following the birth, "
                    "adoption, or foster placement of a child, paid at 100 percent of base pay. To "
                    "be eligible an employee must have completed 12 months of continuous service. "
                    "Parental leave runs concurrently with any leave available under the federal "
                    "Family and Medical Leave Act.",
                ],
            ),
            (
                "6. Bereavement Leave",
                [
                    "Employees may take up to 5 paid days of bereavement leave for the death of an "
                    "immediate family member, and up to 2 paid days for an extended family member. "
                    "Additional unpaid time may be arranged with store leadership.",
                ],
            ),
        ],
    },
    {
        "doc_id": "HR-BEN-002",
        "title": "Employee Benefits Policy",
        "category": "benefits",
        "effective_date": "2025-01-01",
        "sections": [
            (
                "1. Eligibility",
                [
                    f"Employees scheduled to work 30 or more hours per week are eligible for "
                    f"{COMPANY} medical, dental, and vision benefits on the first day of the month "
                    f"following 30 days of employment. Coverage may be extended to spouses, domestic "
                    f"partners, and dependent children.",
                ],
            ),
            (
                "2. Medical, Dental, and Vision",
                [
                    f"{COMPANY} pays 80 percent of the medical premium for employee-only coverage "
                    f"and 60 percent of the premium for covered dependents. Dental and vision "
                    f"premiums for employee-only coverage are fully paid by the company. A "
                    f"high-deductible plan option includes an annual company HSA contribution of "
                    f"$600 for individual and $1,200 for family coverage.",
                ],
            ),
            (
                "3. Retirement — 401(k)",
                [
                    "The company matches 100 percent of employee 401(k) contributions up to 4 "
                    "percent of eligible pay. Employer matching contributions vest on a three-year "
                    "cliff schedule: an employee who separates before completing three years of "
                    "service forfeits all unvested match. Employee contributions are always 100 "
                    "percent vested.",
                ],
            ),
            (
                "4. Life and Disability",
                [
                    "The company provides basic life insurance equal to one times annual base pay at "
                    "no cost, plus short-term disability covering 60 percent of base pay for up to "
                    "12 weeks. Employees may purchase supplemental life and long-term disability "
                    "coverage through payroll deduction.",
                ],
            ),
        ],
    },
    {
        "doc_id": "HR-COMP-003",
        "title": "Sales Compensation and Commission Policy",
        "category": "compensation",
        "effective_date": "2025-02-01",
        "sections": [
            (
                "1. Base Pay and Commission Overview",
                [
                    f"{COMPANY} retail associates are paid an hourly base wage plus monthly sales "
                    f"commission. Commission rewards net sales above an individual monthly quota and "
                    f"is calculated on components and finished electronics sold, net of returns and "
                    f"discounts.",
                ],
            ),
            (
                "2. Commission Rate",
                [
                    "Associates earn 4 percent commission on net component and electronics sales "
                    "above their monthly quota, and 6 percent on accessories and cables. Commission "
                    "is paid on the 15th of the following month, in the paycheck for that period.",
                ],
            ),
            (
                "3. Monthly Quota",
                [
                    "The standard monthly sales quota for a full-time associate is $18,000 in net "
                    "sales. Commission is paid only on net sales above this quota. Quotas are "
                    "prorated for part-time associates based on scheduled hours.",
                ],
            ),
            (
                "4. Returns and Clawback",
                [
                    "If merchandise is returned within 30 days of purchase, any commission paid on "
                    "that sale is clawed back from the associate's next commission payment. Returns "
                    "after 30 days do not affect previously paid commission.",
                ],
            ),
            (
                "5. Solar and Marine SPIFF",
                [
                    "To support higher-margin categories, the company pays a $25 SPIFF for each solar "
                    "power kit and each marine electronics unit sold, in addition to standard "
                    "commission. SPIFF amounts are not subject to the monthly quota.",
                ],
            ),
        ],
    },
    {
        "doc_id": "HR-CONDUCT-004",
        "title": "Code of Conduct and Store Standards",
        "category": "conduct",
        "effective_date": "2025-01-01",
        "sections": [
            (
                "1. Customer Service Standards",
                [
                    "Every customer must be greeted within 30 seconds of entering the store. "
                    "Associates are expected to offer knowledgeable help on component selection, "
                    "compatibility, and safe handling, and to complete the transaction courteously.",
                ],
            ),
            (
                "2. Attendance",
                [
                    "Reliable attendance is essential to store operations. Three no-call, no-show "
                    "occurrences within a rolling 12-month period result in termination. Employees "
                    "who cannot report for a scheduled shift must notify their manager at least 2 "
                    "hours before the shift start.",
                ],
            ),
            (
                "3. Electrostatic Discharge (ESD) Handling",
                [
                    "Bare boards, ICs, and static-sensitive components must be handled at an "
                    "ESD-safe workstation using a grounded wrist strap. Damaged or mishandled "
                    "static-sensitive stock must be quarantined and reported, not returned to "
                    "sellable inventory.",
                ],
            ),
            (
                "4. Confidentiality and Fair Dealing",
                [
                    "Employees must protect customer information and company pricing data, avoid "
                    "conflicts of interest, and never manipulate sales records to inflate "
                    "commission. Violations are subject to discipline up to and including "
                    "termination.",
                ],
            ),
        ],
    },
    {
        "doc_id": "HR-PAY-005",
        "title": "Payroll and Scheduling Policy",
        "category": "payroll",
        "effective_date": "2025-01-01",
        "sections": [
            (
                "1. Pay Frequency",
                [
                    "Employees are paid biweekly, every other Friday, by direct deposit. If a "
                    "scheduled payday falls on a bank holiday, pay is deposited on the preceding "
                    "business day.",
                ],
            ),
            (
                "2. Overtime",
                [
                    "Non-exempt employees earn overtime at 1.5 times their regular rate for hours "
                    "worked over 40 in a workweek, which runs Sunday through Saturday. All overtime "
                    "must be approved in advance by a manager.",
                ],
            ),
            (
                "3. Scheduling",
                [
                    "Work schedules are posted at least 14 days in advance. The minimum shift length "
                    "is 4 hours. Employees who wish to swap shifts must obtain manager approval and "
                    "record the change before the affected shift.",
                ],
            ),
            (
                "4. Meal and Rest Breaks",
                [
                    "Employees scheduled for more than 5 hours receive an unpaid 30-minute meal "
                    "break, and a paid 10-minute rest break for every 4 hours worked. Breaks may "
                    "not be skipped to leave early.",
                ],
            ),
        ],
    },
    {
        "doc_id": "HR-PERK-006",
        "title": "Employee Perks and Discounts Policy",
        "category": "perks",
        "effective_date": "2025-03-01",
        "sections": [
            (
                "1. Employee Discount",
                [
                    "Employees receive 40 percent off regular-price merchandise and 20 percent off "
                    "sale or clearance merchandise, up to a combined $2,000 in retail value per "
                    "calendar year. The discount is for personal and household use only and may not "
                    "be used to purchase for resale.",
                ],
            ),
            (
                "2. Quarterly Maker Credit",
                [
                    "Each employee may claim one free maker kit per calendar quarter, up to a retail "
                    "value of $150, from current-season inventory. Unused quarterly credits do not "
                    "roll over.",
                ],
            ),
            (
                "3. Tuition Reimbursement",
                [
                    "The company reimburses up to $3,000 per year for job-related coursework — "
                    "including electronics, business, and supply-chain topics — completed with a "
                    "grade of C or better. Employees must obtain approval before enrolling.",
                ],
            ),
            (
                "4. Referral Bonus",
                [
                    "Employees earn a $500 referral bonus for each referral who is hired and remains "
                    "employed for 90 days. There is no annual limit on the number of referral "
                    "bonuses an employee may earn.",
                ],
            ),
        ],
    },
]


def _styles() -> dict:
    """reportlab paragraph styles for the policy PDFs."""
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "PolicyTitle", parent=base["Title"], fontSize=17, leading=21, spaceAfter=6
        ),
        "subtitle": ParagraphStyle(
            "PolicySubtitle", parent=base["Normal"], fontSize=9, leading=12,
            textColor="#555555", alignment=TA_CENTER, spaceAfter=14,
        ),
        "heading": ParagraphStyle(
            "PolicyHeading", parent=base["Heading2"], fontSize=12, leading=15,
            spaceBefore=10, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "PolicyBody", parent=base["Normal"], fontSize=10, leading=14, spaceAfter=6
        ),
    }


def _render_policy(policy: dict, out_path: Path, styles: dict) -> None:
    """Render one policy dict to a PDF at out_path."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def _footer(canvas, doc):  # noqa: ANN001
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawCentredString(
            LETTER[0] / 2, 0.5 * inch,
            f"FICTIONAL SAMPLE — {COMPANY} HR — Page {doc.page}",
        )
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        title=policy["title"], author=f"{COMPANY} People Operations",
    )
    flow: list = [
        Paragraph(policy["title"], styles["title"]),
        # Subtitle starts with "Document " so iter_hr_documents skips it when picking the
        # title, but keeps it in-text so the "Effective YYYY-MM-DD" regex still matches.
        Paragraph(
            f"Document {policy['doc_id']} &nbsp;•&nbsp; Category: {policy['category']} "
            f"&nbsp;•&nbsp; Effective {policy['effective_date']} &nbsp;•&nbsp; "
            f"Applies to: all {COMPANY} store staff",
            styles["subtitle"],
        ),
    ]
    for section in policy["sections"]:
        heading, paragraphs = section[0], section[1]
        flow.append(Paragraph(heading, styles["heading"]))
        for para in paragraphs:
            flow.append(Paragraph(para, styles["body"]))
        if len(section) > 2:  # optional table
            rows = section[2]
            table = Table(rows, hAlign="LEFT")
            table.setStyle(
                TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12324a")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef3f7")]),
                    ("PADDING", (0, 0), (-1, -1), 5),
                ])
            )
            flow.append(Spacer(1, 4))
            flow.append(table)
    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)


def write_pdfs(out_dir: Path = OUT_DIR) -> list[Path]:
    """Render every policy to `<doc_id>_<category>.pdf` under out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    styles = _styles()
    written: list[Path] = []
    for policy in HR_POLICIES:
        path = out_dir / f"{policy['doc_id']}_{policy['category']}.pdf"
        _render_policy(policy, path, styles)
        written.append(path)
    return written


def main() -> None:
    print(f"{COMPANY} HR policy PDF generation")
    print("=" * 62)
    paths = write_pdfs()
    for p in paths:
        print(f"  wrote {p.name}  ({p.stat().st_size:,} bytes)")
    print(f"\nDone. {len(paths)} PDFs in {OUT_DIR}")


if __name__ == "__main__":
    main()
