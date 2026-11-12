"""财务数据的公共工具。"""
from datetime import date, timedelta


def avail_date(report_date: date) -> date:
    """报告期 → 法定披露截止日，避免用到未披露的财报（前视偏差）。"""
    md = (report_date.month, report_date.day)
    if md == (3, 31):
        return date(report_date.year, 4, 30)
    if md == (6, 30):
        return date(report_date.year, 8, 31)
    if md == (9, 30):
        return date(report_date.year, 10, 31)
    if md == (12, 31):
        return date(report_date.year + 1, 4, 30)
    return report_date + timedelta(days=120)
