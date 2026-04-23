import csv
import time
import json
import sys
import os
import uuid
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException

from urllib.parse import urlparse, urlunparse
import re
import glob
from webdriver_manager.chrome import ChromeDriverManager
from tqdm import tqdm
import trafilatura


csv.field_size_limit(sys.maxsize)

BASE_URL = "https://ground.news"
OUTPUT_DIR = "json"
DEBUG_DIR = "debug"
MAX_TOPICS = 5
HOMEPAGE_SCAN_LIMIT = 150

SOURCE_CSV_FILE = os.path.join("dataset", "sources.csv")
SOURCE_CSV_FIELDS = [
    "story_id", "story_url", "source_news_title", "source_news_link", "source_text", "source_bias"
]


def is_ground_news_url(url):
    return "ground.news" in (url or "")


def is_internal_source_page(url):
    url = url or ""
    return (
        "ground.news/interest/" in url
        or "ground.news/article/" in url
        or "ground.news/source/" in url
    )


def pick_best_source_link(card):
    candidates = []
    try:
        anchors = card.find_elements(By.XPATH, ".//a[@href]")
    except Exception:
        anchors = []

    for a in anchors:
        try:
            href = a.get_attribute("href")
            text = (a.text or "").strip().lower()
            aria = (a.get_attribute("aria-label") or "").strip().lower()
            title = (a.get_attribute("title") or "").strip().lower()

            if not href or not href.startswith("http"):
                continue

            score = 0
            if not is_ground_news_url(href):
                score += 100
            if is_internal_source_page(href):
                score -= 100
            if "read article" in text or "read full article" in text:
                score += 40
            if "read article" in aria or "read full article" in aria:
                score += 40
            if "read article" in title or "read full article" in title:
                score += 40
            if text:
                score += 5

            candidates.append((score, href))
        except Exception:
            continue

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def normalize_url(url):
    if not url:
        return ""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if not path:
        path = "/"
    return urlunparse((scheme, netloc, path, "", "", ""))


