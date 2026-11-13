"""ISIN class query pages in the site's own markup, for offline parser inputs."""

from __future__ import annotations

COLUMNS = (
    "頁面編號",
    "國際證券編碼",
    "有價證券代號",
    "有價證券名稱",
    "市場別",
    "有價證券別",
    "產業別",
    "公開發行/上市(櫃)/發行日",
    "CFICode",
    "備註",
)
# A query without results answers HTTP 200 with this redirect and no table.
NOT_FOUND = (
    b'<html>\n<head></head>\n<body>\n<SCRIPT LANGUAGE="JAVASCRIPT">\n'
    b'  window.location.replace("class_nofind.html")\n</SCRIPT>\n'
)


def class_page(
    rows: list[tuple[str, str, str]], *, otc: bool = False, security_type: str = "股票"
) -> bytes:
    """One query result; each row is ``(code, CFI code, listing date)``."""
    market = "上櫃" if otc else "上市"
    html = (
        "<html>\n<head></head>\n<body>\n<table align=center>\n"
        "<h2><strong>證券編碼＿分類查詢結果</strong></h2>\n</table>\n"
        "<table class='h4' border=0 align=center>\n<tr align=center>\n"
        + "".join(f"<td bgcolor=#D5FFD5>{column}</td>\n" for column in COLUMNS)
        + "</tr>\n"
    )
    for number, (symbol, cfi, listed) in enumerate(rows, 1):
        html += (
            f"<tr>\n<td align=center> {number} </td>\n<td>TW000{symbol}</td>\n"
            f"<td align=center>{symbol}</td>\n<td>名稱</td>\n<td>{market} </td>\n"
            f"<td>{security_type}</td>\n<td></td>\n<td align=center>{listed}</td>\n"
            f"<td>{cfi}</td>\n<td></td>\n</tr>\n"
        )
    return (html + "</table>\n</body>\n</html>\n").encode("cp950")
