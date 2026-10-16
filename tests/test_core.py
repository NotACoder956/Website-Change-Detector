from app.core import normalize_page, text_diff, summarize_diff, validate_public_url
import pytest


def test_normalize_ignores_script_and_style():
    a = normalize_page('<html><head><title>A</title><style>.x{}</style></head><body><p>Hello</p><script>secret()</script></body></html>')
    b = normalize_page('<html><head><title>A</title><style>.y{}</style></head><body><p>Hello</p><script>other()</script></body></html>')
    assert a['hash'] == b['hash']
    assert a['text'] == 'Hello'


def test_selector_and_ignore_selectors():
    page = normalize_page('<main><div class="ad">ad changes</div><section id="main">Deadline 10 Nov</section></main>', '#main', '.ad')
    assert page['text'] == 'Deadline 10 Nov'


def test_diff_reports_addition_and_removal():
    result = text_diff('Deadline: 10 Nov', 'Deadline: 15 Nov')
    assert result['changes']
    assert '10 Nov' in result['changes'][0]['old']
    assert '15 Nov' in result['changes'][0]['new']
    assert 'deadline' in summarize_diff(result, 'deadline').lower()


def test_reject_non_http_scheme():
    with pytest.raises(ValueError):
        validate_public_url('file:///etc/passwd')


def test_reject_loopback_except_demo_path():
    with pytest.raises(ValueError):
        validate_public_url('http://127.0.0.1:8000/admin')
    assert validate_public_url('http://127.0.0.1:8000/demo/page')
