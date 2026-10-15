"""`manage.compose.build_new` — pure function, no server. A new mail has no original to
take a recipient from, so every header value is caller input and is checked here."""

import pytest

from mailbox_cleanup.manage.compose import MAX_RECIPIENTS, ComposeError, build_new


def _new(**kw):
    base = dict(
        from_addr="me@example.com",
        to=["alex@example.org"],
        cc=[],
        subject="Offer for the workshop",
        body="Hello Alex",
    )
    base.update(kw)
    return build_new(**base)


def test_new_mail_has_the_given_headers_and_no_threading():
    msg = _new(cc=["sam@example.org", "kim@example.org"])
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "alex@example.org"
    assert msg["Cc"] == "sam@example.org, kim@example.org"
    assert msg["Subject"] == "Offer for the workshop"
    assert msg["Message-ID"] and msg["Date"]
    assert msg["In-Reply-To"] is None and msg["References"] is None
    assert msg["Bcc"] is None
    assert msg.get_content().strip() == "Hello Alex"


def test_no_cc_means_no_cc_header():
    assert _new()["Cc"] is None


def test_several_to_addresses_are_all_written():
    msg = _new(to=["alex@example.org", "sam@example.org"])
    assert [a.addr_spec for a in msg["To"].addresses] == ["alex@example.org", "sam@example.org"]


def test_non_ascii_subject_is_encoded_on_the_wire():
    raw = _new(subject="Rückfrage zum Angebot").as_bytes()
    assert b"=?utf-8?" in raw.lower()


@pytest.mark.parametrize(
    "subject",
    ["Hello\r\nBcc: evil@example.org", "Hello\nX-Injected: 1", "Hello\x00there", "Hello\x85there"],
)
def test_control_characters_in_the_subject_collapse_to_a_space(subject):
    msg = _new(subject=subject)
    assert "\r" not in msg["Subject"] and "\n" not in msg["Subject"]
    assert msg["Bcc"] is None and msg["X-Injected"] is None
    raw = msg.as_bytes()
    assert b"\nBcc:" not in raw and b"\nX-Injected:" not in raw


@pytest.mark.parametrize("subject", ["", "   ", "\r\n", "\x00\t"])
def test_a_subject_that_is_empty_after_cleaning_is_refused(subject):
    with pytest.raises(ComposeError, match="subject"):
        _new(subject=subject)


@pytest.mark.parametrize(
    "addr",
    [
        "Alex Example <alex@example.org>",  # display name
        "alex@example.org, evil@example.org",  # two addresses in one value
        "alex@@example.org",
        "alex@example.org@evil.example",  # a second '@'
        "alex@example.org\r\nBcc: evil@example.org",
        "alex @example.org",
        "alex",
        "@example.org",
        "alex@",
        "",
        "müller@example.org",  # non-ASCII: the mail library would mangle it silently
        "alex@münchen.example",
    ],
)
@pytest.mark.parametrize("field", ["to", "cc"])
def test_anything_but_one_bare_ascii_address_is_refused(field, addr):
    kw = {"to": ["ok@example.org"], "cc": []}
    kw[field] = [*kw[field], addr]
    with pytest.raises(ComposeError) as e:
        _new(**kw)
    assert f"--{field}" in str(e.value)
    # the message names the rule, never the value
    assert "evil" not in str(e.value) and "alex" not in str(e.value)


def test_no_recipient_is_refused():
    with pytest.raises(ComposeError, match="--to"):
        _new(to=[])


def test_cc_alone_is_not_a_recipient_list():
    with pytest.raises(ComposeError, match="--to"):
        _new(to=[], cc=["sam@example.org"])


def test_recipient_cap_counts_to_and_cc_together():
    ok_to = [f"t{i}@example.org" for i in range(6)]
    ok_cc = [f"c{i}@example.org" for i in range(MAX_RECIPIENTS - 6)]
    assert _new(to=ok_to, cc=ok_cc)["Cc"]
    with pytest.raises(ComposeError, match=str(MAX_RECIPIENTS)):
        _new(to=ok_to, cc=[*ok_cc, "one-too-many@example.org"])


def test_cap_is_ten():
    assert MAX_RECIPIENTS == 10


