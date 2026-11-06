import csv
import json
import tempfile
import unittest
from pathlib import Path

from scraper import ScrapeError, Store, crawl, parse_category, parse_lot, parse_sitemap

BASE = 'https://www.bidspotter.co.uk'
CAT = BASE + '/en-gb/for-sale/woodworking/saws'
LOT = BASE + '/en-gb/auction-catalogues/seller/catalogue-id-test/lot-abc123'
INDEX = BASE + '/secondary_categories_sitemapindex'
CHILD = BASE + '/sitemap-secondarycategories.xml'


def xml(kind, urls):
    entry = 'sitemap' if kind == 'sitemapindex' else 'url'
    return f'<{kind} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
        f'<{entry}><loc>{u}</loc></{entry}>' for u in urls) + f'</{kind}>'


CATEGORY = f'''<h1>Saws For Sale</h1><article class="panel item">
<a href="{LOT}"><img alt="Saw"></a><a href="{LOT}?tracking=x">Saw</a></article>
<ul class="pagination-content" data-current-page-number="1" data-pages="2"></ul>
<aside class="recently-viewed"><a href="{LOT}-unrelated">Unrelated</a></aside>'''
LOT_HTML = '''<h1 class="header-lot-title">Precision saw &amp; stand</h1>
<p class="lot-number">12A</p><div class="segment" data-tab="description">
<p>Working saw.</p><p>Includes stand.</p></div>
<div class="segment" data-tab="auction"><h3>Workshop clearance</h3></div>
<div class="auctioneer-info"><p class="header"><a>Example Auctions</a></p></div>
<span id="current-bid-price"></span><span id="currency"></span>
<img data-zoom-url="https://cdn.globalauctionplatform.com/auctions-2026/a/images/saw.jpg">
<img src="https://cdn.globalauctionplatform.com/auctions-2026/a/images/saw.jpg?w=80">
<aside><img src="https://cdn.globalauctionplatform.com/auctions-2026/b/images/other.jpg?w=155"></aside>
<script>window.dataLayer.push({"lotId":"abc123","openingPrice":"125.00",
"auctioneer":"Example Auctions","lotCategory":"Saws","lotPrimaryCategory":"Woodworking",
"lotEndsFrom":"2026-11-08T10:00:00Z","auctionCity":"Leeds","auctionCountry":"United Kingdom",});</script>'''


class ParsingTests(unittest.TestCase):
    def test_sitemap_distinguishes_indexes_and_ignores_alternate_languages(self):
        self.assertEqual(parse_sitemap(xml('sitemapindex', [CHILD]), INDEX), ('sitemapindex', [CHILD]))
        body = xml('urlset', [CAT]).replace('</url>', '<link href="https://evil.test/"/></url>')
        self.assertEqual(parse_sitemap(body, CHILD), ('urlset', [CAT]))

    def test_challenge_or_unexpected_xml_fails_instead_of_empty_success(self):
        for body in ['<html><title>Human Verification</title></html>', '<nonsense/>']:
            with self.subTest(body=body), self.assertRaises(ScrapeError):
                parse_sitemap(body, INDEX)

    def test_category_deduplicates_lots_excludes_recommendations_and_follows_pagination(self):
        page = parse_category(CATEGORY, CAT)
        self.assertEqual(page['lots'], [LOT])
        self.assertEqual(page['next_url'], CAT + '?page=2')
        self.assertEqual(page['title'], 'Saws For Sale')

    def test_unrendered_category_is_not_recorded_as_empty(self):
        with self.assertRaises(ScrapeError):
            parse_category('<h1>Saws</h1><div id="results-listing" data-url="/search-filter"></div>', CAT)
        self.assertEqual(parse_category('<h1>Saws</h1><div>0 item(s)</div>', CAT)['lots'], [])

    def test_lot_keeps_full_description_and_unknown_price_separate_from_opening_price(self):
        data = parse_lot(LOT_HTML, LOT)
        self.assertEqual(data['title'], 'Precision saw & stand')
        self.assertEqual(data['lot_number'], '12A')
        self.assertEqual(data['description'], 'Working saw.\nIncludes stand.')
        self.assertEqual(data['opening_price'], '125.00')
        self.assertIsNone(data['current_bid'])
        self.assertIsNone(data['currency'])
        self.assertEqual(data['images'], ['https://cdn.globalauctionplatform.com/auctions-2026/a/images/saw.jpg'])
        self.assertEqual(data['auctioneer'], 'Example Auctions')
        self.assertEqual(data['end_date'], '2026-11-08T10:00:00Z')

    def test_missing_lot_content_fails(self):
        with self.assertRaises(ScrapeError):
            parse_lot('<h1>Human Verification</h1>', LOT)

    def test_current_bid_uses_visible_panel_not_hidden_confirmation(self):
        panel = '<div id="bidPanel"><span id="currentBid"><span id="price">350.00</span><span class="currency">GBP</span></span></div>'
        data = parse_lot(LOT_HTML + panel, LOT)
        self.assertEqual(data['current_bid'], '350.00')
        self.assertEqual(data['currency'], 'GBP')
        self.assertIsNone(parse_lot(LOT_HTML + panel.replace('350.00', 'None'), LOT)['current_bid'])


