from backend.utils.http_error_text import (
    looks_like_html,
    safe_response_snippet,
    summarize_http_error,
)

CF_520_HTML = """<!DOCTYPE html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<html class="no-js" lang="en-US">
<head>
<title>ixacg.de | 520: Web server is returning an unknown error</title>
</head>
<body>
<div id="cf-wrapper">Cloudflare 520 error page</div>
</body>
</html>
"""


def test_looks_like_html_detects_cloudflare_ie_comments():
    assert looks_like_html(CF_520_HTML)
    assert looks_like_html("  <html lang='en'>")
    assert looks_like_html(f"upstream returned an invalid body: {CF_520_HTML}")
    assert not looks_like_html("Error: TelegramNewupload error")


def test_summarize_http_error_strips_host_and_html():
    message = summarize_http_error(520, CF_520_HTML, action="Upload failed")
    assert message == "Upload failed HTTP 520: Web server is returning an unknown error"
    assert "<!DOCTYPE" not in message
    assert "ixacg.de" not in message
    assert "oldie" not in message


def test_summarize_http_error_reads_title_when_status_is_200():
    message = summarize_http_error(200, CF_520_HTML, action="Upload failed")
    assert "520" in message
    assert "unknown error" in message.lower()
    assert "<html" not in message.lower()


def test_summarize_http_error_strips_prefixed_html():
    body = f"proxy response was not JSON: {CF_520_HTML}"
    message = summarize_http_error(502, body, action="Cloud request failed")
    assert "unknown error" in message.lower()
    assert "<!doctype" not in message.lower()
    assert "oldie" not in message.lower()
    assert "ixacg.de" not in message


def test_safe_response_snippet_keeps_plain_text():
    response = type("Resp", (), {"status_code": 500, "text": "boom\nplease retry"})()
    assert safe_response_snippet(response) == "boom please retry"