def test_reply_and_new_mail_share_one_address_check():
    """Design §3: shared code, not copied code."""
    import mailbox_cleanup.manage.compose as compose_mod
    import mailbox_cleanup.manage.draft as draft_mod
    import mailbox_cleanup.manage.headers as headers_mod

    assert draft_mod.is_bare_address is headers_mod.is_bare_address
    assert compose_mod.is_bare_address is headers_mod.is_bare_address
    assert draft_mod.clean_header_value is headers_mod.clean_header_value
    assert compose_mod.clean_header_value is headers_mod.clean_header_value


# --- validator and writer must agree on what one address is ------------------------------
# Security review of 25519cf: a denylist check accepted RFC 2047 encoded-words, comments
# and group syntax, which the mail library then decoded into something else. Measured on
# Python 3.11: one accepted value became two recipients in the stored draft.


@pytest.mark.parametrize(
    "addr",
    [
        "=?utf-8?q?evil=40x.org=2C_b?=@example.org",  # decodes to two recipients
        "=?utf-8?q?a=0D=0ABcc:_evil=40x.org?=@example.org",  # decodes to CR LF + a header
        "=?utf-8?b?ZXZpbEB4Lm9yZw==?=@example.org",
        "a(comment)@example.org",
        "a(b@example.org",
        "group:b@example.org;",
        "a@[192.0.2.1]",
        "a@[192.0.2.1",
        "a\\b@example.org",
        "a@example.org.",
        ".a@example.org",
        "a..b@example.org",
        "a@-bad.example.org",
        "a@example..org",
    ],
)
@pytest.mark.parametrize("field", ["to", "cc"])
def test_values_the_mail_library_would_reinterpret_are_refused(field, addr):
    kw = {"to": ["ok@example.org"], "cc": []}
    kw[field] = [*kw[field], addr]
    with pytest.raises(ComposeError):
        _new(**kw)


@pytest.mark.parametrize(
    "addr",
    [
        "alex@example.org",
        "a.b+tag@sub.example.org",
        "o'neil_1@mail-host.example.org",
        "x=y@example.org",
    ],
)
def test_ordinary_addresses_are_accepted_and_stored_as_typed(addr):
    msg = _new(to=[addr], cc=["sam@example.org"])
    assert [a.addr_spec for a in msg["To"].addresses] == [addr]
    assert all(a.display_name == "" for a in msg["To"].addresses)
    line = next(ln for ln in msg.as_bytes().split(b"\n") if ln.lower().startswith(b"to:"))
    assert line.strip() == b"To: " + addr.encode()


def test_an_encoded_word_is_refused_even_with_the_address_rule_switched_off(monkeypatch):
    """Second lock: the value is written as an address object, and the mail library
    refuses an encoded-word there by itself."""
    import mailbox_cleanup.manage.headers as compose_mod

    monkeypatch.setattr(compose_mod, "is_bare_address", lambda v: True)  # first lock off
    with pytest.raises(ComposeError, match="--to"):
        _new(to=["=?utf-8?q?evil=40x.org=2C_b?=@example.org"])


@pytest.mark.parametrize("field", ["to", "cc"])
def test_a_header_that_holds_anything_but_the_given_addresses_is_refused(monkeypatch, field):
    """Third lock, the read-back: the header is compared with the input after it is set.
    No input is known that gets this far, so the parser surprise is simulated: the address
    object silently becomes a different address."""
    import mailbox_cleanup.manage.headers as compose_mod

    real = compose_mod.Address
    monkeypatch.setattr(
        compose_mod,
        "Address",
        lambda addr_spec: real(
            addr_spec="swapped@example.net" if addr_spec == "alex@example.org" else addr_spec
        ),
    )
    kw = {"to": ["ok@example.org"], "cc": []}
    kw[field] = ["alex@example.org"]
    with pytest.raises(ComposeError, match=f"--{field}"):
        _new(**kw)


def test_a_display_name_appearing_in_the_header_is_refused(monkeypatch):
    import mailbox_cleanup.manage.headers as compose_mod

    real = compose_mod.Address
    monkeypatch.setattr(
        compose_mod, "Address", lambda addr_spec: real(display_name="Boss", addr_spec=addr_spec)
    )
    with pytest.raises(ComposeError, match="--to"):
        _new()
