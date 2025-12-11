import random
import requests
import threading
import logging
import sys
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

import warnings
warnings.filterwarnings("ignore")

# Configure logging
logger = logging.getLogger("robin.scrape")
logger.setLevel(logging.DEBUG)

# File handler - full debug logs
if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
    file_handler = logging.FileHandler("robin_debug.log", mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

# Console handler - info level
if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

# Define a list of rotating user agents.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (X11; Linux i686; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.3179.54",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.3179.54"
]

def check_tor_connection():
    """
    Check if Tor is running and accessible.
    """
    try:
        session = requests.Session()
        session.proxies = {
            "http": "socks5h://127.0.0.1:9050",
            "https": "socks5h://127.0.0.1:9050"
        }
        # Try to connect to Tor check service (use HTTPS)
        response = session.get("https://check.torproject.org/api/ip", timeout=15)
        if response.status_code == 200:
            data = response.json()
            logger.info(f"Tor connection OK. IP: {data.get('IP', 'unknown')}, IsTor: {data.get('IsTor', False)}")
            return True
    except Exception as e:
        logger.error(f"Tor connection check failed: {e}")
    return False


def get_tor_session():
    """
    Creates a requests Session with Tor SOCKS proxy and automatic retries.
    """
    logger.debug("Creating Tor session...")
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.proxies = {
        "http": "socks5h://127.0.0.1:9050",
        "https": "socks5h://127.0.0.1:9050"
    }
    logger.debug("Tor session created with SOCKS5 proxy on 127.0.0.1:9050")
    return session

def scrape_single(url_data, rotate=False, rotate_interval=5, control_port=9051, control_password=None):
    """
    Scrapes a single URL using a robust Tor session.
    Returns a tuple (url, scraped_text).
    """
    url = url_data['link']
    use_tor = ".onion" in url

    logger.debug(f"Scraping URL: {url} (use_tor={use_tor})")

    headers = {
        "User-Agent": random.choice(USER_AGENTS)
    }

    try:
        if use_tor:
            session = get_tor_session()
            # Reduced timeout to avoid long waits
            logger.debug(f"Fetching via Tor: {url}")
            response = session.get(url, headers=headers, timeout=20)
        else:
            # Fallback for clearweb if needed, though tool focuses on dark web
            logger.debug(f"Fetching directly (clearweb): {url}")
            response = requests.get(url, headers=headers, timeout=30)

        logger.debug(f"Response status: {response.status_code} for {url}")

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            # Clean up text: remove scripts/styles
            for script in soup(["script", "style"]):
                script.extract()
            text = soup.get_text(separator=' ')
            # Normalize whitespace
            text = ' '.join(text.split())
            scraped_text = f"{url_data['title']} - {text}"
            logger.info(f"Successfully scraped: {url[:60]}... ({len(scraped_text)} chars)")
        else:
            logger.warning(f"Non-200 status ({response.status_code}) for {url}")
            scraped_text = url_data['title']
    except requests.exceptions.ConnectTimeout as e:
        logger.error(f"Connection timeout for {url}: {e}")
        scraped_text = url_data['title']
    except requests.exceptions.ProxyError as e:
        logger.error(f"Proxy error (Tor not running?) for {url}: {e}")
        scraped_text = url_data['title']
    except Exception as e:
        logger.error(f"Error scraping {url}: {type(e).__name__}: {e}")
        scraped_text = url_data['title']

    return url, scraped_text

def scrape_multiple(urls_data, max_workers=5, progress_callback=None):
    """
    Scrapes multiple URLs concurrently using a thread pool.

    Args:
        urls_data: List of dicts with 'link' and 'title' keys
        max_workers: Number of concurrent threads
        progress_callback: Optional callback(url, status, current, total) for progress updates
    """
    logger.info(f"Starting scrape of {len(urls_data)} URLs with {max_workers} workers")
    total_urls = len(urls_data)

    # Check Tor connection first for .onion URLs
    has_onion = any(".onion" in url_data.get('link', '') for url_data in urls_data)
    if has_onion:
        logger.info("Detected .onion URLs, checking Tor connection...")
        if progress_callback:
            progress_callback("Checking Tor connection...", "checking", 0, total_urls)
        tor_ok = check_tor_connection()
        if not tor_ok:
            logger.error("Tor is NOT running! .onion URLs will fail. Start Tor with: brew services start tor (macOS) or sudo systemctl start tor (Linux)")
            if progress_callback:
                progress_callback("Tor NOT running!", "error", 0, total_urls)

    results = {}
    max_chars = 2000  # Increased limit slightly for better context
    success_count = 0
    fail_count = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {
            executor.submit(scrape_single, url_data): url_data
            for url_data in urls_data
        }
        for future in as_completed(future_to_url):
            url_data = future_to_url[future]
            url = url_data.get('link', 'unknown')
            try:
                result_url, content = future.result()
                if len(content) > max_chars:
                    content = content[:max_chars] + "...(truncated)"
                results[result_url] = content
                completed += 1

                # Determine if scrape was successful (got actual content vs just title)
                if len(content) > len(url_data.get('title', '')) + 10:
                    success_count += 1
                    status = "success"
                else:
                    fail_count += 1
                    status = "failed"

                if progress_callback:
                    progress_callback(url, status, completed, total_urls)

            except Exception as e:
                logger.error(f"Future failed: {e}")
                completed += 1
                fail_count += 1
                if progress_callback:
                    progress_callback(url, "error", completed, total_urls)
                continue

    logger.info(f"Scraping complete: {success_count} succeeded, {fail_count} failed out of {total_urls} URLs")
    return results