class CrawlTests(unittest.TestCase):
    def test_recursive_crawl_resumes_limits_and_exports_one_lot_with_both_categories(self):
        other = BASE + '/en-gb/for-sale/woodworking/tools'
        pages = {INDEX: xml('sitemapindex', [CHILD]), CHILD: xml('urlset', [CAT, other]),
                 CAT: CATEGORY, CAT + '?page=2': '<h1>Saws</h1><div>0 item(s)</div>',
                 other: CATEGORY.replace('data-pages="2"', 'data-pages="1"'), LOT: LOT_HTML}
        calls = []

        def fetch(url):
            calls.append(url)
            return pages[url]

        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp)) as store:
                crawl(store, fetch, INDEX, max_categories=1, max_pages=1, max_lots=1)
                self.assertEqual(store.summary()['lots'], 1)
                self.assertEqual(store.summary()['pending'], 2)
            first_calls = calls.count(LOT)
            with Store(Path(tmp)) as store:
                crawl(store, fetch, INDEX)
                store.export()
                self.assertEqual(store.summary()['pending'], 0)
            self.assertEqual(calls.count(LOT), first_calls)
            data = json.loads((Path(tmp) / 'lots.json').read_text())
            self.assertEqual(len(data), 1)
            self.assertEqual(set(data[0]['category_urls']), {CAT, other})
            with (Path(tmp) / 'lots.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]['lot_number'], '12A')

    def test_failed_lot_remains_retryable_and_crawl_reports_failure(self):
        pages = {INDEX: xml('urlset', [CAT]), CAT: CATEGORY.replace('data-pages="2"', 'data-pages="1"'),
                 LOT: '<title>Human Verification</title>'}
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)) as store:
            with self.assertRaises(ScrapeError):
                crawl(store, pages.__getitem__, INDEX)
            self.assertEqual(store.summary()['lots'], 0)
            self.assertEqual(store.summary()['failed'], 1)
            pages[LOT] = LOT_HTML
            crawl(store, pages.__getitem__, INDEX)
            self.assertEqual(store.summary()['lots'], 1)
            self.assertEqual(store.summary()['failed'], 0)

    def test_lot_limit_bounds_requests_even_when_a_lot_fails(self):
        second = LOT + '-second'
        category = CATEGORY.replace('data-pages="2"', 'data-pages="1"').replace(
            '</article>', f'<a href="{second}">Second lot</a></article>')
        pages = {INDEX: xml('urlset', [CAT]), CAT: category,
                 LOT: '<title>Human Verification</title>', second: LOT_HTML}
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)) as store:
            with self.assertRaises(ScrapeError):
                crawl(store, pages.__getitem__, INDEX, max_lots=1)
            self.assertEqual(store.summary()['lots'], 0)
            self.assertEqual(store.summary()['pending'], 1)

    def test_interrupt_preserves_completed_records_and_writes_exports(self):
        pages = {INDEX: xml('urlset', [CAT]), CAT: CATEGORY, LOT: LOT_HTML}

        def interrupted(url):
            if url.endswith('?page=2'):
                raise KeyboardInterrupt
            return pages[url]

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyboardInterrupt), Store(Path(tmp)) as store:
                crawl(store, interrupted, INDEX)
            self.assertEqual(len(json.loads((Path(tmp) / 'categories.json').read_text())), 1)
            with Store(Path(tmp)) as store:
                self.assertEqual(store.summary()['done'], 1)
                self.assertEqual(store.summary()['pending'], 2)


if __name__ == '__main__':
    unittest.main()
