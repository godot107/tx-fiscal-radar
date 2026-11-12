"""The Power BI 'Ask' links: readable question, URL-encoded, never the real host in tracked report files."""
import json
from urllib.parse import parse_qs, urlparse

from txfr import powerbi


def test_bill_label():
    assert powerbi.bill_label("89R:SB2206") == ("SB 2206", "2025 regular session")
    assert powerbi.bill_label("90R:HB00012") == ("HB 12", "2027 session")


def test_ask_link_round_trips():
    url = powerbi.ask_link("https://example.test", powerbi.ASK_QUESTIONS["ai_review"], "892:HB16", "OFFICE OF COURT ADMINISTRATION")
    q = parse_qs(urlparse(url).query)["q"][0]
    assert url.startswith("https://example.test/?q=")
    assert q == "What does the fiscal note for HB 16 (2025 second called session) say Office Of Court Administration would pay?"
    assert len(q) <= 500   # the page's textarea limit


def test_report_files_hold_placeholder_not_host(tmp_path, monkeypatch):
    monkeypatch.setattr(powerbi, "OUT", tmp_path / "report")
    monkeypatch.setenv("APP_URL", "https://real-host.example")
    powerbi.write_definition("00000000-0000-0000-0000-000000000000", "ws")
    text = "".join(p.read_text() for p in (tmp_path / "report").rglob("*.json"))
    assert "real-host.example" not in text
    buttons = [json.loads(p.read_text()) for p in (tmp_path / "report").rglob("visual.json")
               if json.loads(p.read_text())["visual"]["visualType"] == "actionButton"]
    assert len(buttons) == len(powerbi.PAGES)
    assert powerbi.APP_PLACEHOLDER in json.dumps(buttons[0])