def normalize_title(title):
    if not title:
        return ""
    title = title.lower().strip()
    title = re.sub(r"\b\d+%\s+(left|center|right)\s+coverage:.*$", "", title).strip()
    title = re.sub(r"\b\d+\s+sources\b.*$", "", title).strip()
    title = re.sub(r"[^a-z0-9\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def load_results():
    seen_urls = set()
    seen_titles = set()

    # Read sources.csv as the single source of truth
    if os.path.exists(SOURCE_CSV_FILE):
        with open(SOURCE_CSV_FILE, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                norm_url = normalize_url(row.get("story_url", ""))
                if norm_url:
                    seen_urls.add(norm_url)
        print(f"Loaded {len(seen_urls)} seen story URLs from sources.csv.")
    else:
        print("No sources.csv found — starting fresh.")

    # Also scan json/ files as a fallback safety net
    for path in glob.glob(os.path.join(OUTPUT_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get("metadata", {})
            norm_url = normalize_url(meta.get("url", ""))
            norm_title = normalize_title(meta.get("title", ""))
            if norm_url:
                seen_urls.add(norm_url)
            if norm_title:
                seen_titles.add(norm_title)
        except Exception:
            continue

    print(f"Total seen URLs (sources.csv + json/): {len(seen_urls)}")
    print(f"Total seen title fingerprints (json/): {len(seen_titles)}")
    return seen_urls, seen_titles


def append_sources_to_csv(story_data):
    if not story_data or "sources" not in story_data:
        return

    os.makedirs(os.path.dirname(SOURCE_CSV_FILE), exist_ok=True)
    write_header = not os.path.exists(SOURCE_CSV_FILE) or os.path.getsize(SOURCE_CSV_FILE) == 0

    with open(SOURCE_CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SOURCE_CSV_FIELDS)
        if write_header:
            writer.writeheader()

        for src in story_data["sources"]:
            text = src.get("text") or ""
            text = " ".join(text.split())  # collapses all whitespace/newlines to single spaces

            writer.writerow({
                "story_id": story_data["story_id"],
                "story_url": story_data["metadata"].get("url", ""),
                "source_news_title": src.get("news_title", ""),
                "source_news_link": src.get("news_link", ""),
                "source_text": text,
                "source_bias": src.get("bias", ""),
            })


def init_driver():
    options = webdriver.ChromeOptions()
    is_ci = os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS") or True

    if is_ci:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
        options.add_argument("--incognito")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    driver.set_page_load_timeout(45)
    return driver


def wait_for_page_ready(driver, timeout=20):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def safe_click(driver, element):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.5)
        element.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", element)
            return True
        except Exception:
            return False


def dump_debug(driver, name):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    try:
        with open(os.path.join(DEBUG_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        pass
    try:
        driver.save_screenshot(os.path.join(DEBUG_DIR, f"{name}.png"))
    except Exception:
        pass


def dismiss_modal_if_present(driver):
    selectors = [
        (By.CSS_SELECTOR, "[data-testid='onboarding-close-button']"),
        (By.CSS_SELECTOR, "button[aria-label='Close']"),
        (By.CSS_SELECTOR, "[data-testid='close-button']"),
    ]
    for by, sel in selectors:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((by, sel))
            )
            if safe_click(driver, btn):
                print("Dismissed modal.")
                time.sleep(0.5)
                return
        except Exception:
            pass


def get_text_safe(driver, selectors, default=""):
    for by, sel in selectors:
        try:
            el = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((by, sel))
            )
            txt = el.text.strip()
            if txt:
                return txt
        except Exception:
            continue
    return default


def scroll_homepage_for_story_cards(driver):
    last_height = 0
    stable_rounds = 0
    for i in range(8):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_height = new_height
        if stable_rounds >= 2:
            break
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)


def collect_latest_stories(driver):
    driver.get(BASE_URL)
    wait_for_page_ready(driver)
    print("Homepage loaded.")
    dismiss_modal_if_present(driver)

    selectors = [
        (By.XPATH, "//a[@data-dd-action-name='article-card-click']"),
        (By.XPATH, "//main//a[contains(@href, '/article/')]"),
    ]

    found_any = False
    for by, sel in selectors:
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((by, sel))
            )
            found_any = True
            break
        except Exception:
            continue

    if not found_any:
        print("Timed out waiting for homepage stories.")
        dump_debug(driver, "homepage_load_failed")
        return []

    scroll_homepage_for_story_cards(driver)

    raw_links = driver.find_elements(By.XPATH, "//a[@data-dd-action-name='article-card-click']")
    if not raw_links:
        raw_links = driver.find_elements(By.XPATH, "//main//a[contains(@href, '/article/')]")

    print(f"DEBUG: found {len(raw_links)} raw homepage article links")

    stories = []
    seen_urls = set()

    for el in raw_links:
        try:
            href = normalize_url(el.get_attribute("href"))
            if not href or "/article/" not in href:
                continue
            if href in seen_urls:
                continue

            title = (el.text or "").strip()
            if not title:
                try:
                    title = el.find_element(By.XPATH, ".//h1|.//h2|.//h3|.//h4|.//span").text.strip()
                except Exception:
                    title = ""
            if not title:
                title = href.rstrip("/").split("/")[-1].replace("-", " ").strip()

            stories.append({"title": title, "url": href})
            seen_urls.add(href)

            if len(stories) >= HOMEPAGE_SCAN_LIMIT:
                break

        except StaleElementReferenceException:
            continue
        except Exception:
            continue

    print(f"Collected {len(stories)} candidate homepage stories.")
    return stories


def expand_all_sources(driver, max_clicks=100):
    click_count = 0
    while click_count < max_clicks:
        clicked = False
        candidates = [
            (By.ID, "more-stories"),
            (By.ID, "more_stories"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'more stories')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'load more')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'show more')]"),
        ]
        for by, sel in candidates:
            try:
                btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((by, sel))
                )
                if safe_click(driver, btn):
                    click_count += 1
                    clicked = True
                    time.sleep(1.5)
                    driver.execute_script("window.scrollBy(0, 1000);")
                    time.sleep(1)
                    print(f"Loaded more sources batch {click_count}")
                    break
            except Exception:
                continue
        if not clicked:
            break
    print(f"Finished loading source batches: {click_count}")


def extract_source_cards(driver):
    selectors = [
        (By.XPATH, "//article[.//a[contains(@href, 'http')]]"),
        (By.XPATH, "//a[contains(@href, 'http') and .//h4]/ancestor::*[self::div or self::article][1]"),
    ]
    cards = []
    seen = set()
    for by, sel in selectors:
        try:
            found = driver.find_elements(by, sel)
            for el in found:
                try:
                    key = el.get_attribute("id") or str(id(el))
                    if key not in seen:
                        seen.add(key)
                        cards.append(el)
                except Exception:
                    continue
        except Exception:
            continue
    return cards


def extract_one_source_text(url):
    if not url or is_internal_source_page(url):
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        return trafilatura.extract(downloaded)
    except Exception:
        return None


