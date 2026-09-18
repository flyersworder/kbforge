import pytest
from kbforge_web_source.clean import allowed, clean_markdown, domains_from


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.bosch-semiconductors.com/stories/eg120/", True),
        ("https://bosch-semiconductors.com/x", True),
        ("https://news.ti.com/2026/release", True),
        ("https://www.linkedin.com/posts/ti-demo", False),
        # a suffix match must fall on a label boundary
        ("https://evilbosch-semiconductors.com/x", False),
        ("https://bosch-semiconductors.com.attacker.net/x", False),
        ("not a url", False),
    ],
)
def test_allowed_matches_whole_domain_labels(url, expected):
    assert allowed(url, ("bosch-semiconductors.com", "ti.com")) is expected


def test_an_explicit_star_allows_every_http_url():
    assert allowed("https://anything.example/x", ("*",)) is True
    assert allowed("ftp://anything.example/x", ("*",)) is False


def test_domains_from_parses_and_normalizes():
    assert domains_from(" ti.com, .Bosch-Semiconductors.COM ,, ") == (
        "ti.com",
        "bosch-semiconductors.com",
    )


def test_domains_from_refuses_an_empty_allowlist():
    # Fail closed: a search source with no allowlist would read the whole web.
    with pytest.raises(ValueError, match="WEB_SOURCE_ALLOWED_DOMAINS"):
        domains_from("  ")


def test_clean_markdown_drops_per_request_challenge_links_keeping_text():
    md = (
        "Verifying...\n"
        "Stuck? [Troubleshoot](https://challenges.cloudflare.com/cdn-cgi/"
        "challenge-platform/h/g/turnstile/f/av0/rch/gwxmk/0x4AA)\n"
        "[Cloudflare](https://www.cloudflare.com/products/turnstile/)"
    )
    assert clean_markdown(md) == (
        "Verifying...\n"
        "Stuck? Troubleshoot\n"
        "[Cloudflare](https://www.cloudflare.com/products/turnstile/)"
    )


def test_clean_markdown_makes_two_fetches_of_one_page_identical():
    token = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/{}"
    a = f"# Title\n\n[Refresh]({token.format('gwxmk/0x4')})\n\nBody."
    b = f"# Title\n\n[Refresh]({token.format('fn90s/0x4')})\n\nBody."
    assert clean_markdown(a) == clean_markdown(b)


def test_clean_markdown_leaves_ordinary_links_and_trailing_space_alone():
    md = "See [the datasheet](https://example.com/ds.pdf?rev=3).  \nNext line"
    assert clean_markdown(md) == md
