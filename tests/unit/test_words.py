"""
The words Satori Lite's pages show follow the Satori app's glossary, its docs/words.md.

Each template's visible text (text nodes, and the placeholder, title, aria-label
and alt attributes) is read without its scripts, styles, comments, Jinja tags or
icon ligatures, and must not contain a never-say word. Text built in JS is not
seen here.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / 'web' / 'templates'
ATTRS = {'placeholder', 'title', 'aria-label', 'alt'}

NEVER = {
    'central': r'\bcentral\b',
    'server': r'\bservers?\b',
    'password': r'\bpasswords?\b',
    'log in / log out': r'\blog\s?(in|out)\b',
    'logged in / out': r'\blogged\s(in|out)\b',
    'vault': r'\bvaults?\b',
    'identity': r'\bidentit(y|ies)\b',
    'wallet pubkey': r'\bwallet pubkeys?\b',
    'Download Wallet': r'\bdownload wallet\b',
    'Import Wallet': r'\bimport wallet\b',
    'Satori Neuron': r'\bsatori neuron\b',
    'Neuron Dashboard': r'\bneuron dashboard\b',
    # The product is Satori Lite; "neurons" (plural) still names other people's, such as workers.
    'neuron for the product': r'\bneuron\b',
    'stake to': r'\b(stake[ds]?|staking)\s+to\b',
    'Pool Staking': r'\bpool staking\b',
    'stake / predict on Base': r'\b(stake[ds]?|staking|predicts?|predicting)\s+on\s+base\b',
    'earned / earnings': r'\bearn(ed|ings)\b',
    'the relay network': r'\brelay network\b',
    'Satori relays': r'\bsatori relays\b',
}

# Wording left as it is until its feature is decided (file, phrase). None of it trips a pattern
# today: the Evrmore payment channels and the SATORIEVR airdrop (they really run on Evrmore),
# "sats", Lite's local "Accuracy", "Rewards go to your wallet" and the navbar's "Connected".
ALLOW = [
]


def _strip(src):
    src = re.sub(r'\{#.*?#\}', ' ', src, flags=re.S)
    src = re.sub(r'<!--.*?-->', ' ', src, flags=re.S)
    src = re.sub(r'<script\b.*?</script\s*>', ' ', src, flags=re.S | re.I)
    src = re.sub(r'<style\b.*?</style\s*>', ' ', src, flags=re.S | re.I)
    src = re.sub(r'\{%.*?%\}', ' ', src, flags=re.S)
    src = re.sub(r'\{\{.*?\}\}', ' ', src, flags=re.S)
    return src


class _Visible(HTMLParser):
    """Text nodes and the shown attributes; an icon's ligature name is not a word."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces = []
        self._icon = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if re.search(r'\bmaterial-(icons|symbols)', attrs.get('class') or ''):
            self._icon = tag
        for k in ATTRS:
            if attrs.get(k):
                self.pieces.append(attrs[k])

    def handle_startendtag(self, tag, attrs):
        for k, v in attrs:
            if k in ATTRS and v:
                self.pieces.append(v)

    def handle_endtag(self, tag):
        if tag == self._icon:
            self._icon = None

    def handle_data(self, data):
        if data.strip() and not self._icon:
            self.pieces.append(data)


def visible_text(path):
    p = _Visible()
    p.feed(_strip(path.read_text()))
    p.close()
    return [' '.join(t.split()) for t in p.pieces]


def _allowed(name, text):
    return any(f == name and phrase.lower() in text.lower() for f, phrase in ALLOW)


def _hits(path):
    name = str(path.relative_to(TEMPLATES))
    # The pieces read on together, so a phrase split across tags is seen too.
    text = ' '.join(visible_text(path))
    hits = []
    for label, pat in NEVER.items():
        for m in re.finditer(pat, text, flags=re.I):
            around = text[max(0, m.start() - 40):m.end() + 40]
            if not _allowed(name, around):
                hits.append((label, around))
    return hits


TEMPLATE_FILES = sorted(TEMPLATES.rglob('*.html'))


@pytest.mark.unit
@pytest.mark.parametrize('path', TEMPLATE_FILES, ids=lambda p: str(p.relative_to(TEMPLATES)))
def test_no_never_say_words(path):
    hits = _hits(path)
    assert not hits, '\n'.join(f'{label}: ...{around}...' for label, around in hits)


@pytest.mark.unit
def test_the_reader_sees_text_but_not_code():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / 't.html'
        f.write_text(
            '<p title="Your password">Hello</p>'
            '<script>const server = 1;</script><style>.central{}</style>'
            '<!-- central --> {# server #} {% if central %}{{ password }}{% endif %}'
            '<i class="material-icons">logout</i><span>Lock</span>'
            '<input placeholder="Choose a passphrase">')
        assert visible_text(f) == ['Your password', 'Hello', 'Lock', 'Choose a passphrase']
