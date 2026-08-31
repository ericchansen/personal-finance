import base64
import json
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest

from importers.simplefin.client import (
    SimpleFinError,
    build_url,
    claim_access_url,
    decode_setup_token,
    fetch,
    parse_accounts,
    split_credentials,
)

CLAIM = "https://bridge.example.org/simplefin/claim/abc123"
TOKEN = base64.b64encode(CLAIM.encode()).decode()
ACCESS = "https://user:pass@bridge.example.org/simplefin"

PAYLOAD = {
    "errors": [],
    "accounts": [
        {
            "org": {"name": "Example Bank", "domain": "example.com"},
            "id": "acct-1",
            "name": "Checking",
            "currency": "USD",
            "balance": "1234.56",
            "available-balance": "1200.00",
            "balance-date": 1787788800,
            "transactions": [
                {"id": "t2", "posted": 1787788800, "amount": "-45.20",
                 "description": "COFFEE"},
                {"id": "t1", "posted": 1787702400, "amount": "1000.00",
                 "description": "PAYROLL"},
            ],
        }
    ],
}


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def opener_returning(body):
    def _open(request, timeout=None):
        return FakeResponse(body if isinstance(body, bytes) else body.encode())
    return _open


def test_requests_carry_a_user_agent():
    # The Bridge sits behind Cloudflare, which rejects urllib's default agent
    # with error 1010. That surfaces as a bare 403 and is indistinguishable
    # from an already-claimed token -- an expensive confusion, because a setup
    # token is one-time use.
    seen = {}

    def capturing(request, timeout=None):
        seen["ua"] = request.get_header("User-agent")
        return FakeResponse(ACCESS.encode())

    claim_access_url(TOKEN, opener=capturing)
    assert seen["ua"] and "urllib" not in seen["ua"].lower()


def test_fetch_also_sends_a_user_agent():
    seen = {}

    def capturing(request, timeout=None):
        seen["ua"] = request.get_header("User-agent")
        return FakeResponse(json.dumps(PAYLOAD).encode())

    fetch(ACCESS, opener=capturing)
    assert seen["ua"] and "urllib" not in seen["ua"].lower()


# -- setup token ----------------------------------------------------------


def test_a_setup_token_decodes_to_its_claim_url():
    assert decode_setup_token(TOKEN) == CLAIM


def test_an_unpadded_token_still_decodes():
    # Bridge tokens are not always padded to a multiple of four.
    assert decode_setup_token(TOKEN.rstrip("=")) == CLAIM


def test_whitespace_from_copy_paste_is_tolerated():
    assert decode_setup_token(f"  {TOKEN}\n") == CLAIM


def test_an_empty_token_is_refused():
    with pytest.raises(SimpleFinError):
        decode_setup_token("   ")


def test_a_token_that_is_not_base64_is_refused():
    with pytest.raises(SimpleFinError):
        decode_setup_token("not a token!!")


def test_a_token_decoding_to_something_other_than_https_is_refused():
    with pytest.raises(SimpleFinError):
        decode_setup_token(base64.b64encode(b"http://insecure").decode())


def test_claiming_returns_the_access_url():
    assert claim_access_url(TOKEN, opener=opener_returning(ACCESS)) == ACCESS


def test_a_claim_response_that_is_not_a_url_is_refused():
    with pytest.raises(SimpleFinError):
        claim_access_url(TOKEN, opener=opener_returning("Token already claimed"))


# -- request shaping ------------------------------------------------------


def test_a_bare_request_asks_for_accounts():
    assert build_url(ACCESS) == ACCESS + "/accounts"


def test_a_trailing_slash_does_not_double_up():
    assert build_url(ACCESS + "/") == ACCESS + "/accounts"


def test_dates_are_sent_as_unix_seconds():
    url = build_url(ACCESS, start=date(2026, 8, 27))
    assert "start-date=1787788800" in url


def test_balances_only_is_requestable_for_cheap_assertion_checks():
    assert "balances-only=1" in build_url(ACCESS, balances_only=True)


def test_pending_is_off_unless_asked_for():
    assert "pending" not in build_url(ACCESS)
    assert "pending=1" in build_url(ACCESS, pending=True)


# -- embedded credentials -------------------------------------------------