def fetch_article_data(driver, url):
    try:
        driver.get(url)
        wait_for_page_ready(driver)
        dismiss_modal_if_present(driver)
        time.sleep(1)

        print(f"\nPage loaded: {url}")

        title = get_text_safe(driver, [
            (By.ID, "titleArticle"),
            (By.CSS_SELECTOR, "h1"),
            (By.XPATH, "//main//h1"),
            (By.XPATH, "//article//h1"),
            (By.XPATH, "//*[self::h1 or self::h2][string-length(normalize-space()) > 10]"),
        ], default="")

        if not title:
            title = url.rstrip("/").split("/")[-1].replace("-", " ").strip()
        if not title:
            raise Exception("Could not find article title")

        print(f"Title: {title}")

        safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)[:80].strip()
        story_id = f"{safe_title}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{str(uuid.uuid4())[:8]}"

        total_sources = get_text_safe(driver, [
            (By.XPATH, "//span[contains(text(), 'sources')]"),
            (By.XPATH, "//div[contains(., 'sources')]//span"),
        ], default="")

        leaning_left = get_text_safe(driver, [
            (By.XPATH, "//*[contains(text(), 'Left')]/following::span[1]"),
        ], default="")

        center = get_text_safe(driver, [
            (By.XPATH, "//*[contains(text(), 'Center')]/following::span[1]"),
        ], default="")

        leaning_right = get_text_safe(driver, [
            (By.XPATH, "//*[contains(text(), 'Right')]/following::span[1]"),
        ], default="")

        expand_all_sources(driver)

        source_cards = extract_source_cards(driver)
        print(f"Source cards found: {len(source_cards)}")

        sources = []
        seen_links = set()

        for card in tqdm(source_cards, desc="Processing sources", unit="source"):
            try:
                link = pick_best_source_link(card)
                if not link:
                    continue

                link = normalize_url(link)

                if is_internal_source_page(link):
                    print(f"  - Skipping internal Ground News page: {link}")
                    continue
                if link in seen_links:
                    continue
                seen_links.add(link)

                title_text = ""
                for xp in [".//h4", ".//h3", ".//h2", ".//*[@data-testid='source-title']"]:
                    try:
                        title_text = card.find_element(By.XPATH, xp).text.strip()
                        if title_text:
                            break
                    except Exception:
                        pass
                if not title_text:
                    title_text = (card.text or "").strip().split("\n")[0]

                bias = "unknown"
                try:
                    bias_candidates = card.find_elements(By.XPATH, ".//*[contains(@id, 'article-source-bias') or contains(@class, 'bias')]")
                    for b in bias_candidates:
                        txt = b.text.strip()
                        if txt:
                            bias = txt
                            break
                except Exception:
                    pass

                text = extract_one_source_text(link)
                if text is None:
                    print(f"  - Could not fetch article text: {link}")

                sources.append({
                    "news_title": title_text,
                    "news_link": link,
                    "bias": bias,
                    "text": text,
                })

            except Exception as e:
                print(f"  - Error extracting source: {e}")
                continue

        tqdm.write(f"Sources extracted: {len(sources)}")

        structured_data = {
            "story_id": story_id,
            "metadata": {
                "title": title,
                "timestamp": datetime.now().isoformat(),
                "url": url,
            },
            "bias_distribution": {
                "total_sources": total_sources,
                "leaning_left": leaning_left,
                "center": center,
                "leaning_right": leaning_right,
            },
            "sources": sources,
        }

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(OUTPUT_DIR, f"{story_id}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(structured_data, f, indent=2, ensure_ascii=False)

        append_sources_to_csv(structured_data)
        print(f"Saved: {out_path}")
        return structured_data

    except Exception as e:
        print(f"Error scraping {url}: {e}")
        dump_debug(driver, f"article_failure_{int(time.time())}")
        return None


def run_pipeline():
    seen_urls, seen_titles = load_results()

    driver = init_driver()
    try:
        stories = collect_latest_stories(driver)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if not stories:
        print("No homepage stories found.")
        return

    fresh_stories = []
    skipped_by_url = 0
    skipped_by_title = 0

    for s in stories:
        story_url = normalize_url(s["url"])
        story_title = normalize_title(s["title"])

        if story_url in seen_urls:
            skipped_by_url += 1
            continue
        if story_title and story_title in seen_titles:
            skipped_by_title += 1
            continue

        fresh_stories.append({
            "title": s["title"],
            "url": story_url,
            "title_key": story_title,
        })

    print(f"Homepage candidates collected: {len(stories)}")
    print(f"Skipped by URL: {skipped_by_url}")
    print(f"Skipped by title: {skipped_by_title}")
    print(f"Fresh homepage topics available: {len(fresh_stories)}")

    pending = fresh_stories[:MAX_TOPICS]

    if not pending:
        print("No new homepage topics found.")
        return

    print(f"Selected {len(pending)} new homepage topic(s):")
    for i, story in enumerate(pending, 1):
        print(f"[{i}] {story['title']}")
        print(f"    {story['url']}")

    success_count = 0

    for story in tqdm(pending, desc="Overall progress", unit="topic"):
        story_url = normalize_url(story["url"])
        story_title_key = normalize_title(story["title"])

        if story_url in seen_urls:
            print(f"Skipping already scraped by URL: {story_url}")
            continue
        if story_title_key in seen_titles:
            print(f"Skipping already scraped by title: {story['title']}")
            continue

        driver = init_driver()
        try:
            result = fetch_article_data(driver, story_url)
            if result:
                seen_urls.add(story_url)
                seen_titles.add(normalize_title(result["metadata"]["title"]))
                success_count += 1
        except KeyboardInterrupt:
            print("\nInterrupted.")
            try:
                driver.quit()
            except Exception:
                pass
            raise
        except Exception as e:
            print(f"Topic-level error for {story_url}: {e}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass

        time.sleep(1)

    print(f"\nDone. Scraped {success_count} new homepage topic(s) this run.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        driver = init_driver()
        try:
            fetch_article_data(driver, normalize_url(sys.argv[1]))
        finally:
            driver.quit()
    else:
        run_pipeline()