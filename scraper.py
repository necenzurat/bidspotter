#!/usr/bin/env python3
"""Resumable BidSpotter category and lot scraper. See README.md for usage."""

import argparse
import csv
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

BASE = 'https://www.bidspotter.co.uk'
DEFAULT_SITEMAP = BASE + '/secondary_categories_sitemapindex'
LOG = logging.getLogger('bidspotter')


class ScrapeError(Exception):
    pass


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def text(node):
    return (clean(node.get_text(' ', strip=True)) or None) if node else None


def site_url(value, base=BASE, page=False):
    parts = urlsplit(urljoin(base, value))
    if parts.scheme not in ('http', 'https') or parts.hostname not in ('www.bidspotter.co.uk', 'bidspotter.co.uk'):
        raise ScrapeError(f'Unexpected off-site URL: {value}')
    query = parse_qs(parts.query)
    query = urlencode({'page': int(query['page'][0])}) if page and 'page' in query else ''
    return urlunsplit(('https', 'www.bidspotter.co.uk', parts.path.rstrip('/'), query, ''))


def check_content(body):
    soup = BeautifulSoup(body, 'html.parser')
    title = text(soup.title) or ''
    if re.search(r'human verification|access denied|just a moment|robot check', title, re.I) or soup.select_one('#captcha-container'):
        raise ScrapeError('Human verification required. Use --transport browser and complete it in the opened window.')
    return soup


def parse_sitemap(body, url):
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ScrapeError(f'Expected sitemap XML at {url}') from exc
    kind = root.tag.split('}')[-1]
    if kind not in ('sitemapindex', 'urlset'):
        raise ScrapeError(f'Expected sitemapindex or urlset at {url}; got {kind}')
    urls = []
    for entry in root:
        for child in entry:
            if child.tag.split('}')[-1] == 'loc' and child.text:
                target = site_url(child.text.strip(), url)
                if target not in urls:
                    urls.append(target)
    if not urls:
        raise ScrapeError(f'Sitemap has no URLs: {url}')
    return kind, urls


def parse_category(body, url):
    soup = check_content(body)
    # Limit extraction to result cards; navigation/recently viewed lots are unrelated.
    cards = soup.select('article.panel.item, .lot-single, [data-testid="lot-card"]')
    lots = []
    for card in cards:
        for anchor in card.select('a[href*="/lot-"]'):
            target = site_url(anchor['href'], url)
            if target not in lots:
                lots.append(target)
    if not lots and not re.search(r'\b0\s+item\(s\)|no\s+(?:lots|results|items)\s+(?:found|available|match)', soup.get_text(' ', strip=True), re.I):
        raise ScrapeError(f'No result cards or explicit empty result at {url}; page may not be rendered.')
    current = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
    pagination = soup.select_one('.pagination-content[data-pages]')
    next_url = None
    if pagination and int(pagination['data-pages']) > current:
        next_url = site_url(urlsplit(url)._replace(query=urlencode({'page': current + 1})).geturl(), page=True)
    else:
        anchor = soup.select_one('a[rel~="next"][href]')
        if anchor:
            candidate = site_url(anchor['href'], url, page=True)
            if urlsplit(candidate).path == urlsplit(url).path and int(parse_qs(urlsplit(candidate).query).get('page', ['1'])[0]) > current:
                next_url = candidate
    return {'title': text(soup.h1), 'lots': lots, 'next_url': next_url}