def test_embedded_credentials_move_out_of_the_url():
    # urllib reads the text after the first colon as a port and fails with
    # "nonnumeric port", so userinfo has to become an Authorization header.
    clean, auth = split_credentials("https://user:secret@example.org/simplefin")
    assert clean == "https://example.org/simplefin"
    assert auth == "Basic " + base64.b64encode(b"user:secret").decode()


def test_a_url_without_credentials_is_left_alone():
    clean, auth = split_credentials("https://example.org/simplefin")
    assert clean == "https://example.org/simplefin"
    assert auth is None


def test_a_password_containing_an_at_sign_still_splits_correctly():
    # rpartition, not partition: the host is after the LAST @.
    clean, auth = split_credentials("https://user:p@ss@example.org/x")
    assert clean == "https://example.org/x"
    assert auth == "Basic " + base64.b64encode(b"user:p@ss").decode()


def test_the_query_string_survives_the_split():
    clean, _ = split_credentials("https://u:p@example.org/accounts?start-date=1")
    assert clean.endswith("/accounts?start-date=1")


def test_fetch_sends_credentials_as_a_header_not_in_the_url():
    seen = {}

    def capturing(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(json.dumps(PAYLOAD).encode())

    fetch("https://user:secret@example.org/simplefin", opener=capturing)
    assert "secret" not in seen["url"]
    assert seen["auth"].startswith("Basic ")


# -- parsing --------------------------------------------------------------


def test_an_account_is_parsed_with_its_institution():
    (account,), _ = parse_accounts(PAYLOAD)
    assert account.org == "Example Bank"
    assert account.name == "Checking"


def test_money_is_decimal_not_float():
    (account,), _ = parse_accounts(PAYLOAD)
    assert account.balance == Decimal("1234.56")
    assert isinstance(account.balance, Decimal)


def test_epoch_seconds_become_dates():
    (account,), _ = parse_accounts(PAYLOAD)
    assert account.balance_date == date(2026, 8, 27)


def test_transactions_are_sorted_by_date_not_response_order():
    (account,), _ = parse_accounts(PAYLOAD)
    assert [t.id for t in account.transactions] == ["t1", "t2"]


def test_transaction_signs_are_preserved():
    (account,), _ = parse_accounts(PAYLOAD)
    amounts = {t.id: t.amount for t in account.transactions}
    assert amounts["t1"] == Decimal("1000.00")
    assert amounts["t2"] == Decimal("-45.20")


def test_a_transaction_without_a_date_is_dropped_rather_than_guessed():
    payload = json.loads(json.dumps(PAYLOAD))
    payload["accounts"][0]["transactions"].append(
        {"id": "bad", "posted": None, "amount": "1.00", "description": "X"})
    (account,), _ = parse_accounts(payload)
    assert "bad" not in {t.id for t in account.transactions}


def test_connection_errors_are_returned_not_raised():
    # One dead institution must not discard the accounts that did work.
    payload = json.loads(json.dumps(PAYLOAD))
    payload["errors"] = ["Connection to Example Bank needs attention"]
    accounts, errors = parse_accounts(payload)
    assert len(accounts) == 1
    assert errors == ["Connection to Example Bank needs attention"]


def test_an_empty_response_yields_nothing_and_does_not_explode():
    accounts, errors = parse_accounts({})
    assert accounts == [] and errors == []


def test_a_missing_available_balance_stays_none():
    payload = json.loads(json.dumps(PAYLOAD))
    del payload["accounts"][0]["available-balance"]
    (account,), _ = parse_accounts(payload)
    assert account.available_balance is None


def test_a_dollar_account_is_reported_as_currency():
    (account,), _ = parse_accounts(PAYLOAD)
    assert account.is_currency


def test_a_security_denominated_account_is_not_treated_as_dollars():
    # SimpleFIN puts a URL in `currency` when the unit is not fiat, and the
    # balance then counts units. Treating that as dollars would be wrong.
    payload = json.loads(json.dumps(PAYLOAD))
    payload["accounts"][0]["currency"] = "https://example.com/currency/BTC"
    (account,), _ = parse_accounts(payload)
    assert not account.is_currency


def test_fetch_parses_a_live_style_response():
    accounts, errors = fetch(ACCESS, opener=opener_returning(json.dumps(PAYLOAD)))
    assert len(accounts) == 1 and errors == []


def test_fetch_wraps_transport_failures():
    def boom(request, timeout=None):
        raise OSError("connection reset")
    with pytest.raises(SimpleFinError):
        fetch(ACCESS, opener=boom)
