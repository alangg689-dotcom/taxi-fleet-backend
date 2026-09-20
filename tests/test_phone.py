from app.core.phone import InvalidPhoneError, normalize_mx_phone, phone_lookup_values
import pytest


def test_normalize_ten_digits():
    assert normalize_mx_phone("6621234567") == "6621234567"


def test_normalize_e164():
    assert normalize_mx_phone("+52 662 123 4567") == "6621234567"
    assert normalize_mx_phone("+526621234567") == "6621234567"


def test_normalize_empty_is_none():
    assert normalize_mx_phone(None) is None
    assert normalize_mx_phone("  ") is None


def test_normalize_rejects_short():
    with pytest.raises(InvalidPhoneError):
        normalize_mx_phone("662123")


def test_lookup_includes_both_stored_forms():
    assert phone_lookup_values("+526621234567") == ["6621234567", "+526621234567"]
