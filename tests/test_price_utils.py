import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_bot(tmp_path):
    os.environ["DATABASE_PATH"] = str(tmp_path / "auction.db")
    os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
    os.environ.setdefault("SUPER_ADMIN_ID", "1")
    sys.modules.pop("bot", None)
    return importlib.import_module("bot")


def test_parse_price_supports_crore_lakh_and_plain_lakhs(tmp_path):
    bot = load_bot(tmp_path)

    assert bot.parse_price("2cr") == 200
    assert bot.parse_price("2.5 cr") == 250
    assert bot.parse_price("50l") == 50
    assert bot.parse_price("75") == 75


def test_parse_price_rejects_invalid_values(tmp_path):
    bot = load_bot(tmp_path)

    assert bot.parse_price("abc") is None
    assert bot.parse_price("2 crore") is None


def test_format_price_uses_lakhs_below_one_crore(tmp_path):
    bot = load_bot(tmp_path)

    assert bot.fmt(50) == "Rs.50L"
    assert bot.fmt(200) == "Rs.2Cr"
    assert bot.fmt(250) == "Rs.2.5Cr"
