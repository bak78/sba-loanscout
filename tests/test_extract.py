from sba_loanscout.extract import clean_text, extract_links


def test_strips_nav_chrome(page_factory):
    page = page_factory("growing_hvac.html")
    assert "Home About Careers" not in page.text
    assert "family owned hvac" in page.text.lower()


def test_keeps_footer_text(page_factory):
    """Footers carry Member FDIC, 501(c)(3), and location lists — the text
    that decides disqualifiers, so it must survive extraction."""
    page = page_factory("growing_hvac.html")
    assert "Privacy policy" in page.text


def test_keeps_title_and_description_separate(page_factory):
    page = page_factory("growing_hvac.html")
    assert "Ridgeline" in page.title
    assert "since 1998" in page.description
    # searchable folds all three together so signals can match metadata too
    assert "since 1998" in page.searchable


def test_script_content_is_removed(page_factory):
    page = page_factory("thin_spa.html")
    assert "__APP__" not in page.text


def test_thin_page_yields_almost_no_text(page_factory, rules):
    page = page_factory("thin_spa.html")
    assert len(page.text) < rules.thin_content_chars


def test_extract_links_resolves_and_dedupes():
    html = """
      <a href="/about">a</a>
      <a href="/about">dupe</a>
      <a href="/about#team">fragment of same page</a>
      <a href="https://other.com/x">absolute</a>
      <a href="mailto:x@y.com">mail</a>
      <a href="#top">anchor</a>
      <a href="javascript:void(0)">js</a>
    """
    links = extract_links(html, "https://example.com/home")

    assert "https://example.com/about" in links
    assert "https://other.com/x" in links
    assert links.count("https://example.com/about") == 1
    assert not any(l.startswith(("mailto:", "javascript:")) for l in links)


def test_clean_text_handles_missing_title_and_meta():
    title, description, text = clean_text("<html><body><p>hi</p></body></html>")
    assert title == ""
    assert description == ""
    assert text == "hi"