def parse_lot(body, url):
    soup = check_content(body)
    title = text(soup.select_one('h1.header-lot-title, h1'))
    desc = soup.select_one('div[data-tab="description"], [itemprop="description"], .lot-description')
    if not title or not desc or not text(desc) or '{{' in title:
        raise ScrapeError(f'Missing lot title or full description at {url}')
    # Read string values without evaluating the site's JavaScript. Trailing commas are common.
    analytics = {}
    for script in soup.find_all('script'):
        source = script.string or ''
        if 'dataLayer.push' in source and '"lotId"' in source:
            for match in re.finditer(r'"(\w+)"\s*:\s*("(?:[^"\\]|\\.)*")', source):
                analytics[match[1]] = json.loads(match[2])
    images = []
    for img in soup.select('img[data-zoom-url], .lot-images img, .lot-image img'):
        value = img.get('data-zoom-url') or img.get('data-src') or img.get('src')
        if value:
            image = urlsplit(urljoin(url, value))._replace(query='', fragment='').geturl()
            if image not in images:
                images.append(image)
    auction = soup.select_one('div[data-tab="auction"] h3')
    catalogue = url.rsplit('/lot-', 1)[0]
    description = '\n'.join(line.strip() for line in desc.get_text('\n', strip=True).splitlines() if line.strip())
    bid = text(soup.select_one('#bidPanel #currentBid #price'))
    if bid and (bid.casefold() in ('none', 'no bid', 'nobid', 'n/a', '-') or not re.search(r'\d', bid)):
        bid = None
    currency = next((text(node) for node in soup.select('#bidPanel #currentBid .currency, #bidPanel .currency-symbol') if text(node)), None)
    return {
        'url': url, 'lot_id': url.rsplit('/lot-', 1)[-1],
        'lot_number': text(soup.select_one('.lot-details .lot-number, p.lot-number, .lot-number')),
        'title': title, 'description': description, 'images': images,
        'auctioneer': analytics.get('auctioneer') or text(soup.select_one('.auctioneer-info .header')),
        'auction_title': text(auction) or unquote(analytics.get('catalogueName', '')) or None,
        'auction_url': catalogue, 'auction_type': analytics.get('auctionType'),
        'category': analytics.get('lotCategory'), 'primary_category': analytics.get('lotPrimaryCategory'),
        'city': analytics.get('auctionCity'), 'country': analytics.get('auctionCountry'),
        'end_date': analytics.get('lotEndsFrom'), 'start_date': analytics.get('lotStartDate'),
        'status': analytics.get('lotStatus'),
        'opening_price': analytics.get('openingPrice'),
        'current_bid': bid, 'currency': currency,
        'scraped_at': datetime.now(timezone.utc).isoformat(),
    }


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'state.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS categories (url TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lots (url TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memberships (lot TEXT, category TEXT, PRIMARY KEY(lot, category));
            CREATE TABLE IF NOT EXISTS jobs (
                url TEXT PRIMARY KEY, kind TEXT NOT NULL, category TEXT NOT NULL,
                page INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending', error TEXT);
        ''')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            self.export()
        finally:
            self.db.close()

    def enqueue(self, url, kind, category):
        page = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
        self.db.execute('INSERT OR IGNORE INTO jobs(url,kind,category,page) VALUES (?,?,?,?)', (url, kind, category, page))

    def summary(self):
        result = {key: self.db.execute(f'SELECT count(*) FROM {key}').fetchone()[0] for key in ('categories', 'lots')}
        for key in ('pending', 'failed', 'done'):
            result[key] = self.db.execute('SELECT count(*) FROM jobs WHERE status=?', (key,)).fetchone()[0]
        return result

    def export(self):
        for table in ('categories', 'lots'):
            records = []
            for row in self.db.execute(f'SELECT url,data FROM {table} ORDER BY url'):
                data = json.loads(row['data'])
                if table == 'lots':
                    data['category_urls'] = [r[0] for r in self.db.execute('SELECT category FROM memberships WHERE lot=? ORDER BY category', (row['url'],))]
                records.append(data)
            temp = self.directory / f'{table}.json.tmp'
            temp.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temp.replace(self.directory / f'{table}.json')
            if records:
                fields = list(dict.fromkeys(k for record in records for k in record))
                temp = self.directory / f'{table}.csv.tmp'
                with temp.open('w', newline='', encoding='utf-8-sig') as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    for record in records:
                        # Prevent spreadsheet formula execution when opening untrusted site text.
                        row = {}
                        for k, v in record.items():
                            v = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                            row[k] = "'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v
                        writer.writerow(row)
                temp.replace(self.directory / f'{table}.csv')
        (self.directory / 'summary.json').write_text(json.dumps(self.summary(), indent=2) + '\n')


def crawl(store, fetch, sitemap=DEFAULT_SITEMAP, *, max_categories=0, max_pages=0, max_lots=0, category_filter=None, categories_only=False):
    seen, categories, queue = set(), [], [site_url(sitemap)]
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            kind, urls = parse_sitemap(fetch(url), url)
        except ScrapeError:
            if hasattr(fetch, 'invalidate'):
                fetch.invalidate(url)
            raise
        if kind == 'sitemapindex':
            queue.extend(urls)
        else:
            categories.extend(u for u in urls if '/en-gb/for-sale/' in u and u not in categories)
    if not categories:
        raise ScrapeError('No English category URLs found in the sitemap.')
    for url in categories:
        parts = urlsplit(url).path.split('/')
        data = {'url': url, 'name': unquote(parts[-1]).replace('-', ' '), 'primary_category': parts[-2].replace('-', ' ')}
        store.db.execute('INSERT OR IGNORE INTO categories VALUES (?,?)', (url, json.dumps(data)))
        store.enqueue(url, 'category', url)
    allowed = [u for u in categories if not category_filter or category_filter.casefold() in u.casefold()]
    if max_categories:
        allowed = allowed[:max_categories]
    if not allowed:
        raise ScrapeError(f'No categories match {category_filter!r}.')
    allowed = set(allowed)
    store.db.commit()
    LOG.info('Discovered %d categories; selected %d', len(categories), len(allowed))
    if categories_only:
        store.export()
        return
    category_jobs, lot_jobs, queued = deque(), deque(), set()

    def offer(job):
        if job['url'] in queued or job['status'] == 'done':
            return
        if job['kind'] == 'category':
            if job['category'] not in allowed or (max_pages and job['page'] > max_pages):
                return
            category_jobs.append(job)
        else:
            memberships = {r[0] for r in store.db.execute('SELECT category FROM memberships WHERE lot=?', (job['url'],))}
            if not memberships.intersection(allowed):
                return
            lot_jobs.append(job)
        queued.add(job['url'])

    def schedule(url, kind, category):
        store.enqueue(url, kind, category)
        if url not in queued:
            offer(store.db.execute('SELECT * FROM jobs WHERE url=?', (url,)).fetchone())

    # Scan persisted work once; each newly discovered job is offered once more.
    # Selecting every next page is O(1), avoiding repeated whole-queue sorts.
    for job in store.db.execute("SELECT * FROM jobs WHERE status != 'done' ORDER BY rowid"):
        offer(job)
    failures, lot_count = [], 0
    while category_jobs or lot_jobs:
        if category_jobs:
            job = category_jobs.popleft()
        elif max_lots and lot_count >= max_lots:
            break
        else:
            job = lot_jobs.popleft()
        url = job['url']
        LOG.info('%s %s', job['kind'], url)
        if job['kind'] == 'lot':
            lot_count += 1
        try:
            body = fetch(url)
            with store.db:
                if job['kind'] == 'category':
                    data = parse_category(body, url)
                    if job['page'] == 1:
                        existing = json.loads(store.db.execute('SELECT data FROM categories WHERE url=?', (job['category'],)).fetchone()[0])
                        existing['title'] = data['title']
                        store.db.execute('UPDATE categories SET data=? WHERE url=?', (json.dumps(existing), job['category']))
                    for lot in data['lots']:
                        store.db.execute('INSERT OR IGNORE INTO memberships VALUES (?,?)', (lot, job['category']))
                        schedule(lot, 'lot', job['category'])
                    if data['next_url']:
                        schedule(data['next_url'], 'category', job['category'])
                else:
                    data = parse_lot(body, url)
                    store.db.execute('INSERT OR REPLACE INTO lots VALUES (?,?)', (url, json.dumps(data, ensure_ascii=False)))
                store.db.execute("UPDATE jobs SET status='done', error=NULL WHERE url=?", (url,))
        except (ScrapeError, ValueError) as exc:
            if hasattr(fetch, 'invalidate'):
                fetch.invalidate(url)
            with store.db:
                store.db.execute("UPDATE jobs SET status='failed', error=? WHERE url=?", (str(exc), url))
            failures.append(url)
            LOG.error('%s: %s', url, exc)
    store.export()
    LOG.info('Saved: %s', store.summary())
    if failures:
        raise ScrapeError(f'{len(failures)} page(s) failed. Rerun the same command to retry; details are in state.sqlite3.')


class Fetcher:
    def __init__(self, directory, transport='firecrawl', delay=2, retries=2, timeout=120, headless=False, wait=2, human_timeout=180):
        self.cache = Path(directory) / 'pages'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.transport, self.delay, self.retries, self.timeout = transport, delay, retries, timeout
        self.wait, self.human_timeout = wait, human_timeout
        self.last_request, self.browser, self.playwright = 0, None, None
        if transport == 'firecrawl' and not shutil.which('firecrawl'):
            raise ScrapeError('Firecrawl CLI missing. Install it with npm install -g firecrawl-cli and run firecrawl login, or use --transport browser.')
        if transport == 'browser':
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            try:
                self.browser = self.playwright.chromium.launch_persistent_context(
                    str(Path(directory).resolve() / 'browser-profile'), channel='chrome', headless=headless)
                self.page = self.browser.new_page()
            except Exception:
                self.playwright.stop()
                raise

    def path(self, url):
        return self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.html')

    def invalidate(self, url):
        self.path(url).unlink(missing_ok=True)

    def __call__(self, url):
        target = self.path(url)
        if target.exists():
            return target.read_text(encoding='utf-8')
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                body = self.firecrawl(url) if self.transport == 'firecrawl' else self.browser_fetch(url)
                if not re.match(r'\s*(?:<\?xml|<(?:sitemapindex|urlset)\b)', body):
                    check_content(body)
                temp = target.with_suffix('.tmp')
                temp.write_text(body, encoding='utf-8')
                temp.replace(target)
                return body
            except Exception as exc:
                if attempt == self.retries:
                    raise ScrapeError(f'Fetch failed for {url}: {exc}') from exc
                LOG.warning('Retry %d/%d: %s (%s)', attempt + 1, self.retries, url, clean(exc)[:200])
                time.sleep(min(30, 2 ** (attempt + 1)))

    def firecrawl(self, url):
        # Use the CLI's configured credentials without reading or copying them.
        result = subprocess.run(['firecrawl', 'scrape', url, '--format', 'rawHtml', '--json',
                                 '--max-age', '0', '--wait-for', str(int(self.wait * 1000))],
                                capture_output=True, text=True, timeout=self.timeout)
        if result.returncode:
            raise ScrapeError(clean(result.stderr)[:300] or 'Firecrawl request failed')
        try:
            data = json.loads(result.stdout)
            data = data.get('data', data)
            status = data.get('metadata', {}).get('statusCode', 200)
            if status >= 400:
                raise ScrapeError(f'Firecrawl returned HTTP {status}')
            body = data.get('rawHtml') or data.get('html')
            if not body:
                raise ScrapeError('Firecrawl returned no HTML.')
            return body
        except json.JSONDecodeError as exc:
            raise ScrapeError('Firecrawl returned invalid JSON.') from exc

    def browser_fetch(self, url):
        response = self.page.goto(url, wait_until='domcontentloaded', timeout=self.timeout * 1000)
        self.page.wait_for_timeout(self.wait * 1000)
        deadline = time.monotonic() + self.human_timeout
        prompted = False
        while True:
            try:
                body = self.page.content()
                check_content(body)
                break
            except ScrapeError:
                if time.monotonic() >= deadline:
                    raise ScrapeError('Human verification timed out. Complete it in Chrome, then rerun.')
                if not prompted:
                    LOG.warning('Complete human verification in the opened Chrome window. Waiting up to %ss.', self.human_timeout)
                    prompted = True
                self.page.wait_for_timeout(2000)
        # XML documents are displayed inside Chrome's XML viewer.
        xml = self.page.locator('#webkit-xml-viewer-source-xml')
        if xml.count():
            return xml.evaluate('(el) => el.innerHTML')
        if response and response.status >= 400 and not prompted:
            raise ScrapeError(f'HTTP {response.status}')
        if '/for-sale/' in url:
            self.page.wait_for_function("document.querySelector('article.panel.item, .lot-single') || /0\\s+item\\(s\\)|no (lots|results|items) (found|available|match)/i.test(document.body.innerText)", timeout=self.timeout * 1000)
        return self.page.content()

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('Must be zero or greater.')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sitemap', default=DEFAULT_SITEMAP)
    parser.add_argument('--output', type=Path, default=Path('data'))
    parser.add_argument('--transport', choices=['firecrawl', 'browser'], default='firecrawl')
    parser.add_argument('--category', help='Only URLs containing this text, e.g. woodworking/saws')
    parser.add_argument('--max-categories', type=nonnegative, default=0, help='0 = all')
    parser.add_argument('--max-pages', type=nonnegative, default=0, help='Pages per category; 0 = all')
    parser.add_argument('--max-lots', type=nonnegative, default=0, help='Lot pages attempted per run, including failures; 0 = all')
    parser.add_argument('--categories-only', action='store_true')
    parser.add_argument('--export-only', action='store_true', help='Export saved state without network calls')
    parser.add_argument('--delay', type=float, default=2, help='Seconds between requests')
    parser.add_argument('--retries', type=nonnegative, default=2)
    parser.add_argument('--timeout', type=nonnegative, default=120)
    parser.add_argument('--wait', type=float, default=2, help='Seconds to allow JavaScript rendering')
    parser.add_argument('--headless', action='store_true', help='Browser mode only; cannot complete verification manually')
    parser.add_argument('--human-timeout', type=nonnegative, default=180)
    args = parser.parse_args(argv)
    if args.delay < 0 or args.wait < 0 or not args.timeout:
        parser.error('Delay/wait must be nonnegative and timeout must be positive.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    fetch = None
    try:
        with Store(args.output) as store:
            if args.export_only:
                store.export()
                LOG.info('%s', store.summary())
                return 0
            fetch = Fetcher(args.output, args.transport, args.delay, args.retries, args.timeout, args.headless, args.wait, args.human_timeout)
            crawl(store, fetch, args.sitemap, max_categories=args.max_categories, max_pages=args.max_pages,
                  max_lots=args.max_lots, category_filter=args.category, categories_only=args.categories_only)
        return 0
    except KeyboardInterrupt:
        LOG.warning('Interrupted. Saved work can be resumed with the same command.')
        return 130
    except Exception as exc:
        LOG.error('%s', exc)
        return 1
    finally:
        if fetch:
            fetch.close()


if __name__ == '__main__':
    sys.exit(main())
