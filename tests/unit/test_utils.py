import pytest
from datetime import datetime, timedelta
from core.utils import parse_date

def test_parse_date_none_and_empty():
    assert parse_date(None) is None
    assert parse_date("") is None
    assert parse_date("   ") is None

def test_parse_date_keywords():
    now = datetime.utcnow() - timedelta(hours=5)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    assert parse_date("today") == today_str
    assert parse_date("TODAY ") == today_str
    assert parse_date("yesterday") == yesterday_str
    assert parse_date("tomorrow") == tomorrow_str

def test_parse_date_relative_offsets():
    now = datetime.utcnow() - timedelta(hours=5)
    
    plus_two = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    minus_five = (now - timedelta(days=5)).strftime("%Y-%m-%d")
    
    assert parse_date("+2") == plus_two
    assert parse_date("-5") == minus_five
    
    # Invalid relative offsets should return None
    assert parse_date("+invalid") is None
    assert parse_date("-") is None

def test_parse_date_absolute_formats():
    now = datetime.utcnow() - timedelta(hours=5)
    current_year = now.year
    
    # Slash formatting
    assert parse_date("10/15/2025") == "2025-10-15"
    assert parse_date("04/07/26") == "2026-04-07"
    assert parse_date("5/9") == f"{current_year}-05-09"
    
    # Dash formatting
    assert parse_date("12-25-2024") == "2024-12-25"
    assert parse_date("1-1-30") == "2030-01-01"
    assert parse_date("08-14") == f"{current_year}-08-14"

def test_parse_date_invalid_formats():
    assert parse_date("not a date") is None
    assert parse_date("12/abc/2025") is None
    assert parse_date("12/31/abc") is None
    assert parse_date("12/") is None
